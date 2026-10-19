"""transplant-archive-rows.py

One-time operational script: transplant archive-scraper outbound sentinel rows
(pst_source LIKE 'archive:%') from a pre-rebuild SQLite backup into a
freshly-rebuilt staging DB.

Preserves message-level identity (message_id, dedup_key) while reassigning
integer primary keys.  Also remaps message_recipients and message_attachments
FKs and rebuilds FTS5 rowids.

Usage:
    uv run python scripts/transplant-archive-rows.py \\
        --source-db <backup.db> \\
        --target-db <stage.db> \\
        [--dry-run]

Output: single JSON object to stdout; exit 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Core transplant logic
# ---------------------------------------------------------------------------


def transplant(
    source_path: Path,
    target_path: Path,
    dry_run: bool = False,
) -> dict:
    """Transplant archive rows (pst_source LIKE 'archive:%') from source_path
    into target_path.

    Parameters
    ----------
    source_path:
        Path to the read-only source SQLite DB (pre-rebuild backup).
    target_path:
        Path to the writable target SQLite DB (freshly-rebuilt stage).
    dry_run:
        If True, open a transaction, run all INSERTs, collect the plan JSON,
        then ROLLBACK unconditionally.  The target DB is left unchanged.

    Returns
    -------
    dict
        JSON-serialisable summary dict (see module docstring for shape).
    """
    src_con = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    src_con.row_factory = sqlite3.Row

    tgt_con = sqlite3.connect(str(target_path))
    tgt_con.execute("PRAGMA foreign_keys = OFF;")  # we manage FK integrity ourselves

    result: dict = {
        "mode": "dry-run" if dry_run else "commit",
        "rows_transplanted": 0,
        "old_to_new_id_count": 0,
        "recipients_remapped": 0,
        "attachments_remapped": 0,
        "fts_rows_inserted": 0,
        "result": "ok",
    }

    try:
        tgt_con.execute("BEGIN IMMEDIATE")

        # ------------------------------------------------------------------
        # Step A: fetch archive messages from source (pst_source LIKE 'archive:%')
        # ------------------------------------------------------------------
        src_rows = src_con.execute(
            """
            SELECT
                id,
                subject,
                sender_name,
                sender_email,
                sent_at,
                received_at,
                body_text,
                message_id,
                in_reply_to,
                references_header,
                pst_source,
                folder_path,
                logical_locator,
                source_input_mode,
                source_requested_from,
                dedup_key,
                dedup_basis,
                recipient_search_text,
                attachment_search_text,
                created_at
            FROM messages
            WHERE pst_source LIKE 'archive:%'
            """
        ).fetchall()

        old_to_new: dict[int, int] = {}

        # ------------------------------------------------------------------
        # Step B: INSERT into target.messages, collect old→new id map
        # ------------------------------------------------------------------
        for row in src_rows:
            old_id: int = row["id"]

            cur = tgt_con.execute(
                """
                INSERT INTO messages (
                    subject,
                    sender_name,
                    sender_email,
                    sent_at,
                    received_at,
                    body_text,
                    message_id,
                    in_reply_to,
                    references_header,
                    pst_source,
                    folder_path,
                    logical_locator,
                    source_input_mode,
                    source_requested_from,
                    dedup_key,
                    dedup_basis,
                    recipient_search_text,
                    attachment_search_text,
                    created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    row["subject"],
                    row["sender_name"],
                    row["sender_email"],
                    row["sent_at"],
                    row["received_at"],
                    row["body_text"],
                    row["message_id"],
                    row["in_reply_to"],
                    row["references_header"],
                    row["pst_source"],
                    row["folder_path"],
                    row["logical_locator"],
                    row["source_input_mode"],
                    row["source_requested_from"],
                    row["dedup_key"],
                    row["dedup_basis"],
                    row["recipient_search_text"],
                    row["attachment_search_text"],
                    row["created_at"],
                ),
            )
            new_id: int = cur.lastrowid  # type: ignore[assignment]
            old_to_new[old_id] = new_id

        result["rows_transplanted"] = len(src_rows)
        result["old_to_new_id_count"] = len(old_to_new)

        # ------------------------------------------------------------------
        # Step C: remap message_recipients
        # ------------------------------------------------------------------
        recipients_remapped = 0
        for old_id, new_id in old_to_new.items():
            rec_rows = src_con.execute(
                """
                SELECT recipient_type, display_name, email
                FROM message_recipients
                WHERE message_id = ?
                """,
                (old_id,),
            ).fetchall()

            for rec in rec_rows:
                tgt_con.execute(
                    """
                    INSERT INTO message_recipients
                        (message_id, recipient_type, display_name, email)
                    VALUES (?, ?, ?, ?)
                    """,
                    (new_id, rec["recipient_type"], rec["display_name"], rec["email"]),
                )
                recipients_remapped += 1

        result["recipients_remapped"] = recipients_remapped

        # ------------------------------------------------------------------
        # Step D: remap message_attachments
        # ------------------------------------------------------------------
        attachments_remapped = 0
        for old_id, new_id in old_to_new.items():
            att_rows = src_con.execute(
                """
                SELECT attachment_name, content_hash, size_bytes,
                       mime_type, storage_path
                FROM message_attachments
                WHERE message_id = ?
                """,
                (old_id,),
            ).fetchall()

            for att in att_rows:
                tgt_con.execute(
                    """
                    INSERT INTO message_attachments (
                        message_id, attachment_name, content_hash,
                        size_bytes, mime_type, storage_path
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id,
                        att["attachment_name"],
                        att["content_hash"],
                        att["size_bytes"],
                        att["mime_type"],
                        att["storage_path"],
                    ),
                )
                attachments_remapped += 1

        result["attachments_remapped"] = attachments_remapped

        # ------------------------------------------------------------------
        # Step E: rebuild FTS5 (rowid = new_id)
        # ------------------------------------------------------------------
        fts_rows_inserted = 0
        for _old_id, new_id in old_to_new.items():
            # Re-fetch from target to get the row we just inserted
            msg = tgt_con.execute(
                """
                SELECT subject, sender_name, sender_email,
                       recipient_search_text, body_text, attachment_search_text
                FROM messages
                WHERE id = ?
                """,
                (new_id,),
            ).fetchone()

            if msg is None:
                raise RuntimeError(
                    f"Inserted message with new_id={new_id} not found in target — "
                    "transaction integrity broken."
                )

            # Build sender search text matching how storage.py does it:
            # "Name <email>" or just email
            sender_name = msg[1] or ""
            sender_email = msg[2] or ""
            if sender_name:
                sender_text = f"{sender_name} <{sender_email}>"
            else:
                sender_text = sender_email

            tgt_con.execute(
                """
                INSERT INTO message_fts (
                    rowid,
                    message_rowid,
                    subject,
                    sender,
                    recipients,
                    body_text,
                    attachment_names
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    new_id,
                    msg[0] or "",  # subject
                    sender_text,  # sender
                    msg[3] or "",  # recipient_search_text
                    msg[4] or "",  # body_text
                    msg[5] or "",  # attachment_search_text
                ),
            )
            fts_rows_inserted += 1

        result["fts_rows_inserted"] = fts_rows_inserted

        # ------------------------------------------------------------------
        # Commit or rollback based on dry_run flag
        # ------------------------------------------------------------------
        if dry_run:
            tgt_con.execute("ROLLBACK")
        else:
            tgt_con.execute("COMMIT")

    except Exception as exc:
        with contextlib.suppress(Exception):
            tgt_con.execute("ROLLBACK")
        result["result"] = f"rollback-on-error: {exc}"
    finally:
        src_con.close()
        tgt_con.close()

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Transplant archive rows (pst_source LIKE 'archive:%') from a "
            "pre-rebuild backup DB into a freshly-rebuilt staging DB, "
            "remapping PKs and FTS5 rowids."
        )
    )
    parser.add_argument(
        "--source-db",
        required=True,
        type=Path,
        metavar="PATH",
        help="Path to the source SQLite DB (pre-rebuild backup; opened read-only).",
    )
    parser.add_argument(
        "--target-db",
        required=True,
        type=Path,
        metavar="PATH",
        help=(
            "Path to the target SQLite DB (freshly-rebuilt stage; "
            "must already have schema)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Run the full transplant inside a transaction but force "
            "ROLLBACK at the end. Emits the same plan JSON without "
            "modifying the target DB."
        ),
    )
    args = parser.parse_args()

    source_path = Path(args.source_db).expanduser().resolve()
    target_path = Path(args.target_db).expanduser().resolve()

    if not source_path.exists():
        sys.stderr.write(f"ERROR: source-db not found: {source_path}\n")
        return 1
    if not target_path.exists():
        sys.stderr.write(f"ERROR: target-db not found: {target_path}\n")
        return 1

    result = transplant(source_path, target_path, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))

    if isinstance(result.get("result"), str) and result["result"].startswith(
        "rollback-on-error:"
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

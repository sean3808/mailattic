from __future__ import annotations

import argparse
import json
import mailbox
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic.attachment_store import (  # noqa: E402
    resolve_store_root,
    write_attachment,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    database_path = args.database.expanduser().resolve()
    store_root = resolve_store_root(
        database_path,
        cli_override=args.attachment_root,
    )
    pst_sources = [str(path.expanduser().resolve()) for path in args.pst]

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        if not pst_sources:
            pst_sources = [
                row["pst_source"]
                for row in connection.execute(
                    "SELECT DISTINCT pst_source FROM messages ORDER BY pst_source"
                )
            ]

        summary = {
            "pst_sources": len(pst_sources),
            "matched_messages": 0,
            "updated_attachments": 0,
            "skipped_messages": 0,
            "errors": 0,
        }
        for pst_source in pst_sources:
            _backfill_pst(
                connection=connection,
                pst_source=pst_source,
                readpst_path=args.readpst_path,
                store_root=store_root,
                batch_size=args.batch_size,
                summary=summary,
            )
        connection.commit()
    finally:
        connection.close()

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["errors"] == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill attachment binaries into the content-addressable store."
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--readpst-path", required=True, type=Path)
    parser.add_argument("--pst", action="append", default=[], type=Path)
    parser.add_argument("--attachment-root", default=None, type=Path)
    parser.add_argument("--batch-size", default=500, type=int)
    return parser


def _backfill_pst(
    *,
    connection: sqlite3.Connection,
    pst_source: str,
    readpst_path: Path,
    store_root: Path,
    batch_size: int,
    summary: dict[str, int],
) -> None:
    with tempfile.TemporaryDirectory() as temp_dir_name:
        output_root = Path(temp_dir_name) / "out"
        output_root.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [str(readpst_path), "-r", "-q", "-o", str(output_root), pst_source],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
        if result.returncode != 0:
            summary["errors"] += 1
            return

        pending_updates = 0
        for message in _iter_extracted_messages(output_root):
            message_id = message.get("Message-ID")
            if not message_id:
                summary["skipped_messages"] += 1
                continue
            row = connection.execute(
                """
                SELECT id
                FROM messages
                WHERE message_id = ? AND pst_source = ?
                ORDER BY id
                LIMIT 1
                """,
                [message_id, pst_source],
            ).fetchone()
            if row is None:
                summary["skipped_messages"] += 1
                continue

            all_rows = connection.execute(
                """
                SELECT id, attachment_name, storage_path
                FROM message_attachments
                WHERE message_id = ?
                ORDER BY id
                """,
                [row["id"]],
            ).fetchall()
            pending_rows = [r for r in all_rows if r["storage_path"] is None]
            if not pending_rows:
                continue

            summary["matched_messages"] += 1
            matched = _match_pending(
                all_rows, pending_rows, list(_iter_attachment_parts(message))
            )
            if matched is None:
                # Ambiguous or count-mismatched: refuse, write nothing.
                summary["errors"] += 1
                continue
            for attachment_row, part, payload in matched:
                record = write_attachment(
                    payload,
                    attachment_row["attachment_name"],
                    part.get_content_type(),
                    store_root,
                )
                connection.execute(
                    """
                    UPDATE message_attachments
                    SET content_hash = ?,
                        size_bytes = ?,
                        mime_type = ?,
                        storage_path = ?
                    WHERE id = ?
                    """,
                    [
                        record.content_hash,
                        record.size_bytes,
                        record.mime_type,
                        record.storage_path,
                        attachment_row["id"],
                    ],
                )
                summary["updated_attachments"] += 1
                pending_updates += 1
                if pending_updates >= batch_size:
                    connection.commit()
                    pending_updates = 0
        if pending_updates:
            connection.commit()


def _match_pending(
    all_rows: Sequence[sqlite3.Row],
    pending_rows: Sequence[sqlite3.Row],
    parts: Sequence[Message],
) -> list[tuple[sqlite3.Row, Message, bytes]] | None:
    """Pair each pending placeholder with its original part by unique name.

    Returns None (caller refuses the whole message) when counts differ, a name is
    not unique on either side, or a payload cannot be decoded.
    """
    if len(parts) != len(all_rows):
        return None
    matched: list[tuple[sqlite3.Row, Message, bytes]] = []
    for row in pending_rows:
        name = row["attachment_name"]
        same_rows = [r for r in all_rows if r["attachment_name"] == name]
        same_parts = [p for p in parts if p.get_filename() == name]
        if len(same_rows) != 1 or len(same_parts) != 1:
            return None
        payload = same_parts[0].get_payload(decode=True)
        if not isinstance(payload, bytes):
            return None
        matched.append((row, same_parts[0], payload))
    return matched


def _iter_extracted_messages(output_root: Path) -> Iterable[Message]:
    for eml_path in sorted(output_root.rglob("*.eml")):
        with eml_path.open("rb") as handle:
            yield BytesParser(policy=policy.default).parse(handle)

    for mbox_path in sorted(output_root.rglob("mbox")):
        mbox = mailbox.mbox(mbox_path, create=False)
        try:
            yield from mbox
        finally:
            mbox.close()


def _iter_attachment_parts(message: Message) -> Iterable[Message]:
    for part in message.walk():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if disposition == "attachment" or (disposition == "inline" and filename):
            yield part


if __name__ == "__main__":
    raise SystemExit(main())

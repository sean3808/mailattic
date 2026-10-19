"""backfill_semicolon_recipients.py — One-time repair of recipients that
issue #15 silently dropped from stored ``raw_headers`` rows.

``email.policy.default``'s address-list parser truncates a bare,
semicolon-separated ``To``/``Cc``/``Bcc`` header to its first address. The
parser fix (``mailattic.parser._recover_truncated_recipients``)
stops the bleed going forward for messages ingested with raw bytes available.
This script repairs the rows that were already persisted with the truncated
recipient set, per
``openspec/changes/fix-semicolon-recipient-truncation/design.md`` Decisions
2, 4 and 5.

It scans every ``messages`` row whose ``raw_headers`` column is populated,
reconstructs header-only bytes as the exact inverse of the ingest-time
Latin-1 decode (``raw_headers.encode("latin-1") + b"\\r\\n\\r\\n"``), and
feeds those bytes through the SAME parser recovery helper used at ingest
time to compute the full To/Cc/Bcc recipient set. Only recipients missing
from the currently-stored ``message_recipients`` rows are inserted; nothing
is ever removed.

Usage:
    uv run python scripts/backfill_semicolon_recipients.py --dry-run
        [--database <path>]
    uv run python scripts/backfill_semicolon_recipients.py --confirm
        [--database <path>] [--backup-path <path>]

Default mode is ``--dry-run`` (no writes). ``--confirm`` is required to
write; the confirmed path automatically creates a database backup via
``backup_database`` before any write and aborts without writing if that
backup raises (design.md Decision 5, fail-closed).

Exit codes:
    0 — dry-run report printed, or confirmed run completed (including a
        no-op "nothing to do" run)
    1 — confirmed run refused because the pre-write backup failed
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running directly without editable install
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic.cjk_segmentation import segment_cjk  # noqa: E402
from mailattic.db_maintenance import backup_database  # noqa: E402
from mailattic.models import build_participant_search_text  # noqa: E402
from mailattic.parser import (  # noqa: E402
    _parse_address_list,
    _recover_truncated_recipients,
)
from mailattic.storage import connect_database  # noqa: E402

# ---------------------------------------------------------------------------
# Defaults (mirror cli.py conventions —
# absolute, cwd-invariant)
# ---------------------------------------------------------------------------
_ENV_VAR_DB = "MAILATTIC_DB"
_DEFAULT_DB_NAME = r"C:\IT_Projects\mailattic\mailattic.db"

_HEADER_NAMES: dict[str, str] = {"to": "To", "cc": "Cc", "bcc": "Bcc"}
_RECIPIENT_TYPES: tuple[str, ...] = tuple(_HEADER_NAMES)


def _resolve_database_default() -> Path:
    """Return the default database path (env var > hardcoded absolute default)."""
    env_val = os.environ.get(_ENV_VAR_DB, "")
    if env_val.strip():
        return Path(env_val)
    return Path(_DEFAULT_DB_NAME)


@dataclass(frozen=True, slots=True)
class AffectedMessage:
    """One message whose stored recipients are missing addresses raw_headers proves."""

    db_id: int
    message_id: str | None
    stored_count: int
    recovered_count: int
    missing_by_type: dict[str, list[dict[str, str | None]]] = field(
        default_factory=dict
    )


def _reconstruct_header_bytes(raw_headers: str) -> bytes:
    """Inverse of parser._extract_raw_headers' Latin-1 decode (design.md Decision 2)."""
    return raw_headers.encode("latin-1") + b"\r\n\r\n"


def _recovered_addresses_by_type(
    header_bytes: bytes,
) -> dict[str, list[dict[str, str | None]]]:
    """Recover the full To/Cc/Bcc recipient set via the shared parser helper.

    Re-parses *header_bytes* with ``email.policy.default`` (the same policy
    ingestion uses) to obtain header-registry values, then runs each header
    through ``_recover_truncated_recipients`` — the exact strict-replace-gated
    helper the parser fix wired into ``_parse_email_message`` — so the
    backfill and ingestion never diverge on when recovery applies.
    """
    message = BytesParser(policy=policy.default).parsebytes(header_bytes)
    result: dict[str, list[dict[str, str | None]]] = {}
    for recipient_type, header_name in _HEADER_NAMES.items():
        header_values = message.get_all(header_name, [])
        default_addresses = _parse_address_list(header_values)
        result[recipient_type] = _recover_truncated_recipients(
            header_name=header_name,
            header_values=header_values,
            default_addresses=default_addresses,
            raw_bytes=header_bytes,
        )
    return result


def _load_stored_recipients(
    con: sqlite3.Connection, db_id: int
) -> dict[str, list[dict[str, str | None]]]:
    rows = con.execute(
        "SELECT recipient_type, display_name, email "
        "FROM message_recipients WHERE message_id = ?",
        [db_id],
    ).fetchall()
    result: dict[str, list[dict[str, str | None]]] = {
        rtype: [] for rtype in _RECIPIENT_TYPES
    }
    for row in rows:
        rtype = row["recipient_type"]
        if rtype in result:
            result[rtype].append({"name": row["display_name"], "email": row["email"]})
    return result


def compute_backfill_plan(
    con: sqlite3.Connection,
) -> tuple[int, list[AffectedMessage]]:
    """Scan raw_headers-bearing rows and compute the missing-recipient plan.

    Returns (rows_scanned, affected_messages). A row is affected only when
    the recovered recipient set contains at least one address (by type +
    email) absent from the currently-stored ``message_recipients`` rows.
    Never raises: a row whose reconstructed bytes fail to parse is skipped.
    """
    rows = con.execute(
        "SELECT id, message_id, raw_headers FROM messages WHERE raw_headers IS NOT NULL"
    ).fetchall()

    plan: list[AffectedMessage] = []
    for row in rows:
        db_id = int(row["id"])
        try:
            header_bytes = _reconstruct_header_bytes(row["raw_headers"])
            recovered_by_type = _recovered_addresses_by_type(header_bytes)
        except Exception as exc:
            print(
                f"[warn] db_id={db_id}: skipping — could not re-parse "
                f"raw_headers ({exc!r})",
                file=sys.stderr,
            )
            continue

        stored_by_type = _load_stored_recipients(con, db_id)

        missing_by_type: dict[str, list[dict[str, str | None]]] = {}
        for rtype in _RECIPIENT_TYPES:
            stored_emails = {
                entry["email"] for entry in stored_by_type[rtype] if entry["email"]
            }
            missing = [
                entry
                for entry in recovered_by_type[rtype]
                if entry.get("email") and entry["email"] not in stored_emails
            ]
            if missing:
                missing_by_type[rtype] = missing

        if not missing_by_type:
            continue

        stored_count = sum(len(v) for v in stored_by_type.values())
        recovered_count = sum(len(v) for v in recovered_by_type.values())
        plan.append(
            AffectedMessage(
                db_id=db_id,
                message_id=row["message_id"],
                stored_count=stored_count,
                recovered_count=recovered_count,
                missing_by_type=missing_by_type,
            )
        )

    return len(rows), plan


def _recompute_recipient_search_text(con: sqlite3.Connection, db_id: int) -> str:
    """Rebuild recipient_search_text from ALL current message_recipients rows.

    Mirrors ``NormalizedMessage.recipient_search_text``: to + cc + bcc order,
    each participant's ``build_participant_search_text``, joined by a space,
    skipping empty entries.
    """
    parts: list[str] = []
    for rtype in _RECIPIENT_TYPES:
        rows = con.execute(
            "SELECT display_name, email FROM message_recipients "
            "WHERE message_id = ? AND recipient_type = ? ORDER BY id",
            [db_id, rtype],
        ).fetchall()
        for row in rows:
            text = build_participant_search_text(row["display_name"], row["email"])
            if text:
                parts.append(text)
    return " ".join(parts)


def _print_dry_run_report(rows_scanned: int, plan: list[AffectedMessage]) -> None:
    for affected in plan:
        print(
            f"[dry-run] message_id={affected.message_id!r} db_id={affected.db_id} "
            f"stored={affected.stored_count} recovered={affected.recovered_count}"
        )
        for rtype, entries in affected.missing_by_type.items():
            for entry in entries:
                print(f"    + {rtype}: {entry.get('name')!r} <{entry['email']}>")
    print(
        f"[dry-run] {rows_scanned} raw_headers row(s) scanned; "
        f"{len(plan)} message(s) affected; no rows changed."
    )


def apply_backfill(con: sqlite3.Connection, plan: list[AffectedMessage]) -> int:
    """Insert missing recipients + recompute derived fields in one transaction.

    Returns the number of recipient rows inserted. Rolls back entirely on
    any error (the caller's ``with con:`` block owns the transaction).
    """
    inserted = 0
    with con:
        for affected in plan:
            for rtype, entries in affected.missing_by_type.items():
                for entry in entries:
                    con.execute(
                        "INSERT INTO message_recipients "
                        "(message_id, recipient_type, display_name, email) "
                        "VALUES (?, ?, ?, ?)",
                        [affected.db_id, rtype, entry.get("name"), entry["email"]],
                    )
                    inserted += 1

            new_search_text = _recompute_recipient_search_text(con, affected.db_id)
            con.execute(
                "UPDATE messages SET recipient_search_text = ? WHERE id = ?",
                [new_search_text, affected.db_id],
            )
            con.execute(
                "UPDATE message_fts SET recipients = ? WHERE rowid = ?",
                [segment_cjk(new_search_text), affected.db_id],
            )
    return inserted


def run_backfill(
    database_path: Path,
    *,
    confirm: bool,
    dry_run: bool = False,
    backup_path: Path | None = None,
) -> int:
    if dry_run and confirm:
        print(
            "[dry-run] --dry-run overrides --confirm; no changes will be written.",
            file=sys.stderr,
        )

    con = connect_database(database_path, create=False)
    con.execute("PRAGMA busy_timeout = 30000;")  # Tolerate concurrent IMAP sync writes.
    try:
        rows_scanned, plan = compute_backfill_plan(con)

        if not plan:
            print(
                f"[backfill] {rows_scanned} raw_headers row(s) scanned; nothing to do."
            )
            return 0

        if dry_run or not confirm:
            _print_dry_run_report(rows_scanned, plan)
            return 0

        try:
            snapshot_path = backup_database(database_path, backup_path)
        except Exception as exc:
            print(
                f"[abort] Pre-write backup failed ({exc!r}); no changes were made.",
                file=sys.stderr,
            )
            return 1
        print(f"[confirm] Backup snapshot created at {snapshot_path}")

        inserted = apply_backfill(con, plan)
        print(
            f"[confirm] {rows_scanned} row(s) scanned; "
            f"{len(plan)} message(s) repaired; "
            f"{inserted} recipient row(s) inserted."
        )
        return 0
    finally:
        con.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-time repair of recipients dropped by the issue #15 "
            "semicolon-truncation bug, driven by stored raw_headers."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help=(
            f"SQLite database path (default: ${_ENV_VAR_DB} env var, else "
            f"{_DEFAULT_DB_NAME})."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List intended changes; write nothing (default mode).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Required to write. Automatically creates a database backup "
            "before writing and aborts without writing if that backup fails."
        ),
    )
    parser.add_argument(
        "--backup-path",
        type=Path,
        default=None,
        help="Optional override for the pre-write backup snapshot location.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    database_path = (
        (args.database or _resolve_database_default()).expanduser().resolve()
    )

    if not database_path.exists():
        print(f"[error] Database not found: {database_path}", file=sys.stderr)
        return 1

    return run_backfill(
        database_path,
        confirm=args.confirm,
        dry_run=args.dry_run,
        backup_path=args.backup_path,
    )


if __name__ == "__main__":
    sys.exit(main())

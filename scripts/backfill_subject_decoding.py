"""backfill_subject_decoding.py — One-time repair of stored subjects that the
pre-#202 decoder left broken (issue #219).

PR #215 fixed Subject decoding for new ingests only. This script re-decodes
the subject of already-persisted rows from their stored ``raw_headers`` via
the same ``parser.subject_from_raw_headers`` that ingest now uses.

A row is repaired only when its stored subject contains U+FFFD or a lone
surrogate AND the re-decoded subject contains neither. Rows that merely
differ in whitespace are left alone. Rows whose ``dedup_key`` is not
Message-ID based are skipped and listed: their fallback key hashes the
subject, so rewriting it would need a key recompute this script does not do.

Usage:
    uv run python scripts/backfill_subject_decoding.py --database <path>
    uv run python scripts/backfill_subject_decoding.py --database <path> --confirm
        [--backup-path <path>]

Default is a dry run (read-only connection). ``--confirm`` backs the
database up first, aborts without writing if that fails, then updates
``messages.subject`` and ``message_fts.subject`` in one transaction.

Exit codes: 0 — report printed / repair done; 1 — database missing or
pre-write backup failed.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic.cjk_segmentation import segment_cjk  # noqa: E402
from mailattic.db_maintenance import backup_database  # noqa: E402
from mailattic.outbound import _looks_undecoded  # noqa: E402
from mailattic.parser import subject_from_raw_headers  # noqa: E402
from mailattic.storage import connect_database  # noqa: E402


def is_broken(text: str | None) -> bool:
    """U+FFFD or a lone surrogate: the two marks of a failed decode."""
    return bool(text) and _looks_undecoded(text)


@dataclass(frozen=True, slots=True)
class Repair:
    db_id: int
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class Plan:
    repairs: list[Repair]
    skipped_fallback_key: list[int]
    unrecoverable: dict[str, list[int]]  # source_input_mode -> db_ids


def compute_plan(con: sqlite3.Connection) -> Plan:
    repairs: list[Repair] = []
    skipped: list[int] = []
    unrecoverable: dict[str, list[int]] = {}
    rows = con.execute(
        "SELECT id, subject, dedup_key, source_input_mode FROM messages"
    ).fetchall()
    for row in rows:
        db_id, old = int(row["id"]), row["subject"]
        if not is_broken(old):
            continue
        # raw_headers is large; read it only for the few broken rows.
        (raw,) = con.execute(
            "SELECT raw_headers FROM messages WHERE id = ?", [db_id]
        ).fetchone()
        new = subject_from_raw_headers(raw) if raw else None
        if new is None or is_broken(new):
            unrecoverable.setdefault(row["source_input_mode"] or "?", []).append(db_id)
        elif not str(row["dedup_key"]).startswith("message-id:"):
            skipped.append(db_id)
        else:
            repairs.append(Repair(db_id, old, new))
    return Plan(repairs, skipped, unrecoverable)


def print_report(plan: Plan) -> None:
    for r in plan.repairs:
        print(f"[repair] db_id={r.db_id}\n    old: {r.old!r}\n    new: {r.new!r}")
    for db_id in plan.skipped_fallback_key:
        print(f"[skip] db_id={db_id}: fallback dedup_key hashes the subject; kept")
    for mode, ids in sorted(plan.unrecoverable.items()):
        print(f"[unrecoverable] source_input_mode={mode}: {len(ids)} row(s): {ids}")
    total_unrecoverable = sum(len(ids) for ids in plan.unrecoverable.values())
    print(
        f"[summary] repair={len(plan.repairs)} "
        f"skip_fallback_key={len(plan.skipped_fallback_key)} "
        f"unrecoverable={total_unrecoverable}"
    )


def apply_plan(con: sqlite3.Connection, repairs: list[Repair]) -> None:
    with con:
        for r in repairs:
            con.execute(
                "UPDATE messages SET subject = ? WHERE id = ?", [r.new, r.db_id]
            )
            con.execute(
                "UPDATE message_fts SET subject = ? WHERE rowid = ?",
                [segment_cjk(r.new), r.db_id],
            )


def run(database: Path, *, confirm: bool, backup_path: Path | None = None) -> int:
    if not database.exists():
        print(f"[error] Database not found: {database}", file=sys.stderr)
        return 1
    if not confirm:
        con = connect_database(database, read_only=True)
        try:
            print_report(compute_plan(con))
            print("[dry-run] no rows changed.")
        finally:
            con.close()
        return 0

    con = connect_database(database, create=False)
    con.execute("PRAGMA busy_timeout = 30000;")  # Tolerate concurrent IMAP sync writes.
    try:
        plan = compute_plan(con)
        print_report(plan)
        if not plan.repairs:
            print("[confirm] nothing to repair.")
            return 0
        try:
            snapshot = backup_database(database, backup_path)
        except Exception as exc:
            print(
                f"[abort] Pre-write backup failed ({exc!r}); no changes were made.",
                file=sys.stderr,
            )
            return 1
        print(f"[confirm] Backup snapshot created at {snapshot}")
        apply_plan(con, plan.repairs)
        print(f"[confirm] {len(plan.repairs)} subject(s) repaired.")
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--confirm", action="store_true", help="Back up, then write.")
    parser.add_argument("--backup-path", type=Path, default=None)
    args = parser.parse_args(argv)
    return run(args.database, confirm=args.confirm, backup_path=args.backup_path)


if __name__ == "__main__":
    sys.exit(main())

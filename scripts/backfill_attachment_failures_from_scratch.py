"""backfill_attachment_failures_from_scratch.py — One-time fill of the legacy
PST attachment failures from the #30 trial scratch DBs (issue #30, route A).

The 2026-05-27 PST import (retired readpst route) left open ``attachment-decode``
ledger rows with no ``attachment_name`` / ``attachment_size``, so the
repair-on-reimport path (which matches on those two columns) can never fill
them. The #30 trials re-imported the same PSTs through Outlook into scratch DBs,
where those attachments decoded. This script copies each one across.

Per open legacy failure (name taken from the error message):
production message by ``(pst_source, logical_locator)`` -> exactly one
production attachment row with that name (case-insensitive), still empty ->
scratch message by Message-ID, else dedup key -> exactly one scratch attachment
with that name, carrying stored content -> scratch file bytes re-hashed against
its hash. A record that fails one of these known checks is skipped and reported
by reason. Only failures recorded on the #30 batch day are candidates.

Fail-closed, not hardened (#30 route decision after the review cap): the input
is a fixed set already run end to end on a copy of production. So any
unexpected error while planning stops the run before anything is written, and
``--confirm`` writes only when the plan has exactly ``--expect-fillable`` fills
(137 on that copy). Any odd input either stops planning or moves the count.

With ``--confirm`` the production write lock is taken before planning and held
to the commit, so nothing can change between the plan and the write, and all
fills land in one transaction or none do. A fill writes the bytes into
production's attachment store, sets the placeholder row's hash / size / MIME
type / path and marks the ledger row resolved; the whole run rolls back unless
each update hits exactly one row. Scratch DBs are only read. Counts and ledger
row ids are printed (ids are needed to abandon the rest by hand); no attachment
names or content.

Usage:
    uv run python scripts/backfill_attachment_failures_from_scratch.py
        --database <path> --scratch <scratch.db> [--scratch ...]
        [--confirm --expect-fillable <n> [--backup-path <path>]
         [--attachment-root <dir>]]

Each scratch DB must hold exactly one PST; failures are paired with the scratch
DB whose PST has the same file name.

Exit codes: 0 — report printed / fill done; 1 — bad arguments, planning
stopped, fill count not as expected, pre-write backup failed, or a fill found
its rows changed since planning.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic.attachment_store import (  # noqa: E402
    read_attachment,
    resolve_store_root,
    write_attachment,
)
from mailattic.db_maintenance import backup_database  # noqa: E402
from mailattic.storage import connect_database  # noqa: E402

_NAME_IN_MESSAGE = re.compile(r":\s*(.+)$")
# The import batch #30 covers (UTC day of created_at); nothing else is touched.
BATCH_DAY = "2026-05-27"


@dataclass(frozen=True, slots=True)
class Scratch:
    connection: sqlite3.Connection
    store_root: Path


@dataclass(frozen=True, slots=True)
class Fill:
    failure_id: int
    attachment_id: int
    name: str
    payload: bytes
    mime_type: str | None


@dataclass(slots=True)
class Plan:
    candidates: int = 0
    fills: list[Fill] = field(default_factory=list)
    skipped: dict[str, list[int]] = field(default_factory=dict)

    def skip(self, reason: str, failure_id: int) -> None:
        self.skipped.setdefault(reason, []).append(failure_id)


def pst_basename(pst_source: str) -> str:
    # PureWindowsPath splits on both "/" (WSL import paths) and "\\".
    return PureWindowsPath(pst_source).name.casefold()


def open_scratch(path: Path) -> tuple[str, Scratch]:
    connection = connect_database(path, read_only=True)
    names = {
        pst_basename(row[0])
        for row in connection.execute("SELECT DISTINCT pst_source FROM messages")
    }
    if len(names) != 1:
        connection.close()
        raise ValueError(f"{path} holds {len(names)} PSTs, expected exactly one")
    return names.pop(), Scratch(connection, resolve_store_root(path))


def compute_plan(production: sqlite3.Connection, scratches: dict[str, Scratch]) -> Plan:
    plan = Plan()
    failures = production.execute(
        """
        SELECT id, pst_source, logical_locator, error_message FROM ingest_failures
        WHERE error_stage = 'attachment-decode' AND resolved_at IS NULL
          AND attachment_name IS NULL AND substr(created_at, 1, 10) = ?
        ORDER BY id
        """,
        [BATCH_DAY],
    ).fetchall()
    failures = [f for f in failures if pst_basename(f[1] or "") in scratches]
    # One pass over messages: there is no (pst_source, logical_locator) index and
    # each full scan of a multi-GB table takes seconds.
    sources = {f[1] for f in failures}
    by_location: dict[tuple[str, str | None], list[tuple[int, str | None, str]]] = {}
    for db_id, source, locator, message_id, dedup_key in production.execute(
        "SELECT id, pst_source, logical_locator, message_id, dedup_key FROM messages"
    ):
        if source in sources:
            by_location.setdefault((source, locator), []).append(
                (db_id, message_id, dedup_key)
            )
    plan.candidates = len(failures)
    for failure_id, pst_source, locator, error_message in failures:
        try:
            result = _plan_one(
                production,
                scratches[pst_basename(pst_source)],
                failure_id,
                by_location.get((pst_source, locator), []),
                error_message,
            )
        except Exception as exc:
            # Type only: the message may carry a path or name.
            raise ValueError(
                f"planning stopped at ledger id {failure_id}"
                f" ({type(exc).__name__}); nothing was written"
            ) from None
        if isinstance(result, Fill):
            plan.fills.append(result)
        else:
            plan.skip(result, failure_id)
    return plan


def _plan_one(
    production: sqlite3.Connection,
    scratch: Scratch,
    failure_id: int,
    messages: list[tuple[int, str | None, str]],
    error_message: str | None,
) -> Fill | str:
    """The fill for one failure, or the reason it is skipped."""
    match = _NAME_IN_MESSAGE.search(error_message or "")
    name = match.group(1).strip().casefold() if match else ""
    if not name:
        return "no attachment name in error message"

    if not messages:
        return "no production message"
    if len(messages) > 1:
        return "several production messages"
    db_id, message_id, dedup_key = messages[0]

    rows = [
        row
        for row in production.execute(
            "SELECT id, attachment_name, content_hash, storage_path"
            " FROM message_attachments WHERE message_id = ?",
            [db_id],
        )
        if (row[1] or "").casefold() == name
    ]
    if not rows:
        return "no production attachment row"
    if len(rows) > 1:
        return "production attachment name not unique"
    attachment_id, stored_name, content_hash, storage_path = rows[0]
    if content_hash is not None or storage_path is not None:
        return "production attachment already has content"

    scratch_ids: list[int] = []
    for column, value in (("message_id", message_id), ("dedup_key", dedup_key)):
        if value:
            scratch_ids = [
                row[0]
                for row in scratch.connection.execute(
                    f"SELECT id FROM messages WHERE {column} = ?", [value]
                )
            ]
            if scratch_ids:
                break
    if not scratch_ids:
        return "message not in scratch DB"
    if len(scratch_ids) > 1:
        return "several scratch messages"

    same_name = [
        row
        for row in scratch.connection.execute(
            "SELECT attachment_name, content_hash, size_bytes, storage_path, mime_type"
            " FROM message_attachments WHERE message_id = ?",
            [scratch_ids[0]],
        )
        if (row[0] or "").casefold() == name
    ]
    # Like production, the name must be unique: with two same-name parts there
    # is no proof which one the empty placeholder stood for.
    if not same_name:
        return "no scratch attachment row"
    if len(same_name) > 1:
        return "scratch attachment name not unique"
    _name, expected_hash, size, scratch_path, mime_type = same_name[0]
    if not (expected_hash and size and scratch_path):
        return "scratch attachment has no content"
    try:
        payload = read_attachment(scratch_path, scratch.store_root)
    except OSError:  # includes AttachmentStoreError (file absent)
        return "scratch file missing or unreadable"
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        return "scratch file does not match its hash"
    return Fill(failure_id, attachment_id, stored_name, payload, mime_type)


def apply_plan(
    production: sqlite3.Connection, fills: list[Fill], store_root: Path
) -> int:
    """Write every fill inside the caller's open transaction; the caller commits."""
    for fill in fills:
        record = write_attachment(fill.payload, fill.name, fill.mime_type, store_root)
        attachment_rows = production.execute(
            """
            UPDATE message_attachments
            SET content_hash = ?, size_bytes = ?, mime_type = ?, storage_path = ?
            WHERE id = ? AND content_hash IS NULL AND storage_path IS NULL
            """,
            [
                record.content_hash,
                record.size_bytes,
                record.mime_type,
                record.storage_path,
                fill.attachment_id,
            ],
        ).rowcount
        ledger_rows = production.execute(
            "UPDATE ingest_failures SET resolved_at = CURRENT_TIMESTAMP"
            " WHERE id = ? AND resolved_at IS NULL",
            [fill.failure_id],
        ).rowcount
        if (attachment_rows, ledger_rows) != (1, 1):
            raise ValueError(
                f"ledger id {fill.failure_id}: update hit"
                f" {attachment_rows}/{ledger_rows} rows; nothing was written"
            )
    return len(fills)


def print_report(plan: Plan) -> None:
    print(f"candidates: {plan.candidates}")
    print(f"fillable: {len(plan.fills)}")
    for reason, ids in sorted(plan.skipped.items()):
        print(f"skipped ({reason}): {len(ids)} — ledger ids {ids}")


def run(
    database: Path,
    scratch_paths: list[Path],
    *,
    confirm: bool,
    expect_fillable: int | None = None,
    attachment_root: Path | None = None,
    backup_path: Path | None = None,
) -> int:
    if confirm and expect_fillable is None:
        print("[error] --confirm needs --expect-fillable.", file=sys.stderr)
        return 1
    if not database.exists():
        print(f"[error] Database not found: {database}", file=sys.stderr)
        return 1
    if any(database.resolve() == path.resolve() for path in scratch_paths):
        print("[error] --database is also given as --scratch.", file=sys.stderr)
        return 1
    scratches: dict[str, Scratch] = {}
    try:
        for path in scratch_paths:
            name, scratch = open_scratch(path)
            if name in scratches:
                scratch.connection.close()
                raise ValueError(f"two --scratch DBs hold the same PST ({path})")
            scratches[name] = scratch
        production = connect_database(database, read_only=not confirm, create=False)
        try:
            if confirm:
                production.execute("PRAGMA busy_timeout = 30000;")
                # Held until the commit below; closing without it rolls back.
                production.execute("BEGIN IMMEDIATE")
            plan = compute_plan(production, scratches)
            print_report(plan)
            if not confirm:
                print("[dry-run] no rows changed.")
                return 0
            if len(plan.fills) != expect_fillable:
                print(
                    f"[abort] {len(plan.fills)} fillable, expected {expect_fillable};"
                    " no changes were made.",
                    file=sys.stderr,
                )
                return 1
            if not plan.fills:
                print("[confirm] nothing to fill.")
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
            store_root = resolve_store_root(
                database,
                cli_override=attachment_root,
                env_override=os.environ.get("MAILATTIC_ATTACHMENT_ROOT"),
            )
            filled = apply_plan(production, plan.fills, store_root)
            production.commit()
            print(
                f"[confirm] {filled} attachment(s) filled and ledger row(s) resolved."
            )
            return 0
        finally:
            production.close()
    except ValueError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    finally:
        for scratch in scratches.values():
            scratch.connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, action="append", required=True)
    parser.add_argument("--confirm", action="store_true", help="Back up, then write.")
    parser.add_argument(
        "--expect-fillable",
        type=int,
        default=None,
        help="Required with --confirm: write only if the plan has exactly this many.",
    )
    parser.add_argument("--backup-path", type=Path, default=None)
    parser.add_argument("--attachment-root", type=Path, default=None)
    args = parser.parse_args(argv)
    return run(
        args.database,
        args.scratch,
        confirm=args.confirm,
        expect_fillable=args.expect_fillable,
        attachment_root=args.attachment_root,
        backup_path=args.backup_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""backfill_subject_from_outlook.py — One-time repair of broken subjects on
PST-imported rows that have no stored ``raw_headers`` (issue #219, part 2).

``backfill_subject_decoding.py`` repaired rows whose ``raw_headers`` were kept.
Rows imported from PST by readpst have none, so this script reads each
message's ``PR_TRANSPORT_MESSAGE_HEADERS`` from the PST as mounted in the
running Outlook and re-decodes the subject with the same
``parser.subject_from_raw_headers`` that ingest uses.

Candidates: stored subject contains U+FFFD / a lone surrogate, ``raw_headers``
is NULL, and ``dedup_key`` is Message-ID based (a fallback key hashes the
subject and would need a recompute this script does not do).

Lookup, per PST (matched by file basename — ``pst_source`` holds the path the
PST had at import time): Message-ID search in the row's ``folder_path``
first; rows not found there get one whole-store walk, folder-outer and
Message-ID-inner (per-row full walks do not finish in reasonable time).

A hit is used only when its Message-ID reads back equal to the row's and its
sender matches ``sender_email``, and the re-decoded subject is clean.
Only ``messages.subject`` and ``message_fts.subject`` are written;
``raw_headers`` is deliberately left NULL — Outlook returns the headers as a
Unicode string, and whether that restores the original bytes losslessly is
unverified, so it must not be mixed into that column.

Requires Windows, pywin32 and a running Outlook with the PSTs mounted; it
never launches Outlook (``outlook_pst._load_outlook_namespace``). Outlook is
only read.

Usage:
    uv run python scripts/backfill_subject_from_outlook.py --database <path>
    uv run python scripts/backfill_subject_from_outlook.py --database <path> --confirm
        [--backup-path <path>]

Exit codes: 0 — report printed / repair done; 1 — database missing, Outlook
unavailable, or pre-write backup failed.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

# Importing the sibling script also puts src/ on sys.path.
from backfill_subject_decoding import Repair, apply_plan, is_broken

from mailattic.db_maintenance import backup_database
from mailattic.outlook_pst import (
    PR_INTERNET_MESSAGE_ID,
    PR_SENDER_SMTP_ADDRESS,
    PR_TRANSPORT_MESSAGE_HEADERS,
    _load_outlook_namespace,
    _optional_property,
    _safe_attr,
)
from mailattic.parser import subject_from_raw_headers
from mailattic.storage import connect_database


@dataclass(frozen=True, slots=True)
class Candidate:
    db_id: int
    message_id: str
    subject: str
    sender_email: str
    pst_name: str  # casefolded basename
    folder_path: str


@dataclass(frozen=True, slots=True)
class Hit:
    message_id: str | None
    sender_emails: tuple[str, ...]
    headers: str | None
    folder: str


class Locator(Protocol):
    def locate(self, pst_name: str, rows: list[Candidate]) -> dict[int, Hit] | None:
        """db_id -> hit for rows found; ``None`` when the PST is not mounted."""
        ...


@dataclass(slots=True)
class Plan:
    repairs: list[Repair] = field(default_factory=list)
    found_in: dict[int, str] = field(default_factory=dict)  # db_id -> Outlook folder
    skipped: dict[str, list[int]] = field(default_factory=dict)

    def skip(self, reason: str, db_id: int) -> None:
        self.skipped.setdefault(reason, []).append(db_id)


def pst_basename(pst_source: str) -> str:
    # PureWindowsPath splits on both "/" (WSL import paths) and "\\".
    return PureWindowsPath(pst_source).name.casefold()


def select_candidates(con: sqlite3.Connection, plan: Plan) -> list[Candidate]:
    rows = con.execute(
        "SELECT id, subject, message_id, dedup_key, sender_email, pst_source,"
        " folder_path FROM messages WHERE raw_headers IS NULL"
    ).fetchall()
    out: list[Candidate] = []
    for row in rows:
        if not is_broken(row["subject"]):
            continue
        db_id = int(row["id"])
        if not row["message_id"]:
            plan.skip("no_message_id", db_id)
        elif not str(row["dedup_key"]).startswith("message-id:"):
            plan.skip("fallback_dedup_key", db_id)
        elif not row["pst_source"]:
            plan.skip("no_pst_source", db_id)
        else:
            out.append(
                Candidate(
                    db_id=db_id,
                    message_id=row["message_id"],
                    subject=row["subject"],
                    sender_email=row["sender_email"] or "",
                    pst_name=pst_basename(row["pst_source"]),
                    folder_path=row["folder_path"] or "",
                )
            )
    return out


def compute_plan(con: sqlite3.Connection, locator: Locator) -> Plan:
    plan = Plan()
    by_pst: dict[str, list[Candidate]] = {}
    for c in select_candidates(con, plan):
        by_pst.setdefault(c.pst_name, []).append(c)
    for pst_name, rows in sorted(by_pst.items()):
        hits = locator.locate(pst_name, rows)
        if hits is None:
            for c in rows:
                plan.skip(f"pst_not_mounted:{pst_name}", c.db_id)
            continue
        for c in rows:
            hit = hits.get(c.db_id)
            if hit is None:
                plan.skip("not_found_in_outlook", c.db_id)
            elif hit.message_id != c.message_id:
                plan.skip("message_id_mismatch", c.db_id)
            elif c.sender_email.casefold() not in hit.sender_emails:
                plan.skip("sender_mismatch", c.db_id)
            elif not hit.headers:
                plan.skip("no_transport_headers", c.db_id)
            else:
                new = subject_from_raw_headers(_crlf(hit.headers))
                if new is None or is_broken(new):
                    plan.skip("still_broken_at_source", c.db_id)
                else:
                    plan.repairs.append(Repair(c.db_id, c.subject, new))
                    plan.found_in[c.db_id] = hit.folder
    return plan


def _crlf(headers: str) -> str:
    return headers.replace("\r\n", "\n").replace("\n", "\r\n")


def print_report(plan: Plan) -> None:
    for r in plan.repairs:
        print(
            f"[repair] db_id={r.db_id} found_in={plan.found_in[r.db_id]!r}\n"
            f"    old: {r.old!r}\n    new: {r.new!r}"
        )
    for reason, ids in sorted(plan.skipped.items()):
        print(f"[skip] {reason}: {len(ids)} row(s): {ids}")
    # Candidates are Message-ID keyed only; those keys never hash the subject.
    print("[dedup_key] changed=0 (every repaired row has a message-id: key)")
    skips = " ".join(f"{k}={len(v)}" for k, v in sorted(plan.skipped.items()))
    print(f"[summary] repair={len(plan.repairs)} {skips}".rstrip())


class OutlookLocator:
    """Read-only Message-ID lookup against PST stores mounted in Outlook."""

    def __init__(self, namespace: Any) -> None:
        stores = namespace.Stores
        self._stores: dict[str, Any] = {}
        for index in range(1, int(stores.Count) + 1):
            store = stores.Item(index)
            if path := _safe_attr(store, "FilePath"):
                self._stores[pst_basename(str(path))] = store

    def locate(self, pst_name: str, rows: list[Candidate]) -> dict[int, Hit] | None:
        store = self._stores.get(pst_name)
        if store is None:
            return None
        root = store.GetRootFolder()
        hits: dict[int, Hit] = {}
        for c in rows:
            folder = _walk(root, c.folder_path.split("/")[1:])
            if folder is not None and (item := _find(folder, c.message_id)):
                hits[c.db_id] = _hit(item, c.folder_path)
        pending = {c.db_id: c for c in rows if c.db_id not in hits}
        for path, folder in _iter_tree(root, str(_safe_attr(root, "Name") or "")):
            if not pending:
                break
            for db_id, c in list(pending.items()):
                if item := _find(folder, c.message_id):
                    hits[db_id] = _hit(item, path)
                    del pending[db_id]
        return hits


def _walk(folder: Any, parts: list[str]) -> Any | None:
    for name in parts:
        try:
            folder = folder.Folders.Item(name)
        except Exception:  # renamed folder (readpst maps "/" to "_"): tree walk
            return None
    return folder


def _iter_tree(root: Any, root_path: str) -> Iterable[tuple[str, Any]]:
    stack = [(root, root_path)]
    while stack:
        folder, path = stack.pop()
        yield path, folder
        subs = folder.Folders
        for index in range(1, int(subs.Count) + 1):
            sub = subs.Item(index)
            stack.append((sub, f"{path}/{_safe_attr(sub, 'Name') or ''}"))


def _find(folder: Any, message_id: str) -> Any | None:
    escaped = message_id.replace("'", "''")
    try:
        return folder.Items.Find(f"@SQL=\"{PR_INTERNET_MESSAGE_ID}\" = '{escaped}'")
    except Exception:  # a folder that rejects the filter counts as a miss
        return None


def _hit(item: Any, folder: str) -> Hit:
    accessor = _safe_attr(item, "PropertyAccessor")
    senders = (
        _safe_attr(item, "SenderEmailAddress"),
        _optional_property(accessor, PR_SENDER_SMTP_ADDRESS),
    )
    return Hit(
        message_id=_optional_property(accessor, PR_INTERNET_MESSAGE_ID),
        sender_emails=tuple(str(s).strip().casefold() for s in senders if s),
        headers=_optional_property(accessor, PR_TRANSPORT_MESSAGE_HEADERS),
        folder=folder,
    )


def run(
    database: Path,
    *,
    confirm: bool,
    locator: Locator | None = None,
    backup_path: Path | None = None,
) -> int:
    if not database.exists():
        print(f"[error] Database not found: {database}", file=sys.stderr)
        return 1
    if locator is None:
        try:
            locator = OutlookLocator(_load_outlook_namespace())
        except RuntimeError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1
    if not confirm:
        con = connect_database(database, read_only=True)
        try:
            print_report(compute_plan(con, locator))
            print("[dry-run] no rows changed.")
        finally:
            con.close()
        return 0

    con = connect_database(database, create=False)
    con.execute("PRAGMA busy_timeout = 30000;")  # Tolerate concurrent IMAP sync writes.
    try:
        plan = compute_plan(con, locator)
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

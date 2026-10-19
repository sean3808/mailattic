"""backfill_folder_path_mutf7.py — Repair mojibake IMAP folder metadata.

Outlook's IMAP LIST responses carry folder names in modified UTF-7 (RFC 3501
section 5.1.3), and before the sync path learned to decode them, rows synced
from Chinese-named folders (e.g. `寄件備份`) were persisted with the raw wire
form (`&W8RO9lCZTv0-`) baked into `folder_path`, `pst_source`, and
`logical_locator`. New syncs already persist the decoded display name (see
openspec/changes/add-mutf7-folder-codec design Decision 1); re-ingesting an
existing message is a dedup no-op that leaves the stored row untouched
(Decision 2). This script is therefore the sole repair path for rows already
written with mojibake (design Decision 4).

Scope — IMAP-sourced rows only:
    Eligibility is decided by `pst_source`, which for IMAP-synced rows has
    the shape `imap:<account_email>:<folder_name>` (see
    `MaildirSource.source_path` in models.py). PST-sourced rows carry a
    filesystem path in `pst_source` and are never touched, even if their
    folder text happens to resemble a modified UTF-7 shift sequence — a PST
    folder name was never mUTF-7 to begin with.

Repair unit — the folder-name segment only:
    For an eligible row, `folder_path` IS the folder name in full (the
    maildir ingestion path sets it verbatim from the wire name). That exact
    string is used to locate and replace the folder segment inside
    `pst_source` (its trailing component) and `logical_locator` (its leading
    path component) — not a blind whole-string regex replace. A row is only
    rewritten when `decode_mutf7(folder_path)` actually differs from
    `folder_path`; an already-decoded value (no shift sequence) is left
    alone, which is what makes a second confirmed run a no-op.

Repair is all-or-nothing per row:
    If `pst_source` or `logical_locator` has a shape this script does not
    recognize (see `_repair_pst_source` / `_repair_logical_locator`), the
    WHOLE row is left untouched -- `folder_path` included -- and reported as
    skipped with the reason. Rewriting `folder_path` alone in that case would
    leave the row internally inconsistent, and because `folder_path` would
    then already be decoded, the idempotency check above would silently skip
    the row on every later run, making the other two columns permanently
    unrepairable.

Known, irreducible limitation -- literal-display-name ambiguity:
    A folder whose *display* name happens to be literally `&W8RO9lCZTv0-`
    would have the wire form `&-W8RO9lCZTv0-` (the literal-ampersand escape
    followed by the rest), and the post-change sync path correctly stores
    that display name verbatim in `folder_path`. This script cannot tell
    that row apart from a legacy row that stored the pre-change wire
    mojibake `&W8RO9lCZTv0-` (without the escape) for the real 寄件備份
    folder -- both would decode identically. This ambiguity is inherent to
    the legacy data (the row alone does not carry enough information to
    resolve it) and is NOT worked around here. In practice it does not
    arise: INBOX and the localized Sent/Trash/Drafts display names are plain
    words, none of them shaped like a modified UTF-7 `&...-` wire string. This
    script is a one-shot repair for rows written before the sync path learned to
    decode folder names, not a routine job to run on every sync.

Usage:
    Dry-run (default; prints what would change, writes nothing):
        uv run python scripts/backfill_folder_path_mutf7.py [--database <path>]

    Confirmed write:
        uv run python scripts/backfill_folder_path_mutf7.py --confirm \
            [--database <path>]

Exit codes:
    0 — ran to completion (dry-run report, or confirmed write applied)
    1 — database file not found
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running directly without editable install
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic.mutf7 import decode_mutf7  # noqa: E402
from mailattic.storage import connect_database  # noqa: E402

# ---------------------------------------------------------------------------
# Default DB path (mirrors CLI convention — absolute, cwd-invariant)
# ---------------------------------------------------------------------------
_DEFAULT_DB = REPO_ROOT / "mailattic.db"
_IMAP_PST_SOURCE_PREFIX = "imap:"
_DEFAULT_SAMPLE_SIZE = 10


@dataclass(frozen=True, slots=True)
class RepairPlan:
    """One row's intended rewrite of its three folder-metadata columns."""

    row_id: int
    old_folder_path: str
    new_folder_path: str
    old_pst_source: str
    new_pst_source: str
    old_logical_locator: str | None
    new_logical_locator: str | None


@dataclass(frozen=True, slots=True)
class SkippedRow:
    """A row whose folder-metadata columns could not be consistently
    repaired, and was therefore left entirely untouched (Codex semantic
    review, all-or-nothing repair)."""

    row_id: int
    reason: str


class _InconsistentRowError(Exception):
    """Raised when a column's actual shape does not match what an
    IMAP-sourced row is expected to look like, so the row's repair cannot be
    trusted. Caught by ``_plan_repair``, which turns it into a ``SkippedRow``
    -- the row's OTHER columns (including ``folder_path``) must not be
    rewritten either, since a partially-repaired row is not just cosmetically
    wrong, it also becomes permanently unrepairable: `folder_path` would
    already be decoded, so a second run's idempotency check
    (``new_folder_path == old_folder_path``) would skip the row entirely."""


def _connect(database_path: Path) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with busy_timeout."""
    con = connect_database(database_path, create=False)
    con.execute("PRAGMA busy_timeout = 30000;")  # 30 s, tolerate concurrent sync
    return con


def _repair_pst_source(pst_source: str, old_folder: str, new_folder: str) -> str:
    """Replace the trailing folder-name segment of an `imap:...:` pst_source.

    `pst_source` for an IMAP row is `imap:<account_email>:<folder_name>` (see
    `MaildirSource.source_path` in models.py), and `folder_name` is exactly
    the value stored in `folder_path`.

    The value is parsed structurally, not by suffix matching. An email address
    cannot contain a colon, so the account is everything up to the FIRST colon
    after the `imap:` prefix, and the folder segment is the entire remainder --
    colons and all. That segment must equal `old_folder` EXACTLY. A mere suffix
    match (even one anchored on a ':') would half-rewrite a value whose folder
    segment only ends with `folder_path`, e.g. `imap:a@b.com:prefix:<wire>`,
    setting `folder_path` to the decoded name while leaving `pst_source`
    carrying a different folder -- a permanently inconsistent row.

    Raises ``_InconsistentRowError`` on any unexpected shape rather than
    silently returning ``pst_source`` unchanged -- the caller must then skip
    the WHOLE row, not just leave this column alone (all-or-nothing repair).
    """
    if not pst_source.startswith(_IMAP_PST_SOURCE_PREFIX):
        raise _InconsistentRowError(
            f"pst_source {pst_source!r} does not start with {_IMAP_PST_SOURCE_PREFIX!r}"
        )
    account, separator, folder_segment = pst_source[
        len(_IMAP_PST_SOURCE_PREFIX) :
    ].partition(":")
    if not separator:
        raise _InconsistentRowError(
            f"pst_source {pst_source!r} has no '<account>:<folder>' separator"
        )
    if folder_segment != old_folder:
        raise _InconsistentRowError(
            f"pst_source {pst_source!r} carries folder segment "
            f"{folder_segment!r}, which is not exactly folder_path {old_folder!r}"
        )
    return f"{_IMAP_PST_SOURCE_PREFIX}{account}:{new_folder}"


def _repair_logical_locator(
    logical_locator: str | None, old_folder: str, new_folder: str
) -> str | None:
    """Replace the leading folder-name path component of `logical_locator`.

    `logical_locator` for an IMAP row is `<folder_name>/<relative_path>`
    (see `_parse_maildir_message` in ingest.py). Only the leading component
    that exactly matches the stored folder name is rewritten. ``None`` is a
    legitimate value (no locator recorded), not an unexpected shape, and is
    returned as-is.

    Raises ``_InconsistentRowError`` on an unexpected non-``None`` shape
    rather than silently returning ``logical_locator`` unchanged -- the
    caller must then skip the WHOLE row (all-or-nothing repair).
    """
    if logical_locator is None:
        return None
    prefix = old_folder + "/"
    if logical_locator.startswith(prefix):
        return new_folder + "/" + logical_locator[len(prefix) :]
    if logical_locator == old_folder:
        return new_folder
    raise _InconsistentRowError(
        f"logical_locator {logical_locator!r} does not match folder_path {old_folder!r}"
    )


def _plan_repair(row: sqlite3.Row) -> RepairPlan | SkippedRow | None:
    """Return the rewrite plan for one row, a ``SkippedRow`` if it cannot be
    consistently repaired, or ``None`` if it needs no repair at all.

    All-or-nothing: if any of the three columns cannot be repaired to a
    consistent state, the row is reported as a ``SkippedRow`` and NONE of its
    columns are rewritten (not even ``folder_path``) -- a row left partially
    repaired would have its idempotency check silently swallow it on every
    subsequent run, so the other columns could never be repaired later.
    """
    pst_source = row["pst_source"] or ""
    if not pst_source.startswith(_IMAP_PST_SOURCE_PREFIX):
        return None  # not IMAP-sourced — PST rows are never touched

    old_folder_path = row["folder_path"]
    if not old_folder_path:
        return None

    new_folder_path = decode_mutf7(old_folder_path)
    if new_folder_path == old_folder_path:
        return None  # not modified UTF-7, or already decoded — idempotent no-op

    row_id = int(row["id"])
    try:
        new_pst_source = _repair_pst_source(
            pst_source, old_folder_path, new_folder_path
        )
        old_logical_locator = row["logical_locator"]
        new_logical_locator = _repair_logical_locator(
            old_logical_locator, old_folder_path, new_folder_path
        )
    except _InconsistentRowError as exc:
        return SkippedRow(row_id=row_id, reason=str(exc))

    return RepairPlan(
        row_id=row_id,
        old_folder_path=old_folder_path,
        new_folder_path=new_folder_path,
        old_pst_source=pst_source,
        new_pst_source=new_pst_source,
        old_logical_locator=old_logical_locator,
        new_logical_locator=new_logical_locator,
    )


def compute_backfill_plan(
    con: sqlite3.Connection,
) -> tuple[int, list[RepairPlan], list[SkippedRow]]:
    """Scan IMAP-sourced rows and return
    (rows_scanned, repair_plans, skipped_rows)."""
    rows = con.execute(
        "SELECT id, pst_source, folder_path, logical_locator FROM messages "
        "WHERE pst_source LIKE 'imap:%'"
    ).fetchall()
    plans: list[RepairPlan] = []
    skipped: list[SkippedRow] = []
    for row in rows:
        result = _plan_repair(row)
        if isinstance(result, RepairPlan):
            plans.append(result)
        elif isinstance(result, SkippedRow):
            skipped.append(result)
    return len(rows), plans, skipped


def run_backfill(
    database_path: Path,
    *,
    confirm: bool,
) -> int:
    """Report (and, if confirmed, apply) the folder-metadata repair plan."""
    con = _connect(database_path)
    try:
        scanned, plans, skipped = compute_backfill_plan(con)
        print(f"[backfill] Scanned {scanned:,} IMAP-sourced rows.")
        print(
            f"[backfill] {len(plans):,} row(s) repaired (folder-metadata "
            "columns rewritten)."
        )
        if skipped:
            print(
                f"[backfill] {len(skipped):,} row(s) skipped as inconsistent "
                "(left entirely untouched, including folder_path) -- "
                "reasons below:"
            )
            for skip in skipped[:_DEFAULT_SAMPLE_SIZE]:
                print(f"[backfill]   Skipped id={skip.row_id}: {skip.reason}")
            if len(skipped) > _DEFAULT_SAMPLE_SIZE:
                print(
                    f"[backfill]   … and "
                    f"{len(skipped) - _DEFAULT_SAMPLE_SIZE:,} more skipped."
                )

        verb = "Would change" if not confirm else "Changing"
        for plan in plans[:_DEFAULT_SAMPLE_SIZE]:
            print(
                f"[backfill]   {verb} id={plan.row_id}: "
                f"folder_path {plan.old_folder_path!r} -> "
                f"{plan.new_folder_path!r}"
            )
        if len(plans) > _DEFAULT_SAMPLE_SIZE:
            print(f"[backfill]   … and {len(plans) - _DEFAULT_SAMPLE_SIZE:,} more.")

        if not confirm:
            print(
                "[backfill] Dry-run only — no rows written. "
                "Re-run with --confirm to write these changes."
            )
            return 0

        if plans:
            with con:  # single transaction wrapping all UPDATEs
                con.executemany(
                    "UPDATE messages SET folder_path = ?, pst_source = ?, "
                    "logical_locator = ? WHERE id = ?",
                    [
                        (
                            plan.new_folder_path,
                            plan.new_pst_source,
                            plan.new_logical_locator,
                            plan.row_id,
                        )
                        for plan in plans
                    ],
                )
        print(f"[backfill] Done. {len(plans):,} row(s) updated.")
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Repair IMAP folder metadata (folder_path / pst_source / "
            "logical_locator) that was persisted as modified UTF-7 wire "
            "mojibake. Dry-run by default; PST-sourced rows are never "
            "touched."
        )
    )
    p.add_argument(
        "--database",
        type=Path,
        default=_DEFAULT_DB,
        metavar="PATH",
        help=f"Path to the SQLite database (default: {_DEFAULT_DB})",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Write the repaired values. Without this flag the script only "
            "prints what it would change."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    database_path: Path = args.database.expanduser().resolve()

    if not database_path.exists():
        print(f"[error] Database not found: {database_path}", file=sys.stderr)
        return 1

    return run_backfill(database_path, confirm=args.confirm)


if __name__ == "__main__":
    sys.exit(main())

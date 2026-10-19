"""#256: quarantine for staged raw mail whose attachment failure is still open.

The sync staging area keeps raw mail 7 days (D-20260703-1). A mail that still
has an unresolved attachment failure is the one case where the raw file is the
only local source able to repair it, so prune moves it here instead of
deleting it. The quarantine mirrors the staging layout
(``<root>/<account>/<folder>/new/<file>.eml``) so the same folder-dir lookup
finds mail in either place.
"""

from __future__ import annotations

import filecmp
import shutil
import sqlite3
from pathlib import Path

_ATTACHMENT_STAGES = "('attachment-store', 'attachment-decode')"


def default_quarantine_root(database_path: str | Path) -> Path:
    """``<database dir>/.mailattic-work/quarantine/<database file name>``.

    Follows the DATABASE, so a copy of it (other directory or other file name)
    never reaches the original's quarantine. The full file name, not the stem:
    ``mail.db`` and ``mail.sqlite`` side by side are two databases.
    """
    from .ingest import _resolve_work_root  # lazy: ingest imports this module

    return (
        _resolve_work_root(database_path)
        / "quarantine"
        / Path(database_path).expanduser().name
    )


def _failure_columns(connection: sqlite3.Connection) -> set[str]:
    return {row[1] for row in connection.execute("PRAGMA table_info(ingest_failures)")}


def _unresolved_attachment_where(connection: sqlite3.Connection) -> str:
    """The single definition of "unresolved attachment failure".

    Shared by the hold rule and ``unresolved_attachment_failures`` (what
    ``bes db doctor`` lists). A database that predates ``resolved_at`` has
    every attachment-stage row unresolved.
    """
    open_filter = (
        "resolved_at IS NULL"
        if "resolved_at" in _failure_columns(connection)
        else "1 = 1"
    )
    return f"{open_filter} AND error_stage IN {_ATTACHMENT_STAGES}"


def is_held(
    connection: sqlite3.Connection,
    logical_locator: str,
    *,
    ignore_failure_id: int | None = None,
) -> bool:
    """The one hold rule (#256): any unresolved attachment failure at this locator.

    Entry (prune quarantines) and exit (release) both ask this. No filename,
    account, source mode or folder filter: those can only release more.
    ``ignore_failure_id`` answers "would it still be held without this one?".
    """
    sql = (
        "SELECT 1 FROM ingest_failures WHERE "
        + _unresolved_attachment_where(connection)
        + " AND logical_locator IS ?"
    )
    params: list[object] = [logical_locator]
    if ignore_failure_id is not None:
        sql += " AND id != ?"
        params.append(ignore_failure_id)
    return connection.execute(sql + " LIMIT 1", params).fetchone() is not None


def quarantine_mail(
    eml_path: Path, account_root: Path, quarantine_account_root: Path
) -> Path:
    """Move a staged .eml into the quarantine, keeping its relative path and mtime."""
    target = quarantine_account_root / eml_path.relative_to(account_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Never overwrite a held file: same name can be a different mail after a
        # resync or UIDVALIDITY reset. Raise so prune keeps the staged copy.
        if not filecmp.cmp(eml_path, target, shallow=False):
            raise FileExistsError(f"quarantine already holds a different {target}")
        eml_path.unlink()
        return target
    shutil.move(str(eml_path), str(target))
    return target


def release_quarantined_mail(
    connection: sqlite3.Connection, path: Path, logical_locator: str
) -> bool:
    """Delete one quarantined file unless the hold rule still holds it.

    The only place that removes a file from the quarantine; callers (retry
    success, human abandon) never decide. Returns True when removed.
    """
    if is_held(connection, logical_locator):
        return False
    path.unlink(missing_ok=True)
    return True


def mail_locator(eml_path: Path, folder_names: dict[Path, str]) -> str | None:
    """``<folder>/<path below its folder dir>``, the locator ingest records.

    The one path -> locator rule, shared by prune (which quarantines) and the
    quarantine listing (which abandon releases from), so a file prune can move
    in is always one abandon can find. The nearest folder ancestor wins (a
    nested folder is its own folder); None when no ancestor is a folder dir.
    """
    folder_dir = next((p for p in eml_path.parents if p in folder_names), None)
    if folder_dir is None:
        return None
    return f"{folder_names[folder_dir]}/{eml_path.relative_to(folder_dir).as_posix()}"


def _quarantined_files(quarantine_root: Path):
    """Yield ``(path, logical_locator)`` for every quarantined .eml, at any depth."""
    from .ingest import _local_folder_dirs  # lazy: ingest imports this module

    root = Path(quarantine_root).expanduser()
    if not root.is_dir():
        return
    for account_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        folder_names = {p: name for name, p in _local_folder_dirs(account_dir).items()}
        for path in sorted(account_dir.rglob("*.eml")):
            locator = mail_locator(path, folder_names)
            if locator is not None:
                yield path, locator


def release_quarantined_locator(
    connection: sqlite3.Connection, quarantine_root: Path, locator: str
) -> int:
    """Offer every quarantined file at ``locator`` to ``release_quarantined_mail``."""
    return sum(
        release_quarantined_mail(connection, path, found)
        for path, found in _quarantined_files(quarantine_root)
        if found == locator
    )


def count_quarantined_mail(quarantine_root: Path) -> int:
    """Every ``*.eml`` under the root, whatever its shape."""
    root = Path(quarantine_root).expanduser()
    return sum(1 for _ in root.rglob("*.eml")) if root.is_dir() else 0


def quarantined_files_freed_by_abandon(
    connection: sqlite3.Connection,
    quarantine_root: Path,
    *,
    locator: str,
    ignore_failure_id: int | None = None,
) -> list[Path]:
    """Quarantined files of THIS mail that no other open failure would still hold.

    Preview for the abandon confirmation; the actual removal goes through
    ``release_quarantined_mail``.
    """
    if is_held(connection, locator, ignore_failure_id=ignore_failure_id):
        return []
    return [p for p, found in _quarantined_files(quarantine_root) if found == locator]


def unresolved_attachment_failures(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every open attachment failure, legacy rows without filename/size included.

    Works on a database that predates the ``attachment_name`` / ``attachment_size``
    / ``resolved_at`` columns (a new version adds them when it first opens the
    database for writing): without them every attachment-stage row is unresolved
    with an unknown name and size. Read-only, never migrates.
    """
    columns = _failure_columns(connection)
    name = "attachment_name" if "attachment_name" in columns else "NULL"
    size = "attachment_size" if "attachment_size" in columns else "NULL"
    return connection.execute(
        f"""
        SELECT id, pst_source, folder_path, logical_locator,
               {name} AS attachment_name, {size} AS attachment_size, created_at
        FROM ingest_failures
        WHERE {_unresolved_attachment_where(connection)}
        ORDER BY id
        """
    ).fetchall()


def find_unresolved_attachment_failure(
    connection: sqlite3.Connection, failure_id: int
) -> sqlite3.Row | None:
    return next(
        (
            r
            for r in unresolved_attachment_failures(connection)
            if r["id"] == failure_id
        ),
        None,
    )


class AbandonError(ValueError):
    """The named failure does not exist or is not an open attachment failure."""


def abandon_attachment_failure(
    connection: sqlite3.Connection, failure_id: int, reason: str
) -> sqlite3.Row:
    """Close one open attachment failure as abandoned (human decision, #256).

    Stored as ``resolved_at`` (time) + ``abandon_reason``; every existing
    ``resolved_at IS NULL`` filter then skips it, so it is never retried or
    counted. Returns the closed row (for locating the quarantined mail).
    Needs the migrated schema: call after ``initialize_database``.
    """
    if not reason.strip():
        raise AbandonError("a non-empty reason is required")
    row = find_unresolved_attachment_failure(connection, failure_id)
    if row is None:
        raise AbandonError(f"no unresolved attachment failure with id {failure_id}")
    connection.execute(
        "UPDATE ingest_failures SET resolved_at = CURRENT_TIMESTAMP,"
        " abandon_reason = ? WHERE id = ?",
        [reason.strip(), failure_id],
    )
    connection.commit()
    return row

"""Migrate suffix-bearing attachment CAS paths to digest-only filenames.

The command is dry-run by default and requires explicit database and attachment
root paths.  It never resolves the production defaults used by ``bes``.

Usage:
    uv run python scripts/migrate_attachment_cas.py \
        --database <copy.db> --attachment-root <copy-root>
    uv run python scripts/migrate_attachment_cas.py \
        --database <copy.db> --attachment-root <copy-root> --confirm
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import uuid
from collections import defaultdict
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class MigrationError(RuntimeError):
    """Raised when CAS preflight or migration execution cannot safely continue."""


@dataclass(frozen=True, slots=True)
class CasFile:
    digest: str
    physical_path: Path
    storage_path: str
    canonical: bool


@dataclass(frozen=True, slots=True)
class AttachmentRowPlan:
    row_id: int
    content_hash: str
    old_storage_path: str
    canonical_storage_path: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    files_by_digest: dict[str, tuple[CasFile, ...]]
    legacy_files: tuple[CasFile, ...]
    row_plans: tuple[AttachmentRowPlan, ...]
    storage_path_rows: int
    rows_unchanged: int

    @property
    def physical_files_before(self) -> int:
        return sum(len(files) for files in self.files_by_digest.values())

    @property
    def canonical_payloads_retained(self) -> int:
        return len(self.files_by_digest)

    @property
    def redundant_payload_copies(self) -> int:
        return self.physical_files_before - self.canonical_payloads_retained

    @property
    def file_reconciliation_difference(self) -> int:
        return self.physical_files_before - (
            self.canonical_payloads_retained + self.redundant_payload_copies
        )

    @property
    def row_reconciliation_difference(self) -> int:
        return self.storage_path_rows - (len(self.row_plans) + self.rows_unchanged)

    @property
    def multi_path_hash_groups_before(self) -> int:
        return sum(len(files) > 1 for files in self.files_by_digest.values())


def _digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError as exc:
        raise MigrationError(f"could not read CAS file {path}: {exc}") from exc
    return hasher.hexdigest()


def _canonical_storage_path(digest: str) -> str:
    return f"attachments/{digest[:2]}/{digest[2:4]}/{digest}"


def _storage_path_for_file(path: Path, attachment_root: Path) -> str:
    relative = path.relative_to(attachment_root).as_posix()
    return f"attachments/{relative}"


def _resolve_row_path(storage_path: str, attachment_root: Path) -> Path:
    posix_path = PurePosixPath(storage_path)
    if posix_path.is_absolute() or not posix_path.parts:
        raise MigrationError(f"attachment storage path escapes root: {storage_path!r}")
    if posix_path.parts[0] == "attachments":
        posix_path = PurePosixPath(*posix_path.parts[1:])
    if not posix_path.parts or ".." in posix_path.parts:
        raise MigrationError(f"attachment storage path escapes root: {storage_path!r}")

    resolved_root = attachment_root.resolve()
    resolved_path = resolved_root.joinpath(*posix_path.parts).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise MigrationError(f"attachment storage path escapes root: {storage_path!r}")
    return resolved_path


def _scan_cas(attachment_root: Path) -> dict[str, tuple[CasFile, ...]]:
    root = attachment_root.expanduser().resolve()
    if not root.is_dir():
        raise MigrationError(f"attachment root is not a directory: {root}")

    grouped: dict[str, list[CasFile]] = defaultdict(list)
    for physical_path in sorted(path for path in root.rglob("*") if path.is_file()):
        filename = physical_path.name.lower()
        if _DIGEST_RE.fullmatch(filename):
            digest = filename
            canonical = True
        else:
            digest = physical_path.stem.lower()
            if not _DIGEST_RE.fullmatch(digest) or not physical_path.suffix:
                raise MigrationError(f"invalid CAS filename: {physical_path}")
            canonical = False

        expected_parent = root / digest[:2] / digest[2:4]
        if physical_path.parent.resolve() != expected_parent.resolve():
            raise MigrationError(
                f"CAS file is outside its digest bucket: {physical_path}"
            )
        actual_digest = _digest_file(physical_path)
        if actual_digest != digest:
            raise MigrationError(
                f"CAS payload digest mismatch: {physical_path} "
                f"(filename={digest}, actual={actual_digest})"
            )
        grouped[digest].append(
            CasFile(
                digest=digest,
                physical_path=physical_path,
                storage_path=_storage_path_for_file(physical_path, root),
                canonical=canonical,
            )
        )

    return {digest: tuple(files) for digest, files in grouped.items()}


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    database = database_path.expanduser().resolve()
    if not database.is_file():
        raise MigrationError(f"database is not a file: {database}")
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _plan_rows(
    connection: sqlite3.Connection,
    attachment_root: Path,
    files_by_digest: dict[str, tuple[CasFile, ...]],
) -> tuple[tuple[AttachmentRowPlan, ...], int, int]:
    rows = connection.execute(
        """
        SELECT id, content_hash, storage_path
        FROM message_attachments
        WHERE storage_path IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    file_lookup = {
        file.physical_path.resolve(): file
        for files in files_by_digest.values()
        for file in files
    }
    plans: list[AttachmentRowPlan] = []
    unchanged = 0
    for row in rows:
        row_id = int(row["id"])
        content_hash = str(row["content_hash"] or "")
        storage_path = str(row["storage_path"])
        if not _DIGEST_RE.fullmatch(content_hash):
            raise MigrationError(
                f"attachment row {row_id} has invalid content_hash: {content_hash!r}"
            )
        physical_path = _resolve_row_path(storage_path, attachment_root)
        if not physical_path.is_file():
            raise MigrationError(
                f"referenced attachment is missing for row {row_id}: {storage_path}"
            )
        cas_file = file_lookup.get(physical_path.resolve())
        if cas_file is None:
            raise MigrationError(
                f"attachment row {row_id} references an unvalidated CAS file: "
                f"{storage_path}"
            )
        if cas_file.digest != content_hash:
            raise MigrationError(
                f"attachment row {row_id} hash/path conflict: "
                f"content_hash={content_hash}, path_digest={cas_file.digest}"
            )

        canonical_storage_path = _canonical_storage_path(content_hash)
        if storage_path == canonical_storage_path:
            unchanged += 1
            continue
        plans.append(
            AttachmentRowPlan(
                row_id=row_id,
                content_hash=content_hash,
                old_storage_path=storage_path,
                canonical_storage_path=canonical_storage_path,
            )
        )

    return tuple(plans), len(rows), unchanged


def build_migration_plan(
    database_path: Path,
    attachment_root: Path,
) -> MigrationPlan:
    root = attachment_root.expanduser().resolve()
    files_by_digest = _scan_cas(root)
    connection = _connect_read_only(database_path)
    try:
        return _build_plan_from_connection(
            connection,
            root,
            files_by_digest=files_by_digest,
        )
    finally:
        connection.close()


def _build_plan_from_connection(
    connection: sqlite3.Connection,
    attachment_root: Path,
    *,
    files_by_digest: dict[str, tuple[CasFile, ...]] | None = None,
) -> MigrationPlan:
    root = attachment_root.expanduser().resolve()
    if files_by_digest is None:
        files_by_digest = _scan_cas(root)
    row_plans, storage_path_rows, rows_unchanged = _plan_rows(
        connection, root, files_by_digest
    )

    legacy_files = tuple(
        file
        for digest in sorted(files_by_digest)
        for file in files_by_digest[digest]
        if not file.canonical
    )
    plan = MigrationPlan(
        files_by_digest=files_by_digest,
        legacy_files=legacy_files,
        row_plans=row_plans,
        storage_path_rows=storage_path_rows,
        rows_unchanged=rows_unchanged,
    )
    if plan.file_reconciliation_difference or plan.row_reconciliation_difference:
        raise MigrationError("migration plan reconciliation difference is non-zero")
    return plan


def _canonical_physical_path(attachment_root: Path, digest: str) -> Path:
    return attachment_root / digest[:2] / digest[2:4] / digest


def _sync_file(path: Path) -> None:
    with path.open("rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _sync_windows_directory(directory: Path) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [wintypes.HANDLE]
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    generic_write = 0x40000000
    share_read_write_delete = 0x00000007
    open_existing = 3
    backup_semantics = 0x02000000
    handle = create_file(
        str(directory),
        generic_write,
        share_read_write_delete,
        None,
        open_existing,
        backup_semantics,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not flush_file_buffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        _sync_windows_directory(directory)
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_canonical_file(source: Path, target: Path, digest: str) -> None:
    if target.exists():
        if not target.is_file() or _digest_file(target) != digest:
            raise MigrationError(
                f"canonical target exists with unexpected content: {target}"
            )
        _sync_file(target)
        _sync_directory(target.parent)
        return

    try:
        os.link(source, target)
    except FileExistsError:
        pass
    except OSError:
        attachment_root = target.parents[2]
        staging_directory = (
            attachment_root.parent / ".project-runtime" / "attachment-cas-migration"
        )
        staging_directory.mkdir(parents=True, exist_ok=True)
        temporary = staging_directory / f"{uuid.uuid4().hex}.tmp"
        try:
            shutil.copyfile(source, temporary)
            if _digest_file(temporary) != digest:
                raise MigrationError(
                    f"staged canonical payload digest mismatch: {temporary}"
                )
            _sync_file(temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                staging_directory.rmdir()

    if not target.is_file() or _digest_file(target) != digest:
        raise MigrationError(f"failed to stage verified canonical file: {target}")
    _sync_file(target)
    _sync_directory(target.parent)


def _update_rows(
    connection: sqlite3.Connection,
    row_plans: tuple[AttachmentRowPlan, ...],
) -> int:
    rewritten = 0
    for row_plan in row_plans:
        cursor = connection.execute(
            """
            UPDATE message_attachments
            SET storage_path = ?
            WHERE id = ? AND content_hash = ? AND storage_path = ?
            """,
            [
                row_plan.canonical_storage_path,
                row_plan.row_id,
                row_plan.content_hash,
                row_plan.old_storage_path,
            ],
        )
        if cursor.rowcount != 1:
            raise MigrationError(
                f"attachment row changed during migration: {row_plan.row_id}"
            )
        rewritten += 1
    return rewritten


def _remove_legacy_file(path: Path) -> None:
    path.unlink()


def _apply_confirmed_migration(
    database_path: Path,
    attachment_root: Path,
    expected_plan: MigrationPlan,
) -> tuple[MigrationPlan, int, int, list[tuple[Path, OSError]]]:
    database = database_path.expanduser().resolve()
    root = attachment_root.expanduser().resolve()
    if not database.is_file():
        raise MigrationError(f"database is not a file: {database}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            plan = _build_plan_from_connection(connection, root)
            if plan != expected_plan:
                raise MigrationError(
                    "migration plan changed before locked execution; "
                    "run dry-run again and review the new plan"
                )
            for digest, files in plan.files_by_digest.items():
                target = _canonical_physical_path(root, digest)
                _stage_canonical_file(files[0].physical_path, target, digest)
            rewritten = _update_rows(connection, plan.row_plans)
            for row_plan in plan.row_plans:
                target = _canonical_physical_path(root, row_plan.content_hash)
                if (
                    not target.is_file()
                    or _digest_file(target) != row_plan.content_hash
                ):
                    raise MigrationError(
                        f"canonical row target failed verification: "
                        f"{row_plan.canonical_storage_path}"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()

    deleted = 0
    cleanup_failures: list[tuple[Path, OSError]] = []
    for legacy_file in plan.legacy_files:
        try:
            _remove_legacy_file(legacy_file.physical_path)
        except OSError as exc:
            cleanup_failures.append((legacy_file.physical_path, exc))
            continue
        deleted += 1
    return plan, rewritten, deleted, cleanup_failures


def _post_migration_counts(attachment_root: Path) -> tuple[int, int, int]:
    files_by_digest = _scan_cas(attachment_root)
    physical_files = sum(len(files) for files in files_by_digest.values())
    legacy_files = sum(
        not file.canonical for files in files_by_digest.values() for file in files
    )
    multi_path_groups = sum(len(files) > 1 for files in files_by_digest.values())
    return physical_files, legacy_files, multi_path_groups


def _print_plan(plan: MigrationPlan, *, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else "[confirm] "
    for legacy_file in plan.legacy_files:
        print(f"{prefix}delete: {legacy_file.storage_path}")
    print(f"{prefix}physical_files_before: {plan.physical_files_before}")
    print(f"{prefix}canonical_payloads_retained: {plan.canonical_payloads_retained}")
    print(f"{prefix}redundant_payload_copies: {plan.redundant_payload_copies}")
    print(
        f"{prefix}file_reconciliation_difference: {plan.file_reconciliation_difference}"
    )
    print(f"{prefix}storage_path_rows: {plan.storage_path_rows}")
    print(f"{prefix}rows_to_rewrite: {len(plan.row_plans)}")
    print(f"{prefix}rows_unchanged: {plan.rows_unchanged}")
    print(
        f"{prefix}row_reconciliation_difference: {plan.row_reconciliation_difference}"
    )
    print(
        f"{prefix}multi_path_hash_groups_before: {plan.multi_path_hash_groups_before}"
    )


def run_migration(
    database_path: Path,
    attachment_root: Path,
    *,
    confirm: bool,
) -> int:
    if not confirm:
        plan = build_migration_plan(database_path, attachment_root)
        _print_plan(plan, dry_run=True)
        print("[dry-run] no files or rows changed; re-run with --confirm to execute.")
        return 0

    expected_plan = build_migration_plan(database_path, attachment_root)
    plan, rewritten, deleted, cleanup_failures = _apply_confirmed_migration(
        database_path,
        attachment_root,
        expected_plan,
    )
    _print_plan(plan, dry_run=False)
    (
        physical_files_after,
        legacy_files_after,
        multi_path_groups_after,
    ) = _post_migration_counts(attachment_root)
    print(f"[confirm] rows_rewritten: {rewritten}")
    print(f"[confirm] legacy_files_deleted: {deleted}")
    print(f"[confirm] physical_files_after: {physical_files_after}")
    print(f"[confirm] legacy_files_after: {legacy_files_after}")
    print(f"[confirm] multi_path_hash_groups_after: {multi_path_groups_after}")
    for path, exc in cleanup_failures:
        print(f"[error] legacy cleanup failed for {path}: {exc}", file=sys.stderr)
    if physical_files_after != plan.canonical_payloads_retained:
        print(
            "[error] physical file count after migration does not match "
            f"canonical payload count: {physical_files_after} != "
            f"{plan.canonical_payloads_retained}",
            file=sys.stderr,
        )
    if legacy_files_after:
        print(
            f"[error] legacy CAS paths remain after migration: {legacy_files_after}",
            file=sys.stderr,
        )
    if multi_path_groups_after:
        print(
            "[error] multi-path hash groups remain after migration: "
            f"{multi_path_groups_after}",
            file=sys.stderr,
        )
    if (
        cleanup_failures
        or physical_files_after != plan.canonical_payloads_retained
        or legacy_files_after
        or multi_path_groups_after
    ):
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate suffix-bearing attachment CAS paths to digest-only paths. "
            "Dry-run is the default."
        )
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--attachment-root", required=True, type=Path)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Execute the validated migration; omitted means read-only dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run_migration(
            args.database.expanduser().resolve(),
            args.attachment_root.expanduser().resolve(),
            confirm=args.confirm,
        )
    except (MigrationError, sqlite3.Error, OSError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

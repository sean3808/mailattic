from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .export_files import (
    format_sent_date_token,
    land_exported_file,
    sanitize_windows_filename,
    truncate_export_filename,
)
from .storage import connect_database


class AttachmentStoreError(FileNotFoundError):
    """Raised when a requested attachment path does not exist in the store."""


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    content_hash: str
    size_bytes: int
    mime_type: str | None
    storage_path: str


@dataclass(frozen=True, slots=True)
class CasVerifyResult:
    total: int
    ok: int
    mismatched: int
    read_error: int
    bad_files: list[Path]
    non_cas_files: list[Path] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MissingCasReference:
    attachment_id: int
    db_id: int
    storage_path: str


@dataclass(frozen=True, slots=True)
class CasReferenceResult:
    referenced: int  # attachment rows with a non-NULL storage_path
    distinct_paths: int
    missing: list[MissingCasReference]


@dataclass(frozen=True, slots=True)
class AttachmentExportItem:
    attachment_id: int
    # "skipped-blocked-type" only when the caller passes refuse_filename --
    # the MCP writer does (#205); the CLI does not.
    status: Literal[
        "exported",
        "skipped-existing",
        "skipped-non-exportable",
        "skipped-blocked-type",
        "failed",
    ]
    destination_path: Path | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentExportResult:
    copied: int
    skipped_non_exportable: int
    skipped_existing: int
    failed: int
    report_lines: list[str]
    items: list[AttachmentExportItem]


def write_attachment(
    payload: bytes,
    original_name: str,
    mime_type: str | None,
    store_root: Path,
) -> AttachmentRecord:
    """Write attachment payload to a content-addressable store."""
    digest = hashlib.sha256(payload).hexdigest()
    bucket_l1 = digest[0:2]
    bucket_l2 = digest[2:4]
    filename = digest
    bucket_dir = store_root / bucket_l1 / bucket_l2
    file_path = bucket_dir / filename
    storage_path = f"attachments/{bucket_l1}/{bucket_l2}/{filename}"

    already_complete = (
        file_path.exists()
        and file_path.stat().st_size == len(payload)
        and file_path.read_bytes() == payload
    )
    if not already_complete:
        # A short-write (OSError mid-write), a pre-existing truncated file, or
        # a same-size corrupted file all land here; write to a sibling temp
        # file and atomically replace the target so the final path never
        # observes anything but the requested payload.
        bucket_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = bucket_dir / f"{filename}.{uuid.uuid4().hex}.tmp"
        try:
            tmp_path.write_bytes(payload)
            tmp_path.replace(file_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    return AttachmentRecord(
        content_hash=digest,
        size_bytes=len(payload),
        mime_type=mime_type,
        storage_path=storage_path,
    )


def read_attachment(storage_path: str, store_root: Path) -> bytes:
    """Read attachment bytes by storage_path."""
    file_path = _resolve_store_file_path(storage_path, store_root)
    if not file_path.exists():
        raise AttachmentStoreError(f"Attachment not found in store: {storage_path!r}")
    return file_path.read_bytes()


def resolve_store_root(
    database_path: Path,
    cli_override: Path | None = None,
    env_override: str | None = None,
) -> Path:
    """Resolve the attachments/ directory path."""
    if cli_override is not None:
        return cli_override.expanduser().resolve()
    if env_override is not None:
        return Path(env_override).expanduser().resolve()
    return database_path.expanduser().resolve().parent / "attachments"


_HEX_2 = re.compile(r"^[0-9a-fA-F]{2}$")
_CAS_FILENAME = re.compile(r"^[0-9a-fA-F]{64}(?:\.[a-zA-Z0-9]+)?$")


def is_cas_entry_shape(file_path: Path, store_root: Path | None = None) -> bool:
    """Return True if file_path conforms to the CAS store layout.

    Layout: <root>/<l1>/<l2>/<digest> where <l1> and <l2> are the first 4
    hex characters of the 64-hex SHA-256 digest (optional legacy single extension
    allowed).
    """
    if store_root is not None:
        try:
            rel = file_path.resolve().relative_to(store_root.resolve())
        except ValueError:
            return False
        parts = rel.parts
        if len(parts) != 3:
            return False
        l1, l2, filename = parts
    else:
        parts = file_path.parts
        if len(parts) < 3:
            return False
        l1, l2, filename = parts[-3:]

    if not _HEX_2.match(l1) or not _HEX_2.match(l2):
        return False
    if not _CAS_FILENAME.match(filename):
        return False
    return filename[:2].lower() == l1.lower() and filename[2:4].lower() == l2.lower()


def verify_cas(attachment_root: Path) -> CasVerifyResult:
    """Verify content-addressed attachment files against their SHA-256 filenames."""
    total = 0
    ok = 0
    mismatched = 0
    read_error = 0
    bad_files: list[Path] = []
    non_cas_files: list[Path] = []

    files = sorted(path for path in attachment_root.rglob("*") if path.is_file())
    for file_path in files:
        total += 1
        if not is_cas_entry_shape(file_path, attachment_root):
            non_cas_files.append(file_path)
            continue

        try:
            payload = file_path.read_bytes()
        except Exception:
            read_error += 1
            bad_files.append(file_path)
            continue

        actual_digest = hashlib.sha256(payload).hexdigest()
        expected_digest = file_path.stem.lower()
        if actual_digest == expected_digest:
            ok += 1
            continue

        mismatched += 1
        bad_files.append(file_path)

    return CasVerifyResult(
        total=total,
        ok=ok,
        mismatched=mismatched,
        read_error=read_error,
        bad_files=bad_files,
        non_cas_files=non_cas_files,
    )


def verify_cas_references(
    database_path: Path, attachment_root: Path
) -> CasReferenceResult:
    """Reconcile every DB-referenced storage_path against the store (read-only).

    Existence only, no payload reads; rows with NULL storage_path are
    legitimate non-exportable placeholders and are not references.
    """
    connection = connect_database(database_path, create=False, read_only=True)
    try:
        rows = connection.execute(
            "SELECT id, message_id, storage_path FROM message_attachments "
            "WHERE storage_path IS NOT NULL ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    exists_cache: dict[str, bool] = {}
    missing: list[MissingCasReference] = []
    for attachment_id, db_id, storage_path in rows:
        if storage_path not in exists_cache:
            try:
                exists_cache[storage_path] = _resolve_store_file_path(
                    storage_path, attachment_root
                ).is_file()
            except ValueError:
                exists_cache[storage_path] = False
        if not exists_cache[storage_path]:
            missing.append(MissingCasReference(attachment_id, db_id, storage_path))
    return CasReferenceResult(
        referenced=len(rows), distinct_paths=len(exists_cache), missing=missing
    )


def prune_cas(bad_files: list[Path]) -> tuple[int, int]:
    """Delete files already identified as invalid by CAS verification."""
    deleted = 0
    failed = 0

    for file_path in bad_files:
        if not is_cas_entry_shape(file_path):
            failed += 1
            continue
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            failed += 1
            continue
        deleted += 1

    return deleted, failed


def build_attachment_export_filename(
    attachment_row: Mapping[str, object],
    destination_dir: Path,
) -> str:
    sent_date = format_sent_date_token(attachment_row.get("sent_at"))
    attachment_id = int(attachment_row["attachment_id"])
    original_name = str(attachment_row.get("attachment_name") or "")
    if _has_usable_extension(original_name):
        candidate_name = sanitize_windows_filename(original_name)
    else:
        content_hash = str(attachment_row.get("content_hash") or "unknown-hash")
        inferred_ext = _infer_attachment_extension(
            mime_type=attachment_row.get("mime_type"),
        )
        candidate_name = sanitize_windows_filename(f"{content_hash}{inferred_ext}")
    prefix = f"{sent_date}_att{attachment_id}_"
    return truncate_export_filename(prefix, candidate_name, destination_dir)


def export_attachments(
    *,
    attachment_rows: list[Mapping[str, object]],
    store_root: Path,
    destination_dir: Path,
    overwrite: bool = False,
    skip_existing: bool = False,
    verify_content_hash: bool = False,
    refuse_filename: Callable[[str], str | None] | None = None,
) -> AttachmentExportResult:
    """Copy each row's stored payload into *destination_dir*.

    ``refuse_filename`` is asked about the exact name each file would land
    as; a non-None answer is the reason that row is reported
    ``skipped-blocked-type`` without reading its payload or writing anything.
    """
    if overwrite and skip_existing:
        raise ValueError(
            "conflicting export flags: cannot use both overwrite and skip-existing"
        )

    destination_dir = destination_dir.expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped_non_exportable = 0
    skipped_existing = 0
    failed = 0
    report_lines: list[str] = []
    items: list[AttachmentExportItem] = []

    for row in attachment_rows:
        attachment_id = int(row["attachment_id"])
        attachment_name = str(row.get("attachment_name") or "<unnamed>")
        if row.get("storage_path") is None:
            skipped_non_exportable += 1
            report_lines.append(
                f"skipped-non-exportable: att#{attachment_id} {attachment_name} "
                "(no stored payload)"
            )
            items.append(
                AttachmentExportItem(attachment_id, "skipped-non-exportable", None)
            )
            continue

        destination_path: Path | None = None
        try:
            filename = build_attachment_export_filename(row, destination_dir)
            refusal = refuse_filename(filename) if refuse_filename else None
            if refusal is not None:
                items.append(
                    AttachmentExportItem(
                        attachment_id, "skipped-blocked-type", None, refusal
                    )
                )
                continue
            destination_path = destination_dir / filename

            if skip_existing and destination_path.exists():
                skipped_existing += 1
                items.append(
                    AttachmentExportItem(
                        attachment_id, "skipped-existing", destination_path
                    )
                )
                continue
            if not overwrite and destination_path.exists():
                raise FileExistsError(str(destination_path))

            payload = read_attachment(str(row["storage_path"]), store_root)
            if verify_content_hash and hashlib.sha256(payload).hexdigest() != row.get(
                "content_hash"
            ):
                raise ValueError("stored payload does not match its content_hash")
            try:
                land_exported_file(
                    destination_path,
                    overwrite=overwrite,
                    payload=payload,
                )
            except FileExistsError:
                if destination_path.exists():
                    skipped_existing += 1
                    items.append(
                        AttachmentExportItem(
                            attachment_id, "skipped-existing", destination_path
                        )
                    )
                    continue
                raise
        except FileExistsError:
            failed += 1
            report_lines.append(
                f"failed: att#{attachment_id} {attachment_name} (destination exists)"
            )
            items.append(
                AttachmentExportItem(
                    attachment_id, "failed", destination_path, "destination exists"
                )
            )
            continue
        except (AttachmentStoreError, OSError, ValueError) as exc:
            failed += 1
            report_lines.append(
                f"failed: att#{attachment_id} {attachment_name} ({exc})"
            )
            items.append(
                AttachmentExportItem(
                    attachment_id, "failed", destination_path, str(exc)
                )
            )
            continue

        copied += 1
        items.append(AttachmentExportItem(attachment_id, "exported", destination_path))

    return AttachmentExportResult(
        copied=copied,
        skipped_non_exportable=skipped_non_exportable,
        skipped_existing=skipped_existing,
        failed=failed,
        report_lines=report_lines,
        items=items,
    )


def _resolve_store_file_path(storage_path: str, store_root: Path) -> Path:
    prefix = "attachments/"
    if storage_path.startswith(prefix):
        relative_path = storage_path[len(prefix) :]
    else:
        relative_path = storage_path
    relative = Path(relative_path)
    if relative.is_absolute() or relative.drive or relative.root:
        raise ValueError(f"Attachment path escapes store root: {storage_path!r}")
    if any(part == ".." for part in relative.parts):
        raise ValueError(f"Attachment path escapes store root: {storage_path!r}")

    resolved_root = store_root.expanduser().resolve()
    resolved_path = (resolved_root / relative).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError(f"Attachment path escapes store root: {storage_path!r}")
    return resolved_path


def _has_usable_extension(name: str) -> bool:
    candidate = Path(name.strip())
    return bool(candidate.name and candidate.suffix and candidate.stem)


def _infer_attachment_extension(
    *,
    mime_type: object,
) -> str:
    guessed = mimetypes.guess_extension(str(mime_type)) if mime_type else None
    if guessed:
        return guessed.lower()
    return ".bin"

"""Shared file-naming and atomic file-landing helpers for exporters."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from pathlib import Path

_WINDOWS_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *{f"COM{index}" for index in range(1, 10)},
    *{f"LPT{index}" for index in range(1, 10)},
}
_MAX_EXPORT_PATH = 240
EXPORT_TMP_PREFIX = ".mailattic-export-"


def sanitize_windows_filename(filename: str) -> str:
    """Return *filename* made safe for Windows paths."""
    cleaned = _WINDOWS_ILLEGAL_CHARS.sub("_", filename).strip().rstrip(" .")
    if not cleaned:
        cleaned = "attachment"

    suffixes = "".join(Path(cleaned).suffixes)
    stem = cleaned[: -len(suffixes)] if suffixes else cleaned
    stem = stem.rstrip(" .") or "attachment"
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"{stem}_"
    return f"{stem}{suffixes}"


def truncate_export_filename(
    prefix: str,
    candidate_name: str,
    destination_dir: Path,
    *,
    max_path_length: int = _MAX_EXPORT_PATH,
) -> str:
    """Truncate *candidate_name* with a short hash suffix to fit *max_path_length*."""
    filename = f"{prefix}{candidate_name}"
    if len(str(destination_dir / filename)) <= max_path_length:
        return filename

    suffixes = "".join(Path(candidate_name).suffixes)
    stem = candidate_name[: -len(suffixes)] if suffixes else candidate_name
    hash_suffix = "_" + hashlib.sha1(candidate_name.encode("utf-8")).hexdigest()[:8]
    available = (
        max_path_length
        - len(str(destination_dir))
        - 1
        - len(prefix)
        - len(suffixes)
        - len(hash_suffix)
    )
    truncated_stem = stem[: max(1, available)].rstrip(" ._") or "file"
    filename = f"{prefix}{truncated_stem}{hash_suffix}{suffixes}"
    while (
        len(str(destination_dir / filename)) > max_path_length
        and len(truncated_stem) > 1
    ):
        truncated_stem = truncated_stem[:-1].rstrip(" ._") or truncated_stem[:-1] or "f"
        filename = f"{prefix}{truncated_stem}{hash_suffix}{suffixes}"
    return filename


def format_sent_date_token(sent_at: object) -> str:
    """Extract a ``YYYY-MM-DD`` prefix token from *sent_at*, or ``unknown-date``."""
    if not sent_at:
        return "unknown-date"
    sent_at_text = str(sent_at)
    if "T" in sent_at_text:
        return sent_at_text.split("T", 1)[0]
    return sent_at_text[:10] if len(sent_at_text) >= 10 else "unknown-date"


def land_exported_file(
    destination_path: Path,
    *,
    overwrite: bool,
    payload: bytes | None = None,
    save_to_tmp: Callable[[Path], None] | None = None,
) -> None:
    """Write an exported file to a sibling temp file, then move it into place.

    Exactly one of *payload* or *save_to_tmp* must be provided.

    Every failure path only ever unlinks a temp file we created ourselves
    (never *destination_path* by pathname), so a foreign file that lands at
    *destination_path* during the write is never touched.
    """
    if (payload is None) == (save_to_tmp is None):
        raise ValueError("Provide exactly one of payload or save_to_tmp.")

    tmp_path = destination_path.parent / f"{EXPORT_TMP_PREFIX}{uuid.uuid4().hex}.tmp"
    if payload is not None:
        # Cleanup responsibility starts only once `open("xb")` itself
        # has succeeded: if the open call raises (e.g. a foreign
        # file already sits at this exact temp path), that path was
        # never ours to unlink. Only a write failure on an already-
        # opened handle -- which we did create -- triggers cleanup.
        # Residual risk accepted (issue #28, 2026-09-24 decision):
        # a same-user malicious local process racing the directory
        # between handle close and rename/unlink is out of scope --
        # it needs no new privilege to do that damage.
        handle = tmp_path.open("xb")
        try:
            with handle:
                handle.write(payload)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
    else:
        assert save_to_tmp is not None
        # Do not pre-create tmp_path: callers like Outlook SaveAs write directly
        # to a pathname and have not been verified to overwrite an empty file.
        try:
            save_to_tmp(tmp_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    try:
        if overwrite:
            tmp_path.replace(destination_path)
        else:
            # Windows os.rename (which Path.rename wraps) refuses to
            # replace an existing file, making it a no-overwrite move
            # primitive: if a file now sits at destination_path (a
            # race since the caller's exists() check), the move fails
            # and destination_path is left completely untouched.
            tmp_path.rename(destination_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

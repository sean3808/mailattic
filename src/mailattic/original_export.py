"""Original export (匯出原信): retrieves a stored message's original from source.

Issue #53, D-20261105-1, GLOSSARY.md:
- PST source via Outlook COM -> `.msg`
- IMAP source via IMAP server -> `.eml`
- When not found -> returns subject, sender, sent_at without raising an error
- Ambiguous/failed location never exports a different message
- Read/write failures are reported as failures, not success.
"""

from __future__ import annotations

import email
import imaplib
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .attachment_store import (
    _EXPORT_TMP_PREFIX,
    _format_sent_date_token,
    _sanitize_windows_filename,
    _truncate_export_filename,
)
from .mutf7 import encode_mutf7
from .outlook_pst import (
    PR_INTERNET_MESSAGE_ID,
    _normalized_windows_path,
)
from .query import (
    connect_database,
    normalize_message_id,
)


@dataclass(frozen=True)
class OriginalExportResult:
    """Outcome of an original export operation."""

    status: Literal["exported", "skipped-existing", "not_found", "failed"]
    dest_path: Path | None = None
    format: Literal["msg", "eml"] | None = None
    source_type: Literal["pst", "imap"] | None = None
    subject: str = ""
    sender: str = ""
    sent_at: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "dest_path": str(self.dest_path) if self.dest_path else None,
            "format": self.format,
            "source_type": self.source_type,
            "subject": self.subject,
            "sender": self.sender,
            "sent_at": self.sent_at,
            "detail": self.detail,
        }


def classify_message_source(
    message: Mapping[str, object],
) -> Literal["pst", "imap", "unsupported"]:
    """Classify message origin (PST, IMAP, or unsupported)."""
    pst_source = str(message.get("pst_source") or "").strip()
    input_mode = str(message.get("source_input_mode") or "").strip().casefold()

    if input_mode in ("outbound", "archive") or pst_source.casefold().startswith(
        ("outbound-", "archive:")
    ):
        return "unsupported"
    if pst_source.casefold().endswith(".pst") or input_mode in ("directory", "file"):
        return "pst"
    if (
        input_mode == "maildir"
        or pst_source.casefold().startswith("imap")
        or "uid" in str(message.get("logical_locator") or "").casefold()
    ):
        return "imap"
    return "imap" if not pst_source and not input_mode else "unsupported"


def build_original_export_filename(
    message: Mapping[str, object],
    destination_dir: Path,
    fmt: Literal["msg", "eml"],
) -> str:
    """Build a sanitized export filename: {date}_msg{id}_{subject}.{ext}."""
    sent_date = _format_sent_date_token(message.get("sent_at"))
    message_id = int(message.get("id") or 0)
    raw_subject = str(message.get("subject") or "").strip()
    if raw_subject:
        cleaned_subject = _sanitize_windows_filename(raw_subject)
    else:
        cleaned_subject = "message"

    candidate_name = f"{cleaned_subject}.{fmt}"
    prefix = f"{sent_date}_msg{message_id}_"
    return _truncate_export_filename(prefix, candidate_name, destination_dir)


def _format_sender_string(message: Mapping[str, object]) -> str:
    name = message.get("sender_name")
    email_addr = message.get("sender_email")
    if name and email_addr:
        return f"{name} <{email_addr}>"
    return str(email_addr or name or "")


def _verify_outlook_item(item: Any, message: Mapping[str, object]) -> bool:
    """Check whether *item* matches *message*, strictly preventing wrong-mail export."""
    db_msg_id = message.get("message_id")
    accessor = getattr(item, "PropertyAccessor", None)
    item_msg_id = None
    if accessor:
        try:
            item_msg_id = accessor.GetProperty(PR_INTERNET_MESSAGE_ID)
        except (KeyError, IndexError):
            item_msg_id = None
        except Exception as exc:
            hresult = getattr(exc, "hresult", None)
            if hresult is None and len(getattr(exc, "args", ())) > 0:
                hresult = exc.args[0]
            if hresult == -2147221233:  # MAPI_E_NOT_FOUND (0x8004010F)
                item_msg_id = None
            else:
                raise

    if db_msg_id:
        if not item_msg_id:
            return False
        return normalize_message_id(str(item_msg_id)) == normalize_message_id(
            str(db_msg_id)
        )

    db_subject = str(message.get("subject") or "").strip()
    item_subject = str(getattr(item, "Subject", None) or "").strip()
    return db_subject == item_subject


def _resolve_pst_store(namespace: Any, pst_source: str) -> Any | None:
    expected_path = _normalized_windows_path(pst_source)
    expected_name = Path(pst_source).name.casefold()
    stores = getattr(namespace, "Stores", None)
    if stores is None:
        return None

    count = int(getattr(stores, "Count", 0))
    # First pass: exact normalized path match
    for i in range(1, count + 1):
        try:
            store = stores.Item(i)
            fp = getattr(store, "FilePath", None)
            if fp and _normalized_windows_path(fp) == expected_path:
                return store
        except Exception:
            continue

    # Second pass: filename match (if PST path was relocated)
    for i in range(1, count + 1):
        try:
            store = stores.Item(i)
            fp = getattr(store, "FilePath", None)
            if fp and Path(fp).name.casefold() == expected_name:
                return store
        except Exception:
            continue

    return None


def _find_pst_folder_by_path(root_folder: Any, folder_path: str) -> Any | None:
    """Navigate Outlook folder hierarchy matching *folder_path*."""
    parts = [p.strip() for p in re.split(r"[/\\]", folder_path) if p.strip()]
    if not parts:
        return None
    current = root_folder
    for part in parts:
        child_folders = getattr(current, "Folders", None)
        if child_folders is None:
            return None
        count = int(getattr(child_folders, "Count", 0))
        found = None
        for i in range(1, count + 1):
            try:
                sub = child_folders.Item(i)
                if str(getattr(sub, "Name", "")).casefold() == part.casefold():
                    found = sub
                    break
            except Exception:
                continue
        if found is None:
            return None
        current = found
    return current


def _find_pst_item(
    namespace: Any,
    store: Any,
    message: Mapping[str, object],
) -> tuple[Any | None, str | None]:
    """Locate the target item in *store*, returning (item, error_or_ambiguity)."""
    logical_locator = str(message.get("logical_locator") or "").strip()
    db_msg_id = message.get("message_id")
    folder_path = str(message.get("folder_path") or "").strip()
    unreadable_count = 0

    # 1. Try logical_locator (EntryID)
    if logical_locator:
        try:
            store_id = getattr(store, "StoreID", None)
            candidate = (
                namespace.GetItemFromID(logical_locator, store_id)
                if store_id
                else namespace.GetItemFromID(logical_locator)
            )
            if candidate is not None and _verify_outlook_item(candidate, message):
                return candidate, None
        except Exception:
            pass

    try:
        root_folder = store.GetRootFolder()
    except Exception as exc:
        return None, f"PST root folder access failed: {exc}"

    if root_folder is None:
        return None, "PST root folder is unavailable"

    # 2. Try locating inside folder_path first
    if folder_path:
        target_folder = None
        try:
            target_folder = _find_pst_folder_by_path(root_folder, folder_path)
        except Exception:
            unreadable_count += 1

        if target_folder is not None:
            items = None
            try:
                items = getattr(target_folder, "Items", None)
            except Exception:
                unreadable_count += 1

            if items is not None:
                try:
                    count = int(getattr(items, "Count", 0))
                except Exception:
                    unreadable_count += 1
                    count = 0
                folder_candidates: list[Any] = []
                for i in range(1, count + 1):
                    try:
                        item = items.Item(i)
                        if _verify_outlook_item(item, message):
                            folder_candidates.append(item)
                    except Exception:
                        unreadable_count += 1
                        continue
                if len(folder_candidates) == 1:
                    return folder_candidates[0], None
                if len(folder_candidates) > 1:
                    first_body = getattr(folder_candidates[0], "Body", None) or ""
                    if all(
                        (getattr(c, "Body", None) or "") == first_body
                        for c in folder_candidates[1:]
                    ):
                        return folder_candidates[0], None
                    return (
                        None,
                        "Ambiguous Message-ID: multiple differing items found in PST",
                    )

    # 3. Fall back to scanning store by Message-ID
    if not db_msg_id:
        if unreadable_count > 0:
            return (
                None,
                f"Target item not found and {unreadable_count} items "
                "could not be read due to errors",
            )
        return None, None

    candidates: list[tuple[Any, str]] = []  # (item, folder_name)

    def scan_folder(folder: Any, current_path: str) -> None:
        nonlocal unreadable_count
        fname = str(getattr(folder, "Name", ""))
        full_p = f"{current_path}/{fname}" if current_path else fname
        try:
            items = getattr(folder, "Items", None)
        except Exception:
            unreadable_count += 1
            items = None

        if items is not None:
            try:
                count = int(getattr(items, "Count", 0))
            except Exception:
                unreadable_count += 1
                count = 0
            for i in range(1, count + 1):
                try:
                    item = items.Item(i)
                    if _verify_outlook_item(item, message):
                        candidates.append((item, full_p))
                except Exception:
                    unreadable_count += 1
                    continue
        try:
            child_folders = getattr(folder, "Folders", None)
        except Exception:
            unreadable_count += 1
            child_folders = None

        if child_folders is not None:
            try:
                count = int(getattr(child_folders, "Count", 0))
            except Exception:
                unreadable_count += 1
                count = 0
            for i in range(1, count + 1):
                try:
                    scan_folder(child_folders.Item(i), full_p)
                except Exception:
                    unreadable_count += 1
                    continue

    try:
        scan_folder(root_folder, "")
    except Exception as exc:
        return None, f"PST folder scan failed: {exc}"

    if candidates:
        if len(candidates) == 1:
            return candidates[0][0], None

        # Disambiguate by folder_path if provided
        if folder_path:
            norm_target = folder_path.casefold().replace("\\", "/")
            matching_candidates = [
                c[0]
                for c in candidates
                if norm_target in c[1].casefold() or c[1].casefold() in norm_target
            ]
            if len(matching_candidates) == 1:
                return matching_candidates[0], None

        # Multiple candidates: check if bodies are identical (D-20261106-1)
        items_only = [c[0] for c in candidates]
        first_body = getattr(items_only[0], "Body", None) or ""
        all_identical = all(
            (getattr(c, "Body", None) or "") == first_body for c in items_only[1:]
        )
        if all_identical:
            return items_only[0], None

        return None, "Ambiguous Message-ID: multiple differing items found in PST"

    if unreadable_count > 0:
        return (
            None,
            f"Target item not found and {unreadable_count} items "
            "could not be read due to errors",
        )

    return None, None


def _load_default_outlook_reader() -> Any | None:
    try:
        from .outlook_pst import OutlookComReader

        return OutlookComReader()
    except Exception:
        return None


def _write_original_file(
    destination_path: Path,
    write_fn: Callable[[Path], None],
    *,
    overwrite: bool,
    skip_existing: bool,
    format: Literal["msg", "eml"],
    source_type: Literal["pst", "imap"],
    subject: str,
    sender: str,
    sent_at: str,
) -> OriginalExportResult:
    """Atomic write via sibling temp file, handling overwrite and skip-existing."""
    destination_dir = destination_path.parent
    destination_dir.mkdir(parents=True, exist_ok=True)

    if destination_path.exists():
        if skip_existing:
            return OriginalExportResult(
                status="skipped-existing",
                dest_path=destination_path,
                format=format,
                source_type=source_type,
                subject=subject,
                sender=sender,
                sent_at=sent_at,
            )
        if not overwrite:
            return OriginalExportResult(
                status="failed",
                dest_path=destination_path,
                format=format,
                source_type=source_type,
                subject=subject,
                sender=sender,
                sent_at=sent_at,
                detail=f"Destination exists: {destination_path}",
            )

    tmp_path = destination_dir / f"{_EXPORT_TMP_PREFIX}{uuid.uuid4().hex}.tmp"
    try:
        write_fn(tmp_path)
        if overwrite:
            tmp_path.replace(destination_path)
        else:
            tmp_path.rename(destination_path)
    except FileExistsError:
        tmp_path.unlink(missing_ok=True)
        if skip_existing:
            return OriginalExportResult(
                status="skipped-existing",
                dest_path=destination_path,
                format=format,
                source_type=source_type,
                subject=subject,
                sender=sender,
                sent_at=sent_at,
            )
        return OriginalExportResult(
            status="failed",
            dest_path=destination_path,
            format=format,
            source_type=source_type,
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail=f"Destination exists: {destination_path}",
        )
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        return OriginalExportResult(
            status="failed",
            dest_path=None,
            format=format,
            source_type=source_type,
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail=f"Writing original file failed: {exc}",
        )

    return OriginalExportResult(
        status="exported",
        dest_path=destination_path,
        format=format,
        source_type=source_type,
        subject=subject,
        sender=sender,
        sent_at=sent_at,
    )


def export_from_pst(
    message: Mapping[str, object],
    destination_path: Path,
    *,
    overwrite: bool = False,
    skip_existing: bool = False,
    outlook_reader: Any | None = None,
) -> OriginalExportResult:
    subject = str(message.get("subject") or "")
    sender = _format_sender_string(message)
    sent_at = str(message.get("sent_at") or "")

    reader = outlook_reader or _load_default_outlook_reader()
    if reader is None:
        return OriginalExportResult(
            status="failed",
            dest_path=None,
            format="msg",
            source_type="pst",
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail="Outlook is not running or unavailable.",
        )

    try:
        namespace = getattr(reader, "namespace", reader)
        if namespace is None:
            raise RuntimeError("Outlook namespace is None")
    except Exception as exc:
        return OriginalExportResult(
            status="failed",
            dest_path=None,
            format="msg",
            source_type="pst",
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail=f"Outlook COM is unavailable: {exc}",
        )

    pst_source = str(message.get("pst_source") or "").strip()
    store = _resolve_pst_store(namespace, pst_source)
    if store is None:
        return OriginalExportResult(
            status="not_found",
            dest_path=None,
            format="msg",
            source_type="pst",
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail=f"PST source not mounted in Outlook: {pst_source}",
        )

    item, err = _find_pst_item(namespace, store, message)
    if err is not None:
        return OriginalExportResult(
            status="failed",
            dest_path=None,
            format="msg",
            source_type="pst",
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail=err,
        )
    if item is None:
        return OriginalExportResult(
            status="not_found",
            dest_path=None,
            format="msg",
            source_type="pst",
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail="Original message not found in PST.",
        )

    def write_item(tmp_path: Path) -> None:
        item.SaveAs(str(tmp_path), 3)  # olMSG = 3

    return _write_original_file(
        destination_path,
        write_item,
        overwrite=overwrite,
        skip_existing=skip_existing,
        format="msg",
        source_type="pst",
        subject=subject,
        sender=sender,
        sent_at=sent_at,
    )


def _verify_imap_rfc822(rfc822: bytes, message: Mapping[str, object]) -> bool:
    """Verify that fetched RFC822 bytes match the stored message."""
    db_msg_id = message.get("message_id")
    try:
        parsed = email.message_from_bytes(rfc822)
        raw_msg_id = parsed.get("Message-ID")
        if db_msg_id:
            if not raw_msg_id:
                return False
            return normalize_message_id(str(raw_msg_id)) == normalize_message_id(
                str(db_msg_id)
            )
        db_subject = str(message.get("subject") or "").strip()
        item_subject = str(parsed.get("Subject") or "").strip()
        return db_subject == item_subject
    except Exception:
        return False


def _find_imap_rfc822(
    client: Any,
    message: Mapping[str, object],
) -> tuple[bytes | None, str | None]:
    """Retrieve verified RFC822 bytes from IMAP server."""
    folder_path = str(message.get("folder_path") or "INBOX").strip()
    try:
        wire_name = encode_mutf7(folder_path)
        client.select_folder(wire_name)
    except Exception as exc:
        return None, f"Failed to SELECT folder {folder_path!r} on IMAP server: {exc}"

    unreadable_count = 0

    # 1. Try UID from logical_locator
    locator = str(message.get("logical_locator") or "")
    match = re.search(r"(?:^|[\W_])(?:uid-|U=)(\d+)", locator, re.IGNORECASE)
    if match:
        uid = int(match.group(1))
        try:
            rfc822 = client.fetch_rfc822(uid)
            if rfc822 and _verify_imap_rfc822(rfc822, message):
                return rfc822, None
        except (
            imaplib.IMAP4.abort,
            imaplib.IMAP4.error,
            OSError,
            EOFError,
            TimeoutError,
        ) as exc:
            return None, f"IMAP error fetching UID {uid}: {exc}"
        except Exception:
            unreadable_count += 1

    # 2. Try Message-ID search
    db_msg_id = message.get("message_id")
    if not db_msg_id:
        if unreadable_count > 0:
            return (
                None,
                f"Target item not found and {unreadable_count} items "
                "could not be read due to errors",
            )
        return None, None

    clean_id = normalize_message_id(str(db_msg_id))
    raw_uids: list[int] = []

    for search_term in (f'"{clean_id}"', f'"<{clean_id}>"'):
        try:
            conn = getattr(client, "_conn", client)
            typ, data = conn.uid("SEARCH", "HEADER", "Message-ID", search_term)
            if typ != "OK":
                return None, f"IMAP SEARCH failed with status {typ!r}: {data}"
            if data and data[0]:
                raw = (
                    data[0].decode("ascii", errors="replace")
                    if isinstance(data[0], bytes)
                    else str(data[0])
                )
                raw_uids = [int(u) for u in raw.split() if u.isdigit()]
                if raw_uids:
                    break
        except (
            imaplib.IMAP4.abort,
            imaplib.IMAP4.error,
            OSError,
            EOFError,
            TimeoutError,
        ) as exc:
            return None, f"IMAP SEARCH protocol error: {exc}"
        except Exception as exc:
            return None, f"IMAP SEARCH failed: {exc}"

    if not raw_uids:
        if unreadable_count > 0:
            return (
                None,
                f"Target item not found and {unreadable_count} items "
                "could not be read due to errors",
            )
        return None, None

    candidates: list[bytes] = []
    for uid in raw_uids:
        try:
            candidate_bytes = client.fetch_rfc822(uid)
            if candidate_bytes and _verify_imap_rfc822(candidate_bytes, message):
                candidates.append(candidate_bytes)
        except (
            imaplib.IMAP4.abort,
            OSError,
            EOFError,
            TimeoutError,
        ) as exc:
            return (
                None,
                f"IMAP abort or connection failure fetching UID {uid}: {exc}",
            )
        except Exception:
            unreadable_count += 1
            continue

    if candidates:
        if len(candidates) == 1:
            return candidates[0], None

        if all(c == candidates[0] for c in candidates[1:]):
            return candidates[0], None

        return (
            None,
            "Ambiguous Message-ID: multiple differing messages found on IMAP server",
        )

    if unreadable_count > 0:
        return (
            None,
            f"Target item not found and {unreadable_count} items "
            "could not be read due to errors",
        )

    return None, None


def _load_default_imap_client(env_file: Path | None = None) -> Any | None:
    try:
        from .imap_native import NativeImapClient
        from .imap_runtime import load_env_file
        from .imap_sync import DEFAULT_ENV_FILE

        resolved_env = env_file or DEFAULT_ENV_FILE
        if resolved_env.exists():
            return NativeImapClient(load_env_file(resolved_env))
    except Exception:
        pass
    return None


def export_from_imap(
    message: Mapping[str, object],
    destination_path: Path,
    *,
    overwrite: bool = False,
    skip_existing: bool = False,
    imap_client: Any | None = None,
    env_file: Path | None = None,
) -> OriginalExportResult:
    subject = str(message.get("subject") or "")
    sender = _format_sender_string(message)
    sent_at = str(message.get("sent_at") or "")

    client = imap_client or _load_default_imap_client(env_file)
    if client is None:
        return OriginalExportResult(
            status="failed",
            dest_path=None,
            format="eml",
            source_type="imap",
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail="IMAP server connection could not be established.",
        )

    rfc822, err = _find_imap_rfc822(client, message)
    if err is not None:
        return OriginalExportResult(
            status="failed",
            dest_path=None,
            format="eml",
            source_type="imap",
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail=err,
        )
    if rfc822 is None:
        return OriginalExportResult(
            status="not_found",
            dest_path=None,
            format="eml",
            source_type="imap",
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail="Original message not found on IMAP server.",
        )

    def write_rfc822(tmp_path: Path) -> None:
        tmp_path.write_bytes(rfc822)

    return _write_original_file(
        destination_path,
        write_rfc822,
        overwrite=overwrite,
        skip_existing=skip_existing,
        format="eml",
        source_type="imap",
        subject=subject,
        sender=sender,
        sent_at=sent_at,
    )


def export_original_message(
    database_path: str | Path,
    identifier: int | str,
    destination_dir: Path,
    *,
    overwrite: bool = False,
    skip_existing: bool = False,
    outlook_reader: Any | None = None,
    imap_client: Any | None = None,
    env_file: Path | None = None,
    refuse_filename: Callable[[str], str | None] | None = None,
    message: Mapping[str, object] | None = None,
) -> OriginalExportResult:
    """Retrieve one message's original from source and export to destination_dir."""
    if message is None:
        connection = connect_database(database_path, create=False, read_only=True)
        try:
            row = None
            if isinstance(identifier, int) or (
                isinstance(identifier, str) and identifier.isdigit()
            ):
                stored_id = int(identifier)
                row = connection.execute(
                    "SELECT * FROM messages WHERE id = ?", [stored_id]
                ).fetchone()
            else:
                clean_id = normalize_message_id(str(identifier))
                rows = connection.execute(
                    "SELECT * FROM messages WHERE message_id = ?", [clean_id]
                ).fetchall()
                if not rows:
                    rows = connection.execute(
                        "SELECT * FROM messages WHERE message_id = ?", [f"<{clean_id}>"]
                    ).fetchall()
                if rows:
                    row = rows[0]

            if row is None:
                raise KeyError(f"No message found for identifier {identifier!r}")
            message = dict(row)
        finally:
            connection.close()

    source_type = classify_message_source(message)
    if source_type == "unsupported":
        subject = str(message.get("subject") or "")
        sender = _format_sender_string(message)
        sent_at = str(message.get("sent_at") or "")
        src_label = str(
            message.get("pst_source") or message.get("source_input_mode") or "unknown"
        )
        return OriginalExportResult(
            status="not_found",
            dest_path=None,
            format="msg",
            source_type="pst",
            subject=subject,
            sender=sender,
            sent_at=sent_at,
            detail=f"Source {src_label!r} does not support original export.",
        )

    fmt: Literal["msg", "eml"] = "msg" if source_type == "pst" else "eml"

    dest_dir = destination_dir.expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    filename = build_original_export_filename(message, dest_dir, fmt)
    if refuse_filename:
        refusal = refuse_filename(filename)
        if refusal is not None:
            return OriginalExportResult(
                status="failed",
                dest_path=None,
                format=fmt,
                source_type=source_type,
                subject=str(message.get("subject") or ""),
                sender=_format_sender_string(message),
                sent_at=str(message.get("sent_at") or ""),
                detail=refusal,
            )

    destination_path = dest_dir / filename

    if source_type == "pst":
        return export_from_pst(
            message,
            destination_path,
            overwrite=overwrite,
            skip_existing=skip_existing,
            outlook_reader=outlook_reader,
        )
    return export_from_imap(
        message,
        destination_path,
        overwrite=overwrite,
        skip_existing=skip_existing,
        imap_client=imap_client,
        env_file=env_file,
    )

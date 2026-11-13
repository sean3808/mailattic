"""Original export (匯出原信): retrieves a stored message's original from source.

Issue #53, D-20261105-1, GLOSSARY.md:
- PST source via Outlook COM -> `.msg`
- IMAP source via IMAP server -> `.eml`
- When not found -> returns subject, sender, sent_at without raising an error
- Ambiguous/failed location never exports a different message
- Read/write failures are reported as failures, not success.
"""

from __future__ import annotations

import imaplib
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from email.parser import BytesHeaderParser
from email.utils import parseaddr, parsedate_to_datetime
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
    PR_SENDER_SMTP_ADDRESS,
    _coerce_optional_text,
    _com_wall_clock_to_aware,
    _normalized_windows_path,
    _optional_property,
)
from .parser import _MAIL_POLICY
from .query import (
    connect_database,
    get_message,
    normalize_message_id,
    select_rows_by_message_id,
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


def _base_result(
    message: Mapping[str, object],
    fmt: Literal["msg", "eml"],
    source_type: Literal["pst", "imap"],
) -> OriginalExportResult:
    """Failed-by-default result carrying the message identity; derive outcomes
    from it with ``dataclasses.replace``."""
    return OriginalExportResult(
        status="failed",
        format=fmt,
        source_type=source_type,
        subject=str(message.get("subject") or ""),
        sender=_format_sender_string(message),
        sent_at=str(message.get("sent_at") or ""),
    )


def _nothing_found(unreadable_count: int) -> tuple[None, str | None]:
    """No verified match: an error when some items could not be looked at."""
    if unreadable_count > 0:
        return (
            None,
            f"Target item not found and {unreadable_count} items "
            "could not be read due to errors",
        )
    return None, None


def _single_match[T](
    candidates: Sequence[T], ambiguity_message: str
) -> tuple[T | None, str | None]:
    """The one verified candidate; more than one is ambiguous even if they look
    like copies (#53 ruling 5: Sean finds it by hand rather than the tool
    guessing which copy is the original)."""
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return candidates[0], None
    return None, f"{ambiguity_message} ({len(candidates)} matches)"


def _no_message_id_unresolved(
    where: str, unreadable_count: int
) -> tuple[None, str | None]:
    """A message without a Message-ID whose recorded location did not give it
    back: never searched for by subject/sender/time, never reported as
    not_found (#53 ruling 4). PST and IMAP share this so they report alike."""
    if unreadable_count:
        return _nothing_found(unreadable_count)
    return None, (
        f"This message has no Message-ID and its recorded {where} no longer "
        "points to it, so it is not searched for elsewhere; find it by hand "
        "using the subject, sender and date."
    )


def _read_item_message_id(accessor: Any) -> str:
    """The opened item's Message-ID, "" when it has none.

    Raises when the value cannot be read: only a string (real Outlook returns
    "" for an item without one, measured on 3 PSTs in #71) or MAPI_E_NOT_FOUND
    proves what the item holds; anything else is "could not check", never
    "verified as another message" (ruling 3).
    """
    if accessor is None:
        raise LookupError("item has no PropertyAccessor")
    try:
        value = accessor.GetProperty(PR_INTERNET_MESSAGE_ID)
    except Exception as exc:
        hresult = getattr(exc, "hresult", None)
        if hresult is None and len(getattr(exc, "args", ())) > 0:
            hresult = exc.args[0]
        if hresult == -2147221233:  # MAPI_E_NOT_FOUND (0x8004010F)
            return ""
        raise
    if not isinstance(value, str):
        raise TypeError(f"Message-ID read as {type(value).__name__}, not text")
    return value


def _verify_outlook_item(item: Any, message: Mapping[str, object]) -> bool:
    """Check whether *item* matches *message*, strictly preventing wrong-mail export.

    False means verified to be another message; a read that proves nothing raises.
    """
    db_msg_id = message.get("message_id")
    accessor = getattr(item, "PropertyAccessor", None)

    if db_msg_id:
        item_msg_id = _read_item_message_id(accessor)
        if not item_msg_id:
            return False
        return normalize_message_id(item_msg_id) == normalize_message_id(str(db_msg_id))

    # Without a Message-ID a subject alone cannot identify a message (a reused
    # EntryID or UID can point at another message with the same subject).
    # Same sender derivation as PST ingest (OutlookComReader), so an Exchange
    # sender compares by its SMTP address rather than never matching.
    sender_type = str(getattr(item, "SenderEmailType", None) or "").strip()
    if sender_type.casefold() == "smtp":
        sender = getattr(item, "SenderEmailAddress", None)
    else:
        sender = (
            _coerce_optional_text(_optional_property(accessor, PR_SENDER_SMTP_ADDRESS))
            if accessor
            else None
        )
    return _matches_without_message_id(
        message,
        subject=getattr(item, "Subject", None),
        sender=sender,
        sent_at=_com_wall_clock_to_aware(getattr(item, "SentOn", None)),
    )


_SENT_AT_TOLERANCE_SECONDS = 60


def _matches_without_message_id(
    message: Mapping[str, object],
    *,
    subject: object,
    sender: object,
    sent_at: datetime | None,
) -> bool:
    """Identity check for a stored message that has no Message-ID: subject,
    sender address (casefold) and sent time (within a minute) must all agree.
    A candidate missing a sender or a usable time never matches."""
    if str(message.get("subject") or "").strip() != str(subject or "").strip():
        return False
    db_sender = str(message.get("sender_email") or "").strip().casefold()
    item_sender = str(sender or "").strip().casefold()
    if not db_sender or db_sender != item_sender:
        return False
    db_sent_at = _parse_aware_datetime(message.get("sent_at"))
    if db_sent_at is None or sent_at is None or sent_at.tzinfo is None:
        return False
    return abs((db_sent_at - sent_at).total_seconds()) <= _SENT_AT_TOLERANCE_SECONDS


def _parse_aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").strip())
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else None


def _resolve_pst_store(
    namespace: Any, pst_source: str
) -> tuple[Any | None, str | None]:
    """The mounted store for *pst_source*, or (None, why it is a failure rather
    than "not mounted"): stores that could not be read, or several mounted PSTs
    sharing the recorded filename when the recorded path itself is gone."""
    expected_path = _normalized_windows_path(pst_source)
    expected_name = Path(pst_source).name.casefold()
    try:
        stores = getattr(namespace, "Stores", None)
        store_count = int(getattr(stores, "Count", 0)) if stores is not None else 0
    except Exception as exc:
        return None, f"Stores mounted in Outlook could not be read: {exc}"
    if stores is None:
        return None, None

    entries: list[tuple[Any, str, str]] = []  # (store, normalized path, name)
    unreadable = 0
    for i in range(1, store_count + 1):
        try:
            store = stores.Item(i)
            fp = getattr(store, "FilePath", None)
            if fp:
                entries.append(
                    (store, _normalized_windows_path(fp), Path(fp).name.casefold())
                )
        except Exception:
            unreadable += 1

    # Exact normalized path match first, then filename (PST path relocated).
    for store, path, _ in entries:
        if path == expected_path:
            return store, None
    same_name = [store for store, _, name in entries if name == expected_name]
    if len(same_name) == 1:
        return same_name[0], None
    if len(same_name) > 1:
        return None, (
            f"PST source {pst_source} is not mounted at its recorded path and "
            f"{len(same_name)} mounted PSTs share its filename; cannot tell which"
        )
    if unreadable:
        return None, (
            f"PST source {pst_source} not found and {unreadable} "
            f"store(s) mounted in Outlook could not be read"
        )
    return None, None


def _find_pst_folder_by_path(
    root_folder: Any, folder_path: str, tally: _ScanTally
) -> Any | None:
    """Navigate Outlook folder hierarchy matching *folder_path*; a subfolder
    that cannot be read is counted in *tally*."""
    parts = [p.strip() for p in re.split(r"[/\\]", folder_path) if p.strip()]
    if not parts:
        # PST ingest records a message in the store root as "/".
        return root_folder
    current = root_folder
    for index, part in enumerate(parts):
        child_folders = getattr(current, "Folders", None)
        if child_folders is None:
            return None
        count = int(getattr(child_folders, "Count", 0))
        found = None
        unreadable_before = tally.unreadable
        for i in range(1, count + 1):
            try:
                sub = child_folders.Item(i)
                if str(getattr(sub, "Name", "")).casefold() == part.casefold():
                    found = sub
                    break
            except Exception:
                tally.unreadable += 1
                continue
        if found is None:
            # Skip the root's display name only when no same-name child exists.
            if (
                index == 0
                and tally.unreadable == unreadable_before
                and part.casefold() == str(getattr(root_folder, "Name", "")).casefold()
            ):
                continue
            return None
        current = found
    return current


@dataclass
class _ScanTally:
    """Count of items/folders that could not be looked at during a PST search."""

    unreadable: int = 0


_TABLE_BATCH_ROWS = 512

# Table string columns are cut at 255 characters (one real row measured, #71).
# A value this long may be a cut-off copy of the target, so it proves nothing.
_TABLE_TRUNCATION_RISK = 250


def _usable_entry_id(entry_id: object) -> str | None:
    if isinstance(entry_id, str) and entry_id.strip():
        return entry_id
    return None


def _matching_entries(
    folder: Any, expected: str, tally: _ScanTally, missing: list[str]
) -> list[str | None]:
    """Read *folder*'s Message-IDs in batches without opening every item.

    Each row is decided once. It is set aside only on positive proof that it is
    another message: a plain, non-blank string, too short to be truncated, that
    normalises to something other than *expected*. A row that equals *expected*
    is returned as a hit (None when it has no usable EntryID, so the caller
    reports it as found but unconfirmed). Every other row -- None, blank,
    non-text, possibly truncated, or any shape not foreseen -- goes to *missing*
    to be opened, or is counted unreadable when it cannot be opened.

    The listing itself must also show that every item was seen: each row a
    2-tuple with its own EntryID, and as many rows as the folder holds items.
    A listing that fails this is counted unreadable. Beyond that, a row's values
    are trusted to belong to that row, as the per-item scan before #71 trusted
    Outlook's item listing.
    """
    hits: list[str | None] = []
    try:
        table = folder.GetTable()
        table.Columns.RemoveAll()
        table.Columns.Add(PR_INTERNET_MESSAGE_ID)
        table.Columns.Add("EntryID")
    except Exception:
        tally.unreadable += 1
        return hits
    seen = 0
    listed: set[str] = set()
    complete = False
    while True:
        try:
            if table.EndOfTable:
                complete = True
                break
            rows = list(table.GetArray(_TABLE_BATCH_ROWS))
            ended = table.EndOfTable
        except Exception:
            tally.unreadable += 1
            break
        if not rows and not ended:
            tally.unreadable += 1
            break
        for row in rows:
            seen += 1
            # Real tables give each row as a 2-tuple with a distinct EntryID
            # (all ~65k rows of 3 PSTs, #71); anything else means this listing
            # cannot show that every item was seen.
            if not isinstance(row, tuple) or len(row) != 2:
                tally.unreadable += 1
                continue
            msg_id, entry_id = row
            entry = _usable_entry_id(entry_id)
            if entry is not None:
                if entry in listed:
                    tally.unreadable += 1
                    continue
                listed.add(entry)
            if isinstance(msg_id, str) and len(msg_id) < _TABLE_TRUNCATION_RISK:
                normalized = normalize_message_id(msg_id)
                if normalized == expected:
                    hits.append(entry)
                    continue
                if normalized and entry is not None:
                    continue  # proven to be another message
            if entry is None:
                tally.unreadable += 1
            else:
                missing.append(entry)
    if complete:
        try:
            item_count = int(folder.Items.Count)
        except Exception:
            tally.unreadable += 1
        else:
            if item_count != seen:
                tally.unreadable += 1
    return hits


_PST_AMBIGUOUS = "Ambiguous: the message was found more than once in the PST"


class _VerificationError(Exception):
    """An item was opened but checking its identity raised (a read error),
    as opposed to the EntryID lookup itself failing."""


def _find_pst_item(
    namespace: Any,
    store: Any,
    message: Mapping[str, object],
) -> tuple[Any | None, str | None]:
    """Locate the target item in *store*, returning (item, error_or_ambiguity).

    (None, None) — i.e. not_found — is returned only after the whole store was
    searched with nothing unreadable; every other miss carries an error.
    """
    logical_locator = str(message.get("logical_locator") or "").strip()
    db_msg_id = message.get("message_id")
    folder_path = str(message.get("folder_path") or "").strip()
    tally = _ScanTally()

    try:
        store_id = getattr(store, "StoreID", None)
    except Exception:
        store_id = None  # same fallback as a store without a StoreID

    def open_verified(entry: str) -> Any | None:
        item = (
            namespace.GetItemFromID(entry, store_id)
            if store_id
            else namespace.GetItemFromID(entry)
        )
        if item is None:
            # Not opened is not "verified as another message" (ruling 3).
            raise LookupError(f"Outlook returned no item for EntryID {entry}")
        try:
            return item if _verify_outlook_item(item, message) else None
        except Exception as exc:
            raise _VerificationError from exc

    # 1. Try logical_locator (EntryID). Outlook raises the same way for an
    # EntryID that no longer exists (the normal case after a PST is
    # reorganised) as for one it cannot read, so a failed lookup is not
    # counted; the searches below count real read errors. An item that was
    # returned but cannot be verified is a real read error and is counted.
    if logical_locator:
        candidate = None
        try:
            candidate = open_verified(logical_locator)
        except _VerificationError:
            tally.unreadable += 1
        except Exception:
            pass
        if candidate is not None:
            return candidate, None

    if not db_msg_id:
        return _no_message_id_unresolved("Outlook location", tally.unreadable)

    expected = normalize_message_id(str(db_msg_id))
    missing: list[str] = []

    try:
        root_folder = store.GetRootFolder()
    except Exception as exc:
        return None, f"PST root folder access failed: {exc}"

    if root_folder is None:
        return None, "PST root folder is unavailable"

    unconfirmed = (None, "A matching table row was found but could not be confirmed.")

    def confirm(hits: list[str | None]) -> list[Any] | None:
        """Open every table hit; each must verify as the target.

        A hit without a usable EntryID, one that cannot be opened, or one that
        opens as a different message contradicts the table: None (ruling 1,
        unsure means fail), never a fall-back to a copy elsewhere (ruling 5).
        """
        found: list[Any] = []
        for entry in hits:
            if entry is None:
                return None
            try:
                item = open_verified(entry)
            except Exception:
                return None
            if item is None:
                return None
            found.append(item)
        return found

    def hidden(entries: list[str], already: int) -> list[Any]:
        """Open rows whose Message-ID the table could not give: one of them may
        be another copy, which the uniqueness check (ruling 5) must count.
        Stops once two matches exist in total, since that is already ambiguous."""
        found: list[Any] = []
        for entry in entries:
            if already + len(found) > 1:
                break
            try:
                item = open_verified(entry)
            except Exception:
                tally.unreadable += 1
                continue
            if item is not None:
                found.append(item)
        return found

    # 2. Try locating inside folder_path first
    target_folder = None
    if folder_path:
        try:
            target_folder = _find_pst_folder_by_path(root_folder, folder_path, tally)
        except Exception:
            tally.unreadable += 1

        if target_folder is not None:
            unreadable_before = tally.unreadable
            missing_before = len(missing)
            folder_hits = _matching_entries(target_folder, expected, tally, missing)
            # Only a table that could not be read in full blocks the export; an
            # item that cannot be opened below is counted, as before the table
            # read, without blocking a match found beside it.
            table_incomplete = tally.unreadable > unreadable_before
            # The recorded folder's unreadable-ID rows are settled here, so the
            # whole-store pass below does not open them a second time.
            folder_missing = missing[missing_before:]
            del missing[missing_before:]
            folder_found = confirm(folder_hits)
            if folder_found is None:
                return unconfirmed
            folder_found += hidden(folder_missing, len(folder_found))
            if folder_found:
                if table_incomplete:
                    return _nothing_found(tally.unreadable)
                return _single_match(folder_found, _PST_AMBIGUOUS)

    # 3. Fall back to scanning the whole store by Message-ID
    candidates: list[str | None] = []
    try:
        target_entry = getattr(target_folder, "EntryID", None)
    except Exception:
        target_entry = None

    def is_target(folder: Any) -> bool:
        if target_folder is None:
            return False
        if folder is target_folder:
            return True
        try:
            return target_entry is not None and folder.EntryID == target_entry
        except Exception:
            return False

    def scan_folder(folder: Any) -> None:
        # The folder_path pass already read this folder's table (and found no
        # match); re-reading would count its unreadable items twice.
        if not is_target(folder):
            candidates.extend(_matching_entries(folder, expected, tally, missing))
        try:
            child_folders = getattr(folder, "Folders", None)
        except Exception:
            child_folders = None
        if child_folders is None:
            # A missing Folders collection means the subtree
            # was not searched, so the scan is incomplete.
            tally.unreadable += 1
        else:
            try:
                count = int(getattr(child_folders, "Count", 0))
            except Exception:
                tally.unreadable += 1
                count = 0
            for i in range(1, count + 1):
                try:
                    child = child_folders.Item(i)
                except Exception:
                    tally.unreadable += 1
                    continue
                scan_folder(child)

    scan_folder(root_folder)

    found = confirm(candidates)
    if found is None:
        return unconfirmed
    found += hidden(missing, len(found))
    if found:
        return _single_match(found, _PST_AMBIGUOUS)

    return _nothing_found(tally.unreadable)


def _load_default_outlook_reader() -> Any | None:
    try:
        from .outlook_pst import OutlookComReader

        return OutlookComReader()
    except Exception:
        return None


def _write_original_file(
    destination_path: Path,
    write_fn: Callable[[Path], None],
    base: OriginalExportResult,
    *,
    overwrite: bool,
    skip_existing: bool,
) -> OriginalExportResult:
    """Atomic write via sibling temp file, handling overwrite and skip-existing.

    The destination directory must already exist (created at the entry point).
    """
    exists_result = replace(
        base,
        status="skipped-existing" if skip_existing else "failed",
        dest_path=destination_path,
        detail=None if skip_existing else f"Destination exists: {destination_path}",
    )
    if destination_path.exists() and (skip_existing or not overwrite):
        return exists_result

    tmp_path = destination_path.parent / f"{_EXPORT_TMP_PREFIX}{uuid.uuid4().hex}.tmp"
    try:
        write_fn(tmp_path)
        if overwrite:
            tmp_path.replace(destination_path)
        else:
            tmp_path.rename(destination_path)
    except FileExistsError:
        tmp_path.unlink(missing_ok=True)
        return exists_result
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        return replace(base, detail=f"Writing original file failed: {exc}")

    return replace(base, status="exported", dest_path=destination_path)


def export_from_pst(
    message: Mapping[str, object],
    destination_path: Path,
    *,
    overwrite: bool = False,
    skip_existing: bool = False,
    outlook_reader: Any | None = None,
) -> OriginalExportResult:
    base = _base_result(message, "msg", "pst")

    reader = outlook_reader or _load_default_outlook_reader()
    if reader is None:
        return replace(base, detail="Outlook is not running or unavailable.")

    # The Outlook loader already discriminates its failures (CODING_STANDARDS
    # A1) and raises RuntimeError only for "not running" / "pywin32 missing";
    # any other error is an adapter defect and propagates unrelabelled.
    # Plain attribute access: getattr() with a default would swallow an
    # AttributeError raised inside the property and carry on with the reader.
    try:
        namespace = reader.namespace
    except RuntimeError as exc:
        return replace(base, detail=f"Outlook COM is unavailable: {exc}")
    if namespace is None:
        return replace(base, detail="Outlook COM is unavailable: no namespace.")

    pst_source = str(message.get("pst_source") or "").strip()
    store, store_problem = _resolve_pst_store(namespace, pst_source)
    if store is None:
        if store_problem:
            return replace(base, detail=store_problem)
        # Not mounted is not "really not there" (#53 ruling 6).
        return replace(
            base,
            detail=(
                f"PST source not open in Outlook: {pst_source}. Open it in "
                "Outlook and retry."
            ),
        )

    item, err = _find_pst_item(namespace, store, message)
    if err is not None:
        return replace(base, detail=err)
    if item is None:
        return replace(
            base, status="not_found", detail="Original message not found in PST."
        )

    def write_item(tmp_path: Path) -> None:
        item.SaveAs(str(tmp_path), 3)  # olMSG = 3

    return _write_original_file(
        destination_path,
        write_item,
        base,
        overwrite=overwrite,
        skip_existing=skip_existing,
    )


def _verify_imap_rfc822(rfc822: bytes, message: Mapping[str, object]) -> bool:
    """Verify that fetched RFC822 bytes match the stored message.

    A parse error propagates: the caller counts it as unreadable, so it can
    never be read as "not this message" and end as not_found.
    """
    db_msg_id = message.get("message_id")
    # Ingest's own policy (field-tolerant, #288) decodes RFC 2047 words and
    # unfolds headers the same way the stored subject was decoded; only the
    # headers are parsed, never the MIME body.
    parsed = BytesHeaderParser(policy=_MAIL_POLICY).parsebytes(rfc822)
    raw_msg_id = parsed.get("Message-ID")
    if db_msg_id:
        if not raw_msg_id:
            return False
        return normalize_message_id(str(raw_msg_id)) == normalize_message_id(
            str(db_msg_id)
        )
    try:
        sent_at = parsedate_to_datetime(str(parsed.get("Date") or ""))
    except (TypeError, ValueError):
        sent_at = None
    return _matches_without_message_id(
        message,
        subject=str(parsed.get("Subject") or ""),
        sender=parseaddr(str(parsed.get("From") or ""))[1],
        sent_at=sent_at,
    )


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
        return _no_message_id_unresolved("server UID", unreadable_count)

    # HEADER search is a substring match, so the bare id also finds the
    # bracketed header value.
    try:
        raw_uids = client.search_uids_by_message_id(
            normalize_message_id(str(db_msg_id))
        )
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
        return _single_match(
            candidates,
            "Ambiguous: the message was found more than once on the IMAP server",
        )

    return _nothing_found(unreadable_count)


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
    base = _base_result(message, "eml", "imap")

    # A client created here is closed here; an injected one is the caller's.
    owns_client = imap_client is None
    client = imap_client or _load_default_imap_client(env_file)
    if client is None:
        return replace(base, detail="IMAP server connection could not be established.")

    try:
        rfc822, err = _find_imap_rfc822(client, message)
    finally:
        if owns_client:
            client.close()
    if err is not None:
        return replace(base, detail=err)
    if rfc822 is None:
        return replace(
            base,
            status="not_found",
            detail="Original message not found on IMAP server.",
        )

    def write_rfc822(tmp_path: Path) -> None:
        tmp_path.write_bytes(rfc822)

    return _write_original_file(
        destination_path,
        write_rfc822,
        base,
        overwrite=overwrite,
        skip_existing=skip_existing,
    )


def _load_stored_message(
    database_path: str | Path, identifier: int | str
) -> dict[str, object] | OriginalExportResult:
    """Look up the stored message by db id or Message-ID.

    A Message-ID that matches more than one stored row is refused (returned as
    a failed result) rather than resolved to an arbitrary one.
    """
    if isinstance(identifier, int) or identifier.isdigit():
        return get_message(database_path, int(identifier), read_only=True)

    clean_id = normalize_message_id(str(identifier))
    connection = connect_database(database_path, create=False, read_only=True)
    try:
        rows = select_rows_by_message_id(connection, clean_id, "id")
    finally:
        connection.close()
    if not rows:
        raise KeyError(f"No message found for identifier {identifier!r}")
    if len(rows) > 1:
        return OriginalExportResult(
            status="failed",
            detail=(
                f"Ambiguous Message-ID: {len(rows)} stored messages match "
                f"{identifier!r}; use the stored id instead."
            ),
        )
    return get_message(database_path, int(rows[0]["id"]), read_only=True)


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
        loaded = _load_stored_message(database_path, identifier)
        if isinstance(loaded, OriginalExportResult):
            return loaded
        message = loaded

    source_type = classify_message_source(message)
    if source_type == "unsupported":
        src_label = str(
            message.get("pst_source") or message.get("source_input_mode") or "unknown"
        )
        input_mode = str(message.get("source_input_mode") or "").strip().casefold()
        # #53 ruling 2: mail sent through bes reports not_found for now (its
        # server location is not recorded; tracked separately). Every other
        # unsupported source has no retrievable original, which is not the
        # same as "really not there" (ruling 6), so it is failed with the reason.
        if input_mode == "outbound" or src_label.casefold().startswith("outbound-"):
            return replace(
                _base_result(message, "msg", "pst"),
                status="not_found",
                detail=f"Source {src_label!r} does not support original export.",
            )
        return replace(
            _base_result(message, "msg", "pst"),
            detail=(
                f"Source {src_label!r} (e.g. a web-archive scrape) has no "
                "retrievable original; find it by hand using the subject, "
                "sender and date."
            ),
        )

    fmt: Literal["msg", "eml"] = "msg" if source_type == "pst" else "eml"

    dest_dir = destination_dir.expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    filename = build_original_export_filename(message, dest_dir, fmt)
    if refuse_filename:
        refusal = refuse_filename(filename)
        if refusal is not None:
            return replace(_base_result(message, fmt, source_type), detail=refusal)

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

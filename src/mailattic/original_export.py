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
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.parser import BytesHeaderParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal

from .export_files import (
    format_sent_date_token,
    land_exported_file,
    sanitize_windows_filename,
    truncate_export_filename,
)
from .mutf7 import encode_mutf7
from .outlook_pst import (
    PR_INTERNET_MESSAGE_ID,
    PR_SENDER_SMTP_ADDRESS,
    _coerce_optional_text,
    _com_wall_clock_to_aware,
    _normalized_windows_path,
    _optional_property,
    require_taipei_machine_timezone,
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
    sent_at_epoch: int | None = None
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
            "sent_at_epoch": self.sent_at_epoch,
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
    sent_date = format_sent_date_token(message.get("sent_at"))
    message_id = int(message.get("id") or 0)
    raw_subject = str(message.get("subject") or "").strip()
    if raw_subject:
        cleaned_subject = sanitize_windows_filename(raw_subject)
    else:
        cleaned_subject = "message"

    candidate_name = f"{cleaned_subject}.{fmt}"
    prefix = f"{sent_date}_msg{message_id}_"
    return truncate_export_filename(prefix, candidate_name, destination_dir)


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
    raw_epoch = message.get("sent_at_epoch")
    sent_at_epoch = (
        raw_epoch
        if isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool)
        else None
    )
    return OriginalExportResult(
        status="failed",
        format=fmt,
        source_type=source_type,
        subject=str(message.get("subject") or ""),
        sender=_format_sender_string(message),
        sent_at=str(message.get("sent_at") or ""),
        sent_at_epoch=sent_at_epoch,
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
    require_taipei_machine_timezone()
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
    candidate_sent_at = _parse_aware_datetime(sent_at)
    if db_sent_at is None or candidate_sent_at is None:
        return False
    return (
        abs((db_sent_at - candidate_sent_at).total_seconds())
        <= _SENT_AT_TOLERANCE_SECONDS
    )


def _parse_aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").strip())
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _resolve_pst_store(
    namespace: Any, pst_source: str
) -> tuple[Any | None, str | None]:
    """The mounted store for *pst_source*, or (None, why it is a failure rather
    than "not mounted"): stores that could not be read, or several mounted PSTs
    sharing the recorded filename when the recorded path itself is gone.

    Kept separate from ``outlook_pst.OutlookComReader.resolve_store`` and
    ``_optional_property`` (issue #64):
    (a) Ingest's ``resolve_store`` matches only exact full paths and raises on
        missing/ambiguous stores, whereas export must fall back to filename
        matching for relocated PSTs and count unreadable stores.
    (b) Ingest's ``_optional_property`` swallows all exceptions, whereas export
        must treat a Message-ID read failure as "cannot confirm" (#53 ruling 3)
        rather than "not found".
    Merging them would also touch the upstream of rebuild (behaviour contract #6).
    """
    expected_path = _normalized_windows_path(pst_source)
    expected_name = Path(pst_source).name.casefold()
    try:
        stores = getattr(namespace, "Stores", None)
        store_count = int(getattr(stores, "Count", 0)) if stores is not None else 0
    # Any store-list read failure must report failed, including adapter errors.
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
    that cannot be read is counted in *tally*.

    Residual risk (accepted, see #90 and D-20260908-1):
        If a child folder with the same name as *root_folder* existed at import
        time and was later renamed or deleted in Outlook, skipping the first
        segment restarts lookup from *root_folder* and can apply the "recorded
        folder holds exactly one -> prefer it" exception to a different folder
        (worst case: a duplicate in another folder is not flagged as ambiguous).
        Export still passes through ``_verify_outlook_item`` Message-ID
        verification, so a wrong message is never exported. This risk is
        accepted because dropping the exception on skip would forfeit the #71
        speedup and #53 ruling 5 on the normal path, and triggering it requires
        an ordinary Outlook folder rename/delete rather than a deliberate
        collision (see D-20260908-1 in:
    docs/decisions/D-20260908-1-guards-defend-confused-deputy-not-deliberate-collision.md
        ).
    """
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


class _ScanTally:
    """Bookkeeping for a PST search: unreadable items/folders and entries whose
    Message-ID was not available from the folder table."""

    def __init__(self) -> None:
        self.unreadable: int = 0
        self.unlisted_id_entries: list[str] = []

    def take_unlisted_id_entries(self) -> list[str]:
        """Return and clear entries queued for full-item inspection."""
        entries = self.unlisted_id_entries
        self.unlisted_id_entries = []
        return entries


_TABLE_BATCH_ROWS = 512

# Table string columns are cut at 255 characters (one real row measured, #71).
# A value this long may be a cut-off copy of the target, so it proves nothing.
_TABLE_TRUNCATION_RISK = 250


def _usable_entry_id(entry_id: object) -> str | None:
    if isinstance(entry_id, str) and entry_id.strip():
        return entry_id
    return None


def _matching_entries(
    folder: Any, expected: str, tally: _ScanTally
) -> list[str | None]:
    """Read *folder*'s Message-IDs in batches without opening every item.

    Each row is decided once. It is set aside only on positive proof that it is
    another message: a plain, non-blank string, too short to be truncated, that
    normalises to something other than *expected*. A row that equals *expected*
    is returned as a hit (None when it has no usable EntryID, so the caller
    reports it as found but unconfirmed). Every other row -- None, blank,
    non-text, possibly truncated, or any shape not foreseen -- goes into
    *tally.unlisted_id_entries* to be opened, or is counted unreadable when it
    cannot be opened.

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
                tally.unlisted_id_entries.append(entry)
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
_PST_UNCONFIRMED = (
    None,
    "A matching table row was found but could not be confirmed.",
)
_MAX_PST_FAILURE_REASON_CHARS = 200


class _VerificationError(Exception):
    """An item was opened but checking its identity raised (a read error),
    as opposed to the EntryID lookup itself failing."""


def _format_pst_failure_reason(exc: BaseException) -> str:
    """Format a PST read exception as ``Type: message``, unwrapped and capped."""
    cause = (
        exc.__cause__
        if isinstance(exc, _VerificationError) and exc.__cause__ is not None
        else exc
    )
    msg = " ".join(str(cause).split())
    reason = f"{type(cause).__name__}: {msg}" if msg else type(cause).__name__
    if len(reason) > _MAX_PST_FAILURE_REASON_CHARS:
        return reason[: _MAX_PST_FAILURE_REASON_CHARS - 3] + "..."
    return reason


def _append_failure_reason(detail: str, reason: str | None) -> str:
    if not reason:
        return detail
    return f"{detail} ({reason})"


def _is_same_pst_folder(
    folder: Any, target_folder: Any, target_entry_id: str | None
) -> bool:
    if target_folder is None:
        return False
    if folder is target_folder:
        return True
    try:
        return target_entry_id is not None and folder.EntryID == target_entry_id
    except Exception:
        return False


class _PstItemSearch:
    """Three-stage PST item lookup keeping Outlook call count and order intact."""

    def __init__(
        self,
        namespace: Any,
        store_id: str | None,
        message: Mapping[str, object],
        tally: _ScanTally,
    ) -> None:
        self._namespace = namespace
        self._store_id = store_id
        self._message = message
        self._tally = tally
        self._confirm_error: str | None = None
        self._unlisted_error: str | None = None

    def _unconfirmed_result(self) -> tuple[None, str]:
        return (
            None,
            _append_failure_reason(_PST_UNCONFIRMED[1], self._confirm_error),
        )

    def open_verified(self, entry: str) -> Any | None:
        item = (
            self._namespace.GetItemFromID(entry, self._store_id)
            if self._store_id
            else self._namespace.GetItemFromID(entry)
        )
        if item is None:
            # Not opened is not "verified as another message" (ruling 3).
            raise LookupError(f"Outlook returned no item for EntryID {entry}")
        try:
            return item if _verify_outlook_item(item, self._message) else None
        except Exception as exc:
            raise _VerificationError from exc

    def try_entry_id(self, logical_locator: str) -> Any | None:
        # Outlook raises the same way for an EntryID that no longer exists (the
        # normal case after a PST is reorganised) as for one it cannot read, so
        # a failed lookup is not counted; the searches below count real read
        # errors. An item that was returned but cannot be verified is a real
        # read error and is counted.
        try:
            return self.open_verified(logical_locator)
        except _VerificationError:
            self._tally.unreadable += 1
        except Exception:
            pass
        return None

    def confirm_table_hits(self, hits: list[str | None]) -> list[Any] | None:
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
                item = self.open_verified(entry)
            except Exception as exc:
                self._confirm_error = _format_pst_failure_reason(exc)
                return None
            if item is None:
                return None
            found.append(item)
        return found

    def open_entries_without_table_id(
        self, entries: list[str], already: int
    ) -> list[Any]:
        """Open rows whose Message-ID the table could not give: one of them may
        be another copy, which the uniqueness check (ruling 5) must count.
        Stops once two matches exist in total, since that is already ambiguous."""
        found: list[Any] = []
        for entry in entries:
            if already + len(found) > 1:
                break
            try:
                item = self.open_verified(entry)
            except Exception as exc:
                self._tally.unreadable += 1
                if self._unlisted_error is None:
                    self._unlisted_error = _format_pst_failure_reason(exc)
                continue
            if item is not None:
                found.append(item)
        return found

    def _resolve_candidates(
        self, hits: list[str | None], unlisted: list[str]
    ) -> list[Any] | None:
        """Confirm table hits, then inspect unlisted-ID rows for extra copies."""
        found = self.confirm_table_hits(hits)
        if found is None:
            return None
        found += self.open_entries_without_table_id(unlisted, len(found))
        return found

    def search_recorded_folder(
        self, root_folder: Any, folder_path: str, expected: str
    ) -> tuple[Any | None, tuple[Any | None, str | None] | None]:
        """Locate the item inside *folder_path*.

        Returns (target_folder, result_or_none).
        """
        if not folder_path:
            return None, None
        target_folder = None
        try:
            target_folder = _find_pst_folder_by_path(
                root_folder, folder_path, self._tally
            )
        except Exception:
            self._tally.unreadable += 1

        if target_folder is None:
            return None, None

        unreadable_before = self._tally.unreadable
        folder_hits = _matching_entries(target_folder, expected, self._tally)
        # Only a table that could not be read in full blocks the export; an
        # item that cannot be opened below is counted, as before the table
        # read, without blocking a match found beside it.
        table_incomplete = self._tally.unreadable > unreadable_before
        # The recorded folder's unreadable-ID rows are settled here, so the
        # whole-store pass below does not open them a second time.
        folder_unlisted = self._tally.take_unlisted_id_entries()

        folder_found = self._resolve_candidates(folder_hits, folder_unlisted)
        if folder_found is None:
            return target_folder, self._unconfirmed_result()
        if folder_found:
            if table_incomplete:
                return target_folder, (
                    None,
                    "A matching item was found, but the folder could not be "
                    "fully read, so its uniqueness could not be proven.",
                )
            return target_folder, _single_match(folder_found, _PST_AMBIGUOUS)
        return target_folder, None

    def _scan_folder_tree(
        self,
        folder: Any,
        expected: str,
        candidates: list[str | None],
        skip_folder: Any,
        skip_entry_id: str | None,
    ) -> None:
        # The folder_path pass already read this folder's table (and found no
        # match); re-reading would count its unreadable items twice.
        if not _is_same_pst_folder(folder, skip_folder, skip_entry_id):
            candidates.extend(_matching_entries(folder, expected, self._tally))
        try:
            child_folders = getattr(folder, "Folders", None)
        except Exception:
            child_folders = None
        if child_folders is None:
            # A missing Folders collection means the subtree
            # was not searched, so the scan is incomplete.
            self._tally.unreadable += 1
            return
        try:
            count = int(getattr(child_folders, "Count", 0))
        except Exception:
            self._tally.unreadable += 1
            count = 0
        for i in range(1, count + 1):
            try:
                child = child_folders.Item(i)
            except Exception:
                self._tally.unreadable += 1
                continue
            self._scan_folder_tree(
                child, expected, candidates, skip_folder, skip_entry_id
            )

    def search_store(
        self,
        root_folder: Any,
        expected: str,
        *,
        skip_folder: Any,
    ) -> tuple[Any | None, str | None]:
        """Fall back to scanning the whole PST store by Message-ID."""
        candidates: list[str | None] = []
        try:
            skip_entry_id = getattr(skip_folder, "EntryID", None)
        except Exception:
            skip_entry_id = None

        self._scan_folder_tree(
            root_folder, expected, candidates, skip_folder, skip_entry_id
        )

        found = self._resolve_candidates(
            candidates, self._tally.take_unlisted_id_entries()
        )
        if found is None:
            return self._unconfirmed_result()
        if found:
            return _single_match(found, _PST_AMBIGUOUS)
        item_none, detail = _nothing_found(self._tally.unreadable)
        if detail is not None and self._unlisted_error is not None:
            return item_none, _append_failure_reason(detail, self._unlisted_error)
        return item_none, detail


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

    search = _PstItemSearch(namespace, store_id, message, tally)

    # 1. Try logical_locator (EntryID) directly.
    if logical_locator:
        candidate = search.try_entry_id(logical_locator)
        if candidate is not None:
            return candidate, None

    if not db_msg_id:
        return _no_message_id_unresolved("Outlook location", tally.unreadable)

    expected = normalize_message_id(str(db_msg_id))

    try:
        root_folder = store.GetRootFolder()
    except Exception as exc:
        return None, f"PST root folder access failed: {exc}"

    if root_folder is None:
        return None, "PST root folder is unavailable"

    # 2. Try locating inside the recorded folder_path first.
    target_folder, recorded_result = search.search_recorded_folder(
        root_folder, folder_path, expected
    )
    if recorded_result is not None:
        return recorded_result

    # 3. Fall back to scanning the whole store by Message-ID.
    return search.search_store(root_folder, expected, skip_folder=target_folder)


def _load_default_outlook_reader() -> Any | None:
    try:
        from .outlook_pst import OutlookComReader

        return OutlookComReader()
    except Exception:
        return None


def _write_original_file(
    destination_path: Path,
    base: OriginalExportResult,
    *,
    overwrite: bool,
    skip_existing: bool,
    payload: bytes | None = None,
    save_to_tmp: Callable[[Path], None] | None = None,
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

    try:
        land_exported_file(
            destination_path,
            overwrite=overwrite,
            payload=payload,
            save_to_tmp=save_to_tmp,
        )
    except Exception as exc:
        # A rename race leaves the destination in place; any other failure,
        # including a temp-file collision, is a write failure.
        if isinstance(exc, FileExistsError) and destination_path.exists():
            return exists_result
        return replace(base, detail=f"Writing original file failed: {exc}")

    return replace(base, status="exported", dest_path=destination_path)


def _is_same_pst_store(a: Any, b: Any) -> bool:
    if a is b:
        return True
    try:
        id_a = getattr(a, "StoreID", None)
        id_b = getattr(b, "StoreID", None)
        if isinstance(id_a, str) and isinstance(id_b, str) and id_a and id_a == id_b:
            return True
    except Exception:
        pass
    try:
        fp_a = getattr(a, "FilePath", None)
        fp_b = getattr(b, "FilePath", None)
        if isinstance(fp_a, str) and isinstance(fp_b, str) and fp_a and fp_b:
            return _normalized_windows_path(fp_a) == _normalized_windows_path(fp_b)
    except Exception:
        pass
    return False


def _store_basename(store: Any) -> str:
    try:
        fp = getattr(store, "FilePath", None)
        if isinstance(fp, str) and fp.strip():
            return Path(fp).name
    except Exception:
        pass
    return "unknown.pst"


@dataclass(frozen=True)
class _CrossStoreMatch:
    item: Any | None = None
    error: str | None = None
    store_name: str | None = None


def _filter_pst_sources(sources: Sequence[object]) -> list[str]:
    """Keep non-empty source strings that classify as PST by `.pst` suffix."""
    filtered: list[str] = []
    for source in sources:
        raw = str(source or "").strip()
        if raw and classify_message_source({"pst_source": raw}) == "pst":
            filtered.append(raw)
    return filtered


def _list_known_pst_sources(database_path: str | Path) -> list[str]:
    """Return distinct PST source paths recorded in the DB."""
    connection = connect_database(database_path, create=False, read_only=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT pst_source FROM messages WHERE pst_source IS NOT NULL"
        ).fetchall()
    finally:
        connection.close()
    return _filter_pst_sources([row[0] for row in rows])


def _resolve_other_pst_stores(
    namespace: Any,
    recorded_store: Any,
    known_pst_sources: Sequence[str],
) -> tuple[list[Any], str | None]:
    """Map DB-known PST sources to mounted stores, excluding *recorded_store*."""
    other_stores: list[Any] = []
    for raw in _filter_pst_sources(known_pst_sources):
        candidate, store_problem = _resolve_pst_store(namespace, raw)
        if store_problem is not None:
            return [], store_problem
        if candidate is None:
            continue
        if _is_same_pst_store(candidate, recorded_store):
            continue
        if any(_is_same_pst_store(candidate, seen) for seen in other_stores):
            continue
        other_stores.append(candidate)
    return other_stores, None


def _find_pst_item_in_other_stores(
    namespace: Any,
    recorded_store: Any,
    message: Mapping[str, object],
    known_pst_sources: Sequence[str],
) -> _CrossStoreMatch:
    """Search other DB-known mounted PST stores by Message-ID only."""
    db_msg_id = message.get("message_id")
    if not db_msg_id:
        return _CrossStoreMatch()

    other_stores, store_problem = _resolve_other_pst_stores(
        namespace, recorded_store, known_pst_sources
    )
    if store_problem is not None:
        return _CrossStoreMatch(error=store_problem)

    expected = normalize_message_id(str(db_msg_id))
    matches: list[tuple[str, Any]] = []
    errors: list[str] = []

    for other_store in other_stores:
        store_name = _store_basename(other_store)
        tally = _ScanTally()
        try:
            store_id = getattr(other_store, "StoreID", None)
        except Exception:
            store_id = None

        try:
            root_folder = other_store.GetRootFolder()
        except Exception as exc:
            errors.append(f"PST root folder access failed ({store_name}): {exc}")
            continue
        if root_folder is None:
            errors.append(f"PST root folder is unavailable ({store_name})")
            continue

        search = _PstItemSearch(namespace, store_id, message, tally)
        item, err = search.search_store(root_folder, expected, skip_folder=None)
        if err is not None:
            errors.append(err)
            continue
        if item is not None:
            if tally.unreadable > 0:
                errors.append(
                    f"Target item found in {store_name}, but {tally.unreadable} "
                    "items/folders could not be read, so uniqueness could not "
                    "be proven."
                )
                continue
            matches.append((store_name, item))

    if len(matches) > 1:
        return _CrossStoreMatch(
            error=(
                f"Ambiguous: the message was found in {len(matches)} mounted PSTs "
                f"({len(matches)} matches)"
            )
        )
    if errors:
        return _CrossStoreMatch(error=errors[0])
    if len(matches) == 1:
        return _CrossStoreMatch(item=matches[0][1], store_name=matches[0][0])
    return _CrossStoreMatch()


def export_from_pst(
    message: Mapping[str, object],
    destination_path: Path,
    *,
    overwrite: bool = False,
    skip_existing: bool = False,
    outlook_reader: Any | None = None,
    known_pst_sources: Sequence[str] | Callable[[], Sequence[str]] | None = None,
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
    found_store_name: str | None = None
    if err is not None:
        return replace(base, detail=err)
    if item is None:
        if known_pst_sources is not None and message.get("message_id"):
            if callable(known_pst_sources):
                try:
                    resolved_sources = known_pst_sources()
                except (sqlite3.Error, OSError) as exc:
                    return replace(
                        base, detail=f"could not list other PST sources: {exc}"
                    )
            else:
                resolved_sources = known_pst_sources
            cross_match = _find_pst_item_in_other_stores(
                namespace, store, message, resolved_sources
            )
            if cross_match.error is not None:
                return replace(base, detail=cross_match.error)
            item = cross_match.item
            found_store_name = cross_match.store_name
        if item is None:
            return replace(
                base, status="not_found", detail="Original message not found in PST."
            )

    def write_item(tmp_path: Path) -> None:
        item.SaveAs(str(tmp_path), 3)  # olMSG = 3

    result = _write_original_file(
        destination_path,
        base,
        overwrite=overwrite,
        skip_existing=skip_existing,
        save_to_tmp=write_item,
    )
    if result.status == "exported" and found_store_name:
        return replace(
            result, detail=f"Found in another mounted PST: {found_store_name}"
        )
    return result


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


_IMAP_CONNECTION_ERRORS = (
    imaplib.IMAP4.abort,
    OSError,
    EOFError,
    TimeoutError,
)


@dataclass(frozen=True)
class _FolderSearchResult:
    candidates: list[bytes]
    unreadable_count: int
    error_message: str | None = None
    is_connection_abort: bool = False


def _search_folder_by_message_id(
    client: Any,
    message: Mapping[str, object],
    normalized_msg_id: str,
) -> _FolderSearchResult:
    """Search the currently selected IMAP folder by Message-ID and verify hits."""
    try:
        raw_uids = client.search_uids_by_message_id(normalized_msg_id)
    except _IMAP_CONNECTION_ERRORS as exc:
        return _FolderSearchResult(
            [], 0, f"IMAP SEARCH protocol error: {exc}", is_connection_abort=True
        )
    except imaplib.IMAP4.error as exc:
        return _FolderSearchResult([], 0, f"IMAP SEARCH protocol error: {exc}")
    except Exception as exc:
        return _FolderSearchResult([], 0, f"IMAP SEARCH failed: {exc}")

    candidates: list[bytes] = []
    unreadable_count = 0
    for uid in raw_uids:
        try:
            candidate_bytes = client.fetch_rfc822(uid)
            if not candidate_bytes:
                # A hit whose body could not be read leaves the scope unread.
                unreadable_count += 1
                continue
            if _verify_imap_rfc822(candidate_bytes, message):
                candidates.append(candidate_bytes)
        except _IMAP_CONNECTION_ERRORS as exc:
            return _FolderSearchResult(
                [],
                unreadable_count,
                f"IMAP abort or connection failure fetching UID {uid}: {exc}",
                is_connection_abort=True,
            )
        except Exception:
            unreadable_count += 1
            continue
    return _FolderSearchResult(candidates, unreadable_count)


def _same_imap_folder(a: str, b: str) -> bool:
    """Compare IMAP folder names exactly; INBOX alone is case-insensitive."""
    return a == b or (a.upper() == "INBOX" and b.upper() == "INBOX")


def _search_other_imap_folders(
    client: Any,
    message: Mapping[str, object],
    normalized_msg_id: str,
    recorded_wire_name: str,
    unreadable_count: int,
) -> tuple[bytes | None, str | None]:
    """Search all other IMAP folders by Message-ID when recorded folder missed."""
    try:
        folders = client.list_folders()
    except Exception as exc:
        return None, f"Failed to list IMAP folders: {exc}"

    skipped = list(getattr(client, "skipped_listings", []) or [])
    cross_matches: list[tuple[str, bytes]] = []
    folder_errors: list[str] = []

    for folder in folders:
        folder_wire = getattr(folder, "wire_name", None)
        if not folder_wire:
            continue
        if _same_imap_folder(folder_wire, recorded_wire_name):
            continue
        folder_display = getattr(folder, "display_name", None) or folder_wire
        try:
            client.select_folder(folder_wire)
        except _IMAP_CONNECTION_ERRORS as exc:
            return (
                None,
                "IMAP abort or connection failure selecting folder "
                f"{folder_display!r}: {exc}",
            )
        except Exception as exc:
            folder_errors.append(
                f"Failed to SELECT folder {folder_display!r} on IMAP server: {exc}"
            )
            continue

        folder_result = _search_folder_by_message_id(client, message, normalized_msg_id)
        unreadable_count += folder_result.unreadable_count
        if (
            folder_result.is_connection_abort
            and folder_result.error_message is not None
        ):
            return None, folder_result.error_message
        if folder_result.error_message is not None:
            folder_errors.append(
                f"Folder {folder_display!r}: {folder_result.error_message}"
            )
            continue
        for candidate_bytes in folder_result.candidates:
            cross_matches.append((folder_display, candidate_bytes))

    if len(cross_matches) > 1:
        matched_folders = ", ".join(
            repr(name) for name in dict.fromkeys(name for name, _ in cross_matches)
        )
        return (
            None,
            "Ambiguous: the message was found more than once on the IMAP server "
            f"across folders {matched_folders} ({len(cross_matches)} matches)",
        )
    if len(cross_matches) == 1:
        return cross_matches[0][1], None
    if skipped:
        return (
            None,
            "Target message not found in accessible folders and "
            f"{len(skipped)} IMAP folder listing(s) could not be decoded: "
            + "; ".join(skipped),
        )
    if folder_errors:
        return None, "; ".join(folder_errors)
    return _nothing_found(unreadable_count)


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
    db_msg_id = message.get("message_id")

    # 1. Try UID from logical_locator
    locator = str(message.get("logical_locator") or "")
    match = re.search(r"(?:^|[\W_])(?:uid-|U=)(\d+)", locator, re.IGNORECASE)
    if match:
        uid = int(match.group(1))
        rfc822: bytes | None = None
        try:
            rfc822 = client.fetch_rfc822(uid)
        except (*_IMAP_CONNECTION_ERRORS, imaplib.IMAP4.error) as exc:
            return None, f"IMAP error fetching UID {uid}: {exc}"
        except Exception:
            # A stale UID (moved/deleted) is resolved by Message-ID search below;
            # only count it unreadable when there is no Message-ID fallback.
            if not db_msg_id:
                unreadable_count += 1
        if rfc822:
            try:
                if _verify_imap_rfc822(rfc822, message):
                    return rfc822, None
            except Exception:
                unreadable_count += 1
        # An empty recorded-UID body is left to the Message-ID search below,
        # as before #67; it is not counted unreadable here.

    # 2. Try Message-ID search in the recorded folder
    if not db_msg_id:
        return _no_message_id_unresolved("server UID", unreadable_count)

    # HEADER search is a substring match, so the bare id also finds the
    # bracketed header value.
    normalized_msg_id = normalize_message_id(str(db_msg_id))
    recorded_result = _search_folder_by_message_id(client, message, normalized_msg_id)
    if recorded_result.error_message is not None:
        return None, recorded_result.error_message
    unreadable_count += recorded_result.unreadable_count

    if recorded_result.candidates:
        return _single_match(
            recorded_result.candidates,
            "Ambiguous: the message was found more than once on the IMAP server",
        )

    # 3. Recorded folder had no match: search all other folders by Message-ID
    return _search_other_imap_folders(
        client, message, normalized_msg_id, wire_name, unreadable_count
    )


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

    return _write_original_file(
        destination_path,
        base,
        overwrite=overwrite,
        skip_existing=skip_existing,
        payload=rfc822,
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
            known_pst_sources=lambda: _list_known_pst_sources(database_path),
        )
    return export_from_imap(
        message,
        destination_path,
        overwrite=overwrite,
        skip_existing=skip_existing,
        imap_client=imap_client,
        env_file=env_file,
    )

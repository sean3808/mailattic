from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.errors import HeaderParseError
from email.headerregistry import Address
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Protocol, TypedDict
from uuid import uuid4

from . import attachment_store
from .datetime_bounds import TAIPEI
from .models import (
    ExtractedMessage,
    ExtractionResult,
    IngestFailure,
    PstSource,
    _coerce_optional_text,
)

# MK_E_UNAVAILABLE (0x800401E3): nothing registered in the Running Object Table,
# which for a fixed ProgID means "that application is not running". Measured, not
# assumed: GetActiveObject("Outlook.Application") with Outlook closed returns this,
# while an unknown ProgID returns CO_E_CLASSSTRING (0x800401F3).
_MK_E_UNAVAILABLE = -2147221021
_OUTLOOK_MAIL_ITEM_CLASS = 43
_UNSUPPORTED_ITEM_CLASS_STAGE = "unsupported-item-class"
_MAPI_PROPTAG = "http://schemas.microsoft.com/mapi/proptag/"
PR_SMTP_ADDRESS = f"{_MAPI_PROPTAG}0x39FE001E"
PR_EMAIL_ADDRESS = f"{_MAPI_PROPTAG}0x3003001E"
PR_ADDRTYPE = f"{_MAPI_PROPTAG}0x3002001E"
PR_INTERNET_MESSAGE_ID = f"{_MAPI_PROPTAG}0x1035001E"
PR_IN_REPLY_TO_ID = f"{_MAPI_PROPTAG}0x1042001E"
PR_INTERNET_REFERENCES = f"{_MAPI_PROPTAG}0x1039001E"
PR_TRANSPORT_MESSAGE_HEADERS = f"{_MAPI_PROPTAG}0x007D001E"
PR_SENDER_SMTP_ADDRESS = f"{_MAPI_PROPTAG}0x5D01001E"
PR_ATTACH_DATA_BIN = f"{_MAPI_PROPTAG}0x37010102"
PR_ATTACH_MIME_TAG = f"{_MAPI_PROPTAG}0x370E001E"


def resolve_extraction_summary_root(database_path: str | Path) -> Path:
    """Resolve the recipient-extraction runtime root from the database path.

    Mirrors `attachment_store.resolve_store_root`: derived from the resolved
    database location, never from this module's own file location. A
    `__file__`-relative default is only correct when the package runs out of
    a checkout; under the released wheel it resolves inside site-packages.
    """
    return (
        Path(database_path).expanduser().resolve().parent
        / ".project-runtime"
        / "recipient-extraction"
    )


RecipientRole = Literal["to", "cc", "bcc"]
_RECIPIENT_ROLE_BY_TYPE: dict[int, RecipientRole] = {1: "to", 2: "cc", 3: "bcc"}
_UNRESOLVED_REASON_KEYS = (
    "missing_address",
    "missing_addrtype",
    "non_smtp_addrtype",
    "unknown_recipient_type",
)


class NormalizedRecipient(TypedDict):
    name: str | None
    email: str
    recipient_type: RecipientRole


@dataclass(frozen=True, slots=True)
class RawOutlookRecipient:
    display_name: str | None
    smtp_address: str | None
    email_address: str | None
    addrtype: str | None
    recipient_type: int


@dataclass(frozen=True, slots=True)
class RawOutlookMessage:
    raw_message: Mapping[str, object]
    folder_path: str
    logical_locator: str
    display_recipients: Mapping[RecipientRole, str]
    recipients: tuple[RawOutlookRecipient, ...]


@dataclass(frozen=True, slots=True)
class RawOutlookFailure:
    folder_path: str | None
    logical_locator: str | None
    error_stage: str
    error_type: str
    error_message: str


@dataclass(slots=True)
class PstExtractionAccount:
    source_path: str
    messages_seen: int = 0
    messages_emitted: int = 0
    parse_failures: int = 0
    skipped_non_mail_items: int = 0
    recipients_seen: int = 0
    recipients_emitted: int = 0
    unresolved_by_reason: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(_UNRESOLVED_REASON_KEYS, 0)
    )
    recipients_missing_from_display_count: int = 0
    elapsed_seconds: float = 0.0
    messages_per_second: float = 0.0
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
    completed: bool = False

    @property
    def message_reconciliation_difference(self) -> int:
        return (
            self.messages_seen
            - self.messages_emitted
            - self.parse_failures
            - self.skipped_non_mail_items
        )

    @property
    def recipient_reconciliation_difference(self) -> int:
        return (
            self.recipients_seen
            - self.recipients_emitted
            - sum(self.unresolved_by_reason.values())
        )

    def finish(self, *, elapsed_seconds: float, completed: bool) -> None:
        self.elapsed_seconds = max(elapsed_seconds, 0.0)
        self.messages_per_second = (
            self.messages_seen / self.elapsed_seconds
            if self.elapsed_seconds > 0
            else 0.0
        )
        self.completed_at = datetime.now(UTC).isoformat()
        self.completed = completed

    def validate(self) -> None:
        if set(self.unresolved_by_reason) != set(_UNRESOLVED_REASON_KEYS):
            raise RuntimeError(
                "PST extraction account has invalid unresolved reason keys: "
                f"{sorted(self.unresolved_by_reason)}"
            )
        if not self.completed:
            raise RuntimeError("PST extraction account did not complete enumeration")
        if self.message_reconciliation_difference != 0:
            raise RuntimeError(
                "PST extraction message reconciliation difference: "
                f"{self.message_reconciliation_difference}"
            )
        if self.recipient_reconciliation_difference != 0:
            raise RuntimeError(
                "PST extraction recipient reconciliation difference: "
                f"{self.recipient_reconciliation_difference}"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "messages_seen": self.messages_seen,
            "messages_emitted": self.messages_emitted,
            "parse_failures": self.parse_failures,
            "skipped_non_mail_items": self.skipped_non_mail_items,
            "recipients_seen": self.recipients_seen,
            "recipients_emitted": self.recipients_emitted,
            "unresolved_by_reason": dict(self.unresolved_by_reason),
            "recipients_missing_from_display_count": (
                self.recipients_missing_from_display_count
            ),
            "message_reconciliation_difference": (
                self.message_reconciliation_difference
            ),
            "recipient_reconciliation_difference": (
                self.recipient_reconciliation_difference
            ),
            "elapsed_seconds": self.elapsed_seconds,
            "messages_per_second": self.messages_per_second,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "completed": self.completed,
        }


class OutlookReader(Protocol):
    def iter_messages(
        self, source: PstSource
    ) -> Iterable[RawOutlookMessage | RawOutlookFailure]: ...


class OutlookComReader:
    def __init__(
        self,
        *,
        namespace: Any | None = None,
        attachment_root: Path | None = None,
    ) -> None:
        self._namespace = namespace
        self._attachment_root = attachment_root

    @property
    def namespace(self) -> Any:
        if self._namespace is None:
            self._namespace = _load_outlook_namespace()
        return self._namespace

    def resolve_store(self, source: PstSource) -> Any:
        expected_path = _normalized_windows_path(source.path)
        stores = self.namespace.Stores
        matches = [
            store
            for index in range(1, int(stores.Count) + 1)
            if (store := stores.Item(index)).FilePath
            and _normalized_windows_path(store.FilePath) == expected_path
        ]
        if not matches:
            raise RuntimeError(
                "PST source must be already mounted in the active Outlook profile: "
                f"{source.path}"
            )
        if len(matches) > 1:
            raise RuntimeError(
                f"Multiple Outlook stores match PST source: {source.path}"
            )
        return matches[0]

    def iter_messages(
        self, source: PstSource
    ) -> Iterable[RawOutlookMessage | RawOutlookFailure]:
        root_folder = self.resolve_store(source).GetRootFolder()
        yield from self._iter_folder(
            root_folder, parent_path="", include_folder_name=False
        )

    def _iter_folder(
        self, folder: Any, *, parent_path: str, include_folder_name: bool = True
    ) -> Iterable[RawOutlookMessage | RawOutlookFailure]:
        folder_name = str(_safe_attr(folder, "Name") or "").strip()
        folder_path = "/".join(
            part
            for part in (
                parent_path,
                folder_name if include_folder_name else "",
            )
            if part
        )
        effective_path = folder_path or "/"

        items = folder.Items
        for index in range(1, int(items.Count) + 1):
            yield self._read_item(items.Item(index), folder_path=effective_path)

        child_folders = folder.Folders
        for index in range(1, int(child_folders.Count) + 1):
            yield from self._iter_folder(
                child_folders.Item(index), parent_path=folder_path
            )

    def _read_item(
        self, item: Any, *, folder_path: str
    ) -> RawOutlookMessage | RawOutlookFailure:
        logical_locator = _coerce_optional_text(_safe_attr(item, "EntryID"))
        try:
            item_class = item.Class
        except Exception as exc:
            # A Class read failure is not "unsupported item": it must stay
            # distinguishable from a real calendar/contact/NDR skip and be
            # recorded as a parse failure (balanced in the extraction ledger).
            return RawOutlookFailure(
                folder_path=folder_path,
                logical_locator=logical_locator,
                error_stage="read-item-class",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        if item_class != _OUTLOOK_MAIL_ITEM_CLASS:
            return RawOutlookFailure(
                folder_path=folder_path,
                logical_locator=logical_locator,
                error_stage=_UNSUPPORTED_ITEM_CLASS_STAGE,
                error_type="UnsupportedOutlookItemClass",
                error_message=f"Unsupported Outlook item class: {item_class!r}",
            )

        try:
            recipients = self._read_recipients(item.Recipients)
        except Exception as exc:
            return RawOutlookFailure(
                folder_path=folder_path,
                logical_locator=logical_locator,
                error_stage="read-recipient-table",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        # These are core message evidence, not optional metadata: a COM
        # failure reading any of them must fail the message, not silently
        # degrade to None while the message still persists and counts as
        # emitted (adjudication: "Bare except blocks report success on
        # corrupted rows").
        try:
            subject = item.Subject
            sender_name = _coerce_optional_text(item.SenderName)
            sender_type = _coerce_optional_text(item.SenderEmailType)
            sender_email_via_om = (
                _coerce_optional_text(item.SenderEmailAddress)
                if sender_type and sender_type.casefold() == "smtp"
                else None
            )
            sent_at = _com_wall_clock_to_aware(item.SentOn)
            received_at = _com_wall_clock_to_aware(item.ReceivedTime)
            body_text = item.Body or ""
        except Exception as exc:
            return RawOutlookFailure(
                folder_path=folder_path,
                logical_locator=logical_locator,
                error_stage="read-message-properties",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        accessor = _safe_attr(item, "PropertyAccessor")
        attachment_collection_error: str | None = None
        try:
            collection = item.Attachments
            if collection is not None:
                int(collection.Count)
        except Exception as exc:
            # Body is readable, so keep the message (partial-failure policy)
            # but leave failure evidence instead of faking zero attachments.
            collection = None
            attachment_collection_error = f"{type(exc).__name__}: {exc}"
        attachments = self._read_attachments(collection)
        sender_email = (
            sender_email_via_om
            if sender_type and sender_type.casefold() == "smtp"
            else _coerce_optional_text(
                _optional_property(accessor, PR_SENDER_SMTP_ADDRESS)
            )
        )
        raw_message: dict[str, object] = {
            "subject": subject,
            "sender": {
                "name": sender_name,
                "email": sender_email,
            },
            "sent_at": sent_at,
            "received_at": received_at,
            "body_text": body_text,
            "attachments": attachments,
            "attachment_names": [
                str(attachment["attachment_name"]) for attachment in attachments
            ],
            "attachment_collection_error": attachment_collection_error,
            "message_id": _coerce_optional_text(
                _optional_property(accessor, PR_INTERNET_MESSAGE_ID)
            ),
            "in_reply_to": _coerce_optional_text(
                _optional_property(accessor, PR_IN_REPLY_TO_ID)
            ),
            "references": _coerce_optional_text(
                _optional_property(accessor, PR_INTERNET_REFERENCES)
            ),
            "raw_headers": _coerce_optional_text(
                _optional_property(accessor, PR_TRANSPORT_MESSAGE_HEADERS)
            ),
        }
        return RawOutlookMessage(
            raw_message=raw_message,
            folder_path=folder_path,
            logical_locator=logical_locator or "",
            display_recipients={
                "to": str(_safe_attr(item, "To") or ""),
                "cc": str(_safe_attr(item, "CC") or ""),
                "bcc": str(_safe_attr(item, "BCC") or ""),
            },
            recipients=recipients,
        )

    def _read_recipients(self, recipients: Any) -> tuple[RawOutlookRecipient, ...]:
        raw_recipients: list[RawOutlookRecipient] = []
        for index in range(1, int(recipients.Count) + 1):
            recipient = recipients.Item(index)
            accessor = recipient.PropertyAccessor
            raw_recipients.append(
                RawOutlookRecipient(
                    display_name=_coerce_optional_text(_safe_attr(recipient, "Name")),
                    smtp_address=_coerce_optional_text(
                        _optional_property(accessor, PR_SMTP_ADDRESS)
                    ),
                    email_address=_coerce_optional_text(
                        _optional_property(accessor, PR_EMAIL_ADDRESS)
                    ),
                    addrtype=_coerce_optional_text(
                        _optional_property(accessor, PR_ADDRTYPE)
                    ),
                    recipient_type=int(_safe_attr(recipient, "Type") or 0),
                )
            )
        return tuple(raw_recipients)

    def _read_attachments(self, attachments: Any) -> list[dict[str, object]]:
        if attachments is None:
            return []
        extracted: list[dict[str, object]] = []
        for index in range(1, int(attachments.Count) + 1):
            source_attachment = attachments.Item(index)
            attachment: dict[str, object] = {
                "attachment_name": str(_safe_attr(source_attachment, "FileName") or ""),
                "content_hash": None,
                "size_bytes": None,
                "mime_type": None,
                "storage_path": None,
            }
            if self._attachment_root is not None:
                accessor = source_attachment.PropertyAccessor
                try:
                    payload = _optional_property(accessor, PR_ATTACH_DATA_BIN)
                    if not isinstance(payload, bytes | bytearray):
                        raise TypeError("Attachment binary property is unavailable")
                    # Kept before the CAS write so a failed write still leaves the
                    # size that shared duplicate repair matches against.
                    attachment["_expected_size"] = len(payload)
                    mime_type = _coerce_optional_text(
                        _optional_property(accessor, PR_ATTACH_MIME_TAG)
                    )
                    record = attachment_store.write_attachment(
                        bytes(payload),
                        str(attachment["attachment_name"]),
                        mime_type,
                        self._attachment_root,
                    )
                    attachment.update(
                        {
                            "content_hash": record.content_hash,
                            "size_bytes": record.size_bytes,
                            "mime_type": record.mime_type,
                            "storage_path": record.storage_path,
                        }
                    )
                except OSError:
                    attachment["_store_error"] = True
                except Exception:
                    attachment["_decode_error"] = True
            extracted.append(attachment)
        return extracted


@dataclass(frozen=True, slots=True)
class RecipientNormalization:
    recipients: tuple[NormalizedRecipient, ...]
    missing_address: int = 0
    missing_addrtype: int = 0
    non_smtp_addrtype: int = 0
    unknown_recipient_type: int = 0

    @property
    def unresolved_by_reason(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in _UNRESOLVED_REASON_KEYS}


def normalize_outlook_recipients(
    raw_recipients: list[RawOutlookRecipient] | tuple[RawOutlookRecipient, ...],
) -> RecipientNormalization:
    recipients: list[NormalizedRecipient] = []
    unresolved = dict.fromkeys(_UNRESOLVED_REASON_KEYS, 0)

    def record_unresolved(reason: str) -> None:
        unresolved[reason] += 1

    for raw in raw_recipients:
        display_name = _normalize_optional_display_name(raw.display_name)
        role = _RECIPIENT_ROLE_BY_TYPE.get(raw.recipient_type)
        if role is None:
            record_unresolved("unknown_recipient_type")
            continue

        addrtype = (raw.addrtype or "").strip()
        if not addrtype:
            record_unresolved("missing_addrtype")
            continue
        if addrtype.casefold() != "smtp":
            record_unresolved("non_smtp_addrtype")
            continue

        address = _first_nonblank(raw.smtp_address, raw.email_address)
        if not address:
            record_unresolved("missing_address")
            continue

        try:
            atomic_address = Address(addr_spec=address)
        except (HeaderParseError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Outlook recipient SMTP address is not atomic: {address!r}"
            ) from exc
        if not atomic_address.username or not atomic_address.domain:
            raise ValueError(
                f"Outlook recipient SMTP address is not atomic: {address!r}"
            )

        recipients.append(
            {
                "name": display_name,
                "email": address,
                "recipient_type": role,
            }
        )

    return RecipientNormalization(
        recipients=tuple(recipients),
        **unresolved,
    )


def _normalize_optional_display_name(value: str | None) -> str | None:
    normalized = unicodedata.normalize("NFC", value or "").strip()
    return normalized or None


def _first_nonblank(*values: str | None) -> str:
    for value in values:
        normalized = (value or "").strip()
        if normalized:
            return normalized
    return ""


def _normalized_windows_path(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve()).casefold()


def _com_wall_clock_to_aware(value: Any) -> datetime | None:
    """Relabel a COM timestamp with the timezone its numbers are really in.

    `pywintypes` stamps every COM date with UTC, but Outlook hands back the
    reading machine's local wall clock, so the label is wrong by the local
    offset -- `SentOn` for a message truly sent at 06:42:33Z arrives as
    `2025-03-26 14:42:33+00:00`, which is the Taipei reading with a UTC tag.
    Attaching a zone only when `tzinfo is None` would be a no-op here: the
    value arrives aware, and wrong.

    Rebuilding from the components rather than calling `value.replace()` is
    deliberate. `.replace()` preserves the `pywintypes.datetime` subclass,
    which survives `models._coerce_datetime` and only fails much later, at
    `sqlite3` bind time ("type 'pywintypes.datetime' is not supported").
    Today every write path calls `.isoformat()` first so nothing binds the
    raw object, but that is a property of the current callers, not of the
    value -- so the foreign type is normalized here, at the boundary where
    it enters.
    """
    if value is None:
        return None
    return datetime(
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        tzinfo=TAIPEI,
    )


def _safe_attr(value: Any, name: str) -> Any | None:
    try:
        return getattr(value, name)
    except Exception:
        return None


def _optional_property(accessor: Any, property_name: str) -> Any | None:
    if accessor is None:
        return None
    try:
        return accessor.GetProperty(property_name)
    except Exception:
        return None


def _load_outlook_namespace() -> Any:
    try:
        import pywintypes
        import win32com.client
    except ImportError as exc:
        raise RuntimeError(
            "Outlook PST ingestion requires Windows, Outlook, and pywin32."
        ) from exc
    # GetActiveObject, never Dispatch (#181): Dispatch would launch Outlook under
    # a profile that need not carry the caller's PST.
    try:
        application = win32com.client.GetActiveObject("Outlook.Application")
    except pywintypes.com_error as exc:
        if exc.hresult != _MK_E_UNAVAILABLE:
            # A bad ProgID, a broken COM apartment or a permissions failure is a
            # different problem; relabelling it "not running" sends the operator
            # to start an Outlook that is already running, or not installed.
            raise
        raise RuntimeError(
            "Outlook is not running. Start Outlook and mount the PST first; "
            "this reader never launches Outlook itself."
        ) from exc
    return application.GetNamespace("MAPI")


def write_pst_extraction_summary(
    accounts: list[PstExtractionAccount],
    *,
    database_path: str | Path,
    summary_root: Path | None = None,
    run_id: str | None = None,
) -> tuple[Path, bool]:
    effective_run_id = run_id or _new_run_id()
    destination_root = summary_root or resolve_extraction_summary_root(database_path)
    run_root = destination_root / effective_run_id
    # exist_ok=True: a reused run_id must not lose an already-committed
    # database import behind a spurious FileExistsError on the second write.
    run_root.mkdir(parents=True, exist_ok=True)
    summary_path = run_root / "summary.json"
    temporary_path = run_root / "summary.json.tmp"

    complete = True
    sources_payload: list[dict[str, object]] = []
    for account in accounts:
        entry = account.as_dict()
        try:
            account.validate()
        except RuntimeError as exc:
            complete = False
            entry["reconciliation_error"] = str(exc)
        sources_payload.append(entry)

    totals = _aggregate_accounts(accounts)
    payload = {
        "schema_version": 1,
        "run_id": effective_run_id,
        "started_at": min((account.started_at for account in accounts), default=None),
        "completed_at": max(
            (
                account.completed_at
                for account in accounts
                if account.completed_at is not None
            ),
            default=None,
        ),
        "complete": complete,
        "totals": totals,
        "sources": sources_payload,
    }
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(summary_path)
    return summary_path, complete


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _aggregate_accounts(accounts: list[PstExtractionAccount]) -> dict[str, object]:
    elapsed_seconds = sum(account.elapsed_seconds for account in accounts)
    messages_seen = sum(account.messages_seen for account in accounts)
    unresolved = {
        reason: sum(account.unresolved_by_reason.get(reason, 0) for account in accounts)
        for reason in _UNRESOLVED_REASON_KEYS
    }
    totals: dict[str, object] = {
        "source_count": len(accounts),
        "messages_seen": messages_seen,
        "messages_emitted": sum(account.messages_emitted for account in accounts),
        "parse_failures": sum(account.parse_failures for account in accounts),
        "recipients_seen": sum(account.recipients_seen for account in accounts),
        "recipients_emitted": sum(account.recipients_emitted for account in accounts),
        "unresolved_by_reason": unresolved,
        "recipients_missing_from_display_count": sum(
            account.recipients_missing_from_display_count for account in accounts
        ),
        "message_reconciliation_difference": sum(
            account.message_reconciliation_difference for account in accounts
        ),
        "recipient_reconciliation_difference": sum(
            account.recipient_reconciliation_difference for account in accounts
        ),
        "elapsed_seconds": elapsed_seconds,
        "messages_per_second": (
            messages_seen / elapsed_seconds if elapsed_seconds > 0 else 0.0
        ),
    }
    return totals


class OutlookPstParser:
    """Issue #151 D3: same parser shape as `ReadpstParser`/`MaildirParser` --
    `iter_extraction_results(source) -> Iterable[ExtractionResult]`. Kept
    exact for protocol conformance; `self.summary` is set as a side effect
    for callers that read it after the generator drains (legacy pattern,
    matches the pre-review shape).

    `extract()` is a second, honestly-named entry point for the caller that
    wants the account back directly instead of reading `self.summary` as an
    out-parameter (ingest.py's `import_pst_sources`). It shares the same
    lazy generator, so results still stream -- nothing here materializes an
    entire PST's messages into a list.
    """

    def __init__(self, *, reader: OutlookReader) -> None:
        self._reader = reader
        self.summary: PstExtractionAccount | None = None

    def iter_extraction_results(self, source: PstSource) -> Iterable[ExtractionResult]:
        account = PstExtractionAccount(source_path=source.source_path)
        self.summary = account
        yield from self._generate(source, account)

    def extract(
        self, source: PstSource
    ) -> tuple[PstExtractionAccount, Iterable[ExtractionResult]]:
        account = PstExtractionAccount(source_path=source.source_path)
        return account, self._generate(source, account)

    def _generate(
        self, source: PstSource, account: PstExtractionAccount
    ) -> Iterable[ExtractionResult]:
        started = perf_counter()
        completed = False
        try:
            for message in self._reader.iter_messages(source):
                account.messages_seen += 1
                if (
                    isinstance(message, RawOutlookFailure)
                    and message.error_stage == _UNSUPPORTED_ITEM_CLASS_STAGE
                ):
                    # Calendar, contact, and NDR items are real store content,
                    # not extraction failures; account for them separately so
                    # a whole-store import does not fail reconciliation on
                    # every non-mail item it encounters.
                    account.skipped_non_mail_items += 1
                    continue
                if isinstance(message, RawOutlookFailure):
                    account.parse_failures += 1
                    yield ExtractionResult(
                        failure=IngestFailure.from_source(
                            source,
                            folder_path=message.folder_path,
                            logical_locator=message.logical_locator,
                            error_stage=message.error_stage,
                            error_type=message.error_type,
                            error_message=message.error_message,
                        )
                    )
                    continue

                try:
                    normalized = normalize_outlook_recipients(message.recipients)
                except ValueError as exc:
                    account.parse_failures += 1
                    yield ExtractionResult(
                        failure=IngestFailure.from_source(
                            source,
                            folder_path=message.folder_path,
                            logical_locator=message.logical_locator,
                            error_stage="normalize-recipients",
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
                    continue
                account.messages_emitted += 1
                account.recipients_seen += len(message.recipients)
                account.recipients_emitted += len(normalized.recipients)
                for reason, count in normalized.unresolved_by_reason.items():
                    account.unresolved_by_reason[reason] += count
                account.recipients_missing_from_display_count += (
                    _missing_from_display_count(message)
                )

                raw_message = dict(message.raw_message)
                recipients_by_role: dict[RecipientRole, list[dict[str, str | None]]] = {
                    "to": [],
                    "cc": [],
                    "bcc": [],
                }
                for recipient in normalized.recipients:
                    role = recipient["recipient_type"]
                    recipients_by_role[role].append(
                        {"name": recipient["name"], "email": recipient["email"]}
                    )
                raw_message["to"] = recipients_by_role["to"]
                raw_message["cc"] = recipients_by_role["cc"]
                raw_message["bcc"] = recipients_by_role["bcc"]

                yield ExtractionResult(
                    message=ExtractedMessage(
                        raw_message=raw_message,
                        folder_path=message.folder_path,
                        logical_locator=message.logical_locator,
                    )
                )
            completed = True
        finally:
            account.finish(
                elapsed_seconds=perf_counter() - started,
                completed=completed,
            )
        if completed:
            account.validate()


def _missing_from_display_count(message: RawOutlookMessage) -> int:
    recipient_counts: dict[RecipientRole, int] = {"to": 0, "cc": 0, "bcc": 0}
    for recipient in message.recipients:
        role = _RECIPIENT_ROLE_BY_TYPE.get(recipient.recipient_type)
        if role is not None:
            recipient_counts[role] += 1

    return sum(
        max(recipient_counts[role] - _display_entry_count(display_value), 0)
        for role, display_value in message.display_recipients.items()
    )


def _display_entry_count(value: str) -> int:
    return sum(1 for fragment in value.split(";") if fragment.strip())

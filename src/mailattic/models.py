from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol


def build_participant_search_text(display_name: str | None, email: str | None) -> str:
    """Canonical FTS sender/recipient search text from a name + email pair.

    Single source of truth shared by ``MailboxParticipant.search_text`` (index
    write path) and the ``reindex_fts`` rebuild path, so the two never drift.
    """
    parts = [value for value in (display_name, email) if value]
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class MailboxParticipant:
    display_name: str | None
    email: str | None

    @property
    def search_text(self) -> str:
        return build_participant_search_text(self.display_name, self.email)


class IngestionSource(Protocol):
    @property
    def source_path(self) -> str: ...

    @property
    def requested_from_path(self) -> str: ...

    @property
    def input_mode(self) -> str: ...


@dataclass(frozen=True, slots=True)
class PstSource:
    path: Path
    input_mode: Literal["directory", "file"]
    requested_from: Path

    @property
    def source_path(self) -> str:
        return str(self.path)

    @property
    def requested_from_path(self) -> str:
        return str(self.requested_from)


@dataclass(frozen=True, slots=True)
class MaildirSource:
    path: Path
    account_email: str
    account_name: str
    folder_name: str
    requested_from: Path

    @property
    def source_path(self) -> str:
        return f"imap:{self.account_email}:{self.folder_name}"

    @property
    def input_mode(self) -> str:
        return "maildir"

    @property
    def requested_from_path(self) -> str:
        return str(self.requested_from)


@dataclass(frozen=True, slots=True)
class NormalizedAttachment:
    attachment_name: str
    content_hash: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    storage_path: str | None = None


@dataclass(frozen=True)
class NormalizedMessage:
    subject: str
    sender: MailboxParticipant
    to_recipients: tuple[MailboxParticipant, ...]
    cc_recipients: tuple[MailboxParticipant, ...]
    bcc_recipients: tuple[MailboxParticipant, ...]
    sent_at: datetime | None
    received_at: datetime | None
    body_text: str
    attachments: tuple[NormalizedAttachment, ...]
    message_id: str | None
    in_reply_to: str | None
    references: str | None
    pst_source: str
    folder_path: str
    logical_locator: str
    source_input_mode: str
    source_requested_from: str
    raw_headers: str | None = None

    @property
    def all_recipients(self) -> tuple[MailboxParticipant, ...]:
        return self.to_recipients + self.cc_recipients + self.bcc_recipients

    @property
    def attachment_names(self) -> tuple[str, ...]:
        return tuple(attachment.attachment_name for attachment in self.attachments)

    @property
    def recipient_search_text(self) -> str:
        return " ".join(
            participant.search_text
            for participant in self.all_recipients
            if participant.search_text
        )

    @property
    def attachment_search_text(self) -> str:
        return " ".join(name for name in self.attachment_names if name)

    @property
    def dedup_key(self) -> str:
        if self.message_id:
            return f"message-id:{self.message_id}"

        fingerprint = sha256(
            json.dumps(self._fallback_dedup_key_fields(), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"fallback:{fingerprint}"

    @property
    def dedup_basis(self) -> str:
        if self.message_id:
            return json.dumps(
                {
                    "debug_context": {
                        "logical_locator": self.logical_locator,
                        "pst_source": self.pst_source,
                    },
                    "key_fields": {
                        "message_id": self.message_id,
                    },
                    "method": "message-id",
                },
                sort_keys=True,
            )

        return json.dumps(
            {
                "debug_context": {
                    "attachment_names": list(self.attachment_names),
                    "logical_locator": self.logical_locator,
                    "pst_source": self.pst_source,
                    "recipient_search_text": self.recipient_search_text,
                },
                "key_fields": self._fallback_dedup_key_fields(),
                "method": "fallback",
            },
            sort_keys=True,
        )

    def _fallback_dedup_key_fields(self) -> dict[str, str]:
        sent_text = self.sent_at.isoformat() if self.sent_at else ""
        return {
            "body_sha256": sha256(self.body_text.encode("utf-8")).hexdigest(),
            "sender": self.sender.search_text,
            "sent_at": sent_text,
            "subject": self.subject,
        }


@dataclass(frozen=True, slots=True)
class ExtractedMessage:
    raw_message: Mapping[str, object]
    folder_path: str
    logical_locator: str


@dataclass(frozen=True, slots=True)
class IngestFailure:
    pst_source: str
    folder_path: str | None
    logical_locator: str | None
    source_input_mode: str
    source_requested_from: str
    error_stage: str
    error_type: str
    error_message: str
    # #255: set only for attachment sub-failures. The size is what the retry
    # compares against before filling an empty attachment row (None = unknown).
    attachment_name: str | None = None
    attachment_size: int | None = None

    @classmethod
    def from_source(
        cls,
        source: IngestionSource,
        *,
        error_stage: str,
        error_type: str,
        error_message: str,
        folder_path: str | None = None,
        logical_locator: str | None = None,
        attachment_name: str | None = None,
        attachment_size: int | None = None,
    ) -> IngestFailure:
        return cls(
            pst_source=source.source_path,
            folder_path=folder_path,
            logical_locator=logical_locator,
            source_input_mode=source.input_mode,
            source_requested_from=source.requested_from_path,
            error_stage=error_stage,
            error_type=error_type,
            error_message=error_message,
            attachment_name=attachment_name,
            attachment_size=attachment_size,
        )


@dataclass(slots=True)
class ImportSummary:
    discovered_psts: int = 0
    imported_messages: int = 0
    duplicate_messages: int = 0
    failure_count: int = 0
    # #255: empty attachment rows filled from a re-extraction (dedup branch).
    recovered_attachments: int = 0
    recipient_extraction_summary_path: str | None = None
    recipient_extraction_complete: bool = True


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    message: ExtractedMessage | None = None
    failure: IngestFailure | None = None


def build_normalized_message(
    raw_message: Mapping[str, object],
    *,
    source: IngestionSource,
    folder_path: str,
    logical_locator: str,
) -> NormalizedMessage:
    return NormalizedMessage(
        subject=_coerce_text(raw_message.get("subject")),
        sender=_coerce_participant(raw_message.get("sender")),
        to_recipients=_coerce_participants(raw_message.get("to")),
        cc_recipients=_coerce_participants(raw_message.get("cc")),
        bcc_recipients=_coerce_participants(raw_message.get("bcc")),
        sent_at=_coerce_datetime(raw_message.get("sent_at")),
        received_at=_coerce_datetime(raw_message.get("received_at")),
        body_text=_normalize_body_text(raw_message.get("body_text")),
        attachments=_coerce_attachments(
            raw_message.get("attachments"), raw_message.get("attachment_names")
        ),
        message_id=_coerce_optional_preserved_text(raw_message.get("message_id")),
        in_reply_to=_coerce_optional_preserved_text(raw_message.get("in_reply_to")),
        references=_coerce_optional_preserved_text(raw_message.get("references")),
        pst_source=source.source_path,
        folder_path=folder_path.strip(),
        logical_locator=logical_locator.strip(),
        source_input_mode=source.input_mode,
        source_requested_from=source.requested_from_path,
        raw_headers=_coerce_optional_raw_headers(raw_message.get("raw_headers")),
    )


def _coerce_participant(raw_value: object) -> MailboxParticipant:
    if isinstance(raw_value, Mapping):
        display_name = _coerce_optional_text(raw_value.get("name"))
        email = _coerce_optional_text(raw_value.get("email"))
        return MailboxParticipant(display_name=display_name, email=email)

    text = _coerce_optional_text(raw_value)
    if text is None:
        return MailboxParticipant(display_name=None, email=None)
    return MailboxParticipant(display_name=None, email=text)


def _coerce_participants(raw_value: object) -> tuple[MailboxParticipant, ...]:
    if raw_value is None:
        return ()
    if isinstance(raw_value, Sequence) and not isinstance(
        raw_value, str | bytes | bytearray
    ):
        return tuple(
            participant
            for item in raw_value
            for participant in _coerce_participants(item)
            if participant.display_name or participant.email
        )

    participant = _coerce_participant(raw_value)
    if participant.display_name or participant.email:
        return (participant,)
    return ()


def _coerce_text_sequence(raw_value: object) -> tuple[str, ...]:
    if raw_value is None:
        return ()
    if isinstance(raw_value, Sequence) and not isinstance(
        raw_value, str | bytes | bytearray
    ):
        values = []
        for item in raw_value:
            text = _coerce_optional_text(item)
            if text:
                values.append(text)
        return tuple(values)

    text = _coerce_optional_text(raw_value)
    return (text,) if text else ()


def _coerce_attachments(
    raw_attachments: object,
    raw_attachment_names: object,
) -> tuple[NormalizedAttachment, ...]:
    if raw_attachments is not None:
        result: list[NormalizedAttachment] = []
        if isinstance(raw_attachments, Sequence) and not isinstance(
            raw_attachments, str | bytes | bytearray
        ):
            for item in raw_attachments:
                if not isinstance(item, Mapping):
                    continue
                name = _coerce_text(item.get("attachment_name"))
                if not name:
                    continue
                result.append(
                    NormalizedAttachment(
                        attachment_name=name,
                        content_hash=_coerce_optional_text(item.get("content_hash")),
                        size_bytes=_coerce_optional_int(item.get("size_bytes")),
                        mime_type=_coerce_optional_text(item.get("mime_type")),
                        storage_path=_coerce_optional_text(item.get("storage_path")),
                    )
                )
        return tuple(result)

    names = _coerce_text_sequence(raw_attachment_names)
    return tuple(NormalizedAttachment(attachment_name=name) for name in names)


def _coerce_optional_int(raw_value: object) -> int | None:
    if raw_value is None or raw_value == "":
        return None
    if isinstance(raw_value, str | bytes | bytearray):
        return int(raw_value)
    if hasattr(raw_value, "__int__"):
        return int(raw_value)
    raise TypeError(f"Unsupported integer value: {raw_value!r}")


def _coerce_datetime(raw_value: object) -> datetime | None:
    if raw_value in (None, ""):
        return None
    if isinstance(raw_value, datetime):
        return raw_value.replace(microsecond=0)
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized)
    raise TypeError(f"Unsupported datetime value: {raw_value!r}")


def _normalize_body_text(raw_value: object) -> str:
    return _coerce_text(raw_value).replace("\r\n", "\n").replace("\r", "\n")


def _coerce_text(raw_value: object) -> str:
    if raw_value is None:
        return ""
    return str(raw_value).strip()


def _coerce_optional_text(raw_value: object) -> str | None:
    text = _coerce_text(raw_value)
    return text or None


def _coerce_optional_preserved_text(raw_value: object) -> str | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, Sequence) and not isinstance(
        raw_value, str | bytes | bytearray
    ):
        raise TypeError(
            "Thread header values must be scalar text to preserve them exactly."
        )
    text = str(raw_value)
    if not text.strip():
        return None
    return text


def _coerce_optional_raw_headers(raw_value: object) -> str | None:
    """Coerce parser dict's raw_headers value to ``str | None``.

    ``None`` is the canonical sentinel for "no header data available".
    Empty string is treated as None (not a valid sentinel per spec).
    Any non-None, non-empty string is returned as-is (verbatim Latin-1 decoded
    header section from the parser).
    """
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        return None
    # Empty string is not a valid sentinel — map to None.
    return raw_value if raw_value else None

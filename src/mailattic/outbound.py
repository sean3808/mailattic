"""Outbound email assembly and SMTP submission for mailattic.

This module is the peer of imap_sync.py and owns all outbound-facing logic:
- SMTP configuration reading
- Legacy TLS context aligned with the self-hosted mail server
- (later chunks) RFC5322 draft assembly, reply-all recipient derivation,
  interactive send gate, crash-safe ordering, and draft listing.

Design constraints (see openspec/changes/add-outbound-send-capability/design.md):
- No AI/generative-model dependencies — bes stays deterministic.
- Caller provides reply body via --body-from; bes never generates content.
- Every send needs a draft fingerprint matching the current draft bytes, or a
  real-TTY typed authorization (#195). Whether the user was actually asked is
  the calling agent's duty at the conversation layer; bes only guarantees that
  what is sent is what was reviewed.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import html
import json
import mimetypes
import os
import re
import smtplib
import sqlite3
import ssl
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.headerregistry import Address
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import (
    formataddr,
    formatdate,
    getaddresses,
    make_msgid,
    parseaddr,
    parsedate_to_datetime,
)
from pathlib import Path
from typing import Any

from .attachment_store import resolve_store_root, write_attachment
from .datetime_bounds import TAIPEI
from .identity import list_self_addresses
from .imap_native import NativeImapClient
from .imap_runtime import load_env_file
from .models import MailboxParticipant, NormalizedAttachment, NormalizedMessage
from .mutf7 import encode_mutf7
from .parser import subject_from_raw_headers
from .query import get_message, render_text
from .storage import connect_database, persist_message

__all__ = [
    "reply_all_recipients",
    "build_thread_headers",
    "build_quoted_reply_bodies",
    "reply_font_style",
    "resolve_attachment_mime_type",
    "resolve_smtp_env_file",
    "DEFAULT_SMTP_ENV_FILE",
    "ENV_VAR_SMTP_ENV_FILE",
    "assemble_draft_reply",
    "DraftReplyError",
    "DraftReplyResult",
    "draft_fingerprint",
    "send_draft",
    "send_batch",
]

# Default SMTP settings file (issue #199): same absolute path as
# imap_sync.DEFAULT_ENV_FILE -- this single-mailbox account keeps IMAP and
# SMTP credentials in one .env file at the repo root. cwd of the caller must
# not affect resolution, so this is a fixed absolute literal, not a relative
# path (mirrors storage.DEFAULT_DB_NAME).
DEFAULT_SMTP_ENV_FILE = r"C:\IT_Projects\mailattic\.env.sean3808"
ENV_VAR_SMTP_ENV_FILE = "MAILATTIC_SMTP_ENV_FILE"


def resolve_smtp_env_file(explicit_path: str | Path | None = None) -> Path:
    """Return the SMTP settings file path using explicit/env/default precedence.

    Mirrors :func:`storage.resolve_database_default`: an explicit path wins,
    then the ``MAILATTIC_SMTP_ENV_FILE`` environment variable (empty or
    whitespace-only treated as unset), then the built-in absolute default.
    """
    if explicit_path is not None:
        return Path(explicit_path)
    env_val = os.environ.get(ENV_VAR_SMTP_ENV_FILE, "")
    if env_val.strip():
        return Path(env_val)
    return Path(DEFAULT_SMTP_ENV_FILE)


def draft_fingerprint(draft_bytes: bytes) -> str:
    """Return a draft's fingerprint: SHA-256 (lowercase hex) of its ``.eml`` bytes."""
    return hashlib.sha256(draft_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Outbound attachment MIME types
# ---------------------------------------------------------------------------

# The Content-Type of an attachment we send is our own outbound contract, so
# it is decided here rather than borrowed from the sending machine.
# `mimetypes` seeds its extension map from the Windows registry, whose
# `Content Type` values are written by whatever software is installed: the
# #60 spike measured `.xlsx` as the OOXML spreadsheet type on a box with
# Office and as `application/octet-stream` on a Windows Server 2025 box
# without it. Same draft, different bytes on the wire, no error either way.
#
# Values are the IANA / OOXML registrations, not a snapshot of any host's
# registry. This is source code on purpose -- there is deliberately no
# configuration hook, because a machine-local override would reintroduce the
# host dependence this table exists to remove.
#
# Extensions outside the table keep the previous `guess_type` behaviour:
# covering everything would mean a table nobody maintains, and a mislabelled
# image is a lesser harm than a mislabelled spreadsheet.
_ATTACHMENT_MIME_TYPES: dict[str, str] = {
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
}


def resolve_attachment_mime_type(filename: str) -> tuple[str, str]:
    """Return the ``(maintype, subtype)`` to send an attachment under.

    Resolution order: the repository's own extension table (matched on the
    filename's final suffix, case-insensitively), then `mimetypes.guess_type`
    for suffixes the table does not cover, then `application/octet-stream`.

    Only the first step is host-independent, and that is the point: the
    formats this mailbox actually sends are pinned here, and the host map is
    consulted for the long tail where being wrong is cheap.
    """
    suffix = Path(filename).suffix.lower()
    pinned = _ATTACHMENT_MIME_TYPES.get(suffix)
    if pinned is not None:
        maintype, _, subtype = pinned.partition("/")
        return maintype, subtype

    guessed_type, _encoding = mimetypes.guess_type(filename)
    if guessed_type:
        maintype, _, subtype = guessed_type.partition("/")
        if maintype and subtype:
            return maintype, subtype
    return "application", "octet-stream"


def _sanitize_for_terminal(value: str) -> str:
    """Escape terminal control characters so attacker/data-influenced text

    (email Subject, recipient display names, attachment filenames, etc.)
    cannot inject ANSI/terminal control sequences into a TTY authorization
    summary (e.g. clear-screen / conceal / embedded-newline line injection
    used to hide recipients from the operator before an irreversible send).

    Escapes every Unicode category "Cc" codepoint (C0 controls incl. ESC/CR/
    LF/TAB, DEL, and C1 controls) to a visible `\\xHH` form; every other
    character -- including CJK and other non-ASCII text -- passes through
    unchanged.
    """
    return "".join(
        ch if unicodedata.category(ch) != "Cc" else f"\\x{ord(ch):02x}" for ch in value
    )


# ---------------------------------------------------------------------------
# SMTP configuration
# ---------------------------------------------------------------------------

_REQUIRED_SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")

# IMAP settings needed to open a connection for the best-effort Sent-folder
# APPEND (design Decision 3, add-mutf7-folder-codec). Distinct from the sync
# path's REQUIRED_SYNC_ENV in imap_runtime.py (which also requires
# IMAP_ACCOUNT_NAME): the append needs only enough to open a client.
_REQUIRED_IMAP_APPEND_KEYS = ("IMAP_HOST", "IMAP_PORT", "IMAP_USER", "IMAP_PASSWORD")

# Design Decision 5 (add-mutf7-folder-codec): the Sent folder name is a
# module constant, not configuration -- there is exactly one mailbox, one
# Outlook client, one Sent folder. Upgrade path if a second account with a
# different Sent-folder name ever appears: promote to an env var read
# alongside the SMTP settings (e.g. IMAP_SENT_FOLDER_NAME).
_SENT_FOLDER_DISPLAY_NAME = "寄件備份"
_SENT_FOLDER_WIRE_NAME = encode_mutf7(_SENT_FOLDER_DISPLAY_NAME)

# Bounded socket timeout (seconds) for the best-effort Sent-folder APPEND
# connection (Codex semantic review, add-mutf7-folder-codec design Decision
# 3): without a timeout, a server that accepts the TCP connection but never
# responds would block the APPEND -- and therefore a completed send, and
# every remaining draft in a send-batch run -- forever. 30s is generous for
# a nearby mail server while still bounding the worst case.
_APPEND_IMAP_TIMEOUT_SECONDS = 30.0

# Per-socket-operation timeout (seconds) for SMTP submission (#227): applies to
# connect and each read/write, NOT a total deadline for the whole send. Without
# it the stdlib inherits socket.getdefaulttimeout() (None) and one stalled
# server hangs send-draft, the rest of a batch, and the MCP send lock.
_SMTP_TIMEOUT_SECONDS = 60.0

# A mail server may apply a recipient-rate limit across consecutive SMTP
# submissions. Production batches use a deliberately fixed cadence; keeping
# the wait behind a tiny seam lets unit tests prove ordering without sleeping.
BATCH_SEND_INTERVAL_SECONDS = 90


def _wait_for_batch_interval(seconds: float) -> None:
    time.sleep(seconds)


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """Parsed SMTP submission settings for the outbound mail account."""

    host: str
    port: int
    user: str
    password: str


class SendRefused(Exception):
    pass


def load_smtp_config(env: dict[str, str] | str | Path) -> SmtpConfig:
    """Return an :class:`SmtpConfig` from an env dict or an env file path.

    Parameters
    ----------
    env:
        Either a ``dict[str, str]`` of already-loaded env values, or a
        ``str`` / ``Path`` pointing to a ``.env``-style file that will be
        parsed with :func:`~mailattic.imap_runtime.load_env_file`.

    Raises
    ------
    FileNotFoundError
        When *env* is a path and the file does not exist.
    ValueError
        When any required SMTP key is absent or empty.
    """
    if isinstance(env, str | Path):
        env = load_env_file(env)

    missing = [k for k in _REQUIRED_SMTP_KEYS if not env.get(k, "").strip()]
    if missing:
        raise ValueError(
            f"Missing required SMTP configuration key(s): {', '.join(missing)}"
        )

    try:
        port = int(env["SMTP_PORT"])
    except ValueError as exc:
        raise ValueError(
            f"SMTP_PORT must be an integer, got: {env['SMTP_PORT']!r}"
        ) from exc

    return SmtpConfig(
        host=env["SMTP_HOST"].strip(),
        port=port,
        user=env["SMTP_USER"].strip(),
        password=env["SMTP_PASSWORD"].strip(),
    )


# ---------------------------------------------------------------------------
# Legacy TLS context for the self-hosted mail server (SMTPS)
# ---------------------------------------------------------------------------


def build_smtp_ssl_context() -> ssl.SSLContext:
    """Return a legacy-compatible SSL context for SMTPS connections to the mail server.

    The self-hosted mail server uses a self-signed / legacy certificate that is
    incompatible with the default SSL context in two ways:
      (1) weak DH parameters → the OpenSSL security level must be lowered
      (2) a certificate the default verifier rejects
          → check_hostname must be disabled + certificate verification skipped

    This is identical in intent to :func:`imap_sync._build_legacy_ssl_context`.
    Both IMAPS and SMTPS land on the same server, so the same trust model
    applies: the owner controls the server; PKI chain-of-trust is not required.

    If the mail server is ever upgraded to a proper certificate and strong DH
    parameters, replace this with ``ssl.create_default_context()`` and remove
    the security-level override.

    NOTE: A real-server TLS handshake smoke test (connect to
    mail.mailattic.test:465 without sending mail) is a manual step to be run by
    the operator before the first production send.  Do NOT attempt a live
    connection in automated unit tests.
    """
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------------------------------------------------------------------
# Draft reply assembly helpers
# ---------------------------------------------------------------------------


def reply_all_recipients(
    original: dict[str, object],
    own_addresses: set[str],
) -> dict[str, list[tuple[str | None, str]]]:
    """Return reply-all To/Cc recipients for a stored original message."""
    own_normalized = {
        address.casefold() for address in own_addresses if address.strip()
    }
    seen: set[str] = set()
    result: dict[str, list[tuple[str | None, str]]] = {"to": [], "cc": []}

    def add_recipient(
        bucket: str,
        display_name: object,
        email: object,
    ) -> None:
        if not isinstance(email, str) or not email:
            return
        normalized = email.casefold()
        if normalized in own_normalized or normalized in seen:
            return
        seen.add(normalized)
        result[bucket].append(
            (display_name if isinstance(display_name, str) else None, email)
        )

    add_recipient("to", original.get("sender_name"), original.get("sender_email"))

    for bucket in ("to", "cc"):
        raw_recipients = original.get(bucket)
        if not isinstance(raw_recipients, list):
            continue
        for recipient in raw_recipients:
            if not isinstance(recipient, dict):
                continue
            add_recipient(bucket, recipient.get("name"), recipient.get("email"))

    return result


def _looks_undecoded(text: str) -> bool:
    return "\ufffd" in text or any("\ud800" <= char <= "\udfff" for char in text)


def _original_subject(original: dict[str, object]) -> str:
    """The original's subject, repaired from ``raw_headers`` when it is broken.

    A row ingested before the charset fix (#202) can store a subject with
    U+FFFD or surrogates; re-encoding that into a reply sends the garbage to
    the recipient. ``raw_headers`` keeps the original bytes, so such a subject
    is re-decoded from there. A stored subject that decoded cleanly is left
    alone: re-decoding cannot improve it, and ``raw_headers`` of an Outlook
    rebuild row is already-decoded text rather than a byte image.
    """
    subject_value = original.get("subject")
    stored = subject_value if isinstance(subject_value, str) else ""
    raw_headers = original.get("raw_headers")
    if _looks_undecoded(stored) and isinstance(raw_headers, str) and raw_headers:
        decoded = subject_from_raw_headers(raw_headers)
        if decoded is not None and not _looks_undecoded(decoded):
            return decoded.strip()
    return stored


def build_thread_headers(original: dict[str, object]) -> dict[str, str]:
    """Build thread-continuity headers for a reply draft."""
    message_id_value = original.get("message_id")
    if not isinstance(message_id_value, str) or not message_id_value.strip():
        raise ValueError(
            "Cannot build thread headers: original message has no Message-ID"
        )

    references_value = original.get("references_header")
    if references_value is None:
        references_value = original.get("references")
    reference_tokens = (
        str(references_value).split()
        if references_value is not None and str(references_value).strip()
        else []
    )
    if not reference_tokens or reference_tokens[-1] != message_id_value:
        reference_tokens.append(message_id_value)

    subject = _original_subject(original)
    if not subject.startswith("Re: "):
        subject = f"Re: {subject}"

    return {
        "in_reply_to": message_id_value,
        "references": " ".join(reference_tokens),
        "subject": subject,
    }


def generate_draft_message_id(own_address: str | None = None) -> str:
    """Return a fresh bracketed Message-ID for a draft message.

    The domain is the sender's own: a hard-coded domain would put someone
    else's (or a placeholder) domain on every outgoing mail. Without a sender
    the stdlib default (local hostname) is used.
    """
    domain = parseaddr(own_address or "")[1].rpartition("@")[2]
    return make_msgid(domain=domain or None)


# ---------------------------------------------------------------------------
# Quoted-reply body assembly
# ---------------------------------------------------------------------------

# Locale-independent English day and month names for Outlook-style Sent formatting.
# strftime %A / %B are locale-dependent; these constants avoid locale surprises on
# Windows systems with a non-English system locale.
_WEEKDAY_EN = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_MONTH_EN = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _outlook_sent_format(dt: datetime) -> str:
    """Format a datetime to Outlook-style English string, locale-independent."""
    hour12 = dt.hour % 12 or 12
    period = "AM" if dt.hour < 12 else "PM"
    return (
        f"{_WEEKDAY_EN[dt.weekday()]}, {_MONTH_EN[dt.month]} {dt.day:02d}, {dt.year}"
        f" {hour12:02d}:{dt.minute:02d}:{dt.second:02d} {period}"
    )


def _decode_rfc2047_text(value: str) -> str:
    return str(make_header(decode_header(value)))


def _format_recipient_header(entries_raw: object) -> str:
    return ", ".join(
        _decode_rfc2047_text(formataddr((r.get("name") or "", r.get("email") or "")))
        for r in (entries_raw if isinstance(entries_raw, list) else [])
        if isinstance(r, dict) and r.get("email")
    )


def build_quoted_reply_bodies(
    original: dict[str, object],
    reply_text: str,
    *,
    font_style: str = "",
) -> tuple[str, str]:
    """Build plain-text and HTML quoted-reply bodies from an original message dict.

    Returns ``(plain_body, html_body)``.

    The quoted block is placed below the caller-provided reply text, preceded by a
    visual separator.  Field order: From / Sent / To / [Cc] / Subject.  The Cc line
    is omitted entirely when the original has no CC recipients.
    A non-empty ``font_style`` (from :func:`reply_font_style`) wraps the whole
    HTML body, reply and quoted block alike, in one styled ``div``.
    """
    sender_name = original.get("sender_name")
    sender_email = str(original.get("sender_email") or "")
    from_val = _decode_rfc2047_text(
        formataddr((sender_name if isinstance(sender_name, str) else "", sender_email))
    )

    sent_at_raw = original.get("sent_at")
    if not sent_at_raw:
        sent_val = ""
    else:
        try:
            dt = datetime.fromisoformat(str(sent_at_raw))
            if dt.tzinfo is None:
                # Stored timestamps without an offset mean UTC (D-20260723-1).
                dt = dt.replace(tzinfo=UTC)
            sent_val = _outlook_sent_format(dt.astimezone(TAIPEI))
        except (ValueError, TypeError):
            sent_val = str(sent_at_raw)

    to_val = _format_recipient_header(original.get("to"))
    cc_val = _format_recipient_header(original.get("cc"))

    subject_val = _original_subject(original)
    # Same cleaner as the display path, so style text already stored in an
    # older body is not quoted back to the recipient (#294).
    body_text_val = render_text(str(original.get("body_text") or ""))

    # Plain-text body: reply + separator line + header block + original body
    separator = "_" * 32
    header_lines = [
        f"From: {from_val}",
        f"Sent: {sent_val}",
        f"To: {to_val}",
    ]
    if cc_val:
        header_lines.append(f"Cc: {cc_val}")
    header_lines.append(f"Subject: {subject_val}")

    plain_body = (
        reply_text
        + "\n\n"
        + separator
        + "\n"
        + "\n".join(header_lines)
        + "\n\n"
        + body_text_val
    )

    # HTML body: escaped reply + quoted block div with <hr> separator and <b> labels
    def _esc(s: str) -> str:
        return html.escape(s).replace("\n", "<br>")

    cc_row = f"<b>Cc:</b> {_esc(cc_val)}<br>\n" if cc_val else ""
    html_body = (
        _esc(reply_text)
        + "\n<div>\n<hr>\n"
        + f"<b>From:</b> {_esc(from_val)}<br>\n"
        + f"<b>Sent:</b> {_esc(sent_val)}<br>\n"
        + f"<b>To:</b> {_esc(to_val)}<br>\n"
        + cc_row
        + f"<b>Subject:</b> {_esc(subject_val)}<br>\n"
        + "<br>\n"
        + _esc(body_text_val)
        + "\n</div>"
    )
    if font_style:
        html_body = f'<div style="{font_style}">\n{html_body}\n</div>'

    return plain_body, html_body


def _get_message_by_rfc822_message_id(
    database_path: str | Path,
    message_id: str,
    *,
    read_only: bool = False,
) -> dict[str, object]:
    identifier = message_id.strip()
    clean_identifier = identifier.strip("<>")
    candidates = list(
        dict.fromkeys([identifier, clean_identifier, f"<{clean_identifier}>"])
    )

    connection = connect_database(database_path, create=False, read_only=read_only)
    try:
        placeholders = ",".join("?" for _ in candidates)
        rows = connection.execute(
            "SELECT id, source_input_mode, pst_source FROM messages "
            f"WHERE message_id IN ({placeholders})",
            candidates,
        ).fetchall()
    finally:
        connection.close()

    if not rows:
        raise KeyError(f"No message found for message_id {message_id!r}")
    # The bracketed and bare spellings can each hold a copy of the same message
    # (e.g. Sent and archive-scraped). Prefer a non-archive copy so the
    # archive-anchor refusal only applies when nothing better exists (#208);
    # the lowest id breaks ties so the pick never depends on the query plan.
    best = min(rows, key=lambda row: (_is_archive_copy(row), int(row["id"])))
    return get_message(database_path, int(best["id"]), read_only=read_only)


def _is_archive_copy(message: Mapping[str, object] | sqlite3.Row) -> bool:
    source_input_mode = str(message["source_input_mode"] or "").casefold()
    pst_source = str(message["pst_source"] or "").casefold()
    return source_input_mode == "archive" or pst_source.startswith("archive:")


def _resolve_draft_recipients(
    original: dict[str, object],
    *,
    own_address: str,
    self_addresses: Sequence[str],
    explicit_to: Sequence[str] | None,
    explicit_cc: Sequence[str] | None,
    explicit_bcc: Sequence[str] | None,
    excluded: Sequence[str] | None,
    replace: bool,
) -> dict[str, list[tuple[str | None, str]]]:
    """Resolve draft recipients without affecting message-based thread headers."""
    parsed_explicit = {
        "to": _parse_recipient_options(explicit_to, "--to"),
        "cc": _parse_recipient_options(explicit_cc, "--cc"),
        "bcc": _parse_recipient_options(explicit_bcc, "--bcc"),
    }

    excluded_addresses = {
        address.casefold()
        for _display_name, address in _parse_recipient_options(excluded, "--exclude")
    }
    explicit_visible_addresses = {
        address.casefold()
        for bucket in ("to", "cc")
        for _display_name, address in parsed_explicit[bucket]
    }
    result: dict[str, list[tuple[str | None, str]]] = {
        "to": [],
        "cc": [],
        "bcc": [],
    }
    seen: set[str] = set()

    def add(bucket: str, recipient: tuple[str | None, str]) -> None:
        normalized = recipient[1].casefold()
        if normalized in seen:
            return
        seen.add(normalized)
        result[bucket].append(recipient)

    if not replace:
        derived = reply_all_recipients(original, {own_address, *self_addresses})
        for bucket in ("to", "cc"):
            for recipient in derived[bucket]:
                normalized = recipient[1].casefold()
                if (
                    normalized not in excluded_addresses
                    and normalized not in explicit_visible_addresses
                ):
                    add(bucket, recipient)

    for bucket in ("to", "cc"):
        for recipient in parsed_explicit[bucket]:
            add(bucket, recipient)

    if not result["to"]:
        raise ValueError("draft-reply: at least one To recipient is required")

    # Preserve the existing automatic Bcc-self safety copy independently of
    # caller-controlled To/Cc selection.
    result["bcc"].append((None, own_address))
    bcc_seen = {own_address.casefold()}
    for recipient in parsed_explicit["bcc"]:
        normalized = recipient[1].casefold()
        if normalized in seen or normalized in bcc_seen:
            continue
        bcc_seen.add(normalized)
        result["bcc"].append(recipient)

    return result


def _find_excluded_explicit_conflicts(
    *,
    explicit_to: Sequence[str] | None,
    explicit_cc: Sequence[str] | None,
    explicit_bcc: Sequence[str] | None,
    excluded: Sequence[str] | None,
) -> list[str]:
    excluded_addresses = {
        address.casefold()
        for _display_name, address in _parse_recipient_options(excluded, "--exclude")
    }
    explicit_addresses = {
        address.casefold()
        for values, flag in (
            (explicit_to, "--to"),
            (explicit_cc, "--cc"),
            (explicit_bcc, "--bcc"),
        )
        for _display_name, address in _parse_recipient_options(values, flag)
    }
    return sorted(excluded_addresses & explicit_addresses)


def _parse_recipient_options(
    values: Sequence[str] | None,
    flag: str,
) -> list[tuple[str | None, str]]:
    parsed_recipients: list[tuple[str | None, str]] = []
    for value in values or ():
        try:
            candidate = EmailMessage(policy=policy.default)
            candidate["To"] = value
            header = candidate["To"]
            addresses = header.addresses
            if header.defects or len(addresses) != 1:
                raise ValueError
            display_name = addresses[0].display_name
            address = addresses[0].addr_spec
            if not address or not address.isascii():
                raise ValueError
            Address(display_name=display_name, addr_spec=address)
        except (AttributeError, HeaderParseError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{flag} requires one valid email address: {value!r}"
            ) from exc
        parsed_recipients.append((display_name or None, address))
    return parsed_recipients


def _format_address_header(recipients: list[tuple[str | None, str]]) -> str:
    return ", ".join(
        formataddr((display_name or "", email)) for display_name, email in recipients
    )


#: MCP ``mail_draft_reply`` delivers this as a fragment capped at
#: ``mcp_delivery.ERROR_ECHO_CAP_CHARS`` (200), so it must stay under that
#: length or the remedy at its end is cut off (#209).
ARCHIVE_ANCHOR_REFUSAL = (
    "Archive-scraped outbound mail cannot be a reply anchor: its recipients "
    "and Message-ID are unverified. "
    "Reply to an inbound message in the same thread, then verify To/Cc."
)


class DraftReplyError(Exception):
    """Expected refusal from ``assemble_draft_reply``; its message is user-facing."""


# A font name lands inside a quoted CSS value inside an HTML attribute, so
# anything that could close either is refused rather than escaped (#13).
_FONT_NAME_FORBIDDEN = frozenset("\"'`;:<>&\\{}()\r\n")
_FONT_SIZE_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:pt|px)")


def reply_font_style(env: Mapping[str, str]) -> str:
    """CSS for the reply's HTML body from the SMTP settings file (#13).

    ``MAIL_FONT_LATIN`` / ``MAIL_FONT_CJK`` / ``MAIL_FONT_SIZE`` are optional;
    none set returns ``""`` (no styling, the draft is unchanged). Latin text
    falls back to Calibri, CJK text to Microsoft JhengHei / PingFang TC, and
    Outlook desktop gets the CJK font through ``mso-fareast-font-family``;
    either font key alone still brings in both fallback chains.
    A malformed value raises ``DraftReplyError`` naming the key.
    """
    latin = env.get("MAIL_FONT_LATIN", "").strip()
    cjk = env.get("MAIL_FONT_CJK", "").strip()
    size = env.get("MAIL_FONT_SIZE", "").strip()
    for key, name in (("MAIL_FONT_LATIN", latin), ("MAIL_FONT_CJK", cjk)):
        if any(char in _FONT_NAME_FORBIDDEN for char in name):
            raise DraftReplyError(
                f"{key} in the SMTP settings file must be a plain font name, "
                f"got {name!r}."
            )
    if size and not _FONT_SIZE_PATTERN.fullmatch(size):
        raise DraftReplyError(
            "MAIL_FONT_SIZE in the SMTP settings file must look like 12pt or "
            f"16px, got {size!r}."
        )

    declarations = []
    if latin or cjk:
        # Either key alone fills in the other side's fallbacks too, so CJK
        # text is never left to the client default (Sean, #13 review).
        families = [latin, "Calibri", cjk, "Microsoft JhengHei", "PingFang TC"]
        quoted = (f"'{family}'" for family in dict.fromkeys(filter(None, families)))
        declarations.append(f"font-family: {', '.join(quoted)}, sans-serif")
        # Outlook desktop picks a font per script from these, as in the
        # HTML Outlook itself writes; other clients ignore mso-*.
        for slot in ("ascii", "hansi"):
            declarations.append(f"mso-{slot}-font-family: '{latin or 'Calibri'}'")
        declarations.append(f"mso-fareast-font-family: '{cjk or 'Microsoft JhengHei'}'")
    if size:
        declarations.append(f"font-size: {size}")
    return "; ".join(declarations)


def _declared_self_addresses(
    database_path: str | Path, *, read_only: bool = False
) -> list[str]:
    connection = connect_database(database_path, create=False, read_only=read_only)
    try:
        return list_self_addresses(connection)
    finally:
        connection.close()


def own_address_without_smtp_settings(
    database_path: str | Path, *, read_only: bool = False
) -> str:
    """The draft's From when the SMTP settings cannot name the account (#268).

    The one declared self address stands in. Zero or several cannot say which
    address sends, and there is no built-in one to fall back to, so that is
    refused naming both things the operator can set. The refusal reaches
    CoWork as a capped error fragment (#209), so it stays under
    ``mcp_delivery.ERROR_ECHO_CAP_CHARS``.
    """
    declared = _declared_self_addresses(database_path, read_only=read_only)
    if len(declared) != 1:
        raise DraftReplyError(
            "No sender address: SMTP settings are unreadable and "
            f"{len(declared)} self addresses are declared (need exactly 1). "
            "Set SMTP_USER in the SMTP settings file, or run "
            "`bes identity add <email>`."
        )
    return declared[0]


@dataclass(frozen=True, slots=True)
class DraftReplyResult:
    """What ``assemble_draft_reply`` wrote: the draft file and its rendered headers.

    ``attachments`` is ``(filename, size_in_bytes)`` per attachment, in order;
    ``fingerprint`` is :func:`draft_fingerprint` of the written file.
    """

    draft_id: str
    draft_path: Path
    message_id: str
    from_address: str
    subject: str
    to: str
    cc: str
    bcc: str
    attachments: tuple[tuple[str, int], ...]
    fingerprint: str
    body_text: str = ""
    attachment_sources: tuple[tuple[str, str, int], ...] = ()


def _validate_custom_subject(subject: str) -> None:
    if not subject.strip():
        raise DraftReplyError(
            '--subject must not be empty. Omit it to reply as "Re: <original subject>".'
        )
    # Line breaks (including U+2028/U+2029, which the email package also
    # refuses) cannot go into a header value, and the draft summary the user
    # reviews must not carry terminal control sequences.
    if any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in subject):
        raise DraftReplyError(
            "--subject must be a single line without control characters."
        )


def assemble_draft_reply(
    database_path: str | Path,
    message_id: str,
    *,
    own_address: str,
    draft_dir: str | Path | None = None,
    body_text: str | None = None,
    body_from: str | Path | None = None,
    to: Sequence[str] | None = None,
    cc: Sequence[str] | None = None,
    bcc: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    replace_recipients: bool = False,
    attachments: Sequence[str | Path] | None = None,
    allow_archive_anchor: bool = False,
    on_archive_anchor: Callable[[], None] | None = None,
    read_only: bool = False,
    subject: str | None = None,
    font_style: str = "",
) -> DraftReplyResult:
    """Build a reply draft to ``message_id`` and write ``<draft_dir>/<draft_id>.eml``.

    ``read_only`` opens the database read-only (the MCP server's contract #1);
    the anchor lookup is the only database access here.
    The body comes from ``body_text`` or, if that is None, the file ``body_from``.
    An archive-scraped anchor is refused unless ``allow_archive_anchor``; when it
    is allowed, ``on_archive_anchor`` fires before any later step can fail.
    ``subject`` replaces the default "Re: <original subject>" verbatim (no
    prefix added); threading headers still come from the anchor.
    ``font_style`` styles the HTML body (see :func:`build_quoted_reply_bodies`).
    Expected refusals (bad recipients, unknown or archive-scraped anchor,
    unreadable body or attachment) raise ``DraftReplyError``; anything else,
    such as a failed draft write, propagates unwrapped.
    """
    if subject is not None:
        _validate_custom_subject(subject)

    if replace_recipients and exclude:
        raise DraftReplyError(
            "--exclude only filters reply-all-derived recipients; "
            "--replace-recipients skips derivation entirely. Remove --exclude, "
            "or drop --replace-recipients."
        )

    try:
        excluded_explicit_conflicts = _find_excluded_explicit_conflicts(
            explicit_to=to,
            explicit_cc=cc,
            explicit_bcc=bcc,
            excluded=exclude,
        )
    except ValueError as exc:
        raise DraftReplyError(str(exc)) from exc
    if excluded_explicit_conflicts:
        raise DraftReplyError(
            "--exclude conflicts with explicit recipient address(es): "
            f"{', '.join(excluded_explicit_conflicts)}. Remove each address from "
            "--exclude or from --to/--cc/--bcc."
        )

    try:
        original = _get_message_by_rfc822_message_id(
            database_path, message_id, read_only=read_only
        )
    except KeyError as exc:
        raise DraftReplyError(str(exc.args[0]) if exc.args else str(exc)) from exc
    except (FileNotFoundError, sqlite3.Error) as exc:
        raise DraftReplyError(str(exc)) from exc

    if _is_archive_copy(original):
        if not allow_archive_anchor:
            raise DraftReplyError(ARCHIVE_ANCHOR_REFUSAL)
        if on_archive_anchor is not None:
            on_archive_anchor()

    if body_text is None:
        if body_from is None:
            raise ValueError("Either body_text or body_from is required")
        try:
            body_text = Path(body_from).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DraftReplyError(f"Could not read reply body file: {exc}") from exc

    if not original.get("message_id"):
        raise DraftReplyError("original message has no Message-ID")

    try:
        thread_headers = build_thread_headers(original)
        recipients = _resolve_draft_recipients(
            original,
            own_address=own_address,
            # Every declared self address is the operator, not only the one
            # sending: an alias on the original must not get a copy back.
            self_addresses=_declared_self_addresses(database_path, read_only=read_only),
            explicit_to=to,
            explicit_cc=cc,
            explicit_bcc=bcc,
            excluded=exclude,
            replace=replace_recipients,
        )
    except ValueError as exc:
        raise DraftReplyError(str(exc)) from exc

    draft_message_id = generate_draft_message_id(own_address)
    draft_id = str(uuid.uuid4())

    msg = EmailMessage()
    msg["From"] = own_address
    msg["To"] = _format_address_header(recipients["to"])
    if recipients["cc"]:
        msg["Cc"] = _format_address_header(recipients["cc"])
    msg["Bcc"] = _format_address_header(recipients["bcc"])
    msg["Message-ID"] = draft_message_id
    msg["In-Reply-To"] = thread_headers["in_reply_to"]
    msg["References"] = thread_headers["references"]
    msg["Subject"] = thread_headers["subject"] if subject is None else subject
    msg["Date"] = formatdate(localtime=True)
    plain_body, html_body = build_quoted_reply_bodies(
        original, body_text, font_style=font_style
    )
    msg.set_content(plain_body, charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    attachment_payloads: list[tuple[Path, bytes]] = []
    for path_item in attachments or []:
        path = Path(path_item)
        try:
            if not path.is_file():
                raise DraftReplyError(f"Attachment path is not a file: {path}")
            data = path.read_bytes()
        except OSError as exc:
            raise DraftReplyError(
                f"Could not read attachment file {path}: {exc}"
            ) from exc
        attachment_payloads.append((path, data))

    for path, data in attachment_payloads:
        maintype, subtype = resolve_attachment_mime_type(path.name)
        if (maintype, subtype) == ("message", "rfc822"):
            # RFC 2046 5.2.1: message/rfc822 may not be base64; hand the content
            # manager a parsed message so it nests it with a legal 7bit/8bit CTE.
            msg.add_attachment(
                BytesParser(policy=policy.default).parsebytes(data),
                filename=path.name,
            )
            continue
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    out_dir = Path(draft_dir) if draft_dir is not None else _draft_dir(database_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    draft_path = out_dir / f"{draft_id}.eml"
    draft_bytes = msg.as_bytes()
    draft_path.write_bytes(draft_bytes)

    return DraftReplyResult(
        draft_id=draft_id,
        draft_path=draft_path,
        message_id=draft_message_id,
        from_address=str(msg["From"]),
        subject=str(msg["Subject"]),
        to=str(msg["To"]),
        cc=str(msg.get("Cc", "")),
        bcc=str(msg["Bcc"]),
        attachments=tuple((path.name, len(data)) for path, data in attachment_payloads),
        fingerprint=draft_fingerprint(draft_bytes),
        body_text=plain_body,
        attachment_sources=tuple(
            (str(path.resolve()), path.name, len(data))
            for path, data in attachment_payloads
        ),
    )


# ---------------------------------------------------------------------------
# Send-draft gates and submission
# ---------------------------------------------------------------------------


def _draft_dir(database_path: Path | str) -> Path:
    return Path(database_path).resolve().parent / "outbound" / "drafts"


def _marker_dir(database_path: Path | str) -> Path:
    return Path(database_path).resolve().parent / "outbound" / "markers"


def _marker_path(database_path: Path | str, message_id: str) -> Path:
    fs_safe = base64.urlsafe_b64encode(message_id.encode()).decode()
    return _marker_dir(database_path) / f"{fs_safe}.json"


def _read_marker(database_path: Path | str, message_id: str) -> dict | None:
    path = _marker_path(database_path, message_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _marker_recovery_steps(database_path: Path | str, message_id: str) -> str:
    """How to resolve a draft stuck at 'sending' (#304).

    The marker is left on purpose after any failure that may have reached the
    server (#19 / #227; contract #3's reason). The text states the condition a
    resend needs, not a checklist for establishing it: no check the CLI could
    name proves a mail will never arrive, so the decision stays the reader's.
    """
    return (
        f"Marker file: {_marker_path(database_path, message_id)}. This mail may "
        "already have been delivered to some or all recipients (To, Cc and "
        "Bcc), or a send of this draft may still be running. Resend only if you "
        "are certain that no send of this draft is still running and that no "
        "recipient received it; then delete that marker file and send the draft "
        "again. If any recipient received it, you are unsure, or you abandon "
        "this mail, leave the marker in place and do not resend."
    )


def _sending_hint(database_path: Path | str, message_id: str, status: str) -> str:
    """``". <steps>"`` to append to a message when ``status`` is 'sending', else ''."""
    if status != "sending":
        return ""
    return ". " + _marker_recovery_steps(database_path, message_id)


def _write_marker(
    database_path: Path | str,
    message_id: str,
    draft_id: str,
    status: str,
    refused: list[dict[str, Any]] | None = None,
) -> None:
    path = _marker_path(database_path, message_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    record: dict[str, Any] = {
        "status": status,
        "message_id": message_id,
        "draft_id": draft_id,
    }
    if refused:
        record["refused"] = refused
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)


def _check_tty_authorization(draft_summary: str) -> str:
    # Authorization is accidental-send friction and an audit signal, NOT a security
    # boundary against a local process that imports this module directly.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SendRefused(
            "interactive authorization required: stdin/stdout is not a terminal "
            "(Claude Code's `!` and agent shell tools are not TTYs). Either "
            "(1) after the user has reviewed the draft and said to send it, pass "
            "the fingerprint printed by draft-reply (--fingerprint <sha256>, "
            "send-batch <draft_id>=<sha256>, or fingerprint= in the Python API), "
            "or (2) run the command in a real terminal and type the confirmation."
        )

    print(draft_summary, file=sys.stdout)
    confirmation = input(
        "Type a non-empty confirmation to send (e.g. a recipient email address): "
    ).strip()
    if not confirmation:
        raise SendRefused("explicit authorization required; confirmation was empty")
    return confirmation


def _check_from_invariant(msg: Message, smtp_user: str) -> None:
    # Anti-spoofing invariant inside bes. NOT an SPF/DKIM deliverability guarantee --
    # DKIM/SPF alignment is enforced server-side by the mail server.
    from_header = msg.get("From", "")
    addresses = [
        (display, addr) for display, addr in getaddresses([from_header]) if addr
    ]
    if len(addresses) > 1:
        raise SendRefused(
            "draft has multiple From addresses; not sent. Create a new draft."
        )
    if len(addresses) == 0:
        raise SendRefused("draft has no From address; not sent. Create a new draft.")

    _, from_addr = parseaddr(from_header)
    if from_addr.casefold() != smtp_user.casefold():
        raise SendRefused(
            "anti-spoofing invariant violated inside bes; not sent. Create a new "
            f"draft from the SMTP account. From address {from_addr!r} does not "
            f"match SMTP authentication account {smtp_user!r}. This is NOT an "
            "SPF/DKIM deliverability guarantee."
        )

    if msg.get("Sender") is not None:
        raise SendRefused(
            "draft carries Sender header; rejected by V1 anti-spoofing guard; "
            "not sent. Create a new draft without it."
        )

    for header_name in msg:
        if header_name.lower().startswith("resent-"):
            raise SendRefused(
                f"draft carries {header_name} header; "
                "rejected by V1 anti-spoofing guard; not sent. Create a new "
                "draft without it."
            )


PARTIAL_REFUSAL_WARNING = (
    "SMTP accepted this message for some recipients but refused others at RCPT "
    "time (see 'refused'): the refused recipients did NOT receive it; for the "
    "others the server accepted it, which is not final delivery (it can still "
    "bounce). Do NOT resend the whole draft; send a new draft "
    "to the refused recipients only. The recorded DB recipients are the intended "
    "list, not a delivery guarantee."
)


def _send_reviewed_message(
    smtp: smtplib.SMTP,
    msg: EmailMessage,
    from_addr: str,
    to_addrs: list[str],
) -> dict[str, tuple[int, bytes]]:
    """Put ``msg`` on the wire as the draft bytes minus Bcc, CRLF line endings.

    Replaces ``SMTP.send_message``, which re-serialises with a generator that
    ignores the message's own policy and rewrites body lines starting with
    ``From `` to ``>From `` (#295). The parts of ``send_message`` this call
    site relies on are reproduced: Bcc / Resent-Bcc are dropped from the wire
    copy, non-ASCII envelope addresses need the server's SMTPUTF8 and switch
    serialisation to ``utf8=True`` with ``SMTPUTF8`` / ``BODY=8BITMIME``, and
    the return value is ``sendmail``'s refused-recipients dict. Envelope
    addresses are always passed in, so its header-derived defaults and
    Resent-* block selection are not used.
    """
    smtp.ehlo_or_helo_if_needed()
    wire_msg = copy.copy(msg)
    del wire_msg["Bcc"]
    del wire_msg["Resent-Bcc"]
    mail_options: tuple[str, ...] = ()
    wire_policy = msg.policy.clone(linesep="\r\n")
    try:
        "".join([from_addr, *to_addrs]).encode("ascii")
    except UnicodeEncodeError:
        if not smtp.has_extn("smtputf8"):
            raise smtplib.SMTPNotSupportedError(
                "One or more source or delivery addresses require"
                " internationalized email support, but the server"
                " does not advertise the required SMTPUTF8 capability"
            ) from None
        wire_policy = wire_policy.clone(utf8=True)
        mail_options = ("SMTPUTF8", "BODY=8BITMIME")
    return smtp.sendmail(
        from_addr, to_addrs, wire_msg.as_bytes(policy=wire_policy), mail_options
    )


def _smtp_submit(
    msg: EmailMessage,
    smtp_config: SmtpConfig,
    envelope_recipients: list[str],
) -> tuple[list[dict[str, Any]], str | None]:
    """Submit ``msg``; return ``(refused, close_error)``.

    ``refused`` is the recipients the server refused at RCPT time:
    ``_send_reviewed_message`` (via ``sendmail``) returns normally as long as
    at least one RCPT was accepted; its return value is
    ``{address: (code, reason)}`` for the refused rest (#224). All-refused
    raises ``SMTPRecipientsRefused`` instead.

    ``close_error`` is set when ``_send_reviewed_message`` already returned
    (DATA was accepted) but the closing QUIT / close failed
    (``SMTP.__exit__`` raises on a non-221 QUIT reply, #230). The submission
    stands; only the teardown failed, so the error is returned instead of
    raised.
    """
    ctx = build_smtp_ssl_context()
    refused = None
    try:
        with smtplib.SMTP_SSL(
            smtp_config.host,
            smtp_config.port,
            timeout=_SMTP_TIMEOUT_SECONDS,
            context=ctx,
        ) as smtp:
            smtp.login(smtp_config.user, smtp_config.password)
            refused = _send_reviewed_message(
                smtp, msg, smtp_config.user, envelope_recipients
            )
    except (smtplib.SMTPException, OSError) as exc:
        if refused is None:
            raise
        return _refusal_list(refused), str(exc)
    return _refusal_list(refused), None


def _decode_reason(reason: object) -> str:
    if isinstance(reason, bytes):
        return reason.decode("utf-8", errors="replace")
    return str(reason)


def _refusal_list(refused: object) -> list[dict[str, Any]]:
    if not isinstance(refused, dict):
        return []
    return [
        {"address": address, "code": code, "reason": _decode_reason(reason)}
        for address, (code, reason) in refused.items()
    ]


def _append_sent_copy(
    *,
    env: Mapping[str, str],
    smtp_user: str,
    draft_id: str,
    message_id: str,
    msg: EmailMessage,
) -> None:
    """Best-effort APPEND of a submitted message into the IMAP Sent folder.

    Design Decision 3 (add-mutf7-folder-codec/design.md): visibility in
    Outlook's sent view is a bonus on top of an already-accepted SMTP
    submission, never a condition of it. Any failure here -- including
    serializing ``msg`` itself, absent/incomplete IMAP settings, an IMAP
    account that does not match the sending SMTP account, connection,
    authentication, missing folder, or a server-side rejection -- is caught
    and reported as a stderr warning naming the draft; it MUST NOT propagate,
    and MUST NOT change the send outcome, the marker state, or the exit code.
    This function never raises. Serialization happens here, inside the
    best-effort boundary, rather than by the caller passing pre-serialized
    bytes -- a serialization failure must degrade exactly like any other
    append failure, not propagate after SMTP submission, the DB persist, and
    the marker write have already completed.

    Must be called only AFTER SMTP submission and after the DB persist step
    and the send marker have been finalized to ``sent`` -- never as a
    preflight, and never for a draft whose SMTP submission did not succeed.
    """
    try:
        rfc822 = msg.as_bytes()

        missing = [
            key for key in _REQUIRED_IMAP_APPEND_KEYS if not env.get(key, "").strip()
        ]
        if missing:
            raise ValueError(
                f"missing required IMAP configuration key(s): {', '.join(missing)}"
            )

        imap_user = env["IMAP_USER"].strip()
        if imap_user.casefold() != smtp_user.casefold():
            raise ValueError(
                f"IMAP account {imap_user!r} does not match the sending SMTP "
                f"account {smtp_user!r}; refusing to append a copy of outbound "
                "mail into a mailbox other than the one it was sent from"
            )

        # IMAP_SECURITY is a sync-only exception. Sent-folder APPEND retains
        # its existing implicit-TLS behavior unless separately specified by
        # a future outbound contract.
        append_env = {**env, "IMAP_SECURITY": "tls"}
        client = NativeImapClient(
            append_env,
            timeout=_APPEND_IMAP_TIMEOUT_SECONDS,
        )
        try:
            client.append_message(_SENT_FOLDER_WIRE_NAME, rfc822)
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 -- best-effort by design (Decision 3)
        print(
            f"Warning: could not append draft_id={draft_id} "
            f"(message_id={message_id}) to the IMAP Sent folder "
            f"{_SENT_FOLDER_DISPLAY_NAME!r}: {exc}",
            file=sys.stderr,
        )


@dataclass(slots=True)
class _PreparedDraft:
    """Result of TTY-free draft preparation (load + parse + marker read).

    ``skip_status`` holds the marker status when the send-once marker already
    reports ``sending``/``sent`` (an idempotency skip, not a preparation
    failure); it is ``None`` when the draft is sendable. Preparation failures
    (load/parse/Message-ID/marker-read errors) are raised by
    :func:`_prepare_draft_for_send`, not represented on this dataclass.
    """

    draft_id: str
    msg: EmailMessage
    message_id: str
    summary_line: str
    # Fingerprint of the exact bytes ``msg`` was parsed from, so a verified
    # fingerprint always covers the message that is submitted: ``msg``
    # re-serializes to those bytes, and _send_reviewed_message puts exactly
    # those bytes on the wire minus the Bcc header, with CRLF line endings.
    fingerprint: str
    skip_status: str | None = None


class _SubmitTimeSkip(Exception):
    """Raised by :func:`_submit_prepared_draft` when the send-once marker

    flips to sending/sent between preparation and submission (best-effort
    sequential guard, not an atomic cross-process claim).
    """


class _PostSubmitError(Exception):
    """Raised when SMTP already accepted a message but finalization failed."""

    def __init__(self, message_id: str, reason: str) -> None:
        super().__init__(reason)
        self.message_id = message_id


class _PrepFailure(Exception):
    """Raised by :func:`_prepare_draft_for_send` when preparation fails AFTER

    a valid bracketed Message-ID has already been extracted (e.g. a
    `_read_marker` I/O/decode error). Carries `message_id` so callers can
    surface the known marker identity for audit/recovery even though
    preparation did not complete.
    """

    def __init__(self, message_id: str, reason: str) -> None:
        super().__init__(reason)
        self.message_id = message_id


def _unique_recipients_by_header(
    msg: Message,
) -> dict[str, list[tuple[str | None, str]]]:
    grouped: dict[str, list[tuple[str | None, str]]] = {"To": [], "Cc": [], "Bcc": []}
    seen: set[str] = set()
    for header_name in ("To", "Cc", "Bcc"):
        for display_name, address in getaddresses(msg.get_all(header_name, [])):
            if not address:
                continue
            normalized = address.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            grouped[header_name].append((display_name or None, address))
    return grouped


def _format_recipient_summary(
    recipients: list[tuple[str | None, str]],
) -> str:
    # Display-only rendering: sanitize for the TTY summary, never for the
    # actual SMTP envelope (built separately by _extract_envelope_recipients
    # from the unsanitized parsed message). Deliberately NOT formataddr():
    # it RFC 2047-encodes non-ASCII display names into unreadable
    # =?utf-8?...?= encoded-words and raises UnicodeEncodeError on a
    # non-ASCII address -- both unacceptable for a mailbox's
    # human-facing authorization summary.
    parts = []
    for display_name, address in recipients:
        safe_address = _sanitize_for_terminal(address)
        if display_name:
            parts.append(f"{_sanitize_for_terminal(display_name)} <{safe_address}>")
        else:
            parts.append(safe_address)
    return ", ".join(parts)


def _prepare_draft_for_send(
    draft_id: str,
    database_path: Path | str,
    draft_dir: Path | str | None = None,
) -> _PreparedDraft:
    """TTY-free: load, parse, and marker-check a draft. No SMTP, no TTY prompt.

    Raises:
      FileNotFoundError: if draft .eml does not exist
      SendRefused: unparseable draft or missing/invalid bracketed Message-ID
        (no valid Message-ID known yet at these two failure points)
      _PrepFailure: any failure from the marker read onward -- i.e. once a
        valid Message-ID is already known (e.g. a `_read_marker` I/O/decode
        error). Carries `message_id` for audit/recovery.
    """
    draft_dir_path = (
        Path(draft_dir) if draft_dir is not None else _draft_dir(database_path)
    )
    draft_path = draft_dir_path / f"{draft_id}.eml"
    raw = draft_path.read_bytes()
    msg = message_from_bytes(raw, policy=policy.default)
    if not isinstance(msg, EmailMessage):
        raise SendRefused(
            "draft could not be parsed as an EmailMessage; not sent. Create a "
            "new draft."
        )

    message_id = str(msg["Message-ID"] or "").strip()
    if not message_id.startswith("<") or not message_id.endswith(">"):
        raise SendRefused(
            "draft has no valid bracketed Message-ID; not sent. Create a new draft."
        )

    try:
        marker = _read_marker(database_path, message_id)
        skip_status: str | None = None
        if marker and marker.get("status") in ("sending", "sent"):
            skip_status = marker["status"]

        subject = _sanitize_for_terminal(str(msg["Subject"] or ""))
        # #199: the sending account must be visible at review time, so it is
        # shown here rather than left implicit in --env-file.
        from_pairs = [
            (display_name or None, address)
            for display_name, address in getaddresses([str(msg.get("From", ""))])
            if address
        ]
        from_str = _format_recipient_summary(from_pairs)
        recipients_by_header = _unique_recipients_by_header(msg)
        to_str = _format_recipient_summary(recipients_by_header["To"])
        cc_str = _format_recipient_summary(recipients_by_header["Cc"])
        bcc_str = _format_recipient_summary(recipients_by_header["Bcc"])
        attachment_names = [
            _sanitize_for_terminal(filename)
            for part in msg.iter_attachments()
            if (filename := part.get_filename()) is not None
        ]
        summary_line = (
            f"  draft_id: {_sanitize_for_terminal(draft_id)}\n"
            f"  From: {from_str}\n"
            f"  Subject: {subject}\n"
            f"  To: {to_str}\n"
            f"  Cc: {cc_str}\n"
            f"  Bcc: {bcc_str}\n"
        )
        if attachment_names:
            summary_line += f"  Attachments: {len(attachment_names)}\n"
            summary_line += "".join(f"    - {name}\n" for name in attachment_names)
    except Exception as exc:  # noqa: BLE001 -- message_id is already known here
        raise _PrepFailure(
            message_id,
            "draft preparation failed; not sent. Retry, or create a new draft. "
            f"Cause: {exc}",
        ) from exc

    return _PreparedDraft(
        draft_id=draft_id,
        msg=msg,
        message_id=message_id,
        summary_line=summary_line,
        fingerprint=draft_fingerprint(raw),
        skip_status=skip_status,
    )


def _verify_fingerprint(prepared: _PreparedDraft, expected: str) -> None:
    # Deliberately does not echo the current fingerprint: copying it from the
    # error would let a caller resend a changed draft without re-review.
    if expected.strip().lower() != prepared.fingerprint:
        raise SendRefused(
            "draft fingerprint mismatch: this draft was not sent. Present the "
            "current draft to the user again before sending. This is not the draft "
            "that was reviewed (it changed after review, or the draft_id/fingerprint "
            f"pair is wrong). draft_id={prepared.draft_id}"
        )


def _require_env(env: Mapping[str, str] | None) -> None:
    """Refuse a send whose caller passed no env mapping at all (issue #114).

    Callers that forgot it used to send anyway and skip the Sent-folder copy
    with only a stderr warning, which invites a duplicate resend. A mapping
    that is present but lacks the IMAP keys still sends and warns -- the
    no-IMAP-preflight rule of ``_append_sent_copy`` is unchanged.
    """
    if env is None:
        raise SendRefused(
            "env mapping is required (the parsed --env-file settings, used for "
            "the APPEND into the Sent folder); nothing was sent"
        )


def _submit_prepared_draft(
    prepared: _PreparedDraft,
    database_path: Path | str,
    smtp_config: SmtpConfig,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """TTY-free: invariant -> marker re-check -> 'sending' -> SMTP -> persist -> 'sent'.

    Ordering (must match today's single-send contract exactly):
    1. From anti-spoofing invariant check
    2. Re-read the send-once marker; raise `_SubmitTimeSkip` (no SMTP) if it
       has become sending/sent since preparation
    3. Write 'sending' marker
    4. SMTP submission (private `_smtp_submit`)
    5. persist_message to DB with pst_source='outbound-bes'
    6. Update marker to 'sent'
    7. Best-effort APPEND of the submitted message into the IMAP Sent folder
       (design Decision 3, add-mutf7-folder-codec) -- never raises, and any
       failure is reported only as a stderr warning naming the draft.

    Marker state on failure: unchanged for a failure at/before step 3;
    left at 'sending' for a failure after step 3 (SMTP, DB persist/commit,
    or the final 'sent' marker write). Step 7 runs only once the marker has
    reached 'sent', so a step-3-6 failure never reaches the append.

    Raises:
      SendRefused: invariant violation, or SMTP failure (marker stays 'sending')
      _SubmitTimeSkip: marker flipped to sending/sent since preparation
      _PostSubmitError: SMTP accepted the message, but DB/marker finalization
        failed and the marker intentionally stays 'sending'
    """
    _require_env(env)
    msg = prepared.msg
    message_id = prepared.message_id
    draft_id = prepared.draft_id

    _check_from_invariant(msg, smtp_config.user)

    marker = _read_marker(database_path, message_id)
    if marker and marker.get("status") in ("sending", "sent"):
        steps = _sending_hint(database_path, message_id, marker["status"])
        raise _SubmitTimeSkip(
            f"draft already {marker['status']}; this call sent nothing. Do not "
            "resend; check delivery status first. (skipped at submission: marker "
            f"changed since preparation; message_id={message_id})" + steps
        )

    envelope_recipients = _extract_envelope_recipients(msg)
    _write_marker(database_path, message_id, draft_id, "sending")

    steps = _marker_recovery_steps(database_path, message_id)

    def _connection_lost(exc: Exception) -> SendRefused:
        # The conversation broke off (stall, drop, connect/TLS failure) at a
        # stage we cannot pin down -- possibly after the server accepted DATA.
        # Outcome unknown: marker stays 'sending', no retry (#227).
        print(f"SMTP connection lost or timed out: {exc}", file=sys.stderr)
        return SendRefused(
            "SMTP connection lost or timed out; the server may or may not have "
            "accepted the mail. Verify delivery status before any resend. "
            f"message_id={message_id}; marker left at 'sending', operator "
            f"intervention required; draft_id={draft_id} ({exc}) {steps}"
        )

    try:
        refused, close_error = _smtp_submit(msg, smtp_config, envelope_recipients)
    except smtplib.SMTPServerDisconnected as exc:
        # smtplib wraps a read/write socket timeout after connect into this
        # ("Connection unexpectedly closed: timed out"), not TimeoutError.
        raise _connection_lost(exc) from exc
    except smtplib.SMTPAuthenticationError as exc:
        # login() runs before any MAIL/DATA command, so SMTP never accepted
        # the message: the one failure we can say "not sent" about (#304).
        print(f"SMTP login failed: {exc}", file=sys.stderr)
        raise SendRefused(
            "SMTP login was rejected before any message was submitted; the mail "
            "was not sent. Fix the SMTP credentials, then delete the marker "
            f"file {_marker_path(database_path, message_id)} (left at 'sending') "
            "and send the draft again; nothing was delivered, so no delivery "
            f"check is needed. Server reply: {exc}"
        ) from exc
    except smtplib.SMTPException as exc:
        print(f"SMTP submission failed: {exc}", file=sys.stderr)
        # Marker stays 'sending' -- operator must intervene before resend
        raise SendRefused(
            "SMTP submission failed; the server reported an error, so the mail "
            "was most likely not sent. Verify delivery status before any resend "
            "(marker left at 'sending'; operator intervention required). "
            f"{steps} Server reply: {exc}"
        ) from exc
    except OSError as exc:
        # Connect / TLS-handshake timeout or failure (SMTPException is itself
        # an OSError, so this branch must stay last).
        raise _connection_lost(exc) from exc

    try:
        connection = connect_database(database_path)
        try:
            persist_message(
                connection,
                _normalized_from_draft(
                    msg,
                    draft_id,
                    message_id,
                    database_path=database_path,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        _write_marker(database_path, message_id, draft_id, "sent", refused)
    except Exception as exc:
        raise _PostSubmitError(
            message_id,
            "mail was accepted by SMTP and may already be delivered; "
            "verify delivery status before considering any resend. "
            "Post-submit finalization failed and the marker was intentionally "
            f"left at 'sending'. {steps} Finalization error: {exc}"
            + (
                # Don't lose the partial-refusal evidence with the marker (#224).
                f". SMTP refused these recipients at RCPT time: {refused}"
                if refused
                else ""
            ),
        ) from exc

    _append_sent_copy(
        env=env,
        smtp_user=smtp_config.user,
        draft_id=draft_id,
        message_id=message_id,
        msg=msg,
    )
    result: dict[str, Any] = {"message_id": message_id, "draft_id": draft_id}
    if close_error is not None:
        # #230: DATA was accepted; only the closing QUIT/close failed.
        print(f"SMTP close failed after accept: {close_error}", file=sys.stderr)
        result["close_warning"] = (
            f"SMTP accepted this message (DATA 250) but closing the connection "
            f"failed ({close_error}); the mail WAS sent and is recorded as sent. "
            "Do NOT resend the draft."
        )
    if refused:
        refused_keys = {item["address"].casefold() for item in refused}
        result.update(
            delivery="partial",
            accepted=[
                address
                for address in envelope_recipients
                if address.casefold() not in refused_keys
            ],
            refused=refused,
            warning=PARTIAL_REFUSAL_WARNING,
        )
    return result


def send_draft(
    draft_id: str,
    database_path: Path | str,
    smtp_config: SmtpConfig,
    env: Mapping[str, str],
    *,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Send a draft email. ALL gates are checked before any SMTP submission.

    Ordering:
    1. Load .eml from outbound/drafts/<draft_id>.eml
    2. Parse Message-ID; refuse if invalid/absent
    3. Check resend marker; refuse if sending/sent
    4. Authorization (contract #2): with ``fingerprint``, it must match the
       current draft bytes (no prompt, TTY or not); without it, interactive
       TTY authorization. Accidental-send friction, not a security boundary.
    5. From anti-spoofing invariant check
    6. Write 'sending' marker
    7. SMTP submission (private _smtp_submit)
    8. persist_message to DB with pst_source='outbound-bes'
    9. Update marker to 'sent'
    10. Best-effort APPEND into the IMAP Sent folder (never fails the send)

    ``env`` is the parsed env mapping the caller already read for SMTP
    settings (``--env-file``); it is used only for the best-effort IMAP
    append in step 10. It must be passed explicitly (#114): ``None`` is
    refused before anything else.

    Returns ``{"message_id", "draft_id"}``; when SMTP refused some recipients
    at RCPT time (#224) it also carries ``delivery="partial"``, ``accepted``,
    ``refused`` (``[{address, code, reason}]``) and ``warning`` -- the message
    was sent, so the marker is ``sent`` and the draft must not be resent whole.
    If SMTP refused every recipient, ``SMTPRecipientsRefused`` becomes a
    ``SendRefused`` (marker stays 'sending').

    Raises:
      SendRefused: for a missing env, or any authorization, invariant, or
        duplicate-send failure
      FileNotFoundError: if draft .eml does not exist
      smtplib.SMTPException: on SMTP failure (logged; marker stays 'sending')
    """
    _require_env(env)
    prepared = _prepare_draft_for_send(draft_id, database_path)
    if prepared.skip_status is not None:
        steps = _sending_hint(database_path, prepared.message_id, prepared.skip_status)
        raise SendRefused(
            f"draft already {prepared.skip_status} (message_id={prepared.message_id}); "
            "operator intervention required before resend" + steps
        )

    draft_summary = "Draft to send:\n" + prepared.summary_line
    if fingerprint is None:
        _check_tty_authorization(draft_summary)
    else:
        _verify_fingerprint(prepared, fingerprint)
        print(draft_summary, file=sys.stdout)

    try:
        return _submit_prepared_draft(prepared, database_path, smtp_config, env)
    except _SubmitTimeSkip as exc:
        # The marker changed to sent/sending in the window between
        # authorization and submission. Never return a batch-style
        # "skipped" here -- send_draft's contract is refuse, not skip.
        raise SendRefused(str(exc)) from exc


def send_batch(
    draft_ids: list[str],
    database_path: Path | str,
    smtp_config: SmtpConfig,
    env: Mapping[str, str],
    *,
    fingerprints: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Send an explicitly enumerated list of drafts under one authorization.

    Authorization (contract #2) is either one TTY confirmation for the whole
    batch, or ``fingerprints`` -- one per draft_id, all or none -- in which
    case there is no prompt and each draft is checked on its own: a draft
    whose current bytes do not match is ``failed`` and never submitted, while
    the others still send.

    See openspec/changes/add-send-batch/design.md (Decision 1-3) and
    specs/outbound-send/spec.md for the full contract. Summary:

    1. Reject the batch (raise SendRefused) if draft_ids has duplicates.
    2. Prepare every draft (TTY-free); a preparation failure on one draft is
       recorded as a pre-failed ("failed") result and does not abort the
       batch; an already-sending/sent draft is recorded "skipped".
    3. Verify each prepared draft's fingerprint when ``fingerprints`` is given
       (a mismatch marks that draft "failed"), then build the combined
       pre-send summary. If no draft is sendable, print it and return the
       results now WITHOUT any TTY prompt.
    4. Otherwise, without ``fingerprints``, call `_check_tty_authorization`
       exactly once (it prints the summary it is given); with
       ``fingerprints``, just print the summary -- no prompt.
    5. Submit each sendable draft sequentially, isolating per-draft failure.
       Each successfully sent draft independently attempts a best-effort
       IMAP-Sent-folder append (design Decision 3, add-mutf7-folder-codec);
       an append failure on one draft never affects the others.

    ``env`` is the parsed env mapping the caller already read for SMTP
    settings (``--env-file``); it is used only for the best-effort IMAP
    append. It is required (#114): ``None`` refuses the whole batch before
    any preparation.

    Returns one dict per input draft_id (input order) with keys `draft_id`,
    `outcome` ("sent"|"partial"|"skipped"|"failed"), and `message_id`/`reason`.
    "partial" (#224) = sent, but SMTP refused some recipients: the row also
    carries `accepted`, `refused` and `warning`; never resend the whole draft.
    """
    _require_env(env)
    if len(set(draft_ids)) != len(draft_ids):
        raise SendRefused(
            "duplicate draft_id in send-batch argument list; "
            "whole batch refused before any preparation"
        )
    if fingerprints is not None and set(fingerprints) != set(draft_ids):
        raise SendRefused(
            "fingerprints must be given for every draft_id in the batch and for "
            "no other draft (all or none); whole batch refused before any "
            "preparation"
        )

    order = list(draft_ids)
    prepared_by_id: dict[str, _PreparedDraft] = {}
    # Each failure entry always has "reason"; "message_id" is present only
    # when a valid Message-ID was already known at the point of failure
    # (i.e. a _PrepFailure, not a pre-ID SendRefused/FileNotFoundError/parse
    # failure).
    prep_failures: dict[str, dict[str, str]] = {}
    for draft_id in order:
        try:
            prepared_by_id[draft_id] = _prepare_draft_for_send(draft_id, database_path)
        except _PrepFailure as exc:
            prep_failures[draft_id] = {
                "reason": str(exc),
                "message_id": exc.message_id,
            }
        except Exception as exc:  # noqa: BLE001 -- classify, never abort the batch
            prep_failures[draft_id] = {"reason": str(exc)}

    results: dict[str, dict[str, Any]] = {}
    summary_lines: list[str] = []
    sendable_ids: list[str] = []
    for draft_id in order:
        if draft_id in prep_failures:
            failure = prep_failures[draft_id]
            reason = failure["reason"]
            summary_lines.append(
                f"  draft_id: {_sanitize_for_terminal(draft_id)} "
                f"[FAILED-TO-PREPARE: {_sanitize_for_terminal(reason)}]\n"
                "  From: unavailable\n"
                "  Subject: unavailable\n"
                "  To: unavailable\n"
                "  Cc: unavailable\n"
                "  Bcc: unavailable\n"
            )
            result: dict[str, str] = {
                "draft_id": draft_id,
                "outcome": "failed",
                "reason": reason,
            }
            if "message_id" in failure:
                result["message_id"] = failure["message_id"]
            results[draft_id] = result
            continue

        prepared = prepared_by_id[draft_id]
        if prepared.skip_status is not None:
            summary_lines.append(
                f"  [SKIPPED: already {prepared.skip_status}]\n" + prepared.summary_line
            )
            results[draft_id] = {
                "draft_id": draft_id,
                "outcome": "skipped",
                "message_id": prepared.message_id,
                "reason": f"already {prepared.skip_status}"
                + _sending_hint(
                    database_path, prepared.message_id, prepared.skip_status
                ),
            }
            continue

        if fingerprints is not None:
            try:
                _verify_fingerprint(prepared, fingerprints[draft_id])
            except SendRefused as exc:
                summary_lines.append(
                    "  [FAILED: fingerprint mismatch]\n" + prepared.summary_line
                )
                results[draft_id] = {
                    "draft_id": draft_id,
                    "outcome": "failed",
                    "message_id": prepared.message_id,
                    "reason": str(exc),
                }
                continue

        summary_lines.append(prepared.summary_line)
        sendable_ids.append(draft_id)

    combined_summary = f"Drafts to send (batch of {len(order)}):\n\n" + "\n".join(
        summary_lines
    )

    if not sendable_ids:
        print(combined_summary, file=sys.stdout)
        return [results[draft_id] for draft_id in order]

    if fingerprints is None:
        _check_tty_authorization(combined_summary)
    else:
        print(combined_summary, file=sys.stdout)

    for attempt_index, draft_id in enumerate(sendable_ids):
        if attempt_index:
            _wait_for_batch_interval(BATCH_SEND_INTERVAL_SECONDS)
        prepared = prepared_by_id[draft_id]
        try:
            submit_result = _submit_prepared_draft(
                prepared, database_path, smtp_config, env
            )
        except _SubmitTimeSkip as exc:
            results[draft_id] = {
                "draft_id": draft_id,
                "outcome": "skipped",
                "message_id": prepared.message_id,
                "reason": str(exc),
            }
        except _PostSubmitError as exc:
            results[draft_id] = {
                "draft_id": draft_id,
                "outcome": "failed",
                "message_id": exc.message_id,
                "reason": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 -- isolate per-draft failure
            results[draft_id] = {
                "draft_id": draft_id,
                "outcome": "failed",
                "message_id": prepared.message_id,
                "reason": str(exc),
            }
        else:
            sent_row: dict[str, Any] = {
                "draft_id": draft_id,
                "outcome": "sent",
                "message_id": submit_result["message_id"],
            }
            if "close_warning" in submit_result:
                sent_row["close_warning"] = submit_result["close_warning"]
            if "refused" in submit_result:
                sent_row["outcome"] = "partial"
                for key in ("accepted", "refused", "warning"):
                    sent_row[key] = submit_result[key]
            results[draft_id] = sent_row

    return [results[draft_id] for draft_id in order]


def _extract_envelope_recipients(msg: Message) -> list[str]:
    recipients_by_header = _unique_recipients_by_header(msg)
    recipients: list[str] = []
    for header_name in ("To", "Cc", "Bcc"):
        recipients.extend(
            address for _display_name, address in recipients_by_header[header_name]
        )
    return recipients


def _extract_draft_attachments(
    msg: EmailMessage,
    attachment_root: Path,
    draft_id: str,
    message_id: str,
) -> tuple[NormalizedAttachment, ...]:
    attachments: list[NormalizedAttachment] = []
    try:
        parts = list(msg.iter_attachments())
    except Exception as exc:  # noqa: BLE001 -- best effort post-submit bookkeeping
        print(
            f"Warning: could not enumerate attachments for draft_id={draft_id} "
            f"(message_id={message_id}): {exc}",
            file=sys.stderr,
        )
        return ()

    for index, part in enumerate(parts, start=1):
        filename = (part.get_filename() or "").strip(
            " \"'\t\r\n"
        ) or f"attachment-{index}"
        mime_type = part.get_content_type()
        try:
            if mime_type == "message/rfc822":
                payload = part.get_payload(0).as_bytes()
            else:
                payload = part.get_payload(decode=True)
            if payload is None:
                raise ValueError("attachment payload could not be decoded")
            record = write_attachment(
                payload=payload,
                original_name=filename,
                mime_type=mime_type,
                store_root=attachment_root,
            )
            attachments.append(
                NormalizedAttachment(
                    attachment_name=filename,
                    content_hash=record.content_hash,
                    size_bytes=record.size_bytes,
                    mime_type=record.mime_type,
                    storage_path=record.storage_path,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- best effort post-submit bookkeeping
            print(
                f"Warning: could not record attachment {filename!r} "
                f"for draft_id={draft_id} (message_id={message_id}): {exc}",
                file=sys.stderr,
            )
    return tuple(attachments)


def _normalized_from_draft(
    msg: EmailMessage,
    draft_id: str,
    message_id: str,
    database_path: Path | str,
) -> NormalizedMessage:
    attachment_root = resolve_store_root(
        Path(database_path),
        env_override=os.environ.get("MAILATTIC_ATTACHMENT_ROOT"),
    )
    attachments = _extract_draft_attachments(msg, attachment_root, draft_id, message_id)
    from_display, from_addr = parseaddr(str(msg.get("From", "")))
    sent_at = (
        _parse_message_datetime(str(msg["Date"]))
        if msg["Date"]
        else datetime.now(tz=UTC)
    )
    return NormalizedMessage(
        subject=str(msg["Subject"] or ""),
        sender=MailboxParticipant(
            display_name=from_display or None,
            email=from_addr or None,
        ),
        to_recipients=_participants_from_headers(msg, "To"),
        cc_recipients=_participants_from_headers(msg, "Cc"),
        bcc_recipients=_participants_from_headers(msg, "Bcc"),
        sent_at=sent_at,
        received_at=sent_at,
        body_text=_extract_body_text(msg),
        attachments=attachments,
        message_id=message_id,
        in_reply_to=str(msg["In-Reply-To"]) if msg["In-Reply-To"] else None,
        references=str(msg["References"]) if msg["References"] else None,
        pst_source="outbound-bes",
        folder_path="Sent",
        logical_locator=f"outbound:{draft_id}",
        source_input_mode="outbound",
        source_requested_from="bes:send-draft",
    )


def _participants_from_headers(
    msg: Message,
    header_name: str,
) -> tuple[MailboxParticipant, ...]:
    return tuple(
        MailboxParticipant(display_name=display_name or None, email=address)
        for display_name, address in getaddresses(msg.get_all(header_name, []))
        if address
    )


def _parse_message_datetime(value: str) -> datetime:
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _extract_body_text(msg: EmailMessage) -> str:
    # get_body handles multipart/alternative by returning the text/plain sub-part.
    body = msg.get_body(preferencelist=("plain",))
    if body is not None:
        content = body.get_content()
        return content if isinstance(content, str) else str(content)

    # Fallback: walk parts to find text/plain (for non-standard multipart layouts
    # where get_body returns None).  Uses get_content() for proper transfer-encoding
    # decoding rather than get_payload() which returns raw encoded bytes.
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.is_multipart():
            try:
                content = part.get_content()  # type: ignore[union-attr]
                return content if isinstance(content, str) else str(content)
            except Exception:  # noqa: BLE001
                pass

    payload = msg.get_payload()
    if isinstance(payload, str):
        return payload
    return "" if payload is None else str(payload)


# ---------------------------------------------------------------------------
# list-drafts: enumerate pending (unsent) drafts
# ---------------------------------------------------------------------------


def list_drafts(database_path: Path | str) -> list[dict[str, str]]:
    """Return a list of pending (unsent) draft descriptors.

    Each entry is a dict with keys:
      draft_id    - filename stem (UUID)
      recipients  - comma-joined To + Cc addresses from the .eml
      subject     - Subject header value
      created_at  - ISO-8601 file mtime (UTC) used as creation proxy

    A draft whose marker says ``sending`` (an earlier send failed; #19 / #227)
    additionally carries ``status="sending"``, ``marker_path`` and
    ``next_step``; drafts without a marker carry only the four keys above.

    A draft is PENDING when its Message-ID header has no ``sent`` marker.
    Drafts with a ``sent`` marker are retained on disk but excluded from
    this list.  Malformed .eml files (no Message-ID) are also excluded.
    """
    draft_dir = _draft_dir(database_path)
    if not draft_dir.exists():
        return []

    results: list[dict[str, str]] = []
    for eml_path in sorted(draft_dir.glob("*.eml")):
        draft_id = eml_path.stem
        try:
            raw = eml_path.read_bytes()
            msg = message_from_bytes(raw, policy=policy.default)
        except Exception:  # noqa: BLE001
            continue

        message_id = str(msg.get("Message-ID", "")).strip()
        if not message_id:
            continue

        marker = _read_marker(database_path, message_id)
        if marker and marker.get("status") == "sent":
            continue

        # Derive recipients: To + Cc combined
        to_addresses = [
            addr for _dn, addr in getaddresses(msg.get_all("To", [])) if addr
        ]
        cc_addresses = [
            addr for _dn, addr in getaddresses(msg.get_all("Cc", [])) if addr
        ]
        recipient_str = ", ".join(to_addresses + cc_addresses)

        subject = str(msg.get("Subject", ""))

        # Use file mtime as creation time proxy (creation time is not reliably
        # available cross-platform; mtime set at draft-write time is stable)
        mtime_ts = eml_path.stat().st_mtime
        created_at = datetime.fromtimestamp(mtime_ts, tz=UTC).isoformat()

        entry: dict[str, str] = {
            "draft_id": draft_id,
            "recipients": recipient_str,
            "subject": subject,
            "created_at": created_at,
        }
        if marker and marker.get("status") == "sending":
            # #304: a failed send leaves the draft here for good; say so, and
            # where the marker is. Keys exist only for these drafts.
            entry["status"] = "sending"
            entry["marker_path"] = str(_marker_path(database_path, message_id))
            entry["next_step"] = _marker_recovery_steps(database_path, message_id)
        results.append(entry)

    return results

"""Every failure ``mail_send`` can return keeps its outcome and next step (#292).

``mail_send`` wraps any failure as ``Failed to send mail: {detail}`` with the
detail capped at ``ERROR_ECHO_CAP_CHARS``. The cap stays; the sentences we
author must therefore put the outcome ("not sent" / "may or may not have been
sent" / "may already be delivered") and the next step BEFORE any
variable-length value. Each scenario below drives the real MCP ``mail_send``
(same wrapper, same cap) with realistic-maximum interpolated values: a long
bracketed Message-ID, a long draft id, 300-character server replies, and
64-character addresses. No mail is sent: SMTP is an in-process double.
"""

from __future__ import annotations

import asyncio
import re
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import mcp.types as mcp_types
import pytest
from mcp import Client

from mailattic.mcp_delivery import ERROR_ECHO_CAP_CHARS
from mailattic.mcp_server import create_server
from mailattic.models import MailboxParticipant, NormalizedMessage
from mailattic.outbound import (
    ENV_VAR_SMTP_ENV_FILE,
    _marker_path,
    _write_marker,
    draft_fingerprint,
)
from mailattic.storage import connect_database, initialize_database, persist_message

SMTP_USER = "sean3808@mailattic.test"
LONG = "z" * 300
LONG_ADDR = "a" * 50 + "@" + "b" * 20 + ".example.com"
LONG_MESSAGE_ID = "<" + "m" * 40 + "@mailattic.test>"
FRAMEWORK_PREFIX = "Error executing tool mail_send: "
FAILED_PREFIX = "Failed to send mail: "

NOT_SENT = r"(?i)\bnot sent\b|nothing was sent"
MAYBE_SENT = r"may or may not have accepted"
MAYBE_DELIVERED = r"may already be delivered"


@dataclass
class Ctx:
    tmp_path: Path
    db_path: Path
    env_file: Path
    server: object
    smtp: MagicMock
    draft_id: str
    fingerprint: str
    message_id: str
    monkeypatch: pytest.MonkeyPatch

    @property
    def draft_path(self) -> Path:
        return self.tmp_path / "outbound" / "mcp-drafts" / f"{self.draft_id}.eml"

    def rewrite_draft(self, edit: Callable[[bytes], bytes], new_id: str | None = None):
        """Edit the draft bytes (and optionally its file name); refresh fingerprint."""
        raw = edit(self.draft_path.read_bytes())
        if new_id is not None:
            self.draft_path.unlink()
            self.draft_id = new_id
        self.draft_path.write_bytes(raw)
        self.fingerprint = draft_fingerprint(raw)
        match = re.search(rb"(?mi)^Message-ID:\s*([^\r\n]*)", raw)
        self.message_id = match.group(1).decode() if match else ""


def _call(server: object, name: str, args: dict[str, object]):
    async def go() -> mcp_types.CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(name, args)

    return asyncio.run(go())


def _env_text(user: str = SMTP_USER) -> str:
    return (
        "SMTP_HOST=smtp.example.com\nSMTP_PORT=465\n"
        f"SMTP_USER={user}\nSMTP_PASSWORD=secret\n"
        f"IMAP_HOST=imap.example.com\nIMAP_PORT=993\nIMAP_USER={user}\n"
        "IMAP_PASSWORD=secret\n"
    )


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ctx:
    db_path = initialize_database(tmp_path / "test.db")
    conn = connect_database(db_path)
    try:
        persist_message(
            conn,
            NormalizedMessage(
                subject="Original",
                sender=MailboxParticipant(display_name="Ext", email="ext@example.com"),
                to_recipients=(MailboxParticipant(display_name="S", email=SMTP_USER),),
                cc_recipients=(),
                bcc_recipients=(),
                sent_at=datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC),
                received_at=datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC),
                body_text="Hello.",
                attachments=(),
                message_id="<orig@mailattic.test>",
                in_reply_to=None,
                references=None,
                pst_source="pst:test",
                folder_path="Inbox",
                logical_locator="test:1",
                source_input_mode="test",
                source_requested_from="test",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    env_file = tmp_path / "smtp.env"
    env_file.write_text(_env_text(), encoding="utf-8")
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(env_file))
    smtp = MagicMock()
    smtp_ssl = MagicMock()
    smtp_ssl.return_value.__enter__.return_value = smtp
    monkeypatch.setattr("mailattic.outbound.smtplib.SMTP_SSL", smtp_ssl)
    server = create_server(database_path=db_path)
    drafted = _call(
        server,
        "mail_draft_reply",
        {"message_id": "<orig@mailattic.test>", "body_text": "Body"},
    ).structured_content
    assert drafted is not None
    return Ctx(
        tmp_path,
        db_path,
        env_file,
        server,
        smtp,
        drafted["draft_id"],
        drafted["fingerprint"],
        drafted["message_id"],
        monkeypatch,
    )


def _long_ids(c: Ctx) -> None:
    """Realistic-maximum draft id and bracketed Message-ID."""
    c.rewrite_draft(
        lambda raw: re.sub(
            rb"(?mi)^Message-ID:[^\r\n]*",
            b"Message-ID: " + LONG_MESSAGE_ID.encode(),
            raw,
        ),
        new_id="reply-" + "d" * 70,
    )


# --- scenarios: each returns (mail_send args override or None) -------------------


def fingerprint_mismatch(c: Ctx):
    _long_ids(c)
    return {"fingerprint": "0" * 64}


def connection_lost_disconnected(c: Ctx):
    _long_ids(c)
    c.smtp.sendmail.side_effect = smtplib.SMTPServerDisconnected(LONG)


def connection_lost_os_error(c: Ctx):
    _long_ids(c)
    c.smtp.login.side_effect = TimeoutError(LONG)


def smtp_rejected(c: Ctx):
    _long_ids(c)
    c.smtp.sendmail.side_effect = smtplib.SMTPDataError(554, LONG.encode())


def smtp_all_recipients_refused(c: Ctx):
    _long_ids(c)
    c.smtp.sendmail.side_effect = smtplib.SMTPRecipientsRefused(
        {LONG_ADDR: (550, LONG.encode())}
    )


def post_submit_failure(c: Ctx):
    _long_ids(c)
    c.smtp.sendmail.return_value = {}

    def boom(*_a, **_k):
        raise RuntimeError(LONG)

    c.monkeypatch.setattr("mailattic.outbound.persist_message", boom)


def post_submit_failure_with_refusals(c: Ctx):
    _long_ids(c)
    c.smtp.sendmail.return_value = {LONG_ADDR: (550, LONG.encode())}

    def boom(*_a, **_k):
        raise RuntimeError(LONG)

    c.monkeypatch.setattr("mailattic.outbound.persist_message", boom)


def already_sent_marker(c: Ctx):
    _long_ids(c)
    _write_marker(c.db_path, c.message_id, c.draft_id, "sent")


def already_sending_marker(c: Ctx):
    _long_ids(c)
    _write_marker(c.db_path, c.message_id, c.draft_id, "sending")


def from_mismatch(c: Ctx):
    _long_ids(c)
    c.rewrite_draft(
        lambda raw: re.sub(
            rb"(?mi)^From:[^\r\n]*", b"From: " + LONG_ADDR.encode(), raw, count=1
        )
    )
    c.env_file.write_text(_env_text("u" * 50 + "@mailattic.test"), encoding="utf-8")


def multiple_from(c: Ctx):
    c.rewrite_draft(
        lambda raw: re.sub(
            rb"(?mi)^From:[^\r\n]*",
            b"From: a@example.com, b@example.com",
            raw,
            count=1,
        )
    )


def no_from(c: Ctx):
    c.rewrite_draft(lambda raw: re.sub(rb"(?mi)^From:[^\r\n]*\r?\n", b"", raw, count=1))


def sender_header(c: Ctx):
    c.rewrite_draft(lambda raw: b"Sender: x@example.com\n" + raw)


def resent_header(c: Ctx):
    c.rewrite_draft(lambda raw: b"Resent-From: x@example.com\n" + raw)


def no_bracketed_message_id(c: Ctx):
    c.rewrite_draft(
        lambda raw: re.sub(rb"(?mi)^Message-ID:[^\r\n]*", b"Message-ID: bare-id", raw)
    )


def marker_unreadable(c: Ctx):
    _long_ids(c)

    def boom(*_a, **_k):
        raise OSError(LONG)

    c.monkeypatch.setattr("mailattic.outbound._read_marker", boom)


def draft_missing(c: Ctx):
    return {"draft_id": "no-such-draft-" + "q" * 100}


def invalid_draft_id(c: Ctx):
    return {"draft_id": "sub/" + "d" * 250}


def smtp_env_unset(c: Ctx):
    c.monkeypatch.delenv(ENV_VAR_SMTP_ENV_FILE)


def smtp_env_file_missing(c: Ctx):
    missing = c.tmp_path / ("p" * 150) / "gone.env"
    c.monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(missing))


def smtp_env_malformed(c: Ctx):
    bad = c.tmp_path / ("m" * 60) / "bad.env"
    bad.parent.mkdir()
    bad.write_text("NOT_SMTP=1\n", encoding="utf-8")
    c.monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(bad))


# name -> (setup, outcome regex, next-step regex)
SCENARIOS: dict[str, tuple[Callable, str, str]] = {
    "fingerprint_mismatch": (
        fingerprint_mismatch,
        NOT_SENT,
        r"Present the current draft to the user again",
    ),
    "connection_lost_disconnected": (
        connection_lost_disconnected,
        MAYBE_SENT,
        r"(?i)verify delivery status before any resend",
    ),
    "connection_lost_os_error": (
        connection_lost_os_error,
        MAYBE_SENT,
        r"(?i)verify delivery status before any resend",
    ),
    "smtp_rejected": (
        smtp_rejected,
        r"(?i)most likely not sent",
        r"(?i)verify delivery status before any resend",
    ),
    "smtp_all_recipients_refused": (
        smtp_all_recipients_refused,
        r"(?i)most likely not sent",
        r"(?i)verify delivery status before any resend",
    ),
    "post_submit_failure": (
        post_submit_failure,
        MAYBE_DELIVERED,
        r"verify delivery status before considering any resend",
    ),
    "post_submit_failure_with_refusals": (
        post_submit_failure_with_refusals,
        MAYBE_DELIVERED,
        r"verify delivery status before considering any resend",
    ),
    "already_sent_marker": (
        already_sent_marker,
        r"already sent",
        r"(?i)do not resend",
    ),
    "already_sending_marker": (
        already_sending_marker,
        r"already sending",
        r"(?i)do not resend",
    ),
    "from_mismatch": (from_mismatch, NOT_SENT, r"(?i)create a new draft"),
    "multiple_from": (multiple_from, NOT_SENT, r"(?i)create a new draft"),
    "no_from": (no_from, NOT_SENT, r"(?i)create a new draft"),
    "sender_header": (sender_header, NOT_SENT, r"(?i)create a new draft"),
    "resent_header": (resent_header, NOT_SENT, r"(?i)create a new draft"),
    "no_bracketed_message_id": (
        no_bracketed_message_id,
        NOT_SENT,
        r"(?i)create a new draft",
    ),
    "marker_unreadable": (marker_unreadable, NOT_SENT, r"(?i)retry"),
    "draft_missing": (draft_missing, NOT_SENT, r"mail_draft_reply"),
    "invalid_draft_id": (invalid_draft_id, NOT_SENT, r"mail_draft_reply"),
    "smtp_env_unset": (smtp_env_unset, NOT_SENT, r"(?i)operator"),
    "smtp_env_file_missing": (smtp_env_file_missing, NOT_SENT, r"(?i)operator"),
    "smtp_env_malformed": (smtp_env_malformed, NOT_SENT, r"(?i)operator"),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_mail_send_failure_keeps_outcome_and_next_step_under_the_cap(
    name: str, ctx: Ctx
) -> None:
    setup, outcome_re, next_re = SCENARIOS[name]
    override = setup(ctx) or {}
    args = {"draft_id": ctx.draft_id, "fingerprint": ctx.fingerprint, **override}

    res = _call(ctx.server, "mail_send", args)

    assert res.is_error
    text = res.content[0].text
    assert text.startswith(FRAMEWORK_PREFIX)
    detail = text[len(FRAMEWORK_PREFIX) :].removeprefix(FAILED_PREFIX)
    # Cap is applied to the detail; outcome and next step must sit inside it,
    # before any elision marker the variable-length tail may have triggered.
    head = detail.split("...(truncated", 1)[0]
    assert re.search(outcome_re, head), f"outcome clause lost: {text!r}"
    assert re.search(next_re, head), f"next-step clause lost: {text!r}"
    if name in _NO_SMTP:
        ctx.smtp.sendmail.assert_not_called()


_NO_SMTP = {
    "fingerprint_mismatch",
    "already_sent_marker",
    "already_sending_marker",
    "from_mismatch",
    "multiple_from",
    "no_from",
    "sender_header",
    "resent_header",
    "no_bracketed_message_id",
    "marker_unreadable",
    "draft_missing",
    "invalid_draft_id",
    "smtp_env_unset",
    "smtp_env_file_missing",
    "smtp_env_malformed",
}


def test_cap_and_marker_files_unchanged_by_scenarios(ctx: Ctx) -> None:
    """The cap is still the deliberate static bound, and a refused fingerprint
    still writes no marker (this ticket changes message text only)."""
    assert ERROR_ECHO_CAP_CHARS == 200
    fingerprint_mismatch(ctx)
    _call(
        ctx.server,
        "mail_send",
        {"draft_id": ctx.draft_id, "fingerprint": "0" * 64},
    )
    assert not _marker_path(ctx.db_path, ctx.message_id).exists()

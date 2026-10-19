"""Regression tests for send tool error message wording and truncation (#263).

Verifies that refusal reason sentences in mcp_writer error messages survive
truncation under ERROR_ECHO_CAP_CHARS (200 chars) even when paths or values
are long.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import mcp.types as mcp_types
import pytest
from mcp import Client

from mailattic.identity import add_self_address
from mailattic.mcp_delivery import ERROR_ECHO_CAP_CHARS
from mailattic.mcp_server import create_server
from mailattic.models import MailboxParticipant, NormalizedMessage
from mailattic.outbound import ENV_VAR_SMTP_ENV_FILE
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

SMTP_USER = "sean3808@mailattic.test"


def _call_tool(
    server: object, name: str, arguments: dict[str, object]
) -> mcp_types.CallToolResult:
    async def _call() -> mcp_types.CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(_call())


def _seed_message(db_path: Path, message_id: str = "<test-msg@mailattic.test>") -> str:
    conn = connect_database(db_path)
    try:
        # These tests draft with unreadable SMTP settings, so the From comes
        # from the one declared self address (#268).
        add_self_address(conn, SMTP_USER)
        norm = NormalizedMessage(
            subject="Test Subject",
            sender=MailboxParticipant(
                display_name="External Sender", email="external@example.com"
            ),
            to_recipients=(MailboxParticipant(display_name="Sean", email=SMTP_USER),),
            cc_recipients=(),
            bcc_recipients=(),
            sent_at=datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC),
            received_at=datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC),
            body_text="Test message body.",
            attachments=(),
            message_id=message_id,
            in_reply_to=None,
            references=None,
            pst_source="pst:test",
            folder_path="Inbox",
            logical_locator="test:1",
            source_input_mode="test",
            source_requested_from="test",
        )
        persist_message(conn, norm)
        conn.commit()
    finally:
        conn.close()
    return message_id


def _draft_reply_and_send(server: object, msg_id: str) -> mcp_types.CallToolResult:
    draft_res = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": msg_id, "body_text": "Draft body"},
    )
    draft_id = draft_res.structured_content["draft_id"]
    fp = draft_res.structured_content["fingerprint"]
    return _call_tool(
        server,
        "mail_send",
        {"draft_id": draft_id, "fingerprint": fp},
    )


def test_smtp_env_missing_error_preserves_reason_with_long_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason sentence 'does not exist or is unreadable' MUST survive even

    when the non-existent SMTP settings file path is long enough to trigger the
    200-char echo truncation cap (#263).
    """
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)

    # Build a path inside tmp_path that exceeds ERROR_ECHO_CAP_CHARS (200)
    # while staying safely within Windows MAX_PATH (260).
    pad_len = max(20, 215 - len(str(tmp_path)) - len(r"\does_not_exist.env"))
    long_non_existent = tmp_path / ("p" * pad_len) / "does_not_exist.env"

    assert len(str(long_non_existent)) >= ERROR_ECHO_CAP_CHARS, (
        f"Path {long_non_existent} ({len(str(long_non_existent))} chars) "
        f"must exceed {ERROR_ECHO_CAP_CHARS} chars"
    )

    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(long_non_existent))

    server = create_server(database_path=db_path)
    send_res = _draft_reply_and_send(server, msg_id)

    assert send_res.is_error
    err_text = send_res.content[0].text

    # The echo must have been truncated due to the long path...
    assert "(truncated" in err_text

    # ...yet the user-needed reason sentence MUST be intact!
    assert "does not exist or is unreadable" in err_text


def test_smtp_env_malformed_error_preserves_reason_with_long_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason sentence and exception detail MUST survive even when the

    malformed SMTP settings file path is long enough to trigger the 200-char
    echo truncation cap (#263).
    """
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = _seed_message(db_path)

    pad_len = max(20, 215 - len(str(tmp_path)) - len(r"\malformed.env"))
    long_dir = tmp_path / ("m" * pad_len)
    long_dir.mkdir(parents=True, exist_ok=True)
    long_malformed = long_dir / "malformed.env"
    long_malformed.write_text("INVALID_ENV_WITHOUT_REQUIRED_KEYS=1\n", encoding="utf-8")

    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(long_malformed))

    server = create_server(database_path=db_path)
    send_res = _draft_reply_and_send(server, msg_id)

    assert send_res.is_error
    err_text = send_res.content[0].text
    assert "(truncated" in err_text
    assert "Failed to load SMTP settings" in err_text
    assert "Missing required SMTP configuration key" in err_text


def test_invalid_draft_id_bare_filename_preserves_reason_with_long_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason sentence 'must be a bare filename without path components'

    MUST survive even when draft_id is long enough to trigger truncation (#263).
    """
    db_path = initialize_database(tmp_path / "test.db")
    env_file = tmp_path / "smtp.env"
    env_file.write_text("SMTP_HOST=localhost\n", encoding="utf-8")
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(env_file))

    server = create_server(database_path=db_path)
    long_draft_id = "sub/" + "d" * 250

    send_res = _call_tool(
        server,
        "mail_send",
        {"draft_id": long_draft_id, "fingerprint": "a" * 64},
    )

    assert send_res.is_error
    err_text = send_res.content[0].text
    assert "(truncated" in err_text
    assert "must be a bare filename without path components" in err_text

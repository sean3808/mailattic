"""mail_draft_reply refusals that must reach CoWork intact (#209).

The MCP error path caps every dynamic fragment at ERROR_ECHO_CAP_CHARS
(mcp_delivery.py). Our own fixed refusal text travels through that same
capped fragment, so it has to fit under the cap or its remedy -- the part
telling the caller what to do instead -- is cut off.
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
from mailattic.outbound import ARCHIVE_ANCHOR_REFUSAL, ENV_VAR_SMTP_ENV_FILE
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

ANCHOR_MESSAGE_ID = "<archive-anchor@mailattic.test>"


def _seed_archive_anchor(db_path: Path) -> None:
    connection = connect_database(db_path)
    try:
        # No SMTP settings here: the From is the one declared self address.
        add_self_address(connection, "sean3808@mailattic.test")
        persist_message(
            connection,
            NormalizedMessage(
                subject="Sent earlier",
                sender=MailboxParticipant(
                    display_name="Sean", email="sean3808@mailattic.test"
                ),
                to_recipients=(
                    MailboxParticipant(display_name=None, email="client@example.com"),
                ),
                cc_recipients=(),
                bcc_recipients=(),
                sent_at=datetime(2026, 9, 15, 1, 24, tzinfo=UTC),
                received_at=None,
                body_text="outbound body",
                attachments=(),
                message_id=ANCHOR_MESSAGE_ID,
                in_reply_to=None,
                references=None,
                pst_source="archive:mail-archive",
                folder_path="archive",
                logical_locator="archive:1",
                source_input_mode="archive",
                source_requested_from="archive",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _call_tool(
    server, name: str, arguments: dict[str, object]
) -> mcp_types.CallToolResult:
    async def _call() -> mcp_types.CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(_call())


def test_archive_anchor_refusal_reaches_mcp_caller_with_remedy_intact(
    tmp_path: Path,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _seed_archive_anchor(db_path)
    server = create_server(database_path=db_path)

    result = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": ANCHOR_MESSAGE_ID, "body_text": "reply"},
    )

    assert result.is_error
    text = result.content[0].text
    assert ARCHIVE_ANCHOR_REFUSAL in text
    assert "(truncated" not in text
    assert not (tmp_path / "outbound" / "mcp-drafts").exists()


def test_no_sender_address_refusal_reaches_mcp_caller_with_remedy_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#268: no SMTP settings and no declared self address. The refusal has
    to tell CoWork both things the operator can set, uncut."""
    monkeypatch.delenv(ENV_VAR_SMTP_ENV_FILE, raising=False)
    db_path = initialize_database(tmp_path / "test.db")
    server = create_server(database_path=db_path)

    result = _call_tool(
        server,
        "mail_draft_reply",
        {"message_id": ANCHOR_MESSAGE_ID, "body_text": "reply"},
    )

    assert result.is_error
    text = result.content[0].text
    assert "SMTP_USER" in text
    assert "bes identity add <email>" in text
    assert "(truncated" not in text
    assert not (tmp_path / "outbound" / "mcp-drafts").exists()


def test_archive_anchor_refusal_fits_under_the_mcp_error_echo_cap() -> None:
    # The MCP layer delivers this text as a capped fragment; growing it past
    # the cap silently cuts the remedy again. Shorten the text, don't raise
    # the cap (the cap is what bounds error results by construction).
    assert len(ARCHIVE_ANCHOR_REFUSAL) <= ERROR_ECHO_CAP_CHARS

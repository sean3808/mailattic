"""Tests for issue #53: `mail_original_export` MCP tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mailattic.mcp_server import create_server
from mailattic.models import MaildirSource, PstSource, build_normalized_message
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)
from tests.test_original_export import (
    FakeCollection,
    FakeImapClient,
    FakeImapConnection,
    FakeOutlookFolder,
    FakeOutlookItem,
    FakeOutlookNamespace,
    FakeOutlookReader,
    FakeOutlookStore,
)


def _seed_db(tmp_path: Path) -> tuple[Path, int, int]:
    db_path = initialize_database(tmp_path / "mcp_mail.db")
    pst_source = PstSource(
        path=Path("C:/mail/archive_2024.pst"),
        input_mode="file",
        requested_from=Path("C:/mail/archive_2024.pst"),
    )
    pst_msg = build_normalized_message(
        {
            "subject": "Table Quotation PST",
            "sender": {"email": "vendor@example.com", "name": "Vendor"},
            "to": [{"email": "sean3808@mailattic.test"}],
            "sent_at": "2026-05-10T12:00:00+08:00",
            "body_text": "PST body text",
            "message_id": "<pst_quote@example.com>",
        },
        source=pst_source,
        folder_path="Inbox",
        logical_locator="entry-pst-1",
    )

    imap_source = MaildirSource(
        path=tmp_path / "maildir",
        account_email="sean3808@mailattic.test",
        account_name="sean3808",
        folder_name="INBOX",
        requested_from=tmp_path / "maildir",
    )
    imap_msg = build_normalized_message(
        {
            "subject": "Recent Invoice IMAP",
            "sender": {"email": "client@example.com", "name": "Client"},
            "to": [{"email": "sean3808@mailattic.test"}],
            "sent_at": "2026-06-01T15:00:00+08:00",
            "body_text": "IMAP body text",
            "message_id": "<imap_inv@example.com>",
        },
        source=imap_source,
        folder_path="INBOX",
        logical_locator="INBOX/cur/uid-77.eml",
    )

    conn = connect_database(db_path)
    try:
        r1 = persist_message(conn, pst_msg)
        r2 = persist_message(conn, imap_msg)
        conn.commit()
        pst_id = r1.stored_message_id
        imap_id = r2.stored_message_id
    finally:
        conn.close()
    return db_path, pst_id, imap_id


def test_mcp_original_export_tool_is_registered(tmp_path: Path) -> None:
    db_path, _, _ = _seed_db(tmp_path)
    server = create_server(database_path=db_path)
    tools = asyncio.run(server.list_tools())
    tool_names = [tool.name for tool in tools]
    assert "mail_original_export" in tool_names


def test_mcp_original_export_pst_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, pst_id, _ = _seed_db(tmp_path)
    dest_dir = tmp_path / "host_exports"

    fake_item = FakeOutlookItem(
        entry_id="entry-pst-1",
        subject="Table Quotation PST",
        message_id="<pst_quote@example.com>",
    )
    inbox = FakeOutlookFolder("Inbox", [fake_item])
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    store = FakeOutlookStore("C:\\mail\\archive_2024.pst", root)
    namespace = FakeOutlookNamespace([store], items_by_id={"entry-pst-1": fake_item})
    reader = FakeOutlookReader(namespace)

    # Monkeypatch Outlook reader creation in mcp_writer / original_export
    from mailattic import original_export

    monkeypatch.setattr(original_export, "_load_default_outlook_reader", lambda: reader)

    server = create_server(database_path=db_path)
    result = asyncio.run(
        server.call_tool(
            "mail_original_export",
            {"message_id": pst_id, "dest_dir": str(dest_dir)},
        )
    )

    assert not result.is_error
    structured = result.structured_content
    assert structured is not None
    assert structured["status"] == "exported"
    assert structured["format"] == "msg"
    assert structured["source_type"] == "pst"
    assert structured["dest_path"] is not None
    dest_path = Path(str(structured["dest_path"]))
    assert dest_path.exists()
    assert dest_path.suffix.lower() == ".msg"


def test_mcp_original_export_imap_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _, imap_id = _seed_db(tmp_path)
    dest_dir = tmp_path / "host_exports"

    raw_rfc822 = (
        b"From: Client <client@example.com>\r\n"
        b"To: sean3808@mailattic.test\r\n"
        b"Subject: Recent Invoice IMAP\r\n"
        b"Message-ID: <imap_inv@example.com>\r\n"
        b"Date: Mon, 01 Jun 2026 15:00:00 +0800\r\n"
        b"\r\n"
        b"IMAP body text with order data"
    )
    fake_conn = FakeImapConnection(
        rfc822_by_uid={77: raw_rfc822},
        uids_by_header={"imap_inv@example.com": [77]},
    )
    imap_client = FakeImapClient(fake_conn)

    from mailattic import original_export

    monkeypatch.setattr(
        original_export,
        "_load_default_imap_client",
        lambda *args, **kwargs: imap_client,
    )

    server = create_server(database_path=db_path)
    result = asyncio.run(
        server.call_tool(
            "mail_original_export",
            {"message_id": imap_id, "dest_dir": str(dest_dir)},
        )
    )

    assert not result.is_error
    structured = result.structured_content
    assert structured is not None
    assert structured["status"] == "exported"
    assert structured["format"] == "eml"
    assert structured["source_type"] == "imap"
    dest_path = Path(str(structured["dest_path"]))
    assert dest_path.exists()
    assert dest_path.read_bytes() == raw_rfc822


def test_mcp_original_export_neither_found_reports_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, pst_id, _ = _seed_db(tmp_path)
    dest_dir = tmp_path / "host_exports"

    namespace = FakeOutlookNamespace([], items_by_id={})
    reader = FakeOutlookReader(namespace)
    from mailattic import original_export

    monkeypatch.setattr(original_export, "_load_default_outlook_reader", lambda: reader)

    server = create_server(database_path=db_path)
    result = asyncio.run(
        server.call_tool(
            "mail_original_export",
            {"message_id": pst_id, "dest_dir": str(dest_dir)},
        )
    )

    assert not result.is_error
    structured = result.structured_content
    assert structured is not None
    assert structured["status"] == "not_found"
    assert structured["dest_path"] is None
    assert structured["subject"] == "Table Quotation PST"
    assert "vendor@example.com" in structured["sender"]
    assert structured["sent_at"] == "2026-05-10T12:00:00+08:00"


def test_mcp_original_export_refuses_relative_path(tmp_path: Path) -> None:
    db_path, pst_id, _ = _seed_db(tmp_path)
    server = create_server(database_path=db_path)

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            server.call_tool(
                "mail_original_export",
                {"message_id": pst_id, "dest_dir": "relative/path"},
            )
        )
    assert "absolute host path" in str(exc_info.value)


def test_mcp_original_export_refuses_path_in_attachment_store(tmp_path: Path) -> None:
    db_path, pst_id, _ = _seed_db(tmp_path)
    server = create_server(database_path=db_path)
    att_store = (db_path.parent / "attachments").resolve()

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            server.call_tool(
                "mail_original_export",
                {"message_id": pst_id, "dest_dir": str(att_store)},
            )
        )
    assert "attachment store" in str(exc_info.value)


def test_mcp_original_export_unknown_message_id_raises_error(tmp_path: Path) -> None:
    db_path, _, _ = _seed_db(tmp_path)
    server = create_server(database_path=db_path)
    dest_dir = tmp_path / "host_exports"

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            server.call_tool(
                "mail_original_export",
                {"message_id": 999_999, "dest_dir": str(dest_dir)},
            )
        )
    assert "not found" in str(exc_info.value).lower()


def test_mcp_original_export_refuses_rfc_message_id(tmp_path: Path) -> None:
    db_path, _, _ = _seed_db(tmp_path)
    server = create_server(database_path=db_path)
    dest_dir = tmp_path / "host_exports"

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            server.call_tool(
                "mail_original_export",
                {"message_id": "<pst101@example.com>", "dest_dir": str(dest_dir)},
            )
        )
    assert "not a valid stored id" in str(exc_info.value)

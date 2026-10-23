from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import mcp.types as mcp_types
import pytest
from mcp import Client

from mailattic.attachment_store import (
    read_attachment,
    resolve_store_root,
)
from mailattic.cli import build_parser
from mailattic.mcp_server import create_server
from mailattic.models import MailboxParticipant, NormalizedMessage
from mailattic.outbound import (
    ENV_VAR_SMTP_ENV_FILE,
    SmtpConfig,
    _marker_path,
    assemble_draft_reply,
    send_draft,
)
from mailattic.query import (
    SearchFilters,
    get_message,
    search_attachments,
    search_messages,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

SMTP_USER = "sean3808@mailattic.test"
_MATCHING_ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "465",
    "SMTP_USER": SMTP_USER,
    "SMTP_PASSWORD": "secret",
    "IMAP_HOST": "imap.example.com",
    "IMAP_PORT": "993",
    "IMAP_USER": SMTP_USER,
    "IMAP_PASSWORD": "secret",
}


def _smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host="smtp.example.com",
        port=465,
        user=SMTP_USER,
        password="secret",
    )


def _patch_smtp(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_smtp = MagicMock()
    mock_smtp_ssl = MagicMock()
    mock_smtp_ssl.return_value.__enter__.return_value = mock_smtp
    monkeypatch.setattr(
        "mailattic.outbound.smtplib.SMTP_SSL",
        mock_smtp_ssl,
    )
    return mock_smtp


def _patch_imap_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_client = MagicMock()
    monkeypatch.setattr(
        "mailattic.outbound.NativeImapClient",
        lambda *args, **kwargs: mock_client,
    )
    return mock_client


def _auto_authorize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )


def _seed_message(db_path: Path, message_id: str = "<seed-msg@example.com>") -> str:
    connection = connect_database(db_path)
    try:
        norm = NormalizedMessage(
            subject="Inquiry about product",
            sender=MailboxParticipant(
                display_name="Alice Customer", email="alice@example.com"
            ),
            to_recipients=(MailboxParticipant(display_name="Sean", email=SMTP_USER),),
            cc_recipients=(),
            bcc_recipients=(),
            sent_at=datetime(2026, 10, 10, 8, 30, 0, tzinfo=UTC),
            received_at=datetime(2026, 10, 10, 8, 30, 0, tzinfo=UTC),
            body_text="Could you please send the quotation for 2026?",
            attachments=(),
            message_id=message_id,
            in_reply_to=None,
            references=None,
            pst_source="pst:test",
            folder_path="Inbox",
            logical_locator="Inbox/1",
            source_input_mode="file",
            source_requested_from="test",
        )
        persist_message(connection, norm)
        connection.commit()
    finally:
        connection.close()
    return message_id


def _call_tool(
    server, name: str, arguments: dict[str, object]
) -> mcp_types.CallToolResult:
    async def _call() -> mcp_types.CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(_call())


def _read_marker_status(db_path: Path, message_id: str) -> str:
    marker_file = _marker_path(db_path, message_id)
    assert marker_file.exists(), f"Marker file does not exist: {marker_file}"
    return json.loads(marker_file.read_text(encoding="utf-8"))["status"]


def _draft_reply(
    db_path: Path,
    message_id: str,
    body_text: str = "Test body",
    attachments: list[Path] | None = None,
):
    draft_dir = db_path.parent / "outbound" / "drafts"
    return assemble_draft_reply(
        database_path=db_path,
        message_id=message_id,
        draft_dir=draft_dir,
        own_address=SMTP_USER,
        body_text=body_text,
        attachments=attachments,
    )


def test_cli_send_draft_records_attachment_in_store_and_db_and_searchable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #291 AC: After sending a draft that carries one attachment, the stored

    sent record has the attachment (name, size, content in CAS under tmp_path root);
    bes attachments --name-contains and bes search --attachment-name find it.
    """
    db_path = initialize_database(tmp_path / "mailattic.db")
    store_root = resolve_store_root(db_path)
    seed_msg_id = _seed_message(db_path)
    _patch_smtp(monkeypatch)
    _patch_imap_client(monkeypatch)
    _auto_authorize(monkeypatch)

    attach_file = tmp_path / "quotation-2026.pdf"
    attach_bytes = b"%PDF-1.4 test quotation payload content 2026"
    attach_file.write_bytes(attach_bytes)

    draft_res = _draft_reply(
        db_path=db_path,
        message_id=seed_msg_id,
        body_text="Hello Alice, please see attached quotation.",
        attachments=[attach_file],
    )
    draft_id = draft_res.draft_id
    fingerprint = draft_res.fingerprint

    # Execute real send_draft
    send_res = send_draft(
        draft_id=draft_id,
        database_path=db_path,
        smtp_config=_smtp_config(),
        env=_MATCHING_ENV,
        fingerprint=fingerprint,
    )
    sent_msg_id = send_res["message_id"]
    assert _read_marker_status(db_path, sent_msg_id) == "sent"

    # 1. Stored sent record in DB has the attachment
    connection = connect_database(db_path)
    try:
        sent_row = connection.execute(
            "SELECT id, subject, message_id FROM messages WHERE message_id = ?",
            [sent_msg_id],
        ).fetchone()
        assert sent_row is not None
        sent_db_id = int(sent_row["id"])

        att_rows = connection.execute(
            "SELECT * FROM message_attachments WHERE message_id = ?",
            [sent_db_id],
        ).fetchall()
        assert len(att_rows) == 1
        att_row = att_rows[0]
        assert att_row["attachment_name"] == "quotation-2026.pdf"
        assert att_row["size_bytes"] == len(attach_bytes)
        assert att_row["content_hash"] is not None
        assert att_row["storage_path"] is not None
        storage_path = str(att_row["storage_path"])
    finally:
        connection.close()

    # 2. Content in CAS exists and matches original payload
    stored_bytes = read_attachment(storage_path, store_root)
    assert stored_bytes == attach_bytes

    # 3. get_message returns the attachment
    retrieved = get_message(db_path, sent_db_id)
    assert retrieved is not None
    attachments_in_get = retrieved["attachments"]
    assert isinstance(attachments_in_get, list)
    assert len(attachments_in_get) == 1
    assert attachments_in_get[0]["attachment_name"] == "quotation-2026.pdf"
    assert attachments_in_get[0]["size_bytes"] == len(attach_bytes)

    # 4. bes attachments --name-contains quotation finds it
    capsys.readouterr()  # clear prior stdout
    parser = build_parser()
    args_att = parser.parse_args(
        [
            "attachments",
            "--name-contains",
            "quotation",
            "--database",
            str(db_path),
            "--json",
        ]
    )
    exit_code_att = args_att.handler(args_att)
    assert exit_code_att == 0
    att_json = json.loads(capsys.readouterr().out)
    assert len(att_json) == 1
    assert att_json[0]["attachment_name"] == "quotation-2026.pdf"
    assert att_json[0]["size_bytes"] == len(attach_bytes)

    # 5. bes search --attachment-name quotation finds it
    args_search = parser.parse_args(
        [
            "search",
            "--attachment-name",
            "quotation",
            "--database",
            str(db_path),
            "--json",
        ]
    )
    exit_code_search = args_search.handler(args_search)
    assert exit_code_search == 0
    search_json = json.loads(capsys.readouterr().out)
    assert search_json[0]["id"] == sent_db_id
    assert "quotation-2026.pdf" in search_json[0]["attachment_names"]


def test_mcp_mail_send_path_records_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #291 AC: One assertion that the MCP mail_send path records it too."""
    db_path = initialize_database(tmp_path / "mailattic.db")
    store_root = resolve_store_root(db_path)
    seed_msg_id = _seed_message(db_path)
    _patch_smtp(monkeypatch)
    _patch_imap_client(monkeypatch)

    env_file = tmp_path / "mcp.env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in _MATCHING_ENV.items()) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(env_file))

    attach_file = tmp_path / "spec-sheet.pdf"
    attach_bytes = b"%PDF-1.4 spec sheet content bytes"
    attach_file.write_bytes(attach_bytes)

    server = create_server(database_path=db_path)

    # Draft reply via MCP
    draft_res = _call_tool(
        server,
        "mail_draft_reply",
        {
            "message_id": seed_msg_id,
            "body_text": "Spec sheet attached.",
            "attachments": [str(attach_file)],
        },
    )
    assert not draft_res.is_error
    draft_data = draft_res.structured_content
    assert draft_data is not None
    draft_id = str(draft_data["draft_id"])
    fingerprint = str(draft_data["fingerprint"])

    # Send via MCP mail_send
    send_res = _call_tool(
        server,
        "mail_send",
        {"draft_id": draft_id, "fingerprint": fingerprint},
    )
    assert not send_res.is_error
    send_data = send_res.structured_content
    assert send_data is not None
    assert send_data["status"] == "sent"
    sent_msg_id = str(send_data["message_id"])

    # Verify attachment is recorded in DB
    connection = connect_database(db_path)
    try:
        sent_row = connection.execute(
            "SELECT id FROM messages WHERE message_id = ?",
            [sent_msg_id],
        ).fetchone()
        assert sent_row is not None
        sent_db_id = int(sent_row["id"])

        att_rows = connection.execute(
            "SELECT * FROM message_attachments WHERE message_id = ?",
            [sent_db_id],
        ).fetchall()
        assert len(att_rows) == 1
        assert att_rows[0]["attachment_name"] == "spec-sheet.pdf"
        assert att_rows[0]["size_bytes"] == len(attach_bytes)
        storage_path = str(att_rows[0]["storage_path"])
    finally:
        connection.close()

    # Content in CAS exists
    assert read_attachment(storage_path, store_root) == attach_bytes


def test_send_draft_two_attachments_with_same_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #291 AC: Two attachments with the same content are covered."""
    db_path = initialize_database(tmp_path / "mailattic.db")
    store_root = resolve_store_root(db_path)
    seed_msg_id = _seed_message(db_path)
    _patch_smtp(monkeypatch)
    _patch_imap_client(monkeypatch)
    _auto_authorize(monkeypatch)

    same_bytes = b"identical duplicate content for both files"
    att1 = tmp_path / "doc-a.pdf"
    att1.write_bytes(same_bytes)
    att2 = tmp_path / "doc-b.pdf"
    att2.write_bytes(same_bytes)

    draft_res = _draft_reply(
        db_path=db_path,
        message_id=seed_msg_id,
        body_text="Attached are doc-a and doc-b.",
        attachments=[att1, att2],
    )

    send_res = send_draft(
        draft_id=draft_res.draft_id,
        database_path=db_path,
        smtp_config=_smtp_config(),
        env=_MATCHING_ENV,
        fingerprint=draft_res.fingerprint,
    )
    sent_msg_id = send_res["message_id"]

    connection = connect_database(db_path)
    try:
        sent_row = connection.execute(
            "SELECT id FROM messages WHERE message_id = ?",
            [sent_msg_id],
        ).fetchone()
        assert sent_row is not None
        sent_db_id = int(sent_row["id"])

        att_rows = connection.execute(
            "SELECT attachment_name, content_hash, storage_path, size_bytes "
            "FROM message_attachments WHERE message_id = ? ORDER BY id",
            [sent_db_id],
        ).fetchall()
        assert len(att_rows) == 2
        names = [r["attachment_name"] for r in att_rows]
        assert names == ["doc-a.pdf", "doc-b.pdf"]
        # Same content means identical content_hash and storage_path in CAS
        assert att_rows[0]["content_hash"] == att_rows[1]["content_hash"]
        assert att_rows[0]["storage_path"] == att_rows[1]["storage_path"]
        assert att_rows[0]["size_bytes"] == len(same_bytes)

        # File in CAS is readable
        cas_bytes = read_attachment(str(att_rows[0]["storage_path"]), store_root)
        assert cas_bytes == same_bytes
    finally:
        connection.close()


def test_send_draft_attachment_with_non_ascii_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #291 AC: An attachment with a non-ASCII filename is covered."""
    db_path = initialize_database(tmp_path / "mailattic.db")
    store_root = resolve_store_root(db_path)
    seed_msg_id = _seed_message(db_path)
    _patch_smtp(monkeypatch)
    _patch_imap_client(monkeypatch)
    _auto_authorize(monkeypatch)

    non_ascii_name = "報價單-2026.pdf"
    attach_file = tmp_path / non_ascii_name
    attach_bytes = b"%PDF-1.4 chinese filename payload"
    attach_file.write_bytes(attach_bytes)

    draft_res = _draft_reply(
        db_path=db_path,
        message_id=seed_msg_id,
        body_text="Attached is the Chinese quotation.",
        attachments=[attach_file],
    )

    send_res = send_draft(
        draft_id=draft_res.draft_id,
        database_path=db_path,
        smtp_config=_smtp_config(),
        env=_MATCHING_ENV,
        fingerprint=draft_res.fingerprint,
    )
    sent_msg_id = send_res["message_id"]

    # Verify search_attachments and search_messages locate it with CJK query
    results_att = search_attachments(
        database_path=db_path,
        filters=SearchFilters(attachment_name="報價單"),
    )
    assert len(results_att) == 1
    assert results_att[0]["attachment_name"] == non_ascii_name
    assert results_att[0]["storage_path"] is not None
    assert (
        read_attachment(str(results_att[0]["storage_path"]), store_root) == attach_bytes
    )

    results_search = search_messages(
        database_path=db_path,
        filters=SearchFilters(attachment_name="報價單"),
        full_body=True,
    )
    assert len(results_search) == 1
    search_json = results_search[0]
    assert search_json["message_id"] == sent_msg_id
    assert non_ascii_name in search_json["attachment_names"]


def test_send_draft_with_no_attachment_record_stays_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #291 AC: A draft with no attachment (record stays as today) is covered."""
    db_path = initialize_database(tmp_path / "mailattic.db")
    seed_msg_id = _seed_message(db_path)
    _patch_smtp(monkeypatch)
    _patch_imap_client(monkeypatch)
    _auto_authorize(monkeypatch)

    draft_res = _draft_reply(
        db_path=db_path,
        message_id=seed_msg_id,
        body_text="No attachments in this reply.",
        attachments=None,
    )

    send_res = send_draft(
        draft_id=draft_res.draft_id,
        database_path=db_path,
        smtp_config=_smtp_config(),
        env=_MATCHING_ENV,
        fingerprint=draft_res.fingerprint,
    )
    sent_msg_id = send_res["message_id"]

    connection = connect_database(db_path)
    try:
        sent_row = connection.execute(
            "SELECT id, attachment_search_text FROM messages WHERE message_id = ?",
            [sent_msg_id],
        ).fetchone()
        assert sent_row is not None
        assert sent_row["attachment_search_text"] == ""
        sent_db_id = int(sent_row["id"])

        count = connection.execute(
            "SELECT COUNT(*) as c FROM message_attachments WHERE message_id = ?",
            [sent_db_id],
        ).fetchone()["c"]
        assert count == 0
    finally:
        connection.close()

    retrieved = get_message(db_path, sent_db_id)
    assert retrieved is not None
    assert retrieved["attachments"] == []


def test_attachment_recording_failure_after_smtp_accept_does_not_fail_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #291 AC: If recording the attachment fails AFTER the SMTP server
    accepted the mail, the send is still reported as sent (never invite a
    resend - contract #3 reasoning); the failure is surfaced the same way
    existing post-send bookkeeping failures are.
    """
    db_path = initialize_database(tmp_path / "mailattic.db")
    seed_msg_id = _seed_message(db_path)
    _patch_smtp(monkeypatch)
    _patch_imap_client(monkeypatch)
    _auto_authorize(monkeypatch)

    attach_file = tmp_path / "contract.pdf"
    attach_file.write_bytes(b"contract payload")

    draft_res = _draft_reply(
        db_path=db_path,
        message_id=seed_msg_id,
        body_text="Reply with contract.",
        attachments=[attach_file],
    )
    draft_id = draft_res.draft_id
    fingerprint = draft_res.fingerprint

    # Simulate write_attachment failure in outbound
    def _failing_write_attachment(*args, **kwargs):
        raise OSError("Simulated disk error during CAS storage write")

    monkeypatch.setattr(
        "mailattic.outbound.write_attachment",
        _failing_write_attachment,
    )

    send_res = send_draft(
        draft_id=draft_id,
        database_path=db_path,
        smtp_config=_smtp_config(),
        env=_MATCHING_ENV,
        fingerprint=fingerprint,
    )
    sent_msg_id = send_res["message_id"]

    # Send is reported as sent and marker is updated to sent (never invites resend)
    assert _read_marker_status(db_path, sent_msg_id) == "sent"

    # Post-send bookkeeping failure is surfaced on stderr as a warning
    captured = capsys.readouterr()
    assert "Warning: could not record attachment" in captured.err
    assert "contract.pdf" in captured.err
    assert "Simulated disk error" in captured.err


def test_same_message_id_dedup_first_one_wins_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #291 AC: The 'same Message-ID, first one wins' dedup rule is untouched."""
    db_path = initialize_database(tmp_path / "mailattic.db")
    seed_msg_id = _seed_message(db_path)
    _patch_smtp(monkeypatch)
    _patch_imap_client(monkeypatch)
    _auto_authorize(monkeypatch)

    attach_file = tmp_path / "offer.pdf"
    attach_bytes = b"offer payload"
    attach_file.write_bytes(attach_bytes)

    draft_res = _draft_reply(
        db_path=db_path,
        message_id=seed_msg_id,
        body_text="Reply with offer.",
        attachments=[attach_file],
    )

    send_res = send_draft(
        draft_id=draft_res.draft_id,
        database_path=db_path,
        smtp_config=_smtp_config(),
        env=_MATCHING_ENV,
        fingerprint=draft_res.fingerprint,
    )
    sent_msg_id = send_res["message_id"]

    # Verify first one is stored
    connection = connect_database(db_path)
    try:
        sent_row = connection.execute(
            "SELECT id, pst_source FROM messages WHERE message_id = ?",
            [sent_msg_id],
        ).fetchone()
        assert sent_row is not None
        assert sent_row["pst_source"] == "outbound-bes"
        orig_id = int(sent_row["id"])

        # Simulate later IMAP sync importing the sent copy from Sent folder
        # with same Message-ID
        dup_norm = NormalizedMessage(
            subject="Re: Inquiry about product",
            sender=MailboxParticipant(display_name="Sean", email=SMTP_USER),
            to_recipients=(
                MailboxParticipant(
                    display_name="Alice Customer", email="alice@example.com"
                ),
            ),
            cc_recipients=(),
            bcc_recipients=(),
            sent_at=datetime.now(tz=UTC),
            received_at=datetime.now(tz=UTC),
            body_text="Reply with offer.",
            attachments=(),
            message_id=sent_msg_id,
            in_reply_to=seed_msg_id,
            references=seed_msg_id,
            pst_source="imap:sean3808@mailattic.test:Sent",
            folder_path="Sent",
            logical_locator="imap:12345",
            source_input_mode="maildir",
            source_requested_from="test",
        )
        persist_res = persist_message(connection, dup_norm)
        assert persist_res.inserted is False
        assert persist_res.stored_message_id == orig_id

        # Verify original record and its attachment remain intact
        att_rows = connection.execute(
            "SELECT attachment_name FROM message_attachments WHERE message_id = ?",
            [orig_id],
        ).fetchall()
        assert len(att_rows) == 1
        assert att_rows[0]["attachment_name"] == "offer.pdf"
    finally:
        connection.close()


def test_post_submit_cas_failure_leaves_placeholder_ledger_and_healthy_copy_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#18: a CAS failure after SMTP accept keeps a placeholder row plus an open
    ledger entry, so a healthy Sent copy later fills the exact hash/path/size.
    The send stays sent, SMTP is called once, and replay is idempotent.
    """
    from mailattic.attachment_store import write_attachment
    from mailattic.models import NormalizedAttachment
    from mailattic.quarantine import unresolved_attachment_failures

    db_path = initialize_database(tmp_path / "mailattic.db")
    seed_msg_id = _seed_message(db_path)
    smtp_factory = _patch_smtp(monkeypatch)
    _patch_imap_client(monkeypatch)
    _auto_authorize(monkeypatch)

    payload = b"contract payload"
    attach_file = tmp_path / "contract.pdf"
    attach_file.write_bytes(payload)
    draft_res = _draft_reply(
        db_path=db_path,
        message_id=seed_msg_id,
        attachments=[attach_file],
    )

    def _failing_write_attachment(*args, **kwargs):
        raise OSError("Simulated disk error during CAS storage write")

    monkeypatch.setattr(
        "mailattic.outbound.write_attachment", _failing_write_attachment
    )
    send_res = send_draft(
        draft_id=draft_res.draft_id,
        database_path=db_path,
        smtp_config=_smtp_config(),
        env=_MATCHING_ENV,
        fingerprint=draft_res.fingerprint,
    )
    sent_msg_id = send_res["message_id"]
    assert _read_marker_status(db_path, sent_msg_id) == "sent"
    assert smtp_factory.sendmail.call_count == 1

    connection = connect_database(db_path)
    try:
        row = connection.execute(
            "SELECT id, logical_locator FROM messages WHERE message_id = ?",
            [sent_msg_id],
        ).fetchone()
        stored_id = int(row["id"])
        att = connection.execute(
            "SELECT attachment_name, content_hash, storage_path FROM"
            " message_attachments WHERE message_id = ?",
            [stored_id],
        ).fetchall()
        assert [
            (a["attachment_name"], a["content_hash"], a["storage_path"]) for a in att
        ] == [("contract.pdf", None, None)]
        open_failures = unresolved_attachment_failures(connection)
        assert len(open_failures) == 1
        assert open_failures[0]["attachment_name"] == "contract.pdf"
        assert open_failures[0]["attachment_size"] == len(payload)

        # Healthy Sent copy arrives (same Message-ID, real CAS write).
        record = write_attachment(
            payload=payload,
            original_name="contract.pdf",
            mime_type="application/pdf",
            store_root=resolve_store_root(db_path),
        )
        healthy = NormalizedMessage(
            subject="Re: Inquiry about product",
            sender=MailboxParticipant(display_name="Sean", email=SMTP_USER),
            to_recipients=(),
            cc_recipients=(),
            bcc_recipients=(),
            sent_at=datetime.now(tz=UTC),
            received_at=datetime.now(tz=UTC),
            body_text="x",
            attachments=(
                NormalizedAttachment(
                    attachment_name="contract.pdf",
                    content_hash=record.content_hash,
                    size_bytes=record.size_bytes,
                    mime_type=record.mime_type,
                    storage_path=record.storage_path,
                ),
            ),
            message_id=sent_msg_id,
            in_reply_to=None,
            references=None,
            pst_source="imap:sean3808@mailattic.test:Sent",
            folder_path="Sent",
            logical_locator="imap:12345",
            source_input_mode="maildir",
            source_requested_from="test",
        )
        first = persist_message(connection, healthy)
        assert first.inserted is False
        assert first.recovered_attachments == 1
        filled = connection.execute(
            "SELECT content_hash, storage_path, size_bytes FROM message_attachments"
            " WHERE message_id = ?",
            [stored_id],
        ).fetchone()
        assert filled["content_hash"] == record.content_hash
        assert filled["storage_path"] == record.storage_path
        assert filled["size_bytes"] == len(payload)
        assert unresolved_attachment_failures(connection) == []
        assert persist_message(connection, healthy).recovered_attachments == 0
    finally:
        connection.close()

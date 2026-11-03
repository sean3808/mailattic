from __future__ import annotations

from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mailattic.cli import build_parser
from mailattic.models import MailboxParticipant, NormalizedMessage
from mailattic.outbound import assemble_draft_reply
from mailattic.outbound_commands import (
    _handle_draft_new,
    _handle_send_batch,
    _handle_send_draft,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

SMTP_USER = "sean3808@mailattic.test"


def _seed_recipient_history(db_path: Path) -> None:
    conn = connect_database(db_path)
    try:
        # Seed an existing message with sender and recipients to establish familiarity
        msg = NormalizedMessage(
            subject="Prior correspondence",
            sender=MailboxParticipant(
                display_name="Known Sender", email="known_sender@example.com"
            ),
            to_recipients=(
                MailboxParticipant(
                    display_name="Known To", email="known_to@example.com"
                ),
            ),
            cc_recipients=(
                MailboxParticipant(
                    display_name="Colleague", email="colleague@domain-only.test"
                ),
            ),
            bcc_recipients=(),
            sent_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
            received_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
            body_text="Earlier message body",
            attachments=(),
            message_id="<prior-msg-1@mailattic.test>",
            in_reply_to=None,
            references=None,
            pst_source="imap-test",
            folder_path="Inbox",
            logical_locator="imap:1",
            source_input_mode="imap",
            source_requested_from="test",
        )
        persist_message(conn, msg)
        conn.commit()
    finally:
        conn.close()


def _write_smtp_env(tmp_path: Path) -> Path:
    env_file = tmp_path / "smtp.env"
    env_file.write_text(
        "SMTP_HOST=smtp.example.com\n"
        "SMTP_PORT=465\n"
        f"SMTP_USER={SMTP_USER}\n"
        "SMTP_PASSWORD=secret\n",
        encoding="utf-8",
    )
    return env_file


def test_draft_new_cli_creates_draft_with_headers_and_no_reply_headers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _seed_recipient_history(db_path)
    env_file = _write_smtp_env(tmp_path)

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello from new draft", encoding="utf-8")

    attach_file = tmp_path / "doc.pdf"
    attach_file.write_bytes(b"%PDF-1.4 test pdf content")

    parser = build_parser()
    args = parser.parse_args(
        [
            "draft-new",
            "--to",
            "Alice <known_to@example.com>",
            "--cc",
            "Bob <new_user@domain-only.test>",
            "--bcc",
            "Eve <stranger@unknown-world.org>",
            "--subject",
            "A Fresh Conversation",
            "--body-from",
            str(body_file),
            "--attach",
            str(attach_file),
            "--database",
            str(db_path),
            "--env-file",
            str(env_file),
        ]
    )

    exit_code = _handle_draft_new(args)
    assert exit_code == 0

    stdout = capsys.readouterr().out
    assert "draft_id:" in stdout
    assert f"From: {SMTP_USER}" in stdout
    assert "Subject: A Fresh Conversation" in stdout
    assert "To: Alice <known_to@example.com>" in stdout
    assert "Cc: Bob <new_user@domain-only.test>" in stdout
    assert "Bcc: " in stdout
    assert f"{SMTP_USER}" in stdout
    assert "Eve <stranger@unknown-world.org>" in stdout
    assert "Attachments: 1" in stdout
    assert "doc.pdf" in stdout
    assert "fingerprint:" in stdout

    # Recipient checks
    assert "Recipient check:" in stdout
    assert "known_to@example.com: seen" in stdout
    assert "new_user@domain-only.test: never seen (domain seen)" in stdout
    assert "stranger@unknown-world.org: never seen" in stdout
    # BCC-self is not in recipient check
    assert SMTP_USER not in stdout.split("Recipient check:")[1]

    # Verify .eml on disk
    (draft_path,) = (tmp_path / "outbound" / "drafts").glob("*.eml")
    parsed = BytesParser().parsebytes(draft_path.read_bytes())
    assert parsed["Subject"] == "A Fresh Conversation"
    assert parsed["From"] == SMTP_USER
    assert "known_to@example.com" in parsed["To"]
    assert "new_user@domain-only.test" in parsed["Cc"]
    assert "stranger@unknown-world.org" in parsed["Bcc"]
    assert SMTP_USER in parsed["Bcc"]

    # In-Reply-To and References MUST NOT be present
    assert parsed["In-Reply-To"] is None
    assert parsed["References"] is None
    assert parsed["Message-ID"] is not None
    assert parsed["Message-ID"].startswith("<") and parsed["Message-ID"].endswith(">")


@pytest.mark.parametrize(
    "bad_subject",
    ["", "   ", "\t", "\n"],
)
def test_draft_new_cli_refuses_blank_or_whitespace_subject(
    bad_subject: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    env_file = _write_smtp_env(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("body", encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(
        [
            "draft-new",
            "--to",
            "user@example.com",
            "--subject",
            bad_subject,
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(env_file),
        ]
    )
    exit_code = _handle_draft_new(args)
    assert exit_code == 2

    stderr = capsys.readouterr().err
    assert "--subject" in stderr

    # No draft written
    drafts_dir = tmp_path / "outbound" / "drafts"
    assert not drafts_dir.exists() or not list(drafts_dir.glob("*.eml"))


def test_draft_new_cli_refuses_missing_to(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    env_file = _write_smtp_env(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("body", encoding="utf-8")

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "draft-new",
                "--subject",
                "Subject",
                "--body-from",
                str(body_file),
                "--database",
                str(db_path),
                "--env-file",
                str(env_file),
            ]
        )

    drafts_dir = tmp_path / "outbound" / "drafts"
    assert not drafts_dir.exists() or not list(drafts_dir.glob("*.eml"))


def test_draft_new_cli_refuses_malformed_recipient(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    env_file = _write_smtp_env(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("body", encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(
        [
            "draft-new",
            "--to",
            "not-an-email-address",
            "--subject",
            "Valid Subject",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(env_file),
        ]
    )
    exit_code = _handle_draft_new(args)
    assert exit_code == 2

    stderr = capsys.readouterr().err
    assert "--to requires one valid email address" in stderr


def test_draft_new_cli_send_draft_end_to_end_persists_new_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _seed_recipient_history(db_path)
    env_file = _write_smtp_env(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("Brand new message content", encoding="utf-8")

    parser = build_parser()
    draft_args = parser.parse_args(
        [
            "draft-new",
            "--to",
            "partner@example.com",
            "--subject",
            "Starting Fresh Thread",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(env_file),
        ]
    )
    assert _handle_draft_new(draft_args) == 0
    stdout = capsys.readouterr().out

    draft_id_line = next(
        line for line in stdout.splitlines() if line.startswith("draft_id:")
    )
    draft_id = draft_id_line.split(":")[1].strip()

    fingerprint_line = next(
        line for line in stdout.splitlines() if line.startswith("fingerprint:")
    )
    fp = fingerprint_line.split(":")[1].strip()

    mock_smtp_ssl = MagicMock()
    monkeypatch.setattr("mailattic.outbound.smtplib.SMTP_SSL", mock_smtp_ssl)
    monkeypatch.setattr(
        "mailattic.outbound._append_sent_copy",
        lambda *args, **kwargs: None,
    )

    send_args = parser.parse_args(
        [
            "send-draft",
            draft_id,
            "--fingerprint",
            fp,
            "--database",
            str(db_path),
            "--env-file",
            str(env_file),
        ]
    )
    send_code = _handle_send_draft(send_args)
    assert send_code == 0
    mock_smtp_ssl.assert_called_once()

    # Verify DB persistence
    conn = connect_database(db_path)
    try:
        row = conn.execute(
            "SELECT id, subject, in_reply_to, references_header, pst_source, "
            "thread_key "
            "FROM messages WHERE pst_source = 'outbound-bes'"
        ).fetchone()
        assert row is not None
        assert row["subject"] == "Starting Fresh Thread"
        assert row["in_reply_to"] is None
        assert row["references_header"] is None
        assert row["pst_source"] == "outbound-bes"
        new_thread_key = row["thread_key"]
        assert new_thread_key is not None

        # Confirm thread_key is not shared with the prior seeded message
        prior_row = conn.execute(
            "SELECT thread_key FROM messages "
            "WHERE message_id = '<prior-msg-1@mailattic.test>'"
        ).fetchone()
        assert prior_row["thread_key"] != new_thread_key
    finally:
        conn.close()


def test_send_batch_accepts_new_thread_draft_alongside_reply_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _seed_recipient_history(db_path)
    env_file = _write_smtp_env(tmp_path)

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello body", encoding="utf-8")

    parser = build_parser()

    # 1. Create a reply draft
    reply_result = assemble_draft_reply(
        database_path=db_path,
        message_id="<prior-msg-1@mailattic.test>",
        own_address=SMTP_USER,
        body_text="Reply text",
    )
    reply_draft_id = reply_result.draft_id
    reply_fp = reply_result.fingerprint

    # 2. Create a new-thread draft
    draft_args = parser.parse_args(
        [
            "draft-new",
            "--to",
            "newpartner@example.com",
            "--subject",
            "New Batch Subject",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(env_file),
        ]
    )
    assert _handle_draft_new(draft_args) == 0
    stdout = capsys.readouterr().out
    new_draft_id = (
        next(line for line in stdout.splitlines() if line.startswith("draft_id:"))
        .split(":")[1]
        .strip()
    )
    new_fp = (
        next(line for line in stdout.splitlines() if line.startswith("fingerprint:"))
        .split(":")[1]
        .strip()
    )

    mock_smtp_ssl = MagicMock()
    monkeypatch.setattr("mailattic.outbound.smtplib.SMTP_SSL", mock_smtp_ssl)
    monkeypatch.setattr(
        "mailattic.outbound._append_sent_copy",
        lambda *args, **kwargs: None,
    )

    batch_args = parser.parse_args(
        [
            "send-batch",
            f"{reply_draft_id}={reply_fp}",
            f"{new_draft_id}={new_fp}",
            "--database",
            str(db_path),
            "--env-file",
            str(env_file),
        ]
    )
    batch_code = _handle_send_batch(batch_args)
    assert batch_code == 0

    batch_stdout = capsys.readouterr().out
    assert f"Sent: draft_id={reply_draft_id}" in batch_stdout
    assert f"Sent: draft_id={new_draft_id}" in batch_stdout

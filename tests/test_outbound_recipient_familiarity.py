from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mailattic.cli import build_parser
from mailattic.models import PstSource, build_normalized_message
from mailattic.outbound import (
    draft_fingerprint,
)
from mailattic.outbound_commands import (
    _handle_draft_new,
    _handle_draft_reply,
    _handle_send_batch,
    _handle_send_draft,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)
from tests.test_outbound_draft import _placeholder_env_file

SMTP_USER = "sean3808@mailattic.test"
GMAIL = "g" + "mail.com"  # split: see PUBLIC_WEBMAIL_DOMAINS in query.py


def _seed_custom_message(
    db_path: Path,
    *,
    message_id: str,
    sender_email: str = "client@example.com",
    to_emails: list[str] | None = None,
    cc_emails: list[str] | None = None,
    bcc_emails: list[str] | None = None,
    subject: str = "Test subject",
) -> None:
    message = build_normalized_message(
        {
            "subject": subject,
            "sender": {"name": None, "email": sender_email},
            "to": [{"name": None, "email": email} for email in to_emails or []],
            "cc": [{"name": None, "email": email} for email in cc_emails or []],
            "bcc": [{"name": None, "email": email} for email in bcc_emails or []],
            "message_id": message_id,
            "references": None,
            "body_text": "sample body",
        },
        source=PstSource(
            path=Path("C:/archive/mailbox.pst"),
            input_mode="file",
            requested_from=Path("C:/archive/mailbox.pst"),
        ),
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    conn = connect_database(db_path)
    try:
        persist_message(conn, message)
        conn.commit()
    finally:
        conn.close()


def test_draft_reply_cli_seen_as_sender_to_cc_bcc_in_mixed_case(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "evidence.db")
    _seed_custom_message(
        db_path,
        message_id="<anchor-msg@mailattic.test>",
        sender_email="anchor@mailattic.test",
        to_emails=[SMTP_USER],
    )
    _seed_custom_message(
        db_path,
        message_id="<seen-roles-msg@mailattic.test>",
        sender_email="SeNdEr@ExAmPlE.cOm",
        to_emails=["To_UsEr@ExAmPlE.cOm"],
        cc_emails=["Cc_UsEr@ExAmPlE.cOm"],
        bcc_emails=["Bcc_UsEr@ExAmPlE.cOm"],
    )

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello reply", encoding="utf-8")

    args = build_parser().parse_args(
        [
            "draft-reply",
            "<anchor-msg@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
            "--to",
            "sender@example.com",
            "--to",
            "SENDER@EXAMPLE.COM",
            "--to",
            "to_user@example.com",
            "--to",
            "TO_USER@EXAMPLE.COM",
            "--cc",
            "cc_user@example.com",
            "--cc",
            "CC_USER@EXAMPLE.COM",
            "--bcc",
            "bcc_user@example.com",
            "--bcc",
            "BCC_USER@EXAMPLE.COM",
        ]
    )

    exit_code = _handle_draft_reply(args)
    assert exit_code == 0

    stdout = capsys.readouterr().out
    assert "Recipient check:" in stdout
    assert "sender@example.com: seen" in stdout
    assert "SENDER@EXAMPLE.COM: seen" in stdout
    assert "to_user@example.com: seen" in stdout
    assert "TO_USER@EXAMPLE.COM: seen" in stdout
    assert "cc_user@example.com: seen" in stdout
    assert "CC_USER@EXAMPLE.COM: seen" in stdout
    assert "bcc_user@example.com: seen" in stdout
    assert "BCC_USER@EXAMPLE.COM: seen" in stdout


def test_draft_reply_cli_marks_seen_never_seen_and_domain_seen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "evidence.db")
    _seed_custom_message(
        db_path,
        message_id="<anchor-msg@mailattic.test>",
        sender_email="client@example.com",
        to_emails=[SMTP_USER],
    )
    _seed_custom_message(
        db_path,
        message_id="<known-domain-msg@mailattic.test>",
        sender_email="vendor@partner.com",
    )

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello reply", encoding="utf-8")

    args = build_parser().parse_args(
        [
            "draft-reply",
            "<anchor-msg@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
            "--to",
            "client@example.com",
            "--to",
            "fresh_contact@partner.com",
            "--to",
            "stranger@unknown-world.org",
        ]
    )

    exit_code = _handle_draft_reply(args)
    assert exit_code == 0

    stdout = capsys.readouterr().out
    assert "Recipient check:" in stdout
    assert "client@example.com: seen" in stdout
    assert "fresh_contact@partner.com: never seen (domain seen)" in stdout
    assert "stranger@unknown-world.org: never seen" in stdout
    # Ensure domain hint is only on the domain seen one
    assert "stranger@unknown-world.org: never seen (domain seen)" not in stdout


def test_draft_reply_cli_webmail_never_seen_gets_no_domain_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "evidence.db")
    _seed_custom_message(
        db_path,
        message_id="<anchor-msg@mailattic.test>",
        sender_email="client@example.com",
        to_emails=[SMTP_USER],
    )
    _seed_custom_message(
        db_path,
        message_id="<webmail-msg@mailattic.test>",
        sender_email=f"existing_user@{GMAIL}",
        to_emails=["other@163.com"],
    )

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello reply", encoding="utf-8")

    new_user = f"new_user@{GMAIL}"
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<anchor-msg@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
            "--to",
            new_user,
            "--to",
            "brand_new_user@163.com",
        ]
    )

    exit_code = _handle_draft_reply(args)
    assert exit_code == 0

    stdout = capsys.readouterr().out
    assert "Recipient check:" in stdout
    assert f"{new_user}: never seen" in stdout
    assert "brand_new_user@163.com: never seen" in stdout
    assert "domain seen" not in stdout


def test_draft_reply_cli_quoted_local_part_at_domain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "evidence.db")
    _seed_custom_message(
        db_path,
        message_id="<anchor-msg@mailattic.test>",
        sender_email="anchor@mailattic.test",
        to_emails=[SMTP_USER],
    )
    _seed_custom_message(
        db_path,
        message_id="<msg-seed-quoted@mailattic.test>",
        sender_email="colleague@knowncorp.test",
    )

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello reply", encoding="utf-8")

    quoted_addr = '"user@corp"@knowncorp.test'
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<anchor-msg@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
            "--to",
            quoted_addr,
        ]
    )

    exit_code = _handle_draft_reply(args)
    assert exit_code == 0

    stdout = capsys.readouterr().out
    assert "Recipient check:" in stdout
    assert f"{quoted_addr}: never seen (domain seen)" in stdout


def test_plain_reply_all_draft_prints_no_recipient_check_and_no_bcc_self(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "evidence.db")
    _seed_custom_message(
        db_path,
        message_id="<anchor-msg@mailattic.test>",
        sender_email="client@example.com",
        to_emails=[SMTP_USER],
    )

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello reply", encoding="utf-8")

    args = build_parser().parse_args(
        [
            "draft-reply",
            "<anchor-msg@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    exit_code = _handle_draft_reply(args)
    assert exit_code == 0

    stdout = capsys.readouterr().out
    assert "Recipient check:" not in stdout
    assert f"Bcc: {SMTP_USER}" in stdout


def test_draft_reply_cli_replace_recipients_marks_only_explicit_addresses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "evidence.db")
    _seed_custom_message(
        db_path,
        message_id="<anchor-msg@mailattic.test>",
        sender_email="old_sender@example.com",
        to_emails=[SMTP_USER],
    )

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello reply", encoding="utf-8")

    args = build_parser().parse_args(
        [
            "draft-reply",
            "<anchor-msg@mailattic.test>",
            "--replace-recipients",
            "--to",
            "replaced@new-target.test",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    exit_code = _handle_draft_reply(args)
    assert exit_code == 0

    stdout = capsys.readouterr().out
    assert "Recipient check:" in stdout
    assert "replaced@new-target.test: never seen" in stdout
    assert "old_sender@example.com" not in stdout.split("Recipient check:")[1]


def test_send_draft_recomputes_marks_at_send_time_flipping_to_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "evidence.db")
    _seed_custom_message(
        db_path,
        message_id="<anchor-msg@mailattic.test>",
        sender_email="client@example.com",
        to_emails=[SMTP_USER],
    )

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello reply", encoding="utf-8")

    # 1. Create draft with a never-seen address
    args_draft = build_parser().parse_args(
        [
            "draft-reply",
            "<anchor-msg@mailattic.test>",
            "--to",
            "future_partner@newbiz.test",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )
    assert _handle_draft_reply(args_draft) == 0
    draft_stdout = capsys.readouterr().out
    assert "future_partner@newbiz.test: never seen" in draft_stdout

    # Find draft file & extract draft_id and fingerprint
    draft_files = list((tmp_path / "outbound" / "drafts").glob("*.eml"))
    assert len(draft_files) == 1
    draft_path = draft_files[0]
    draft_id = draft_path.stem
    initial_bytes = draft_path.read_bytes()
    initial_fingerprint = draft_fingerprint(initial_bytes)

    # 2. Seed future_partner@newbiz.test into DB after the draft was created
    _seed_custom_message(
        db_path,
        message_id="<fresh-incoming-msg@mailattic.test>",
        sender_email="future_partner@newbiz.test",
        to_emails=[SMTP_USER],
    )

    # 3. Mock SMTP submission so send_draft succeeds without hitting real SMTP
    mock_smtp = MagicMock(return_value=({}, None))
    monkeypatch.setattr("mailattic.outbound._smtp_submit", mock_smtp)
    monkeypatch.setattr("mailattic.outbound._append_sent_copy", MagicMock())

    # Send with fingerprint
    args_send = build_parser().parse_args(
        [
            "send-draft",
            draft_id,
            "--fingerprint",
            initial_fingerprint,
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    send_exit = _handle_send_draft(args_send)
    assert send_exit == 0

    send_stdout = capsys.readouterr().out
    # The review summary shows the mark flipped to seen!
    assert "Recipient check:" in send_stdout
    assert "future_partner@newbiz.test: seen" in send_stdout

    # Draft bytes on disk and fingerprint are unchanged by the marks
    assert draft_path.read_bytes() == initial_bytes
    assert draft_fingerprint(draft_path.read_bytes()) == initial_fingerprint


def test_plain_reply_all_draft_send_review_has_no_recipient_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "evidence.db")
    _seed_custom_message(
        db_path,
        message_id="<anchor-msg@mailattic.test>",
        sender_email="client@example.com",
        to_emails=[SMTP_USER],
    )

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello reply", encoding="utf-8")

    args_draft = build_parser().parse_args(
        [
            "draft-reply",
            "<anchor-msg@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )
    assert _handle_draft_reply(args_draft) == 0
    capsys.readouterr()

    draft_files = list((tmp_path / "outbound" / "drafts").glob("*.eml"))
    draft_id = draft_files[0].stem
    initial_bytes = draft_files[0].read_bytes()
    initial_fingerprint = draft_fingerprint(initial_bytes)

    mock_smtp = MagicMock(return_value=({}, None))
    monkeypatch.setattr("mailattic.outbound._smtp_submit", mock_smtp)
    monkeypatch.setattr("mailattic.outbound._append_sent_copy", MagicMock())

    args_send = build_parser().parse_args(
        [
            "send-draft",
            draft_id,
            "--fingerprint",
            initial_fingerprint,
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    send_exit = _handle_send_draft(args_send)
    assert send_exit == 0

    send_stdout = capsys.readouterr().out
    assert "Recipient check:" not in send_stdout


def test_explicit_own_address_marked_at_draft_and_send_but_bcc_self_never(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #46: every caller-given address is marked at draft time and again at the
    # send review; only the automatic BCC-self is never listed (even when the
    # caller also passes it as --bcc, it is the same single Bcc entry).
    db_path = initialize_database(tmp_path / "evidence.db")
    _seed_custom_message(
        db_path,
        message_id="<seed-msg@mailattic.test>",
        sender_email="client@example.com",
        to_emails=[SMTP_USER],
    )
    body_file = tmp_path / "body.txt"
    body_file.write_text("Note to self", encoding="utf-8")

    args_draft = build_parser().parse_args(
        [
            "draft-new",
            "--to",
            SMTP_USER,
            "--bcc",
            SMTP_USER,
            "--subject",
            "Memo",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )
    assert _handle_draft_new(args_draft) == 0
    draft_stdout = capsys.readouterr().out
    assert draft_stdout.count(f"{SMTP_USER}: seen") == 1

    draft_path = next((tmp_path / "outbound" / "drafts").glob("*.eml"))
    fingerprint = draft_fingerprint(draft_path.read_bytes())

    monkeypatch.setattr(
        "mailattic.outbound._smtp_submit", MagicMock(return_value=({}, None))
    )
    monkeypatch.setattr("mailattic.outbound._append_sent_copy", MagicMock())
    args_send = build_parser().parse_args(
        [
            "send-draft",
            draft_path.stem,
            "--fingerprint",
            fingerprint,
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )
    assert _handle_send_draft(args_send) == 0
    send_stdout = capsys.readouterr().out
    assert send_stdout.count(f"{SMTP_USER}: seen") == 1


def test_explicit_bcc_own_address_is_bcc_self_and_not_marked_at_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "evidence.db")
    body_file = tmp_path / "body.txt"
    body_file.write_text("Hi", encoding="utf-8")
    args_draft = build_parser().parse_args(
        [
            "draft-new",
            "--to",
            "partner@example.com",
            "--bcc",
            SMTP_USER.upper(),
            "--subject",
            "Hello",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )
    assert _handle_draft_new(args_draft) == 0
    stdout = capsys.readouterr().out
    assert "partner@example.com: never seen" in stdout
    assert f"{SMTP_USER.upper()}:" not in stdout


def test_send_batch_shows_recomputed_marks_per_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = initialize_database(tmp_path / "evidence.db")
    _seed_custom_message(
        db_path,
        message_id="<anchor-msg@mailattic.test>",
        sender_email="client@example.com",
        to_emails=[SMTP_USER],
    )

    body_file = tmp_path / "body.txt"
    body_file.write_text("Hello reply", encoding="utf-8")

    # Draft 1: with extra explicit recipient
    args_draft1 = build_parser().parse_args(
        [
            "draft-reply",
            "<anchor-msg@mailattic.test>",
            "--to",
            "batch_contact@testbiz.com",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )
    assert _handle_draft_reply(args_draft1) == 0
    capsys.readouterr()

    draft_files = sorted((tmp_path / "outbound" / "drafts").glob("*.eml"))
    draft1_id = draft_files[0].stem
    fp1 = draft_fingerprint(draft_files[0].read_bytes())

    # Seed batch_contact@testbiz.com into DB so it flips to seen at send time
    _seed_custom_message(
        db_path,
        message_id="<batch-seed-msg@mailattic.test>",
        sender_email="batch_contact@testbiz.com",
        to_emails=[SMTP_USER],
    )

    mock_smtp = MagicMock(return_value=({}, None))
    monkeypatch.setattr("mailattic.outbound._smtp_submit", mock_smtp)
    monkeypatch.setattr("mailattic.outbound._append_sent_copy", MagicMock())

    args_batch = build_parser().parse_args(
        [
            "send-batch",
            f"{draft1_id}={fp1}",
            "--database",
            str(db_path),
            "--env-file",
            str(_placeholder_env_file(tmp_path)),
        ]
    )

    batch_exit = _handle_send_batch(args_batch)
    assert batch_exit == 0

    batch_stdout = capsys.readouterr().out
    assert "Recipient check:" in batch_stdout
    assert "batch_contact@testbiz.com: seen" in batch_stdout

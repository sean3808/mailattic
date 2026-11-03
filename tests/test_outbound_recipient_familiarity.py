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
    _handle_draft_reply,
    _handle_send_batch,
    _handle_send_draft,
)
from mailattic.query import (
    PUBLIC_WEBMAIL_DOMAINS,
    check_recipient_familiarity,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

SMTP_USER = "sean3808@mailattic.test"
GMAIL = "g" + "mail.com"  # split: see PUBLIC_WEBMAIL_DOMAINS in query.py
PLACEHOLDER_SMTP_SETTINGS = (
    "SMTP_HOST=smtp.example.com\n"
    "SMTP_PORT=465\n"
    f"SMTP_USER={SMTP_USER}\n"
    "SMTP_PASSWORD=placeholder\n"
)


def _placeholder_env_file(tmp_path: Path) -> Path:
    env_file = tmp_path / "placeholder.env"
    if not env_file.exists():
        env_file.write_text(PLACEHOLDER_SMTP_SETTINGS, encoding="utf-8")
    return env_file


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


def test_public_webmail_domains_contains_all_pinned_domains() -> None:
    expected = {
        GMAIL,
        "googlemail.com",
        "yahoo.com",
        "yahoo.com.tw",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "qq.com",
        "163.com",
        "126.com",
    }
    assert expected == PUBLIC_WEBMAIL_DOMAINS


def test_check_recipient_familiarity_empty_list_returns_empty(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "scratch.db")
    results = check_recipient_familiarity(db_path, [])
    assert results == []


def test_check_recipient_familiarity_seen_roles_and_mixed_case(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "scratch.db")
    _seed_custom_message(
        db_path,
        message_id="<msg-seed-1@mailattic.test>",
        sender_email="SeNdEr@ExAmPlE.cOm",
        to_emails=["To_UsEr@ExAmPlE.cOm"],
        cc_emails=["Cc_UsEr@ExAmPlE.cOm"],
        bcc_emails=["Bcc_UsEr@ExAmPlE.cOm"],
    )

    addresses_to_check = [
        "sender@example.com",
        "SENDER@EXAMPLE.COM",
        "to_user@example.com",
        "TO_USER@EXAMPLE.COM",
        "cc_user@example.com",
        "CC_USER@EXAMPLE.COM",
        "bcc_user@example.com",
        "BCC_USER@EXAMPLE.COM",
    ]
    results = check_recipient_familiarity(db_path, addresses_to_check)
    assert len(results) == len(addresses_to_check)
    for res in results:
        assert res.seen is True
        assert res.mark == "seen"


def test_check_recipient_familiarity_never_seen_and_domain_seen(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "scratch.db")
    _seed_custom_message(
        db_path,
        message_id="<msg-seed-2@mailattic.test>",
        sender_email="colleague@knowncorp.test",
    )

    addresses_to_check = [
        "newperson@knowncorp.test",
        "stranger@never-seen-domain.invalid",
    ]
    results = check_recipient_familiarity(db_path, addresses_to_check)
    assert len(results) == 2

    known_corp_res = results[0]
    assert known_corp_res.address == "newperson@knowncorp.test"
    assert known_corp_res.seen is False
    assert known_corp_res.domain_seen is True
    assert known_corp_res.mark == "never seen (domain seen)"

    stranger_res = results[1]
    assert stranger_res.address == "stranger@never-seen-domain.invalid"
    assert stranger_res.seen is False
    assert stranger_res.domain_seen is False
    assert stranger_res.mark == "never seen"


def test_check_recipient_familiarity_webmail_no_domain_hint(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "scratch.db")
    _seed_custom_message(
        db_path,
        message_id="<msg-seed-3@mailattic.test>",
        sender_email=f"someone@{GMAIL}",
        to_emails=["other@163.com"],
    )

    addresses_to_check = [
        f"brand_new_user@{GMAIL}",
        "brand_new_user@163.com",
    ]
    results = check_recipient_familiarity(db_path, addresses_to_check)
    assert len(results) == 2
    for res in results:
        assert res.seen is False
        assert res.domain_seen is False
        assert res.mark == "never seen"


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
        ]
    )

    exit_code = _handle_draft_reply(args)
    assert exit_code == 0

    stdout = capsys.readouterr().out
    assert "Recipient check:" in stdout
    assert f"{new_user}: never seen" in stdout
    assert "domain seen" not in stdout


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


def test_check_recipient_familiarity_quoted_local_part_at_domain(
    tmp_path: Path,
) -> None:
    db_path = initialize_database(tmp_path / "scratch.db")
    _seed_custom_message(
        db_path,
        message_id="<msg-seed-quoted@mailattic.test>",
        sender_email="colleague@knowncorp.test",
    )

    addresses_to_check = [
        '"user@corp"@knowncorp.test',
    ]
    results = check_recipient_familiarity(db_path, addresses_to_check)
    assert len(results) == 1
    assert results[0].seen is False
    assert results[0].domain_seen is True
    assert results[0].mark == "never seen (domain seen)"

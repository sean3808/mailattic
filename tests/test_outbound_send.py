from __future__ import annotations

import argparse
import json
import smtplib
import sys
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mailattic.cli import build_parser
from mailattic.models import MailboxParticipant, NormalizedMessage
from mailattic.outbound import (
    SendRefused,
    SmtpConfig,
    _check_from_invariant,
    _check_tty_authorization,
    _marker_path,
    _write_marker,
    draft_fingerprint,
    send_batch,
    send_draft,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)
from tests.test_outbound_draft_attach import _strip_bcc_and_use_crlf

SMTP_USER = "sean3808@mailattic.test"


class _FakeStdin:
    def __init__(self, line: str, *, tty: bool) -> None:
        self._line = line
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        return self._line


class _FakeStdout:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty
        self.writes: list[str] = []

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        return None


def test_non_tty_stdin_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("YES\n", tty=False))
    monkeypatch.setattr(sys, "stdout", _FakeStdout(tty=True))

    with pytest.raises(SendRefused, match="interactive authorization required"):
        _check_tty_authorization("summary")


def test_non_tty_stdout_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("YES\n", tty=True))
    monkeypatch.setattr(sys, "stdout", _FakeStdout(tty=False))

    with pytest.raises(SendRefused, match="interactive authorization required"):
        _check_tty_authorization("summary")


def test_empty_confirmation_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("\n", tty=True))
    monkeypatch.setattr(sys, "stdout", _FakeStdout(tty=True))

    with pytest.raises(SendRefused, match="confirmation was empty"):
        _check_tty_authorization("summary")


def test_typed_confirmation_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("YES\n", tty=True))
    monkeypatch.setattr(sys, "stdout", _FakeStdout(tty=True))

    assert _check_tty_authorization("summary") == "YES"


def test_send_cli_has_no_authorization_bypass_beyond_fingerprint() -> None:
    # Contract #2 (CLI form, #195): a send needs a matching draft fingerprint or
    # a real-TTY typed authorization. The fingerprint is the ONLY non-interactive
    # path, so the send commands' option set is pinned: any new flag trips this.
    parser = build_parser()
    subcommands = next(
        action.choices
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    base = {"-h", "--help", "--database", "--env-file"}
    expected = {"send-draft": base | {"--fingerprint"}, "send-batch": base}

    for name, options in expected.items():
        actual = {
            option
            for action in subcommands[name]._actions
            for option in action.option_strings
        }
        assert actual == options, name

    for flag in ("--confirm", "--auto", "--yes", "--non-interactive"):
        with pytest.raises(SystemExit):
            parser.parse_args(["send-draft", "draft-1", flag])


def test_non_tty_without_fingerprint_refused_listing_both_ways_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<no-fingerprint@mailattic.test>"
    _write_draft(tmp_path, "draft-nofp", message_id)
    mock_smtp_ssl = MagicMock()
    monkeypatch.setattr("mailattic.outbound.smtplib.SMTP_SSL", mock_smtp_ssl)
    # A non-TTY caller that would happily "type" a confirmation if asked.
    monkeypatch.setattr(sys, "stdin", _FakeStdin("YES\n", tty=False))
    monkeypatch.setattr(sys, "stdout", _FakeStdout(tty=False))

    with pytest.raises(SendRefused) as refused:
        send_draft("draft-nofp", db_path, _smtp_config(), {})

    message = str(refused.value)
    assert "interactive authorization required" in message
    assert "--fingerprint" in message
    assert "terminal" in message
    mock_smtp_ssl.assert_not_called()
    assert not _marker_path(db_path, message_id).exists()


def test_fingerprint_mismatch_refused_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<fingerprint-mismatch@mailattic.test>"
    draft_path = _write_draft(tmp_path, "draft-changed", message_id)
    reviewed = draft_fingerprint(draft_path.read_bytes())
    draft_path.write_bytes(
        draft_path.read_bytes().replace(b"Test outbound", b"Test outbounD")
    )
    mock_smtp_ssl = MagicMock()
    monkeypatch.setattr("mailattic.outbound.smtplib.SMTP_SSL", mock_smtp_ssl)
    # Real-TTY fakes that would authorize: the refusal must come from the
    # fingerprint check, not from falling through to the TTY gate.
    monkeypatch.setattr(sys, "stdin", _FakeStdin("YES\n", tty=True))
    monkeypatch.setattr(sys, "stdout", _FakeStdout(tty=True))

    with pytest.raises(SendRefused, match="fingerprint mismatch"):
        send_draft("draft-changed", db_path, _smtp_config(), {}, fingerprint=reviewed)

    mock_smtp_ssl.assert_not_called()
    assert not _marker_path(db_path, message_id).exists()


def test_matching_fingerprint_sends_without_tty_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<fingerprint-match@mailattic.test>"
    draft_path = _write_draft(tmp_path, "draft-match", message_id)
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        MagicMock(side_effect=AssertionError("TTY prompt must not run")),
    )

    reviewed_bytes = draft_path.read_bytes()
    result = send_draft(
        "draft-match",
        db_path,
        _smtp_config(),
        {},
        fingerprint=draft_fingerprint(reviewed_bytes),
    )

    assert result["message_id"] == message_id
    mock_smtp.sendmail.assert_called_once()
    # The bytes handed to SMTP are the fingerprinted draft bytes minus Bcc,
    # with CRLF line endings.
    assert mock_smtp.sendmail.call_args.args[2] == _strip_bcc_and_use_crlf(
        reviewed_bytes
    )
    marker = json.loads(_marker_path(db_path, message_id).read_text(encoding="utf-8"))
    assert marker["status"] == "sent"


def test_batch_fingerprint_mismatch_refuses_only_that_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    kept = _write_draft(tmp_path, "draft-kept", "<batch-kept@mailattic.test>")
    changed = _write_draft(tmp_path, "draft-changed", "<batch-changed@mailattic.test>")
    fingerprints = {
        "draft-kept": draft_fingerprint(kept.read_bytes()),
        "draft-changed": draft_fingerprint(changed.read_bytes()),
    }
    changed.write_bytes(changed.read_bytes().replace(b"Hello", b"HellO"))
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr("mailattic.outbound._wait_for_batch_interval", lambda _s: None)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        MagicMock(side_effect=AssertionError("TTY prompt must not run")),
    )

    results = send_batch(
        ["draft-changed", "draft-kept"],
        db_path,
        _smtp_config(),
        {},
        fingerprints=fingerprints,
    )

    by_id = {r["draft_id"]: r for r in results}
    assert by_id["draft-kept"]["outcome"] == "sent"
    assert by_id["draft-changed"]["outcome"] == "failed"
    assert "fingerprint mismatch" in by_id["draft-changed"]["reason"]
    assert mock_smtp.sendmail.call_count == 1
    assert not _marker_path(db_path, "<batch-changed@mailattic.test>").exists()


def test_from_mismatch_rejected() -> None:
    msg = EmailMessage()
    msg["From"] = "other@example.com"

    with pytest.raises(SendRefused, match="anti-spoofing invariant"):
        _check_from_invariant(msg, SMTP_USER)


def test_from_case_normalized_passes() -> None:
    msg = EmailMessage()
    msg["From"] = "SEAN3808@MAILATTIC.TEST"

    _check_from_invariant(msg, SMTP_USER)


def test_multiple_from_rejected() -> None:
    msg = EmailMessage()
    msg["From"] = "a@example.com, b@example.com"

    with pytest.raises(SendRefused, match="multiple From"):
        _check_from_invariant(msg, SMTP_USER)


def test_sender_header_rejected() -> None:
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["Sender"] = SMTP_USER

    with pytest.raises(SendRefused, match="Sender header"):
        _check_from_invariant(msg, SMTP_USER)


def test_resent_from_header_rejected() -> None:
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["Resent-From"] = SMTP_USER

    with pytest.raises(SendRefused, match="Resent-From header"):
        _check_from_invariant(msg, SMTP_USER)


def test_envelope_sender_is_auth_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _write_draft(tmp_path, "draft-1", "<send-success@mailattic.test>")
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )

    send_draft("draft-1", db_path, _smtp_config(), {})

    mock_smtp.sendmail.assert_called_once()
    assert mock_smtp.sendmail.call_args.args[0] == SMTP_USER


def test_bcc_not_in_transmitted_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    draft_path = _write_draft(tmp_path, "draft-bcc", "<bcc@mailattic.test>")
    draft_bytes = draft_path.read_bytes()
    assert b"Bcc:" in draft_bytes
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )

    send_draft("draft-bcc", db_path, _smtp_config(), {})

    mock_smtp.sendmail.assert_called_once()
    from_addr, to_addrs, transmitted_bytes, _opts = mock_smtp.sendmail.call_args.args
    assert from_addr == SMTP_USER
    assert SMTP_USER in to_addrs
    assert b"Bcc:" not in transmitted_bytes
    assert transmitted_bytes == _strip_bcc_and_use_crlf(draft_bytes)


def test_public_api_has_no_exposed_raw_smtp() -> None:
    import mailattic.outbound as outbound_module

    assert not hasattr(outbound_module, "smtp_submit")
    assert not hasattr(outbound_module, "SmtpSubmit")
    assert hasattr(outbound_module, "send_draft")


def test_send_refused_before_smtp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _write_draft(tmp_path, "draft-refused", "<refused@mailattic.test>")
    mock_smtp_ssl = MagicMock()
    monkeypatch.setattr("mailattic.outbound.smtplib.SMTP_SSL", mock_smtp_ssl)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        MagicMock(side_effect=SendRefused("blocked")),
    )

    with pytest.raises(SendRefused, match="blocked"):
        send_draft("draft-refused", db_path, _smtp_config(), {})

    mock_smtp_ssl.assert_not_called()


def test_already_sent_marker_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<already-sent@mailattic.test>"
    _write_draft(tmp_path, "draft-sent", message_id)
    _write_marker(db_path, message_id, "draft-sent", "sent")
    mock_smtp_ssl = MagicMock()
    monkeypatch.setattr("mailattic.outbound.smtplib.SMTP_SSL", mock_smtp_ssl)

    with pytest.raises(SendRefused, match="already sent"):
        send_draft("draft-sent", db_path, _smtp_config(), {})

    mock_smtp_ssl.assert_not_called()


def test_sending_marker_crash_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<already-sending@mailattic.test>"
    _write_draft(tmp_path, "draft-sending", message_id)
    _write_marker(db_path, message_id, "draft-sending", "sending")
    mock_smtp_ssl = MagicMock()
    monkeypatch.setattr("mailattic.outbound.smtplib.SMTP_SSL", mock_smtp_ssl)

    with pytest.raises(SendRefused, match="already sending"):
        send_draft("draft-sending", db_path, _smtp_config(), {})

    mock_smtp_ssl.assert_not_called()


def test_smtp_failure_no_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<smtp-failure@mailattic.test>"
    _write_draft(tmp_path, "draft-failure", message_id)
    mock_smtp = _patch_smtp(monkeypatch)
    mock_smtp.sendmail.side_effect = smtplib.SMTPException("boom")
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )

    with pytest.raises(SendRefused, match="SMTP submission failed"):
        send_draft("draft-failure", db_path, _smtp_config(), {})

    mock_smtp.sendmail.assert_called_once()
    marker = json.loads(_marker_path(db_path, message_id).read_text(encoding="utf-8"))
    assert marker["status"] == "sending"


def test_outbound_then_imap_dedup(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = "<test-dedup-abc@mailattic.test>"

    conn = connect_database(db_path)
    try:
        r1 = persist_message(conn, _make_outbound_msg(msg_id, "outbound-bes"))
        conn.commit()
        assert r1.inserted is True

        r2 = persist_message(
            conn,
            _make_outbound_msg(msg_id, "imap:sean3808@mailattic.test:INBOX"),
        )
        conn.commit()
        assert r2.inserted is False

        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id=?",
            [msg_id],
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_imap_then_outbound_dedup(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    msg_id = "<test-dedup-xyz@mailattic.test>"

    conn = connect_database(db_path)
    try:
        r1 = persist_message(
            conn,
            _make_outbound_msg(msg_id, "imap:sean3808@mailattic.test:INBOX"),
        )
        conn.commit()
        assert r1.inserted is True

        r2 = persist_message(conn, _make_outbound_msg(msg_id, "outbound-bes"))
        conn.commit()
        assert r2.inserted is False

        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id=?",
            [msg_id],
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def _write_draft(tmp_path: Path, draft_id: str, message_id: str) -> Path:
    draft_dir = tmp_path / "outbound" / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = "Client <client@example.com>"
    msg["Cc"] = "Colleague <colleague@example.com>"
    msg["Bcc"] = SMTP_USER
    msg["Subject"] = "Test outbound"
    msg["Message-ID"] = message_id
    msg["In-Reply-To"] = "<parent@mailattic.test>"
    msg["References"] = "<root@mailattic.test> <parent@mailattic.test>"
    msg["Date"] = "Thu, 28 May 2026 10:00:00 +0000"
    msg.set_content("Hello", charset="utf-8")
    draft_path = draft_dir / f"{draft_id}.eml"
    draft_path.write_bytes(msg.as_bytes())
    return draft_path


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


def _make_outbound_msg(message_id: str, pst_source: str) -> NormalizedMessage:
    return NormalizedMessage(
        subject="Test outbound",
        sender=MailboxParticipant(display_name="Sean", email=SMTP_USER),
        to_recipients=(
            MailboxParticipant(display_name=None, email="client@example.com"),
        ),
        cc_recipients=(),
        bcc_recipients=(MailboxParticipant(display_name=None, email=SMTP_USER),),
        sent_at=datetime(2026, 5, 28, 10, 0, 0, tzinfo=UTC),
        received_at=datetime(2026, 5, 28, 10, 0, 0, tzinfo=UTC),
        body_text="Hello",
        attachments=(),
        message_id=message_id,
        in_reply_to=None,
        references=None,
        pst_source=pst_source,
        folder_path="Sent",
        logical_locator=f"outbound:test-{message_id}",
        source_input_mode="outbound",
        source_requested_from="bes:send-draft",
    )

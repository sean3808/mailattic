"""Issue #230: once DATA is accepted, a failing QUIT must not undo the send.

The real stdlib ``sendmail`` / ``__exit__`` run against a
synthetic transport (no network); only the command replies are faked. The
stdlib ``__exit__`` raises ``SMTPResponseException`` on a non-221 QUIT reply and
swallows ``SMTPServerDisconnected``.
"""

from __future__ import annotations

import argparse
import asyncio
import smtplib
from email.message import EmailMessage
from pathlib import Path

import pytest
from mcp import Client

from mailattic import outbound as o
from mailattic import outbound_commands
from mailattic.mcp_server import create_server
from mailattic.storage import connect_database, initialize_database

USER = "sean3808@mailattic.test"
CONFIG = o.SmtpConfig("invalid.local", 465, USER, "dummy")
BEHAVIOR: dict[str, object] = {}


class FakeSMTP(smtplib.SMTP_SSL):
    """Real __exit__ / sendmail; only replies are scripted."""

    def __init__(self, *args, **kwargs):
        self.does_esmtp = False

    def ehlo_or_helo_if_needed(self):
        pass

    def login(self, *args):
        pass

    def mail(self, *args):
        return 250, b"OK"

    def rcpt(self, recipient, *args):
        if recipient == "refused@example.com":
            return 450, b"too many recipients"
        return 250, b"OK"

    def data(self, *args):
        reply = BEHAVIOR.get("data", (250, b"queued"))
        if isinstance(reply, Exception):
            raise reply
        return reply

    def docmd(self, command, *args):
        assert command == "QUIT"
        return BEHAVIOR.get("quit", (221, b"bye"))

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _transport(monkeypatch):
    BEHAVIOR.clear()
    monkeypatch.setattr(o.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(o, "_append_sent_copy", lambda **kwargs: None)
    yield
    BEHAVIOR.clear()


def _draft(tmp_path: Path, subdir: str = "drafts", to: str = "client@example.com"):
    db = initialize_database(tmp_path / "t.db")
    directory = tmp_path / "outbound" / subdir
    directory.mkdir(parents=True)
    msg = EmailMessage()
    msg["From"] = USER
    msg["To"] = to
    msg["Message-ID"] = "<d1@audit.invalid>"
    msg["Subject"] = "quit after accept"
    msg.set_content("body")
    raw = msg.as_bytes()
    (directory / "d1.eml").write_bytes(raw)
    return db, raw


def _send(db, raw):
    return o.send_draft("d1", db, CONFIG, {}, fingerprint=o.draft_fingerprint(raw))


def _db_count(db) -> int:
    conn = connect_database(db, create=False, read_only=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


def test_quit_421_after_data_accepted_is_recorded_as_sent(tmp_path):
    BEHAVIOR["quit"] = (421, b"service shutting down")
    db, raw = _draft(tmp_path)
    result = _send(db, raw)

    assert result["message_id"] == "<d1@audit.invalid>"
    assert "421" in result["close_warning"]
    assert "do not resend" in result["close_warning"].lower()
    assert o._read_marker(db, "<d1@audit.invalid>")["status"] == "sent"
    assert _db_count(db) == 1
    with pytest.raises(o.SendRefused, match="already sent"):
        _send(db, raw)


def test_normal_quit_221_has_no_close_warning(tmp_path):
    db, raw = _draft(tmp_path)
    assert _send(db, raw) == {"message_id": "<d1@audit.invalid>", "draft_id": "d1"}


def test_quit_failure_keeps_partial_refusal_list(tmp_path):
    BEHAVIOR["quit"] = (421, b"service shutting down")
    db, raw = _draft(tmp_path, to="client@example.com, refused@example.com")
    result = _send(db, raw)

    assert result["delivery"] == "partial"
    assert result["refused"][0]["address"] == "refused@example.com"
    assert "421" in result["close_warning"]
    marker = o._read_marker(db, "<d1@audit.invalid>")
    assert marker["status"] == "sent" and marker["refused"] == result["refused"]


def test_definite_failure_at_data_stays_failed_and_unrecorded(tmp_path):
    BEHAVIOR["data"] = (554, b"rejected")
    BEHAVIOR["quit"] = (421, b"service shutting down")
    db, raw = _draft(tmp_path)
    with pytest.raises(o.SendRefused, match="submission failed"):
        _send(db, raw)
    assert o._read_marker(db, "<d1@audit.invalid>")["status"] == "sending"
    assert _db_count(db) == 0


def test_lost_data_response_stays_unknown_outcome(tmp_path):
    BEHAVIOR["data"] = smtplib.SMTPServerDisconnected(
        "Connection unexpectedly closed: timed out"
    )
    db, raw = _draft(tmp_path)
    with pytest.raises(o.SendRefused, match="may or may not have accepted"):
        _send(db, raw)
    assert o._read_marker(db, "<d1@audit.invalid>")["status"] == "sending"
    assert _db_count(db) == 0


def test_batch_row_is_sent_and_carries_close_warning(tmp_path):
    BEHAVIOR["quit"] = (421, b"service shutting down")
    db, raw = _draft(tmp_path)
    [row] = o.send_batch(
        ["d1"], db, CONFIG, {}, fingerprints={"d1": o.draft_fingerprint(raw)}
    )
    assert row["outcome"] == "sent"
    assert "421" in row["close_warning"]


def test_cli_send_draft_exits_0_and_warns_on_stderr(tmp_path, monkeypatch, capsys):
    BEHAVIOR["quit"] = (421, b"service shutting down")
    db, raw = _draft(tmp_path)
    monkeypatch.setattr(
        outbound_commands, "_load_smtp_env", lambda _env_file: (CONFIG, {})
    )
    args = argparse.Namespace(
        draft_id="d1", database=db, env_file=None, fingerprint=o.draft_fingerprint(raw)
    )
    assert outbound_commands._handle_send_draft(args) == 0
    captured = capsys.readouterr()
    assert "Sent: message_id=<d1@audit.invalid>" in captured.out
    assert "421" in captured.err


def test_mcp_mail_send_reports_sent_with_close_warning(tmp_path, monkeypatch):
    BEHAVIOR["quit"] = (421, b"service shutting down")
    db, raw = _draft(tmp_path, subdir="mcp-drafts")
    env_file = tmp_path / "smtp.env"
    env_file.write_text(
        f"SMTP_HOST=h\nSMTP_PORT=465\nSMTP_USER={USER}\nSMTP_PASSWORD=x\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(o.ENV_VAR_SMTP_ENV_FILE, str(env_file))
    server = create_server(database_path=db)

    async def _call():
        async with Client(server) as client:
            return await client.call_tool(
                "mail_send",
                {"draft_id": "d1", "fingerprint": o.draft_fingerprint(raw)},
            )

    res = asyncio.run(_call())
    assert not res.is_error
    assert res.structured_content["status"] == "sent"
    assert "421" in res.structured_content["close_warning"]

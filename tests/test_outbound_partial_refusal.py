"""Issue #224: SMTP partial RCPT refusal must not be reported as a clean send.

The real stdlib ``sendmail`` runs against a synthetic
in-process transport (no network); only the SMTP command replies are faked.
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
REASON = b"4.7.1 too many recipients"
REFUSE: set[str] = set()


class FakeSMTP(smtplib.SMTP_SSL):
    def __init__(self, *args, **kwargs):
        self.does_esmtp = False

    def __exit__(self, *args):
        return False

    def ehlo_or_helo_if_needed(self):
        pass

    def login(self, *args):
        pass

    def mail(self, *args):
        return 250, b"OK"

    def rcpt(self, recipient, *args):
        return (450, REASON) if recipient in REFUSE else (250, b"OK")

    def data(self, *args):
        return 250, b"queued"


@pytest.fixture(autouse=True)
def _transport(monkeypatch):
    REFUSE.clear()
    monkeypatch.setattr(o.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(o, "_append_sent_copy", lambda **kwargs: None)
    yield
    REFUSE.clear()


def _draft(tmp_path: Path, draft_id: str = "d1", subdir: str = "drafts"):
    db = initialize_database(tmp_path / "t.db")
    directory = tmp_path / "outbound" / subdir
    directory.mkdir(parents=True)
    msg = EmailMessage()
    msg["From"] = USER
    msg["To"] = "accepted@example.com, refused@example.com"
    msg["Bcc"] = USER
    msg["Message-ID"] = f"<{draft_id}@audit.invalid>"
    msg["Subject"] = "partial rcpt"
    msg.set_content("body")
    raw = msg.as_bytes()
    (directory / f"{draft_id}.eml").write_bytes(raw)
    return db, raw, f"<{draft_id}@audit.invalid>"


CONFIG = o.SmtpConfig("invalid.local", 465, USER, "dummy")


def _send(db, raw, draft_id="d1"):
    return o.send_draft(draft_id, db, CONFIG, {}, fingerprint=o.draft_fingerprint(raw))


def test_all_accepted_result_has_no_refusal_fields(tmp_path):
    db, raw, mid = _draft(tmp_path)
    assert _send(db, raw) == {"message_id": mid, "draft_id": "d1"}
    assert "refused" not in o._read_marker(db, mid)


def test_partial_to_refusal_is_reported_and_stays_resend_guarded(tmp_path):
    REFUSE.add("refused@example.com")
    db, raw, mid = _draft(tmp_path)
    result = _send(db, raw)

    assert result["delivery"] == "partial"
    assert result["refused"] == [
        {"address": "refused@example.com", "code": 450, "reason": REASON.decode()}
    ]
    assert result["accepted"] == ["accepted@example.com", USER]
    assert "do not resend" in result["warning"].lower()
    marker = o._read_marker(db, mid)
    assert marker["status"] == "sent"
    assert marker["refused"] == result["refused"]
    with pytest.raises(o.SendRefused, match="already sent"):
        _send(db, raw)
    conn = connect_database(db, create=False, read_only=True)
    try:
        # DB keeps the intended recipients; refusal evidence lives in the marker.
        assert (
            conn.execute("SELECT COUNT(*) FROM message_recipients").fetchone()[0] == 3
        )
    finally:
        conn.close()


def test_partial_refusal_survives_post_submit_failure(tmp_path, monkeypatch):
    # SMTP partially accepted, then DB persist blew up: the marker stays
    # 'sending', so the error itself must carry who was refused.
    REFUSE.add("refused@example.com")
    db, raw, mid = _draft(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(o, "persist_message", _boom)
    with pytest.raises(Exception, match="refused@example.com") as excinfo:
        _send(db, raw)
    assert "may already be delivered" in str(excinfo.value)
    assert o._read_marker(db, mid)["status"] == "sending"


def test_bcc_self_only_refusal_is_reported(tmp_path):
    REFUSE.add(USER)
    db, raw, _mid = _draft(tmp_path)
    result = _send(db, raw)
    assert [r["address"] for r in result["refused"]] == [USER]
    assert result["accepted"] == ["accepted@example.com", "refused@example.com"]


def test_all_refused_raises_with_every_address_code_and_reason(tmp_path):
    REFUSE.update({"accepted@example.com", "refused@example.com", USER})
    db, raw, mid = _draft(tmp_path)
    with pytest.raises(o.SendRefused) as excinfo:
        _send(db, raw)
    text = str(excinfo.value)
    for needle in ("accepted@example.com", "refused@example.com", USER, "450"):
        assert needle in text
    assert REASON.decode() in text
    assert o._read_marker(db, mid)["status"] == "sending"


def test_batch_outcome_partial_carries_refusals(tmp_path):
    REFUSE.add("refused@example.com")
    db, raw, mid = _draft(tmp_path)
    [row] = o.send_batch(
        ["d1"], db, CONFIG, {}, fingerprints={"d1": o.draft_fingerprint(raw)}
    )
    assert row["outcome"] == "partial"
    assert row["message_id"] == mid
    assert row["refused"][0]["code"] == 450
    assert "do not resend" in row["warning"].lower()


def test_cli_send_draft_partial_exits_3_and_names_refusals(
    tmp_path, monkeypatch, capsys
):
    REFUSE.add("refused@example.com")
    db, raw, mid = _draft(tmp_path)
    monkeypatch.setattr(
        outbound_commands, "_load_smtp_env", lambda _env_file: (CONFIG, {})
    )
    args = argparse.Namespace(
        draft_id="d1",
        database=db,
        env_file=None,
        fingerprint=o.draft_fingerprint(raw),
    )
    assert outbound_commands._handle_send_draft(args) == 3
    captured = capsys.readouterr()
    assert "Partially sent" in captured.out and mid in captured.out
    assert "refused@example.com" in captured.err and "450" in captured.err


def test_cli_send_batch_partial_exits_nonzero(tmp_path, monkeypatch, capsys):
    REFUSE.add("refused@example.com")
    db, raw, _mid = _draft(tmp_path)
    monkeypatch.setattr(
        outbound_commands, "_load_smtp_env", lambda _env_file: (CONFIG, {})
    )
    args = argparse.Namespace(
        draft_id=[f"d1={o.draft_fingerprint(raw)}"], database=db, env_file=None
    )
    assert outbound_commands._handle_send_batch(args) == 1
    captured = capsys.readouterr()
    assert "Partial: draft_id=d1" in captured.err
    assert "refused@example.com" in captured.err


def test_mcp_mail_send_reports_partial_not_sent(tmp_path, monkeypatch):
    REFUSE.add("refused@example.com")
    db, raw, _mid = _draft(tmp_path, subdir="mcp-drafts")
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
    data = res.structured_content
    assert data["status"] == "partial"
    assert data["refused"][0]["address"] == "refused@example.com"
    assert data["refused"][0]["code"] == 450
    assert "do not resend" in data["warning"].lower()

"""#227: the SMTP connection has a finite socket timeout, and a stalled stage
is reported as "outcome unknown" instead of hanging or looking safe to retry.

No real network: ``smtplib.SMTP_SSL`` is replaced by a mock that raises
``TimeoutError`` (what a real socket timeout raises) at a chosen stage.
"""

from __future__ import annotations

import asyncio
import json
import math
import socket
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mcp import Client

import mailattic.outbound as outbound_module
from mailattic.mcp_server import create_server
from mailattic.models import MailboxParticipant, NormalizedMessage
from mailattic.outbound import (
    ENV_VAR_SMTP_ENV_FILE,
    SendRefused,
    SmtpConfig,
    _marker_path,
    send_batch,
    send_draft,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

SMTP_USER = "sean3808@mailattic.test"
STAGES = ("greeting", "auth", "data")


def _config() -> SmtpConfig:
    return SmtpConfig("smtp.example.com", 465, SMTP_USER, "secret")


def _write_draft(tmp_path: Path, draft_id: str, message_id: str) -> None:
    drafts = tmp_path / "outbound" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = "client@example.com"
    msg["Subject"] = "t"
    msg["Message-ID"] = message_id
    msg["Date"] = "Thu, 28 May 2026 10:00:00 +0000"
    msg.set_content("Hello")
    (drafts / f"{draft_id}.eml").write_bytes(msg.as_bytes())


def _patch_smtp(
    monkeypatch: pytest.MonkeyPatch, stage: str | None, *, times: int = 1
) -> MagicMock:
    """Patch SMTP_SSL; the first ``times`` uses stall at ``stage``."""
    smtp = MagicMock()
    ssl_ctor = MagicMock()
    ssl_ctor.return_value.__enter__.return_value = smtp
    if stage == "greeting":
        ssl_ctor.side_effect = [TimeoutError("timed out")] * times + [
            ssl_ctor.return_value
        ] * 5
    # After connect, smtplib turns a socket timeout into SMTPServerDisconnected
    # (see smtplib.SMTP.getreply / send) -- mock what really surfaces.
    stalled = outbound_module.smtplib.SMTPServerDisconnected(
        "Connection unexpectedly closed: timed out"
    )
    if stage == "auth":
        smtp.login.side_effect = [stalled] * times + [None] * 5
    elif stage == "data":
        smtp.sendmail.side_effect = [stalled] * times + [{}] * 5
    monkeypatch.setattr(outbound_module.smtplib, "SMTP_SSL", ssl_ctor)
    return ssl_ctor


def _status(db: Path, mid: str) -> str:
    return json.loads(_marker_path(db, mid).read_text(encoding="utf-8"))["status"]


def test_smtp_ssl_gets_finite_positive_timeout_regardless_of_global_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert socket.getdefaulttimeout() is None
    ssl_ctor = _patch_smtp(monkeypatch, None)
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = "client@example.com"
    msg.set_content("x")
    outbound_module._smtp_submit(msg, _config(), ["client@example.com"])
    timeout = ssl_ctor.call_args.kwargs["timeout"]
    assert isinstance(timeout, (int, float))
    assert 0 < timeout < math.inf


@pytest.mark.parametrize("stage", STAGES)
def test_batch_timeout_is_unknown_outcome_and_next_draft_still_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    db = initialize_database(tmp_path / "t.db")
    _write_draft(tmp_path, "a", "<t227-a@mailattic.test>")
    _write_draft(tmp_path, "b", "<t227-b@mailattic.test>")
    _patch_smtp(monkeypatch, stage)
    monkeypatch.setattr(outbound_module, "_wait_for_batch_interval", lambda _s: None)
    monkeypatch.setattr(outbound_module, "_check_tty_authorization", lambda _s: "YES")

    results = {r["draft_id"]: r for r in send_batch(["a", "b"], db, _config(), env={})}

    assert results["a"]["outcome"] == "failed"
    assert results["a"]["message_id"] == "<t227-a@mailattic.test>"
    assert "intervention required" in results["a"]["reason"]
    assert "retry" not in results["a"]["reason"].lower().replace("not retry", "")
    assert _status(db, "<t227-a@mailattic.test>") == "sending"
    assert results["b"]["outcome"] == "sent"
    assert _status(db, "<t227-b@mailattic.test>") == "sent"


def test_send_draft_timeout_raises_send_refused_with_message_id_and_keeps_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = initialize_database(tmp_path / "t.db")
    _write_draft(tmp_path, "a", "<t227-s@mailattic.test>")
    _patch_smtp(monkeypatch, "data")
    monkeypatch.setattr(outbound_module, "_check_tty_authorization", lambda _s: "YES")

    with pytest.raises(SendRefused) as err:
        send_draft("a", db, _config(), env={})

    assert "<t227-s@mailattic.test>" in str(err.value)
    assert "draft_id=a" in str(err.value)
    assert "timed out" in str(err.value)
    assert _status(db, "<t227-s@mailattic.test>") == "sending"


def _seed(db: Path) -> str:
    mid = "<t227-orig@mailattic.test>"
    conn = connect_database(db)
    try:
        persist_message(
            conn,
            NormalizedMessage(
                subject="Orig",
                sender=MailboxParticipant(display_name="Ext", email="ext@example.com"),
                to_recipients=(MailboxParticipant(display_name="S", email=SMTP_USER),),
                cc_recipients=(),
                bcc_recipients=(),
                sent_at=datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC),
                received_at=datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC),
                body_text="orig",
                attachments=(),
                message_id=mid,
                in_reply_to=None,
                references=None,
                pst_source="pst:test",
                folder_path="Inbox",
                logical_locator="test:1",
                source_input_mode="test",
                source_requested_from="test",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return mid


def test_mcp_send_timeout_returns_error_and_next_send_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = initialize_database(tmp_path / "t.db")
    mid = _seed(db)
    env = tmp_path / "smtp.env"
    env.write_text(
        f"SMTP_HOST=smtp.example.com\nSMTP_PORT=465\nSMTP_USER={SMTP_USER}\n"
        "SMTP_PASSWORD=secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(ENV_VAR_SMTP_ENV_FILE, str(env))
    server = create_server(database_path=db)

    def call(name: str, args: dict[str, object]):
        async def go():
            async with Client(server) as client:
                return await client.call_tool(name, args)

        return asyncio.run(go())

    drafts = [
        call("mail_draft_reply", {"message_id": mid, "body_text": f"body {i}"})
        for i in range(2)
    ]
    sc = [d.structured_content for d in drafts]
    _patch_smtp(monkeypatch, "data")

    first = call(
        "mail_send",
        {"draft_id": sc[0]["draft_id"], "fingerprint": sc[0]["fingerprint"]},
    )
    assert first.is_error
    assert sc[0]["message_id"] in first.content[0].text
    assert _status(db, sc[0]["message_id"]) == "sending"

    second = call(
        "mail_send",
        {"draft_id": sc[1]["draft_id"], "fingerprint": sc[1]["fingerprint"]},
    )
    assert not second.is_error
    assert _status(db, sc[1]["message_id"]) == "sent"

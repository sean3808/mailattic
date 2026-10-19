"""A draft stuck at 'sending' is visible and says how to resolve it (#304).

The marker semantics (#19 / #227), the batch skip and the exit codes are
unchanged; these tests pin only what the user can now see.
"""

from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from pathlib import Path

import pytest

import mailattic.outbound as o
from mailattic.cli import main
from mailattic.storage import initialize_database

SMTP_USER = "sean3808@mailattic.test"
CONFIG = o.SmtpConfig(
    host="smtp.example.com", port=465, user=SMTP_USER, password="secret"
)
ENV = {"SMTP_USER": SMTP_USER}


def _draft(tmp_path: Path, draft_id: str) -> tuple[str, bytes]:
    message_id = f"<{draft_id}@mailattic.test>"
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = "client@example.com"
    msg["Subject"] = f"Subject {draft_id}"
    msg["Message-ID"] = message_id
    msg["Date"] = "Thu, 28 May 2026 10:00:00 +0000"
    msg.set_content("Hello", charset="utf-8")
    directory = tmp_path / "outbound" / "drafts"
    directory.mkdir(parents=True, exist_ok=True)
    raw = msg.as_bytes()
    (directory / f"{draft_id}.eml").write_bytes(raw)
    return message_id, raw


class _FakeSmtp:
    """Stands in for SMTP_SSL; ``login`` fails the way a bad password does."""

    login_error: Exception | None = None
    sent = False

    def __init__(self, *_a: object, **_k: object) -> None: ...

    def __enter__(self) -> _FakeSmtp:
        return self

    def __exit__(self, *_a: object) -> None: ...

    def login(self, *_a: object) -> None:
        if self.login_error is not None:
            raise self.login_error

    def sendmail(self, *_a: object, **_k: object) -> dict:
        type(self).sent = True
        return {}

    def ehlo_or_helo_if_needed(self) -> None: ...

    def has_extn(self, _name: str) -> bool:
        return False


@pytest.fixture
def stuck(tmp_path: Path) -> tuple[Path, str, Path]:
    db = initialize_database(tmp_path / "test.db")
    message_id, _raw = _draft(tmp_path, "stuck")
    o._write_marker(db, message_id, "stuck", "sending")
    return db, message_id, o._marker_path(db, message_id)


def test_list_drafts_marks_sending_in_data_and_text_and_json(
    stuck: tuple[Path, str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db, _mid, marker = stuck
    _draft(tmp_path, "plain")

    by_id = {d["draft_id"]: d for d in o.list_drafts(db)}
    assert by_id["stuck"]["status"] == "sending"
    assert by_id["stuck"]["marker_path"] == str(marker)
    assert "no recipient received it" in by_id["stuck"]["next_step"]

    # Drafts without a marker: exactly the old four keys, text line unchanged.
    assert set(by_id["plain"]) == {"draft_id", "recipients", "subject", "created_at"}

    assert main(["list-drafts", "--database", str(db)]) == 0
    text = capsys.readouterr().out
    plain_line = next(x for x in text.splitlines() if "draft_id: plain" in x)
    assert "status" not in plain_line
    assert plain_line.endswith(f"created: {by_id['plain']['created_at']}")
    stuck_line = next(x for x in text.splitlines() if "draft_id: stuck" in x)
    assert stuck_line.endswith("status: sending")
    assert str(marker) in text
    assert "may or may not have been sent" in text

    assert main(["list-drafts", "--database", str(db), "--json"]) == 0
    parsed = {d["draft_id"]: d for d in json.loads(capsys.readouterr().out)}
    assert parsed["stuck"]["status"] == "sending"
    assert parsed["stuck"]["marker_path"] == str(marker)
    assert "status" not in parsed["plain"]


def test_batch_skip_reason_names_marker_and_steps(
    stuck: tuple[Path, str, Path],
) -> None:
    db, _mid, marker = stuck
    (row,) = o.send_batch(["stuck"], db, CONFIG, ENV, fingerprints={"stuck": "unused"})
    assert row["outcome"] == "skipped"
    reason = row["reason"]
    assert reason.startswith("already sending")
    assert str(marker) in reason
    certain = reason.index("Resend only if you are certain")
    assert certain < reason.index("delete that marker file and send")
    # a partial accept must block a whole-draft resend
    assert "If any recipient received it" in reason
    assert "do not resend" in reason


def test_send_draft_refusal_on_sending_marker_names_marker(
    stuck: tuple[Path, str, Path],
) -> None:
    db, _mid, marker = stuck
    with pytest.raises(o.SendRefused) as err:
        o.send_draft("stuck", db, CONFIG, ENV, fingerprint="unused")
    assert "already sending" in str(err.value)
    assert str(marker) in str(err.value)


def test_sent_marker_skip_has_no_delete_advice(tmp_path: Path) -> None:
    db = initialize_database(tmp_path / "test.db")
    message_id, _raw = _draft(tmp_path, "done")
    o._write_marker(db, message_id, "done", "sent")
    (row,) = o.send_batch(["done"], db, CONFIG, ENV, fingerprints={"done": "x"})
    assert row["reason"] == "already sent"


def test_submit_time_skip_names_marker_and_checks_before_resend(
    tmp_path: Path,
) -> None:
    db = initialize_database(tmp_path / "test.db")
    message_id, _raw = _draft(tmp_path, "race")
    prepared = o._prepare_draft_for_send("race", db)
    assert prepared.skip_status is None
    # another process starts sending between preparation and submission
    o._write_marker(db, message_id, "race", "sending")

    with pytest.raises(o._SubmitTimeSkip) as err:
        o._submit_prepared_draft(prepared, db, CONFIG, ENV)

    text = str(err.value)
    assert str(o._marker_path(db, message_id)) in text
    assert (
        text.index("may already have been delivered")
        < text.index("Resend only if you are certain")
        < text.index("delete that marker file")
    )


def _send(db: Path, tmp_path: Path, draft_id: str) -> o.SendRefused:
    raw = (tmp_path / "outbound" / "drafts" / f"{draft_id}.eml").read_bytes()
    with pytest.raises(o.SendRefused) as err:
        o.send_draft(draft_id, db, CONFIG, ENV, fingerprint=o.draft_fingerprint(raw))
    return err.value


def test_login_failure_says_not_sent_and_names_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = initialize_database(tmp_path / "test.db")
    message_id, _raw = _draft(tmp_path, "badlogin")
    _FakeSmtp.login_error = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    _FakeSmtp.sent = False
    monkeypatch.setattr(o.smtplib, "SMTP_SSL", _FakeSmtp)
    reviewed_sends: list[object] = []
    monkeypatch.setattr(
        o, "_send_reviewed_message", lambda *a, **k: reviewed_sends.append(a)
    )

    text = str(_send(db, tmp_path, "badlogin"))

    # the "not sent" claim rests on login() failing before any message goes out
    assert reviewed_sends == []
    assert "the mail was not sent" in text
    assert str(o._marker_path(db, message_id)) in text
    assert not _FakeSmtp.sent
    # semantics unchanged: marker still blocks a plain re-run
    assert o._read_marker(db, message_id)["status"] == "sending"


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPServerDisconnected("dropped"),
        smtplib.SMTPResponseException(554, b"rejected"),
        TimeoutError("timed out"),
    ],
)
def test_other_failures_never_claim_not_sent_and_name_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    db = initialize_database(tmp_path / "test.db")
    message_id, _raw = _draft(tmp_path, "other")
    _FakeSmtp.login_error = error
    monkeypatch.setattr(o.smtplib, "SMTP_SSL", _FakeSmtp)

    text = str(_send(db, tmp_path, "other"))

    assert "the mail was not sent" not in text
    assert str(o._marker_path(db, message_id)) in text
    assert (
        text.index("may already have been delivered")
        < text.index("Resend only if you are certain")
        < text.index("delete that marker file")
    )

"""Tests implemented `send_batch`, CLI reporting, and send_draft race hardening.

Covers openspec/changes/add-send-batch/specs/outbound-send/spec.md and the
Implementation Contract in openspec/changes/add-send-batch/design.md.

The module exercises `send_batch` at the function boundary with SMTP, marker,
and TTY edges mocked, plus focused CLI-level tests for `_handle_send_batch`
reporting/exit-code behavior.

Scenario numbering below matches the dispatch brief:
  1-10  -> send_batch scenarios
  11-12 -> send_draft strengthening (task 1.3)
  CLI   -> `_handle_send_batch` reporting/exit-code behavior
"""

from __future__ import annotations

import argparse
import base64
import json
import smtplib
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import mailattic.cli as cli_module
import mailattic.outbound as outbound_module
import mailattic.outbound_commands as oc_module
from mailattic.outbound import (
    SendRefused,
    SmtpConfig,
    _marker_path,
    _write_marker,
    send_draft,
)
from mailattic.outbound_commands import _handle_send_batch
from mailattic.storage import connect_database, initialize_database

SMTP_USER = "sean3808@mailattic.test"


@pytest.fixture(autouse=True)
def fake_batch_wait(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Keep batch tests instant while exposing the pacing calls for assertions."""
    wait = MagicMock()
    monkeypatch.setattr(
        outbound_module,
        "_wait_for_batch_interval",
        wait,
        raising=False,
    )
    return wait


def _send_batch(*args: object, **kwargs: object) -> list[dict]:
    """Call outbound.send_batch, failing the test (not collection) if absent."""
    fn = getattr(outbound_module, "send_batch", None)
    assert fn is not None, "mailattic.outbound.send_batch is not implemented yet"
    if len(args) < 4:
        kwargs.setdefault("env", {})  # env is required (#114); no IMAP keys here
    return fn(*args, **kwargs)


def _write_draft(
    tmp_path: Path,
    draft_id: str,
    message_id: str,
    *,
    to: str = "Client <client@example.com>",
    cc: str = "Colleague <colleague@example.com>",
    bcc: str = SMTP_USER,
    subject: str = "Test outbound",
    attachments: list[tuple[str, bytes]] | None = None,
    extra_recipient_headers: list[tuple[str, str]] | None = None,
) -> Path:
    draft_dir = tmp_path / "outbound" / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = to
    msg["Cc"] = cc
    msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["In-Reply-To"] = "<parent@mailattic.test>"
    msg["References"] = "<root@mailattic.test> <parent@mailattic.test>"
    msg["Date"] = "Thu, 28 May 2026 10:00:00 +0000"
    msg.set_content("Hello", charset="utf-8")
    for filename, content in attachments or []:
        msg.add_attachment(
            content, maintype="application", subtype="octet-stream", filename=filename
        )
    raw = msg.as_bytes()
    if extra_recipient_headers:
        # `msg[name] = value` enforces EmailPolicy's header_max_count (1 for
        # address headers like To/Cc/Bcc), which would reject a genuine
        # duplicate. Splice extra header lines directly into the raw bytes,
        # right before the header/body blank-line separator (so they land
        # after the originals, preserving header order), to simulate a
        # malformed draft with duplicate address headers -- bypassing that
        # in-memory check entirely.
        header_block, _, body = raw.partition(b"\n\n")
        extra_lines = b"".join(
            f"{header_name}: {header_value}\n".encode()
            for header_name, header_value in extra_recipient_headers
        )
        raw = header_block + b"\n" + extra_lines + b"\n" + body
    draft_path = draft_dir / f"{draft_id}.eml"
    draft_path.write_bytes(raw)
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


def _read_marker_status(db_path: Path, message_id: str) -> str:
    return json.loads(_marker_path(db_path, message_id).read_text(encoding="utf-8"))[
        "status"
    ]


# ---------------------------------------------------------------------------
# Scenario 1: three drafts sent under one confirmation
# ---------------------------------------------------------------------------


def test_three_drafts_sent_under_one_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    ids_and_mids = {
        "draft-a": "<batch-a@mailattic.test>",
        "draft-b": "<batch-b@mailattic.test>",
        "draft-c": "<batch-c@mailattic.test>",
    }
    for draft_id, mid in ids_and_mids.items():
        _write_draft(tmp_path, draft_id, mid)
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )

    results = _send_batch(list(ids_and_mids.keys()), db_path, _smtp_config())

    by_id = {r["draft_id"]: r for r in results}
    assert set(by_id) == set(ids_and_mids)
    for draft_id, mid in ids_and_mids.items():
        assert by_id[draft_id]["outcome"] == "sent"
        assert by_id[draft_id]["message_id"] == mid
        assert _read_marker_status(db_path, mid) == "sent"
    assert mock_smtp.sendmail.call_count == 3

    conn = connect_database(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE pst_source='outbound-bes'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 3


# ---------------------------------------------------------------------------
# Scenario 2: one draft's SMTP raises, the rest still send
# ---------------------------------------------------------------------------


def test_one_draft_smtp_failure_others_still_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    mids = {
        "draft-a": "<b2-a@mailattic.test>",
        "draft-b": "<b2-b@mailattic.test>",
        "draft-c": "<b2-c@mailattic.test>",
    }
    for draft_id, mid in mids.items():
        _write_draft(tmp_path, draft_id, mid)
    mock_smtp = _patch_smtp(monkeypatch)
    # Sequential submission per design: a, b, c -- fail only the middle one.
    mock_smtp.sendmail.side_effect = [{}, smtplib.SMTPException("boom"), {}]
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )

    results = _send_batch(["draft-a", "draft-b", "draft-c"], db_path, _smtp_config())

    by_id = {r["draft_id"]: r for r in results}
    assert by_id["draft-a"]["outcome"] == "sent"
    assert by_id["draft-c"]["outcome"] == "sent"
    assert by_id["draft-b"]["outcome"] == "failed"
    assert by_id["draft-b"]["message_id"] == mids["draft-b"]
    assert by_id["draft-b"].get("reason")
    assert _read_marker_status(db_path, mids["draft-a"]) == "sent"
    assert _read_marker_status(db_path, mids["draft-c"]) == "sent"
    assert _read_marker_status(db_path, mids["draft-b"]) == "sending"
    assert mock_smtp.sendmail.call_count == 3


# ---------------------------------------------------------------------------
# Pacing contract (harden-reply-anchor-and-batch-throttling task 1.2)
# ---------------------------------------------------------------------------


def test_batch_first_attempt_is_immediate_then_waits_90_seconds_in_input_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_batch_wait: MagicMock,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    draft_ids = ["draft-a", "draft-b", "draft-c"]
    for draft_id in draft_ids:
        _write_draft(tmp_path, draft_id, f"<{draft_id}@mailattic.test>")
    _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        outbound_module, "_check_tty_authorization", lambda _summary: "YES"
    )

    events: list[tuple[str, object]] = []
    original_submit = outbound_module._submit_prepared_draft

    def record_submit(prepared, database_path, smtp_config, env=None):
        events.append(("submit", prepared.draft_id))
        return original_submit(prepared, database_path, smtp_config, env)

    def record_wait(seconds: float) -> None:
        events.append(("wait", seconds))

    monkeypatch.setattr(outbound_module, "_submit_prepared_draft", record_submit)
    fake_batch_wait.side_effect = record_wait

    results = _send_batch(draft_ids, db_path, _smtp_config())

    assert outbound_module.BATCH_SEND_INTERVAL_SECONDS == 90
    assert events == [
        ("submit", "draft-a"),
        ("wait", 90),
        ("submit", "draft-b"),
        ("wait", 90),
        ("submit", "draft-c"),
    ]
    assert [result["draft_id"] for result in results] == draft_ids
    assert [result["outcome"] for result in results] == ["sent", "sent", "sent"]


def test_batch_skips_and_preparation_failures_do_not_add_pacing_waits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_batch_wait: MagicMock,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    sent_mid = "<already-sent@mailattic.test>"
    _write_draft(tmp_path, "draft-sent", sent_mid)
    _write_marker(db_path, sent_mid, "draft-sent", "sent")
    _write_draft(tmp_path, "draft-a", "<sendable-a@mailattic.test>")
    _write_draft(tmp_path, "draft-b", "<sendable-b@mailattic.test>")
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        outbound_module, "_check_tty_authorization", lambda _summary: "YES"
    )
    draft_ids = ["draft-sent", "draft-missing", "draft-a", "draft-b"]

    results = _send_batch(draft_ids, db_path, _smtp_config())

    assert [result["draft_id"] for result in results] == draft_ids
    assert [result["outcome"] for result in results] == [
        "skipped",
        "failed",
        "sent",
        "sent",
    ]
    fake_batch_wait.assert_called_once_with(90)
    assert [call.args[1] for call in mock_smtp.sendmail.call_args_list] == [
        ["client@example.com", "colleague@example.com", SMTP_USER],
        ["client@example.com", "colleague@example.com", SMTP_USER],
    ]


def test_batch_failure_is_still_followed_by_pacing_before_next_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_batch_wait: MagicMock,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _write_draft(tmp_path, "draft-fails", "<fails@mailattic.test>")
    _write_draft(tmp_path, "draft-next", "<next@mailattic.test>")
    mock_smtp = _patch_smtp(monkeypatch)
    mock_smtp.sendmail.side_effect = [
        smtplib.SMTPException("simulated refusal"),
        {},
    ]
    monkeypatch.setattr(
        outbound_module, "_check_tty_authorization", lambda _summary: "YES"
    )

    results = _send_batch(["draft-fails", "draft-next"], db_path, _smtp_config())

    assert [result["outcome"] for result in results] == ["failed", "sent"]
    fake_batch_wait.assert_called_once_with(90)
    assert mock_smtp.sendmail.call_count == 2


def test_keyboard_interrupt_during_pacing_wait_leaves_later_markers_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_batch_wait: MagicMock,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    mids = {
        "draft-a": "<interrupt-a@mailattic.test>",
        "draft-b": "<interrupt-b@mailattic.test>",
        "draft-c": "<interrupt-c@mailattic.test>",
    }
    for draft_id, message_id in mids.items():
        _write_draft(tmp_path, draft_id, message_id)
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        outbound_module, "_check_tty_authorization", lambda _summary: "YES"
    )
    fake_batch_wait.side_effect = KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _send_batch(list(mids), db_path, _smtp_config())

    fake_batch_wait.assert_called_once_with(90)
    assert mock_smtp.sendmail.call_count == 1
    assert _read_marker_status(db_path, mids["draft-a"]) == "sent"
    assert not _marker_path(db_path, mids["draft-b"]).exists()
    assert not _marker_path(db_path, mids["draft-c"]).exists()


# ---------------------------------------------------------------------------
# Scenario 3: SMTP succeeds but DB persistence fails leaves marker "sending"
# ---------------------------------------------------------------------------


def test_persist_failure_after_smtp_success_leaves_marker_sending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    fail_mid = "<persist-fail@mailattic.test>"
    ok_mid = "<persist-ok@mailattic.test>"
    _write_draft(tmp_path, "draft-persist-fail", fail_mid)
    _write_draft(tmp_path, "draft-persist-ok", ok_mid)
    _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )

    from mailattic.storage import persist_message as real_persist_message

    def flaky_persist(connection, message):
        if message.logical_locator == "outbound:draft-persist-fail":
            raise RuntimeError("db boom")
        return real_persist_message(connection, message)

    monkeypatch.setattr("mailattic.outbound.persist_message", flaky_persist)

    results = _send_batch(
        ["draft-persist-fail", "draft-persist-ok"], db_path, _smtp_config()
    )

    by_id = {r["draft_id"]: r for r in results}
    assert by_id["draft-persist-fail"]["outcome"] == "failed"
    assert by_id["draft-persist-fail"]["message_id"] == fail_mid
    assert "accepted by SMTP" in by_id["draft-persist-fail"]["reason"]
    assert "may already be delivered" in by_id["draft-persist-fail"]["reason"]
    assert "left at 'sending'" in by_id["draft-persist-fail"]["reason"]
    assert (
        "verify delivery status before considering any resend"
        in by_id["draft-persist-fail"]["reason"]
    )
    assert _read_marker_status(db_path, fail_mid) == "sending"

    assert by_id["draft-persist-ok"]["outcome"] == "sent"
    assert _read_marker_status(db_path, ok_mid) == "sent"

    conn = connect_database(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id=?", [ok_mid]
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


# ---------------------------------------------------------------------------
# Scenario 4: already-sent draft in the list is skipped, not failed
# ---------------------------------------------------------------------------


def test_already_sent_draft_in_list_skipped_others_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    sent_mid = "<batch-already-sent@mailattic.test>"
    fresh_mid = "<batch-fresh@mailattic.test>"
    _write_draft(tmp_path, "draft-sent", sent_mid)
    _write_marker(db_path, sent_mid, "draft-sent", "sent")
    _write_draft(tmp_path, "draft-fresh", fresh_mid)
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )

    results = _send_batch(["draft-sent", "draft-fresh"], db_path, _smtp_config())

    by_id = {r["draft_id"]: r for r in results}
    assert by_id["draft-sent"]["outcome"] == "skipped"
    assert by_id["draft-sent"]["message_id"] == sent_mid
    assert by_id["draft-sent"].get("reason")
    assert by_id["draft-fresh"]["outcome"] == "sent"
    # Contract: exit code is CLI-derived from "any outcome == failed"; a skip
    # alone must never appear as a failed outcome here.
    assert not any(r["outcome"] == "failed" for r in results)
    mock_smtp.sendmail.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 5: duplicate draft_id rejected before any preparation/SMTP
# ---------------------------------------------------------------------------


def test_duplicate_draft_id_rejected_before_any_smtp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _write_draft(tmp_path, "draft-a", "<dup-a@mailattic.test>")
    _write_draft(tmp_path, "draft-b", "<dup-b@mailattic.test>")
    mock_smtp = _patch_smtp(monkeypatch)
    mock_tty = MagicMock(return_value="YES")
    monkeypatch.setattr("mailattic.outbound._check_tty_authorization", mock_tty)

    with pytest.raises(SendRefused):
        _send_batch(["draft-a", "draft-a", "draft-b"], db_path, _smtp_config())

    mock_smtp.sendmail.assert_not_called()
    mock_tty.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 6: marker changed between preparation and submission is skipped
# ---------------------------------------------------------------------------


def test_marker_changed_between_preparation_and_submission_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<race-in-batch@mailattic.test>"
    _write_draft(tmp_path, "draft-race", message_id)
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )

    original_read_marker = outbound_module._read_marker
    call_count = {"n": 0}

    def flaky_read_marker(database_path, message_id_arg):
        call_count["n"] += 1
        # 1st read = preparation (unsent -> sendable); every subsequent read
        # (the submit-time re-check) reports the marker flipped to "sent" by
        # a run that completed within this same batch/process window.
        if call_count["n"] > 1:
            return {
                "status": "sent",
                "message_id": message_id_arg,
                "draft_id": "draft-race",
            }
        return original_read_marker(database_path, message_id_arg)

    monkeypatch.setattr("mailattic.outbound._read_marker", flaky_read_marker)

    results = _send_batch(["draft-race"], db_path, _smtp_config())

    assert len(results) == 1
    assert results[0]["draft_id"] == "draft-race"
    assert results[0]["outcome"] == "skipped"
    assert results[0]["message_id"] == message_id
    mock_smtp.sendmail.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 7: no sendable draft -> TTY authorization is never requested
# ---------------------------------------------------------------------------


def test_all_skipped_input_does_not_request_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<already-sent-only@mailattic.test>"
    _write_draft(tmp_path, "draft-sent", message_id)
    _write_marker(db_path, message_id, "draft-sent", "sent")
    mock_smtp = _patch_smtp(monkeypatch)
    mock_tty = MagicMock(return_value="YES")
    monkeypatch.setattr("mailattic.outbound._check_tty_authorization", mock_tty)

    results = _send_batch(["draft-sent"], db_path, _smtp_config())

    mock_tty.assert_not_called()
    mock_smtp.sendmail.assert_not_called()
    assert len(results) == 1
    assert results[0]["draft_id"] == "draft-sent"
    assert results[0]["outcome"] == "skipped"


# ---------------------------------------------------------------------------
# Scenario 8: non-TTY environment refuses the whole batch
# ---------------------------------------------------------------------------


def test_non_tty_environment_refuses_whole_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _write_draft(tmp_path, "draft-a", "<nontty-a@mailattic.test>")
    _write_draft(tmp_path, "draft-b", "<nontty-b@mailattic.test>")
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        MagicMock(side_effect=SendRefused("interactive authorization required")),
    )

    with pytest.raises(SendRefused, match="interactive authorization required"):
        _send_batch(["draft-a", "draft-b"], db_path, _smtp_config())

    mock_smtp.sendmail.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 9: missing/unparseable draft id reported failed, others unaffected
# ---------------------------------------------------------------------------


def test_missing_draft_reported_failed_others_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    good_mid = "<good@mailattic.test>"
    _write_draft(tmp_path, "draft-good", good_mid)
    # "draft-missing" has no .eml on disk at all.
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )

    results = _send_batch(["draft-good", "draft-missing"], db_path, _smtp_config())

    by_id = {r["draft_id"]: r for r in results}
    assert by_id["draft-good"]["outcome"] == "sent"
    assert by_id["draft-good"]["message_id"] == good_mid
    assert by_id["draft-missing"]["outcome"] == "failed"
    assert by_id["draft-missing"].get("reason")
    # No valid Message-ID was ever established for a draft missing on disk,
    # so the failed result must NOT claim one.
    assert "message_id" not in by_id["draft-missing"]
    mock_smtp.sendmail.assert_called_once()


def test_marker_read_failure_after_valid_message_id_carries_message_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prep failure occurring AFTER the Message-ID is already known (e.g. a

    `_read_marker` I/O/decode error) must surface that known `message_id` in
    the failed result -- unlike a missing-draft/unparseable-draft failure,
    where no valid Message-ID exists yet.
    """
    db_path = initialize_database(tmp_path / "test.db")
    good_mid = "<good-marker-fail@mailattic.test>"
    broken_mid = "<broken-marker-read@mailattic.test>"
    _write_draft(tmp_path, "draft-good", good_mid)
    _write_draft(tmp_path, "draft-marker-broken", broken_mid)
    mock_smtp = _patch_smtp(monkeypatch)
    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        lambda _summary: "YES",
    )

    original_read_marker = outbound_module._read_marker

    def flaky_read_marker(database_path, message_id_arg):
        if message_id_arg == broken_mid:
            raise OSError("simulated marker read I/O error")
        return original_read_marker(database_path, message_id_arg)

    monkeypatch.setattr("mailattic.outbound._read_marker", flaky_read_marker)

    results = _send_batch(
        ["draft-good", "draft-marker-broken"], db_path, _smtp_config()
    )

    by_id = {r["draft_id"]: r for r in results}
    assert by_id["draft-good"]["outcome"] == "sent"
    assert by_id["draft-marker-broken"]["outcome"] == "failed"
    assert by_id["draft-marker-broken"]["message_id"] == broken_mid
    assert "simulated marker read I/O error" in by_id["draft-marker-broken"]["reason"]
    mock_smtp.sendmail.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 10: combined summary lists recipients and attachment filenames
# ---------------------------------------------------------------------------


def test_combined_summary_lists_recipients_and_attachments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _write_draft(
        tmp_path,
        "draft-a",
        "<summary-a@mailattic.test>",
        to="x@e.com",
        cc="y@e.com",
        bcc=SMTP_USER,
        attachments=[("p.pdf", b"%PDF-1.4 fake pdf content")],
    )
    _patch_smtp(monkeypatch)
    mock_tty = MagicMock(return_value="YES")
    monkeypatch.setattr("mailattic.outbound._check_tty_authorization", mock_tty)

    _send_batch(["draft-a"], db_path, _smtp_config())

    mock_tty.assert_called_once()
    summary = mock_tty.call_args.args[0]
    assert "draft-a" in summary
    assert "x@e.com" in summary
    assert "y@e.com" in summary
    assert SMTP_USER in summary
    assert "p.pdf" in summary


def test_combined_summary_lists_recipients_from_duplicate_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _write_draft(
        tmp_path,
        "draft-dup-to",
        "<summary-dup@mailattic.test>",
        to="first@example.com",
        cc="copy@example.com",
        bcc=SMTP_USER,
        extra_recipient_headers=[("To", "second@example.com")],
    )
    mock_smtp = _patch_smtp(monkeypatch)
    mock_tty = MagicMock(return_value="YES")
    monkeypatch.setattr("mailattic.outbound._check_tty_authorization", mock_tty)

    _send_batch(["draft-dup-to"], db_path, _smtp_config())

    summary = mock_tty.call_args.args[0]
    assert "first@example.com" in summary
    assert "second@example.com" in summary
    assert "copy@example.com" in summary
    assert SMTP_USER in summary
    assert mock_smtp.sendmail.call_args.args[1] == [
        "first@example.com",
        "second@example.com",
        "copy@example.com",
        SMTP_USER,
    ]


# ---------------------------------------------------------------------------
# Security regression: attacker/data-influenced fields (Subject, attachment
# filenames) must not be able to inject ANSI/terminal control sequences into
# the TTY authorization summary (adversarial review finding).
# ---------------------------------------------------------------------------


def test_summary_neutralizes_ansi_control_sequences_in_subject_and_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malicious Subject/attachment filename embedding ANSI clear-screen /

    clear-scrollback / conceal sequences or a raw embedded newline must not
    reach the operator's TTY unescaped -- otherwise it could erase or hide
    other drafts' recipient lines from the shared batch authorization screen
    before the operator authorizes an irreversible send.
    """
    db_path = initialize_database(tmp_path / "test.db")
    # A raw CR/LF cannot survive `msg["Subject"] = value` -- Python's own
    # email library already forbids embedded CR/LF in a header value on the
    # wire (ValueError: "Header values may not contain linefeed..."). A
    # realistic attacker instead smuggles an embedded newline (plus ANSI
    # control sequences) through an RFC 2047 encoded-word: the raw header
    # LINE is valid RFC 5322 (no bare CR/LF), but MIME-decoding the
    # encoded-word payload yields a Python string containing a raw newline
    # and ESC bytes. Simulate that realistic vector here.
    malicious_decoded = "Invoice\x1b[2J\x1b[3J\x1b[8mConcealed\nSubject\x1b[0m"
    encoded_word = (
        "=?utf-8?B?"
        + base64.b64encode(malicious_decoded.encode("utf-8")).decode("ascii")
        + "?="
    )
    draft_path = _write_draft(
        tmp_path,
        "draft-ansi",
        "<ansi-injection@mailattic.test>",
        subject="placeholder-subject",
        attachments=[("evil\x1b[2J.pdf", b"%PDF-1.4 fake pdf content")],
    )
    raw = draft_path.read_bytes()
    assert b"Subject: placeholder-subject\n" in raw
    raw = raw.replace(
        b"Subject: placeholder-subject\n", f"Subject: {encoded_word}\n".encode()
    )
    draft_path.write_bytes(raw)

    _patch_smtp(monkeypatch)
    mock_tty = MagicMock(return_value="YES")
    monkeypatch.setattr("mailattic.outbound._check_tty_authorization", mock_tty)

    _send_batch(["draft-ansi"], db_path, _smtp_config())

    summary = mock_tty.call_args.args[0]
    # No raw ESC anywhere -- this alone proves every ANSI/CSI sequence (they
    # all begin with ESC) was neutralized, since none can survive without it.
    assert "\x1b" not in summary
    # Confirm the dangerous sequences were neutralized to a visible,
    # non-executable escaped form rather than silently dropped.
    assert "\\x1b" in summary
    assert "Concealed" in summary  # sanity: content itself still visible
    # The malicious embedded newline (smuggled via the encoded-word) must not
    # have become an actual line break splitting the Subject line in two --
    # it must show up as a visible escape instead.
    assert "Concealed\nSubject" not in summary
    assert "Concealed\\x0aSubject" in summary


def test_summary_preserves_cjk_subject_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sanitizer must NOT mangle normal printable/non-ASCII text -- this

    mailbox is heavily Chinese, so a CJK subject must pass through byte-for-
    byte unchanged (not escaped, not corrupted by e.g. unicode_escape).
    """
    db_path = initialize_database(tmp_path / "test.db")
    cjk_subject = "報價單 測試"
    _write_draft(
        tmp_path,
        "draft-cjk",
        "<cjk-subject@mailattic.test>",
        subject=cjk_subject,
    )
    _patch_smtp(monkeypatch)
    mock_tty = MagicMock(return_value="YES")
    monkeypatch.setattr("mailattic.outbound._check_tty_authorization", mock_tty)

    _send_batch(["draft-cjk"], db_path, _smtp_config())

    summary = mock_tty.call_args.args[0]
    assert cjk_subject in summary


def test_summary_shows_readable_cjk_recipient_display_name_not_encoded_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CJK recipient display name must render as plain readable text in the

    authorization summary, NOT an RFC 2047 =?utf-8?...?= encoded-word.
    (Regression guard: `_format_recipient_summary` must not use
    `formataddr()`, which encodes any non-ASCII display name -- this
    mailbox's recipients are overwhelmingly Chinese, so that would make the
    summary the operator relies on to authorize an irreversible send
    unreadable.)
    """
    db_path = initialize_database(tmp_path / "test.db")
    _write_draft(
        tmp_path,
        "draft-cjk-recipient",
        "<cjk-recipient@mailattic.test>",
        to="王小明 <wang@example.com>",
        cc="Colleague <colleague@example.com>",
        bcc=SMTP_USER,
    )
    _patch_smtp(monkeypatch)
    mock_tty = MagicMock(return_value="YES")
    monkeypatch.setattr("mailattic.outbound._check_tty_authorization", mock_tty)

    _send_batch(["draft-cjk-recipient"], db_path, _smtp_config())

    summary = mock_tty.call_args.args[0]
    assert "王小明" in summary
    assert "wang@example.com" in summary
    assert "=?" not in summary  # no leftover RFC 2047 encoded-word marker


def test_recipient_control_char_display_name_sanitized_in_summary_not_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recipient display name smuggling ANSI/control chars (via an RFC 2047

    encoded-word, the same realistic vector used for the Subject regression
    test above) must be neutralized in the TTY authorization summary -- but
    the REAL, unsanitized address must still be exactly what SMTP sends to.
    Proves sanitization stays display-only and never corrupts the actual
    send target, i.e. Fix A's shared summary/envelope source-of-truth
    guarantee survives the sanitizer.
    """
    db_path = initialize_database(tmp_path / "test.db")
    malicious_name = "Evil\x1b[2JName"
    encoded_word = (
        "=?utf-8?B?"
        + base64.b64encode(malicious_name.encode("utf-8")).decode("ascii")
        + "?="
    )
    draft_path = _write_draft(
        tmp_path,
        "draft-recipient-ansi",
        "<recipient-ansi@mailattic.test>",
        to="placeholder-to <placeholder@example.com>",
        cc="Colleague <colleague@example.com>",
        bcc=SMTP_USER,
    )
    raw = draft_path.read_bytes()
    assert b"To: placeholder-to <placeholder@example.com>\n" in raw
    raw = raw.replace(
        b"To: placeholder-to <placeholder@example.com>\n",
        f"To: {encoded_word} <evil@example.com>\n".encode(),
    )
    draft_path.write_bytes(raw)

    mock_smtp = _patch_smtp(monkeypatch)
    mock_tty = MagicMock(return_value="YES")
    monkeypatch.setattr("mailattic.outbound._check_tty_authorization", mock_tty)

    _send_batch(["draft-recipient-ansi"], db_path, _smtp_config())

    summary = mock_tty.call_args.args[0]
    assert "\x1b" not in summary
    assert "\\x1b" in summary
    assert "evil@example.com" in summary  # address still visible, just not ESC-laden

    # The real SMTP envelope must still carry the unsanitized real address.
    envelope_recipients = mock_smtp.sendmail.call_args.args[1]
    assert "evil@example.com" in envelope_recipients


def test_sanitize_helpers_neutralize_del_c1_and_control_chars_in_address_and_id() -> (
    None
):
    """Direct unit-level coverage for character classes that cannot be

    reliably exercised through a real on-disk draft: Windows NTFS itself
    rejects control characters in filenames (verified separately), so a
    `draft_id` containing a raw control char can never exist as an actual
    .eml on disk -- `_prepare_draft_for_send` applies
    `_sanitize_for_terminal(draft_id)` verbatim, so testing the sanitizer
    directly on a draft_id-shaped string proves that code path. Also covers
    DEL (U+007F), a C1 control (U+0085 NEL), and a control char embedded in
    the recipient ADDRESS (not just the display name) via
    `_format_recipient_summary`.
    """
    assert outbound_module._sanitize_for_terminal("\x7f") == "\\x7f"
    assert outbound_module._sanitize_for_terminal("\x85") == "\\x85"

    draft_id_shaped = "draft\x1b[2Jid"
    sanitized_draft_id = outbound_module._sanitize_for_terminal(draft_id_shaped)
    assert "\x1b" not in sanitized_draft_id
    assert "\\x1b" in sanitized_draft_id
    assert "draft" in sanitized_draft_id
    assert "id" in sanitized_draft_id

    summary = outbound_module._format_recipient_summary(
        [("Safe Name", "addr\x1b[2J@example.com")]
    )
    assert "\x1b" not in summary
    assert "\\x1b" in summary
    assert "addr" in summary
    assert "@example.com" in summary


# ---------------------------------------------------------------------------
# Scenario 11 (task 1.3a): send_draft on an already-sent draft refuses
# before printing any summary or requesting a confirmation token.
# ---------------------------------------------------------------------------


def test_send_draft_already_sent_refused_before_summary_or_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<already-sent-strengthen@mailattic.test>"
    _write_draft(tmp_path, "draft-strengthen", message_id)
    _write_marker(db_path, message_id, "draft-strengthen", "sent")
    mock_smtp = _patch_smtp(monkeypatch)
    mock_tty = MagicMock(return_value="YES")
    monkeypatch.setattr("mailattic.outbound._check_tty_authorization", mock_tty)

    with pytest.raises(SendRefused, match="already sent"):
        send_draft("draft-strengthen", db_path, _smtp_config(), {})

    mock_tty.assert_not_called()
    mock_smtp.sendmail.assert_not_called()
    captured = capsys.readouterr()
    assert "Subject" not in captured.out
    assert "draft-strengthen" not in captured.out


# ---------------------------------------------------------------------------
# Scenario 12 (task 1.3b): send_draft whose marker flips to sent/sending
# AFTER authorization but BEFORE the submit-time re-read raises SendRefused
# and never returns a batch-style "skipped".
# ---------------------------------------------------------------------------


def test_send_draft_marker_changed_after_authorization_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<race@mailattic.test>"
    _write_draft(tmp_path, "draft-race-single", message_id)
    mock_smtp = _patch_smtp(monkeypatch)

    def authorize_then_race(_summary: str) -> str:
        # Simulate a concurrent sender writing the "sent" marker in the
        # window between this call returning and send_draft's submit step.
        _write_marker(db_path, message_id, "draft-race-single", "sent")
        return "YES"

    monkeypatch.setattr(
        "mailattic.outbound._check_tty_authorization",
        authorize_then_race,
    )

    with pytest.raises(SendRefused):
        send_draft("draft-race-single", db_path, _smtp_config(), {})

    mock_smtp.sendmail.assert_not_called()
    assert _read_marker_status(db_path, message_id) == "sent"


# ---------------------------------------------------------------------------
# CLI-level: _handle_send_batch (task 3.1). Minimal -- exercises exit-code
# derivation and the whole-batch-abort vs per-draft-report split; send_batch
# itself is monkeypatched so these do not re-cover the function-boundary
# scenarios above.
# ---------------------------------------------------------------------------


def _make_send_batch_args(tmp_path: Path, draft_ids: list[str]) -> argparse.Namespace:
    return argparse.Namespace(
        draft_id=draft_ids,
        database=tmp_path / "test.db",
        env_file=tmp_path / ".env",
    )


def test_handle_send_batch_exit_nonzero_when_any_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(oc_module, "load_env_file", lambda _env_file: {})
    monkeypatch.setattr(oc_module, "load_smtp_config", lambda _env: _smtp_config())
    monkeypatch.setattr(
        oc_module,
        "send_batch",
        lambda **_kwargs: [
            {"draft_id": "a", "outcome": "sent", "message_id": "<a@mailattic.test>"},
            {
                "draft_id": "b",
                "outcome": "failed",
                "reason": "boom",
                "message_id": "<b@mailattic.test>",
            },
        ],
    )

    exit_code = _handle_send_batch(_make_send_batch_args(tmp_path, ["a", "b"]))

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "Sent: draft_id=a message_id=<a@mailattic.test>" in captured.out
    assert "Failed: draft_id=b (boom) message_id=<b@mailattic.test>" in captured.err


def test_handle_send_batch_exit_zero_when_all_sent_or_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(oc_module, "load_env_file", lambda _env_file: {})
    monkeypatch.setattr(oc_module, "load_smtp_config", lambda _env: _smtp_config())
    monkeypatch.setattr(
        oc_module,
        "send_batch",
        lambda **_kwargs: [
            {"draft_id": "a", "outcome": "sent", "message_id": "<a@mailattic.test>"},
            {
                "draft_id": "b",
                "outcome": "skipped",
                "reason": "already sent",
                "message_id": "<b@mailattic.test>",
            },
        ],
    )

    exit_code = _handle_send_batch(_make_send_batch_args(tmp_path, ["a", "b"]))

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Sent: draft_id=a message_id=<a@mailattic.test>" in captured.out
    assert "Skipped: draft_id=b (already sent) message_id=<b@mailattic.test>" in (
        captured.out
    )


def test_handle_send_batch_duplicate_abort_prints_refusal_not_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(oc_module, "load_env_file", lambda _env_file: {})
    monkeypatch.setattr(oc_module, "load_smtp_config", lambda _env: _smtp_config())

    def raise_refused(**_kwargs: object) -> list[dict]:
        raise SendRefused("duplicate draft_id in send-batch argument list")

    monkeypatch.setattr(oc_module, "send_batch", raise_refused)

    exit_code = _handle_send_batch(_make_send_batch_args(tmp_path, ["a", "a"]))

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "duplicate draft_id" in captured.err
    assert "Sent:" not in captured.out
    assert "Failed:" not in captured.err


# ---------------------------------------------------------------------------
# #195: fingerprint arguments (`<draft_id>=<sha>` for send-batch)
# ---------------------------------------------------------------------------


def test_handle_send_batch_splits_draft_id_fingerprint_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oc_module, "load_env_file", lambda _env_file: {})
    monkeypatch.setattr(oc_module, "load_smtp_config", lambda _env: _smtp_config())
    calls: list[dict] = []

    def fake_send_batch(**kwargs: object) -> list[dict]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(oc_module, "send_batch", fake_send_batch)

    exit_code = _handle_send_batch(_make_send_batch_args(tmp_path, ["a=AAA", "b=bbb"]))

    assert exit_code == 0
    assert calls[0]["draft_ids"] == ["a", "b"]
    assert calls[0]["fingerprints"] == {"a": "AAA", "b": "bbb"}


def test_handle_send_batch_without_pairs_passes_no_fingerprints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oc_module, "load_env_file", lambda _env_file: {})
    monkeypatch.setattr(oc_module, "load_smtp_config", lambda _env: _smtp_config())
    calls: list[dict] = []
    monkeypatch.setattr(
        oc_module, "send_batch", lambda **kwargs: calls.append(kwargs) or []
    )

    assert _handle_send_batch(_make_send_batch_args(tmp_path, ["a", "b"])) == 0
    assert calls[0]["draft_ids"] == ["a", "b"]
    assert calls[0]["fingerprints"] is None


def test_handle_send_batch_mixed_pairs_refused_before_sending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(oc_module, "load_env_file", lambda _env_file: {})
    monkeypatch.setattr(oc_module, "load_smtp_config", lambda _env: _smtp_config())
    send = MagicMock()
    monkeypatch.setattr(oc_module, "send_batch", send)

    exit_code = _handle_send_batch(_make_send_batch_args(tmp_path, ["a=AAA", "b"]))

    assert exit_code == 2
    send.assert_not_called()
    assert "<draft_id>=<fingerprint>" in capsys.readouterr().err


def test_send_draft_cli_forwards_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oc_module, "load_env_file", lambda _env_file: {})
    monkeypatch.setattr(oc_module, "load_smtp_config", lambda _env: _smtp_config())
    calls: list[dict] = []

    def fake_send_draft(**kwargs: object) -> dict[str, str]:
        calls.append(kwargs)
        return {"message_id": "<x@mailattic.test>", "draft_id": "d"}

    monkeypatch.setattr(oc_module, "send_draft", fake_send_draft)
    args = cli_module.build_parser().parse_args(
        ["send-draft", "d", "--fingerprint", "abc", "--env-file", str(tmp_path)]
    )

    assert args.handler(args) == 0
    assert calls[0]["fingerprint"] == "abc"


def test_send_batch_fingerprints_must_cover_exactly_the_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _write_draft(tmp_path, "draft-a", "<cover-a@mailattic.test>")
    _write_draft(tmp_path, "draft-b", "<cover-b@mailattic.test>")
    mock_smtp_ssl = MagicMock()
    monkeypatch.setattr("mailattic.outbound.smtplib.SMTP_SSL", mock_smtp_ssl)

    with pytest.raises(SendRefused, match="every draft_id"):
        _send_batch(
            ["draft-a", "draft-b"],
            db_path,
            _smtp_config(),
            fingerprints={"draft-a": "0" * 64},
        )

    mock_smtp_ssl.assert_not_called()

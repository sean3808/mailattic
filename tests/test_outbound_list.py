"""Tests for outbound.list_drafts (Task 4.1) and outbound Python API (Task 6.1)."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from mailattic.outbound import (
    _write_marker,
    list_drafts,
)
from mailattic.storage import initialize_database

SMTP_USER = "sean3808@mailattic.test"


def _make_draft(tmp_path: Path, draft_id: str, message_id: str) -> Path:
    """Write a minimal .eml file into the outbound/drafts dir relative to tmp_path."""
    draft_dir = tmp_path / "outbound" / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = f"client-{draft_id}@example.com"
    msg["Cc"] = f"colleague-{draft_id}@example.com"
    msg["Subject"] = f"Draft subject {draft_id}"
    msg["Message-ID"] = message_id
    msg["Date"] = "Thu, 28 May 2026 10:00:00 +0000"
    msg.set_content("Hello", charset="utf-8")
    draft_path = draft_dir / f"{draft_id}.eml"
    draft_path.write_bytes(msg.as_bytes())
    return draft_path


# ---------------------------------------------------------------------------
# 4.1-A: two unsent drafts → list_drafts returns both
# ---------------------------------------------------------------------------


def test_list_drafts_returns_all_pending(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "test.db")

    _make_draft(tmp_path, "draft-a", "<draft-a@mailattic.test>")
    _make_draft(tmp_path, "draft-b", "<draft-b@mailattic.test>")

    result = list_drafts(db_path)

    assert len(result) == 2

    # Verify required keys are present for each entry
    for entry in result:
        assert "draft_id" in entry
        assert "recipients" in entry
        assert "subject" in entry
        assert "created_at" in entry

    draft_ids = {d["draft_id"] for d in result}
    assert draft_ids == {"draft-a", "draft-b"}


def test_list_drafts_entry_contains_to_and_cc_addresses(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _make_draft(tmp_path, "draft-x", "<draft-x@mailattic.test>")

    result = list_drafts(db_path)

    assert len(result) == 1
    recipients = result[0]["recipients"]
    # Both To and Cc addresses should appear in the combined recipients string
    assert "client-draft-x@example.com" in recipients
    assert "colleague-draft-x@example.com" in recipients


# ---------------------------------------------------------------------------
# 4.1-B: mark one draft as sent → list_drafts excludes it, one remains
# ---------------------------------------------------------------------------


def test_list_drafts_excludes_sent_marker(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "test.db")

    _make_draft(tmp_path, "draft-pending", "<draft-pending@mailattic.test>")
    _make_draft(tmp_path, "draft-sent", "<draft-sent@mailattic.test>")

    # Mark one draft as sent via _write_marker (same helper used by send_draft)
    _write_marker(db_path, "<draft-sent@mailattic.test>", "draft-sent", "sent")

    result = list_drafts(db_path)

    # Only the pending draft should be returned
    assert len(result) == 1
    assert result[0]["draft_id"] == "draft-pending"


def test_list_drafts_sent_eml_still_on_disk(tmp_path: Path) -> None:
    """The .eml file is retained on disk even after being marked sent."""
    db_path = initialize_database(tmp_path / "test.db")
    _make_draft(tmp_path, "draft-retain", "<draft-retain@mailattic.test>")

    _write_marker(db_path, "<draft-retain@mailattic.test>", "draft-retain", "sent")

    # File should still be on disk
    assert (tmp_path / "outbound" / "drafts" / "draft-retain.eml").exists()

    # But list_drafts should NOT include it
    result = list_drafts(db_path)
    assert all(d["draft_id"] != "draft-retain" for d in result)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_list_drafts_empty_when_no_drafts(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "test.db")

    result = list_drafts(db_path)

    assert result == []


def test_list_drafts_empty_when_all_sent(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    _make_draft(tmp_path, "draft-done", "<draft-done@mailattic.test>")
    _write_marker(db_path, "<draft-done@mailattic.test>", "draft-done", "sent")

    result = list_drafts(db_path)

    assert result == []


# ---------------------------------------------------------------------------
# Task 6.1: outbound Python API surface tests
# ---------------------------------------------------------------------------


def test_outbound_python_api_create_draft_returns_draft_id(tmp_path: Path) -> None:
    """generate_draft_message_id() is the public draft-creation entry point.

    A caller can use it to obtain a fresh Message-ID, write a .eml, and then
    confirm the draft is visible via list_drafts().  list_drafts() returns the
    draft_id (stem of the .eml filename) for each pending draft.
    """
    import uuid

    from mailattic.outbound import generate_draft_message_id

    db_path = initialize_database(tmp_path / "test.db")

    # Generate a draft message ID via the public API
    message_id = generate_draft_message_id()
    assert message_id.startswith("<") and message_id.endswith(">")

    # Materialise the draft on disk (as outbound_commands._handle_draft_reply does)
    draft_id = str(uuid.uuid4())
    _make_draft_with_id(tmp_path, draft_id, message_id)

    # list_drafts() sees the new draft and exposes its draft_id
    result = list_drafts(db_path)
    assert len(result) == 1
    assert result[0]["draft_id"] == draft_id


def test_outbound_send_refused_in_non_tty_context(tmp_path: Path) -> None:
    """send_draft raises SendRefused before touching SMTP when stdin is not a TTY.

    This covers Task 6.1 scenario 2: the public send_draft API rejects
    non-interactive calls, so agents cannot accidentally trigger a live send.
    """
    import sys
    from unittest.mock import MagicMock

    from mailattic.outbound import SendRefused, SmtpConfig, send_draft

    db_path = initialize_database(tmp_path / "test.db")
    message_id = "<non-tty-refused@mailattic.test>"
    _make_draft_with_id(tmp_path, "draft-non-tty", message_id)

    mock_smtp_ssl = MagicMock()

    import mailattic.outbound as outbound_module

    outbound_module.smtplib.SMTP_SSL = mock_smtp_ssl  # type: ignore[attr-defined]

    class _NonTtyStdin:
        def isatty(self) -> bool:
            return False

        def readline(self) -> str:  # pragma: no cover
            return ""

    original_stdin = sys.stdin
    sys.stdin = _NonTtyStdin()  # type: ignore[assignment]
    try:
        with __import__("pytest").raises(
            SendRefused, match="interactive authorization"
        ):
            send_draft(
                "draft-non-tty",
                db_path,
                SmtpConfig(
                    host="smtp.example.com",
                    port=465,
                    user=SMTP_USER,
                    password="secret",
                ),
                {},
            )
    finally:
        sys.stdin = original_stdin

    mock_smtp_ssl.assert_not_called()


def test_outbound_public_api_does_not_expose_raw_smtp() -> None:
    """Task 6.1 scenario 3: _smtp_submit must not be part of the public API."""
    import mailattic.outbound as outbound_module

    # Public helpers that *should* exist
    assert hasattr(outbound_module, "send_draft")
    assert hasattr(outbound_module, "list_drafts")
    assert hasattr(outbound_module, "generate_draft_message_id")

    # Private SMTP submission helper must NOT be accessible as a public name
    assert not hasattr(outbound_module, "smtp_submit")
    assert not hasattr(outbound_module, "SmtpSubmit")


# ---------------------------------------------------------------------------
# Internal helper for Task 6.1 tests (draft dir lives next to the DB)
# ---------------------------------------------------------------------------


def _make_draft_with_id(tmp_path: Path, draft_id: str, message_id: str) -> Path:
    """Write a draft .eml keyed by *draft_id* stem and *message_id* header.

    The outbound/drafts dir is resolved relative to tmp_path (same convention
    as _draft_dir in outbound.py: parent of the DB file / outbound / drafts).
    """
    draft_dir = tmp_path / "outbound" / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = "client@example.com"
    msg["Subject"] = "Test draft"
    msg["Message-ID"] = message_id
    msg["Date"] = "Thu, 28 May 2026 10:00:00 +0000"
    msg.set_content("Hello", charset="utf-8")
    path = draft_dir / f"{draft_id}.eml"
    path.write_bytes(msg.as_bytes())
    return path

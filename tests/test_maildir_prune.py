"""Tests for prune_ingested_maildir and its wiring into sync_native.

Covers all 10 acceptance criteria from design.md (prune-maildir):

  1. Old .eml with Message-ID in messages → deleted.
  2. Old .eml with Message-ID NOT in messages → kept.
  3. Old .eml with Message-ID only in ingest_failures → kept.
  4. Path-reuse collision: file at same locator as ingested message M1, but
     file's current content (M2) is absent from messages → kept (D3).
  5. Absent/empty Message-ID → fall back to dedup_key → deleted when in messages.
  6. Unparseable old .eml → kept (fail-safe).
  7. Recent (< 7-day) .eml → kept even when content is in messages.
  8. Dry-run via estimate_sync_run → no deletion (sync_native never called).
  9. Prune raises inside sync wiring → swallowed; sync exit status unaffected.
 10. Non-.eml file older than retention window → untouched.

Task 1.3 acceptance: assert sync run log contains prune counts.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from mailattic.imap_native import (
    MAILDIR_RETENTION_DAYS,
    prune_ingested_maildir,
    sync_native,
)
from mailattic.imap_sync import estimate_sync_run
from mailattic.models import MaildirSource, build_normalized_message
from mailattic.parser import _parse_eml_file
from mailattic.storage import initialize_database

# ---------------------------------------------------------------------------
# Constants and shared helpers
# ---------------------------------------------------------------------------

_ACCOUNT = "testaccount"

# Fixed reference datetime (naive; tests use .timestamp() consistently so
# timezone is not a factor as long as both sides use the same now).
_NOW = datetime(2026, 1, 10, 12, 0, 0)

_TEST_ENV = {
    "IMAP_HOST": "mail.example.com",
    "IMAP_PORT": "993",
    "IMAP_USER": "user@example.com",
    "IMAP_PASSWORD": "password",
    "IMAP_ACCOUNT_NAME": _ACCOUNT,
}


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    return db_path


def _write_eml(
    path: Path,
    *,
    message_id: str | None = "<default@example.com>",
    subject: str = "Test Subject",
    body: str = "Test body content.",
    sender: str = "sender@example.com",
) -> Path:
    """Write a minimal RFC 822 .eml file.  Pass message_id=None to omit header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"From: {sender}",
        "To: rcpt@example.com",
        f"Subject: {subject}",
        "Date: Mon, 01 Jan 2024 10:00:00 +0000",
    ]
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}")
    lines.extend(["", body])
    path.write_bytes("\r\n".join(lines).encode("utf-8"))
    return path


def _set_mtime(path: Path, *, days_old: float) -> None:
    """Set file mtime to (_NOW - days_old days).timestamp()."""
    ts = (_NOW - timedelta(days=days_old)).timestamp()
    os.utime(path, (ts, ts))


def _insert_messages_row(
    db_path: Path,
    *,
    message_id: str | None,
    dedup_key: str,
    logical_locator: str = "INBOX/new/uid-100.eml",
) -> None:
    """Insert a minimal messages row directly (no FTS update needed here)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO messages (
                subject, pst_source, folder_path, logical_locator,
                source_input_mode, source_requested_from,
                dedup_key, dedup_basis, body_text,
                recipient_search_text, attachment_search_text,
                message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "Test",
                "imap:user@example.com:INBOX",
                "INBOX",
                logical_locator,
                "maildir",
                "",
                dedup_key,
                "",
                "body",
                "",
                "",
                message_id,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _insert_ingest_failure_row(db_path: Path, *, locator: str) -> None:
    """Insert an ingest_failures row (message was attempted but NOT in messages)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO ingest_failures (
                pst_source, folder_path, logical_locator,
                source_input_mode, source_requested_from,
                error_stage, error_type, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "imap:user@example.com:INBOX",
                "INBOX",
                locator,
                "maildir",
                "",
                "persist-message",
                "OperationalError",
                "table messages has no column named raw_headers",
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _derive_content_identity(
    eml_path: Path, account_root: Path
) -> tuple[str | None, str]:
    """Return (message_id, dedup_key) via the same path as the prune helper."""
    raw = _parse_eml_file(eml_path)
    dummy = MaildirSource(
        path=account_root,
        account_email="",
        account_name=_ACCOUNT,
        folder_name="",
        requested_from=account_root,
    )
    normalized = build_normalized_message(
        raw, source=dummy, folder_path="", logical_locator=""
    )
    return normalized.message_id, normalized.dedup_key


# ---------------------------------------------------------------------------
# 1. Old .eml with Message-ID in messages → deleted
# ---------------------------------------------------------------------------


def test_old_eml_with_message_id_in_db_is_deleted(tmp_path: Path) -> None:
    """Acceptance criterion 1: stale ingested .eml is removed."""
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"
    eml = maildir / _ACCOUNT / "INBOX" / "new" / "uid-100.eml"
    mid = "<uid-100@example.com>"

    _write_eml(eml, message_id=mid, subject="Ingested message")
    _set_mtime(eml, days_old=10)
    _insert_messages_row(db, message_id=mid, dedup_key=f"message-id:{mid}")

    counts = prune_ingested_maildir(maildir, _ACCOUNT, db, MAILDIR_RETENTION_DAYS, _NOW)

    assert not eml.exists(), "stale durably-ingested .eml must be deleted"
    assert counts == {
        "deleted": 1,
        "kept_not_in_db": 0,
        "kept_unparseable": 0,
        "skipped_recent": 0,
    }


# ---------------------------------------------------------------------------
# 2. Old .eml with Message-ID NOT in messages → kept
# ---------------------------------------------------------------------------


def test_old_eml_with_message_id_not_in_db_is_kept(tmp_path: Path) -> None:
    """Acceptance criterion 2: un-ingested .eml must never be deleted."""
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"
    eml = maildir / _ACCOUNT / "INBOX" / "new" / "uid-200.eml"
    mid = "<uid-200-not-ingested@example.com>"

    _write_eml(eml, message_id=mid)
    _set_mtime(eml, days_old=10)
    # No row inserted in messages

    counts = prune_ingested_maildir(maildir, _ACCOUNT, db, MAILDIR_RETENTION_DAYS, _NOW)

    assert eml.exists(), "un-ingested .eml must be retained"
    assert counts["kept_not_in_db"] == 1
    assert counts["deleted"] == 0


# ---------------------------------------------------------------------------
# 3. Old .eml with Message-ID only in ingest_failures → kept
# ---------------------------------------------------------------------------


def test_old_eml_in_ingest_failures_only_is_kept(tmp_path: Path) -> None:
    """Acceptance criterion 3: ingest-failure .eml is the only local copy → retain."""
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"
    locator = "INBOX/new/uid-101.eml"
    eml = maildir / _ACCOUNT / locator
    mid = "<uid-101@example.com>"

    _write_eml(eml, message_id=mid)
    _set_mtime(eml, days_old=10)
    _insert_ingest_failure_row(db, locator=locator)
    # message_id is in ingest_failures only, NOT in messages

    counts = prune_ingested_maildir(maildir, _ACCOUNT, db, MAILDIR_RETENTION_DAYS, _NOW)

    assert eml.exists(), "ingest-failure .eml must be retained (only local copy)"
    assert counts["kept_not_in_db"] == 1
    assert counts["deleted"] == 0


# ---------------------------------------------------------------------------
# 4. Path-reuse collision → kept (D3 / Critical constraint 2)
# ---------------------------------------------------------------------------


def test_path_reuse_collision_is_kept(tmp_path: Path) -> None:
    """Acceptance criterion 4: content identity of CURRENT file determines fate.

    Scenario: uid-100.eml once held M1 (now in messages).  The path was later
    overwritten with M2 (failed to ingest, not in messages).  The prune must
    look at the CURRENT file content, not the locator, so M2 is retained.
    """
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"
    eml = maildir / _ACCOUNT / "INBOX" / "new" / "uid-100.eml"

    # Current file content has Message-ID m2, which is NOT in messages.
    mid_current = "<m2-new-content@example.com>"
    _write_eml(eml, message_id=mid_current)
    _set_mtime(eml, days_old=10)

    # Older message M1 with the SAME locator IS in messages (old ingest).
    mid_old = "<m1-old-ingested@example.com>"
    _insert_messages_row(
        db,
        message_id=mid_old,
        dedup_key=f"message-id:{mid_old}",
        logical_locator="INBOX/new/uid-100.eml",  # same path as current file
    )

    counts = prune_ingested_maildir(maildir, _ACCOUNT, db, MAILDIR_RETENTION_DAYS, _NOW)

    assert eml.exists(), (
        "path match alone must NOT delete; only current content identity matters"
    )
    assert counts["kept_not_in_db"] == 1
    assert counts["deleted"] == 0


# ---------------------------------------------------------------------------
# 5. Absent/empty Message-ID → fallback to dedup_key → deleted when in messages
# ---------------------------------------------------------------------------


def test_no_message_id_falls_back_to_dedup_key_and_deletes(tmp_path: Path) -> None:
    """Acceptance criterion 5: Message-ID absent → dedup_key fallback works."""
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"
    account_root = maildir / _ACCOUNT
    eml = account_root / "INBOX" / "new" / "uid-fallback.eml"

    _write_eml(
        eml,
        message_id=None,  # omit Message-ID header
        subject="Fallback dedup subject",
        body="Unique fallback body text for dedup test.",
        sender="fallback@example.com",
    )
    _set_mtime(eml, days_old=10)

    # Derive the dedup_key using the same code path the prune helper will use.
    resolved_mid, dedup_key = _derive_content_identity(eml, account_root)
    assert resolved_mid is None, "test setup: EML must have no Message-ID"
    assert dedup_key.startswith("fallback:"), "test setup: must produce fallback key"

    _insert_messages_row(db, message_id=None, dedup_key=dedup_key)

    counts = prune_ingested_maildir(maildir, _ACCOUNT, db, MAILDIR_RETENTION_DAYS, _NOW)

    assert not eml.exists(), (
        "dedup_key fallback must delete durably-ingested no-Message-ID .eml"
    )
    assert counts["deleted"] == 1
    assert counts["kept_not_in_db"] == 0


# ---------------------------------------------------------------------------
# 6. Unparseable old .eml → kept (fail-safe)
# ---------------------------------------------------------------------------


def test_unparseable_eml_is_kept(tmp_path: Path, monkeypatch) -> None:
    """Acceptance criterion 6: any parse/normalize exception → retain (fail-safe)."""
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"
    eml = maildir / _ACCOUNT / "INBOX" / "new" / "bad.eml"
    eml.parent.mkdir(parents=True, exist_ok=True)
    eml.write_bytes(b"\x00\x01\x02\x03garbage bytes")
    _set_mtime(eml, days_old=10)

    # Force a parse exception in the module-level _parse_eml_file used by the helper.
    def _always_raise(path: Path, **kwargs):
        raise ValueError("simulated parse failure")

    monkeypatch.setattr("mailattic.imap_native._parse_eml_file", _always_raise)

    counts = prune_ingested_maildir(maildir, _ACCOUNT, db, MAILDIR_RETENTION_DAYS, _NOW)

    assert eml.exists(), "unparseable .eml must never be deleted"
    assert counts["kept_unparseable"] == 1
    assert counts["deleted"] == 0


# ---------------------------------------------------------------------------
# 7. Recent (< 7-day) .eml → kept even when content is in messages
# ---------------------------------------------------------------------------


def test_recent_eml_is_kept(tmp_path: Path) -> None:
    """Acceptance criterion 7: files within the retention window are never touched."""
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"
    eml = maildir / _ACCOUNT / "INBOX" / "new" / "uid-recent.eml"
    mid = "<recent@example.com>"

    _write_eml(eml, message_id=mid)
    _set_mtime(eml, days_old=2)  # 2 days old — inside the 7-day window
    _insert_messages_row(db, message_id=mid, dedup_key=f"message-id:{mid}")

    counts = prune_ingested_maildir(maildir, _ACCOUNT, db, MAILDIR_RETENTION_DAYS, _NOW)

    assert eml.exists(), "recent .eml must never be deleted regardless of DB presence"
    assert counts["skipped_recent"] == 1
    assert counts["deleted"] == 0


# ---------------------------------------------------------------------------
# 8. Dry-run via estimate_sync_run → no deletion
# ---------------------------------------------------------------------------


def test_dry_run_performs_no_deletion(tmp_path: Path) -> None:
    """Acceptance criterion 8: estimate_sync_run never calls sync_native → no prune."""
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"
    eml = maildir / _ACCOUNT / "INBOX" / "new" / "uid-dryrun.eml"
    mid = "<dryrun@example.com>"

    _write_eml(eml, message_id=mid)
    _set_mtime(eml, days_old=10)
    _insert_messages_row(db, message_id=mid, dedup_key=f"message-id:{mid}")

    # Minimal env file required by estimate_sync_run.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "IMAP_HOST=mail.example.com\n"
        "IMAP_PORT=993\n"
        "IMAP_USER=user@example.com\n"
        "IMAP_PASSWORD=secret\n"
        f"IMAP_ACCOUNT_NAME={_ACCOUNT}\n",
        encoding="utf-8",
    )

    class _FakeDryRunClient:
        class _Folder:
            """Local stand-in for imap_native.ImapFolder (no import needed)."""

            def __init__(
                self, display_name: str, wire_name: str, delimiter: str
            ) -> None:
                self.display_name = display_name
                self.wire_name = wire_name
                self.delimiter = delimiter

        def list_folders(self):
            return [self._Folder("INBOX", "INBOX", "/")]

        def get_uidvalidity(self, folder):
            return 1

        def select_folder(self, folder):
            pass

        def search_uids_after(self, watermark):
            return []

        def close(self):
            pass

    estimate_sync_run(
        env_file=env_file,
        maildir_root=maildir,
        client_factory=lambda env: _FakeDryRunClient(),
    )

    assert eml.exists(), (
        "dry-run (estimate_sync_run) must never delete files; "
        "sync_native — and the prune it calls — is never reached in dry-run mode"
    )


# ---------------------------------------------------------------------------
# 9. Prune raises inside sync wiring → swallowed; sync exit status unaffected
# ---------------------------------------------------------------------------


def test_prune_error_swallowed_by_sync_wiring(tmp_path: Path, monkeypatch) -> None:
    """Acceptance criterion 9: prune exception must not propagate to sync result."""
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"

    class _EmptyClient:
        def list_folders(self):
            return []

        def close(self):
            pass

    # Make prune_ingested_maildir raise unconditionally.
    def _always_raise(*args, **kwargs):
        raise RuntimeError("simulated prune failure")

    monkeypatch.setattr(
        "mailattic.imap_native.prune_ingested_maildir",
        _always_raise,
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db,
        maildir_root=maildir,
        state_path=tmp_path / ".watermarks.json",
        log_dir=tmp_path / "logs",
        client_factory=lambda env: _EmptyClient(),
    )

    assert result.exit_code == 0, (
        "prune error must be swallowed; sync exit code unaffected"
    )
    assert result.failure_count == 0


# ---------------------------------------------------------------------------
# 10. Non-.eml file older than retention window → untouched
# ---------------------------------------------------------------------------


def test_non_eml_file_is_untouched(tmp_path: Path) -> None:
    """Acceptance criterion 10: only *.eml files are candidates for deletion."""
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"
    notes_txt = maildir / _ACCOUNT / "INBOX" / "new" / "notes.txt"
    notes_txt.parent.mkdir(parents=True, exist_ok=True)
    notes_txt.write_text("some notes", encoding="utf-8")
    _set_mtime(notes_txt, days_old=30)

    counts = prune_ingested_maildir(maildir, _ACCOUNT, db, MAILDIR_RETENTION_DAYS, _NOW)

    assert notes_txt.exists(), "non-.eml files must never be touched"
    # No .eml files were present, so all counts are zero.
    assert counts == {
        "deleted": 0,
        "kept_not_in_db": 0,
        "kept_unparseable": 0,
        "skipped_recent": 0,
    }


# ---------------------------------------------------------------------------
# Task 1.3: sync run log contains prune counts (logger.info output)
# ---------------------------------------------------------------------------


def test_sync_wiring_logs_prune_counts(tmp_path: Path, caplog) -> None:
    """Task 1.3: sync_native emits an INFO log line with prune counts."""
    db = _make_db(tmp_path)
    maildir = tmp_path / "maildir"
    eml = maildir / _ACCOUNT / "INBOX" / "new" / "uid-logged.eml"
    mid = "<logged@example.com>"

    _write_eml(eml, message_id=mid)
    _set_mtime(eml, days_old=10)
    _insert_messages_row(db, message_id=mid, dedup_key=f"message-id:{mid}")

    class _EmptyClient:
        def list_folders(self):
            return []

        def close(self):
            pass

    with caplog.at_level(logging.INFO, logger="mailattic.imap_native"):
        sync_native(
            env=_TEST_ENV,
            database_path=db,
            maildir_root=maildir,
            state_path=tmp_path / ".watermarks.json",
            log_dir=tmp_path / "logs",
            client_factory=lambda env: _EmptyClient(),
        )

    log_text = " ".join(caplog.messages)
    assert "maildir prune" in log_text, "sync log must mention 'maildir prune'"
    assert "deleted=1" in log_text, "sync log must report deleted count"
    assert "kept_not_in_db=0" in log_text, "sync log must report kept_not_in_db count"

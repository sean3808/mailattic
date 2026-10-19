"""Tests for mailbox access security guards (issue #252).

Verifies the structural guarantee that test executions cannot access
production mailbox credentials (the archive_scrape and IMAP credential files)
or establish outbound socket connections to non-loopback addresses.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from mailattic import archive_scrape, rebuild, storage
from mailattic.imap_runtime import load_env_file
from mailattic.imap_sync import DEFAULT_ENV_FILE
from tests.conftest import (
    TestConnectionGuardError,
    TestCredentialsGuardError,
    is_loopback_host,
)


def test_rebuild_resume_without_env_file_reproduction_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #252 reproduction of #245 incident:
    Calling run_rebuild(resume=True) with only preflight disabled and without
    an explicit fake env_file must fail in milliseconds via credentials guard,
    leaving zero downloaded messages in the staging directory.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    stage_db = work_dir / "stage.db"
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")

    # Seed stage db and write a valid checkpoint with completed import phase,
    # matching the state in issue #245 where resume=True advanced to backfill.
    storage.initialize_database(stage_db)
    conn = storage.connect_database(stage_db)
    conn.execute(
        """
        INSERT INTO messages (
            subject, sender_name, sender_email, sent_at, received_at,
            body_text, message_id, pst_source, folder_path, logical_locator,
            source_input_mode, source_requested_from, dedup_key, dedup_basis,
            recipient_search_text, attachment_search_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "Subject",
            "sender@example.com",
            "sender@example.com",
            "2026-06-01T09:00:00",
            "2026-06-01T09:01:00",
            "body",
            "<msg@example.com>",
            str(pst_path.resolve()),
            "Inbox",
            f"Inbox/{pst_path.stem}",
            "file",
            f"{pst_path.stem}.pst",
            "message-id:<msg@example.com>",
            '{"method":"message-id"}',
            "recipient@example.com",
            "",
        ],
    )
    conn.commit()
    conn.close()

    checkpoint_path = work_dir / "rebuild-checkpoint.json"
    checkpoint_path.write_text(
        json.dumps(
            {
                "completed_phases": ["preflight", "coverage-report", "import"],
                "imported_psts": [str(pst_path.resolve())],
                "schema_version": 1,
                "round": {
                    "stage_db": str(stage_db.resolve()),
                    "pst_inputs": [str(pst_path.resolve())],
                },
                "staged_rows": {str(pst_path.resolve()): 1},
                "staged_total": 1,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Issue #245 incident condition: only preflight phase was stubbed.
    monkeypatch.setattr(rebuild, "run_preflight_phase", lambda *_a, **_k: True)

    exit_code = rebuild.run_rebuild(
        resume=True,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=[pst_path],
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "[MailboxGuard: Credentials]" in captured.err
    assert ".env.sean3808" in captured.err
    assert "test_rebuild_resume_without_env_file_reproduction_blocked" in captured.err

    # Guarantee: no messages were downloaded to the staging maildir.
    rebuild_maildir = work_dir / ".maildir-rebuild"
    downloaded_emls = (
        list(rebuild_maildir.glob("**/*.eml")) if rebuild_maildir.exists() else []
    )
    assert len(downloaded_emls) == 0


def test_connection_guard_blocks_non_loopback_socket() -> None:
    """Connection guard test: only violates connection guard (no credentials touched).
    Uses RFC 5737 TEST-NET-1 reserved address 192.0.2.1.
    """
    target = ("192.0.2.1", 993)
    with pytest.raises(TestConnectionGuardError) as exc_info:
        socket.create_connection(target)

    msg = str(exc_info.value)
    assert "[MailboxGuard: Connection]" in msg
    assert "192.0.2.1:993" in msg
    assert "test_connection_guard_blocks_non_loopback_socket" in msg


def test_credentials_guard_blocks_real_env_file() -> None:
    """Credentials guard test: only violates credentials guard."""
    with pytest.raises(TestCredentialsGuardError) as exc_info:
        load_env_file(DEFAULT_ENV_FILE)

    msg = str(exc_info.value)
    assert "[MailboxGuard: Credentials]" in msg
    assert ".env.sean3808" in msg
    assert "test_credentials_guard_blocks_real_env_file" in msg


def test_connection_guard_allows_loopback() -> None:
    """Loopback addresses must be allowed through the guard."""
    assert is_loopback_host("localhost")
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("sub.localhost")

    # A real connect to an unbound loopback port should produce OS
    # ConnectionRefusedError, NOT TestConnectionGuardError.
    with pytest.raises(OSError) as exc_info:
        socket.create_connection(("127.0.0.1", 65431), timeout=0.1)
    assert not isinstance(exc_info.value, TestConnectionGuardError)


def test_credentials_guard_allows_fake_env_in_tmp_path(tmp_path: Path) -> None:
    """Injected fake env files in tmp_path must be allowed."""
    fake_env = tmp_path / ".env.sean3808"
    fake_env.write_text(
        "IMAP_HOST=fake.example.com\nIMAP_PORT=993\nIMAP_USER=fake@example.com\n"
        "IMAP_PASSWORD=secret\nIMAP_ACCOUNT_NAME=fake\n",
        encoding="utf-8",
    )
    loaded = load_env_file(fake_env)
    assert loaded["IMAP_HOST"] == "fake.example.com"
    assert loaded["IMAP_USER"] == "fake@example.com"


def test_credentials_guard_blocks_direct_path_read() -> None:
    """Direct Path.read_text on DEFAULT_ENV_FILE must be blocked."""
    with pytest.raises(TestCredentialsGuardError) as exc_info:
        DEFAULT_ENV_FILE.read_text(encoding="utf-8")

    msg = str(exc_info.value)
    assert "[MailboxGuard: Credentials]" in msg
    assert ".env.sean3808" in msg


def test_credentials_guard_blocks_mail_server_env_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """archive_scrape credentials path must be blocked."""
    monkeypatch.delenv("MAIL_SERVER_ENV_PATH", raising=False)
    with pytest.raises(TestCredentialsGuardError) as exc_info:
        archive_scrape.load_credentials()

    msg = str(exc_info.value)
    assert "[MailboxGuard: Credentials]" in msg
    assert "mail-server\\.env" in msg or "mail-server/.env" in msg

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from email.message import EmailMessage
from pathlib import Path

import pytest

from mailattic.imap_runtime import (
    run_sync_job,
    validate_sync_environment,
)
from mailattic.schema import SCHEMA_SQL


def test_run_sync_job_writes_attachments_to_store(tmp_path: Path, monkeypatch) -> None:
    database = _install_memory_database(monkeypatch)
    payload = b"%PDF-1.4 demo"
    database_path = tmp_path / "evidence.db"
    maildir_root = _write_maildir_message(tmp_path, payload=payload)
    attachment_root = tmp_path / "attachments"

    result = run_sync_job(
        database_path=database_path,
        maildir_root=maildir_root,
        template_path=_write_template(tmp_path),
        config_path=tmp_path / "mbsync.conf",
        attachment_root=attachment_root,
        env=_runtime_env(),
        command_runner=_fake_mbsync,
    )

    assert result.exit_code == 0
    row = database.fetch_attachment_row()
    assert row == (
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "application/pdf",
        f"attachments/{row[0][:2]}/{row[0][2:4]}/{row[0]}",
    )
    cas_path = attachment_root / row[0][:2] / row[0][2:4] / row[0]
    assert cas_path.read_bytes() == payload


def test_run_sync_job_no_attachment_root_leaves_columns_null(
    tmp_path: Path, monkeypatch
) -> None:
    database = _install_memory_database(monkeypatch)
    database_path = tmp_path / "evidence.db"
    maildir_root = _write_maildir_message(tmp_path, payload=b"%PDF-1.4 demo")
    attachment_root = tmp_path / "attachments"

    result = run_sync_job(
        database_path=database_path,
        maildir_root=maildir_root,
        template_path=_write_template(tmp_path),
        config_path=tmp_path / "mbsync.conf",
        env=_runtime_env(),
        command_runner=_fake_mbsync,
    )

    assert result.exit_code == 0
    assert database.fetch_attachment_row() == (None, None, None, None)
    assert not attachment_root.exists()


def _runtime_env() -> dict[str, str]:
    return {
        "IMAP_HOST": "imap.example.com",
        "IMAP_PORT": "993",
        "IMAP_USER": "test@example.com",
        "IMAP_PASSWORD": "secret",
        "IMAP_ACCOUNT_NAME": "testaccount",
    }


def test_validate_sync_environment_accepts_missing_security_as_tls_default() -> None:
    validate_sync_environment(_runtime_env())


def test_validate_sync_environment_accepts_explicit_tls_security() -> None:
    validate_sync_environment(_runtime_env() | {"IMAP_SECURITY": "tls"})


def test_validate_sync_environment_accepts_explicit_plain_security() -> None:
    env = _runtime_env() | {"IMAP_SECURITY": "plain", "IMAP_PORT": "143"}

    validate_sync_environment(env)


@pytest.mark.parametrize("value", ["starttls", "ssl", "true", "", "opportunistic"])
def test_validate_sync_environment_rejects_unknown_security(value: str) -> None:
    env = _runtime_env() | {"IMAP_SECURITY": value}

    with pytest.raises(
        ValueError,
        match=r"IMAP_SECURITY must be one of: tls, plain",
    ):
        validate_sync_environment(env)


def _fake_mbsync(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args[0], returncode=0, stdout="", stderr="")


def _write_template(tmp_path: Path) -> Path:
    template_path = tmp_path / "mbsyncrc.template"
    template_path.write_text(
        "\n".join(
            [
                "IMAPAccount test-remote",
                "Host ${IMAP_HOST}",
                "Port ${IMAP_PORT}",
                "User ${IMAP_USER}",
                "Pass ${IMAP_PASSWORD}",
                "SSLType IMAPS",
                "",
                "IMAPStore test-remote-store",
                "Account test-remote",
                "",
                "MaildirStore test-local-store",
                "SubFolders Verbatim",
                "Path ${MAILDIR_ROOT}/${IMAP_ACCOUNT_NAME}/",
                "Inbox ${MAILDIR_ROOT}/${IMAP_ACCOUNT_NAME}/INBOX",
                "",
                "Channel mailattic-channel",
                "Far :test-remote-store:",
                "Near :test-local-store:",
                "${MBSYNC_PATTERNS_LINE}",
                "Create Near",
                "Expunge None",
                "Sync Pull",
                "SyncState *",
                "",
                "Group mailattic-sync",
                "Channel mailattic-channel",
            ]
        ),
        encoding="utf-8",
    )
    return template_path


def _write_maildir_message(tmp_path: Path, *, payload: bytes) -> Path:
    maildir_root = tmp_path / "maildir"
    folder_root = maildir_root / "testaccount" / "INBOX"
    for bucket_name in ("cur", "new", "tmp"):
        (folder_root / bucket_name).mkdir(parents=True, exist_ok=True)

    message = EmailMessage()
    message["From"] = "Alice <alice@example.com>"
    message["To"] = "Bob <bob@example.com>"
    message["Subject"] = "Attachment quote"
    message["Date"] = "Tue, 02 Jan 2024 03:04:05 +0000"
    message["Message-ID"] = "<quote@example.com>"
    message.set_content("Please see attached quote.")
    message.add_attachment(
        payload,
        maintype="application",
        subtype="pdf",
        filename="quote.pdf",
    )
    (folder_root / "cur" / "message.eml").write_bytes(message.as_bytes())
    return maildir_root


def _install_memory_database(monkeypatch):
    database = _MemoryDatabase()
    monkeypatch.setattr(
        "mailattic.imap_runtime.initialize_database",
        lambda database_path: Path(database_path),
    )
    monkeypatch.setattr(
        "mailattic.ingest.connect_database",
        lambda database_path: database.connect(),
    )
    return database


class _MemoryDatabase:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON;")
        self.connection.executescript(SCHEMA_SQL)

    def connect(self):
        return _NoCloseConnection(self.connection)

    def fetch_attachment_row(self) -> tuple[object, object, object, object]:
        row = self.connection.execute(
            "SELECT content_hash, size_bytes, mime_type, storage_path "
            "FROM message_attachments"
        ).fetchone()
        assert row is not None
        return tuple(row)


class _NoCloseConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, *args, **kwargs):
        return self._connection.execute(*args, **kwargs)

    def commit(self) -> None:
        self._connection.commit()

    def close(self) -> None:
        return None

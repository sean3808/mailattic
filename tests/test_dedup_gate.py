from __future__ import annotations

import sqlite3
from email.message import EmailMessage
from pathlib import Path

from mailattic.ingest import import_maildir_sources
from mailattic.models import (
    MaildirSource,
    PstSource,
    build_normalized_message,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)


def test_pst_then_imap(tmp_path: Path) -> None:
    database_path = initialize_database(tmp_path / "evidence.db")
    pst_message = build_normalized_message(
        {
            "subject": "Cross-source",
            "sender": {"name": "Alice", "email": "alice@example.com"},
            "to": [{"email": "bob@example.com"}],
            "sent_at": "2024-01-02T03:04:05+00:00",
            "body_text": "Body text\n",
            "message_id": "<same@example.com>",
        },
        source=PstSource(
            path=Path("C:/archive/mailbox.pst"),
            input_mode="file",
            requested_from=Path("C:/archive/mailbox.pst"),
        ),
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    connection = connect_database(database_path)
    try:
        persist_message(connection, pst_message)
        connection.commit()
    finally:
        connection.close()

    maildir_source = _write_maildir_message(
        tmp_path=tmp_path,
        folder_name="INBOX",
        subject="Cross-source",
        message_id="<same@example.com>",
    )
    summary = import_maildir_sources(
        database_path=database_path,
        sources=[maildir_source],
    )

    assert summary.imported_messages == 0
    assert summary.duplicate_messages == 1
    assert _message_count(database_path) == 1
    assert _only_source(database_path) == str(Path("C:/archive/mailbox.pst"))


def test_imap_then_pst(tmp_path: Path) -> None:
    database_path = initialize_database(tmp_path / "evidence.db")
    maildir_source = _write_maildir_message(
        tmp_path=tmp_path,
        folder_name="INBOX",
        subject="Cross-source",
        message_id="<same@example.com>",
    )
    summary = import_maildir_sources(
        database_path=database_path,
        sources=[maildir_source],
    )
    assert summary.imported_messages == 1

    pst_message = build_normalized_message(
        {
            "subject": "Cross-source",
            "sender": {"name": "Alice", "email": "alice@example.com"},
            "to": [{"email": "bob@example.com"}],
            "sent_at": "2024-01-02T03:04:05+00:00",
            "body_text": "Body text\n",
            "message_id": "<same@example.com>",
        },
        source=PstSource(
            path=Path("C:/archive/mailbox.pst"),
            input_mode="file",
            requested_from=Path("C:/archive/mailbox.pst"),
        ),
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    connection = connect_database(database_path)
    try:
        result = persist_message(connection, pst_message)
        connection.commit()
    finally:
        connection.close()

    assert not result.inserted
    assert _message_count(database_path) == 1
    assert _only_source(database_path) == "imap:sean3808@mailattic.test:INBOX"


def test_fallback_cross_source(tmp_path: Path) -> None:
    database_path = initialize_database(tmp_path / "evidence.db")
    pst_message = build_normalized_message(
        {
            "subject": "No message id",
            "sender": {"name": "Alice", "email": "alice@example.com"},
            "to": [{"email": "bob@example.com"}],
            "sent_at": "2024-01-02T03:04:05+00:00",
            "body_text": "Body text\n",
        },
        source=PstSource(
            path=Path("C:/archive/mailbox.pst"),
            input_mode="file",
            requested_from=Path("C:/archive/mailbox.pst"),
        ),
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    connection = connect_database(database_path)
    try:
        persist_message(connection, pst_message)
        connection.commit()
    finally:
        connection.close()

    maildir_source = _write_maildir_message(
        tmp_path=tmp_path,
        folder_name="INBOX",
        subject="No message id",
        message_id=None,
    )
    summary = import_maildir_sources(
        database_path=database_path,
        sources=[maildir_source],
    )

    assert summary.imported_messages == 0
    assert summary.duplicate_messages == 1
    assert _message_count(database_path) == 1


def _write_maildir_message(
    *,
    tmp_path: Path,
    folder_name: str,
    subject: str,
    message_id: str | None,
) -> MaildirSource:
    folder_root = tmp_path / "maildir" / "sean3808" / folder_name
    (folder_root / "cur").mkdir(parents=True, exist_ok=True)

    message = EmailMessage()
    message["From"] = "Alice <alice@example.com>"
    message["To"] = "Bob <bob@example.com>"
    message["Subject"] = subject
    message["Date"] = "Tue, 02 Jan 2024 03:04:05 +0000"
    if message_id is not None:
        message["Message-ID"] = message_id
    message.set_content("Body text")
    (folder_root / "cur" / "message.eml").write_bytes(message.as_bytes())
    return MaildirSource(
        path=folder_root,
        account_email="sean3808@mailattic.test",
        account_name="sean3808",
        folder_name=folder_name,
        requested_from=folder_root.parent,
    )


def _message_count(database_path: Path) -> int:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        connection.close()


def _only_source(database_path: Path) -> str:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute("SELECT pst_source FROM messages").fetchone()[0]
    finally:
        connection.close()

from __future__ import annotations

import sqlite3
from pathlib import Path

from mailattic.storage import initialize_database


def _column_names(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(messages)")}


def test_fresh_init_includes_thread_bridge_columns(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "fresh.db")

    conn = sqlite3.connect(db_path)
    try:
        columns = _column_names(conn)
    finally:
        conn.close()

    assert "thread_bridge_target" in columns
    assert "thread_bridge_source" in columns


def test_bridge_schema_migration_is_idempotent_and_preserves_headers(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL DEFAULT '',
                sender_name TEXT,
                sender_email TEXT,
                sent_at TEXT,
                received_at TEXT,
                body_text TEXT NOT NULL DEFAULT '',
                message_id TEXT,
                in_reply_to TEXT,
                references_header TEXT,
                pst_source TEXT NOT NULL,
                folder_path TEXT NOT NULL,
                logical_locator TEXT NOT NULL,
                source_input_mode TEXT NOT NULL DEFAULT '',
                source_requested_from TEXT NOT NULL DEFAULT '',
                dedup_key TEXT NOT NULL,
                dedup_basis TEXT NOT NULL DEFAULT '',
                recipient_search_text TEXT NOT NULL DEFAULT '',
                attachment_search_text TEXT NOT NULL DEFAULT '',
                thread_key INTEGER,
                raw_headers TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE message_recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                recipient_type TEXT NOT NULL,
                display_name TEXT,
                email TEXT
            );
            CREATE TABLE message_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                attachment_name TEXT NOT NULL,
                content_hash TEXT,
                size_bytes INTEGER,
                mime_type TEXT,
                storage_path TEXT
            );
            CREATE TABLE ingest_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pst_source TEXT NOT NULL,
                folder_path TEXT,
                logical_locator TEXT,
                source_input_mode TEXT NOT NULL DEFAULT '',
                source_requested_from TEXT NOT NULL DEFAULT '',
                error_stage TEXT NOT NULL,
                error_type TEXT NOT NULL,
                error_message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE VIRTUAL TABLE message_fts USING fts5(
                message_rowid UNINDEXED,
                subject,
                sender,
                recipients,
                body_text,
                attachment_names
            );
            INSERT INTO messages (
                subject,
                sender_email,
                body_text,
                message_id,
                in_reply_to,
                references_header,
                pst_source,
                folder_path,
                logical_locator,
                dedup_key,
                raw_headers
            ) VALUES (
                'Re: Evidence',
                'sender@example.com',
                'body',
                '<msg@example.com>',
                '<parent@example.com>',
                '<root@example.com> <parent@example.com>',
                'test.pst',
                '/Inbox',
                'loc-1',
                'dedup-1',
                'Message-ID: <msg@example.com>'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    initialize_database(db_path)
    initialize_database(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        columns = _column_names(conn)
        row = conn.execute(
            """
            SELECT
                message_id,
                in_reply_to,
                references_header,
                raw_headers,
                thread_bridge_target,
                thread_bridge_source
            FROM messages
            WHERE dedup_key = ?
            """,
            ["dedup-1"],
        ).fetchone()
    finally:
        conn.close()

    assert "thread_bridge_target" in columns
    assert "thread_bridge_source" in columns
    assert row is not None
    assert row["message_id"] == "<msg@example.com>"
    assert row["in_reply_to"] == "<parent@example.com>"
    assert row["references_header"] == "<root@example.com> <parent@example.com>"
    assert row["raw_headers"] == "Message-ID: <msg@example.com>"
    assert row["thread_bridge_target"] is None
    assert row["thread_bridge_source"] is None

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
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
    thread_bridge_target TEXT,
    thread_bridge_source TEXT
        CHECK (
            thread_bridge_source IS NULL
            OR thread_bridge_source = 'subject_heuristic'
        ),
    raw_headers TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS message_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('to', 'cc', 'bcc')),
    display_name TEXT,
    email TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    attachment_name TEXT NOT NULL,
    content_hash TEXT,
    size_bytes INTEGER,
    mime_type TEXT,
    storage_path TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ingest_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pst_source TEXT NOT NULL,
    folder_path TEXT,
    logical_locator TEXT,
    source_input_mode TEXT NOT NULL DEFAULT '',
    source_requested_from TEXT NOT NULL DEFAULT '',
    error_stage TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attachment_name TEXT,
    attachment_size INTEGER,
    resolved_at TEXT,
    abandon_reason TEXT
);

CREATE TABLE IF NOT EXISTS self_addresses (
    address TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sync_state (
    sync_key TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('running', 'completed', 'failed'))
);

-- IMAP sync progress lives in the DB so it travels with backup/restore: the
-- row is written in the same transaction that commits the message (#223).
CREATE TABLE IF NOT EXISTS imap_sync_progress (
    account_name TEXT NOT NULL,
    folder TEXT NOT NULL,
    delimiter TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    last_uid INTEGER NOT NULL,
    PRIMARY KEY (account_name, folder, delimiter, uidvalidity)
);

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    message_rowid UNINDEXED,
    subject,
    sender,
    recipients,
    body_text,
    attachment_names
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedup_key
    ON messages(dedup_key);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_message_id
    ON messages(message_id)
    WHERE message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_sender_email
    ON messages(sender_email);

CREATE INDEX IF NOT EXISTS idx_messages_sent_at
    ON messages(sent_at);

CREATE INDEX IF NOT EXISTS idx_failures_pst_source
    ON ingest_failures(pst_source);

CREATE INDEX IF NOT EXISTS idx_message_recipients_message_id
    ON message_recipients(message_id);

CREATE INDEX IF NOT EXISTS idx_message_attachments_message_id
    ON message_attachments(message_id);
"""

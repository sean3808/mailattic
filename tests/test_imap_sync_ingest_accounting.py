from __future__ import annotations

import sqlite3
from pathlib import Path

import mailattic.imap_native as imap_native
from mailattic.imap_native import NativeImapClient, sync_native
from mailattic.imap_state import ImapSyncState
from mailattic.models import ImportSummary
from mailattic.storage import initialize_database
from tests.test_imap_native import _TEST_ENV, _make_eml, _MockImapConn


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    return db_path


def test_message_stage_failure_blocks_watermark_and_retries_failed_uid(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"
    list_data = [b'(\\HasNoChildren) "/" INBOX']
    fetch_map = {101: _make_eml(101), 102: _make_eml(102), 103: _make_eml(103)}

    first_conn = _MockImapConn(
        list_data=list_data,
        status_map={"INBOX": 1},
        search_map={"INBOX": [101, 102, 103]},
        fetch_map=fetch_map,
    )

    import mailattic.ingest as ingest_mod

    real_persist_message = ingest_mod.persist_message

    def _persist_message(connection, normalized_message):
        if normalized_message.logical_locator.endswith("uid-102.eml"):
            raise RuntimeError("simulated persist failure for UID 102")
        return real_persist_message(connection, normalized_message)

    monkeypatch.setattr(ingest_mod, "persist_message", _persist_message)

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=first_conn),
    )

    state = ImapSyncState(state_path)
    assert state.get_watermark("INBOX", "/", 1) == 101
    assert result.pulled_messages == 2
    assert result.inserted_messages == 1
    assert result.failure_count > 0
    assert result.exit_code == 1

    with sqlite3.connect(db_path) as connection:
        failure_row = connection.execute(
            "SELECT error_stage FROM ingest_failures WHERE logical_locator = ?",
            ("INBOX/new/uid-102.eml",),
        ).fetchone()
        third_uid_row = connection.execute(
            "SELECT 1 FROM messages WHERE message_id = ?",
            ("<test-103@example.com>",),
        ).fetchone()

    assert failure_row == ("persist-message",)
    assert third_uid_row is None

    monkeypatch.setattr(ingest_mod, "persist_message", real_persist_message)
    second_conn = _MockImapConn(
        list_data=list_data,
        status_map={"INBOX": 1},
        search_map={"INBOX": [101, 102, 103]},
        fetch_map=fetch_map,
    )

    retry_result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=second_conn),
    )

    assert retry_result.exit_code == 0
    assert [uid for uid, _ in second_conn.fetch_calls] == ["102", "103"]
    assert ImapSyncState(state_path).get_watermark("INBOX", "/", 1) == 103


def test_attachment_sub_failure_still_advances_watermark(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"
    conn = _MockImapConn(
        list_data=[b'(\\HasNoChildren) "/" INBOX'],
        status_map={"INBOX": 1},
        search_map={"INBOX": [201]},
        fetch_map={201: _make_eml(201)},
    )

    real_import_maildir_message = imap_native.import_maildir_message

    def _import_maildir_message(**kwargs):
        summary = real_import_maildir_message(**kwargs)
        # Existing harness does not expose a cheap real attachment-decode fault,
        # so this overlays failure_count=1 on a real persisted message row.
        return ImportSummary(
            imported_messages=summary.imported_messages,
            duplicate_messages=summary.duplicate_messages,
            failure_count=summary.failure_count + 1,
        )

    monkeypatch.setattr(imap_native, "import_maildir_message", _import_maildir_message)

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=conn),
    )

    with sqlite3.connect(db_path) as connection:
        message_row = connection.execute(
            "SELECT 1 FROM messages WHERE message_id = ?",
            ("<test-201@example.com>",),
        ).fetchone()

    state = ImapSyncState(state_path)
    assert message_row == (1,)
    assert state.get_watermark("INBOX", "/", 1) == 201
    assert result.failure_count == 0
    assert result.exit_code == 0

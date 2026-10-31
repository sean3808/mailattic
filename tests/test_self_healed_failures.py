from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import mailattic.imap_native as imap_native_mod
from mailattic.imap_native import NativeImapClient, sync_native
from mailattic.imap_state import ImapSyncState
from mailattic.ingest import import_maildir_sources
from mailattic.storage import close_self_healed_failures
from tests.test_imap_native import _TEST_ENV, _make_db, _make_eml, _MockImapConn


def test_self_heal_closes_message_stage_failure_same_source_locator(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO ingest_failures (
                pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at
            ) VALUES (
                'imap:test@example.com:INBOX', 'INBOX',
                'INBOX/new/uid-101.eml', 'persist-message',
                'OperationalError', 'table messages has no column raw_headers',
                '2026-06-24 10:00:00'
            )
            """
        )
        con.execute(
            """
            INSERT INTO messages (
                subject, sender_name, sender_email, pst_source, folder_path,
                logical_locator, dedup_key, created_at
            ) VALUES (
                'Test', 'Sender', 'sender@example.com',
                'imap:test@example.com:INBOX', 'INBOX',
                'INBOX/new/uid-101.eml', 'msg-101', '2026-06-24 10:05:00'
            )
            """
        )
        con.commit()

        closed = close_self_healed_failures(con)
        assert closed == 1

        row = con.execute(
            "SELECT resolved_at, abandon_reason FROM ingest_failures WHERE id = 1"
        ).fetchone()
        assert row is not None
        assert row[0] is not None
        assert row[1] is None


def test_self_heal_leaves_open_when_only_message_id_matches_under_other_source(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO ingest_failures (
                pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at
            ) VALUES (
                'imap:test@example.com:INBOX', 'INBOX',
                'INBOX/new/uid-101.eml', 'persist-message',
                'OperationalError', 'failure', '2026-06-24 10:00:00'
            )
            """
        )
        # Message has the same Message-ID, but arrived under another source
        con.execute(
            """
            INSERT INTO messages (
                subject, message_id, pst_source, folder_path, logical_locator,
                dedup_key, created_at
            ) VALUES (
                'Test', '<mid-101@example.com>', 'pst:other.pst', 'Inbox',
                'other-locator', 'msg-101', '2026-06-24 10:05:00'
            )
            """
        )
        con.commit()

        closed = close_self_healed_failures(con)
        assert closed == 0

        row = con.execute(
            "SELECT resolved_at FROM ingest_failures WHERE id = 1"
        ).fetchone()
        assert row[0] is None


def test_self_heal_leaves_open_when_only_message_id_matches_under_other_locator(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO ingest_failures (
                pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at
            ) VALUES (
                'imap:test@example.com:INBOX', 'INBOX',
                'INBOX/new/uid-101.eml', 'persist-message',
                'OperationalError', 'failure', '2026-06-24 10:00:00'
            )
            """
        )
        # Same source, but different locator
        con.execute(
            """
            INSERT INTO messages (
                subject, message_id, pst_source, folder_path, logical_locator,
                dedup_key, created_at
            ) VALUES (
                'Test', '<mid-101@example.com>',
                'imap:test@example.com:INBOX', 'INBOX',
                'INBOX/new/uid-999.eml', 'msg-101', '2026-06-24 10:05:00'
            )
            """
        )
        con.commit()

        closed = close_self_healed_failures(con)
        assert closed == 0

        row = con.execute(
            "SELECT resolved_at FROM ingest_failures WHERE id = 1"
        ).fetchone()
        assert row[0] is None


def test_self_heal_leaves_open_when_nothing_stored(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO ingest_failures (
                pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at
            ) VALUES (
                'imap:test@example.com:INBOX', 'INBOX',
                'INBOX/new/uid-101.eml', 'persist-message',
                'OperationalError', 'failure', '2026-06-24 10:00:00'
            )
            """
        )
        con.commit()

        closed = close_self_healed_failures(con)
        assert closed == 0

        row = con.execute(
            "SELECT resolved_at FROM ingest_failures WHERE id = 1"
        ).fetchone()
        assert row[0] is None


def test_self_heal_never_touches_attachment_stage_rows(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO ingest_failures (
                id, pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at
            ) VALUES
            (1, 'imap:test@example.com:INBOX', 'INBOX', 'loc1',
             'attachment-store', 'err', 'msg', '2026-06-24 10:00:00'),
            (2, 'imap:test@example.com:INBOX', 'INBOX', 'loc1',
             'attachment-decode', 'err', 'msg', '2026-06-24 10:00:00'),
            (3, 'imap:test@example.com:INBOX', 'INBOX', 'loc1',
             'attachment-collection-read', 'err', 'msg', '2026-06-24 10:00:00')
            """
        )
        con.execute(
            """
            INSERT INTO messages (
                subject, pst_source, folder_path, logical_locator,
                dedup_key, created_at
            ) VALUES (
                'Test', 'imap:test@example.com:INBOX', 'INBOX', 'loc1',
                'd1', '2026-06-24 10:05:00'
            )
            """
        )
        con.commit()

        closed = close_self_healed_failures(con)
        assert closed == 0

        rows = con.execute(
            "SELECT id, resolved_at FROM ingest_failures ORDER BY id"
        ).fetchall()
        assert rows == [(1, None), (2, None), (3, None)]


def test_self_heal_never_touches_already_resolved_or_abandoned_rows(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO ingest_failures (
                id, pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at, resolved_at,
                abandon_reason
            ) VALUES
            (1, 'imap:test@example.com:INBOX', 'INBOX', 'loc1',
             'persist-message', 'err', 'msg',
             '2026-06-24 10:00:00', '2026-06-24 10:01:00', 'human-abandoned'),
            (2, 'imap:test@example.com:INBOX', 'INBOX', 'loc1',
             'persist-message', 'err', 'msg',
             '2026-06-24 10:00:00', '2026-06-24 10:02:00', NULL)
            """
        )
        con.execute(
            """
            INSERT INTO messages (
                subject, pst_source, folder_path, logical_locator,
                dedup_key, created_at
            ) VALUES (
                'Test', 'imap:test@example.com:INBOX', 'INBOX', 'loc1',
                'd1', '2026-06-24 10:05:00'
            )
            """
        )
        con.commit()

        closed = close_self_healed_failures(con)
        assert closed == 0

        rows = con.execute(
            "SELECT id, resolved_at, abandon_reason FROM ingest_failures ORDER BY id"
        ).fetchall()
        assert rows == [
            (1, "2026-06-24 10:01:00", "human-abandoned"),
            (2, "2026-06-24 10:02:00", None),
        ]


def test_self_heal_leaves_open_when_message_stored_before_failure(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as con:
        # Message created at 09:00:00
        con.execute(
            """
            INSERT INTO messages (
                subject, pst_source, folder_path, logical_locator,
                dedup_key, created_at
            ) VALUES (
                'Test', 'imap:test@example.com:INBOX', 'INBOX', 'loc1',
                'd1', '2026-06-24 09:00:00'
            )
            """
        )
        # Failure recorded later at 10:00:00
        con.execute(
            """
            INSERT INTO ingest_failures (
                pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at
            ) VALUES (
                'imap:test@example.com:INBOX', 'INBOX', 'loc1',
                'persist-message', 'err', 'msg', '2026-06-24 10:00:00'
            )
            """
        )
        con.commit()

        closed = close_self_healed_failures(con)
        assert closed == 0

        row = con.execute("SELECT resolved_at FROM ingest_failures").fetchone()
        assert row[0] is None


def test_self_heal_folder_path_mismatch_leaves_open(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO ingest_failures (
                pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at
            ) VALUES (
                'imap:test@example.com:INBOX', 'INBOX', 'loc1',
                'persist-message', 'err', 'msg', '2026-06-24 10:00:00'
            )
            """
        )
        # Same source and locator, but folder_path is different
        con.execute(
            """
            INSERT INTO messages (
                subject, pst_source, folder_path, logical_locator,
                dedup_key, created_at
            ) VALUES (
                'Test', 'imap:test@example.com:INBOX', 'Archive', 'loc1',
                'd1', '2026-06-24 10:05:00'
            )
            """
        )
        con.commit()

        closed = close_self_healed_failures(con)
        assert closed == 0

        row = con.execute("SELECT resolved_at FROM ingest_failures").fetchone()
        assert row[0] is None


def test_self_heal_idempotent_on_repeated_runs(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO ingest_failures (
                pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at
            ) VALUES (
                'imap:test@example.com:INBOX', 'INBOX', 'loc1',
                'persist-message', 'err', 'msg', '2026-06-24 10:00:00'
            )
            """
        )
        con.execute(
            """
            INSERT INTO messages (
                subject, pst_source, folder_path, logical_locator,
                dedup_key, created_at
            ) VALUES (
                'Test', 'imap:test@example.com:INBOX', 'INBOX', 'loc1',
                'd1', '2026-06-24 10:05:00'
            )
            """
        )
        con.commit()

        closed_1 = close_self_healed_failures(con)
        assert closed_1 == 1

        first_resolved_at = con.execute(
            "SELECT resolved_at FROM ingest_failures"
        ).fetchone()[0]
        assert first_resolved_at is not None

        closed_2 = close_self_healed_failures(con)
        assert closed_2 == 0

        second_resolved_at = con.execute(
            "SELECT resolved_at FROM ingest_failures"
        ).fetchone()[0]
        assert second_resolved_at == first_resolved_at


def test_sync_invokes_sweep_and_heals_open_failures(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"
    list_data = [b'(\\HasNoChildren) "/" INBOX']
    fetch_map = {101: _make_eml(101)}

    mock_conn = _MockImapConn(
        list_data=list_data,
        status_map={"INBOX": 1},
        search_map={"INBOX": [101]},
        fetch_map=fetch_map,
    )

    # Pre-seed an open ingest failure for UID 101 from a past failed run
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO ingest_failures (
                pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at
            ) VALUES (
                'imap:user@example.com:INBOX', 'INBOX',
                'INBOX/new/uid-101.eml', 'persist-message',
                'OperationalError', 'table messages has no column raw_headers',
                '2026-06-24 10:00:00'
            )
            """
        )
        con.commit()

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=mock_conn),
    )

    assert result.exit_code == 0
    assert result.inserted_messages == 1

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT resolved_at, abandon_reason FROM ingest_failures WHERE id = 1"
        ).fetchone()
        assert row is not None
        assert row[0] is not None
        assert row[1] is None


def test_sync_sweep_error_does_not_change_watermark_or_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"
    list_data = [b'(\\HasNoChildren) "/" INBOX']
    fetch_map = {101: _make_eml(101)}

    mock_conn = _MockImapConn(
        list_data=list_data,
        status_map={"INBOX": 1},
        search_map={"INBOX": [101]},
        fetch_map=fetch_map,
    )

    calls: list[int] = []

    def _exploding_sweep(connection: sqlite3.Connection) -> int:
        calls.append(1)
        raise sqlite3.OperationalError("simulated database disk I/O error during sweep")

    # Patch where sync_native looks the name up, not where it is defined.
    monkeypatch.setattr(imap_native_mod, "close_self_healed_failures", _exploding_sweep)

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=mock_conn),
    )

    assert calls == [1]
    assert result.exit_code == 0
    assert result.inserted_messages == 1
    assert ImapSyncState(state_path).get_watermark("INBOX", "/", 1) == 101


def test_sync_sweep_never_closes_unpersisted_message_on_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"
    list_data = [b'(\\HasNoChildren) "/" INBOX']
    fetch_map = {101: _make_eml(101)}

    mock_conn = _MockImapConn(
        list_data=list_data,
        status_map={"INBOX": 1},
        search_map={"INBOX": [101]},
        fetch_map=fetch_map,
    )

    import mailattic.ingest as ingest_mod

    def _failing_persist(connection, normalized_message):
        raise RuntimeError("simulated persist failure")

    monkeypatch.setattr(ingest_mod, "persist_message", _failing_persist)

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=mock_conn),
    )

    assert result.exit_code == 1
    with sqlite3.connect(db_path) as con:
        failure_rows = con.execute(
            "SELECT error_stage, resolved_at FROM ingest_failures"
        ).fetchall()
        assert len(failure_rows) >= 1
        for _stage, resolved_at in failure_rows:
            assert resolved_at is None


def test_maildir_sources_invokes_sweep_and_heals_open_failures(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO ingest_failures (
                pst_source, folder_path, logical_locator, error_stage,
                error_type, error_message, created_at
            ) VALUES (
                'imap:user@example.com:INBOX', 'INBOX',
                'INBOX/new/uid-101.eml', 'persist-message',
                'OperationalError', 'err', '2026-06-24 10:00:00'
            )
            """
        )
        con.execute(
            """
            INSERT INTO messages (
                subject, sender_name, sender_email, pst_source, folder_path,
                logical_locator, dedup_key, created_at
            ) VALUES (
                'Test', 'Sender', 'sender@example.com',
                'imap:user@example.com:INBOX', 'INBOX',
                'INBOX/new/uid-101.eml', 'msg-101', '2026-06-24 10:05:00'
            )
            """
        )
        con.commit()

    import_maildir_sources(database_path=db_path, sources=[])

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT resolved_at, abandon_reason FROM ingest_failures WHERE id = 1"
        ).fetchone()
        assert row is not None
        assert row[0] is not None
        assert row[1] is None

"""Tests for the sync-state table: durable database record of the last sync run.

Covers task group 1 of openspec/changes/add-mcp-sync-status: storage-layer
persistence (record_sync_run_start / record_sync_run_end) and the wiring of
those writes onto the native sync path in imap_native.sync_native.

Task 1.3's interrupted-run test is the TDD red-green anchor for placement:
it was manually verified this session to turn RED when the start write is
relocated to after folder resolution (see PC ledger / report), then restored
to its correct position before folder resolution.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mailattic.imap_native import sync_native
from mailattic.imap_state import ImapSyncState
from mailattic.storage import (
    SYNC_STATE_KEY,
    connect_database,
    initialize_database,
    record_sync_run_end,
    record_sync_run_start,
)

# The IMAP fake and message fixtures are shared with the existing native-sync
# suite rather than re-typed here: two independent fakes of the same client
# surface drift silently, and only one of them would still mirror the real
# NativeImapClient. Same reuse pattern as tests/test_imap_sync_ingest_accounting.py.
from tests.test_imap_native import _TEST_ENV, _FakeNativeClient, _make_eml


def _row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM sync_state WHERE sync_key = ?", [SYNC_STATE_KEY]
    ).fetchone()


# ---------------------------------------------------------------------------
# 1.1 / 1.2: storage-layer write semantics
# ---------------------------------------------------------------------------


def test_completed_run_records_both_timestamps_and_completed_outcome(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    connection = connect_database(db_path)
    try:
        started = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)
        ended = datetime(2026, 8, 4, 10, 5, tzinfo=UTC)
        record_sync_run_start(connection, started_at=started)
        record_sync_run_end(connection, ended_at=ended, outcome="completed")

        row = _row(connection)
        assert row is not None
        assert row["started_at"] == started.isoformat()
        assert row["ended_at"] == ended.isoformat()
        assert row["outcome"] == "completed"
    finally:
        connection.close()


def test_run_with_folder_failure_records_failed_outcome(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    connection = connect_database(db_path)
    try:
        started = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)
        ended = datetime(2026, 8, 4, 10, 5, tzinfo=UTC)
        record_sync_run_start(connection, started_at=started)
        record_sync_run_end(connection, ended_at=ended, outcome="failed")

        row = _row(connection)
        assert row is not None
        assert row["outcome"] == "failed"
        assert row["ended_at"] == ended.isoformat()
    finally:
        connection.close()


def test_second_run_overwrites_first_row_not_a_history_table(tmp_path: Path) -> None:
    """Pins design.md decision: "Persist one row of sync state, not a run
    history table" -- a second start must not accumulate a second row."""
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    connection = connect_database(db_path)
    try:
        first_started = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
        first_ended = datetime(2026, 8, 4, 9, 5, tzinfo=UTC)
        record_sync_run_start(connection, started_at=first_started)
        record_sync_run_end(connection, ended_at=first_ended, outcome="completed")

        second_started = datetime(2026, 8, 4, 11, 0, tzinfo=UTC)
        record_sync_run_start(connection, started_at=second_started)

        count_row = connection.execute(
            "SELECT COUNT(*) AS n FROM sync_state"
        ).fetchone()
        assert count_row["n"] == 1

        row = _row(connection)
        assert row is not None
        assert row["started_at"] == second_started.isoformat()
        assert row["ended_at"] is None
        assert row["outcome"] == "running"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 1.3: start write happens before folder resolution
# ---------------------------------------------------------------------------


def test_folder_resolution_failure_leaves_running_outcome_with_no_end_timestamp(
    tmp_path: Path,
) -> None:
    """A run interrupted before its end point remains observable.

    Folder resolution raises before the per-folder loop begins, so
    sync_native's own end point is never reached -- the record must still
    show the run started, with no end timestamp and the running outcome.
    """
    db_path = tmp_path / "test.db"
    fake_client = _FakeNativeClient(list_folders_raises=RuntimeError("LIST failed"))

    with pytest.raises(RuntimeError, match="LIST failed"):
        sync_native(
            env=_TEST_ENV,
            database_path=db_path,
            maildir_root=tmp_path / "maildir",
            state_path=tmp_path / ".imap_watermarks.json",
            log_dir=tmp_path / "logs",
            client_factory=lambda env: fake_client,
        )

    connection = connect_database(db_path)
    try:
        row = _row(connection)
        assert row is not None
        assert row["ended_at"] is None
        assert row["outcome"] == "running"
    finally:
        connection.close()


def test_client_construction_failure_is_recorded_as_a_failed_run(
    tmp_path: Path,
) -> None:
    """#318: an unreachable IMAP server is a recorded failed run (like #223's
    adoption failure), not an exception that escapes with sync_state 'running'.

    The start record is still written before the client is constructed, so
    the row exists to be stamped 'failed'; one log line carries stage+message.
    """
    db_path = tmp_path / "test.db"
    log_dir = tmp_path / "logs"

    def _failing_factory(env: object) -> object:
        raise OSError("connection refused")

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=log_dir,
        client_factory=_failing_factory,
    )

    assert result.exit_code == 1
    assert result.failure_stage == "connect"
    assert result.failure_message == "OSError: connection refused"
    connection = connect_database(db_path)
    try:
        row = _row(connection)
        assert row is not None
        assert row["ended_at"] is not None
        assert row["outcome"] == "failed"
    finally:
        connection.close()
    lines = [
        line
        for log in log_dir.glob("mbsync-*.log")
        for line in log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(lines) == 1
    assert "exit=1" in lines[0]
    assert "stage=connect" in lines[0]
    assert "message=OSError: connection refused" in lines[0]


def test_client_construction_programming_error_still_escapes(tmp_path: Path) -> None:
    """Only connection-class failures are recorded; a bug must stay loud."""

    def _buggy_factory(env: object) -> object:
        raise TypeError("bug")

    with pytest.raises(TypeError, match="bug"):
        sync_native(
            env=_TEST_ENV,
            database_path=tmp_path / "test.db",
            maildir_root=tmp_path / "maildir",
            state_path=tmp_path / ".imap_watermarks.json",
            log_dir=tmp_path / "logs",
            client_factory=_buggy_factory,
        )


# ---------------------------------------------------------------------------
# 1.4: schema is ensured at the sync entry point
# ---------------------------------------------------------------------------


def test_sync_entry_point_creates_sync_state_table_on_uninitialized_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "never_initialized.db"
    assert not db_path.exists()

    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 1},
        search_map={"INBOX": [1]},
        fetch_map={1: _make_eml(1)},
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        client_factory=lambda env: fake_client,
    )

    assert result.exit_code == 0
    connection = connect_database(db_path)
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "sync_state" in tables
        row = _row(connection)
        assert row is not None
        assert row["outcome"] == "completed"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 1.5: recording failures must not alter the sync outcome
# ---------------------------------------------------------------------------


def test_recording_failure_does_not_alter_sync_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    state_path = tmp_path / ".imap_watermarks.json"

    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 1},
        search_map={"INBOX": [1, 2]},
        fetch_map={1: _make_eml(1), 2: _make_eml(2)},
    )

    import mailattic.imap_native as _mod

    def _raising_end(connection, *, ended_at, outcome):
        raise RuntimeError("simulated recording failure")

    monkeypatch.setattr(_mod, "_storage_record_sync_run_end", _raising_end)
    caplog.set_level(logging.WARNING, logger=_mod.__name__)

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: fake_client,
    )

    assert result.exit_code == 0
    assert result.pulled_messages == 2
    assert result.inserted_messages == 2

    watermark_state = ImapSyncState(state_path)
    assert watermark_state.get_watermark("INBOX", "/", 1) == 2

    assert "sync-state end record failed" in caplog.text
    assert "simulated recording failure" in caplog.text


def test_failed_start_write_does_not_stamp_the_previous_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A run that could not open its own row must not stamp anyone else's.

    The end write is a bare UPDATE. Running it after a failed start would
    pair the *previous* run's start timestamp with this run's end and
    outcome -- a record of a run that never happened, which is worse than no
    record at all. Removing the `if sync_state_opened:` guard turns this red.
    """
    db_path = tmp_path / "test.db"
    initialize_database(db_path)

    previous_start = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
    previous_end = datetime(2026, 8, 3, 1, 30, tzinfo=UTC)
    connection = connect_database(db_path)
    try:
        record_sync_run_start(connection, started_at=previous_start)
        record_sync_run_end(connection, ended_at=previous_end, outcome="failed")
    finally:
        connection.close()

    import mailattic.imap_native as _mod

    def _raising_start(connection, *, started_at):
        raise RuntimeError("simulated start-write failure")

    monkeypatch.setattr(_mod, "_storage_record_sync_run_start", _raising_start)
    caplog.set_level(logging.WARNING, logger=_mod.__name__)

    sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        client_factory=lambda env: _FakeNativeClient(folders=[("INBOX", "/")]),
    )

    connection = connect_database(db_path)
    try:
        row = _row(connection)
        assert row is not None
        assert row["started_at"] == previous_start.isoformat()
        assert row["ended_at"] == previous_end.isoformat()
        assert row["outcome"] == "failed"
    finally:
        connection.close()

    assert "sync-state start record failed" in caplog.text


def test_schema_ensure_failure_is_labelled_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A schema-ensure failure must not be reported as a sync-state failure.

    It is a whole-database condition; labelling it as the latter sends the
    next person debugging it to the wrong place. Collapsing the two handlers
    back into one turns this red.
    """
    db_path = tmp_path / "test.db"
    fake_client = _FakeNativeClient(folders=[("INBOX", "/")])

    import mailattic.imap_native as _mod

    def _raising_initialize(database_path: Path) -> None:
        raise RuntimeError("simulated schema failure")

    monkeypatch.setattr(_mod, "initialize_database", _raising_initialize)
    caplog.set_level(logging.WARNING, logger=_mod.__name__)

    sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        client_factory=lambda env: fake_client,
    )

    assert "sync-state schema ensure failed" in caplog.text
    assert "simulated schema failure" in caplog.text
    assert "sync-state start record failed" not in caplog.text


# ---------------------------------------------------------------------------
# 1.6: zero-new-message run is still distinguishable from a stopped sync
# ---------------------------------------------------------------------------


def test_zero_new_messages_still_records_completed_outcome(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)

    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 1},
        search_map={"INBOX": []},
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        client_factory=lambda env: fake_client,
    )

    assert result.pulled_messages == 0
    assert result.exit_code == 0

    connection = connect_database(db_path)
    try:
        row = _row(connection)
        assert row is not None
        assert row["outcome"] == "completed"
        assert row["ended_at"] is not None
    finally:
        connection.close()

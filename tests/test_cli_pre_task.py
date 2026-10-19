"""Tests for sync-imap --pre-task 15-minute gate (Task 5.1)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from mailattic.cli import _handle_sync_imap, build_parser
from mailattic.imap_runtime import SyncExecutionResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _args(extra: list[str] | None = None) -> object:
    """Build a parsed Namespace for sync-imap with a temp database."""
    base = ["sync-imap", "--database", "mydb.db"]
    return build_parser().parse_args(base + (extra or []))


def _synced_outcome(pulled: int = 3) -> dict[str, object]:
    """Return a minimal 'synced' outcome dict as produced by pre_task_sync_imap."""
    return {
        "status": "synced",
        "last_sync_at": datetime.now(tz=UTC).isoformat(),
        "pulled_messages": pulled,
        "folder_count": 2,
        "candidate_messages": pulled,
        "inserted_messages": pulled,
        "deduplicated_messages": 0,
        "failure_count": 0,
        "exit_code": 0,
    }


def _skipped_outcome(last_sync: datetime, elapsed: float) -> dict[str, object]:
    """Return a 'skipped' outcome dict as produced by pre_task_sync_imap."""
    return {
        "status": "skipped",
        "last_sync_at": last_sync.isoformat(),
        "elapsed_seconds": elapsed,
    }


# ---------------------------------------------------------------------------
# 5.1-A: last sync < 15 min ago → SKIP, sync NOT called
# ---------------------------------------------------------------------------


def test_pre_task_skips_when_last_sync_recent(monkeypatch) -> None:
    """With --pre-task, if last sync was 4 minutes ago the gate skips."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)

    last_sync = datetime.now(tz=UTC) - timedelta(minutes=4)
    mock_pre_task = MagicMock(return_value=_skipped_outcome(last_sync, 240.0))
    monkeypatch.setattr("mailattic.cli.pre_task_sync_imap", mock_pre_task)

    args = _args(["--pre-task"])
    exit_code = _handle_sync_imap(args)

    assert exit_code == 0
    # pre_task_sync_imap must have been called (the gate is inside it)
    mock_pre_task.assert_called_once()


def test_pre_task_skips_does_not_call_run_sync_imap(monkeypatch) -> None:
    """When the gate returns 'skipped', run_sync_imap must never be invoked."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)

    last_sync = datetime.now(tz=UTC) - timedelta(minutes=4)
    monkeypatch.setattr(
        "mailattic.cli.pre_task_sync_imap",
        MagicMock(return_value=_skipped_outcome(last_sync, 240.0)),
    )
    mock_run = MagicMock()
    monkeypatch.setattr("mailattic.cli.run_sync_imap", mock_run)

    args = _args(["--pre-task"])
    _handle_sync_imap(args)

    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 5.1-B: last sync > 15 min ago → sync IS called
# ---------------------------------------------------------------------------


def test_pre_task_runs_sync_when_last_sync_stale(monkeypatch) -> None:
    """With --pre-task, if last sync was 20 minutes ago the gate runs sync."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)

    mock_pre_task = MagicMock(return_value=_synced_outcome(pulled=5))
    monkeypatch.setattr("mailattic.cli.pre_task_sync_imap", mock_pre_task)

    args = _args(["--pre-task"])
    exit_code = _handle_sync_imap(args)

    assert exit_code == 0
    mock_pre_task.assert_called_once()


def test_pre_task_gate_uses_injectable_seam_directly(monkeypatch) -> None:
    """pre_task_sync_imap accepts now/last_sync injection — verify the seam works.

    Uses the now/last_sync parameters to control gate behaviour without touching
    the wall clock or real log files.  run_sync_imap is monkeypatched to avoid
    needing a real docker / .env config.
    """
    from mailattic.imap_sync import pre_task_sync_imap

    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
    last_sync_recent = now - timedelta(minutes=4)
    last_sync_stale = now - timedelta(minutes=20)

    mock_run = MagicMock(
        return_value=SyncExecutionResult(
            pulled_messages=0,
            folder_count=0,
            candidate_messages=0,
            inserted_messages=0,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=0,
        )
    )
    monkeypatch.setattr("mailattic.imap_sync.run_sync_imap", mock_run)

    # Recent last_sync → skipped (run_sync_imap must NOT be called)
    outcome_skip = pre_task_sync_imap(
        now=now,
        last_sync=last_sync_recent,
    )
    assert outcome_skip["status"] == "skipped"
    mock_run.assert_not_called()

    # Stale last_sync → synced (run_sync_imap IS called once)
    outcome_sync = pre_task_sync_imap(
        now=now,
        last_sync=last_sync_stale,
    )
    assert outcome_sync["status"] == "synced"
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# 5.1-C: normal sync-imap (no --pre-task) is NOT gated
# ---------------------------------------------------------------------------


def test_pre_task_partial_success_prints_partial_not_skipped(
    monkeypatch, capsys
) -> None:
    """Regression: --pre-task partial failure prints PARTIAL, not SKIPPED."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)

    outcome = {
        "status": "synced",
        "last_sync_at": None,
        "pulled_messages": 5,
        "folder_count": 3,
        "candidate_messages": 5,
        "inserted_messages": 3,
        "deduplicated_messages": 2,
        "failure_count": 1,
        "exit_code": 1,
        "failure_stage": "folder",
        "failure_message": "folder INBOX.Sent failed",
    }
    monkeypatch.setattr(
        "mailattic.cli.pre_task_sync_imap",
        MagicMock(return_value=outcome),
    )

    args = _args(["--pre-task"])
    exit_code = _handle_sync_imap(args)

    assert exit_code == 1
    captured = capsys.readouterr().out
    assert "PARTIAL" in captured
    assert "SKIPPED" not in captured
    assert "3 inserted" in captured
    assert "2 deduped" in captured
    assert "1 folders failed" in captured


def test_normal_sync_imap_bypasses_pre_task_gate(monkeypatch) -> None:
    """Without --pre-task, run_sync_imap is called directly — gate is irrelevant."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)

    mock_pre_task = MagicMock()
    monkeypatch.setattr("mailattic.cli.pre_task_sync_imap", mock_pre_task)

    mock_run = MagicMock(
        return_value=SyncExecutionResult(
            pulled_messages=2,
            folder_count=1,
            candidate_messages=2,
            inserted_messages=2,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=0,
        )
    )
    monkeypatch.setattr("mailattic.cli.run_sync_imap", mock_run)

    args = _args()  # no --pre-task
    exit_code = _handle_sync_imap(args)

    assert exit_code == 0
    mock_pre_task.assert_not_called()
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Issue #305: skipped output indicates whether last sync failed
# ---------------------------------------------------------------------------


def test_pre_task_skipped_after_success_output_byte_for_byte_unchanged(
    monkeypatch, capsys
) -> None:
    """When last sync succeeded, skipped output (text and JSON) is unchanged."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    now = datetime(2026, 10, 14, 15, 10, 0, tzinfo=UTC)
    last_sync = now - timedelta(minutes=4)  # elapsed = 240s

    # Baseline text and JSON:
    baseline_text = (
        f"sync-imap: skipped (last sync: {last_sync.isoformat()}; "
        "elapsed: 240s < 900s interval)\n"
        "exit code: 0\n"
    )
    baseline_json = {
        "status": "skipped",
        "last_sync_at": last_sync.isoformat(),
        "elapsed_seconds": 240.0,
        # fixed shape: failure keys are always present, null when not applicable
        "last_sync_failed": False,
        "last_sync_exit_code": 0,
        "last_sync_failure_stage": None,
        "last_sync_failure_message": None,
    }

    from mailattic.imap_sync import pre_task_sync_imap

    outcome = pre_task_sync_imap(
        now=now,
        last_sync=last_sync,
        last_sync_exit_code=0,
    )
    assert outcome == baseline_json

    mock_pre_task = MagicMock(return_value=outcome)
    monkeypatch.setattr("mailattic.cli.pre_task_sync_imap", mock_pre_task)

    args = _args(["--pre-task"])
    exit_code = _handle_sync_imap(args)

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert captured == baseline_text


def test_pre_task_skipped_after_failure_output_indicates_failure(
    monkeypatch, capsys
) -> None:
    """When last sync failed, skipped output (text and JSON) indicates failure."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    now = datetime(2026, 10, 14, 15, 10, 0, tzinfo=UTC)
    last_sync = now - timedelta(minutes=4)  # elapsed = 240s

    from mailattic.imap_sync import pre_task_sync_imap

    outcome = pre_task_sync_imap(
        now=now,
        last_sync=last_sync,
        last_sync_exit_code=1,
        last_sync_failure_stage="sync",
        last_sync_failure_message="UID 3 did not persist",
    )
    assert outcome["status"] == "skipped"
    assert outcome["last_sync_failed"] is True
    assert outcome["last_sync_exit_code"] == 1
    assert outcome["last_sync_failure_stage"] == "sync"
    assert outcome["last_sync_failure_message"] == "UID 3 did not persist"

    mock_pre_task = MagicMock(return_value=outcome)
    monkeypatch.setattr("mailattic.cli.pre_task_sync_imap", mock_pre_task)

    args = _args(["--pre-task"])
    exit_code = _handle_sync_imap(args)

    assert exit_code == 0
    captured = capsys.readouterr().out

    expected_text = (
        f"sync-imap: skipped (last sync: {last_sync.isoformat()}; "
        "elapsed: 240s < 900s interval; last sync failed)\n"
        "exit code: 0\n"
    )
    assert captured == expected_text
    assert "last sync failed" in captured


def test_pre_task_reads_failed_sync_log_from_log_dir(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """End-to-end: failed sync record in log_dir produces failure indication."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)

    log_dir = tmp_path / "logs"
    now = datetime(2026, 10, 14, 15, 10, 0, tzinfo=UTC)
    last_end = now - timedelta(minutes=3)
    last_start = last_end - timedelta(seconds=10)

    from mailattic.imap_runtime import SyncExecutionResult, _append_sync_log
    from mailattic.imap_sync import pre_task_sync_imap, read_last_sync_record

    _append_sync_log(
        log_dir=log_dir,
        start_at=last_start,
        end_at=last_end,
        result=SyncExecutionResult(
            pulled_messages=0,
            folder_count=1,
            candidate_messages=0,
            inserted_messages=0,
            deduplicated_messages=0,
            failure_count=1,
            exit_code=1,
            failure_stage="sync",
            failure_message="UID 3 did not persist",
        ),
    )

    rec = read_last_sync_record(log_dir)
    assert rec is not None
    assert rec.end_at == last_end
    assert rec.exit_code == 1
    assert rec.failed is True
    assert rec.failure_stage == "sync"
    assert rec.failure_message == "UID 3 did not persist"

    outcome = pre_task_sync_imap(
        log_dir=log_dir,
        now=now,
    )
    assert outcome["status"] == "skipped"
    assert outcome["last_sync_failed"] is True
    assert outcome["last_sync_exit_code"] == 1
    assert outcome["last_sync_failure_stage"] == "sync"
    assert outcome["last_sync_failure_message"] == "UID 3 did not persist"

    args = build_parser().parse_args(
        [
            "sync-imap",
            "--database",
            str(tmp_path / "test.db"),
            "--log-dir",
            str(log_dir),
            "--pre-task",
        ]
    )
    monkeypatch.setattr(
        "mailattic.cli.pre_task_sync_imap",
        lambda **kw: pre_task_sync_imap(now=now, **kw),
    )

    exit_code = _handle_sync_imap(args)
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "last sync failed" in captured


def test_pre_task_cli_json_flag_emits_json_skipped_after_success(
    monkeypatch, capsys
) -> None:
    """CLI surface with --pre-task --json emits valid JSON when skipped on success."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    now = datetime(2026, 10, 14, 15, 10, 0, tzinfo=UTC)
    last_sync = now - timedelta(minutes=4)

    baseline_json = {
        "status": "skipped",
        "last_sync_at": last_sync.isoformat(),
        "elapsed_seconds": 240.0,
        # fixed shape: failure keys are always present, null when not applicable
        "last_sync_failed": False,
        "last_sync_exit_code": 0,
        "last_sync_failure_stage": None,
        "last_sync_failure_message": None,
    }

    from mailattic.imap_sync import pre_task_sync_imap

    outcome = pre_task_sync_imap(
        now=now,
        last_sync=last_sync,
        last_sync_exit_code=0,
    )
    assert outcome == baseline_json

    mock_pre_task = MagicMock(return_value=outcome)
    monkeypatch.setattr("mailattic.cli.pre_task_sync_imap", mock_pre_task)

    args = build_parser().parse_args(
        ["sync-imap", "--database", "mydb.db", "--pre-task", "--json"]
    )
    exit_code = _handle_sync_imap(args)

    assert exit_code == 0
    captured = capsys.readouterr().out
    parsed = json.loads(captured)
    assert parsed == baseline_json
    assert parsed["last_sync_failed"] is False
    assert captured == json.dumps(baseline_json, ensure_ascii=False, indent=2) + "\n"


def test_pre_task_cli_json_flag_emits_json_skipped_after_failure(
    monkeypatch, capsys
) -> None:
    """CLI surface with --pre-task --json emits failure info when skipped on failure."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    now = datetime(2026, 10, 14, 15, 10, 0, tzinfo=UTC)
    last_sync = now - timedelta(minutes=4)

    from mailattic.imap_sync import pre_task_sync_imap

    outcome = pre_task_sync_imap(
        now=now,
        last_sync=last_sync,
        last_sync_exit_code=1,
        last_sync_failure_stage="sync",
        last_sync_failure_message="UID 3 did not persist",
    )

    mock_pre_task = MagicMock(return_value=outcome)
    monkeypatch.setattr("mailattic.cli.pre_task_sync_imap", mock_pre_task)

    args = build_parser().parse_args(
        ["sync-imap", "--database", "mydb.db", "--pre-task", "--json"]
    )
    exit_code = _handle_sync_imap(args)

    assert exit_code == 0
    captured = capsys.readouterr().out
    parsed = json.loads(captured)
    assert parsed["status"] == "skipped"
    assert parsed["last_sync_failed"] is True
    assert parsed["last_sync_exit_code"] == 1
    assert parsed["last_sync_failure_stage"] == "sync"
    assert parsed["last_sync_failure_message"] == "UID 3 did not persist"


def test_read_last_sync_record_same_second_takes_latest_line(tmp_path: Path) -> None:
    """When runs finish within the same second, the later log line is chosen."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "mbsync-2026-10-14.log"
    # First line: exit 0
    line1 = (
        "start=2026-10-14T15:00:00+00:00 end=2026-10-14T15:00:01+00:00 "
        "new=0 deduped=0 exit=0\n"
    )
    # Second line with identical end timestamp: exit 1
    line2 = (
        "start=2026-10-14T15:00:00+00:00 end=2026-10-14T15:00:01+00:00 "
        "new=0 deduped=0 exit=1 stage=sync message=failed\n"
    )
    log_file.write_text(line1 + line2, encoding="utf-8")

    from mailattic.imap_sync import read_last_sync_record

    rec = read_last_sync_record(log_dir)
    assert rec is not None
    assert rec.exit_code == 1
    assert rec.failed is True
    assert rec.failure_stage == "sync"


def test_pre_task_direct_last_sync_record_injection() -> None:
    """pre_task_sync_imap accepts LastSyncRecord directly without unpacking smell."""
    from mailattic.imap_sync import LastSyncRecord, pre_task_sync_imap

    now = datetime(2026, 10, 14, 15, 10, 0, tzinfo=UTC)
    record = LastSyncRecord(
        end_at=now - timedelta(minutes=4),
        exit_code=1,
        failure_stage="folder",
        failure_message="boom",
    )
    outcome = pre_task_sync_imap(
        now=now,
        last_sync=record,
    )
    assert outcome["status"] == "skipped"
    assert outcome["last_sync_failed"] is True
    assert outcome["last_sync_exit_code"] == 1
    assert outcome["last_sync_failure_stage"] == "folder"
    assert outcome["last_sync_failure_message"] == "boom"


def test_pre_task_first_sync_with_no_log_runs_and_reports(
    tmp_path: Path, monkeypatch
) -> None:
    """No sync log yet (first --pre-task ever): sync runs and returns a result."""
    from mailattic.imap_sync import pre_task_sync_imap

    mock_run = MagicMock(
        return_value=SyncExecutionResult(
            pulled_messages=0,
            folder_count=0,
            candidate_messages=0,
            inserted_messages=0,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=0,
        )
    )
    monkeypatch.setattr("mailattic.imap_sync.run_sync_imap", mock_run)

    outcome = pre_task_sync_imap(log_dir=tmp_path / "no-logs-yet")

    assert outcome["status"] == "synced"
    assert outcome["last_sync_at"] is None
    mock_run.assert_called_once()


def _log_success(log_dir: Path, end_at: datetime) -> None:
    from mailattic.imap_runtime import _append_sync_log

    _append_sync_log(
        log_dir=log_dir,
        start_at=end_at - timedelta(seconds=10),
        end_at=end_at,
        result=SyncExecutionResult(
            pulled_messages=0,
            folder_count=1,
            candidate_messages=0,
            inserted_messages=0,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=0,
        ),
    )


def test_pre_task_flags_run_that_died_before_logging(tmp_path: Path) -> None:
    """A run that opened sync_state after the newest log line but never
    completed (e.g. connection failed before the log was written) must not be
    reported as a clean skip."""
    from mailattic.imap_sync import pre_task_sync_imap
    from mailattic.storage import initialize_database, record_sync_run_start

    now = datetime(2026, 10, 14, 15, 10, 0, tzinfo=UTC)
    log_dir = tmp_path / "logs"
    _log_success(log_dir, now - timedelta(minutes=5))
    db = initialize_database(tmp_path / "test.db")
    connection = sqlite3.connect(db)
    try:
        record_sync_run_start(connection, started_at=now - timedelta(minutes=3))
    finally:
        connection.close()

    outcome = pre_task_sync_imap(database_path=db, log_dir=log_dir, now=now)

    assert outcome["status"] == "skipped"
    assert outcome["last_sync_failed"] is True
    assert outcome["last_sync_exit_code"] is None
    assert outcome["last_sync_failure_stage"] == "unfinished"


def test_unfinished_skip_text_says_did_not_complete(monkeypatch, capsys) -> None:
    """A run that may still be running must not be called 'failed'."""
    outcome = {
        "status": "skipped",
        "last_sync_at": "2026-10-14T15:05:00+00:00",
        "elapsed_seconds": 300.0,
        "last_sync_failed": True,
        "last_sync_exit_code": None,
        "last_sync_failure_stage": "unfinished",
        "last_sync_failure_message": "sync started at ... has no recorded completion",
    }
    monkeypatch.setattr(
        "mailattic.cli.pre_task_sync_imap", MagicMock(return_value=outcome)
    )

    assert _handle_sync_imap(_args(["--pre-task"])) == 0

    out = capsys.readouterr().out
    assert "last sync did not complete" in out
    assert "last sync failed" not in out


def test_pre_task_unreadable_state_is_not_a_clean_skip(tmp_path: Path) -> None:
    """A DB that exists but cannot be read is reported, not passed off as fresh."""
    from mailattic.imap_sync import pre_task_sync_imap

    now = datetime(2026, 10, 14, 15, 10, 0, tzinfo=UTC)
    log_dir = tmp_path / "logs"
    _log_success(log_dir, now - timedelta(minutes=5))
    db = tmp_path / "broken.db"
    db.write_bytes(b"this is not an sqlite database")

    outcome = pre_task_sync_imap(database_path=db, log_dir=log_dir, now=now)

    assert outcome["status"] == "skipped"
    assert outcome["last_sync_failed"] is True
    assert outcome["last_sync_failure_stage"] == "state_unreadable"


def test_negative_exit_in_log_counts_as_failed(tmp_path: Path) -> None:
    """A signal-killed subprocess is logged with a negative returncode."""
    from mailattic.imap_sync import read_last_sync_record

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "mbsync-20261014.log").write_text(
        "start=2026-10-14T15:00:00+00:00 end=2026-10-14T15:01:00+00:00 "
        "new=0 deduped=0 exit=-9\n",
        encoding="utf-8",
    )

    rec = read_last_sync_record(log_dir)

    assert rec is not None
    assert rec.exit_code == -9
    assert rec.failed is True


def test_pre_task_malformed_state_timestamp_is_unreadable(tmp_path: Path) -> None:
    """A garbage started_at must yield state_unreadable, not crash the command."""
    from mailattic.imap_sync import pre_task_sync_imap
    from mailattic.storage import initialize_database, record_sync_run_start

    now = datetime(2026, 10, 14, 15, 10, 0, tzinfo=UTC)
    log_dir = tmp_path / "logs"
    _log_success(log_dir, now - timedelta(minutes=5))
    db = initialize_database(tmp_path / "test.db")
    connection = sqlite3.connect(db)
    try:
        record_sync_run_start(connection, started_at=now - timedelta(minutes=3))
        connection.execute("UPDATE sync_state SET started_at = 'not-a-time'")
        connection.commit()
    finally:
        connection.close()

    outcome = pre_task_sync_imap(database_path=db, log_dir=log_dir, now=now)

    assert outcome["status"] == "skipped"
    assert outcome["last_sync_failed"] is True
    assert outcome["last_sync_failure_stage"] == "state_unreadable"


def test_pre_task_completed_state_before_log_stays_clean(tmp_path: Path) -> None:
    """sync_state that completed (or predates the newest log) is not a failure."""
    from mailattic.imap_sync import pre_task_sync_imap
    from mailattic.storage import (
        initialize_database,
        record_sync_run_end,
        record_sync_run_start,
    )

    now = datetime(2026, 10, 14, 15, 10, 0, tzinfo=UTC)
    log_dir = tmp_path / "logs"
    _log_success(log_dir, now - timedelta(minutes=5))
    db = initialize_database(tmp_path / "test.db")
    connection = sqlite3.connect(db)
    try:
        record_sync_run_start(connection, started_at=now - timedelta(minutes=6))
        record_sync_run_end(
            connection, ended_at=now - timedelta(minutes=5), outcome="completed"
        )
    finally:
        connection.close()

    outcome = pre_task_sync_imap(database_path=db, log_dir=log_dir, now=now)

    assert outcome["status"] == "skipped"
    assert outcome["last_sync_failed"] is False


def test_json_without_pre_task_is_rejected(monkeypatch, capsys) -> None:
    """--json only shapes --pre-task output; a plain sync must not accept it."""
    mock_run = MagicMock()
    monkeypatch.setattr("mailattic.cli.run_sync_imap", mock_run, raising=False)
    mock_pre_task = MagicMock()
    monkeypatch.setattr("mailattic.cli.pre_task_sync_imap", mock_pre_task)

    assert _handle_sync_imap(_args(["--json"])) == 2

    assert "--json is only supported with --pre-task" in capsys.readouterr().err
    mock_run.assert_not_called()
    mock_pre_task.assert_not_called()

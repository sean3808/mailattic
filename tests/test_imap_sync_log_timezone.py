from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from mailattic.imap_runtime import (
    SyncExecutionResult,
    _append_sync_log,
)


def _make_result() -> SyncExecutionResult:
    return SyncExecutionResult(
        pulled_messages=48,
        folder_count=10,
        candidate_messages=50,
        inserted_messages=48,
        deduplicated_messages=2,
        failure_count=0,
        exit_code=0,
    )


def test_log_filename_uses_taipei_date_when_utc_crosses_midnight(
    tmp_path: Path,
) -> None:
    start_at = datetime(2026, 5, 20, 16, 5, 10, 963061, tzinfo=UTC)
    end_at = datetime(2026, 5, 20, 16, 6, 39, 545245, tzinfo=UTC)

    _append_sync_log(
        log_dir=tmp_path,
        start_at=start_at,
        end_at=end_at,
        result=_make_result(),
    )

    expected = tmp_path / "mbsync-2026-05-21.log"
    forbidden = tmp_path / "mbsync-2026-05-20.log"
    contents = [p.name for p in tmp_path.iterdir()]
    assert expected.exists(), f"expected {expected.name}, got: {contents}"
    assert not forbidden.exists(), "UTC-date filename must not be created"


def test_log_entry_timestamps_keep_utc_offset(tmp_path: Path) -> None:
    start_at = datetime(2026, 5, 20, 16, 5, 10, 963061, tzinfo=UTC)
    end_at = datetime(2026, 5, 20, 16, 6, 39, 545245, tzinfo=UTC)

    _append_sync_log(
        log_dir=tmp_path,
        start_at=start_at,
        end_at=end_at,
        result=_make_result(),
    )

    log_text = (tmp_path / "mbsync-2026-05-21.log").read_text(encoding="utf-8")
    assert "start=2026-05-20T16:05:10.963061+00:00" in log_text
    assert "end=2026-05-20T16:06:39.545245+00:00" in log_text


def test_log_filename_same_taipei_date_when_within_utc_day(tmp_path: Path) -> None:
    start_at = datetime(2026, 5, 21, 4, 30, 0, tzinfo=UTC)
    end_at = datetime(2026, 5, 21, 4, 31, 0, tzinfo=UTC)

    _append_sync_log(
        log_dir=tmp_path,
        start_at=start_at,
        end_at=end_at,
        result=_make_result(),
    )

    assert (tmp_path / "mbsync-2026-05-21.log").exists()

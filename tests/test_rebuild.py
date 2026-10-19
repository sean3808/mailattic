from __future__ import annotations

import ast
import importlib
import json
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from mailattic.imap_sync import SyncExecutionResult
from mailattic.models import ImportSummary
from mailattic.outlook_pst import OutlookComReader
from mailattic.storage import initialize_database


def _load_rebuild_module():
    return importlib.import_module("mailattic.rebuild")


def _write_checkpoint(
    work_dir: Path,
    *,
    completed_phases: list[str] | None = None,
    imported_psts: list[str] | None = None,
    schema_version: int = 1,
    round_identity: dict[str, object] | None = None,
    staged_rows: dict[str, int] | None = None,
    staged_total: int | None = None,
) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = work_dir / "rebuild-checkpoint.json"
    payload: dict[str, object] = {
        "completed_phases": completed_phases or [],
        "imported_psts": imported_psts or [],
        "schema_version": schema_version,
    }
    if round_identity is not None:
        payload["round"] = round_identity
    if staged_rows is not None:
        payload["staged_rows"] = staged_rows
    if staged_total is not None:
        payload["staged_total"] = staged_total
    checkpoint_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return checkpoint_path


def _seed_searchable_stage_db(db_path: Path, source_tag: str) -> None:
    initialize_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO messages (
                subject,
                sender_name,
                sender_email,
                sent_at,
                received_at,
                body_text,
                message_id,
                in_reply_to,
                references_header,
                pst_source,
                folder_path,
                logical_locator,
                source_input_mode,
                source_requested_from,
                dedup_key,
                dedup_basis,
                recipient_search_text,
                attachment_search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                f"Subject {source_tag}",
                "sender@example.com",
                "sender@example.com",
                "2026-06-01T09:00:00",
                "2026-06-01T09:01:00",
                f"keyword {source_tag} body text",
                f"<{source_tag}@example.com>",
                None,
                None,
                f"{source_tag}.pst",
                "Inbox",
                f"Inbox/{source_tag}",
                "file",
                f"{source_tag}.pst",
                f"message-id:<{source_tag}@example.com>",
                '{"method":"message-id"}',
                "recipient@example.com",
                "",
            ],
        )
        message_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO message_fts (
                rowid,
                message_rowid,
                subject,
                sender,
                recipients,
                body_text,
                attachment_names
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                message_id,
                message_id,
                f"Subject {source_tag}",
                "sender@example.com",
                "recipient@example.com",
                f"keyword {source_tag} body text",
                "",
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _count_messages(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def _make_rebuild_context(
    rebuild,
    *,
    work_dir: Path,
    stage_db: Path,
    pst_paths: tuple[Path, ...] = (),
    resume: bool = False,
    resolve_work_dir: bool = True,
):
    prepared_work_dir = (
        work_dir.expanduser().resolve() if resolve_work_dir else work_dir.expanduser()
    )
    resolved_stage_db = stage_db.expanduser().resolve()
    return rebuild.RebuildContext(
        work_dir=prepared_work_dir,
        stage_db=resolved_stage_db,
        attachment_root=rebuild.resolve_store_root(resolved_stage_db),
        pst_paths=tuple(path.expanduser().resolve() for path in pst_paths),
        readpst_path=None,
        env_file=rebuild.DEFAULT_ENV_FILE,
        maildir_root=rebuild.DEFAULT_MAILDIR_ROOT,
        log_dir=rebuild.DEFAULT_LOG_DIR,
        resume=resume,
    )


def test_preflight_stops_on_mnt_work_dir_before_later_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rebuild = _load_rebuild_module()
    pst_path = tmp_path / "sample.pst"
    pst_path.write_bytes(b"pst")
    later_phase_calls: list[str] = []

    def should_not_run(*_args, **_kwargs):
        later_phase_calls.append("coverage-report")
        return None

    monkeypatch.setattr(rebuild, "run_coverage_report_phase", should_not_run)

    exit_code = rebuild.run_rebuild(
        work_dir=Path("/mnt/disallowed-rebuild"),
        pst_paths=[pst_path],
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "/mnt" in captured.err
    assert later_phase_calls == []


def test_checkpoint_written_after_phase_and_resume_revalidates_before_resuming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed *work* is skipped on resume; preflight is not work.

    It asserts a live Outlook still holds these PSTs, so banking that verdict
    in the checkpoint would let a resume run hours of coverage-report before
    discovering the operator had closed Outlook.
    """
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    pst_path = tmp_path / "sample.pst"
    pst_path.write_bytes(b"pst")

    monkeypatch.setattr(
        rebuild,
        "run_preflight_phase",
        lambda *_args, **_kwargs: None,
    )

    exit_code = rebuild.run_rebuild(
        phase="preflight",
        run_all=False,
        work_dir=work_dir,
        pst_paths=[pst_path],
    )

    assert exit_code == 0
    checkpoint = rebuild.load_checkpoint(work_dir)
    assert checkpoint["completed_phases"] == ["preflight"]

    ordered_calls: list[str] = []

    monkeypatch.setattr(
        rebuild,
        "run_preflight_phase",
        lambda *_args, **_kwargs: ordered_calls.append("preflight"),
    )
    monkeypatch.setattr(
        rebuild,
        "run_coverage_report_phase",
        lambda *_args, **_kwargs: ordered_calls.append("coverage-report"),
    )
    monkeypatch.setattr(
        rebuild,
        "run_import_phase",
        lambda *_args, **_kwargs: ordered_calls.append("import"),
    )
    monkeypatch.setattr(
        rebuild,
        "run_backfill_phase",
        lambda *_args, **_kwargs: ordered_calls.append("backfill"),
    )
    monkeypatch.setattr(
        rebuild,
        "run_verify_cas_phase",
        lambda *_args, **_kwargs: ordered_calls.append("verify-cas"),
    )
    monkeypatch.setattr(
        rebuild,
        "run_validate_phase",
        lambda *_args, **_kwargs: ordered_calls.append("validate"),
    )

    exit_code = rebuild.run_rebuild(
        resume=True,
        work_dir=work_dir,
        pst_paths=[pst_path],
    )

    assert exit_code == 0
    assert ordered_calls == [
        "preflight",
        "coverage-report",
        "import",
        "backfill",
        "verify-cas",
        "validate",
    ]


@pytest.mark.parametrize(
    ("coverage_until", "expected_gap_days"),
    [
        (date(2026, 5, 20), 0),
        (date(2026, 4, 15), 27),
    ],
)
def test_calculate_coverage_report_gap_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    coverage_until: date,
    expected_gap_days: int,
) -> None:
    rebuild = _load_rebuild_module()
    pst_path = tmp_path / "coverage.pst"
    pst_path.write_bytes(b"pst")

    monkeypatch.setattr(
        rebuild,
        "scan_pst_coverage_until",
        lambda **_kwargs: coverage_until,
    )

    report = rebuild.calculate_coverage_report(
        work_dir=tmp_path / "work",
        pst_paths=[pst_path],
        today=date(2026, 6, 11),
    )

    assert report["pst_coverage_until"] == coverage_until.isoformat()
    assert report["server_window_start"] == "2026-05-12"
    assert report["gap_days"] == expected_gap_days
    assert "還原 DB 備份" in report["primary_dr_hint"]


def test_import_resume_skips_already_imported_psts_without_double_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    pst_one = tmp_path / "archive-1.pst"
    pst_two = tmp_path / "archive-2.pst"
    pst_one.write_bytes(b"pst-one")
    pst_two.write_bytes(b"pst-two")

    _seed_searchable_stage_db(stage_db, str(pst_one.with_suffix("").resolve()))
    _write_checkpoint(
        work_dir,
        completed_phases=[],
        imported_psts=[str(pst_one.resolve())],
        round_identity={
            "stage_db": str(stage_db.resolve()),
            "pst_inputs": [str(pst_one.resolve()), str(pst_two.resolve())],
        },
        staged_rows={str(pst_one.resolve()): 1},
    )

    imported_source_paths: list[str] = []

    def fake_import_pst_sources(
        *,
        database_path,
        sources,
        backend=None,
        readpst_path=None,
        work_root=None,
        parallel_jobs=None,
        attachment_root=None,
    ) -> ImportSummary:
        del backend, readpst_path, work_root, parallel_jobs, attachment_root
        assert len(sources) == 1
        source = sources[0]
        imported_source_paths.append(source.source_path)
        _seed_searchable_stage_db(Path(database_path), Path(source.source_path).stem)
        return ImportSummary(
            discovered_psts=1,
            imported_messages=1,
            duplicate_messages=0,
            failure_count=0,
        )

    monkeypatch.setattr(rebuild, "import_pst_sources", fake_import_pst_sources)

    exit_code = rebuild.run_rebuild(
        phase="import",
        run_all=False,
        resume=True,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=[pst_one, pst_two],
    )

    checkpoint = rebuild.load_checkpoint(work_dir)
    assert exit_code == 0
    assert imported_source_paths == [str(pst_two.resolve())]
    assert checkpoint["imported_psts"] == [
        str(pst_one.resolve()),
        str(pst_two.resolve()),
    ]
    assert _count_messages(stage_db) == 2


def test_scan_pst_coverage_until_creates_coverage_scan_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = _load_rebuild_module()
    pst_path = tmp_path / "coverage.pst"
    pst_path.write_bytes(b"pst")
    coverage_scan_dir = (tmp_path / "work" / ".coverage-scan").resolve()
    # An inert stand-in so ReadpstParser's constructor-time path resolution
    # succeeds without the gitignored .tools/ vendored binary; extraction is
    # stubbed out below, so nothing is ever executed. The resolved path is
    # asserted afterwards, otherwise this test would keep passing on a host
    # that does have the vendored binary.
    stub_readpst = tmp_path / "stub-readpst.exe"
    stub_readpst.write_text("", encoding="utf-8")

    resolved_readpst_paths: list[Path] = []

    def fake_iter_extraction_results(self, source):
        del source
        resolved_readpst_paths.append(self._readpst_path)
        return iter(())

    monkeypatch.setattr(
        rebuild.ReadpstParser,
        "iter_extraction_results",
        fake_iter_extraction_results,
    )

    latest = rebuild.scan_pst_coverage_until(
        work_dir=tmp_path / "work",
        pst_paths=[pst_path],
        readpst_path=stub_readpst,
    )

    assert latest is None
    assert coverage_scan_dir.is_dir()
    assert resolved_readpst_paths == [stub_readpst.resolve()]


def test_coverage_report_rejects_mnt_work_dir_on_direct_phase_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = _load_rebuild_module()
    stage_db = tmp_path / "stage.db"
    pst_path = tmp_path / "coverage.pst"
    pst_path.write_bytes(b"pst")
    context = _make_rebuild_context(
        rebuild,
        work_dir=Path("/mnt/disallowed-rebuild"),
        stage_db=stage_db,
        pst_paths=(pst_path,),
        resolve_work_dir=False,
    )

    with pytest.raises(RuntimeError, match="work dir under /mnt"):
        rebuild.run_coverage_report_phase(
            context,
            rebuild.load_checkpoint(tmp_path / "work"),
        )


def test_backfill_rejects_mnt_work_dir_on_direct_phase_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = _load_rebuild_module()
    stage_db = tmp_path / "stage.db"
    initialize_database(stage_db)
    context = _make_rebuild_context(
        rebuild,
        work_dir=Path("/mnt/disallowed-rebuild"),
        stage_db=stage_db,
        resolve_work_dir=False,
    )

    monkeypatch.setattr(
        rebuild,
        "run_sync_imap",
        lambda **_kwargs: SyncExecutionResult(
            pulled_messages=1,
            folder_count=1,
            candidate_messages=1,
            inserted_messages=1,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=0,
        ),
    )

    with pytest.raises(RuntimeError, match="work dir under /mnt"):
        rebuild.run_backfill_phase(
            context,
            rebuild.load_checkpoint(tmp_path / "work"),
        )


def test_backfill_failure_does_not_block_verify_cas_or_validate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")

    _seed_searchable_stage_db(stage_db, str(pst_path.with_suffix("").resolve()))
    _write_checkpoint(
        work_dir,
        completed_phases=["preflight", "coverage-report", "import"],
        imported_psts=[str(pst_path.resolve())],
        round_identity={
            "stage_db": str(stage_db.resolve()),
            "pst_inputs": [str(pst_path.resolve())],
        },
        staged_rows={str(pst_path.resolve()): 1},
        staged_total=1,
    )

    monkeypatch.setattr(
        rebuild,
        "run_sync_imap",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("server unavailable")),
    )
    # Resume re-runs preflight (it validates live Outlook state); this test is
    # about backfill, so stand it down rather than reach for COM.
    monkeypatch.setattr(rebuild, "run_preflight_phase", lambda *_a, **_k: True)

    phase_calls: list[str] = []
    original_verify = rebuild.run_verify_cas_phase
    original_validate = rebuild.run_validate_phase

    def wrapped_verify(context, checkpoint):
        phase_calls.append("verify-cas")
        return original_verify(context, checkpoint)

    def wrapped_validate(context, checkpoint):
        phase_calls.append("validate")
        return original_validate(context, checkpoint)

    monkeypatch.setattr(rebuild, "run_verify_cas_phase", wrapped_verify)
    monkeypatch.setattr(rebuild, "run_validate_phase", wrapped_validate)

    exit_code = rebuild.run_rebuild(
        resume=True,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=[pst_path],
    )

    checkpoint = rebuild.load_checkpoint(work_dir)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "WARNING" in captured.out
    assert phase_calls == ["verify-cas", "validate"]
    assert "backfill" not in checkpoint["completed_phases"]
    assert checkpoint["completed_phases"][-2:] == ["verify-cas", "validate"]


def test_backfill_success_marks_phase_complete_and_calls_full_resync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    initialize_database(stage_db)
    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
    )
    captured_calls: list[dict[str, object]] = []

    def fake_run_sync_imap(**kwargs) -> SyncExecutionResult:
        captured_calls.append(kwargs)
        return SyncExecutionResult(
            pulled_messages=5,
            folder_count=2,
            candidate_messages=5,
            inserted_messages=5,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=0,
            failure_message=None,
        )

    monkeypatch.setattr(rebuild, "run_sync_imap", fake_run_sync_imap)

    result = rebuild.run_backfill_phase(context, rebuild.load_checkpoint(work_dir))

    captured = capsys.readouterr()
    assert result is True
    assert len(captured_calls) == 1
    assert captured_calls[0]["full_resync"] is True
    assert "pulled 5" in captured.out
    # Isolation: backfill must use work_dir-scoped paths, not production defaults
    called_maildir = captured_calls[0]["maildir_root"]
    called_log_dir = captured_calls[0]["log_dir"]
    assert Path(str(called_maildir)).is_relative_to(work_dir), (
        f"maildir_root {called_maildir!r} is not under work_dir {work_dir}"
    )
    assert Path(str(called_log_dir)).is_relative_to(work_dir), (
        f"log_dir {called_log_dir!r} is not under work_dir {work_dir}"
    )
    assert called_maildir != rebuild.DEFAULT_MAILDIR_ROOT
    assert called_log_dir != rebuild.DEFAULT_LOG_DIR


def test_validate_fails_when_fts_count_differs_from_messages_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    _seed_searchable_stage_db(stage_db, "fts-mismatch")
    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
    )

    conn = sqlite3.connect(stage_db)
    try:
        conn.execute(
            """
            INSERT INTO messages (
                subject,
                sender_name,
                sender_email,
                sent_at,
                received_at,
                body_text,
                message_id,
                in_reply_to,
                references_header,
                pst_source,
                folder_path,
                logical_locator,
                source_input_mode,
                source_requested_from,
                dedup_key,
                dedup_basis,
                recipient_search_text,
                attachment_search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "Broken FTS parity",
                "sender@example.com",
                "sender@example.com",
                "2026-06-01T09:10:00",
                "2026-06-01T09:11:00",
                "keyword parity body text",
                "<fts-mismatch-extra@example.com>",
                None,
                None,
                "fts-mismatch.pst",
                "Inbox",
                "Inbox/fts-mismatch-extra",
                "file",
                "fts-mismatch.pst",
                "message-id:<fts-mismatch-extra@example.com>",
                '{"method":"message-id"}',
                "recipient@example.com",
                "",
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        rebuild,
        "check_database",
        lambda *_args, **_kwargs: {"passed": True},
    )

    with pytest.raises(RuntimeError, match="message_fts row count does not match"):
        rebuild.run_validate_phase(
            context,
            rebuild.load_checkpoint(work_dir),
        )


def test_validate_fails_when_keyword_spot_check_returns_no_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    _seed_searchable_stage_db(stage_db, "missing-search-hit")
    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
    )

    monkeypatch.setattr(
        rebuild,
        "check_database",
        lambda *_args, **_kwargs: {"passed": True},
    )
    monkeypatch.setattr(rebuild, "search_messages", lambda **_kwargs: [])

    with pytest.raises(RuntimeError, match="keyword spot-check returned no results"):
        rebuild.run_validate_phase(
            context,
            rebuild.load_checkpoint(work_dir),
        )


def test_import_phase_warns_on_partial_failures_but_still_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rebuild = _load_rebuild_module()
    pst_path = tmp_path / "archive-1.pst"
    pst_path.write_bytes(b"pst")
    work_dir = tmp_path / "work"
    stage_db = work_dir / "stage.db"
    initialize_database(stage_db)
    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=(pst_path,),
        resume=False,
    )
    checkpoint = rebuild.load_checkpoint(work_dir)

    def fake_import_pst_sources(
        *,
        database_path,
        sources,
        backend=None,
        readpst_path=None,
        work_root=None,
        parallel_jobs=None,
        attachment_root=None,
    ) -> ImportSummary:
        del (
            database_path,
            sources,
            backend,
            readpst_path,
            work_root,
            parallel_jobs,
            attachment_root,
        )
        return ImportSummary(
            discovered_psts=1,
            imported_messages=5,
            duplicate_messages=0,
            failure_count=3,
        )

    monkeypatch.setattr(rebuild, "import_pst_sources", fake_import_pst_sources)

    assert rebuild.run_import_phase(context, checkpoint) is True

    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "3" in captured.out
    assert "ingest_failures" in captured.out
    assert checkpoint["imported_psts"] == [str(pst_path.resolve())]


def test_coverage_report_warns_and_does_not_block_when_pst_date_undetermined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """coverage-report must not block later phases when PST coverage date is None."""
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")

    # scan returns None — cannot determine coverage date
    monkeypatch.setattr(rebuild, "scan_pst_coverage_until", lambda **_kwargs: None)

    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=(pst_path,),
    )
    checkpoint = rebuild.load_checkpoint(work_dir)

    result = rebuild.run_coverage_report_phase(context, checkpoint)

    captured = capsys.readouterr()
    assert result is True, (
        "coverage-report phase must return True (completed) even when date undetermined"
    )
    assert "WARNING" in captured.out
    assert "coverage" in captured.out.lower()


def test_coverage_report_warns_and_does_not_block_when_calculate_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """coverage-report must not block later phases when calculate raises."""
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")

    monkeypatch.setattr(
        rebuild,
        "calculate_coverage_report",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("readpst crashed")),
    )

    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=(pst_path,),
    )
    checkpoint = rebuild.load_checkpoint(work_dir)

    result = rebuild.run_coverage_report_phase(context, checkpoint)

    captured = capsys.readouterr()
    assert result is True
    assert "WARNING" in captured.out
    assert "readpst crashed" in captured.out


def test_coverage_report_failure_does_not_block_later_phases_in_run_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--all run: coverage-report warning must not stop later phases."""
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")

    _write_checkpoint(
        work_dir,
        completed_phases=["preflight"],
    )

    # scan returns None → coverage cannot be computed
    monkeypatch.setattr(rebuild, "scan_pst_coverage_until", lambda **_kwargs: None)
    # Resume re-runs preflight (it validates live Outlook state); this test is
    # about coverage-report, so stand it down rather than reach for COM.
    monkeypatch.setattr(rebuild, "run_preflight_phase", lambda *_a, **_k: True)

    later_phase_calls: list[str] = []

    monkeypatch.setattr(
        rebuild,
        "run_import_phase",
        lambda *_args, **_kwargs: later_phase_calls.append("import") or True,
    )
    monkeypatch.setattr(
        rebuild,
        "run_backfill_phase",
        lambda *_args, **_kwargs: later_phase_calls.append("backfill") or True,
    )
    monkeypatch.setattr(
        rebuild,
        "run_verify_cas_phase",
        lambda *_args, **_kwargs: later_phase_calls.append("verify-cas") or True,
    )
    monkeypatch.setattr(
        rebuild,
        "run_validate_phase",
        lambda *_args, **_kwargs: later_phase_calls.append("validate") or True,
    )

    exit_code = rebuild.run_rebuild(
        resume=True,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=[pst_path],
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "WARNING" in captured.out
    assert later_phase_calls == ["import", "backfill", "verify-cas", "validate"]


def test_validate_phase_reports_ingest_failures_count_but_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    _seed_searchable_stage_db(stage_db, "ingest-fail-vis")
    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
    )

    conn = sqlite3.connect(stage_db)
    try:
        conn.execute(
            """
            INSERT INTO ingest_failures (
                pst_source,
                source_input_mode,
                source_requested_from,
                error_stage,
                error_type,
                error_message
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "ingest-fail-vis.pst",
                "file",
                "ingest-fail-vis.pst",
                "parse",
                "RuntimeError",
                "test failure",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        rebuild,
        "check_database",
        lambda *_args, **_kwargs: {"passed": True},
    )

    assert (
        rebuild.run_validate_phase(context, rebuild.load_checkpoint(work_dir)) is True
    )

    captured = capsys.readouterr()
    assert "1" in captured.out
    assert "ingest_failures" in captured.out


def _mount_outlook_stores(
    monkeypatch: pytest.MonkeyPatch, rebuild, *mounted: Path
) -> OutlookComReader:
    """Point preflight at a real `OutlookComReader` over a fake Outlook profile.

    Only the COM collection is faked. Store matching, the "already mounted"
    refusal and its wording all stay the production code's -- a hand-written
    double would have to restate `_normalized_windows_path`'s case folding and
    then keep passing after the real rule changed.
    """
    stores = [SimpleNamespace(FilePath=str(path.resolve())) for path in mounted]
    namespace = SimpleNamespace(
        Stores=SimpleNamespace(Count=len(stores), Item=lambda index: stores[index - 1])
    )
    reader = OutlookComReader(namespace=namespace)
    monkeypatch.setattr(rebuild, "OutlookComReader", lambda: reader)
    return reader


def test_preflight_requires_every_pst_to_be_a_mounted_outlook_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    stage_db = work_dir / "stage.db"
    pst_one = tmp_path / "archive-1.pst"
    pst_two = tmp_path / "archive-2.pst"
    pst_one.write_bytes(b"pst-one")
    pst_two.write_bytes(b"pst-two")

    _mount_outlook_stores(monkeypatch, rebuild, pst_one, pst_two)
    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=(pst_one, pst_two),
    )

    assert rebuild.run_preflight_phase(context, rebuild.load_checkpoint(work_dir))

    captured = capsys.readouterr()
    assert str(pst_one.resolve()) in captured.out
    assert str(pst_two.resolve()) in captured.out


def test_preflight_fails_when_a_pst_is_not_mounted_in_outlook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    stage_db = work_dir / "stage.db"
    mounted_pst = tmp_path / "mounted.pst"
    unmounted_pst = tmp_path / "unmounted.pst"
    mounted_pst.write_bytes(b"pst-mounted")
    unmounted_pst.write_bytes(b"pst-unmounted")

    _mount_outlook_stores(monkeypatch, rebuild, mounted_pst)
    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=(mounted_pst, unmounted_pst),
    )

    with pytest.raises(RuntimeError, match=r"already mounted.*unmounted\.pst"):
        rebuild.run_preflight_phase(context, rebuild.load_checkpoint(work_dir))


def test_preflight_stops_the_rebuild_when_outlook_is_not_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    pst_path = tmp_path / "archive-1.pst"
    pst_path.write_bytes(b"pst")
    later_phases: list[str] = []

    def refuse_to_connect() -> object:
        # Stands in for the real guard, whose wording is anchored in
        # tests/test_outlook_pst_parser.py; what this test owns is that
        # preflight surfaces the reader's refusal rather than swallowing it.
        raise RuntimeError("Outlook is not running (test stand-in)")

    monkeypatch.setattr(
        rebuild,
        "OutlookComReader",
        lambda: OutlookComReader(namespace=None),
    )
    monkeypatch.setattr(
        "mailattic.outlook_pst._load_outlook_namespace",
        refuse_to_connect,
    )
    monkeypatch.setattr(
        rebuild,
        "run_coverage_report_phase",
        lambda *_args, **_kwargs: later_phases.append("coverage-report"),
    )
    monkeypatch.setattr(
        rebuild,
        "run_import_phase",
        lambda *_args, **_kwargs: later_phases.append("import"),
    )

    exit_code = rebuild.run_rebuild(
        work_dir=work_dir,
        stage_db=work_dir / "stage.db",
        pst_paths=[pst_path],
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "Outlook is not running" in captured.err
    assert later_phases == []


def test_import_phase_uses_the_outlook_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    stage_db = work_dir / "stage.db"
    pst_path = tmp_path / "archive-1.pst"
    pst_path.write_bytes(b"pst")
    seen_backends: list[str | None] = []

    def fake_import_pst_sources(*, backend=None, **_kwargs) -> ImportSummary:
        seen_backends.append(backend)
        return ImportSummary(discovered_psts=1, imported_messages=1)

    monkeypatch.setattr(rebuild, "import_pst_sources", fake_import_pst_sources)
    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=(pst_path,),
    )

    assert rebuild.run_import_phase(context, rebuild.load_checkpoint(work_dir)) is True
    assert seen_backends == ["outlook"]


def test_import_phase_fails_when_recipient_extraction_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete recipient extraction is the exact defect this rebuild path
    exists to avoid (issue #180): carrying on would stage a database whose
    recipients are silently wrong, and the later phases cannot tell."""
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    stage_db = work_dir / "stage.db"
    pst_path = tmp_path / "archive-1.pst"
    pst_path.write_bytes(b"pst")

    monkeypatch.setattr(
        rebuild,
        "import_pst_sources",
        lambda **_kwargs: ImportSummary(
            discovered_psts=1,
            imported_messages=5,
            recipient_extraction_complete=False,
            recipient_extraction_summary_path=str(work_dir / "summary.json"),
        ),
    )
    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=(pst_path,),
    )
    checkpoint = rebuild.load_checkpoint(work_dir)

    with pytest.raises(RuntimeError, match="recipient extraction"):
        rebuild.run_import_phase(context, checkpoint)
    assert checkpoint["imported_psts"] == []


def test_preflight_reaches_outlook_only_through_the_no_launch_reader() -> None:
    """Issue #180 acceptance: preflight must not start Outlook.

    The stub-injected preflight tests above prove preflight goes through
    `OutlookComReader`, but they cannot see a second, Dispatch-based path added
    beside it -- and `Dispatch("Outlook.Application")` is exactly what launches
    Outlook under a profile that need not carry these PSTs. This reads the
    module source so that adding one turns this test red. It walks the AST
    rather than the raw text so that naming those calls in a comment -- which
    this module does, to say why it avoids them -- is not itself a failure.
    """
    rebuild = _load_rebuild_module()
    tree = ast.parse(Path(rebuild.__file__).read_text(encoding="utf-8"))
    called_names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    }

    assert called_names.isdisjoint({"Dispatch", "GetActiveObject"})


def test_import_phase_refuses_a_pst_the_checkpoint_never_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-imported PST must stop the rerun, not be silently blessed.

    `persist_message` never updates an existing row, so the second attempt
    would count every one of the first attempt's rows as a duplicate, balance
    its own extraction accounting, and checkpoint the PST with the first
    attempt's recipients still in place.
    """
    rebuild = _load_rebuild_module()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    pst_path = tmp_path / "archive-1.pst"
    pst_path.write_bytes(b"pst")

    # Rows for this PST, and a checkpoint that never accepted it. The seed
    # helper appends ".pst" to the tag, so the tag is the resolved path minus
    # that suffix -- the stored pst_source then equals what the import phase
    # will look for.
    _seed_searchable_stage_db(stage_db, str((tmp_path / "archive-1").resolve()))
    _write_checkpoint(work_dir, completed_phases=["preflight"], imported_psts=[])

    def fail_if_called(**_kwargs) -> ImportSummary:
        raise AssertionError("import must be refused before it re-reads the PST")

    monkeypatch.setattr(rebuild, "import_pst_sources", fail_if_called)
    context = _make_rebuild_context(
        rebuild,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=(pst_path,),
        resume=True,
    )

    with pytest.raises(RuntimeError, match="fresh stage database"):
        rebuild.run_import_phase(context, rebuild.load_checkpoint(work_dir))

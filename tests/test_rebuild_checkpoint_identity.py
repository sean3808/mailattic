"""#236: a rebuild checkpoint is a claim, verified before work is skipped."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from mailattic import rebuild
from mailattic.models import ImportSummary
from mailattic.storage import initialize_database

ALL_PHASES = list(rebuild.PHASE_SEQUENCE)


def _seed_stage(db_path: Path, pst_source: str, rows: int = 1) -> None:
    initialize_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        for i in range(rows):
            conn.execute(
                "INSERT INTO messages (subject, body_text, message_id, pst_source,"
                " folder_path, logical_locator,"
                " dedup_key, dedup_basis) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    f"s{i}",
                    f"keyword{i}",
                    f"<{i}@{Path(pst_source).stem}>",
                    pst_source,
                    "Inbox",
                    f"Inbox/{i}",
                    f"k{i}-{pst_source}",
                    "{}",
                ],
            )
        conn.commit()
    finally:
        conn.close()


def _run_cli(work_dir: Path, pst: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from mailattic.cli import main; sys.exit(main(sys.argv[1:]))",
            "rebuild",
            "--resume",
            "--work-dir",
            str(work_dir),
            "--pst",
            str(pst),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def test_cli_resume_of_complete_checkpoint_without_stage_db_is_refused(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    rebuild.save_checkpoint(
        work,
        {
            "completed_phases": ALL_PHASES,
            "imported_psts": ["old.pst"],
            "schema_version": 1,
        },
    )
    result = _run_cli(work, tmp_path / "nonexistent.pst")

    assert result.returncode != 0
    assert "stage database" in result.stderr
    assert "without --resume" in result.stderr
    assert not (work / "stage.db").exists()


def test_partial_checkpoint_with_missing_stage_db_does_not_create_empty_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    pst_a = tmp_path / "a.pst"
    pst_a.write_bytes(b"a")
    stage = tmp_path / "other" / "stage.db"
    rebuild.save_checkpoint(
        work,
        {
            "completed_phases": ["preflight", "coverage-report"],
            "imported_psts": [str(pst_a.resolve())],
            "schema_version": 1,
        },
    )
    monkeypatch.setattr(rebuild, "run_preflight_phase", lambda *_a, **_k: True)

    code = rebuild.run_rebuild(
        phase="import",
        run_all=False,
        resume=True,
        work_dir=work,
        stage_db=stage,
        pst_paths=[pst_a],
    )

    assert code != 0
    assert not stage.exists()
    assert "import" not in rebuild.load_checkpoint(work)["completed_phases"]


def _import_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, psts: list[Path]
) -> Path:
    """Run a real `import` phase (fake Outlook reader only) and return work dir."""
    work = tmp_path / "work"
    imports: list[str] = []

    def fake_import(*, database_path, sources, **_kw) -> ImportSummary:
        source = sources[0]
        imports.append(source.source_path)
        _seed_stage(Path(database_path), source.source_path, rows=2)
        return ImportSummary(
            discovered_psts=1,
            imported_messages=2,
            duplicate_messages=0,
            failure_count=0,
        )

    monkeypatch.setattr(rebuild, "import_pst_sources", fake_import)
    code = rebuild.run_rebuild(
        phase="import", run_all=False, work_dir=work, pst_paths=psts
    )
    assert code == 0
    assert len(imports) == len(psts)
    return work


def test_replaced_stage_db_or_changed_inputs_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pst_a = tmp_path / "a.pst"
    pst_a.write_bytes(b"a")
    pst_b = tmp_path / "b.pst"
    pst_b.write_bytes(b"b")
    work = _import_round(tmp_path, monkeypatch, [pst_a])
    monkeypatch.setattr(rebuild, "run_preflight_phase", lambda *_a, **_k: True)
    stage = work / "stage.db"

    # different PST inputs -> not the same round
    assert (
        rebuild.run_rebuild(
            phase="import",
            run_all=False,
            resume=True,
            work_dir=work,
            pst_paths=[pst_a, pst_b],
        )
        != 0
    )
    # different stage DB target
    assert (
        rebuild.run_rebuild(
            phase="import",
            run_all=False,
            resume=True,
            work_dir=work,
            stage_db=tmp_path / "elsewhere.db",
            pst_paths=[pst_a],
        )
        != 0
    )
    # same path, but the DB was replaced by another with different content
    stage.unlink()
    _seed_stage(stage, "unrelated.pst", rows=5)
    assert (
        rebuild.run_rebuild(
            phase="import",
            run_all=False,
            resume=True,
            work_dir=work,
            pst_paths=[pst_a],
        )
        != 0
    )
    # ...or by an empty one
    stage.unlink()
    initialize_database(stage)
    assert (
        rebuild.run_rebuild(
            phase="import",
            run_all=False,
            resume=True,
            work_dir=work,
            pst_paths=[pst_a],
        )
        != 0
    )


def test_same_round_resume_skips_extraction_and_still_runs_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pst_a = tmp_path / "a.pst"
    pst_a.write_bytes(b"a")
    work = _import_round(tmp_path, monkeypatch, [pst_a])

    checkpoint = rebuild.load_checkpoint(work)
    assert checkpoint["round"]["stage_db"] == str((work / "stage.db").resolve())
    assert checkpoint["staged_rows"] == {str(pst_a.resolve()): 2}

    def reader_must_not_run(**_kw):
        raise AssertionError("same-round resume must not re-extract")

    calls: list[str] = []
    monkeypatch.setattr(rebuild, "import_pst_sources", reader_must_not_run)
    monkeypatch.setattr(
        rebuild,
        "run_preflight_phase",
        lambda *_a, **_k: calls.append("preflight") or True,
    )
    for name in (
        "run_coverage_report_phase",
        "run_backfill_phase",
        "run_verify_cas_phase",
        "run_validate_phase",
    ):
        monkeypatch.setattr(rebuild, name, lambda *_a, **_k: True)
    checkpoint["completed_phases"] = ["preflight"]
    rebuild.save_checkpoint(work, checkpoint)

    code = rebuild.run_rebuild(resume=True, work_dir=work, pst_paths=[pst_a])

    assert code == 0  # import skipped the accepted PST: reader never ran
    assert calls == ["preflight"]


def test_fully_completed_same_round_resume_runs_live_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pst_a = tmp_path / "a.pst"
    pst_a.write_bytes(b"a")
    work = _import_round(tmp_path, monkeypatch, [pst_a])
    checkpoint = rebuild.load_checkpoint(work)
    checkpoint["completed_phases"] = ALL_PHASES
    rebuild.save_checkpoint(work, checkpoint)

    monkeypatch.setattr(
        rebuild,
        "import_pst_sources",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("re-extracted")),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        rebuild, "run_preflight_phase", lambda *_a, **_k: calls.append("p") or True
    )

    assert rebuild.run_rebuild(resume=True, work_dir=work, pst_paths=[pst_a]) == 0
    assert calls == ["p"]

    # and a failing live preflight now fails the resume
    def preflight_fails(*_a, **_k):
        raise FileNotFoundError("preflight: PST source not found")

    monkeypatch.setattr(rebuild, "run_preflight_phase", preflight_fails)
    assert rebuild.run_rebuild(resume=True, work_dir=work, pst_paths=[pst_a]) != 0


def test_legacy_checkpoint_without_round_is_refused_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pst_a = tmp_path / "a.pst"
    pst_a.write_bytes(b"a")
    work = tmp_path / "work"
    work.mkdir()
    stage_db = work / "stage.db"
    _seed_stage(stage_db, "whatever.pst")

    legacy = {
        "completed_phases": ["preflight", "coverage-report", "import"],
        "imported_psts": [str(pst_a.resolve())],
        "schema_version": 1,
    }
    ckpt_path = work / rebuild.CHECKPOINT_FILENAME
    ckpt_path.write_text(json.dumps(legacy), "utf-8")

    db_bytes_before = stage_db.read_bytes()
    ckpt_bytes_before = ckpt_path.read_bytes()

    for name in rebuild.PHASE_SEQUENCE:

        def _fail(*_args, _name=name, **_kwargs):
            raise AssertionError(f"Phase {_name} must not run on refused resume")

        monkeypatch.setattr(rebuild, f"run_{name.replace('-', '_')}_phase", _fail)

    nonexistent_env = tmp_path / "nonexistent.env"
    code = rebuild.run_rebuild(
        resume=True,
        work_dir=work,
        pst_paths=[pst_a],
        env_file=nonexistent_env,
    )

    assert code != 0
    assert "round" not in rebuild.load_checkpoint(work)
    assert stage_db.read_bytes() == db_bytes_before
    assert ckpt_path.read_bytes() == ckpt_bytes_before


def test_non_resume_single_phase_does_not_stamp_round_on_legacy_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pst_a = tmp_path / "a.pst"
    pst_a.write_bytes(b"a")
    work = tmp_path / "work"
    work.mkdir()
    legacy = {
        "completed_phases": ["preflight", "coverage-report", "import"],
        "imported_psts": [str(pst_a.resolve())],
        "schema_version": 1,
    }
    (work / rebuild.CHECKPOINT_FILENAME).write_text(json.dumps(legacy), "utf-8")
    _seed_stage(work / "stage.db", "whatever.pst")
    monkeypatch.setattr(rebuild, "run_validate_phase", lambda *_a, **_k: True)

    # an unverified run must not lend this stage DB's identity to the old claims
    assert (
        rebuild.run_rebuild(
            phase="validate", run_all=False, work_dir=work, pst_paths=[pst_a]
        )
        == 0
    )
    assert "round" not in rebuild.load_checkpoint(work)


def _zero_row_import_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pst: Path
) -> Path:
    """An import round whose only PST yields no messages (all mail is on IMAP)."""
    work = tmp_path / "work"

    def fake_import(*, database_path, **_kw) -> ImportSummary:
        initialize_database(Path(database_path))
        return ImportSummary(
            discovered_psts=1,
            imported_messages=0,
            duplicate_messages=0,
            failure_count=0,
        )

    monkeypatch.setattr(rebuild, "import_pst_sources", fake_import)
    assert (
        rebuild.run_rebuild(
            phase="import", run_all=False, work_dir=work, pst_paths=[pst]
        )
        == 0
    )
    assert rebuild.load_checkpoint(work)["staged_rows"] == {str(pst.resolve()): 0}
    monkeypatch.setattr(rebuild, "run_preflight_phase", lambda *_a, **_k: True)
    return work


def test_zero_row_psts_resume_before_backfill_with_an_empty_stage_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pst_a = tmp_path / "a.pst"
    pst_a.write_bytes(b"a")
    work = _zero_row_import_round(tmp_path, monkeypatch, pst_a)

    assert (
        rebuild.run_rebuild(
            phase="import", run_all=False, resume=True, work_dir=work, pst_paths=[pst_a]
        )
        == 0
    )


def _complete_backfill(
    work: Path, monkeypatch: pytest.MonkeyPatch, pst: Path, rows: int
) -> None:
    """Run a real `backfill` phase whose sync adds ``rows`` non-PST messages."""

    def fake_backfill(context, _checkpoint) -> bool:
        _seed_stage(context.stage_db, "imap-backfill", rows=rows)
        return True

    monkeypatch.setattr(rebuild, "run_backfill_phase", fake_backfill)
    assert (
        rebuild.run_rebuild(
            phase="backfill", run_all=False, work_dir=work, pst_paths=[pst]
        )
        == 0
    )
    for name in ("run_verify_cas_phase", "run_validate_phase"):
        monkeypatch.setattr(rebuild, name, lambda *_a, **_k: True)


def test_backfilled_round_with_zero_row_psts_refuses_an_emptied_stage_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every PST imported 0 rows and all mail came from backfill: the per-PST
    # counts (0 == 0) cannot notice the stage DB being swapped for an empty one.
    pst_a = tmp_path / "a.pst"
    pst_a.write_bytes(b"a")
    work = _zero_row_import_round(tmp_path, monkeypatch, pst_a)
    stage = work / "stage.db"
    _complete_backfill(work, monkeypatch, pst_a, rows=3)
    checkpoint = rebuild.load_checkpoint(work)
    checkpoint["completed_phases"] = ALL_PHASES
    rebuild.save_checkpoint(work, checkpoint)

    assert rebuild.run_rebuild(resume=True, work_dir=work, pst_paths=[pst_a]) == 0

    stage.unlink()
    initialize_database(stage)
    assert rebuild.run_rebuild(resume=True, work_dir=work, pst_paths=[pst_a]) != 0


def test_stage_db_rolled_back_to_its_pst_only_snapshot_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same path, same per-PST counts, but the backfilled mail is gone: the
    # per-PST comparison alone would skip the completed backfill and succeed.
    pst_a = tmp_path / "a.pst"
    pst_a.write_bytes(b"a")
    work = _import_round(tmp_path, monkeypatch, [pst_a])
    monkeypatch.setattr(rebuild, "run_preflight_phase", lambda *_a, **_k: True)
    stage = work / "stage.db"
    _complete_backfill(work, monkeypatch, pst_a, rows=3)
    assert rebuild.load_checkpoint(work)["staged_total"] == 5

    stage.unlink()
    _seed_stage(stage, str(pst_a.resolve()), rows=2)
    assert rebuild.run_rebuild(resume=True, work_dir=work, pst_paths=[pst_a]) != 0

    # a single-phase run in between must not lower the recorded total
    assert (
        rebuild.run_rebuild(
            phase="preflight", run_all=False, work_dir=work, pst_paths=[pst_a]
        )
        == 0
    )
    assert rebuild.load_checkpoint(work)["staged_total"] == 5
    assert rebuild.run_rebuild(resume=True, work_dir=work, pst_paths=[pst_a]) != 0


def test_rows_added_by_an_unfinished_phase_do_not_block_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A backfill that failed part-way leaves more mail than any completed phase
    # recorded; resuming it is the normal recovery path.
    pst_a = tmp_path / "a.pst"
    pst_a.write_bytes(b"a")
    work = _import_round(tmp_path, monkeypatch, [pst_a])
    monkeypatch.setattr(rebuild, "run_preflight_phase", lambda *_a, **_k: True)
    _seed_stage(work / "stage.db", "imap-backfill", rows=3)
    for name in ("run_backfill_phase", "run_verify_cas_phase", "run_validate_phase"):
        monkeypatch.setattr(rebuild, name, lambda *_a, **_k: True)

    assert rebuild.run_rebuild(resume=True, work_dir=work, pst_paths=[pst_a]) == 0

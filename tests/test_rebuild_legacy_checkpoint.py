from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mailattic import rebuild
from mailattic.cli import main
from mailattic.storage import initialize_database


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_stage(db_path: Path, pst_source: str, rows: int = 1) -> None:
    initialize_database(db_path)
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        for i in range(rows):
            conn.execute(
                """
                INSERT INTO messages (
                    subject, body_text, message_id, pst_source,
                    folder_path, logical_locator, dedup_key, dedup_basis
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    f"subject {i}",
                    f"keyword {i}",
                    f"<{i}@{Path(pst_source).stem}>",
                    pst_source,
                    "Inbox",
                    f"Inbox/{i}",
                    f"dedup-{i}-{pst_source}",
                    "{}",
                ],
            )
        conn.commit()
    finally:
        conn.close()


def _stub_all_phases_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in rebuild.PHASE_SEQUENCE:

        def _fail(*_args, _name=name, **_kwargs):
            raise AssertionError(f"Phase {_name} must not run on refused resume")

        monkeypatch.setattr(rebuild, f"run_{name.replace('-', '_')}_phase", _fail)


def test_legacy_checkpoint_with_imported_psts_is_refused_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pst = tmp_path / "archive-1.pst"
    pst.write_bytes(b"pst")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"

    _seed_stage(stage_db, "unrelated.pst", rows=5)

    ckpt_path = rebuild.save_checkpoint(
        work_dir,
        {
            "completed_phases": ["preflight", "coverage-report", "import"],
            "imported_psts": [str(pst.resolve())],
            "schema_version": 1,
        },
    )

    db_hash_before = _sha256(stage_db)
    ckpt_hash_before = _sha256(ckpt_path)

    _stub_all_phases_must_not_run(monkeypatch)
    nonexistent_env = tmp_path / "nonexistent.env"

    exit_code = rebuild.run_rebuild(
        resume=True,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=[pst],
        env_file=nonexistent_env,
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "REFUSED" in captured.err
    assert "without --resume" in captured.err
    assert _sha256(stage_db) == db_hash_before
    assert _sha256(ckpt_path) == ckpt_hash_before


def test_legacy_checkpoint_with_banked_phase_but_no_round_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pst = tmp_path / "archive-1.pst"
    pst.write_bytes(b"pst")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    _seed_stage(stage_db, "seed.pst", rows=2)

    ckpt_path = rebuild.save_checkpoint(
        work_dir,
        {
            "completed_phases": ["preflight", "coverage-report"],
            "imported_psts": [],
            "schema_version": 1,
        },
    )

    db_hash_before = _sha256(stage_db)
    ckpt_hash_before = _sha256(ckpt_path)

    _stub_all_phases_must_not_run(monkeypatch)
    nonexistent_env = tmp_path / "nonexistent.env"

    exit_code = rebuild.run_rebuild(
        resume=True,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=[pst],
        env_file=nonexistent_env,
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "REFUSED" in captured.err
    assert "without --resume" in captured.err
    assert _sha256(stage_db) == db_hash_before
    assert _sha256(ckpt_path) == ckpt_hash_before


def test_checkpoint_with_round_but_missing_per_pst_counts_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pst = tmp_path / "archive-1.pst"
    pst.write_bytes(b"pst")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    _seed_stage(stage_db, str(pst.resolve()), rows=3)

    ckpt_path = rebuild.save_checkpoint(
        work_dir,
        {
            "completed_phases": ["preflight", "coverage-report", "import"],
            "imported_psts": [str(pst.resolve())],
            "schema_version": 1,
            "round": {
                "stage_db": str(stage_db.resolve()),
                "pst_inputs": [str(pst.resolve())],
            },
            # staged_rows is absent or missing count for imported PST
        },
    )

    db_hash_before = _sha256(stage_db)
    ckpt_hash_before = _sha256(ckpt_path)

    _stub_all_phases_must_not_run(monkeypatch)
    nonexistent_env = tmp_path / "nonexistent.env"

    exit_code = rebuild.run_rebuild(
        resume=True,
        work_dir=work_dir,
        stage_db=stage_db,
        pst_paths=[pst],
        env_file=nonexistent_env,
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "REFUSED" in captured.err
    assert "without --resume" in captured.err
    assert _sha256(stage_db) == db_hash_before
    assert _sha256(ckpt_path) == ckpt_hash_before


def test_cli_resume_of_legacy_checkpoint_refused_in_process_with_inert_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pst = tmp_path / "archive-1.pst"
    pst.write_bytes(b"pst")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    stage_db = work_dir / "stage.db"
    _seed_stage(stage_db, "swapped.pst", rows=5)

    ckpt_path = rebuild.save_checkpoint(
        work_dir,
        {
            "completed_phases": ["preflight", "coverage-report", "import"],
            "imported_psts": [str(pst.resolve())],
            "schema_version": 1,
        },
    )

    db_hash_before = _sha256(stage_db)
    ckpt_hash_before = _sha256(ckpt_path)

    _stub_all_phases_must_not_run(monkeypatch)
    nonexistent_env = tmp_path / "nonexistent.env"
    monkeypatch.setattr(rebuild, "DEFAULT_ENV_FILE", nonexistent_env)

    exit_code = main(
        [
            "rebuild",
            "--resume",
            "--work-dir",
            str(work_dir),
            "--pst",
            str(pst),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "[rebuild] resume: REFUSED" in captured.err
    assert "without --resume" in captured.err
    assert _sha256(stage_db) == db_hash_before
    assert _sha256(ckpt_path) == ckpt_hash_before

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

_CRASH_WRITER_SCRIPT = """
import os
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE evidence(value TEXT)")
conn.commit()
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.execute("INSERT INTO evidence VALUES ('old-production-uncheckpointed')")
conn.commit()
os._exit(0)
"""


def _create_crash_wal_db(target: Path) -> tuple[Path, Path]:
    wal = Path(str(target) + "-wal")
    shm = Path(str(target) + "-shm")
    for f in (target, wal, shm):
        if f.exists():
            f.unlink()
    subprocess.run(
        [sys.executable, "-c", _CRASH_WRITER_SCRIPT, str(target)],
        check=True,
    )
    assert target.is_file()
    assert wal.is_file() and wal.stat().st_size > 0
    return target, wal


def _create_stage_db(stage: Path) -> Path:
    if stage.exists():
        stage.unlink()
    with closing(sqlite3.connect(stage)) as conn:
        conn.execute("CREATE TABLE evidence(value TEXT)")
        conn.execute("INSERT INTO evidence VALUES ('rebuilt-stage')")
        conn.commit()
    return stage


def test_dr_swap_with_wal_handling_preserves_stage_and_rollback(
    tmp_path: Path,
) -> None:
    """Documented safe swap procedure isolates old WAL sidecars into rollback backup.

    The swapped-in production DB reads 'rebuilt-stage', while the rollback material
    retains the uncheckpointed WAL row 'old-production-uncheckpointed'.
    """
    target = tmp_path / "production.db"
    stage = tmp_path / "stage.db"
    backup_db = tmp_path / "production.db.pre-rebuild.bak"
    backup_wal = Path(str(backup_db) + "-wal")
    backup_shm = Path(str(backup_db) + "-shm")

    _create_crash_wal_db(target)
    _create_stage_db(stage)

    target_wal = Path(str(target) + "-wal")
    target_shm = Path(str(target) + "-shm")

    # Documented safe runbook swap:
    # 1. Rename production DB to backup
    target.rename(backup_db)
    # 2. Isolate WAL and SHM sidecars along with backup
    if target_wal.exists():
        target_wal.rename(backup_wal)
    if target_shm.exists():
        target_shm.rename(backup_shm)
    # 3. Move stage DB to production DB path
    stage.rename(target)

    # Verification 1: Swapped-in DB reads rebuilt-stage, not old uncheckpointed row
    with closing(sqlite3.connect(target)) as conn:
        rows = conn.execute("SELECT value FROM evidence").fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchall()

    assert rows == [("rebuilt-stage",)]
    assert integrity == [("ok",)]

    # Verification 2: Old uncheckpointed row is preserved in rollback material.
    # Read a copy of the whole group, as the runbook says: opening the originals
    # would checkpoint and so alter the rollback material itself.
    inspect_dir = tmp_path / "inspect"
    inspect_dir.mkdir()
    for part in (backup_db, backup_wal, backup_shm):
        if part.exists():
            shutil.copy2(part, inspect_dir / part.name)
    with closing(sqlite3.connect(inspect_dir / backup_db.name)) as conn_bak:
        rows_bak = conn_bak.execute("SELECT value FROM evidence").fetchall()
        integrity_bak = conn_bak.execute("PRAGMA integrity_check").fetchall()

    assert rows_bak == [("old-production-uncheckpointed",)]
    assert integrity_bak == [("ok",)]

    # Verification 3: Rollback restores original uncheckpointed production state
    if target.exists():
        target.unlink()
    backup_db.rename(target)
    if backup_wal.exists():
        backup_wal.rename(target_wal)
    if backup_shm.exists():
        backup_shm.rename(target_shm)

    with closing(sqlite3.connect(target)) as conn_restored:
        rows_restored = conn_restored.execute("SELECT value FROM evidence").fetchall()
    assert rows_restored == [("old-production-uncheckpointed",)]


def test_dr_swap_without_wal_handling_replays_old_wal_onto_stage(
    tmp_path: Path,
) -> None:
    """Proves red-before-green failure mode: omitting WAL handling overwrites stage.

    When only the DB file is renamed and target-wal is left in place, SQLite's WAL
    recovery replaying on the newly moved stage.db replaces stage data with old WAL
    data ('old-production-uncheckpointed').
    """
    target = tmp_path / "production.db"
    stage = tmp_path / "stage.db"
    backup_db = tmp_path / "production.db.pre-rebuild.bak"

    _create_crash_wal_db(target)
    _create_stage_db(stage)

    # Flawed runbook swap (omits WAL handling)
    target.rename(backup_db)
    stage.rename(target)

    # Demonstrates that omitting WAL handling corrupts stage with old WAL replay
    with closing(sqlite3.connect(target)) as conn:
        rows = conn.execute("SELECT value FROM evidence").fetchall()
    assert rows == [("old-production-uncheckpointed",)]

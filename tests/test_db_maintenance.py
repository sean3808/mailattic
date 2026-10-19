"""Tests for db_maintenance: backup_database and check_database.

Covers spec scenarios in:
  openspec/changes/add-db-safety-and-global-install/specs/db-maintenance/spec.md

All tests use tmp_path-scoped SQLite fixtures; they never touch the real
mailattic.db in the repo root.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mailattic.db_maintenance import backup_database, check_database

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wal_db(path: Path) -> Path:
    """Create a minimal WAL-mode SQLite database with one row of data."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT);")
        conn.execute("INSERT INTO items (label) VALUES ('hello');")
        conn.commit()
    finally:
        conn.close()
    return path


# ---------------------------------------------------------------------------
# Scenario 1: Backup produces a standalone, openable consistent snapshot
# ---------------------------------------------------------------------------


def test_backup_produces_openable_snapshot(tmp_path: Path) -> None:
    """Backup file exists and can be opened independently without -wal/-shm sidecars.

    Spec scenario: 'Backup produces an openable consistent snapshot'
    """
    db_path = _make_wal_db(tmp_path / "source.db")
    output_path = tmp_path / "snap.db"

    result = backup_database(db_path, output_path=output_path)

    # Result path exists and is absolute
    assert result.exists()
    assert result.is_absolute()

    # No sidecar files — snapshot is self-contained
    assert not (tmp_path / "snap.db-wal").exists()
    assert not (tmp_path / "snap.db-shm").exists()

    # Data is readable from the backup without touching the source
    conn = sqlite3.connect(str(result))
    try:
        rows = conn.execute("SELECT label FROM items;").fetchall()
    finally:
        conn.close()

    assert rows == [("hello",)]


# ---------------------------------------------------------------------------
# Scenario 2: Default output anchors to DB parent directory, not cwd
# ---------------------------------------------------------------------------


def test_default_output_anchors_to_db_parent_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default backup path is under the source DB's parent, regardless of cwd.

    Spec scenario: 'Default output anchors to the database directory regardless of cwd'
    """
    db_dir = tmp_path / "db_home"
    db_dir.mkdir()
    db_path = _make_wal_db(db_dir / "source.db")

    # Change cwd to an unrelated directory
    unrelated_dir = tmp_path / "unrelated"
    unrelated_dir.mkdir()
    monkeypatch.chdir(unrelated_dir)

    result = backup_database(db_path)  # no output_path

    # Must be inside <db_dir>/backups/, not under unrelated_dir
    assert result.is_relative_to(db_dir / "backups")
    assert not result.is_relative_to(unrelated_dir)
    assert result.exists()


# ---------------------------------------------------------------------------
# Scenario 3: Refuses to overwrite an existing output path
# ---------------------------------------------------------------------------


def test_backup_raises_file_exists_error_when_output_exists(tmp_path: Path) -> None:
    """backup_database raises FileExistsError if the output path already exists.

    Spec scenario: 'Refuses to overwrite an existing output or back up onto itself'
    """
    db_path = _make_wal_db(tmp_path / "source.db")
    existing_output = tmp_path / "existing.db"
    existing_output.write_bytes(b"placeholder")

    with pytest.raises(FileExistsError):
        backup_database(db_path, output_path=existing_output)

    # Placeholder must remain unmodified (no clobber)
    assert existing_output.read_bytes() == b"placeholder"


# ---------------------------------------------------------------------------
# Scenario 4: Refuses when output path equals source path
# ---------------------------------------------------------------------------


def test_backup_raises_value_error_when_output_equals_source(tmp_path: Path) -> None:
    """backup_database raises ValueError if output path is the same as source.

    Spec scenario: 'Refuses to overwrite an existing output or back up onto itself'
    """
    db_path = _make_wal_db(tmp_path / "source.db")

    with pytest.raises(ValueError):
        backup_database(db_path, output_path=db_path)


# ---------------------------------------------------------------------------
# Scenario 5: Source DB does not exist — backup raises FileNotFoundError
# ---------------------------------------------------------------------------


def test_backup_raises_file_not_found_for_missing_source(tmp_path: Path) -> None:
    """backup_database raises FileNotFoundError when source does not exist.

    Spec scenario: 'Source database cannot be opened'
    """
    nonexistent = tmp_path / "no_such.db"

    with pytest.raises(FileNotFoundError):
        backup_database(nonexistent)


# ---------------------------------------------------------------------------
# Scenario 6: check_database on a healthy DB — passed=True, all keys present
# ---------------------------------------------------------------------------


def test_check_database_healthy_db_passes(tmp_path: Path) -> None:
    """check_database returns passed=True for a healthy WAL-mode database.

    Spec scenario: 'Healthy database passes'
    """
    db_path = _make_wal_db(tmp_path / "healthy.db")

    result = check_database(db_path)

    # All required keys must be present
    assert "db_path" in result
    assert "journal_mode" in result
    assert "integrity_check" in result
    assert "quick_check" in result
    assert "foreign_key_check" in result
    assert "passed" in result

    # Health verdict
    assert result["passed"] is True
    assert result["journal_mode"] == "wal"
    assert result["integrity_check"] == ["ok"]
    assert result["quick_check"] == ["ok"]
    assert result["foreign_key_check"] == []


# ---------------------------------------------------------------------------
# Scenario 7: check_database on a corrupted DB — passed=False, all keys present
# ---------------------------------------------------------------------------


def test_check_database_corrupted_db_fails_with_full_report(tmp_path: Path) -> None:
    """check_database returns passed=False for a corrupted DB; all keys still present.

    Uses a FK violation to reliably trigger foreign_key_check without touching
    raw bytes (byte-level corruption may be silently masked by sqlite3 on some
    platforms; FK violation is always detectable).

    Spec scenario: 'Corrupted database fails with full report'
    """
    db_path = tmp_path / "fk_violation.db"

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY);")
        conn.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER,"
            " FOREIGN KEY (parent_id) REFERENCES parent(id));"
        )
        # Insert child row that references a non-existent parent — FK violation
        conn.execute("INSERT INTO child (parent_id) VALUES (999);")
        conn.commit()
    finally:
        conn.close()

    result = check_database(db_path)

    # All four keys must still be present (no abort on first failing check)
    assert "db_path" in result
    assert "journal_mode" in result
    assert "integrity_check" in result
    assert "quick_check" in result
    assert "foreign_key_check" in result
    assert "passed" in result

    # FK violation detected
    assert result["passed"] is False
    assert len(result["foreign_key_check"]) > 0


# ---------------------------------------------------------------------------
# Scenario 8: check_database on a non-existent DB — raises FileNotFoundError
# ---------------------------------------------------------------------------


def test_check_database_raises_file_not_found_for_missing_db(tmp_path: Path) -> None:
    """check_database raises FileNotFoundError when the database does not exist.

    Spec scenario: 'Database does not exist'
    """
    nonexistent = tmp_path / "phantom.db"

    with pytest.raises(FileNotFoundError):
        check_database(nonexistent)


# ---------------------------------------------------------------------------
# Scenario 9: check_database on a malformed file — raises sqlite3.Error
# ---------------------------------------------------------------------------


def test_check_database_raises_sqlite_error_for_malformed_db(tmp_path: Path) -> None:
    """check_database raises sqlite3.Error when the file has an invalid SQLite header.

    A file containing garbage bytes passes the exists() guard but causes SQLite
    PRAGMA execution to raise sqlite3.DatabaseError (a subclass of sqlite3.Error).
    """
    malformed = tmp_path / "malformed.db"
    # Write arbitrary bytes that are not a valid SQLite3 header (which starts with
    # "SQLite format 3\000"). SQLite will raise DatabaseError on first PRAGMA access.
    malformed.write_bytes(b"this is not a valid sqlite database file\x00\xff\xfe")

    with pytest.raises(sqlite3.Error):
        check_database(malformed)


# ---------------------------------------------------------------------------
# Scenario 10: backup_database on a malformed source — raises sqlite3.Error
# ---------------------------------------------------------------------------


def test_backup_raises_sqlite_error_for_malformed_source(tmp_path: Path) -> None:
    """backup_database raises sqlite3.Error when the source file is not valid SQLite.

    sqlite3.Connection.backup() raises sqlite3.DatabaseError for malformed sources.
    """
    malformed = tmp_path / "malformed_src.db"
    malformed.write_bytes(b"not a sqlite file\x00\xff\xfe")
    output_path = tmp_path / "output.db"

    with pytest.raises(sqlite3.Error):
        backup_database(malformed, output_path=output_path)

    # Output must not be created — no partial backup
    assert not output_path.exists()

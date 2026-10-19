"""Tests for WAL journal mode in initialize_database."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mailattic import storage
from mailattic.storage import initialize_database


def test_fresh_db_has_wal_journal_mode(tmp_path: Path) -> None:
    """A newly created database has journal_mode=wal after initialize_database."""
    db_path = tmp_path / "test.db"
    initialize_database(db_path)

    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    finally:
        conn.close()

    assert mode == "wal"


def test_existing_delete_mode_db_upgrades_to_wal(tmp_path: Path) -> None:
    """A pre-existing DB with DELETE journal mode is upgraded to WAL without error."""
    db_path = tmp_path / "legacy.db"

    # Create a DB with default DELETE journal mode
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY);")
        conn.commit()
        mode_before = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    finally:
        conn.close()

    assert mode_before == "delete"

    # initialize_database should upgrade to WAL without raising
    initialize_database(db_path)

    conn = sqlite3.connect(db_path)
    try:
        mode_after = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    finally:
        conn.close()

    assert mode_after == "wal"


def test_wal_refused_raises_runtime_error(tmp_path: Path) -> None:
    """If SQLite silently falls back to non-WAL mode, initialize_database raises."""
    mock_conn = MagicMock(spec=sqlite3.Connection)
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ("delete",)
    mock_conn.execute.return_value = mock_cursor

    with (
        patch.object(storage, "connect_database", return_value=mock_conn),
        pytest.raises(RuntimeError, match="refused journal_mode=WAL"),
    ):
        initialize_database(tmp_path / "refused.db")

    mock_conn.close.assert_called_once()

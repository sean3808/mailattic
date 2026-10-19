"""Tests for the three-layer database path fallback in bes CLI.

Priority: --database flag > MAILATTIC_DB env var > hardcoded absolute default.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from mailattic.cli import main
from mailattic.storage import (
    DEFAULT_DB_NAME,
    ENV_VAR_DB,
    resolve_database_default,
)


def test_flag_wins_over_env_var(tmp_path: Path) -> None:
    """--database flag takes priority over MAILATTIC_DB env var."""
    flag_db = tmp_path / "flag.db"
    env_db = tmp_path / "env.db"
    # init via flag path
    with patch.dict(os.environ, {ENV_VAR_DB: str(env_db)}):
        exit_code = main(["init-db", "--database", str(flag_db)])
    assert exit_code == 0
    assert flag_db.exists()
    assert not env_db.exists()


def test_env_var_wins_over_absolute_default(tmp_path: Path) -> None:
    """MAILATTIC_DB env var takes priority over hardcoded absolute default."""
    env_db = tmp_path / "env_chosen.db"
    with patch.dict(os.environ, {ENV_VAR_DB: str(env_db)}, clear=False):
        resolved = resolve_database_default()
    assert resolved == env_db


def test_empty_env_var_falls_to_absolute_default() -> None:
    """Empty string MAILATTIC_DB falls back to hardcoded absolute default."""
    with patch.dict(os.environ, {ENV_VAR_DB: ""}, clear=False):
        resolved = resolve_database_default()
    assert resolved == Path(DEFAULT_DB_NAME)


def test_whitespace_only_env_var_falls_to_absolute_default() -> None:
    """Whitespace-only MAILATTIC_DB is treated as unset."""
    with patch.dict(os.environ, {ENV_VAR_DB: "   "}, clear=False):
        resolved = resolve_database_default()
    assert resolved == Path(DEFAULT_DB_NAME)


def test_unset_env_var_falls_to_absolute_default() -> None:
    """Absent MAILATTIC_DB falls back to hardcoded absolute default."""
    env = {k: v for k, v in os.environ.items() if k != ENV_VAR_DB}
    with patch.dict(os.environ, env, clear=True):
        resolved = resolve_database_default()
    assert resolved == Path(DEFAULT_DB_NAME)

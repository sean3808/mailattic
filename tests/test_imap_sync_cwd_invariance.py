"""Cwd-invariance unit tests for bes sync-imap default paths.

These tests do NOT require an IMAP connection or any sync execution. They
guard the invariant that all sync-related default path constants resolve
to absolute locations under the project repository root, regardless of
the caller's current working directory.

Failure of any test here means a future PR has reintroduced a
cwd-relative default that will break cross-cwd subagent invocations of
bes sync-imap (see openspec change `imap-env-file-absolute-path`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mailattic.imap_sync import (
    DEFAULT_COMPOSE_FILE,
    DEFAULT_DATABASE_PATH,
    DEFAULT_ENV_FILE,
    DEFAULT_LOG_DIR,
    DEFAULT_MAILDIR_ROOT,
)
from mailattic.storage import DEFAULT_DB_NAME

# Literal (not Path(__file__).parents[1]): this is the test oracle for the
# expected absolute anchor. Deriving it from __file__ would make the test
# pass even if the source constants reverted to relative paths.
REPO_ROOT = Path(r"C:\IT_Projects\mailattic")

DEFAULT_PATHS: dict[str, Path] = {
    "imap_sync.DEFAULT_COMPOSE_FILE": DEFAULT_COMPOSE_FILE,
    "imap_sync.DEFAULT_ENV_FILE": DEFAULT_ENV_FILE,
    "imap_sync.DEFAULT_MAILDIR_ROOT": DEFAULT_MAILDIR_ROOT,
    "imap_sync.DEFAULT_DATABASE_PATH": DEFAULT_DATABASE_PATH,
    "imap_sync.DEFAULT_LOG_DIR": DEFAULT_LOG_DIR,
    "storage.DEFAULT_DB_NAME": Path(DEFAULT_DB_NAME),
}


@pytest.mark.parametrize("name,path", list(DEFAULT_PATHS.items()))
def test_default_is_absolute(name: str, path: Path) -> None:
    assert path.is_absolute(), f"{name} = {path!r} is not absolute"


@pytest.mark.parametrize("name,path", list(DEFAULT_PATHS.items()))
def test_default_anchored_at_repo_root(name: str, path: Path) -> None:
    assert path.is_relative_to(REPO_ROOT), (
        f"{name} = {path!r} is not anchored under {REPO_ROOT}"
    )


@pytest.mark.parametrize("name,path", list(DEFAULT_PATHS.items()))
def test_default_resolve_unaffected_by_cwd(
    name: str, path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = path.expanduser().resolve()
    assert not resolved.is_relative_to(tmp_path), (
        f"{name} resolved against tmp cwd {tmp_path}: {resolved!r}"
    )
    assert resolved.is_relative_to(REPO_ROOT), (
        f"{name} resolved to {resolved!r}, not under {REPO_ROOT}"
    )

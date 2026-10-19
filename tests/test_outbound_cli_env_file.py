"""Tests for the SMTP settings file path resolution (issue #199).

Priority: --env-file flag > MAILATTIC_SMTP_ENV_FILE env var > built-in absolute
default -- the same three-layer shape as the --database fallback
(storage.resolve_database_default, tests/test_cli_db_fallback.py).

All three commands -- draft-reply, send-draft, send-batch -- refuse before
any DB/network I/O when the resolved file does not exist: a silent fallback
identity for draft-reply would reintroduce exactly the "invisible sender
default" #113 guarded against. Only an existing-but-unparseable file (e.g.
missing keys) keeps draft-reply's pre-existing fallback to a default
own_address; see tests/test_outbound_draft.py and friends, which use an
existing-but-empty placeholder --env-file for scenarios unrelated to SMTP
config resolution.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from mailattic.cli import build_parser, main
from mailattic.outbound import (
    DEFAULT_SMTP_ENV_FILE,
    ENV_VAR_SMTP_ENV_FILE,
    resolve_smtp_env_file,
)

_REQUIRED_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")


# ---------------------------------------------------------------------------
# resolve_smtp_env_file: three-layer precedence (unit level, no file I/O --
# these never read a real file, only compare resolved Path values)
# ---------------------------------------------------------------------------


def test_flag_wins_over_env_var(tmp_path: Path) -> None:
    flag_path = tmp_path / "flag.env"
    env_path = tmp_path / "env.env"
    with patch.dict(os.environ, {ENV_VAR_SMTP_ENV_FILE: str(env_path)}):
        resolved = resolve_smtp_env_file(flag_path)
    assert resolved == flag_path


def test_env_var_wins_over_default_when_no_flag(tmp_path: Path) -> None:
    env_path = tmp_path / "env.env"
    with patch.dict(os.environ, {ENV_VAR_SMTP_ENV_FILE: str(env_path)}):
        resolved = resolve_smtp_env_file(None)
    assert resolved == env_path


def test_empty_env_var_falls_to_default() -> None:
    with patch.dict(os.environ, {ENV_VAR_SMTP_ENV_FILE: ""}):
        resolved = resolve_smtp_env_file(None)
    assert resolved == Path(DEFAULT_SMTP_ENV_FILE)


def test_whitespace_only_env_var_falls_to_default() -> None:
    with patch.dict(os.environ, {ENV_VAR_SMTP_ENV_FILE: "   "}):
        resolved = resolve_smtp_env_file(None)
    assert resolved == Path(DEFAULT_SMTP_ENV_FILE)


def test_unset_env_var_falls_to_default() -> None:
    env = {k: v for k, v in os.environ.items() if k != ENV_VAR_SMTP_ENV_FILE}
    with patch.dict(os.environ, env, clear=True):
        resolved = resolve_smtp_env_file(None)
    assert resolved == Path(DEFAULT_SMTP_ENV_FILE)


def test_production_default_points_at_repo_root_env_file() -> None:
    """String/Path comparison only -- must never read the real file's contents."""
    assert Path(DEFAULT_SMTP_ENV_FILE) == Path(
        r"C:\IT_Projects\mailattic\.env.sean3808"
    )


def test_mail_server_env_path_is_never_read_for_smtp(tmp_path: Path) -> None:
    """#113: MAIL_SERVER_ENV_PATH (archive-scrape credentials) must never
    become the SMTP settings source."""
    env = {k: v for k, v in os.environ.items() if k != ENV_VAR_SMTP_ENV_FILE}
    env["MAIL_SERVER_ENV_PATH"] = str(tmp_path / "archive.env")
    with patch.dict(os.environ, env, clear=True):
        resolved = resolve_smtp_env_file(None)
    assert resolved == Path(DEFAULT_SMTP_ENV_FILE)


# PC-096 paired check (manually verified during implementation by reverting
# each branch in turn -- see the implementation report for the red/green
# observations):
#   (a) drop the `explicit_path is not None` branch -> only
#       test_flag_wins_over_env_var goes red.
#   (b) drop the env-var branch -> only test_env_var_wins_over_default_when_no_flag
#       and test_mail_server_env_path_is_never_read_for_smtp go red.
#   (c) drop the `.strip()` blank check -> only test_empty_env_var_falls_to_default
#       and test_whitespace_only_env_var_falls_to_default go red.


# ---------------------------------------------------------------------------
# draft-reply / send-draft / send-batch: resolved file must exist before any
# DB/network I/O
# ---------------------------------------------------------------------------

_COMMANDS = ["draft-reply", "send-draft", "send-batch"]


def _command_required_args(command: str, tmp_path: Path) -> list[str]:
    """Positional/required args each command needs, independent of --env-file.

    None of these are ever read for the missing-file scenarios below -- the
    handler refuses before draft-reply would open --body-from or any command
    would touch the database -- so a nonexistent placeholder path is fine.
    """
    if command == "draft-reply":
        return [
            "<missing-env-file-test@mailattic.test>",
            "--body-from",
            str(tmp_path / "body.txt"),
        ]
    return ["draft-id"]


@pytest.mark.parametrize("command", _COMMANDS)
def test_missing_resolved_env_file_exits_before_io(
    command: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_env_file = tmp_path / "missing.env"
    database_path = tmp_path / "unused.db"

    exit_code = main(
        [
            command,
            *_command_required_args(command, tmp_path),
            "--database",
            str(database_path),
            "--env-file",
            str(missing_env_file),
        ]
    )

    assert exit_code == 2
    err = capsys.readouterr().err
    assert str(missing_env_file) in err
    assert "--env-file" in err
    assert ENV_VAR_SMTP_ENV_FILE in err
    assert DEFAULT_SMTP_ENV_FILE in err
    for key in _REQUIRED_KEYS:
        assert key in err
    # Refused before any DB or draft I/O.
    assert not database_path.exists()
    assert not (tmp_path / "outbound").exists()


@pytest.mark.parametrize("command", _COMMANDS)
def test_missing_env_var_and_default_exits_before_io(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --env-file, no env var, and the (monkeypatched) default is absent."""
    monkeypatch.delenv(ENV_VAR_SMTP_ENV_FILE, raising=False)
    monkeypatch.delenv("MAIL_SERVER_ENV_PATH", raising=False)
    default_path = tmp_path / "default.env"
    monkeypatch.setattr("mailattic.outbound.DEFAULT_SMTP_ENV_FILE", str(default_path))
    monkeypatch.setattr(
        "mailattic.outbound_commands.DEFAULT_SMTP_ENV_FILE", str(default_path)
    )
    database_path = tmp_path / "unused.db"

    exit_code = main(
        [
            command,
            *_command_required_args(command, tmp_path),
            "--database",
            str(database_path),
        ]
    )

    assert exit_code == 2
    err = capsys.readouterr().err
    assert str(default_path) in err
    assert "--env-file" in err
    assert ENV_VAR_SMTP_ENV_FILE in err
    assert not database_path.exists()


def test_existing_unparseable_env_file_keeps_generic_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A file that exists but lacks required keys is a different failure mode
    from a missing file, and keeps the pre-existing generic message."""
    bad_env = tmp_path / "bad.env"
    bad_env.write_text("SOMETHING=else\n", encoding="utf-8")

    exit_code = main(
        [
            "send-draft",
            "draft-id",
            "--database",
            str(tmp_path / "unused.db"),
            "--env-file",
            str(bad_env),
        ]
    )

    assert exit_code == 2
    assert (
        capsys.readouterr().err.strip()
        == "Could not load SMTP config; check --env-file"
    )


# ---------------------------------------------------------------------------
# --help text: names the env var and default instead of claiming no default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["draft-reply", "send-draft", "send-batch"])
def test_env_file_help_names_env_var_default_and_required_keys(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args([command, "--help"])

    # argparse wraps --help text at column width, which can split a long path
    # mid-word at a hyphen -- strip ALL whitespace before substring checks
    # (a single space would not reunite a hyphen-split word).
    raw_help_text = capsys.readouterr().out
    help_text = "".join(raw_help_text.split())
    assert "thereisnodefault" not in help_text
    assert ENV_VAR_SMTP_ENV_FILE in help_text
    assert "".join(DEFAULT_SMTP_ENV_FILE.split()) in help_text
    for key in _REQUIRED_KEYS:
        assert key in help_text

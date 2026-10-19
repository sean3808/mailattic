"""The skill consumes the released CLI even outside the platform checkout."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "skills/mailattic/scripts/ball-in-court.py"
)


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("explicit_database", [False, True])
@pytest.mark.parametrize(
    ("direction", "body", "expected"),
    [
        ("inbound", "請報價?", "[BALL-IN-COURT]"),
        ("inbound", "Thank you", "[likely-handled]"),
        ("outbound", "Please confirm", "[our-side]"),
    ],
)
def test_skill_uses_global_release_from_other_cwd(
    tmp_path: Path,
    single: bool,
    explicit_database: bool,
    direction: str,
    body: str,
    expected: str,
):
    released = tmp_path / "released bin"
    released.mkdir()
    message = {
        "id": 42,
        "sent_at": "2026-09-16T01:00:00",
        "sender_email": "released@example.com",
        "direction": direction,
        "body_text": body,
    }
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(message if single else [message]), encoding="utf-8")
    log = tmp_path / "args.txt"
    (released / "bes.cmd").write_text(
        f'@echo off\necho %* > "{log}"\ntype "{payload}"\n', encoding="utf-8"
    )
    # uv run can prepend a working-tree virtualenv. It must not supply BES.
    (tmp_path / "bes.cmd").write_text("@echo off\necho UNRELEASED\nexit /b 9\n")
    (tmp_path / "uv.cmd").write_text(f"@echo off\necho {released}\n")
    poison = tmp_path / "mailattic"
    poison.mkdir()
    (poison / "__init__.py").write_text("raise RuntimeError('working tree imported')")
    env = {
        **os.environ,
        "PATH": f"{tmp_path};{os.environ['PATH']}",
        "PYTHONPATH": str(tmp_path),
        "PYTHONIOENCODING": "utf-8",
    }
    args = [
        sys.executable,
        str(SCRIPT),
        "--seed-db-id",
        "42",
    ]
    if explicit_database:
        args.extend(["--database", str(tmp_path / "isolated.db")])
    if single:
        args.append("--no-thread")

    result = subprocess.run(
        args, cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8"
    )

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert "released@example.com" in result.stdout
    command = log.read_text()
    assert ("show 42" if single else "thread 42") in command
    assert "--format json" in command
    if explicit_database:
        assert str(tmp_path / "isolated.db") in command
    else:
        assert "--database" not in command


@pytest.mark.parametrize(
    ("response", "code", "error"),
    [
        ("[]", 0, "thread empty"),
        ("not JSON", 0, "Expecting value"),
        ("", 2, "BES failed"),
    ],
)
def test_skill_reports_cli_failures(
    tmp_path: Path, response: str, code: int, error: str
):
    (tmp_path / "uv.cmd").write_text(f"@echo off\necho {tmp_path}\n")
    (tmp_path / "bes.cmd").write_text(
        f"@echo off\necho {response or 'BES failed 1>&2'}\nexit /b {code}\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--seed-db-id", "42"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path};{os.environ['PATH']}",
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode != 0
    assert error in result.stderr
    assert "[BALL-IN-COURT]" not in result.stdout

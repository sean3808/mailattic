from __future__ import annotations

import subprocess
from pathlib import Path

from mailattic.imap_runtime import run_logged_sync_job

REPO_ROOT = Path(__file__).resolve().parents[1]


def _runtime_env() -> dict[str, str]:
    return {
        "IMAP_HOST": "mail.mailattic.test",
        "IMAP_PORT": "993",
        "IMAP_USER": "sean3808@mailattic.test",
        "IMAP_PASSWORD": "placeholder",
        "IMAP_ACCOUNT_NAME": "sean3808",
    }


def test_connection_failure_exits_non_zero_and_skips_ingest(tmp_path: Path) -> None:
    database_path = tmp_path / "evidence.db"
    result = run_logged_sync_job(
        database_path=database_path,
        maildir_root=tmp_path / "maildir",
        template_path=REPO_ROOT / "docker" / "mbsyncrc.template",
        config_path=tmp_path / "mbsyncrc",
        log_dir=tmp_path / "logs",
        env=_runtime_env(),
        command_runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            returncode=1,
            stdout="",
            stderr="Socket error: connection refused",
        ),
    )

    assert result.exit_code == 1
    assert result.failure_stage == "connection"
    assert not database_path.exists()
    log_text = next((tmp_path / "logs").glob("mbsync-*.log")).read_text(
        encoding="utf-8"
    )
    assert "stage=connection" in log_text
    assert "exit=1" in log_text


def test_auth_failure_exits_non_zero_and_skips_ingest(tmp_path: Path) -> None:
    database_path = tmp_path / "evidence.db"
    result = run_logged_sync_job(
        database_path=database_path,
        maildir_root=tmp_path / "maildir",
        template_path=REPO_ROOT / "docker" / "mbsyncrc.template",
        config_path=tmp_path / "mbsyncrc",
        log_dir=tmp_path / "logs",
        env=_runtime_env(),
        command_runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            returncode=1,
            stdout="",
            stderr="AUTHENTICATE failed",
        ),
    )

    assert result.exit_code == 1
    assert result.failure_stage == "auth"
    assert not database_path.exists()
    log_text = next((tmp_path / "logs").glob("mbsync-*.log")).read_text(
        encoding="utf-8"
    )
    assert "stage=auth" in log_text
    assert "exit=1" in log_text


def test_maildir_failure_exits_non_zero_and_skips_ingest(tmp_path: Path) -> None:
    database_path = tmp_path / "evidence.db"
    result = run_logged_sync_job(
        database_path=database_path,
        maildir_root=tmp_path / "maildir",
        template_path=REPO_ROOT / "docker" / "mbsyncrc.template",
        config_path=tmp_path / "mbsyncrc",
        log_dir=tmp_path / "logs",
        env=_runtime_env(),
        command_runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            returncode=1,
            stdout="",
            stderr="Maildir write failed: No space left on device",
        ),
    )

    assert result.exit_code == 1
    assert result.failure_stage == "maildir"
    assert not database_path.exists()
    log_text = next((tmp_path / "logs").glob("mbsync-*.log")).read_text(
        encoding="utf-8"
    )
    assert "stage=maildir" in log_text
    assert "exit=1" in log_text

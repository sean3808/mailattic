from __future__ import annotations

import importlib.util
import pathlib
from pathlib import Path

from mailattic.attachment_store import resolve_store_root
from mailattic.imap_runtime import SyncExecutionResult


def test_docker_run_sync_no_attachment_root_uses_default(monkeypatch) -> None:
    module = load_docker_run_sync()
    captured: dict[str, object] = {}

    def fake_run_logged_sync_job(**kwargs) -> SyncExecutionResult:
        captured.update(kwargs)
        return _successful_result()

    monkeypatch.setattr(module, "run_logged_sync_job", fake_run_logged_sync_job)

    assert (
        module.main(
            [
                "--database",
                "/db/mail.db",
                "--maildir-root",
                "/maildir",
                "--template-path",
                "/t.j2",
                "--config-path",
                "/c.conf",
                "--log-dir",
                "/logs",
            ]
        )
        == 0
    )
    assert captured["attachment_root"] == resolve_store_root(
        Path("/db/mail.db"),
        cli_override=None,
    )


def test_docker_run_sync_explicit_attachment_root(monkeypatch) -> None:
    module = load_docker_run_sync()
    captured: dict[str, object] = {}

    def fake_run_logged_sync_job(**kwargs) -> SyncExecutionResult:
        captured.update(kwargs)
        return _successful_result()

    monkeypatch.setattr(module, "run_logged_sync_job", fake_run_logged_sync_job)

    assert (
        module.main(
            [
                "--database",
                "/db/mail.db",
                "--maildir-root",
                "/maildir",
                "--template-path",
                "/t.j2",
                "--config-path",
                "/c.conf",
                "--log-dir",
                "/logs",
                "--attachment-root",
                "/tmp/foo",
            ]
        )
        == 0
    )
    assert captured["attachment_root"] == resolve_store_root(
        Path("/db/mail.db"),
        cli_override=Path("/tmp/foo"),
    )


def load_docker_run_sync():
    path = pathlib.Path(__file__).parent.parent / "docker" / "run_sync.py"
    spec = importlib.util.spec_from_file_location("docker_run_sync", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _successful_result() -> SyncExecutionResult:
    return SyncExecutionResult(
        pulled_messages=0,
        folder_count=0,
        candidate_messages=0,
        inserted_messages=0,
        deduplicated_messages=0,
        failure_count=0,
        exit_code=0,
    )

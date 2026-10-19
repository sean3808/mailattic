from __future__ import annotations

from pathlib import Path

from mailattic.attachment_store import resolve_store_root
from mailattic.cli import _handle_sync_imap, build_parser
from mailattic.imap_runtime import SyncExecutionResult


def test_sync_imap_attachment_root_flag_passes_through(monkeypatch) -> None:
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    captured: dict[str, object] = {}

    def fake_run_sync_imap(**kwargs) -> SyncExecutionResult:
        captured.update(kwargs)
        return _successful_result()

    monkeypatch.setattr("mailattic.cli.run_sync_imap", fake_run_sync_imap)
    args = build_parser().parse_args(
        ["sync-imap", "--database", "mydb.db", "--attachment-root", "custom/"]
    )

    assert args.attachment_root == Path("custom/")
    assert _handle_sync_imap(args) == 0
    assert captured["attachment_root"] == resolve_store_root(
        Path("mydb.db").resolve(),
        cli_override=Path("custom/"),
        env_override=None,
    )


def test_sync_imap_no_flag_uses_default_resolution(monkeypatch) -> None:
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    captured: dict[str, object] = {}

    def fake_run_sync_imap(**kwargs) -> SyncExecutionResult:
        captured.update(kwargs)
        return _successful_result()

    monkeypatch.setattr("mailattic.cli.run_sync_imap", fake_run_sync_imap)
    args = build_parser().parse_args(["sync-imap", "--database", "mydb.db"])

    assert args.attachment_root is None
    assert _handle_sync_imap(args) == 0
    assert captured["attachment_root"] == resolve_store_root(
        Path("mydb.db").resolve(),
        cli_override=None,
        env_override=None,
    )
    expected_default = Path("mydb.db").resolve().parent / "attachments"
    assert captured["attachment_root"] == expected_default


def test_sync_imap_partial_success_prints_partial_not_skipped(
    monkeypatch, capsys
) -> None:
    """Regression: exit_code=1 with inserted>0 must print PARTIAL, not SKIPPED."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)

    def fake_run_sync_imap(**kwargs) -> SyncExecutionResult:
        return SyncExecutionResult(
            pulled_messages=5,
            folder_count=3,
            candidate_messages=5,
            inserted_messages=4,
            deduplicated_messages=1,
            failure_count=1,
            exit_code=1,
            failure_stage="folder",
            failure_message="folder INBOX.Sent failed",
        )

    monkeypatch.setattr("mailattic.cli.run_sync_imap", fake_run_sync_imap)
    args = build_parser().parse_args(["sync-imap", "--database", "mydb.db"])

    exit_code = _handle_sync_imap(args)

    assert exit_code == 1
    captured = capsys.readouterr().out
    assert "PARTIAL" in captured
    assert "SKIPPED" not in captured
    assert "4 inserted" in captured
    assert "1 deduped" in captured
    assert "1 folders failed" in captured


def test_sync_imap_total_failure_still_prints_skipped(monkeypatch, capsys) -> None:
    """When exit_code=1 and nothing was inserted/deduped, SKIPPED is correct."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)

    def fake_run_sync_imap(**kwargs) -> SyncExecutionResult:
        return SyncExecutionResult(
            pulled_messages=0,
            folder_count=0,
            candidate_messages=0,
            inserted_messages=0,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=1,
            failure_stage="sync",
            failure_message="mbsync exited non-zero",
        )

    monkeypatch.setattr("mailattic.cli.run_sync_imap", fake_run_sync_imap)
    args = build_parser().parse_args(["sync-imap", "--database", "mydb.db"])

    exit_code = _handle_sync_imap(args)

    assert exit_code == 1
    captured = capsys.readouterr().out
    assert "SKIPPED" in captured
    assert "PARTIAL" not in captured


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

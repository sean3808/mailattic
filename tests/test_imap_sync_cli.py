from __future__ import annotations

from pathlib import Path

from mailattic.cli import main
from mailattic.imap_native import ImapFolder
from mailattic.imap_runtime import SyncExecutionResult


def test_sync_imap_basic(tmp_path: Path, monkeypatch, capsys) -> None:
    env_file = _write_env_file(tmp_path)

    monkeypatch.setattr(
        "mailattic.imap_sync.sync_native",
        lambda **kwargs: SyncExecutionResult(
            pulled_messages=4,
            folder_count=2,
            candidate_messages=4,
            inserted_messages=3,
            deduplicated_messages=1,
            failure_count=0,
            exit_code=0,
        ),
    )
    exit_code = main(
        [
            "sync-imap",
            "--database",
            str(tmp_path / "mailattic.db"),
            "--env-file",
            str(env_file),
            "--maildir-root",
            str(tmp_path / ".mailattic-work" / "maildir"),
            "--log-dir",
            str(tmp_path / ".mailattic-work" / "logs"),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "pulled 4 new messages from 2 folders" in captured
    assert "ingest: 4 candidates → 3 inserted, 1 deduped" in captured
    assert "exit code: 0" in captured


def test_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    env_file = _write_env_file(tmp_path)
    maildir_root = tmp_path / ".mailattic-work" / "maildir"

    class FakeImapClient:
        def __init__(self, env) -> None:
            self.selected: str | None = None

        def list_folders(self) -> list[ImapFolder]:
            return [ImapFolder(display_name="Sent", wire_name="Sent", delimiter="/")]

        def get_uidvalidity(self, folder_name: str) -> int:
            assert folder_name == "Sent"
            return 1

        def select_folder(self, folder_name: str) -> None:
            assert folder_name == "Sent"
            self.selected = folder_name

        def search_uids_after(self, watermark: int | None) -> list[int]:
            assert self.selected == "Sent"
            assert watermark is None
            return [1, 2, 3, 4]

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "mailattic.imap_sync.NativeImapClient",
        FakeImapClient,
    )
    database_path = tmp_path / "evidence.db"
    exit_code = main(
        [
            "sync-imap",
            "--dry-run",
            "--folder",
            "Sent",
            "--database",
            str(database_path),
            "--env-file",
            str(env_file),
            "--maildir-root",
            str(maildir_root),
        ]
    )

    assert exit_code == 0
    assert not database_path.exists()
    captured = capsys.readouterr().out
    assert "dry-run estimated 4 new messages from 1 folders" in captured
    assert "ingest: SKIPPED (dry-run)" in captured


def test_dry_run_bulk_guard_warning(tmp_path: Path, monkeypatch, capsys) -> None:
    env_file = _write_env_file(tmp_path)

    class FakeImapClient:
        def __init__(self, env) -> None:
            self.selected: str | None = None

        def list_folders(self) -> list[ImapFolder]:
            return [ImapFolder(display_name="INBOX", wire_name="INBOX", delimiter="/")]

        def get_uidvalidity(self, folder_name: str) -> int:
            assert folder_name == "INBOX"
            return 1

        def select_folder(self, folder_name: str) -> None:
            self.selected = folder_name

        def search_uids_after(self, watermark: int | None) -> list[int]:
            assert self.selected == "INBOX"
            assert watermark is None
            return list(range(1, 502))

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "mailattic.imap_sync.NativeImapClient",
        FakeImapClient,
    )
    exit_code = main(
        [
            "sync-imap",
            "--dry-run",
            "--database",
            str(tmp_path / "test.db"),
            "--env-file",
            str(env_file),
            "--maildir-root",
            str(tmp_path / ".mailattic-work" / "maildir"),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "WARNING" in captured
    assert "INBOX" in captured
    assert "--full-resync" in captured


def test_folder_scope(tmp_path: Path, monkeypatch, capsys) -> None:
    env_file = _write_env_file(tmp_path)
    captured: dict[str, object] = {}

    def fake_native(**kwargs):
        captured.update(kwargs)
        return SyncExecutionResult(
            pulled_messages=1,
            folder_count=1,
            candidate_messages=1,
            inserted_messages=1,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=0,
        )

    monkeypatch.setattr("mailattic.imap_sync.sync_native", fake_native)
    exit_code = main(
        [
            "sync-imap",
            "--env-file",
            str(env_file),
            "--folder",
            "Sent",
            "--maildir-root",
            str(tmp_path / ".mailattic-work" / "maildir"),
            "--log-dir",
            str(tmp_path / ".mailattic-work" / "logs"),
        ]
    )

    assert exit_code == 0
    assert captured.get("folder") == "Sent"
    assert "pulled 1 new messages from 1 folders" in capsys.readouterr().out


def test_full_resync_prompt(tmp_path: Path, monkeypatch, capsys) -> None:
    env_file = _write_env_file(tmp_path)
    maildir_root = tmp_path / ".mailattic-work" / "maildir"
    account_root = maildir_root / "sean3808"
    account_root.mkdir(parents=True, exist_ok=True)
    state_path = account_root / ".imap_watermarks.json"
    state_path.write_text('{"version":1,"watermarks":{}}', encoding="utf-8")

    # User says N → cancelled, state file preserved
    monkeypatch.setattr("builtins.input", lambda prompt: "N")
    exit_code = main(
        [
            "sync-imap",
            "--full-resync",
            "--database",
            str(tmp_path / "mailattic.db"),
            "--env-file",
            str(env_file),
            "--maildir-root",
            str(maildir_root),
            "--log-dir",
            str(tmp_path / ".mailattic-work" / "logs"),
        ]
    )
    assert exit_code == 0
    assert state_path.exists()
    assert "full resync cancelled" in capsys.readouterr().out

    # User says Y → state file deleted, sync proceeds
    monkeypatch.setattr("builtins.input", lambda prompt: "Y")
    monkeypatch.setattr(
        "mailattic.imap_sync.sync_native",
        lambda **kwargs: SyncExecutionResult(
            pulled_messages=0,
            folder_count=0,
            candidate_messages=0,
            inserted_messages=0,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=0,
        ),
    )
    exit_code = main(
        [
            "sync-imap",
            "--full-resync",
            "--database",
            str(tmp_path / "mailattic.db"),
            "--env-file",
            str(env_file),
            "--maildir-root",
            str(maildir_root),
            "--log-dir",
            str(tmp_path / ".mailattic-work" / "logs"),
        ]
    )

    assert exit_code == 0
    assert not state_path.exists()


def _write_env_file(project_root: Path) -> Path:
    env_file = project_root / ".env.sean3808"
    env_file.write_text(
        "\n".join(
            [
                "IMAP_HOST=mail.mailattic.test",
                "IMAP_PORT=993",
                "IMAP_USER=sean3808@mailattic.test",
                "IMAP_PASSWORD=placeholder",
                "IMAP_ACCOUNT_NAME=sean3808",
            ]
        ),
        encoding="utf-8",
    )
    return env_file

"""#223: sync progress must follow the DB, not a newer external watermark file."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mailattic.db_maintenance import backup_database
from mailattic.imap_native import ImapFolder, sync_native
from mailattic.imap_state import ImapSyncState
from mailattic.storage import connect_database, initialize_database

ENV = {"IMAP_ACCOUNT_NAME": "audit", "IMAP_USER": "audit@example.com"}


class _Client:
    def __init__(self, uids: list[int]) -> None:
        self.uids = uids

    def list_folders(self):
        return [ImapFolder("INBOX", "INBOX", "/")]

    def get_uidvalidity(self, folder):
        return 1

    def select_folder(self, folder):
        pass

    def search_uids_after(self, watermark):
        return [u for u in self.uids if watermark is None or u > watermark]

    def fetch_rfc822(self, uid):
        return (
            "From: source@example.com\r\nTo: audit@example.com\r\n"
            f"Subject: Audit UID {uid}\r\nMessage-ID: <audit-{uid}@example.com>\r\n"
            "Date: Fri, 2 Oct 2026 12:00:00 +0800\r\n"
            f"\r\nAudit body {uid}\r\n"
        ).encode()

    def close(self):
        pass


def _sync(root: Path, db: Path, uids: list[int], **kw):
    return sync_native(
        env=ENV,
        database_path=db,
        maildir_root=root / "maildir",
        state_path=root / "maildir" / "audit" / ".imap_watermarks.json",
        log_dir=root / "logs",
        client_factory=lambda _: _Client(uids),
        **kw,
    )


def _ids(db: Path) -> list[str]:
    with connect_database(db, read_only=True) as conn:
        return [
            r[0] for r in conn.execute("SELECT message_id FROM messages ORDER BY id")
        ]


def test_snapshot_restore_recovers_missing_uid(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    initialize_database(db)
    assert _sync(tmp_path, db, [1]).exit_code == 0
    snapshot = backup_database(db, tmp_path / "before-uid2.db")
    assert _sync(tmp_path, db, [1, 2]).exit_code == 0
    with sqlite3.connect(snapshot) as src, sqlite3.connect(db) as dst:
        src.backup(dst)
    assert _ids(db) == ["<audit-1@example.com>"]

    result = _sync(tmp_path, db, [1, 2])

    assert result.exit_code == 0
    assert result.inserted_messages == 1
    assert _ids(db) == ["<audit-1@example.com>", "<audit-2@example.com>"]


def test_second_database_does_not_inherit_watermark(tmp_path: Path) -> None:
    db1 = tmp_path / "one.db"
    db2 = tmp_path / "two.db"
    initialize_database(db1)
    initialize_database(db2)
    assert _sync(tmp_path, db1, [1, 2]).exit_code == 0

    result = _sync(tmp_path, db2, [1, 2])

    assert result.exit_code == 0
    assert result.inserted_messages == 2
    assert _ids(db2) == ["<audit-1@example.com>", "<audit-2@example.com>"]


def test_second_database_large_backlog_refuses_instead_of_skipping(
    tmp_path: Path,
) -> None:
    db1 = tmp_path / "one.db"
    db2 = tmp_path / "two.db"
    initialize_database(db1)
    initialize_database(db2)
    assert _sync(tmp_path, db1, [1, 2]).exit_code == 0

    result = _sync(tmp_path, db2, [1, 2], bulk_threshold=1)

    assert result.exit_code != 0
    assert result.failure_stage == "bulk_guard"
    assert "--full-resync" in (result.failure_message or "")


STATE = "maildir/audit/.imap_watermarks.json"


def _clear_progress(db: Path) -> None:
    with connect_database(db) as conn:
        conn.execute("DELETE FROM imap_sync_progress")
        conn.commit()


def _legacy_json(root: Path, uid: int) -> None:
    """Replace the mirror with a pre-#223 file: bare watermark, no DB binding."""
    path = root / STATE
    path.unlink(missing_ok=True)
    ImapSyncState(path).set_watermark("INBOX", "/", 1, uid)


def test_legacy_watermark_without_db_evidence_is_not_adopted(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    initialize_database(db)
    assert _sync(tmp_path, db, [1]).exit_code == 0  # DB restored to "only UID1"
    _clear_progress(db)  # a pre-fix DB carries no progress rows
    _legacy_json(tmp_path, 2)  # pre-fix file says UID2 was ingested

    result = _sync(tmp_path, db, [1, 2])

    assert result.exit_code == 0
    assert _ids(db) == ["<audit-1@example.com>", "<audit-2@example.com>"]


def test_legacy_watermark_is_not_inherited_by_fresh_database(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    initialize_database(db)
    _legacy_json(tmp_path, 2)

    result = _sync(tmp_path, db, [1, 2])

    assert result.exit_code == 0
    assert _ids(db) == ["<audit-1@example.com>", "<audit-2@example.com>"]


def test_legacy_watermark_backed_by_db_rows_is_adopted_without_rescan(
    tmp_path: Path,
) -> None:
    db = tmp_path / "data.db"
    initialize_database(db)
    assert _sync(tmp_path, db, [1, 2]).exit_code == 0
    _clear_progress(db)  # production DB before the first post-fix sync
    _legacy_json(tmp_path, 2)

    result = _sync(tmp_path, db, [1, 2])

    assert result.exit_code == 0
    assert result.pulled_messages == 0
    assert ImapSyncState(tmp_path / STATE).bound_database == str(db.resolve())


def test_legacy_watermark_is_adopted_only_up_to_what_the_db_holds(
    tmp_path: Path,
) -> None:
    db = tmp_path / "data.db"
    initialize_database(db)
    assert _sync(tmp_path, db, [1, 2]).exit_code == 0
    _clear_progress(db)
    _legacy_json(tmp_path, 3)  # file is one UID ahead of what the DB holds

    result = _sync(tmp_path, db, [1, 2, 3])

    assert result.exit_code == 0
    assert result.pulled_messages == 1  # only UID3, no rescan from UID1
    assert _ids(db)[-1] == "<audit-3@example.com>"


def test_dry_run_watermark_applies_the_same_legacy_limit_without_writing(
    tmp_path: Path,
) -> None:
    # dry-run reads the legacy file through effective_watermark and never adopts;
    # it must estimate from where the real sync would search, not from the file.
    fresh = tmp_path / "fresh.db"
    initialize_database(fresh)
    _legacy_json(tmp_path, 2)
    state = ImapSyncState(tmp_path / STATE)
    args = ("audit", "audit@example.com", "INBOX", "/", 1)
    assert state.effective_watermark(fresh, *args) is None  # real sync rescans

    db = tmp_path / "data.db"
    initialize_database(db)
    assert _sync(tmp_path, db, [1, 2]).exit_code == 0
    _clear_progress(db)
    _legacy_json(tmp_path, 3)
    state = ImapSyncState(tmp_path / STATE)
    assert state.effective_watermark(db, *args) == 2  # limited to what the DB holds
    assert state.bound_database is None  # nothing adopted, nothing written
    with connect_database(db, read_only=True) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM imap_sync_progress").fetchone()[0] == 0
        )


def test_legacy_watermark_ignores_another_accounts_rows(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    initialize_database(db)
    assert _sync(tmp_path, db, [1, 2]).exit_code == 0  # account "audit" holds 1, 2
    _clear_progress(db)
    # Same folder name, different account: its file claims UID 2 was ingested.
    other_env = {"IMAP_ACCOUNT_NAME": "other", "IMAP_USER": "other@example.com"}
    other_state = tmp_path / "maildir" / "other" / ".imap_watermarks.json"
    ImapSyncState(other_state).set_watermark("INBOX", "/", 1, 2)

    result = sync_native(
        env=other_env,
        database_path=db,
        maildir_root=tmp_path / "maildir",
        state_path=other_state,
        log_dir=tmp_path / "logs",
        client_factory=lambda _: _Client([1, 2]),
    )

    # "audit" rows are not evidence for "other": it must fetch both UIDs.
    assert result.exit_code == 0
    assert result.pulled_messages == 2


def test_adoption_failure_is_a_recorded_failed_run(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "data.db"
    initialize_database(db)

    def boom(self, database_path, account_name, account_email):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ImapSyncState, "adopt_legacy", boom)

    result = _sync(tmp_path, db, [1])

    assert result.exit_code != 0
    assert "database is locked" in (result.failure_message or "")
    with connect_database(db, read_only=True) as conn:
        assert [r[0] for r in conn.execute("SELECT outcome FROM sync_state")] == [
            "failed"
        ]
    assert list((tmp_path / "logs").glob("mbsync-*.log"))


def test_full_resync_ignores_stale_db_progress(tmp_path: Path, monkeypatch) -> None:
    import mailattic.imap_native as imap_native
    from mailattic.imap_sync import run_sync_imap

    db = tmp_path / "data.db"
    initialize_database(db)
    assert _sync(tmp_path, db, [1, 2]).exit_code == 0  # DB progress = 2
    env_file = tmp_path / "env"
    env_file.write_text(
        "IMAP_HOST=h\nIMAP_PORT=993\nIMAP_USER=audit@example.com\n"
        "IMAP_PASSWORD=x\nIMAP_ACCOUNT_NAME=audit\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(imap_native, "NativeImapClient", lambda _env: _Client([1, 2]))

    result = run_sync_imap(
        database_path=db,
        env_file=env_file,
        maildir_root=tmp_path / "maildir",
        log_dir=tmp_path / "logs",
        full_resync=True,
    )

    assert result.exit_code == 0
    assert result.pulled_messages == 2  # rescanned from UID 1 despite DB progress


def test_db1_db2_db1_keeps_every_uid_in_both(tmp_path: Path) -> None:
    db1 = tmp_path / "one.db"
    db2 = tmp_path / "two.db"
    initialize_database(db1)
    initialize_database(db2)
    both = ["<audit-1@example.com>", "<audit-2@example.com>"]
    assert _sync(tmp_path, db1, [1]).exit_code == 0
    assert _sync(tmp_path, db2, [1, 2]).exit_code == 0
    assert _sync(tmp_path, db1, [1, 2]).exit_code == 0
    assert _ids(db1) == both
    assert _ids(db2) == both

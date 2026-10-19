"""#256 (A): staging prune quarantines mail with an unresolved attachment failure.

Real stdlib MIME -> real maildir file -> real ingest -> real SQLite -> real
attachment store, all under ``tmp_path``. The only injected faults are an OSError
on the FIRST attachment write and (one test) on the quarantine move itself.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mailattic.imap_native import (
    MAILDIR_RETENTION_DAYS,
    prune_ingested_maildir,
    sync_native,
)
from mailattic.imap_state import ImapSyncState
from mailattic.quarantine import quarantine_mail
from mailattic.storage import connect_database
from tests.test_attachment_recovery import PDF, Env, _always_fail, _mime

ACCOUNT = "acct"
EMAIL = "buyer@example.test"
SYNC_ENV = {
    "IMAP_HOST": "mail.example.test",
    "IMAP_PORT": "993",
    "IMAP_USER": EMAIL,
    "IMAP_PASSWORD": "password",
    "IMAP_ACCOUNT_NAME": ACCOUNT,
}


def _age(path: Path, days: float = 8) -> None:
    ts = (datetime.now() - timedelta(days=days)).timestamp()
    os.utime(path, (ts, ts))


def _prune(env: Env, quarantine: Path) -> dict[str, int]:
    return prune_ingested_maildir(
        env.root / "maildir",
        ACCOUNT,
        env.db,
        MAILDIR_RETENTION_DAYS,
        datetime.now(),
        account_email=EMAIL,
        quarantine_root=quarantine,
    )


class _NoFolderClient:
    def list_folders(self):
        return []

    def close(self):
        pass


def _sync(env: Env, quarantine: Path):
    return sync_native(
        env=SYNC_ENV,
        database_path=env.db,
        maildir_root=env.root / "maildir",
        state_path=env.root / "wm.json",
        log_dir=env.root / "logs",
        attachment_root=env.cas,
        quarantine_root=quarantine,
        client_factory=lambda e: _NoFolderClient(),
    )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def quarantine(tmp_path: Path) -> Path:
    return tmp_path / "quarantine"


def _failed_old_mail(env: Env) -> tuple[Path, bytes]:
    raw = _mime()
    path = env.write_eml(raw)
    env.ingest(path, fail_first_write=True)
    _age(path)
    return path, raw


def test_prune_moves_unresolved_mail_to_quarantine_byte_identical(
    env: Env, quarantine: Path
) -> None:
    path, raw = _failed_old_mail(env)

    _prune(env, quarantine)

    moved = quarantine / ACCOUNT / "INBOX" / "new" / "uid-1.eml"
    assert not path.exists()
    assert moved.read_bytes() == raw


def test_same_prune_run_still_deletes_complete_mail(env: Env, quarantine: Path) -> None:
    failed, _raw = _failed_old_mail(env)
    healthy = env.write_eml(_mime(message_id="<ok@example.test>"), "uid-2.eml")
    env.ingest(healthy)
    _age(healthy)

    _prune(env, quarantine)

    assert not failed.exists() and not healthy.exists()
    assert [p.name for p in quarantine.rglob("*.eml")] == ["uid-1.eml"]


def test_failure_without_filename_holds_mail_in_quarantine(
    env: Env, quarantine: Path
) -> None:
    """#256 route 2026-10-13: any unresolved attachment failure pins the raw file.

    Inverts the earlier "only rows with a filename hold" reading: the ticket text says
    "unresolved attachment failure" with no filename qualifier, and `bes db doctor`
    lists the nameless row as unresolved.
    """
    path, raw = _failed_old_mail(env)
    connection = connect_database(env.db)
    connection.execute("UPDATE ingest_failures SET attachment_name = NULL")
    connection.commit()
    connection.close()

    _prune(env, quarantine)

    assert not path.exists()
    assert (quarantine / ACCOUNT / "INBOX" / "new" / "uid-1.eml").read_bytes() == raw


def test_quarantined_mail_is_recovered_by_next_sync_and_leaves_quarantine(
    env: Env, quarantine: Path
) -> None:
    _failed_old_mail(env)
    _prune(env, quarantine)  # fault is gone from here on

    result = _sync(env, quarantine)

    assert result.exit_code == 0
    assert result.attachments_recovered == 1
    row = env.attachment_rows()[0]
    assert row[1] == hashlib.sha256(PDF).hexdigest()
    assert list(quarantine.rglob("*.eml")) == []


def test_quarantined_mail_stays_while_attachment_still_fails(
    env: Env, quarantine: Path
) -> None:
    _failed_old_mail(env)
    _prune(env, quarantine)

    with _always_fail(OSError("disk still broken")):
        result = _sync(env, quarantine)

    assert result.attachments_still_failing == 1
    assert len(list(quarantine.rglob("*.eml"))) == 1


def test_quarantine_move_failure_never_fails_the_sync(
    env: Env, quarantine: Path, monkeypatch
) -> None:
    """One failed move must not stop the prune run for the other stale mail."""
    first = env.write_eml(_mime(message_id="<a@example.test>"), "uid-1.eml")
    second = env.write_eml(_mime(message_id="<b@example.test>"), "uid-2.eml")
    for path in (first, second):
        env.ingest(path, fail_first_write=True)
        _age(path)
    state = ImapSyncState(env.root / "wm.json")
    state.set_watermark("INBOX", "/", 1, 7)
    real_move = shutil.move
    calls = {"n": 0}

    def flaky_move(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("synthetic quarantine disk failure")
        return real_move(*args, **kwargs)

    monkeypatch.setattr(shutil, "move", flaky_move)

    # fault persists, so the retry cannot recover first; the real prune runs
    with _always_fail(OSError("disk still broken")):
        result = _sync(env, quarantine)

    assert result.exit_code == 0
    assert result.failure_count == 0
    assert ImapSyncState(env.root / "wm.json").get_watermark("INBOX", "/", 1) == 7
    assert len(list(quarantine.rglob("*.eml"))) == 1  # the second move still ran
    assert sum(p.exists() for p in (first, second)) == 1  # the failed one is retained


def test_sync_never_removes_quarantined_mail_for_a_closed_failure_row(
    env: Env, quarantine: Path
) -> None:
    """A database copy may have closed this row while the real one still waits."""
    _failed_old_mail(env)
    _prune(env, quarantine)
    connection = connect_database(env.db)
    connection.execute("UPDATE ingest_failures SET resolved_at = CURRENT_TIMESTAMP")
    connection.commit()
    connection.close()

    assert _sync(env, quarantine).exit_code == 0

    assert len(list(quarantine.rglob("*.eml"))) == 1


def test_recovered_quarantine_cleanup_failure_never_fails_the_sync(
    env: Env, quarantine: Path, monkeypatch
) -> None:
    """Contract #5: failed cleanup of a recovered raw file keeps exit and watermark."""
    _failed_old_mail(env)
    _prune(env, quarantine)
    ImapSyncState(env.root / "wm.json").set_watermark("INBOX", "/", 1, 7)

    def boom(*_args):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr("mailattic.ingest.release_quarantined_mail", boom)

    result = _sync(env, quarantine)

    assert result.exit_code == 0
    assert result.failure_count == 0
    assert result.attachments_recovered == 1
    assert ImapSyncState(env.root / "wm.json").get_watermark("INBOX", "/", 1) == 7


def _staged_and_quarantined(tmp_path: Path, staged: bytes, held: bytes):
    account_root = tmp_path / "maildir" / ACCOUNT
    source = account_root / "INBOX" / "new" / "uid-1.eml"
    source.parent.mkdir(parents=True)
    source.write_bytes(staged)
    qroot = tmp_path / "quarantine" / ACCOUNT
    target = qroot / "INBOX" / "new" / "uid-1.eml"
    target.parent.mkdir(parents=True)
    target.write_bytes(held)
    return source, account_root, qroot, target


def test_quarantine_mail_identical_existing_target_drops_only_the_staging_copy(
    tmp_path: Path,
) -> None:
    source, account_root, qroot, target = _staged_and_quarantined(
        tmp_path, b"same", b"same"
    )
    os.utime(target, (1_000_000_000, 1_000_000_000))

    assert quarantine_mail(source, account_root, qroot) == target

    assert not source.exists()
    assert target.read_bytes() == b"same"
    assert target.stat().st_mtime == 1_000_000_000  # the held file was not rewritten


def test_quarantine_mail_never_overwrites_a_different_quarantined_file(
    tmp_path: Path,
) -> None:
    source, account_root, qroot, target = _staged_and_quarantined(
        tmp_path, b"new mail", b"held mail"
    )

    with pytest.raises(FileExistsError):
        quarantine_mail(source, account_root, qroot)

    assert source.read_bytes() == b"new mail"
    assert target.read_bytes() == b"held mail"

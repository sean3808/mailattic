"""#256 route 2026-10-13: ONE hold rule for a raw mail at a locator.

The raw file stays (is quarantined at prune, is not released after a successful
retry or a manual abandon) while ANY unresolved attachment-stage failure row exists
for its ``logical_locator`` -- named or not, any account, any source mode. That is
the same set ``bes db doctor`` lists as unresolved.

Everything is real (stdlib MIME -> maildir file -> ingest -> SQLite -> CAS) under
``tmp_path``. The truth table is hand-written: the expected booleans never come from
``has_open_attachment_failure`` or any other function under test.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from mailattic import cli
from mailattic.imap_native import (
    MAILDIR_RETENTION_DAYS,
    prune_ingested_maildir,
    sync_native,
)
from mailattic.models import IngestFailure
from mailattic.quarantine import count_quarantined_mail, quarantine_mail
from mailattic.storage import connect_database, record_ingest_failure
from tests.test_attachment_abandon import _abandon
from tests.test_attachment_quarantine import (
    ACCOUNT,
    EMAIL,
    SYNC_ENV,
    _age,
    _NoFolderClient,
    _sync,
)
from tests.test_attachment_recovery import Env, _mime

LOCATOR = "INBOX/new/uid-1.eml"
OTHER_EMAIL = "other@example.test"


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def quarantine(tmp_path: Path) -> Path:
    return tmp_path / "quarantine"


def _record(env: Env, *, email: str = EMAIL, name: str | None) -> None:
    """One unresolved attachment-stage row shaped like the one ingest writes."""
    connection = connect_database(env.db)
    record_ingest_failure(
        connection,
        IngestFailure(
            pst_source=f"imap:{email}:INBOX",
            folder_path="INBOX",
            logical_locator=LOCATOR,
            source_input_mode="maildir",
            source_requested_from=str(env.folder_dir.parent),
            error_stage="attachment-store",
            error_type="AttachmentError",
            error_message=f"Attachment binary failure for: {name or 'unknown'}",
            attachment_name=name,
            attachment_size=None if name is None else 35,
        ),
    )
    connection.commit()
    connection.close()


def _sql(env: Env, sql: str) -> None:
    connection = connect_database(env.db)
    connection.execute(sql)
    connection.commit()
    connection.close()


def _build(env: Env, state: str) -> Path:
    """Ingest the mail, put the ledger into ``state``, return the aged staging file.

    Row id 1 is always the failure the real ingest recorded (when there is one).
    """
    path = env.write_eml(_mime())
    failing = state not in ("clean", "other-account-only")
    env.ingest(path, fail_first_write=failing)
    if state == "unnamed":
        _sql(env, "UPDATE ingest_failures SET attachment_name = NULL WHERE id = 1")
    elif state == "named+unnamed":
        _record(env, name=None)
    elif state == "recovered+unnamed":
        _sql(env, "UPDATE ingest_failures SET resolved_at = CURRENT_TIMESTAMP")
        _record(env, name=None)
    elif state == "abandoned":
        _sql(
            env,
            "UPDATE ingest_failures SET resolved_at = CURRENT_TIMESTAMP,"
            " abandon_reason = 'gone'",
        )
    elif state == "other-account-only" or state == "named+other-account":
        _record(env, email=OTHER_EMAIL, name="a.pdf")
    else:
        assert state in ("named", "clean"), state
    _age(path)
    return path


def _present(env: Env, quarantine: Path) -> bool:
    """Is the raw file kept anywhere (staging or quarantine)?"""
    return env.folder_dir.joinpath("new", "uid-1.eml").exists() or bool(
        list(quarantine.rglob("*.eml"))
    )


def _move_to_quarantine(env: Env, path: Path, quarantine: Path) -> None:
    account_root = env.root / "maildir" / ACCOUNT
    quarantine_mail(path, account_root, quarantine / ACCOUNT)
    assert len(list(quarantine.rglob("*.eml"))) == 1


def _prune_all_accounts(env: Env, quarantine: Path) -> None:
    prune_ingested_maildir(
        env.root / "maildir",
        ACCOUNT,
        env.db,
        MAILDIR_RETENTION_DAYS,
        datetime.now(),
        quarantine_root=quarantine,
    )


# state x action -> is the raw file still present afterwards (literal expectations).
# Not constructible, therefore absent from the table:
#   * retry of "the named failure" needs an UNRESOLVED NAMED row: absent for
#     "unnamed" (a nameless row can never be retried), "recovered+unnamed",
#     "abandoned", "clean", "other-account-only".
#   * abandon of "the named failure" needs that same unresolved row: absent for
#     "recovered+unnamed", "abandoned", "clean", "other-account-only". ("unnamed"
#     abandons its one nameless row, id 1.)
TABLE = [
    # state,               action,    still present
    ("named", "prune", True),
    ("named", "retry", False),
    ("named", "abandon", False),
    ("unnamed", "prune", True),
    ("unnamed", "abandon", False),
    ("named+unnamed", "prune", True),
    ("named+unnamed", "retry", True),
    ("named+unnamed", "abandon", True),
    ("recovered+unnamed", "prune", True),
    ("abandoned", "prune", False),
    ("clean", "prune", False),
    ("other-account-only", "prune", True),
    ("named+other-account", "prune", True),
    ("named+other-account", "retry", True),
    ("named+other-account", "abandon", True),
]


@pytest.mark.parametrize(
    ("state", "action", "expected_present"),
    [pytest.param(*row, id=f"{row[0]}-{row[1]}") for row in TABLE],
)
def test_hold_rule_truth_table(
    env: Env, quarantine: Path, state: str, action: str, expected_present: bool
) -> None:
    path = _build(env, state)

    if action == "prune":
        _prune_all_accounts(env, quarantine)
    elif action == "retry":
        _move_to_quarantine(env, path, quarantine)
        assert _sync(env, quarantine).exit_code == 0  # the write fault is gone
    else:
        _move_to_quarantine(env, path, quarantine)
        assert _abandon(env, quarantine, 1, "--yes") == 0

    assert _present(env, quarantine) is expected_present


# --- the default quarantine root follows the DATABASE, not the staging maildir -----


def _held_with_default_root(tmp_path: Path, env: Env, monkeypatch) -> Path:
    """Prune with default arguments; return where the database-derived root puts it."""
    # Keep the old production-default lookup (cli.DEFAULT_MAILDIR_ROOT) away from the
    # real machine whatever the code under test does.
    monkeypatch.setattr(
        cli, "DEFAULT_MAILDIR_ROOT", tmp_path / "decoy" / "maildir", raising=False
    )
    path = _build(env, "named")
    prune_ingested_maildir(
        env.root / "maildir",
        ACCOUNT,
        env.db,
        MAILDIR_RETENTION_DAYS,
        datetime.now(),
        account_email=EMAIL,
    )
    held = tmp_path / ".mailattic-work" / "quarantine" / "t.db" / ACCOUNT / LOCATOR
    assert not path.exists()
    assert held.is_file(), (
        "default root must be <db dir>/.mailattic-work/quarantine/<file name>"
    )
    return held


def _copy_database(env: Env, where: str) -> Path:
    copy = env.root / where
    copy.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(env.db)
    target = sqlite3.connect(copy)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return copy


def _default_sync(env: Env, database: Path):
    return sync_native(
        env=SYNC_ENV,
        database_path=database,
        maildir_root=env.root / "maildir",
        state_path=env.root / f"wm-{database.stem}.json",
        log_dir=env.root / "logs",
        attachment_root=env.cas,
        client_factory=lambda e: _NoFolderClient(),
    )


COPIES = [
    pytest.param("copydir/t.db", id="copy-in-other-directory"),
    pytest.param("t-copy.db", id="copy-with-other-file-name"),
    pytest.param("t.sqlite", id="copy-with-other-extension"),
]


@pytest.mark.parametrize("where", COPIES)
def test_retry_success_on_a_database_copy_leaves_the_original_quarantine_alone(
    tmp_path: Path, env: Env, monkeypatch, where: str
) -> None:
    held = _held_with_default_root(tmp_path, env, monkeypatch)
    copy = _copy_database(env, where)

    result = _default_sync(env, copy)  # default arguments, fault gone

    assert result.exit_code == 0
    assert held.is_file()


def test_retry_success_on_the_original_database_releases_its_quarantine(
    tmp_path: Path, env: Env, monkeypatch
) -> None:
    """Control for the isolation tests: the default root really is the one used."""
    held = _held_with_default_root(tmp_path, env, monkeypatch)

    result = _default_sync(env, env.db)

    assert result.attachments_recovered == 1
    assert not held.exists()


@pytest.mark.parametrize("where", COPIES)
def test_abandon_on_a_database_copy_leaves_the_original_quarantine_alone(
    tmp_path: Path, env: Env, monkeypatch, where: str
) -> None:
    held = _held_with_default_root(tmp_path, env, monkeypatch)
    copy = _copy_database(env, where)

    code = cli.main(
        [
            "db",
            "abandon-attachment-failure",
            "--database",
            str(copy),
            "--id",
            "1",
            "--reason",
            "experiment on a copy",
            "--yes",
        ]
    )

    assert code == 0
    assert held.is_file()


def test_abandon_on_the_original_database_releases_its_quarantine(
    tmp_path: Path, env: Env, monkeypatch
) -> None:
    """Control for the isolation tests."""
    held = _held_with_default_root(tmp_path, env, monkeypatch)

    code = cli.main(
        [
            "db",
            "abandon-attachment-failure",
            "--database",
            str(env.db),
            "--id",
            "1",
            "--reason",
            "real abandon",
            "--yes",
        ]
    )

    assert code == 0
    assert not held.exists()


def test_doctor_on_a_database_copy_counts_only_its_own_quarantine(
    tmp_path: Path, env: Env, monkeypatch, capsys
) -> None:
    _held_with_default_root(tmp_path, env, monkeypatch)
    copy = _copy_database(env, "copydir/t.db")
    capsys.readouterr()

    cli.main(["db", "doctor", "--database", str(env.db)])
    original = capsys.readouterr().out
    cli.main(["db", "doctor", "--database", str(copy)])
    copied = capsys.readouterr().out

    assert "quarantined_mail:   1" in original
    assert "quarantined_mail:   0" in copied


def test_default_quarantine_root_is_derived_from_the_database_path(
    tmp_path: Path,
) -> None:
    from mailattic.quarantine import default_quarantine_root

    database = tmp_path / "somewhere" / "mail-2.db"

    assert default_quarantine_root(database) == (
        tmp_path / "somewhere" / ".mailattic-work" / "quarantine" / "mail-2.db"
    )


# --- prune keeps what it cannot place -------------------------------------------------


def test_prune_retains_an_aged_ingested_file_whose_folder_cannot_be_resolved(
    env: Env, quarantine: Path
) -> None:
    raw = _mime()
    original = env.write_eml(raw)
    env.ingest(original)  # healthy: no failure row anywhere
    original.unlink()
    stray = env.root / "maildir" / ACCOUNT / "stray" / "uid-9.eml"  # no new/cur below
    stray.parent.mkdir(parents=True)
    stray.write_bytes(raw)
    _age(stray)

    _prune_all_accounts(env, quarantine)

    assert stray.exists() and stray.read_bytes() == raw


# --- count_quarantined_mail -----------------------------------------------------------


def test_count_quarantined_mail_counts_every_eml_once_whatever_the_shape(
    quarantine: Path,
) -> None:
    shapes = [
        "acct/INBOX/new/a.eml",  # the usual shape
        "acct/INBOX/Sub/cur/c.eml",  # nested folder
        "acct/stray/b.eml",  # no new/cur directory
        "acct/INBOX/new/nested/d.eml",  # below a bucket
        "top.eml",  # directly under the root
    ]
    for relative in [*shapes, "acct/INBOX/new/note.txt"]:
        held = quarantine / relative
        held.parent.mkdir(parents=True, exist_ok=True)
        held.write_bytes(b"x")

    assert count_quarantined_mail(quarantine) == 5


# --- a successful retry that read the STAGING copy ------------------------------------


def _staged_and_held(env: Env, quarantine: Path) -> Path:
    path = env.write_eml(_mime())
    env.ingest(path, fail_first_write=True)
    held = quarantine / ACCOUNT / LOCATOR
    held.parent.mkdir(parents=True)
    shutil.copy(path, held)
    return held


def test_staging_copy_retry_also_releases_the_quarantined_file(
    env: Env, quarantine: Path
) -> None:
    held = _staged_and_held(env, quarantine)

    result = _sync(env, quarantine)  # staging copy is read first; fault is gone

    assert result.attachments_recovered == 1
    assert env.folder_dir.joinpath("new", "uid-1.eml").exists()
    assert not held.exists()


def test_staging_copy_retry_keeps_the_quarantined_file_while_another_failure_is_open(
    env: Env, quarantine: Path
) -> None:
    held = _staged_and_held(env, quarantine)
    _record(env, name=None)

    result = _sync(env, quarantine)

    assert result.attachments_recovered == 1
    assert held.is_file()


def test_a_nested_file_prune_quarantined_is_released_by_abandon(
    env: Env, quarantine: Path
) -> None:
    """Prune and abandon share one path -> locator rule, at any depth below a folder."""
    staged = _build(env, "named")
    nested = staged.parent / "sub" / staged.name
    nested.parent.mkdir()
    staged.rename(nested)
    _age(nested)
    _sql(
        env,
        "UPDATE ingest_failures SET logical_locator = 'INBOX/new/sub/uid-1.eml'",
    )

    _prune_all_accounts(env, quarantine)
    held = quarantine / ACCOUNT / "INBOX" / "new" / "sub" / "uid-1.eml"
    assert held.is_file()
    assert not nested.exists()

    assert _abandon(env, quarantine, 1, "--yes") == 0
    assert not held.exists()

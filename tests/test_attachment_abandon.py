"""#256 (B): human-only abandon of an attachment failure, doctor visibility.

Same real-pipeline setup as test_attachment_quarantine.py, everything under
``tmp_path``; the CLI is driven through ``cli.main`` with explicit ``--database``
and ``--quarantine-root`` so nothing resolves to a production default.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from mailattic import cli
from mailattic.imap_native import sync_native
from mailattic.quarantine import (
    count_quarantined_mail,
    quarantined_files_freed_by_abandon,
)
from mailattic.storage import connect_database, initialize_database
from tests.test_attachment_quarantine import (
    ACCOUNT,
    SYNC_ENV,
    _age,
    _failed_old_mail,
    _NoFolderClient,
    _prune,
    _sync,
)
from tests.test_attachment_recovery import PDF, XLS, Env, _always_fail, _mime


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def quarantine_root(tmp_path: Path) -> Path:
    return tmp_path / "quarantine"


def _bes(env: Env, quarantine_root: Path, *args: str) -> int:
    return cli.main(
        [
            "db",
            *args[:1],
            "--database",
            str(env.db),
            "--quarantine-root",
            str(quarantine_root),
            *args[1:],
        ]
    )


def _abandon(env: Env, quarantine_root: Path, failure_id: int, *flags: str) -> int:
    return _bes(
        env,
        quarantine_root,
        "abandon-attachment-failure",
        "--id",
        str(failure_id),
        "--reason",
        "attachment unrecoverable",
        *flags,
    )


def _ledger(env: Env) -> list[tuple]:
    return [
        tuple(r)
        for r in env.query(
            "SELECT id, resolved_at IS NOT NULL, abandon_reason FROM ingest_failures"
            " ORDER BY id"
        )
    ]


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_abandon_records_reason_and_time_then_sync_neither_retries_nor_counts(
    env: Env, quarantine_root: Path
) -> None:
    """Raw mail is still there: only the abandon keeps one attachment out of retry."""
    path = env.write_eml(_mime(attachments=(("quote.pdf", PDF), ("sheet.xls", XLS))))
    with _always_fail(OSError("both fail")):
        env.ingest(path)
    ids = {
        r["attachment_name"]: r["id"]
        for r in env.query("SELECT id, attachment_name FROM ingest_failures")
    }

    assert _abandon(env, quarantine_root, ids["quote.pdf"], "--yes") == 0

    row = env.query(
        "SELECT resolved_at, abandon_reason FROM ingest_failures"
        f" WHERE id = {ids['quote.pdf']}"
    )[0]
    assert row["resolved_at"] and row["abandon_reason"] == "attachment unrecoverable"
    result = _sync(env, quarantine_root)  # fault is gone
    assert result.exit_code == 0
    assert (result.attachments_recovered, result.attachments_still_failing) == (1, 0)
    hashes = {r[0]: r[1] for r in env.attachment_rows()}
    assert hashes["quote.pdf"] is None  # abandoned: never retried
    assert hashes["sheet.xls"] is not None
    assert _ledger(env)[ids["quote.pdf"] - 1][2] == "attachment unrecoverable"


def test_abandon_one_of_two_failing_attachments_keeps_the_raw_mail_until_both_are_gone(
    env: Env, quarantine_root: Path
) -> None:
    path = env.write_eml(_mime(attachments=(("quote.pdf", PDF), ("sheet.xls", XLS))))
    with _always_fail(OSError("both fail")):
        env.ingest(path)
    _age(path)
    _prune(env, quarantine_root)
    assert len(list(quarantine_root.rglob("*.eml"))) == 1

    assert _abandon(env, quarantine_root, 1, "--yes") == 0
    assert len(list(quarantine_root.rglob("*.eml"))) == 1  # other one still open

    assert _abandon(env, quarantine_root, 2, "--yes") == 0
    assert list(quarantine_root.rglob("*.eml")) == []


def test_abandon_without_confirmation_in_non_interactive_env_changes_nothing(
    env: Env, quarantine_root: Path, capsys
) -> None:
    _failed_old_mail(env)
    _prune(env, quarantine_root)
    db_before = env.db.read_bytes()
    files_before = _tree(quarantine_root)

    code = _abandon(env, quarantine_root, 1)

    assert code == 1
    assert "confirmation required" in capsys.readouterr().err
    assert env.db.read_bytes() == db_before
    assert _tree(quarantine_root) == files_before
    assert _ledger(env) == [(1, 0, None)]


def test_doctor_reports_unresolved_count_and_quarantined_mail_count(
    env: Env, quarantine_root: Path, capsys
) -> None:
    _failed_old_mail(env)
    _prune(env, quarantine_root)

    code = _bes(env, quarantine_root, "doctor")

    out = capsys.readouterr().out
    assert "unresolved_attachment_failures: 1" in out
    assert "quarantined_mail:   1" in out
    assert code == 1  # open failures follow doctor's "finding -> exit 1" convention


def test_doctor_is_clean_with_exit_0_once_everything_is_resolved(
    env: Env, quarantine_root: Path, capsys
) -> None:
    _failed_old_mail(env)
    _prune(env, quarantine_root)
    assert _abandon(env, quarantine_root, 1, "--yes") == 0
    capsys.readouterr()

    code = _bes(env, quarantine_root, "doctor")

    out = capsys.readouterr().out
    assert "unresolved_attachment_failures: 0" in out
    assert "quarantined_mail:   0" in out
    assert code == 0


def test_legacy_failure_without_filename_or_size_is_listed_and_abandonable(
    env: Env, quarantine_root: Path, capsys
) -> None:
    connection = connect_database(env.db)
    connection.execute(
        "INSERT INTO ingest_failures (pst_source, folder_path, logical_locator,"
        " source_input_mode, error_stage, error_type, error_message)"
        " VALUES ('imap:buyer@example.test:INBOX', 'INBOX', 'INBOX/old.eml',"
        " 'maildir', 'attachment-store', 'OSError', 'disk')"
    )
    connection.commit()
    connection.close()

    assert _bes(env, quarantine_root, "doctor") == 1
    out = capsys.readouterr().out
    assert "unresolved_attachment_failures: 1" in out
    assert "id=1 INBOX/old.eml attachment=(unknown) size=(unknown)" in out

    assert _abandon(env, quarantine_root, 1, "--yes") == 0
    assert _ledger(env) == [(1, 1, "attachment unrecoverable")]
    assert _bes(env, quarantine_root, "doctor") == 0


def test_abandon_rejects_unknown_id_and_blank_reason_without_touching_anything(
    env: Env, quarantine_root: Path
) -> None:
    _failed_old_mail(env)
    assert _abandon(env, quarantine_root, 99, "--yes") == 2
    assert (
        _bes(
            env,
            quarantine_root,
            "abandon-attachment-failure",
            "--id",
            "1",
            "--reason",
            "  ",
            "--yes",
        )
        == 2
    )
    assert _ledger(env) == [(1, 0, None)]


def _quarantine_for_other_db(env: Env, quarantine_root: Path) -> Path:
    _failed_old_mail(env)
    _prune(env, quarantine_root)
    assert len(list(quarantine_root.rglob("*.eml"))) == 1
    return initialize_database(env.root / "unrelated.db")


def test_sync_with_an_unrelated_database_leaves_the_quarantine_alone(
    env: Env, quarantine_root: Path
) -> None:
    other = _quarantine_for_other_db(env, quarantine_root)

    result = sync_native(
        env=SYNC_ENV,
        database_path=other,
        maildir_root=env.root / "maildir",
        state_path=env.root / "wm2.json",
        log_dir=env.root / "logs",
        attachment_root=env.cas,
        quarantine_root=quarantine_root,
        client_factory=lambda e: _NoFolderClient(),
    )

    assert result.exit_code == 0
    assert len(list(quarantine_root.rglob("*.eml"))) == 1


def test_abandon_lists_quarantined_files_before_asking_for_confirmation(
    env: Env, quarantine_root: Path, capsys
) -> None:
    _failed_old_mail(env)
    _prune(env, quarantine_root)

    assert _abandon(env, quarantine_root, 1) == 1  # refused: non-interactive

    captured = capsys.readouterr()
    expected = quarantine_root / ACCOUNT / "INBOX" / "new" / "uid-1.eml"
    assert str(expected) in captured.out
    assert "confirmation required" in captured.err


# --- a database that predates the #255/#256 ledger columns -------------------

_OLD_DDL = """
CREATE TABLE ingest_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pst_source TEXT NOT NULL,
    folder_path TEXT,
    logical_locator TEXT,
    source_input_mode TEXT NOT NULL DEFAULT '',
    source_requested_from TEXT NOT NULL DEFAULT '',
    error_stage TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO ingest_failures (pst_source, folder_path, logical_locator,
    source_input_mode, error_stage, error_type, error_message)
VALUES ('imap:buyer@example.test:INBOX', 'INBOX', 'INBOX/old.eml',
    'maildir', 'attachment-store', 'OSError', 'disk');
"""


@pytest.fixture
def old_env(tmp_path: Path) -> SimpleNamespace:
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    connection.executescript(_OLD_DDL)
    connection.commit()
    connection.close()
    return SimpleNamespace(db=db)


def test_doctor_reports_legacy_rows_of_a_pre_255_database_without_writing(
    old_env, quarantine_root: Path, capsys
) -> None:
    before = old_env.db.read_bytes()

    code = _bes(old_env, quarantine_root, "doctor")

    out = capsys.readouterr().out
    assert "unresolved_attachment_failures: 1" in out
    assert "id=1 INBOX/old.eml attachment=(unknown) size=(unknown)" in out
    assert code == 1
    assert old_env.db.read_bytes() == before


def test_doctor_on_an_unreadable_database_is_exit_2_never_zero(
    tmp_path: Path, quarantine_root: Path, capsys
) -> None:
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)

    code = _bes(SimpleNamespace(db=bad), quarantine_root, "doctor")

    assert code == 2
    assert "unresolved_attachment_failures: 0" not in capsys.readouterr().out


def test_abandon_on_a_pre_255_database_closes_the_row_after_confirmation(
    old_env, quarantine_root: Path
) -> None:
    assert _abandon(old_env, quarantine_root, 1, "--yes") == 0

    connection = sqlite3.connect(old_env.db)
    try:
        row = connection.execute(
            "SELECT resolved_at IS NOT NULL, abandon_reason FROM ingest_failures"
        ).fetchone()
    finally:
        connection.close()
    assert row == (1, "attachment unrecoverable")


def test_refused_abandon_leaves_a_pre_255_database_byte_identical(
    old_env, quarantine_root: Path
) -> None:
    before = old_env.db.read_bytes()

    assert _abandon(old_env, quarantine_root, 1) == 1

    assert old_env.db.read_bytes() == before


def _open_failure(connection: sqlite3.Connection, email: str) -> int:
    cursor = connection.execute(
        "INSERT INTO ingest_failures (pst_source, folder_path, logical_locator,"
        " source_input_mode, error_stage, error_type, error_message,"
        " attachment_name) VALUES (?, 'INBOX', 'INBOX/new/uid-1.eml', 'maildir',"
        " 'attachment-store', 'OSError', 'disk', 'a.pdf')",
        [f"imap:{email}:INBOX"],
    )
    connection.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_abandon_never_frees_another_accounts_mail_with_the_same_locator(
    env: Env, quarantine_root: Path
) -> None:
    """Two accounts can both hold INBOX/new/uid-1.eml. The ledger knows accounts
    by email, the quarantine by directory name, so the two cannot be matched up:
    while any account still has an open failure for the locator, nothing is freed."""
    connection = connect_database(env.db)
    abandoned = _open_failure(connection, "a@example.test")
    _open_failure(connection, "b@example.test")
    for account in ("acct-a", "acct-b"):
        held = quarantine_root / account / "INBOX" / "new" / "uid-1.eml"
        held.parent.mkdir(parents=True)
        held.write_bytes(account.encode())

    freed = quarantined_files_freed_by_abandon(
        connection,
        quarantine_root,
        locator="INBOX/new/uid-1.eml",
        ignore_failure_id=abandoned,
    )
    connection.close()

    assert freed == []


def test_quarantined_mail_count_does_not_double_count_nested_folders(
    quarantine_root: Path,
) -> None:
    for relative in ("INBOX/new/a.eml", "INBOX/Sub/new/b.eml"):
        held = quarantine_root / "acct" / relative
        held.parent.mkdir(parents=True)
        held.write_bytes(b"x")

    assert count_quarantined_mail(quarantine_root) == 2

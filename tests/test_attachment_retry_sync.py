"""#255: the sync itself retries recorded attachment failures, counts them and
keeps contract #5 (exit code and watermark are not affected by a retry)."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import patch

from mailattic import attachment_store
from mailattic.cli import _attachment_retry_line
from mailattic.imap_native import NativeImapClient, sync_native
from mailattic.imap_state import ImapSyncState
from mailattic.storage import initialize_database
from tests.test_attachment_recovery import PDF, REAL_WRITE, _mime
from tests.test_imap_native import _TEST_ENV, _MockImapConn


def _sync(tmp_path: Path, uids: list[int], *, write=None):
    conn = _MockImapConn(
        list_data=[b'(\\HasNoChildren) "/" INBOX'],
        status_map={"INBOX": 1},
        search_map={"INBOX": uids},
        fetch_map={201: _mime(message_id="<sync-255@example.test>")},
    )

    def run():
        return sync_native(
            env=_TEST_ENV,
            database_path=tmp_path / "t.db",
            maildir_root=tmp_path / "maildir",
            state_path=tmp_path / ".imap_watermarks.json",
            log_dir=tmp_path / "logs",
            attachment_root=tmp_path / "cas",
            client_factory=lambda env: NativeImapClient(env, _connection=conn),
        )

    if write is None:
        return run()
    with patch.object(attachment_store, "write_attachment", write):
        return run()


def _fail_once():
    state = {"n": 0}

    def write(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("synthetic temporary disk failure")
        return REAL_WRITE(*args, **kwargs)

    return write


def _boom(*args, **kwargs):
    raise OSError("disk still broken")


def _hash(tmp_path: Path):
    with sqlite3.connect(tmp_path / "t.db") as c:
        return c.execute("SELECT content_hash FROM message_attachments").fetchone()[0]


def test_next_sync_recovers_the_attachment_and_reports_it(tmp_path: Path) -> None:
    initialize_database(tmp_path / "t.db")
    first = _sync(tmp_path, [201], write=_fail_once())
    assert _hash(tmp_path) is None
    assert (first.exit_code, first.failure_count) == (0, 0)
    assert first.attachments_recovered == 0

    second = _sync(tmp_path, [])  # nothing new on the server; fault gone

    assert _hash(tmp_path) == hashlib.sha256(PDF).hexdigest()
    assert second.attachments_recovered == 1
    assert (second.attachments_still_failing, second.attachments_source_missing) == (
        0,
        0,
    )
    assert second.exit_code == 0
    assert _attachment_retry_line(second.to_dict()) == (
        "attachment-retry: 1 recovered, 0 still failing, 0 source missing"
    )


def test_legacy_ledger_row_of_the_same_mail_does_not_skew_the_counts(
    tmp_path: Path,
) -> None:
    initialize_database(tmp_path / "t.db")
    _sync(tmp_path, [201], write=_fail_once())
    # A failure recorded before #255 has no name/size and is never retried.
    with sqlite3.connect(tmp_path / "t.db") as c:
        c.execute("CREATE TEMP TABLE legacy AS SELECT * FROM ingest_failures")
        c.execute(
            "UPDATE legacy SET id = NULL, attachment_name = NULL,"
            " attachment_size = NULL"
        )
        c.execute("INSERT INTO ingest_failures SELECT * FROM legacy")

    second = _sync(tmp_path, [])

    assert _hash(tmp_path) == hashlib.sha256(PDF).hexdigest()
    assert (
        second.attachments_recovered,
        second.attachments_still_failing,
        second.attachments_source_missing,
    ) == (1, 0, 0)


def test_failing_retry_keeps_exit_code_and_watermark(tmp_path: Path) -> None:
    initialize_database(tmp_path / "t.db")
    _sync(tmp_path, [201], write=_fail_once())

    result = _sync(tmp_path, [201], write=_boom)

    assert result.attachments_still_failing == 1
    assert result.attachments_recovered == 0
    assert (result.exit_code, result.failure_count) == (0, 0)
    watermark = ImapSyncState(tmp_path / ".imap_watermarks.json").get_watermark(
        "INBOX", "/", 1
    )
    assert watermark == 201
    assert _hash(tmp_path) is None


def test_retry_whose_mail_fails_to_normalize_adds_no_ledger_row(
    tmp_path: Path,
) -> None:
    initialize_database(tmp_path / "t.db")
    _sync(tmp_path, [201], write=_fail_once())

    def ledger():
        with sqlite3.connect(tmp_path / "t.db") as c:
            return c.execute(
                "SELECT error_stage, resolved_at FROM ingest_failures ORDER BY id"
            ).fetchall()

    before = ledger()

    with patch(
        "mailattic.ingest.build_normalized_message",
        side_effect=ValueError("synthetic normalize failure"),
    ):
        result = _sync(tmp_path, [])

    assert ledger() == before == [("attachment-store", None)]
    assert result.attachments_still_failing == 1
    assert (result.exit_code, result.failure_count) == (0, 0)


def test_retry_runs_from_local_files_before_any_connection_is_made(
    tmp_path: Path,
) -> None:
    initialize_database(tmp_path / "t.db")
    _sync(tmp_path, [201], write=_fail_once())
    assert _hash(tmp_path) is None
    seen: list[object] = []

    conn = _MockImapConn(
        list_data=[b'(\\HasNoChildren) "/" INBOX'],
        status_map={"INBOX": 1},
        search_map={"INBOX": []},
        fetch_map={},
    )

    def factory(env):
        # A factory that raises would escape sync_native (no result object),
        # so record what the DB looked like at the moment a connection opens.
        seen.append(_hash(tmp_path))
        return NativeImapClient(env, _connection=conn)

    result = sync_native(
        env=_TEST_ENV,
        database_path=tmp_path / "t.db",
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        attachment_root=tmp_path / "cas",
        client_factory=factory,
    )

    assert seen == [hashlib.sha256(PDF).hexdigest()]
    assert result.attachments_recovered == 1


def test_attachment_in_nested_imap_folder_is_retried_from_its_nested_directory(
    tmp_path: Path,
) -> None:
    initialize_database(tmp_path / "t.db")
    folder = "INBOX/Sub"

    def run(uids: list[int], write=None):
        conn = _MockImapConn(
            list_data=[f'(\\HasNoChildren) "/" {folder}'.encode()],
            status_map={folder: 1},
            search_map={folder: uids},
            fetch_map={201: _mime(message_id="<nested-255@example.test>")},
        )

        def go():
            return sync_native(
                env=_TEST_ENV,
                database_path=tmp_path / "t.db",
                maildir_root=tmp_path / "maildir",
                state_path=tmp_path / ".imap_watermarks.json",
                log_dir=tmp_path / "logs",
                attachment_root=tmp_path / "cas",
                client_factory=lambda env: NativeImapClient(env, _connection=conn),
            )

        if write is None:
            return go()
        with patch.object(attachment_store, "write_attachment", write):
            return go()

    run([201], write=_fail_once())
    assert (tmp_path / "maildir" / "testaccount" / "INBOX" / "Sub" / "new").is_dir()
    assert _hash(tmp_path) is None

    second = run([])

    assert _hash(tmp_path) == hashlib.sha256(PDF).hexdigest()
    assert (
        second.attachments_recovered,
        second.attachments_still_failing,
        second.attachments_source_missing,
    ) == (1, 0, 0)

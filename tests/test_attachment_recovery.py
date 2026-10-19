"""#255: a recorded attachment sub-failure is repaired from the original mail.

Everything here is real: stdlib-built MIME -> real maildir file -> real ingest
-> real SQLite -> real attachment store under ``tmp_path``. The only injected
fault is an OSError on the FIRST attachment write.
"""

from __future__ import annotations

import hashlib
import sqlite3
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import pytest

from mailattic import attachment_store
from mailattic.ingest import import_maildir_message
from mailattic.models import MaildirSource
from mailattic.storage import connect_database, initialize_database

PDF = b"%PDF-1.4 complete quotation payload"
XLS = b"second attachment, never faulted"
REAL_WRITE = attachment_store.write_attachment


def _mime(
    *,
    message_id: str = "<recover@example.test>",
    attachments: tuple[tuple[str, bytes], ...] = (("quote.pdf", PDF),),
) -> bytes:
    msg = EmailMessage()
    msg["From"] = "supplier@example.test"
    msg["To"] = "buyer@example.test"
    msg["Subject"] = "Quotation"
    msg["Message-ID"] = message_id
    msg.set_content("See attached.")
    for name, payload in attachments:
        msg.add_attachment(
            payload, maintype="application", subtype="octet-stream", filename=name
        )
    return msg.as_bytes()


class Env:
    def __init__(self, root: Path, folder: str = "INBOX") -> None:
        self.root = root
        self.db = initialize_database(root / "t.db")
        self.cas = root / "cas"
        self.folder_dir = root / "maildir" / "acct" / folder
        (self.folder_dir / "new").mkdir(parents=True)
        self.source = MaildirSource(
            path=self.folder_dir,
            account_email="buyer@example.test",
            account_name="acct",
            folder_name=folder,
            requested_from=self.folder_dir.parent,
        )

    def write_eml(self, raw: bytes, name: str = "uid-1.eml") -> Path:
        path = self.folder_dir / "new" / name
        path.write_bytes(raw)
        return path

    def ingest(self, path: Path, *, fail_first_write: bool = False):
        connection = connect_database(self.db)
        try:
            if fail_first_write:
                calls = {"n": 0}

                def flaky(*args, **kwargs):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise OSError("synthetic temporary disk failure")
                    return REAL_WRITE(*args, **kwargs)

                with patch.object(attachment_store, "write_attachment", flaky):
                    summary = import_maildir_message(
                        connection=connection,
                        source=self.source,
                        message_path=path,
                        attachment_root=self.cas,
                    )
            else:
                summary = import_maildir_message(
                    connection=connection,
                    source=self.source,
                    message_path=path,
                    attachment_root=self.cas,
                )
            connection.commit()
            return summary
        finally:
            connection.close()

    def query(self, sql: str) -> list[sqlite3.Row]:
        connection = connect_database(self.db)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def attachment_rows(self) -> list[tuple]:
        return [
            tuple(r)
            for r in self.query(
                "SELECT attachment_name, content_hash, size_bytes, mime_type,"
                " storage_path FROM message_attachments ORDER BY id"
            )
        ]

    def cas_files(self) -> set[Path]:
        return {p for p in self.cas.rglob("*") if p.is_file()}


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_reimport_after_store_failure_repairs_the_attachment_row(env: Env) -> None:
    path = env.write_eml(_mime())
    env.ingest(path, fail_first_write=True)
    assert env.attachment_rows()[0][1:] == (None, None, None, None)

    summary = env.ingest(path)  # fault gone

    name, content_hash, size, _mime_type, storage_path = env.attachment_rows()[0]
    assert (name, size) == ("quote.pdf", len(PDF))
    assert content_hash == hashlib.sha256(PDF).hexdigest()
    assert storage_path
    assert summary.duplicate_messages == 1


def _count(env: Env, sql: str) -> int:
    return int(env.query(sql)[0][0])


OPEN_LEDGER = "SELECT COUNT(*) FROM ingest_failures WHERE resolved_at IS NULL"


def _retry(env: Env):
    from mailattic.ingest import retry_attachment_failures

    connection = connect_database(env.db)
    try:
        return retry_attachment_failures(
            connection=connection,
            maildir_root=env.root / "maildir",
            account_name="acct",
            account_email="buyer@example.test",
            attachment_root=env.cas,
        )
    finally:
        connection.close()


def _counts(result) -> tuple[int, int, int]:
    return (result.recovered, result.still_failing, result.source_missing)


def _always_fail(exc: Exception):
    def write(*args, **kwargs):
        raise exc

    return patch.object(attachment_store, "write_attachment", write)


def test_repair_keeps_one_message_has_exact_bytes_and_leaves_no_orphan(
    env: Env,
) -> None:
    path = env.write_eml(_mime())
    env.ingest(path, fail_first_write=True)
    assert _count(env, "SELECT COUNT(*) FROM ingest_failures") == 1
    env.ingest(path)

    assert _count(env, "SELECT COUNT(*) FROM messages") == 1
    storage_path = env.attachment_rows()[0][4]
    files = env.cas_files()
    assert len(files) == 1  # nothing unreferenced
    assert storage_path.endswith(next(iter(files)).relative_to(env.cas).as_posix())
    assert hashlib.sha256(next(iter(files)).read_bytes()).hexdigest() == (
        hashlib.sha256(PDF).hexdigest()
    )
    # ledger row is kept and marked resolved, so no later retry picks it up
    rows = env.query("SELECT error_stage, resolved_at FROM ingest_failures")
    assert [(r[0], r[1] is not None) for r in rows] == [("attachment-store", True)]
    assert _counts(_retry(env)) == (0, 0, 0)


def test_attachments_cli_finds_and_exports_the_repaired_attachment(
    env: Env, tmp_path: Path, capsys
) -> None:
    from mailattic.cli import main

    path = env.write_eml(_mime())
    env.ingest(path, fail_first_write=True)
    env.ingest(path)
    out_dir = tmp_path / "export"

    code = main(
        [
            "attachments",
            "--database",
            str(env.db),
            "--attachment-root",
            str(env.cas),
            "--name-contains",
            "quote",
            "--copy-to",
            str(out_dir),
            "--json",
        ]
    )

    capsys.readouterr()
    assert code == 0
    exported = [p for p in out_dir.rglob("*") if p.is_file()]
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in exported] == [
        hashlib.sha256(PDF).hexdigest()
    ]


def test_retry_recovers_from_local_mail_and_counts_it(env: Env) -> None:
    path = env.write_eml(_mime())
    env.ingest(path, fail_first_write=True)

    assert _counts(_retry(env)) == (1, 0, 0)
    assert env.attachment_rows()[0][1] == hashlib.sha256(PDF).hexdigest()
    assert _counts(_retry(env)) == (0, 0, 0)


def test_retry_still_failing_is_counted_each_time_and_adds_no_ledger_rows(
    env: Env,
) -> None:
    path = env.write_eml(_mime())
    env.ingest(path, fail_first_write=True)
    before = env.attachment_rows()

    with _always_fail(OSError("disk still broken")):
        one, two = _retry(env), _retry(env)

    assert _counts(one) == (0, 1, 0)
    assert _counts(two) == (0, 1, 0)
    assert env.attachment_rows() == before
    assert _count(env, "SELECT COUNT(*) FROM ingest_failures") == 1
    assert _count(env, OPEN_LEDGER) == 1


def test_permanent_decode_failure_is_never_treated_as_recovered(env: Env) -> None:
    path = env.write_eml(_mime())
    with _always_fail(ValueError("cannot decode")):
        env.ingest(path)
        results = [_retry(env), _retry(env)]

    assert [_counts(r) for r in results] == [(0, 1, 0), (0, 1, 0)]
    assert env.attachment_rows()[0][1:] == (None, None, None, None)
    assert _count(env, OPEN_LEDGER) == 1


def test_missing_raw_mail_is_counted_as_source_missing(env: Env) -> None:
    path = env.write_eml(_mime())
    env.ingest(path, fail_first_write=True)
    path.unlink()

    assert _counts(_retry(env)) == (0, 0, 1)
    assert env.attachment_rows()[0][1] is None


def test_only_the_failed_attachment_of_a_multi_attachment_mail_is_repaired(
    env: Env,
) -> None:
    path = env.write_eml(_mime(attachments=(("quote.pdf", PDF), ("sheet.xls", XLS))))
    env.ingest(path, fail_first_write=True)
    before = env.attachment_rows()
    assert before[0][1] is None and before[1][1] is not None

    assert _counts(_retry(env)) == (1, 0, 0)

    after = env.attachment_rows()
    assert after[1] == before[1]  # untouched, byte for byte
    assert after[0][1] == hashlib.sha256(PDF).hexdigest()


def test_historical_placeholder_without_ledger_entry_is_left_alone(env: Env) -> None:
    path = env.write_eml(_mime())
    env.ingest(path)  # healthy first import
    connection = connect_database(env.db)
    connection.execute(
        "UPDATE message_attachments SET content_hash=NULL, size_bytes=NULL,"
        " mime_type=NULL, storage_path=NULL"
    )
    connection.commit()
    connection.close()
    before = env.attachment_rows()

    env.ingest(path)  # reimport
    assert env.attachment_rows() == before
    assert _counts(_retry(env)) == (0, 0, 0)


@pytest.mark.parametrize(
    ("new_name", "new_payload"),
    [("quote.pdf", PDF + b" tampered"), ("other.pdf", PDF)],
    ids=["size-differs", "name-differs"],
)
def test_name_or_size_mismatch_is_not_filled(
    env: Env, new_name: str, new_payload: bytes
) -> None:
    path = env.write_eml(_mime())
    env.ingest(path, fail_first_write=True)
    before = env.attachment_rows()
    env.write_eml(_mime(attachments=((new_name, new_payload),)))  # same Message-ID

    env.ingest(path)

    assert env.attachment_rows() == before
    assert _count(env, OPEN_LEDGER) == 1


def test_other_source_same_message_id_fills_only_the_empty_row(
    env: Env, tmp_path: Path
) -> None:
    attachments = (("quote.pdf", PDF), ("sheet.xls", XLS))
    env.ingest(env.write_eml(_mime(attachments=attachments)), fail_first_write=True)
    complete_before = env.attachment_rows()[1]

    other = Env(tmp_path / "other", folder="Archive")
    other.db, other.cas = env.db, env.cas
    other.ingest(other.write_eml(_mime(attachments=attachments)))

    rows = env.attachment_rows()
    assert rows[0][1] == hashlib.sha256(PDF).hexdigest()
    assert rows[1] == complete_before
    assert _count(env, "SELECT COUNT(*) FROM messages") == 1
    assert env.query("SELECT folder_path FROM messages")[0][0] == "INBOX"


def test_existing_database_upgrades_in_place_when_ledger_gains_columns(
    tmp_path: Path,
) -> None:
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE ingest_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pst_source TEXT NOT NULL,
            folder_path TEXT,
            logical_locator TEXT,
            error_stage TEXT NOT NULL,
            error_type TEXT NOT NULL,
            error_message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO ingest_failures (pst_source, error_stage, error_type, error_message)
        VALUES ('imap:a:INBOX', 'attachment-store', 'AttachmentError', 'old row');
        """
    )
    connection.commit()
    connection.close()

    initialize_database(db)

    connection = connect_database(db)
    columns = {
        r["name"] for r in connection.execute("PRAGMA table_info(ingest_failures)")
    }
    row = connection.execute(
        "SELECT error_message, attachment_name, attachment_size, resolved_at"
        " FROM ingest_failures"
    ).fetchone()
    connection.close()
    assert {"attachment_name", "attachment_size", "resolved_at"} <= columns
    assert tuple(row) == ("old row", None, None, None)


SAME_NAME = (("dup.pdf", PDF), ("dup.pdf", PDF + b" -- the second, longer one"))


def _fail_only(payload: bytes):
    """Fail the store of one specific payload, let every other one through."""

    def write(data, *args, **kwargs):
        if data == payload:
            raise OSError("synthetic failure for one attachment")
        return REAL_WRITE(data, *args, **kwargs)

    return patch.object(attachment_store, "write_attachment", write)


def _same_name_failed_twice(env: Env) -> Path:
    path = env.write_eml(_mime(attachments=SAME_NAME))
    with _always_fail(OSError("both fail")):
        env.ingest(path)
    assert [r[1] for r in env.attachment_rows()] == [None, None]
    return path


def _ledger(env: Env) -> dict[int, bool]:
    return {
        r[0]: r[1] is not None
        for r in env.query(
            "SELECT attachment_size, resolved_at FROM ingest_failures"
            " ORDER BY attachment_size"
        )
    }


def test_same_name_attachments_are_matched_by_position_not_by_name(env: Env) -> None:
    _same_name_failed_twice(env)
    first_size, second_size = (len(p) for _, p in SAME_NAME)

    with _fail_only(SAME_NAME[0][1]):  # only the SECOND can be stored
        one, two = _retry(env), _retry(env)

    rows = env.attachment_rows()
    assert rows[0][1:] == (None, None, None, None)  # row 1 stays empty
    assert rows[1][1] == hashlib.sha256(SAME_NAME[1][1]).hexdigest()
    assert _ledger(env) == {first_size: False, second_size: True}
    assert _counts(one) == (1, 1, 0)
    assert _counts(two) == (0, 1, 0)


def test_same_name_attachments_both_recovered_are_not_swapped(env: Env) -> None:
    _same_name_failed_twice(env)

    assert _counts(_retry(env)) == (2, 0, 0)

    rows = env.attachment_rows()
    assert [r[1] for r in rows] == [hashlib.sha256(p).hexdigest() for _, p in SAME_NAME]
    assert all(_ledger(env).values())


def test_same_name_after_partial_recovery_the_rest_is_recovered_in_place(
    env: Env,
) -> None:
    _same_name_failed_twice(env)
    with _fail_only(SAME_NAME[0][1]):
        _retry(env)

    assert _counts(_retry(env)) == (1, 0, 0)

    assert [r[1] for r in env.attachment_rows()] == [
        hashlib.sha256(p).hexdigest() for _, p in SAME_NAME
    ]
    assert all(_ledger(env).values())

"""#226: DB attachment references must be reconciled against the CAS store."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path

import pytest

from mailattic.attachment_store import (
    verify_cas_references,
    write_attachment,
)
from mailattic.cli import _handle_db_verify_cas
from mailattic.rebuild import RebuildContext, run_verify_cas_phase
from mailattic.storage import initialize_database


def _make_db(tmp_path: Path) -> Path:
    db = initialize_database(tmp_path / "audit.db")
    conn = sqlite3.connect(db)
    for i in (1, 2):
        conn.execute(
            "INSERT INTO messages (subject,pst_source,folder_path,logical_locator,"
            "dedup_key,dedup_basis) VALUES ('s','a.pst','Inbox',?,?, 'message_id')",
            (str(i), f"k{i}"),
        )
    conn.commit()
    conn.close()
    return db


def _add_row(db: Path, message_id: int, name: str, storage_path: str | None) -> None:
    conn = sqlite3.connect(db)
    digest = hashlib.sha256(name.encode()).hexdigest() if storage_path else None
    conn.execute(
        "INSERT INTO message_attachments (message_id,attachment_name,content_hash,"
        "size_bytes,storage_path) VALUES (?,?,?,?,?)",
        (message_id, name, digest, 1, storage_path),
    )
    conn.commit()
    conn.close()


def _store(root: Path, db: Path, message_id: int, payload: bytes) -> str:
    record = write_attachment(payload, "f.bin", None, root)
    _add_row(db, message_id, "f.bin", record.storage_path)
    return record.storage_path


def _run_cli(db: Path, root: Path) -> int:
    return _handle_db_verify_cas(argparse.Namespace(database=db, attachment_root=root))


def test_missing_root_with_referenced_attachment_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _make_db(tmp_path)
    path = "attachments/aa/aa/" + "a" * 64
    _add_row(db, 1, "missing.pdf", path)

    rc = _run_cli(db, tmp_path / "no-such-dir")

    out = capsys.readouterr().out
    assert rc == 1
    assert "missing_refs:    1" in out
    assert f"attachment_id=1 db_id=1 path={path}" in out


def test_single_missing_file_in_otherwise_complete_tree_fails(
    tmp_path: Path,
) -> None:
    db = _make_db(tmp_path)
    root = tmp_path / "attachments"
    _store(root, db, 1, b"one")
    gone = _store(root, db, 2, b"two")
    (root / gone.removeprefix("attachments/")).unlink()

    result = verify_cas_references(db, root)

    assert result.referenced == 2
    assert [m.storage_path for m in result.missing] == [gone]
    assert (result.missing[0].attachment_id, result.missing[0].db_id) == (2, 2)
    assert _run_cli(db, root) == 1


def test_two_attachments_both_checked_not_skipped(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    root = tmp_path / "attachments"
    _add_row(db, 1, "x", "attachments/bb/bb/" + "b" * 64)
    _add_row(db, 2, "y", "attachments/cc/cc/" + "c" * 64)

    assert len(verify_cas_references(db, root).missing) == 2


def test_empty_db_and_missing_root_succeeds(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    assert _run_cli(db, tmp_path / "no-such-dir") == 0


def test_all_null_placeholder_rows_succeed(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _add_row(db, 1, "inline.png", None)
    _add_row(db, 2, "sig.png", None)

    result = verify_cas_references(db, tmp_path / "no-such-dir")

    assert result.referenced == 0
    assert result.missing == []
    assert _run_cli(db, tmp_path / "no-such-dir") == 0


def test_complete_tree_succeeds(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    root = tmp_path / "attachments"
    _store(root, db, 1, b"one")
    _store(root, db, 2, b"two")

    assert _run_cli(db, root) == 0


def test_same_hash_referenced_by_many_rows_reports_every_row(
    tmp_path: Path,
) -> None:
    db = _make_db(tmp_path)
    root = tmp_path / "attachments"
    path = _store(root, db, 1, b"shared")
    _store(root, db, 2, b"shared")
    assert verify_cas_references(db, root).missing == []

    (root / path.removeprefix("attachments/")).unlink()
    result = verify_cas_references(db, root)

    assert result.referenced == 2
    assert result.distinct_paths == 1
    assert [m.attachment_id for m in result.missing] == [1, 2]


def test_verification_is_read_only(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    root = tmp_path / "attachments"
    _add_row(db, 1, "x", "attachments/bb/bb/" + "b" * 64)
    before = db.read_bytes()

    verify_cas_references(db, root)

    assert db.read_bytes() == before
    assert not root.exists()


def test_escaping_storage_path_reported_missing(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _add_row(db, 1, "x", "attachments/../../outside")

    assert len(verify_cas_references(db, tmp_path / "attachments").missing) == 1


def test_missing_database_is_an_error_not_a_pass(tmp_path: Path) -> None:
    assert _run_cli(tmp_path / "nope.db", tmp_path / "attachments") == 2


def test_rebuild_verify_cas_phase_fails_on_missing_reference(
    tmp_path: Path,
) -> None:
    db = _make_db(tmp_path)
    _add_row(db, 1, "x", "attachments/bb/bb/" + "b" * 64)
    context = RebuildContext(
        work_dir=tmp_path,
        stage_db=db,
        attachment_root=tmp_path / "attachments",
        pst_paths=(),
        readpst_path=None,
        env_file=tmp_path / ".env",
        maildir_root=tmp_path / "maildir",
        log_dir=tmp_path / "logs",
    )

    with pytest.raises(RuntimeError, match="verification failed"):
        run_verify_cas_phase(context, {})

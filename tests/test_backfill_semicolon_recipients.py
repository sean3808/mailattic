"""Tests for scripts/backfill_semicolon_recipients.py.

Covers the acceptance criteria in
openspec/changes/fix-semicolon-recipient-truncation/design.md (Decisions 4-5)
and specs/message-ingestion/spec.md's "repair previously truncated
recipients" requirement: dry-run reports without writing, a confirmed run
inserts exactly the missing recipients and updates the derived search text +
FTS index, a second confirmed run is a no-op, and a failing pre-write backup
aborts the run with the database left unmodified.

Uses a real schema (via ``initialize_database``) seeded through the
production ``build_normalized_message`` / ``persist_message`` path so the
fixture never drifts from the actual storage contract; it never touches the
real mailattic.db.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backfill_semicolon_recipients as script_module  # noqa: E402
from backfill_semicolon_recipients import run_backfill  # noqa: E402

from mailattic.models import (  # noqa: E402
    PstSource,
    build_normalized_message,
)
from mailattic.parser import _extract_raw_headers  # noqa: E402
from mailattic.storage import (  # noqa: E402
    connect_database,
    initialize_database,
)
from mailattic.storage import (  # noqa: E402
    persist_message as storage_persist_message,
)

_DUMMY_SOURCE = PstSource(
    path=Path("dummy.pst"), input_mode="file", requested_from=Path("dummy.pst")
)

# The exact bug shape from design.md: a bare semicolon-separated To header of
# three addresses, with no display names or group syntax.
_RAW_BYTES = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: bob@recoverme.example; carol@recoverme.example; dave@recoverme.example\r\n"
    b"Subject: Truncated recipients fixture\r\n"
    b"Message-ID: <fixture-1@example.com>\r\n"
    b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
    b"\r\n"
    b"Body text\r\n"
)


def _seed_truncated_message(db_path: Path) -> int:
    """Persist a message reproducing the historical issue #15 bug.

    Only the first To recipient is stored in message_recipients /
    recipient_search_text / message_fts (mirroring ingestion before the
    parser fix), while raw_headers carries the full semicolon list — exactly
    the shape the backfill script must repair.
    """
    raw_headers = _extract_raw_headers(_RAW_BYTES)
    assert raw_headers is not None

    raw_message = {
        "subject": "Truncated recipients fixture",
        "sender": {"name": "Alice", "email": "alice@example.com"},
        "to": [{"name": None, "email": "bob@recoverme.example"}],
        "cc": [],
        "bcc": [],
        "sent_at": "2024-01-01T12:00:00+00:00",
        "received_at": None,
        "body_text": "Body text",
        "attachments": [],
        "attachment_names": [],
        "message_id": "<fixture-1@example.com>",
        "in_reply_to": None,
        "references": None,
        "raw_headers": raw_headers,
    }

    msg = build_normalized_message(
        raw_message,
        source=_DUMMY_SOURCE,
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )

    conn = connect_database(db_path)
    try:
        result = storage_persist_message(conn, msg)
        conn.commit()
    finally:
        conn.close()
    return result.stored_message_id


def _fetch_message_row(db_path: Path, db_id: int) -> dict:
    conn = connect_database(db_path)
    try:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", [db_id]).fetchone()
        return dict(row)
    finally:
        conn.close()


def _fetch_recipients(db_path: Path, db_id: int) -> list[dict]:
    conn = connect_database(db_path)
    try:
        rows = conn.execute(
            "SELECT recipient_type, display_name, email FROM message_recipients "
            "WHERE message_id = ? ORDER BY id",
            [db_id],
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _fetch_fts_recipients(db_path: Path, db_id: int) -> str | None:
    conn = connect_database(db_path)
    try:
        row = conn.execute(
            "SELECT recipients FROM message_fts WHERE rowid = ?", [db_id]
        ).fetchone()
        return row["recipients"] if row else None
    finally:
        conn.close()


@pytest.fixture
def seeded_db(tmp_path: Path) -> tuple[Path, int]:
    db_path = tmp_path / "fixture.db"
    initialize_database(db_path)
    db_id = _seed_truncated_message(db_path)
    return db_path, db_id


def test_dry_run_reports_missing_and_writes_nothing(
    seeded_db: tuple[Path, int],
) -> None:
    db_path, db_id = seeded_db

    exit_code = run_backfill(db_path, confirm=False)

    assert exit_code == 0
    recipients = _fetch_recipients(db_path, db_id)
    assert len(recipients) == 1  # unchanged
    row = _fetch_message_row(db_path, db_id)
    assert "carol@recoverme.example" not in row["recipient_search_text"]
    assert not (db_path.parent / "backups").exists()  # no backup on dry-run


def test_confirm_inserts_missing_updates_search_text_and_fts(
    seeded_db: tuple[Path, int],
) -> None:
    db_path, db_id = seeded_db

    exit_code = run_backfill(db_path, confirm=True)

    assert exit_code == 0
    recipients = _fetch_recipients(db_path, db_id)
    emails = {row["email"] for row in recipients}
    assert emails == {
        "bob@recoverme.example",
        "carol@recoverme.example",
        "dave@recoverme.example",
    }

    row = _fetch_message_row(db_path, db_id)
    for email in emails:
        assert email in row["recipient_search_text"]

    fts_recipients = _fetch_fts_recipients(db_path, db_id)
    assert fts_recipients is not None
    assert "carol" in fts_recipients

    # A previously-dropped recipient's domain search now matches via FTS.
    conn = connect_database(db_path)
    try:
        hits = conn.execute(
            "SELECT rowid FROM message_fts WHERE message_fts MATCH ?",
            ["carol"],
        ).fetchall()
        assert [hit["rowid"] for hit in hits] == [db_id]
    finally:
        conn.close()

    backups_dir = db_path.parent / "backups"
    assert backups_dir.exists()
    assert list(backups_dir.glob("*.db"))


def test_confirm_second_run_is_idempotent(seeded_db: tuple[Path, int]) -> None:
    db_path, db_id = seeded_db

    first_exit = run_backfill(db_path, confirm=True)
    assert first_exit == 0

    backups_dir = db_path.parent / "backups"
    backups_before = set(backups_dir.glob("*.db"))

    second_exit = run_backfill(db_path, confirm=True)
    assert second_exit == 0

    # Nothing left to repair → no new backup snapshot, no further writes.
    assert set(backups_dir.glob("*.db")) == backups_before
    recipients = _fetch_recipients(db_path, db_id)
    assert len(recipients) == 3


def test_confirm_aborts_and_db_unmodified_when_backup_fails(
    seeded_db: tuple[Path, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, db_id = seeded_db

    def _raise_backup(*args: object, **kwargs: object) -> None:
        raise OSError("simulated backup failure")

    monkeypatch.setattr(script_module, "backup_database", _raise_backup)

    exit_code = run_backfill(db_path, confirm=True)

    assert exit_code == 1
    recipients = _fetch_recipients(db_path, db_id)
    assert len(recipients) == 1  # unchanged — nothing written
    row = _fetch_message_row(db_path, db_id)
    assert "carol@recoverme.example" not in row["recipient_search_text"]

    backups_dir = db_path.parent / "backups"
    assert not backups_dir.exists() or not list(backups_dir.glob("*.db"))


def test_compute_backfill_plan_scans_only_raw_headers_rows(
    seeded_db: tuple[Path, int],
) -> None:
    """A row with no raw_headers is scanned-out, never treated as affected."""
    db_path, _db_id = seeded_db
    conn = connect_database(db_path)
    try:
        conn.execute(
            """
            INSERT INTO messages (
                subject, sender_email, body_text, pst_source, folder_path,
                logical_locator, dedup_key, recipient_search_text
            ) VALUES ('no headers', 'x@example.com', '', 'test', '/Inbox',
                      'loc-no-headers', 'no-headers-dedup', '')
            """
        )
        conn.commit()
    finally:
        conn.close()

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows_scanned, plan = script_module.compute_backfill_plan(con)
    finally:
        con.close()

    assert rows_scanned == 1  # only the seeded raw_headers row
    assert len(plan) == 1
    assert plan[0].message_id == "<fixture-1@example.com>"

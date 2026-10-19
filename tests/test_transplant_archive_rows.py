"""Tests for scripts/transplant-archive-rows.py

Covers:
  - dry-run leaves target empty and reports plan
    (test_dry_run_leaves_target_empty_and_reports_plan)
  - commit inserts with new ids and remaps FKs and FTS5
    (test_commit_inserts_with_new_ids_and_remaps_fks)
  - unique-constraint rollback preserves target pre-state
    (test_unique_constraint_rollback_preserves_target)
  - empty source returns zero counts
    (test_empty_source_returns_zero_counts)
  - decoy modes excluded (test_decoy_modes_excluded)
  - end-to-end CLI path via subprocess
    (test_cli_dry_run_exit0_and_json_output)

Note on IntegrityError handling: the transplant() function catches ALL
exceptions internally, performs ROLLBACK, and returns
result["result"] = "rollback-on-error: <exc>". It does NOT re-raise. The
unique-constraint rollback test therefore asserts on result["result"]
rather than using pytest.raises.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from mailattic.storage import initialize_database

# ---------------------------------------------------------------------------
# Module loader (scripts/ is not a package on PYTHONPATH)
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_transplant_module() -> ModuleType:
    script_path = _SCRIPTS_DIR / "transplant-archive-rows.py"
    spec = importlib.util.spec_from_file_location(
        "transplant_archive_rows", script_path
    )
    assert spec is not None, f"Could not load spec from {script_path}"
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Cache module at collection time (avoids repeated IO per test)
_MOD = _load_transplant_module()
transplant = _MOD.transplant


# ---------------------------------------------------------------------------
# Shared seed helper
# ---------------------------------------------------------------------------

_ARCHIVE_ROWS = [
    {
        "subject": "Order Update 1",
        "sender_name": "Alice",
        "sender_email": "alice@supplier.com",
        "sent_at": "2024-01-01T10:00:00",
        "received_at": None,
        "body_text": "body one",
        "message_id": "<order-1@mailattic.test>",
        "in_reply_to": None,
        "references_header": None,
        "pst_source": "archive:order-update-1.eml",
        "folder_path": "Outbox",
        "logical_locator": "Outbox/1",
        "source_input_mode": "archive",
        "source_requested_from": "https://example.com/order/1",
        "dedup_key": "dedup-order-1",
        "dedup_basis": "message_id",
        "recipient_search_text": "bob@buyer.com",
        "attachment_search_text": "invoice.pdf",
        "created_at": "2024-01-01T10:00:00",
    },
    {
        "subject": "Order Update 2",
        "sender_name": "Alice",
        "sender_email": "alice@supplier.com",
        "sent_at": "2024-01-02T10:00:00",
        "received_at": None,
        "body_text": "body two",
        "message_id": "<order-2@mailattic.test>",
        "in_reply_to": "<order-1@mailattic.test>",
        "references_header": "<order-1@mailattic.test>",
        "pst_source": "archive:order-update-2.eml",
        "folder_path": "Outbox",
        "logical_locator": "Outbox/2",
        "source_input_mode": "archive",
        "source_requested_from": "https://example.com/order/2",
        "dedup_key": "dedup-order-2",
        "dedup_basis": "message_id",
        "recipient_search_text": "carol@buyer.com",
        "attachment_search_text": "",
        "created_at": "2024-01-02T10:00:00",
    },
    {
        "subject": "Order Update 3",
        "sender_name": "Bob",
        "sender_email": "bob@supplier.com",
        "sent_at": "2024-01-03T10:00:00",
        "received_at": None,
        "body_text": "body three",
        "message_id": "<order-3@mailattic.test>",
        "in_reply_to": None,
        "references_header": None,
        "pst_source": "archive:order-update-3.eml",
        "folder_path": "Outbox",
        "logical_locator": "Outbox/3",
        "source_input_mode": "archive",
        "source_requested_from": "https://example.com/order/3",
        "dedup_key": "dedup-order-3",
        "dedup_basis": "message_id",
        "recipient_search_text": "dave@buyer.com",
        "attachment_search_text": "spec.pdf",
        "created_at": "2024-01-03T10:00:00",
    },
]

_PST_DECOY = {
    "subject": "PST Import Message",
    "sender_name": "Zeta",
    "sender_email": "zeta@pst.com",
    "sent_at": "2024-01-04T10:00:00",
    "received_at": None,
    "body_text": "pst body",
    "message_id": "<pst-decoy@mailattic.test>",
    "in_reply_to": None,
    "references_header": None,
    "pst_source": "/test/path/archive_2024.pst",
    "folder_path": "Inbox",
    "logical_locator": "Inbox/1",
    "source_input_mode": "file",
    "source_requested_from": "",
    "dedup_key": "dedup-pst-decoy",
    "dedup_basis": "message_id",
    "recipient_search_text": "",
    "attachment_search_text": "",
    "created_at": "2024-01-04T10:00:00",
}

_INSERT_SQL = """
    INSERT INTO messages (
        subject, sender_name, sender_email, sent_at, received_at, body_text,
        message_id, in_reply_to, references_header, pst_source, folder_path,
        logical_locator, source_input_mode, source_requested_from, dedup_key,
        dedup_basis, recipient_search_text, attachment_search_text, created_at
    ) VALUES (
        :subject, :sender_name, :sender_email, :sent_at, :received_at, :body_text,
        :message_id, :in_reply_to, :references_header, :pst_source, :folder_path,
        :logical_locator, :source_input_mode, :source_requested_from, :dedup_key,
        :dedup_basis, :recipient_search_text, :attachment_search_text, :created_at
    )
"""


def _seed_source(source_path: Path) -> None:
    """Seed source DB: 3 archive rows (pst_source LIKE 'archive:%') + 1 pst decoy
    + recipients + attachments."""
    con = sqlite3.connect(str(source_path))
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        for row in _ARCHIVE_ROWS:
            cur = con.execute(_INSERT_SQL, row)
            msg_id = cur.lastrowid
            # One recipient per message
            con.execute(
                "INSERT INTO message_recipients "
                "(message_id, recipient_type, display_name, email) "
                "VALUES (?, 'to', 'Buyer', ?)",
                (msg_id, row["recipient_search_text"] or "unknown@buyer.com"),
            )
            # One attachment for rows 1 and 3 only
            if row["attachment_search_text"]:
                con.execute(
                    "INSERT INTO message_attachments "
                    "(message_id, attachment_name, content_hash, "
                    "size_bytes, mime_type, storage_path) "
                    "VALUES (?, ?, 'abc123', 1024, 'application/pdf', "
                    "'att/file.pdf')",
                    (msg_id, row["attachment_search_text"]),
                )
            # FTS5 row
            sender_text = f"{row['sender_name']} <{row['sender_email']}>"
            con.execute(
                "INSERT INTO message_fts "
                "(rowid, message_rowid, subject, sender, recipients, "
                "body_text, attachment_names) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    msg_id,
                    msg_id,
                    row["subject"],
                    sender_text,
                    row["recipient_search_text"],
                    row["body_text"],
                    row["attachment_search_text"],
                ),
            )
        # Decoy row (pst family — pst_source has no 'archive:' / 'imap:' prefix,
        # should be excluded)
        con.execute(_INSERT_SQL, _PST_DECOY)
        con.commit()
    finally:
        con.close()


@pytest.fixture()
def source_db(tmp_path: Path) -> Path:
    """Initialised + seeded source DB."""
    path = tmp_path / "source.db"
    initialize_database(path)
    _seed_source(path)
    return path


@pytest.fixture()
def target_db(tmp_path: Path) -> Path:
    """Fresh target DB with schema only (no rows)."""
    path = tmp_path / "target.db"
    initialize_database(path)
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dry_run_leaves_target_empty_and_reports_plan(
    source_db: Path, target_db: Path
) -> None:
    result = transplant(source_db, target_db, dry_run=True)

    # Returned dict shape
    assert result["mode"] == "dry-run"
    assert result["rows_transplanted"] == 3
    assert result["old_to_new_id_count"] == 3
    assert result["recipients_remapped"] == 3
    assert result["attachments_remapped"] == 2  # rows 1 and 3 have attachments
    assert result["fts_rows_inserted"] == 3
    assert result["result"] == "ok"

    # Target must remain empty (ROLLBACK executed)
    con = sqlite3.connect(str(target_db))
    try:
        assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM message_recipients").fetchone()[0] == 0
        assert (
            con.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
        )
        assert con.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0] == 0
    finally:
        con.close()


def test_commit_inserts_with_new_ids_and_remaps_fks(
    source_db: Path, target_db: Path
) -> None:
    result = transplant(source_db, target_db, dry_run=False)

    assert result["mode"] == "commit"
    assert result["rows_transplanted"] == 3
    assert result["old_to_new_id_count"] == 3
    assert result["recipients_remapped"] == 3
    assert result["attachments_remapped"] == 2
    assert result["fts_rows_inserted"] == 3
    assert result["result"] == "ok"

    con = sqlite3.connect(str(target_db))
    con.row_factory = sqlite3.Row
    try:
        # Only archive rows present (pst decoy excluded)
        rows = con.execute(
            "SELECT id, message_id, source_input_mode FROM messages ORDER BY id"
        ).fetchall()
        assert len(rows) == 3
        assert all(r["source_input_mode"] == "archive" for r in rows)

        # Decoy NOT in target
        decoy = con.execute(
            "SELECT id FROM messages WHERE message_id = ?",
            (_PST_DECOY["message_id"],),
        ).fetchone()
        assert decoy is None

        # RFC 5322 message_id preserved verbatim
        ids_in_target = {r["message_id"] for r in rows}
        expected_ids = {r["message_id"] for r in _ARCHIVE_ROWS}
        assert ids_in_target == expected_ids

        # FK remapping: every recipient points to a valid target messages.id
        target_msg_ids = {r["id"] for r in rows}
        rec_msg_ids = {
            r[0]
            for r in con.execute("SELECT message_id FROM message_recipients").fetchall()
        }
        assert rec_msg_ids.issubset(target_msg_ids)
        assert len(rec_msg_ids) > 0

        # FK remapping: every attachment points to a valid target messages.id
        att_msg_ids = {
            r[0]
            for r in con.execute(
                "SELECT message_id FROM message_attachments"
            ).fetchall()
        }
        assert att_msg_ids.issubset(target_msg_ids)

        # FTS5 alignment: rowid and message_rowid both equal the new messages.id
        for row in rows:
            new_id: int = row["id"]
            fts_row = con.execute(
                "SELECT rowid, message_rowid FROM message_fts WHERE rowid = ?",
                (new_id,),
            ).fetchone()
            assert fts_row is not None, f"FTS5 row missing for messages.id={new_id}"
            assert fts_row["message_rowid"] == new_id
    finally:
        con.close()


def test_unique_constraint_rollback_preserves_target(
    source_db: Path, target_db: Path
) -> None:
    # Pre-seed target with one archive-scrape row whose message_id
    # collides with source row 1
    con = sqlite3.connect(str(target_db))
    try:
        con.execute(_INSERT_SQL, {**_ARCHIVE_ROWS[0], "dedup_key": "pre-seeded-dedup"})
        con.commit()
        pre_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        con.close()

    result = transplant(source_db, target_db, dry_run=False)

    # Transplant must have rolled back on IntegrityError
    assert result["result"].startswith("rollback-on-error:"), (
        f"Expected rollback-on-error, got: {result['result']!r}"
    )

    # Target post-state == pre-state (only the 1 pre-seeded row)
    con = sqlite3.connect(str(target_db))
    try:
        post_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert post_count == pre_count == 1
        assert con.execute("SELECT COUNT(*) FROM message_recipients").fetchone()[0] == 0
        assert (
            con.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
        )
        assert con.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0] == 0
    finally:
        con.close()


def test_empty_source_returns_zero_counts(target_db: Path, tmp_path: Path) -> None:
    # Source DB with schema but no archive-scrape rows
    empty_source = tmp_path / "empty_source.db"
    initialize_database(empty_source)

    result = transplant(empty_source, target_db, dry_run=False)

    assert result["mode"] == "commit"
    assert result["rows_transplanted"] == 0
    assert result["old_to_new_id_count"] == 0
    assert result["recipients_remapped"] == 0
    assert result["attachments_remapped"] == 0
    assert result["fts_rows_inserted"] == 0
    assert result["result"] == "ok"

    con = sqlite3.connect(str(target_db))
    try:
        assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    finally:
        con.close()


def test_decoy_modes_excluded(target_db: Path, tmp_path: Path) -> None:
    # Source DB: mixed modes — only 1 archive row (pst_source LIKE 'archive:%'),
    # 2 pst decoys
    mixed_source = tmp_path / "mixed_source.db"
    initialize_database(mixed_source)

    con = sqlite3.connect(str(mixed_source))
    try:
        # 1 archive row (pst_source LIKE 'archive:%')
        con.execute(_INSERT_SQL, _ARCHIVE_ROWS[0])
        # 2 pst decoys (pst_source without 'archive:' / 'imap:' prefix)
        for suffix, dedupkey in [("a", "dedup-pst-a"), ("b", "dedup-pst-b")]:
            con.execute(
                _INSERT_SQL,
                {
                    **_PST_DECOY,
                    "message_id": f"<pst-decoy-{suffix}@example.com>",
                    "dedup_key": dedupkey,
                    "logical_locator": f"Inbox/{suffix}",
                },
            )
        con.commit()
    finally:
        con.close()

    result = transplant(mixed_source, target_db, dry_run=False)

    assert result["rows_transplanted"] == 1
    assert result["result"] == "ok"

    con = sqlite3.connect(str(target_db))
    try:
        rows = con.execute("SELECT source_input_mode FROM messages").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "archive"
    finally:
        con.close()


def test_cli_dry_run_exit0_and_json_output(source_db: Path, target_db: Path) -> None:
    """End-to-end: invoke the script via subprocess, parse JSON, check exit 0."""
    project_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            "uv",
            "run",
            "python",
            str(project_root / "scripts" / "transplant-archive-rows.py"),
            "--source-db",
            str(source_db),
            "--target-db",
            str(target_db),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=60,
    )
    assert proc.returncode == 0, f"CLI exited {proc.returncode}; stderr: {proc.stderr}"

    output = json.loads(proc.stdout)
    assert output["mode"] == "dry-run"
    assert output["rows_transplanted"] == 3
    assert output["result"] == "ok"

    # Dry-run must not have touched target
    con = sqlite3.connect(str(target_db))
    try:
        assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    finally:
        con.close()

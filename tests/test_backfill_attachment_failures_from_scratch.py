"""Tests for scripts/backfill_attachment_failures_from_scratch.py (issue #30, route A).

Production holds a legacy PST failure (no attachment name/size columns) and an
empty placeholder; the scratch DB holds the same message from the same PST with
the attachment stored. Both are seeded through ``persist_message``.
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

import backfill_attachment_failures_from_scratch as script_module  # noqa: E402
from backfill_attachment_failures_from_scratch import run  # noqa: E402

from mailattic.attachment_store import read_attachment, write_attachment  # noqa: E402
from mailattic.models import (  # noqa: E402
    IngestFailure,
    PstSource,
    build_normalized_message,
)
from mailattic.storage import (  # noqa: E402
    connect_database,
    initialize_database,
    persist_message,
    record_ingest_failure,
)

PAYLOAD = b"attachment body"
NAME = "Report.TXT"


def _seed(
    database: Path,
    pst: str,
    *,
    message_id: str,
    attachments: list[dict[str, object]],
    failure_name: str | None = None,
) -> None:
    initialize_database(database)
    source = PstSource(path=Path(pst), input_mode="file", requested_from=Path(pst))
    message = build_normalized_message(
        {
            "subject": "s",
            "sender": {"email": "a@example.com"},
            "sent_at": "2024-01-02T03:04:05",
            "body_text": "b",
            "message_id": message_id,
            "attachments": attachments,
        },
        source=source,
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    con = connect_database(database)
    try:
        persist_message(con, message)
        if failure_name is not None:
            record_ingest_failure(
                con,
                IngestFailure(
                    pst_source=message.pst_source,
                    folder_path="Inbox",
                    logical_locator="Inbox/1",
                    source_input_mode="file",
                    source_requested_from=message.pst_source,
                    error_stage="attachment-decode",
                    error_type="AttachmentError",
                    # The shape ingest.py writes for attachment failures.
                    error_message=f"Attachment binary failure for: {failure_name}",
                ),
            )
            con.execute("UPDATE ingest_failures SET created_at = '2026-05-27 13:00:00'")
        con.commit()
    finally:
        con.close()


def _setup(tmp_path: Path, *, scratch_payload: bytes = PAYLOAD) -> tuple[Path, Path]:
    production = tmp_path / "prod" / "mail.db"
    production.parent.mkdir()
    _seed(
        production,
        "/old/wsl/path/archive.pst",
        message_id="<m1@example.com>",
        attachments=[{"attachment_name": NAME}],
        failure_name="report.txt",
    )
    scratch = tmp_path / "scratch" / "trial.db"
    scratch.parent.mkdir()
    record = write_attachment(
        scratch_payload, NAME, "text/plain", scratch.parent / "attachments"
    )
    _seed(
        scratch,
        r"C:\PST\Archive.pst",
        message_id="<m1@example.com>",
        attachments=[
            {
                "attachment_name": NAME,
                "content_hash": record.content_hash,
                "size_bytes": record.size_bytes,
                "mime_type": "text/plain",
                "storage_path": record.storage_path,
            }
        ],
    )
    return production, scratch


def _state(database: Path) -> tuple[sqlite3.Row, sqlite3.Row]:
    con = connect_database(database, read_only=True)
    con.row_factory = sqlite3.Row
    try:
        attachment = con.execute("SELECT * FROM message_attachments").fetchone()
        failure = con.execute("SELECT * FROM ingest_failures").fetchone()
    finally:
        con.close()
    return attachment, failure


def test_dry_run_reports_fillable_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    production, scratch = _setup(tmp_path)

    assert run(production, [scratch], confirm=False) == 0

    out = capsys.readouterr().out
    assert "candidates: 1" in out
    assert "fillable: 1" in out
    attachment, failure = _state(production)
    assert attachment["storage_path"] is None
    assert failure["resolved_at"] is None


def test_confirm_fills_placeholder_and_resolves_ledger_row(tmp_path: Path) -> None:
    production, scratch = _setup(tmp_path)
    store = tmp_path / "prod-attachments"

    code = run(
        production,
        [scratch],
        confirm=True,
        attachment_root=store,
        backup_path=tmp_path / "backup.db",
    )

    assert code == 0
    assert (tmp_path / "backup.db").exists()
    attachment, failure = _state(production)
    assert read_attachment(attachment["storage_path"], store) == PAYLOAD
    assert attachment["size_bytes"] == len(PAYLOAD)
    assert attachment["attachment_name"] == NAME
    assert failure["resolved_at"] is not None
    assert failure["abandon_reason"] is None

    # A second run finds nothing left to do.
    assert run(production, [scratch], confirm=True, attachment_root=store) == 0
    assert _state(production)[0]["content_hash"] == attachment["content_hash"]


def test_scratch_file_not_matching_its_hash_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    production, scratch = _setup(tmp_path)
    con = sqlite3.connect(scratch)
    con.execute("UPDATE message_attachments SET content_hash = ?", ["0" * 64])
    con.commit()
    con.close()

    assert run(production, [scratch], confirm=True, attachment_root=tmp_path / "s") == 0

    assert (
        "skipped (scratch file does not match its hash): 1" in capsys.readouterr().out
    )
    attachment, failure = _state(production)
    assert attachment["storage_path"] is None
    assert failure["resolved_at"] is None


def test_message_missing_from_scratch_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    production, scratch = _setup(tmp_path)
    con = sqlite3.connect(scratch)
    con.execute(
        "UPDATE messages SET message_id = '<other@example.com>', dedup_key = 'x'"
    )
    con.commit()
    con.close()

    assert run(production, [scratch], confirm=False) == 0

    assert "skipped (message not in scratch DB): 1" in capsys.readouterr().out


def test_second_same_name_scratch_attachment_without_content_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # One same-name part decoded, the other did not: which one the production
    # placeholder stands for cannot be proven.
    production, scratch = _setup(tmp_path)
    con = sqlite3.connect(scratch)
    con.execute(
        "INSERT INTO message_attachments (message_id, attachment_name)"
        " SELECT message_id, attachment_name FROM message_attachments"
    )
    con.commit()
    con.close()

    assert run(production, [scratch], confirm=True, attachment_root=tmp_path / "s") == 0

    assert "skipped (scratch attachment name not unique): 1" in capsys.readouterr().out
    assert _state(production)[0]["storage_path"] is None


def test_failure_outside_the_batch_day_is_not_a_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    production, scratch = _setup(tmp_path)
    con = sqlite3.connect(production)
    con.execute("UPDATE ingest_failures SET created_at = '2026-06-01 09:00:00'")
    con.commit()
    con.close()

    assert run(production, [scratch], confirm=True, attachment_root=tmp_path / "s") == 0

    assert "candidates: 0" in capsys.readouterr().out
    assert _state(production)[1]["resolved_at"] is None


def test_two_scratch_dbs_of_the_same_pst_are_refused(tmp_path: Path) -> None:
    production, scratch = _setup(tmp_path)
    twin = tmp_path / "twin.db"
    twin.write_bytes(scratch.read_bytes())

    assert run(production, [scratch, twin], confirm=False) == 1


def test_unreadable_scratch_file_skips_only_that_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production, scratch = _setup(tmp_path)

    def unreadable(*_args: object) -> bytes:
        raise PermissionError("locked")

    monkeypatch.setattr(script_module, "read_attachment", unreadable)

    assert run(production, [scratch], confirm=False) == 0

    assert "skipped (scratch file missing or unreadable): 1" in capsys.readouterr().out


def test_database_given_as_scratch_is_refused(tmp_path: Path) -> None:
    production, scratch = _setup(tmp_path)

    assert run(scratch, [scratch], confirm=True, attachment_root=tmp_path / "s") == 1

    assert _state(production)[1]["resolved_at"] is None

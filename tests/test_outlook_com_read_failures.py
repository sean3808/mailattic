"""#238: COM read failures of Class / Attachments leave failure evidence.

Properties really raise; items go through the real OutlookComReader._read_item
-> import_pst_sources -> SQLite and the written extraction summary.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from mailattic.ingest import import_pst_sources
from mailattic.models import PstSource
from mailattic.outlook_pst import OutlookComReader
from mailattic.storage import connect_database, initialize_database


class BadClass(SimpleNamespace):
    @property
    def Class(self):  # noqa: N802
        raise RuntimeError("simulated COM classification unavailable")


class BadAttachments(SimpleNamespace):
    Class = 43

    @property
    def Attachments(self):  # noqa: N802
        raise RuntimeError("simulated COM attachment collection unavailable")


class BadAttachmentCount(SimpleNamespace):
    Class = 43

    @property
    def Attachments(self):  # noqa: N802
        return BadCount()


class BadCount:
    @property
    def Count(self):  # noqa: N802
        raise RuntimeError("simulated COM attachment count unavailable")


class _Reader:
    def __init__(self, result) -> None:
        self.result = result

    def iter_messages(self, source):
        yield self.result


def _fields() -> dict:
    return {
        "EntryID": "synthetic-entry",
        "Recipients": SimpleNamespace(Count=0),
        "Subject": "audit",
        "SenderName": "source",
        "SenderEmailType": "SMTP",
        "SenderEmailAddress": "source@example.com",
        "SentOn": None,
        "ReceivedTime": None,
        "Body": "audit body",
        "PropertyAccessor": None,
        "To": "",
        "CC": "",
        "BCC": "",
    }


def _run(tmp_path: Path, item):
    pst = tmp_path / "synthetic.pst"
    pst.write_bytes(b"fixture: reader is injected, PST is never opened")
    source = PstSource(pst, "file", pst)
    result = OutlookComReader(attachment_root=tmp_path / "attachments")._read_item(
        item, folder_path="INBOX"
    )
    db = tmp_path / "data.db"
    initialize_database(db)
    summary = import_pst_sources(
        database_path=db,
        sources=[source],
        reader=_Reader(result),
        summary_root=tmp_path / "summaries",
    )
    with connect_database(db, read_only=True) as conn:
        counts = dict(
            conn.execute(
                "SELECT (SELECT COUNT(*) FROM messages) AS messages, "
                "(SELECT COUNT(*) FROM message_attachments) AS attachments"
            ).fetchone()
        )
        failures = [
            dict(row)
            for row in conn.execute(
                "SELECT error_stage, error_message, logical_locator, "
                "folder_path, pst_source FROM ingest_failures"
            )
        ]
    ledger = json.loads(
        Path(summary.recipient_extraction_summary_path).read_text("utf-8")
    )
    return summary, counts, failures, ledger


def _account(ledger: dict) -> dict:
    return ledger["sources"][0] if "sources" in ledger else ledger["accounts"][0]


def test_class_read_failure_is_recorded_not_a_non_mail_skip(tmp_path: Path) -> None:
    summary, counts, failures, ledger = _run(tmp_path, BadClass(**_fields()))

    assert counts["messages"] == 0
    assert len(failures) == 1
    assert failures[0]["error_stage"] == "read-item-class"
    assert "simulated COM classification unavailable" in failures[0]["error_message"]
    assert failures[0]["logical_locator"] == "synthetic-entry"
    assert failures[0]["folder_path"] == "INBOX"
    account = _account(ledger)
    assert account["parse_failures"] == 1
    assert account["skipped_non_mail_items"] == 0
    assert summary.failure_count == 1
    # Recorded failures are balanced in the ledger (same policy as other
    # parse failures); completeness judgment is unchanged.
    assert summary.recipient_extraction_complete is True


def test_attachment_collection_failure_is_recorded_message_still_imported(
    tmp_path: Path,
) -> None:
    summary, counts, failures, ledger = _run(tmp_path, BadAttachments(**_fields()))

    assert counts == {"messages": 1, "attachments": 0}
    assert len(failures) == 1
    assert failures[0]["error_stage"] == "attachment-collection-read"
    assert (
        "simulated COM attachment collection unavailable"
        in (failures[0]["error_message"])
    )
    assert failures[0]["logical_locator"] == "synthetic-entry"
    assert summary.imported_messages == 1
    assert _account(ledger)["parse_failures"] == 0
    assert summary.recipient_extraction_complete is True


def test_attachment_count_read_failure_is_recorded(tmp_path: Path) -> None:
    _, counts, failures, _ = _run(tmp_path, BadAttachmentCount(**_fields()))

    assert counts["messages"] == 1
    assert [f["error_stage"] for f in failures] == ["attachment-collection-read"]
    assert "simulated COM attachment count unavailable" in failures[0]["error_message"]


def test_genuine_non_mail_class_is_still_a_silent_skip(tmp_path: Path) -> None:
    item = SimpleNamespace(Class=26, EntryID="entry-meeting")
    summary, counts, failures, ledger = _run(tmp_path, item)

    assert counts["messages"] == 0
    assert failures == []
    assert _account(ledger)["skipped_non_mail_items"] == 1
    assert summary.failure_count == 0


def test_zero_attachment_count_adds_no_failure(tmp_path: Path) -> None:
    item = SimpleNamespace(Class=43, Attachments=SimpleNamespace(Count=0), **_fields())
    summary, counts, failures, _ = _run(tmp_path, item)

    assert counts == {"messages": 1, "attachments": 0}
    assert failures == []
    assert summary.failure_count == 0

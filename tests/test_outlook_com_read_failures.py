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


class _Accessor:
    def __init__(self, props: dict) -> None:
        self.props = props

    def GetProperty(self, name: str):  # noqa: N802
        if name not in self.props:
            raise RuntimeError("property unavailable")
        return self.props[name]


def _pdf_item(*, data=b"healthy PDF bytes"):
    from mailattic import outlook_pst

    props = {outlook_pst.PR_ATTACH_MIME_TAG: "application/pdf"}
    if data is not None:
        props[outlook_pst.PR_ATTACH_DATA_BIN] = data
    attachment = SimpleNamespace(
        FileName="quote.pdf", PropertyAccessor=_Accessor(props)
    )
    collection = SimpleNamespace(Count=1, Item=lambda index: attachment)
    return SimpleNamespace(Class=43, Attachments=collection, **_fields())


def _import_again(tmp_path: Path, item):
    pst = tmp_path / "synthetic.pst"
    pst.write_bytes(b"fixture: reader is injected, PST is never opened")
    result = OutlookComReader(attachment_root=tmp_path / "attachments")._read_item(
        item, folder_path="INBOX"
    )
    db = tmp_path / "data.db"
    initialize_database(db)
    return import_pst_sources(
        database_path=db,
        sources=[PstSource(pst, "file", pst)],
        reader=_Reader(result),
        summary_root=tmp_path / "summaries",
    )


def _attachment_state(tmp_path: Path):
    with connect_database(tmp_path / "data.db", read_only=True) as conn:
        att = conn.execute(
            "SELECT attachment_name, size_bytes, content_hash, storage_path "
            "FROM message_attachments"
        ).fetchall()
        open_failures = conn.execute(
            "SELECT COUNT(*) FROM ingest_failures WHERE resolved_at IS NULL "
            "AND abandon_reason IS NULL AND error_stage = 'attachment-store'"
        ).fetchone()[0]
    return [tuple(r) for r in att], open_failures


def test_cas_failure_then_healthy_reimport_repairs_placeholder(
    tmp_path: Path, monkeypatch
) -> None:
    from mailattic import attachment_store

    real_write = attachment_store.write_attachment

    def failing_write(*args, **kwargs):
        raise OSError("simulated CAS failure")

    monkeypatch.setattr(attachment_store, "write_attachment", failing_write)
    first = _import_again(tmp_path, _pdf_item())
    assert first.imported_messages == 1
    assert first.failure_count == 1
    assert _attachment_state(tmp_path)[1] == 1

    monkeypatch.setattr(attachment_store, "write_attachment", real_write)
    second = _import_again(tmp_path, _pdf_item())
    assert second.duplicate_messages == 1
    assert second.recovered_attachments == 1
    assert second.failure_count == 0
    rows, open_failures = _attachment_state(tmp_path)
    assert len(rows) == 1
    name, size, content_hash, storage_path = rows[0]
    assert (name, size) == ("quote.pdf", len(b"healthy PDF bytes"))
    assert content_hash and storage_path
    assert open_failures == 0

    third = _import_again(tmp_path, _pdf_item())
    assert third.recovered_attachments == 0
    assert len(_attachment_state(tmp_path)[0]) == 1


def test_unreadable_attachment_bytes_keep_size_unknown(tmp_path: Path) -> None:
    from mailattic.outlook_pst import OutlookComReader as Reader

    item = _pdf_item(data=None)
    reader = Reader(attachment_root=tmp_path / "attachments")
    extracted = reader._read_attachments(item.Attachments)
    assert extracted[0]["_decode_error"] is True
    assert "_expected_size" not in extracted[0]


def test_zero_attachment_count_adds_no_failure(tmp_path: Path) -> None:
    item = SimpleNamespace(Class=43, Attachments=SimpleNamespace(Count=0), **_fields())
    summary, counts, failures, _ = _run(tmp_path, item)

    assert counts == {"messages": 1, "attachments": 0}
    assert failures == []
    assert summary.failure_count == 0


# --- #20: unreadable FileName must not drop a healthy attachment payload ---


class _RaisingName:
    """Attachment whose FileName getter raises."""

    def __init__(self, props: dict) -> None:
        self.PropertyAccessor = _Accessor(props)

    @property
    def FileName(self):  # noqa: N802
        raise RuntimeError("simulated COM FileName unavailable")


def _unnamed_item(kinds: list[str], payloads: list[bytes | None]):
    from mailattic import outlook_pst

    atts = []
    for kind, data in zip(kinds, payloads, strict=True):
        props = {outlook_pst.PR_ATTACH_MIME_TAG: "application/pdf"}
        if data is not None:
            props[outlook_pst.PR_ATTACH_DATA_BIN] = data
        if kind == "raise":
            atts.append(_RaisingName(props))
        else:
            atts.append(SimpleNamespace(FileName="", PropertyAccessor=_Accessor(props)))
    collection = SimpleNamespace(Count=len(atts), Item=lambda i: atts[i - 1])
    return SimpleNamespace(Class=43, Attachments=collection, **_fields())


def _rows(tmp_path: Path):
    with connect_database(tmp_path / "data.db", read_only=True) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT attachment_name, size_bytes, storage_path "
                "FROM message_attachments ORDER BY id"
            )
        ]


def test_unreadable_filename_keeps_attachment_with_stable_distinct_names(
    tmp_path: Path,
) -> None:
    item = _unnamed_item(["raise", "empty"], [b"first bytes", b"second bytes"])
    summary = _import_again(tmp_path, item)

    rows = _rows(tmp_path)
    assert summary.imported_messages == 1
    assert [r[1] for r in rows] == [len(b"first bytes"), len(b"second bytes")]
    assert len({r[0] for r in rows}) == 2
    assert all(r[0] and r[2] for r in rows)
    assert summary.failure_count == 0

    # Same input again: names are positional, so a re-import is idempotent.
    again = _import_again(
        tmp_path, _unnamed_item(["raise", "empty"], [b"first bytes", b"second bytes"])
    )
    assert _rows(tmp_path) == rows
    assert again.imported_messages == 0


def test_unreadable_filename_and_bytes_leaves_failure_evidence(
    tmp_path: Path,
) -> None:
    summary = _import_again(tmp_path, _unnamed_item(["raise"], [None]))

    assert summary.failure_count == 1
    assert len(_rows(tmp_path)) == 1
    with connect_database(tmp_path / "data.db", read_only=True) as conn:
        stages = [r[0] for r in conn.execute("SELECT error_stage FROM ingest_failures")]
    assert stages == ["attachment-decode"]

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mailattic import outlook_pst
from mailattic.ingest import import_pst_sources
from mailattic.models import PstSource
from mailattic.outlook_pst import (
    PR_ADDRTYPE,
    PR_EMAIL_ADDRESS,
    PR_INTERNET_MESSAGE_ID,
    PR_SMTP_ADDRESS,
    OutlookComReader,
    OutlookPstParser,
)
from mailattic.storage import initialize_database


class _FakeCollection:
    def __init__(self, *items: object) -> None:
        self._items = items
        self.Count = len(items)

    def Item(self, index: int) -> object:
        return self._items[index - 1]


class _FakeAccessor:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def GetProperty(self, name: str) -> object:
        if name not in self._values:
            raise KeyError(name)
        return self._values[name]


class _FakeFolder:
    def __init__(
        self,
        name: str,
        *,
        items: tuple[object, ...] = (),
        folders: tuple[_FakeFolder, ...] = (),
    ) -> None:
        self.Name = name
        self.Items = _FakeCollection(*items)
        self.Folders = _FakeCollection(*folders)


class _FakeStore:
    def __init__(self, file_path: Path, root: _FakeFolder) -> None:
        self.FilePath = str(file_path)
        self._root = root

    def GetRootFolder(self) -> _FakeFolder:
        return self._root


class _FakeNamespace:
    def __init__(self, *stores: _FakeStore) -> None:
        self.Stores = _FakeCollection(*stores)


def _build_fake_com_reader(pst_path: Path) -> OutlookComReader:
    recipient = SimpleNamespace(
        Name="Client",
        Type=1,
        PropertyAccessor=_FakeAccessor(
            {
                PR_SMTP_ADDRESS: "client@example.com",
                PR_EMAIL_ADDRESS: "client@example.com",
                PR_ADDRTYPE: "SMTP",
            }
        ),
    )
    wall_clock_sent = datetime(2026, 1, 21, 14, 48, tzinfo=UTC)
    item = SimpleNamespace(
        Class=43,
        EntryID="entry-tz-001",
        Subject="Timezone guard check",
        SenderName="Sean Example",
        SenderEmailType="SMTP",
        SenderEmailAddress="sean@example.com",
        SentOn=wall_clock_sent,
        ReceivedTime=wall_clock_sent,
        Body="Fake COM body",
        To="Client",
        CC="",
        BCC="",
        Recipients=_FakeCollection(recipient),
        Attachments=_FakeCollection(),
        PropertyAccessor=_FakeAccessor(
            {
                PR_INTERNET_MESSAGE_ID: "<tz-guard-001@example.com>",
            }
        ),
    )
    inbox = _FakeFolder("Inbox", items=(item,))
    root = _FakeFolder("Archive", folders=(inbox,))
    return OutlookComReader(namespace=_FakeNamespace(_FakeStore(pst_path, root)))


def test_pst_import_succeeds_and_labels_plus_08_when_machine_offset_is_utc_plus_8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        outlook_pst, "get_machine_utc_offset", lambda: timedelta(hours=8)
    )
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "tz_ok.db")
    reader = _build_fake_com_reader(pst_path)

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=tmp_path / "recipient-extraction",
    )

    assert summary.imported_messages == 1
    assert summary.failure_count == 0
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute("SELECT sent_at, received_at FROM messages").fetchone()
    finally:
        connection.close()
    assert row == ("2026-01-21T14:48:00+08:00", "2026-01-21T14:48:00+08:00")


@pytest.mark.parametrize(
    ("simulated_offset", "expected_label"),
    [
        (timedelta(hours=9), "UTC+09:00"),
        (timedelta(0), "UTC+00:00"),
    ],
)
def test_pst_import_fails_loudly_before_writing_when_machine_offset_is_not_utc_plus_8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    simulated_offset: timedelta,
    expected_label: str,
) -> None:
    monkeypatch.setattr(outlook_pst, "get_machine_utc_offset", lambda: simulated_offset)
    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "tz_fail.db")
    summary_root = tmp_path / "recipient-extraction"
    reader = _build_fake_com_reader(pst_path)

    with pytest.raises(RuntimeError, match=expected_label.replace("+", r"\+")):
        import_pst_sources(
            database_path=database_path,
            sources=[source],
            reader=reader,
            summary_root=summary_root,
        )

    connection = sqlite3.connect(database_path)
    try:
        message_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[
            0
        ]
        failure_count = connection.execute(
            "SELECT COUNT(*) FROM ingest_failures"
        ).fetchone()[0]
    finally:
        connection.close()
    assert message_count == 0
    assert failure_count == 0
    assert not summary_root.exists()


def test_outlook_reader_and_parser_reject_non_taipei_offset_before_iterating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(outlook_pst, "get_machine_utc_offset", lambda: timedelta(0))
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    reader = OutlookComReader(namespace=SimpleNamespace())
    parser = OutlookPstParser(reader=reader)

    with pytest.raises(RuntimeError, match=r"UTC\+00:00"):
        reader.iter_messages(source)

    with pytest.raises(RuntimeError, match=r"UTC\+00:00"):
        parser.iter_extraction_results(source)

    with pytest.raises(RuntimeError, match=r"UTC\+00:00"):
        parser.extract(source)

"""Characterization tests for issue #90.

Pins the root-folder-name skip decision in ``_find_pst_folder_by_path``.
"""

from __future__ import annotations

from pathlib import Path

from mailattic.models import PstSource, build_normalized_message
from mailattic.original_export import export_original_message
from mailattic.outlook_pst import PR_INTERNET_MESSAGE_ID
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)


class _FakeCollection:
    def __init__(self, *values: object) -> None:
        self._values = list(values)
        self.Count = len(values)

    def Item(self, index: int) -> object:
        value = self._values[index - 1]
        if isinstance(value, Exception):
            raise value
        return value


class _FakeTableColumns:
    def RemoveAll(self) -> None:
        return None

    def Add(self, _name: str) -> None:
        return None


class _FakeTable:
    def __init__(self, rows: list[tuple[object, object]]) -> None:
        self._rows = rows
        self._pos = 0
        self.Columns = _FakeTableColumns()

    @property
    def EndOfTable(self) -> bool:
        return self._pos >= len(self._rows)

    def GetArray(self, max_rows: int) -> list[tuple[object, object]]:
        batch = self._rows[self._pos : self._pos + max_rows]
        self._pos += len(batch)
        return batch


class _FakeAccessor:
    def __init__(self, message_id: str) -> None:
        self._message_id = message_id

    def GetProperty(self, name: str) -> object:
        if name == PR_INTERNET_MESSAGE_ID:
            return self._message_id
        raise KeyError(name)


class _FakeItem:
    def __init__(self, entry_id: str, message_id: str) -> None:
        self.Class = 43
        self.EntryID = entry_id
        self.Subject = "Target Subject"
        self.PropertyAccessor = _FakeAccessor(message_id)

    def SaveAs(self, path: str, _save_type: int = 3) -> None:
        Path(path).write_bytes(f"MSG:{self.EntryID}".encode())


class _FakeItems:
    def __init__(self, count: int) -> None:
        self.Count = count


class _FakeFolder:
    def __init__(
        self,
        name: str,
        rows: list[tuple[object, object]] | None = None,
        children: list[object] | None = None,
    ) -> None:
        self.Name = name
        self.EntryID = f"folder-{name}"
        self._rows = rows or []
        self.Items = _FakeItems(len(self._rows))
        self.Folders = _FakeCollection(*(children or []))

    def GetTable(self) -> _FakeTable:
        return _FakeTable(self._rows)


class _FakeStore:
    def __init__(self, file_path: str, root: _FakeFolder) -> None:
        self.FilePath = file_path
        self.DisplayName = "Archive"
        self.StoreID = "store-1"
        self._root = root

    def GetRootFolder(self) -> _FakeFolder:
        return self._root


class _FakeNamespace:
    def __init__(
        self,
        store: _FakeStore,
        items_by_id: dict[str, _FakeItem],
    ) -> None:
        self.Stores = _FakeCollection(store)
        self._items_by_id = items_by_id

    def GetItemFromID(self, entry_id: str, _store_id: str | None = None) -> object:
        item = self._items_by_id.get(entry_id)
        if item is None:
            raise KeyError(entry_id)
        return item


class _FakeOutlookReader:
    def __init__(self, namespace: _FakeNamespace) -> None:
        self.namespace = namespace


def _seed_db(tmp_path: Path, pst_path: str, folder_path: str) -> tuple[Path, int]:
    db_path = initialize_database(tmp_path / "mail.db")
    source = PstSource(
        path=Path(pst_path),
        input_mode="file",
        requested_from=Path(pst_path),
    )
    msg = build_normalized_message(
        {
            "subject": "Target Subject",
            "sender": {"email": "alice@example.com", "name": "Alice"},
            "to": [{"email": "bob@example.com"}],
            "sent_at": "2026-05-10T14:30:00+08:00",
            "body_text": "Target body",
            "message_id": "<target@example.com>",
        },
        source=source,
        folder_path=folder_path,
        logical_locator="stale-entry",
    )
    with connect_database(db_path) as conn:
        db_id = persist_message(conn, msg).stored_message_id
        conn.commit()
    return db_path, db_id


def test_root_name_skipped_when_no_same_name_child_and_no_read_error(
    tmp_path: Path,
) -> None:
    pst_path = str(tmp_path / "archive.pst")
    db_path, db_id = _seed_db(tmp_path, pst_path, folder_path="Personal Folders/Inbox")
    inbox = _FakeFolder("Inbox", rows=[("<target@example.com>", "entry-inbox")])
    other = _FakeFolder("Other", rows=[("<target@example.com>", "entry-other")])
    root = _FakeFolder("Personal Folders", children=[inbox, other])
    namespace = _FakeNamespace(
        _FakeStore(pst_path, root),
        {
            "entry-inbox": _FakeItem("entry-inbox", "<target@example.com>"),
            "entry-other": _FakeItem("entry-other", "<target@example.com>"),
        },
    )

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=_FakeOutlookReader(namespace),
    )

    assert result.status == "exported"
    assert result.dest_path is not None
    assert result.dest_path.read_bytes() == b"MSG:entry-inbox"


def test_root_name_not_skipped_when_same_name_child_exists(tmp_path: Path) -> None:
    pst_path = str(tmp_path / "archive.pst")
    db_path, db_id = _seed_db(tmp_path, pst_path, folder_path="Personal Folders/Inbox")
    nested_inbox = _FakeFolder("Inbox", rows=[("<target@example.com>", "entry-nested")])
    same_name_child = _FakeFolder("Personal Folders", children=[nested_inbox])
    root_inbox = _FakeFolder("Inbox", rows=[("<target@example.com>", "entry-root")])
    root = _FakeFolder("Personal Folders", children=[same_name_child, root_inbox])
    namespace = _FakeNamespace(
        _FakeStore(pst_path, root),
        {
            "entry-nested": _FakeItem("entry-nested", "<target@example.com>"),
            "entry-root": _FakeItem("entry-root", "<target@example.com>"),
        },
    )

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=_FakeOutlookReader(namespace),
    )

    assert result.status == "exported"
    assert result.dest_path is not None
    assert result.dest_path.read_bytes() == b"MSG:entry-nested"


def test_root_name_not_skipped_when_read_failure_occurs_at_root_level(
    tmp_path: Path,
) -> None:
    pst_path = str(tmp_path / "archive.pst")
    db_path, db_id = _seed_db(tmp_path, pst_path, folder_path="Personal Folders/Inbox")
    inbox = _FakeFolder("Inbox", rows=[("<target@example.com>", "entry-inbox")])
    other = _FakeFolder("Other", rows=[("<target@example.com>", "entry-other")])
    root = _FakeFolder(
        "Personal Folders",
        children=[RuntimeError("MAPI_E_FAIL"), inbox, other],
    )
    namespace = _FakeNamespace(
        _FakeStore(pst_path, root),
        {
            "entry-inbox": _FakeItem("entry-inbox", "<target@example.com>"),
            "entry-other": _FakeItem("entry-other", "<target@example.com>"),
        },
    )

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=_FakeOutlookReader(namespace),
    )

    assert result.status == "failed"
    assert result.dest_path is None
    assert result.detail is not None
    assert "Ambiguous:" in result.detail

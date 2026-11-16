"""Tests for issue #89: attach PST read failure reason to detail when unconfirmed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        return self._values[index - 1]


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
    def __init__(self, message_id: str | Exception) -> None:
        self._message_id = message_id

    def GetProperty(self, name: str) -> object:
        if name == PR_INTERNET_MESSAGE_ID:
            if isinstance(self._message_id, Exception):
                raise self._message_id
            return self._message_id
        raise KeyError(name)


class _FakeItem:
    def __init__(self, message_id: str | Exception) -> None:
        self.PropertyAccessor = _FakeAccessor(message_id)


class _FakeItems:
    def __init__(self, count: int) -> None:
        self.Count = count


class _FakeFolder:
    def __init__(
        self,
        name: str,
        rows: list[tuple[object, object]] | None = None,
        children: list[_FakeFolder] | None = None,
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
        items_by_id: dict[str, object],
    ) -> None:
        self.Stores = _FakeCollection(store)
        self._items_by_id = items_by_id

    def GetItemFromID(self, entry_id: str, _store_id: str | None = None) -> object:
        value = self._items_by_id.get(entry_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise KeyError(entry_id)
        return value


class _FakeOutlookReader:
    def __init__(self, namespace: _FakeNamespace) -> None:
        self.namespace = namespace


def _seed_db(tmp_path: Path, pst_path: str) -> tuple[Path, int]:
    db_path = initialize_database(tmp_path / "mail.db")
    source = PstSource(
        path=Path(pst_path),
        input_mode="file",
        requested_from=Path(pst_path),
    )
    msg = build_normalized_message(
        {
            "subject": "Confidential Subject",
            "sender": {"email": "alice@example.com", "name": "Alice"},
            "to": [{"email": "bob@example.com"}],
            "sent_at": "2026-05-10T14:30:00+08:00",
            "body_text": "Confidential body",
            "message_id": "<target@example.com>",
        },
        source=source,
        folder_path="Inbox",
        logical_locator="stale-entry",
    )
    with connect_database(db_path) as conn:
        db_id = persist_message(conn, msg).stored_message_id
        conn.commit()
    return db_path, db_id


def _run_case(
    tmp_path: Path,
    rows: list[tuple[object, object]],
    items_by_id: dict[str, Any],
):
    pst_path = str(tmp_path / "archive.pst")
    db_path, db_id = _seed_db(tmp_path, pst_path)
    inbox = _FakeFolder("Inbox", rows=rows)
    root = _FakeFolder("Root", children=[inbox])
    namespace = _FakeNamespace(_FakeStore(pst_path, root), items_by_id)
    return export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=_FakeOutlookReader(namespace),
    )


def test_confirm_stage_get_item_exception_appends_reason(tmp_path: Path) -> None:
    result = _run_case(
        tmp_path,
        rows=[("<target@example.com>", "hit-1")],
        items_by_id={"hit-1": RuntimeError("MAPI_E_CORRUPT_DATA")},
    )
    assert result.status == "failed"
    assert result.detail is not None
    assert "A matching table row was found but could not be confirmed." in result.detail
    assert "RuntimeError" in result.detail
    assert "MAPI_E_CORRUPT_DATA" in result.detail


def test_confirm_stage_verification_exception_unwraps_cause(tmp_path: Path) -> None:
    result = _run_case(
        tmp_path,
        rows=[("<target@example.com>", "hit-1")],
        items_by_id={"hit-1": _FakeItem(OSError("property stream unreadable"))},
    )
    assert result.status == "failed"
    assert result.detail is not None
    assert "A matching table row was found but could not be confirmed." in result.detail
    assert "OSError" in result.detail
    assert "property stream unreadable" in result.detail
    assert "_VerificationError" not in result.detail


def test_hidden_stage_get_item_exception_appends_reason(tmp_path: Path) -> None:
    result = _run_case(
        tmp_path,
        rows=[(None, "hidden-1")],
        items_by_id={"hidden-1": ValueError("invalid entry payload")},
    )
    assert result.status == "failed"
    assert result.detail is not None
    assert (
        "Target item not found and 1 items could not be read due to errors"
        in result.detail
    )
    assert "ValueError" in result.detail
    assert "invalid entry payload" in result.detail


def test_hidden_stage_verification_exception_unwraps_cause(tmp_path: Path) -> None:
    result = _run_case(
        tmp_path,
        rows=[(None, "hidden-1")],
        items_by_id={"hidden-1": _FakeItem(TimeoutError("outlook rpc timed out"))},
    )
    assert result.status == "failed"
    assert result.detail is not None
    assert (
        "Target item not found and 1 items could not be read due to errors"
        in result.detail
    )
    assert "TimeoutError" in result.detail
    assert "outlook rpc timed out" in result.detail
    assert "_VerificationError" not in result.detail


def test_overlong_exception_message_is_truncated(tmp_path: Path) -> None:
    long_msg = "x" * 500
    result = _run_case(
        tmp_path,
        rows=[("<target@example.com>", "hit-1")],
        items_by_id={"hit-1": RuntimeError(long_msg)},
    )
    assert result.status == "failed"
    assert result.detail is not None
    assert "RuntimeError" in result.detail
    assert long_msg not in result.detail
    assert "..." in result.detail
    assert len(result.detail) <= 280


def test_confirm_stage_mismatch_without_exception_keeps_detail_unchanged(
    tmp_path: Path,
) -> None:
    result = _run_case(
        tmp_path,
        rows=[("<target@example.com>", "hit-1")],
        items_by_id={"hit-1": _FakeItem("<other@example.com>")},
    )
    assert result.status == "failed"
    assert result.detail == "A matching table row was found but could not be confirmed."

"""Tests for issue #53: Original export (匯出原信).

Retrieves one stored message's original from its source:
- PST source via Outlook COM -> `.msg`
- IMAP source via IMAP server -> `.eml`
- Neither found -> result carries subject, sender, sent_at without error
- Ambiguous/failed location never exports a different message
- Read/write failures are reported as failures, not success.
"""

from __future__ import annotations

import imaplib
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from mailattic import original_export
from mailattic.models import MaildirSource, PstSource, build_normalized_message
from mailattic.original_export import (
    export_original_message,
)
from mailattic.outlook_pst import PR_INTERNET_MESSAGE_ID
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)


class FakeCollection:
    def __init__(self, *values: object) -> None:
        self._values = list(values)
        self.Count = len(values)

    def Item(self, index: int) -> object:
        return self._values[index - 1]

    def __iter__(self):
        return iter(self._values)


class FakeAccessor:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self._values = values or {}

    def GetProperty(self, name: str) -> object:
        if name not in self._values:
            raise KeyError(name)
        value = self._values[name]
        if isinstance(value, Exception):
            raise value
        return value


class FakeOutlookItem:
    def __init__(
        self,
        entry_id: str,
        subject: str,
        message_id: str | None = None,
        body: str = "Body text",
        sender_email: str = "sender@example.com",
        sent_on: datetime | None = None,
    ) -> None:
        self.Class = 43
        self.EntryID = entry_id
        self.Subject = subject
        self.Body = body
        self.SenderEmailAddress = sender_email
        self.SenderEmailType = "SMTP"
        self.SentOn = sent_on
        self.ReceivedTime = None
        props: dict[str, object] = {}
        if message_id:
            props[PR_INTERNET_MESSAGE_ID] = message_id
        self.PropertyAccessor = FakeAccessor(props)
        self.saved_calls: list[tuple[str, int]] = []

    def SaveAs(self, path: str, save_type: int = 3) -> None:
        self.saved_calls.append((path, save_type))
        Path(path).write_bytes(f"MSG-BYTES:{self.Subject}:{self.EntryID}".encode())


class FakeOutlookFolder:
    def __init__(self, name: str, items: list[FakeOutlookItem] | None = None) -> None:
        self.Name = name
        self._items = items or []
        self.Items = FakeCollection(*self._items)
        self.Folders = FakeCollection()

    def GetTable(self):
        if self.Items is None:
            raise RuntimeError("unreadable folder")
        self.table = FakeTable(self._items, self.registry)
        return self.table


class FakeTable:
    def __init__(self, items, registry):
        self.items = items
        self.registry = registry
        self.Columns = self
        self.EndOfTable = not items
        self.columns = []
        self.position = 0
        self.calls = 0

    def RemoveAll(self):
        self.columns.clear()

    def Add(self, name):
        self.columns.append(name)

    def GetArray(self, count):
        self.calls += 1
        rows = []
        for item in self.items[self.position : self.position + count]:
            try:
                value = item.PropertyAccessor.GetProperty(PR_INTERNET_MESSAGE_ID)
            except Exception:
                value = None
            value = getattr(item, "table_message_id", value)
            self.registry[item.EntryID] = item
            values = {PR_INTERNET_MESSAGE_ID: value, "EntryID": item.EntryID}
            rows.append(tuple(values[name] for name in self.columns))
        self.position += len(rows)
        self.EndOfTable = self.position >= len(self.items)
        return rows


class FakeOutlookStore:
    def __init__(self, file_path: str, root_folder: FakeOutlookFolder) -> None:
        self.FilePath = file_path
        self.StoreID = f"store-id-{file_path}"
        self._root_folder = root_folder

    def GetRootFolder(self) -> FakeOutlookFolder:
        return self._root_folder


class FakeOutlookNamespace:
    def __init__(
        self,
        stores: list[FakeOutlookStore],
        items_by_id: dict[str, FakeOutlookItem] | None = None,
    ) -> None:
        self.Stores = FakeCollection(*stores)
        self._items_by_id = items_by_id or {}
        self._emitted = {}

        def attach(folder):
            folder.registry = self._emitted
            if folder.Folders:
                for child in folder.Folders:
                    attach(child)

        for store in stores:
            with suppress(Exception):
                attach(store.GetRootFolder())

    def GetItemFromID(
        self, entry_id: str, store_id: str | None = None
    ) -> FakeOutlookItem | None:
        if entry_id in self._items_by_id:
            return self._items_by_id[entry_id]

        if entry_id in self._emitted:
            return self._emitted[entry_id]
        raise RuntimeError(f"Item not found: {entry_id}")


class FakeOutlookReader:
    def __init__(self, namespace: FakeOutlookNamespace) -> None:
        self.namespace = namespace


class FakeImapConnection:
    def __init__(
        self,
        rfc822_by_uid: dict[int, bytes],
        uids_by_header: dict[str, list[int]] | None = None,
    ) -> None:
        self.rfc822_by_uid = rfc822_by_uid
        self.uids_by_header = uids_by_header or {}
        self.selected_folder: str | None = None

    def select(self, folder: str, readonly: bool = True) -> tuple[str, list[object]]:
        self.selected_folder = folder
        return "OK", [b"1"]

    def uid(self, command: str, *args: str) -> tuple[str, list[object]]:
        if command.upper() == "FETCH":
            uid = int(args[0])
            if uid in self.rfc822_by_uid:
                body = self.rfc822_by_uid[uid]
                return "OK", [(b"1 (BODY[]", body)]
            return "NO", [b"UID not found"]
        if command.upper() == "SEARCH":
            # e.g. SEARCH HEADER Message-ID <foo@bar>
            search_str = " ".join(args)
            for key, uids in self.uids_by_header.items():
                if key in search_str:
                    uid_str = " ".join(str(u) for u in uids).encode("ascii")
                    return "OK", [uid_str]
            return "OK", [b""]
        return "NO", [b"Unknown command"]


class FakeImapClient:
    def __init__(self, connection: FakeImapConnection) -> None:
        self._conn = connection
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def select_folder(self, folder_name: str) -> None:
        self._conn.select(folder_name, readonly=True)

    def search_uids_by_message_id(self, term: str) -> list[int]:
        typ, data = self._conn.uid("SEARCH", "HEADER", "Message-ID", f'"{term}"')
        if typ != "OK":
            raise RuntimeError(f"UID SEARCH failed with status {typ!r}: {data}")
        raw = data[0] if data and data[0] else b""
        assert isinstance(raw, bytes)
        return sorted(int(u) for u in raw.decode("ascii").split() if u.isdigit())

    def fetch_rfc822(self, uid: int) -> bytes:
        typ, data = self._conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data:
            raise RuntimeError(f"UID FETCH failed for UID {uid}")
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                body = item[1]
                if isinstance(body, bytes):
                    return body
        raise RuntimeError(f"Could not extract RFC822 for UID {uid}")

    def list_folders(self) -> list[Any]:
        return []


def _seed_pst_message(
    tmp_path: Path,
    *,
    pst_path: str = "C:/mail/archive_2024.pst",
    folder_path: str = "Inbox",
    entry_id: str = "entry-101",
    message_id: str = "<msg101@example.com>",
    subject: str = "PST Message Subject",
    sender_email: str = "alice@example.com",
    sent_at: str = "2026-05-10T14:30:00+08:00",
) -> tuple[Path, int]:
    db_path = initialize_database(tmp_path / "mail.db")
    source = PstSource(
        path=Path(pst_path),
        input_mode="file",
        requested_from=Path(pst_path),
    )
    msg = build_normalized_message(
        {
            "subject": subject,
            "sender": {"email": sender_email, "name": "Alice"},
            "to": [{"email": "bob@example.com"}],
            "sent_at": sent_at,
            "body_text": "Sample PST body",
            "message_id": message_id,
        },
        source=source,
        folder_path=folder_path,
        logical_locator=entry_id,
    )
    conn = connect_database(db_path)
    try:
        persist_result = persist_message(conn, msg)
        conn.commit()
        db_id = persist_result.stored_message_id
    finally:
        conn.close()
    return db_path, db_id


def _seed_imap_message(
    tmp_path: Path,
    *,
    account_email: str = "sean3808@mailattic.test",
    folder_name: str = "INBOX",
    uid: int = 42,
    message_id: str = "<imap42@mailattic.test>",
    subject: str = "IMAP Message Subject",
    sender_email: str = "charlie@example.com",
    sent_at: str = "2026-06-15T09:00:00+08:00",
) -> tuple[Path, int]:
    db_path = initialize_database(tmp_path / "mail.db")
    source = MaildirSource(
        path=tmp_path / "maildir",
        account_email=account_email,
        account_name="sean3808",
        folder_name=folder_name,
        requested_from=tmp_path / "maildir",
    )
    msg = build_normalized_message(
        {
            "subject": subject,
            "sender": {"email": sender_email, "name": "Charlie"},
            "to": [{"email": "sean3808@mailattic.test"}],
            "sent_at": sent_at,
            "body_text": "Sample IMAP body",
            "message_id": message_id,
        },
        source=source,
        folder_path=folder_name,
        logical_locator=f"{folder_name}/cur/uid-{uid}.eml",
    )
    conn = connect_database(db_path)
    try:
        persist_result = persist_message(conn, msg)
        conn.commit()
        db_id = persist_result.stored_message_id
    finally:
        conn.close()
    return db_path, db_id


def test_export_pst_message_success_saves_msg(tmp_path: Path) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-101",
        message_id="<msg101@example.com>",
        subject="Order Table Details",
    )

    fake_item = FakeOutlookItem(
        entry_id="entry-101",
        subject="Order Table Details",
        message_id="<msg101@example.com>",
    )
    inbox = FakeOutlookFolder("Inbox", [fake_item])
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    store = FakeOutlookStore(pst_file, root)
    namespace = FakeOutlookNamespace([store], items_by_id={"entry-101": fake_item})
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )

    assert result.status == "exported"
    assert result.format == "msg"
    assert result.source_type == "pst"
    assert result.dest_path is not None
    assert result.dest_path.exists()
    assert result.dest_path.suffix.lower() == ".msg"
    assert "2026-05-10" in result.dest_path.name
    assert f"msg{db_id}" in result.dest_path.name
    assert result.dest_path.read_bytes().startswith(b"MSG-BYTES:Order Table Details")


def test_export_imap_message_success_saves_eml(tmp_path: Path) -> None:
    db_path, db_id = _seed_imap_message(
        tmp_path,
        uid=42,
        message_id="<imap42@mailattic.test>",
        subject="Invoice #1234",
    )

    raw_rfc822 = (
        b"From: Charlie <charlie@example.com>\r\n"
        b"To: sean3808@mailattic.test\r\n"
        b"Subject: Invoice #1234\r\n"
        b"Message-ID: <imap42@mailattic.test>\r\n"
        b"Date: Mon, 15 Jun 2026 09:00:00 +0800\r\n"
        b"\r\n"
        b"Original raw email body with tables"
    )
    fake_conn = FakeImapConnection(
        rfc822_by_uid={42: raw_rfc822},
        uids_by_header={"imap42@mailattic.test": [42]},
    )
    imap_client = FakeImapClient(fake_conn)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        imap_client=imap_client,
    )

    assert result.status == "exported"
    assert result.format == "eml"
    assert result.source_type == "imap"
    assert result.dest_path is not None
    assert result.dest_path.exists()
    assert result.dest_path.suffix.lower() == ".eml"
    assert result.dest_path.read_bytes() == raw_rfc822


def test_neither_found_returns_subject_sender_date_without_error(
    tmp_path: Path,
) -> None:
    pst_file = str(tmp_path / "missing_archive.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        subject="Old Deleted Memo",
        sender_email="old@example.com",
        sent_at="2024-01-15T10:00:00+08:00",
    )

    # The PST is mounted and searched completely; the message is not in it.
    # (A PST that is not mounted is "failed", #53 ruling 6.)
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore(pst_file, FakeOutlookFolder("Root"))], items_by_id={}
    )
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )

    assert result.status == "not_found"
    assert result.dest_path is None
    assert result.subject == "Old Deleted Memo"
    assert "old@example.com" in result.sender
    assert result.sent_at == "2024-01-15T10:00:00+08:00"


def test_location_mismatch_never_exports_different_message(tmp_path: Path) -> None:
    """When stored logical_locator points to an item with different Message-ID,
    and no candidate matches target Message-ID, never export the wrong message.
    """
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-stale",
        message_id="<target@example.com>",
        subject="Target Subject",
    )

    # Stale entry points to a different message
    wrong_item = FakeOutlookItem(
        entry_id="entry-stale",
        subject="Different Wrong Mail",
        message_id="<different@example.com>",
    )
    inbox = FakeOutlookFolder("Inbox", [wrong_item])
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    store = FakeOutlookStore(pst_file, root)
    namespace = FakeOutlookNamespace([store], items_by_id={"entry-stale": wrong_item})
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )

    # Must NOT export wrong_item
    assert result.status == "not_found"
    assert result.dest_path is None
    assert not any(dest_dir.glob("*.msg"))


def test_location_mismatch_falls_back_to_message_id_match(tmp_path: Path) -> None:
    """When locator is stale, but Message-ID matches another item, export that item."""
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-stale",
        message_id="<target@example.com>",
        subject="Target Subject",
    )

    stale_item = FakeOutlookItem(
        entry_id="entry-stale",
        subject="Moved Mail",
        message_id="<different@example.com>",
    )
    correct_item = FakeOutlookItem(
        entry_id="entry-new",
        subject="Target Subject",
        message_id="<target@example.com>",
    )
    inbox = FakeOutlookFolder("Inbox", [stale_item, correct_item])
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    store = FakeOutlookStore(pst_file, root)
    namespace = FakeOutlookNamespace([store], items_by_id={"entry-stale": stale_item})
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )

    assert result.status == "exported"
    assert result.dest_path is not None
    assert result.dest_path.read_bytes().startswith(
        b"MSG-BYTES:Target Subject:entry-new"
    )


def test_ambiguous_message_id_with_differing_bodies_fails(tmp_path: Path) -> None:
    """When multiple items share Message-ID with differing bodies, report failure."""
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-unknown",
        message_id="<duplicate@example.com>",
    )

    candidate1 = FakeOutlookItem(
        entry_id="c1",
        subject="Subject A",
        message_id="<duplicate@example.com>",
        body="Body 1",
    )
    candidate2 = FakeOutlookItem(
        entry_id="c2",
        subject="Subject B",
        message_id="<duplicate@example.com>",
        body="Body 2",
    )
    inbox = FakeOutlookFolder("Inbox", [candidate1, candidate2])
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    store = FakeOutlookStore(pst_file, root)
    namespace = FakeOutlookNamespace([store], items_by_id={})
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )

    assert result.status == "failed"
    assert "ambiguous" in str(result.detail).lower()
    assert not any(dest_dir.glob("*.msg"))


def test_skip_existing_leaves_destination_untouched(tmp_path: Path) -> None:
    pst_file = str(tmp_path / "archive.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-1",
        subject="Subject Once",
    )
    fake_item = FakeOutlookItem(
        entry_id="entry-1", subject="Subject Once", message_id="<msg101@example.com>"
    )
    inbox = FakeOutlookFolder("Inbox", [fake_item])
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    store = FakeOutlookStore(pst_file, root)
    namespace = FakeOutlookNamespace([store], items_by_id={"entry-1": fake_item})
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    dest_dir.mkdir(parents=True)
    # Pre-create destination file with pre-existing content
    # First get the filename that would be used
    first_run = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )
    assert first_run.status == "exported"
    assert first_run.dest_path is not None
    first_run.dest_path.write_bytes(b"EXISTING_USER_CONTENT")

    # Second run with skip_existing=True
    second_run = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        skip_existing=True,
        outlook_reader=reader,
    )
    assert second_run.status == "skipped-existing"
    assert second_run.dest_path == first_run.dest_path
    assert second_run.dest_path.read_bytes() == b"EXISTING_USER_CONTENT"


def test_destination_exists_without_overwrite_or_skip_fails(tmp_path: Path) -> None:
    pst_file = str(tmp_path / "archive.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-1",
        subject="Subject Once",
    )
    fake_item = FakeOutlookItem(
        entry_id="entry-1", subject="Subject Once", message_id="<msg101@example.com>"
    )
    inbox = FakeOutlookFolder("Inbox", [fake_item])
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    store = FakeOutlookStore(pst_file, root)
    namespace = FakeOutlookNamespace([store], items_by_id={"entry-1": fake_item})
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    first_run = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )
    assert first_run.status == "exported"

    # Second run without overwrite or skip_existing
    second_run = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        overwrite=False,
        skip_existing=False,
        outlook_reader=reader,
    )
    assert second_run.status == "failed"
    assert "exists" in str(second_run.detail).lower()


def test_cli_export_original_conflicting_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mailattic.cli import main

    db_path = initialize_database(tmp_path / "cli.db")
    exit_code = main(
        [
            "export-original",
            "1",
            "--database",
            str(db_path),
            "--overwrite",
            "--skip-existing",
        ]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "cannot use both --overwrite and --skip-existing" in captured.err


def test_cli_export_original_neither_found(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mailattic import original_export
    from mailattic.cli import main

    db_path, db_id = _seed_pst_message(
        tmp_path,
        subject="Missing Quotation",
        sender_email="vendor@example.com",
        sent_at="2026-04-01T10:00:00+08:00",
    )
    # Mounted and searched completely, message absent (#53 ruling 6: an
    # unmounted PST is failed, not not_found).
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore("C:/mail/archive_2024.pst", FakeOutlookFolder("Root"))],
        items_by_id={},
    )
    reader = FakeOutlookReader(namespace)
    monkeypatch.setattr(original_export, "_load_default_outlook_reader", lambda: reader)

    dest_dir = tmp_path / "cli_exports"
    exit_code = main(
        [
            "export-original",
            str(db_id),
            "--database",
            str(db_path),
            "--copy-to",
            str(dest_dir),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Original message not found in source." in captured.out
    assert "Missing Quotation" in captured.out
    assert "vendor@example.com" in captured.out
    assert "2026-04-01T10:00:00+08:00" in captured.out


def test_cli_export_original_json_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from mailattic import original_export
    from mailattic.cli import main

    db_path, db_id = _seed_imap_message(
        tmp_path,
        uid=88,
        message_id="<json88@example.com>",
        subject="JSON Export Test",
    )
    raw_rfc822 = (
        b"From: Charlie <charlie@example.com>\r\n"
        b"To: sean3808@mailattic.test\r\n"
        b"Subject: JSON Export Test\r\n"
        b"Message-ID: <json88@example.com>\r\n"
        b"\r\n"
        b"Body"
    )
    fake_conn = FakeImapConnection(
        rfc822_by_uid={88: raw_rfc822},
        uids_by_header={"json88@example.com": [88]},
    )
    imap_client = FakeImapClient(fake_conn)
    monkeypatch.setattr(
        original_export,
        "_load_default_imap_client",
        lambda *args, **kwargs: imap_client,
    )

    dest_dir = tmp_path / "cli_exports"
    exit_code = main(
        [
            "export-original",
            str(db_id),
            "--database",
            str(db_path),
            "--copy-to",
            str(dest_dir),
            "--json",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "exported"
    assert data["format"] == "eml"
    assert data["source_type"] == "imap"
    assert Path(data["dest_path"]).exists()


def test_outlook_unavailable_returns_failed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        subject="Unavailable Outlook",
    )
    from mailattic import original_export

    monkeypatch.setattr(original_export, "_load_default_outlook_reader", lambda: None)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=None,
    )
    assert result.status == "failed"
    assert result.detail is not None
    assert "Outlook is not running or unavailable" in result.detail


def test_imap_unavailable_returns_failed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, db_id = _seed_imap_message(
        tmp_path,
        uid=99,
        message_id="<unavail@example.com>",
        subject="Unavailable IMAP",
    )
    from mailattic import original_export

    monkeypatch.setattr(
        original_export,
        "_load_default_imap_client",
        lambda *args, **kwargs: None,
    )

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        imap_client=None,
    )
    assert result.status == "failed"
    assert result.detail is not None
    assert "IMAP server connection could not be established" in result.detail


def test_pst_location_prioritizes_folder_path(tmp_path: Path) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        folder_path="Reports/Monthly",
        message_id="<multi@example.com>",
        subject="Monthly Report",
    )

    item_inbox = FakeOutlookItem(
        entry_id="inbox-item",
        subject="Monthly Report",
        message_id="<multi@example.com>",
    )
    item_reports = FakeOutlookItem(
        entry_id="reports-item",
        subject="Monthly Report",
        message_id="<multi@example.com>",
    )

    inbox = FakeOutlookFolder("Inbox", [item_inbox])
    monthly = FakeOutlookFolder("Monthly", [item_reports])
    reports = FakeOutlookFolder("Reports", [])
    reports.Folders = FakeCollection(monthly)

    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox, reports)
    store = FakeOutlookStore(pst_file, root)
    namespace = FakeOutlookNamespace([store], items_by_id={})
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )

    assert result.status == "exported"
    assert result.dest_path is not None
    assert result.dest_path.read_bytes().startswith(
        b"MSG-BYTES:Monthly Report:reports-item"
    )


def test_unsupported_outbound_source_returns_not_found(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "test.db")
    conn = connect_database(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO messages (
            message_id, subject, sender_name, sender_email, sent_at,
            pst_source, folder_path, logical_locator, dedup_key, source_input_mode
        ) VALUES (
            '<outbound1@example.com>', 'Outbound Email', 'Sean', 'sean@mailattic.test',
            '2026-05-01T10:00:00+08:00', 'outbound-bes', 'Sent',
            'loc-1', 'dedup-1', 'outbound'
        )
        """
    )
    db_id = cur.lastrowid
    conn.commit()
    conn.close()

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
    )

    assert result.status == "not_found"
    assert result.dest_path is None
    assert result.subject == "Outbound Email"
    assert result.sent_at == "2026-05-01T10:00:00+08:00"
    assert "does not support original export" in (result.detail or "")


def test_imap_search_raising_imap_error_fails(tmp_path: Path) -> None:
    db_path, db_id = _seed_imap_message(
        tmp_path,
        uid=99999,
        message_id="<search_err@example.com>",
        subject="Search Err",
    )
    fake_conn = FakeImapConnection(rfc822_by_uid={})

    def raise_search(*args: str) -> tuple[str, list[object]]:
        if args and args[0].upper() == "SEARCH":
            raise imaplib.IMAP4.error("SEARCH protocol failure")
        return "OK", [b""]

    fake_conn.uid = raise_search  # type: ignore[assignment]
    imap_client = FakeImapClient(fake_conn)
    imap_client.fetch_rfc822 = lambda uid: None  # type: ignore[assignment]

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        imap_client=imap_client,
    )
    assert result.status == "failed"
    assert "SEARCH protocol failure" in (result.detail or "")


def test_imap_fetch_raising_imap_abort_fails(tmp_path: Path) -> None:
    db_path, db_id = _seed_imap_message(
        tmp_path,
        uid=42,
        message_id="<abort@example.com>",
        subject="Fetch Abort",
    )
    fake_conn = FakeImapConnection(rfc822_by_uid={})
    imap_client = FakeImapClient(fake_conn)

    def raise_fetch(uid: int) -> bytes:
        raise imaplib.IMAP4.abort("socket connection broken")

    imap_client.fetch_rfc822 = raise_fetch  # type: ignore[assignment]

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        imap_client=imap_client,
    )
    assert result.status == "failed"
    assert "socket connection broken" in (result.detail or "")


def test_imap_select_folder_failing_fails_without_inbox_fallback(
    tmp_path: Path,
) -> None:
    db_path, db_id = _seed_imap_message(
        tmp_path,
        folder_name="Archive",
        message_id="<select_fail@example.com>",
        subject="Select Fail",
    )
    fake_conn = FakeImapConnection(rfc822_by_uid={})
    imap_client = FakeImapClient(fake_conn)

    def raise_select(folder_name: str) -> None:
        raise imaplib.IMAP4.error(f"NO folder {folder_name} does not exist")

    imap_client.select_folder = raise_select  # type: ignore[assignment]

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        imap_client=imap_client,
    )
    assert result.status == "failed"
    assert "Archive" in (result.detail or "")
    assert fake_conn.selected_folder != "INBOX"


def test_pst_root_folder_access_raising_fails(tmp_path: Path) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        subject="Root Folder Err",
    )

    class FailingStore(FakeOutlookStore):
        def GetRootFolder(self) -> FakeOutlookFolder:
            raise RuntimeError("Root folder COM access error")

    root = FakeOutlookFolder("Root", [])
    store = FailingStore(pst_file, root)
    namespace = FakeOutlookNamespace([store], items_by_id={})
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )
    assert result.status == "failed"
    assert "Root folder COM access error" in (result.detail or "")


def test_pst_item_raising_during_scan_succeeds_if_target_found_elsewhere(
    tmp_path: Path,
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        folder_path="Inbox",
        entry_id="none",
        message_id="<find_me@example.com>",
        subject="Find Me",
    )

    corrupt_item = FakeOutlookItem("c1", "Corrupt")

    def raise_corrupt(*args: object) -> object:
        raise RuntimeError("Corrupt MAPI properties")

    corrupt_item.PropertyAccessor.GetProperty = raise_corrupt  # type: ignore[assignment]

    good_item = FakeOutlookItem(
        "g1",
        "Find Me",
        message_id="<find_me@example.com>",
    )

    inbox = FakeOutlookFolder("Inbox", [corrupt_item, good_item])
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    store = FakeOutlookStore(pst_file, root)
    namespace = FakeOutlookNamespace([store], items_by_id={})
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )
    assert result.status == "exported"
    assert result.dest_path is not None


def test_pst_item_raising_during_scan_fails_if_target_not_found(
    tmp_path: Path,
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        folder_path="Inbox",
        entry_id="none",
        message_id="<target@example.com>",
        subject="Target",
    )

    corrupt_item = FakeOutlookItem("c1", "Corrupt")

    def raise_corrupt(*args: object) -> object:
        raise RuntimeError("Corrupt MAPI properties")

    corrupt_item.PropertyAccessor.GetProperty = raise_corrupt  # type: ignore[assignment]

    inbox = FakeOutlookFolder("Inbox", [corrupt_item])
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    store = FakeOutlookStore(pst_file, root)
    namespace = FakeOutlookNamespace([store], items_by_id={})
    reader = FakeOutlookReader(namespace)

    dest_dir = tmp_path / "exports"
    result = export_original_message(
        db_path,
        db_id,
        destination_dir=dest_dir,
        outlook_reader=reader,
    )
    assert result.status == "failed"
    assert "could not be read" in (result.detail or "").lower()


_DEFAULT_CLIENT_RFC822 = (
    b"From: Charlie <charlie@example.com>\r\n"
    b"To: sean3808@mailattic.test\r\n"
    b"Subject: Closing Client\r\n"
    b"Message-ID: <closing1@example.com>\r\n"
    b"\r\n"
    b"Body"
)


def _seed_closing_client_message(tmp_path: Path) -> tuple[Path, int]:
    return _seed_imap_message(
        tmp_path,
        uid=5,
        message_id="<closing1@example.com>",
        subject="Closing Client",
    )


def test_default_created_imap_client_is_closed_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailattic import original_export

    db_path, db_id = _seed_closing_client_message(tmp_path)
    client = FakeImapClient(
        FakeImapConnection(
            rfc822_by_uid={5: _DEFAULT_CLIENT_RFC822},
            uids_by_header={"closing1@example.com": [5]},
        )
    )
    monkeypatch.setattr(
        original_export, "_load_default_imap_client", lambda *a, **k: client
    )

    result = export_original_message(
        db_path, db_id, destination_dir=tmp_path / "exports"
    )

    assert result.status == "exported"
    assert client.close_calls == 1


def test_default_created_imap_client_is_closed_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailattic import original_export

    db_path, db_id = _seed_closing_client_message(tmp_path)
    connection = FakeImapConnection(rfc822_by_uid={})

    def raise_search(*args: str) -> tuple[str, list[object]]:
        raise imaplib.IMAP4.error("SEARCH protocol failure")

    connection.uid = raise_search  # type: ignore[assignment]
    client = FakeImapClient(connection)
    monkeypatch.setattr(
        original_export, "_load_default_imap_client", lambda *a, **k: client
    )

    result = export_original_message(
        db_path, db_id, destination_dir=tmp_path / "exports"
    )

    assert result.status == "failed"
    assert client.close_calls == 1


def test_injected_imap_client_is_never_closed(tmp_path: Path) -> None:
    db_path, db_id = _seed_closing_client_message(tmp_path)
    client = FakeImapClient(
        FakeImapConnection(
            rfc822_by_uid={5: _DEFAULT_CLIENT_RFC822},
            uids_by_header={"closing1@example.com": [5]},
        )
    )

    result = export_original_message(
        db_path, db_id, destination_dir=tmp_path / "exports", imap_client=client
    )

    assert result.status == "exported"
    assert client.close_calls == 0


def test_message_id_identifier_matching_two_stored_rows_is_refused(
    tmp_path: Path,
) -> None:
    # Two stored rows whose Message-IDs differ only in angle brackets are one
    # identity; the lookup must refuse rather than export an arbitrary one.
    _seed_imap_message(
        tmp_path, uid=1, message_id="<dup@example.com>", subject="First copy"
    )
    db_path, _ = _seed_imap_message(
        tmp_path, uid=2, message_id="dup@example.com", subject="Second copy"
    )
    client = FakeImapClient(FakeImapConnection(rfc822_by_uid={}))

    result = export_original_message(
        db_path,
        "dup@example.com",
        destination_dir=tmp_path / "exports",
        imap_client=client,
    )

    assert result.status == "failed"
    assert "Ambiguous Message-ID" in (result.detail or "")
    assert not (tmp_path / "exports").exists()


def test_message_id_identifier_resolves_single_stored_row(tmp_path: Path) -> None:
    db_path, _ = _seed_closing_client_message(tmp_path)
    client = FakeImapClient(
        FakeImapConnection(
            rfc822_by_uid={5: _DEFAULT_CLIENT_RFC822},
            uids_by_header={"closing1@example.com": [5]},
        )
    )

    result = export_original_message(
        db_path,
        "closing1@example.com",
        destination_dir=tmp_path / "exports",
        imap_client=client,
    )

    assert result.status == "exported"


# --- No Message-ID: subject alone must never identify a message -------------

_NO_ID_SENT_AT = "2026-05-10T14:30:00+08:00"
_NO_ID_SENT_WALL_CLOCK = datetime(2026, 5, 10, 14, 30, 0)


def _export_pst_without_message_id(
    tmp_path: Path,
    *,
    sender_email: str,
    sent_on: datetime | None,
) -> Any:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-reused",
        message_id="",
        subject="Shared Subject",
        sender_email="alice@example.com",
        sent_at=_NO_ID_SENT_AT,
    )
    item = FakeOutlookItem(
        "entry-reused",
        "Shared Subject",
        sender_email=sender_email,
        sent_on=sent_on,
    )
    inbox = FakeOutlookFolder("Inbox", [item])
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore(pst_file, root)], items_by_id={"entry-reused": item}
    )
    return export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )


def test_pst_without_message_id_different_sender_is_not_exported(
    tmp_path: Path,
) -> None:
    result = _export_pst_without_message_id(
        tmp_path,
        sender_email="mallory@example.com",
        sent_on=_NO_ID_SENT_WALL_CLOCK,
    )
    assert result.status == "failed"  # #53 ruling 4: never not_found
    assert not any((tmp_path / "exports").glob("*.msg"))


def test_pst_without_message_id_different_sent_time_is_not_exported(
    tmp_path: Path,
) -> None:
    result = _export_pst_without_message_id(
        tmp_path,
        sender_email="Alice@Example.com",
        sent_on=datetime(2026, 5, 10, 16, 30, 0),
    )
    assert result.status == "failed"  # #53 ruling 4: never not_found
    assert not any((tmp_path / "exports").glob("*.msg"))


def test_pst_without_message_id_missing_sent_time_is_not_exported(
    tmp_path: Path,
) -> None:
    result = _export_pst_without_message_id(
        tmp_path, sender_email="alice@example.com", sent_on=None
    )
    assert result.status == "failed"  # #53 ruling 4: never not_found


def test_pst_without_message_id_subject_sender_and_time_match_exports(
    tmp_path: Path,
) -> None:
    result = _export_pst_without_message_id(
        tmp_path,
        sender_email="Alice@Example.com",
        sent_on=datetime(2026, 5, 10, 14, 30, 30),
    )
    assert result.status == "exported"
    assert result.dest_path is not None


def _export_imap_without_message_id(
    tmp_path: Path, *, from_header: str, date_header: str
) -> Any:
    db_path, db_id = _seed_imap_message(
        tmp_path,
        uid=42,
        message_id="",
        subject="Shared Subject",
        sender_email="charlie@example.com",
        sent_at="2026-06-15T09:00:00+08:00",
    )
    raw = (
        f"From: {from_header}\r\n"
        "To: sean3808@mailattic.test\r\n"
        "Subject: Shared Subject\r\n"
        f"Date: {date_header}\r\n"
        "\r\nBody"
    ).encode()
    client = FakeImapClient(FakeImapConnection(rfc822_by_uid={42: raw}))
    return export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        imap_client=client,
    )


def test_imap_without_message_id_different_sender_is_not_exported(
    tmp_path: Path,
) -> None:
    result = _export_imap_without_message_id(
        tmp_path,
        from_header="Mallory <mallory@example.com>",
        date_header="Mon, 15 Jun 2026 09:00:00 +0800",
    )
    assert result.status == "failed"  # #53 ruling 4: never not_found
    assert not any((tmp_path / "exports").glob("*.eml"))


def test_imap_without_message_id_different_sent_time_is_not_exported(
    tmp_path: Path,
) -> None:
    result = _export_imap_without_message_id(
        tmp_path,
        from_header="Charlie <charlie@example.com>",
        date_header="Mon, 15 Jun 2026 11:00:00 +0800",
    )
    assert result.status == "failed"  # #53 ruling 4: never not_found
    assert not any((tmp_path / "exports").glob("*.eml"))


def test_imap_without_message_id_unparsable_date_is_not_exported(
    tmp_path: Path,
) -> None:
    result = _export_imap_without_message_id(
        tmp_path,
        from_header="Charlie <charlie@example.com>",
        date_header="not a date",
    )
    assert result.status == "failed"  # #53 ruling 4: never not_found


def test_imap_without_message_id_subject_sender_and_time_match_exports(
    tmp_path: Path,
) -> None:
    result = _export_imap_without_message_id(
        tmp_path,
        from_header="Charlie <CHARLIE@example.com>",
        date_header="Mon, 15 Jun 2026 01:00:00 +0000",
    )
    assert result.status == "exported"


# --- Folder disambiguation is exact, not substring --------------------------


def test_folder_disambiguation_does_not_match_prefix_sibling(
    tmp_path: Path,
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        folder_path="Inbox/A",
        entry_id="entry-gone",
        message_id="<target@example.com>",
        subject="Target",
    )
    old_copy = FakeOutlookItem(
        "e-old", "Target", message_id="<target@example.com>", body="one"
    )
    other_copy = FakeOutlookItem(
        "e-b", "Target", message_id="<target@example.com>", body="two"
    )
    inbox = FakeOutlookFolder("Inbox", [])
    inbox.Folders = FakeCollection(
        FakeOutlookFolder("A-old", [old_copy]),
        FakeOutlookFolder("B", [other_copy]),
    )
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(inbox)
    namespace = FakeOutlookNamespace([FakeOutlookStore(pst_file, root)])

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "failed"
    assert "Ambiguous" in (result.detail or "")
    assert not any((tmp_path / "exports").glob("*.msg"))


# --- A store that cannot be read is not "not mounted" -----------------------


class _UnreadableStore:
    @property
    def FilePath(self) -> str:  # noqa: N802
        raise RuntimeError("store offline")


def test_unreadable_store_with_no_match_fails_instead_of_not_found(
    tmp_path: Path,
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(tmp_path, pst_path=pst_file)
    namespace = FakeOutlookNamespace([_UnreadableStore()])  # type: ignore[list-item]

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "failed"
    assert "1 store" in (result.detail or "")


def test_unreadable_store_with_matching_other_store_still_exports(
    tmp_path: Path,
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-ok",
        message_id="<msg101@example.com>",
        subject="PST Message Subject",
    )
    item = FakeOutlookItem(
        "entry-ok", "PST Message Subject", message_id="<msg101@example.com>"
    )
    root = FakeOutlookFolder("Root", [item])
    namespace = FakeOutlookNamespace(
        [_UnreadableStore(), FakeOutlookStore(pst_file, root)],  # type: ignore[list-item]
        items_by_id={"entry-ok": item},
    )

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "exported"


# --- Codex round 2: same body is not "same message"; same-name stores; -------
# --- unreadable folder on the folder_path walk -------------------------------


class _FakeAttachment:
    def __init__(self, file_name: str, size: int) -> None:
        self.FileName = file_name
        self.Size = size


def _export_two_pst_copies(
    tmp_path: Path, first: FakeOutlookItem, second: FakeOutlookItem
) -> Any:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-gone",
        message_id="<copy@example.com>",
        subject="Copy",
    )
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(
        # Neither copy sits in the recorded folder ("Inbox"), so the
        # store-wide Message-ID scan has to choose between them.
        FakeOutlookFolder("Old", [first]),
        FakeOutlookFolder("Archive", [second]),
    )
    namespace = FakeOutlookNamespace([FakeOutlookStore(pst_file, root)])
    return export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )


def test_same_body_but_different_attachments_is_ambiguous(tmp_path: Path) -> None:
    first = FakeOutlookItem("e-1", "Copy", message_id="<copy@example.com>")
    second = FakeOutlookItem("e-2", "Copy", message_id="<copy@example.com>")
    first.Attachments = FakeCollection(_FakeAttachment("quote.pdf", 1000))
    second.Attachments = FakeCollection(_FakeAttachment("other.pdf", 2000))

    result = _export_two_pst_copies(tmp_path, first, second)

    assert result.status == "failed"
    assert "Ambiguous" in (result.detail or "")
    assert not any((tmp_path / "exports").glob("*.msg"))


def test_fully_identical_copies_are_still_ambiguous(tmp_path: Path) -> None:
    # #53 ruling 5: the tool never guesses which copy is the original.
    first = FakeOutlookItem("e-1", "Copy", message_id="<copy@example.com>")
    second = FakeOutlookItem("e-2", "Copy", message_id="<copy@example.com>")
    first.Attachments = FakeCollection(_FakeAttachment("quote.pdf", 1000))
    second.Attachments = FakeCollection(_FakeAttachment("quote.pdf", 1000))

    result = _export_two_pst_copies(tmp_path, first, second)

    assert result.status == "failed"
    assert "Ambiguous" in (result.detail or "")
    assert not any((tmp_path / "exports").glob("*.msg"))


def test_two_mounted_stores_with_the_recorded_filename_fail(tmp_path: Path) -> None:
    recorded = str(tmp_path / "old" / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path, pst_path=recorded, entry_id="entry-gone"
    )
    item = FakeOutlookItem(
        "e-x", "PST Message Subject", message_id="<msg101@example.com>"
    )
    namespace = FakeOutlookNamespace(
        [
            FakeOutlookStore(
                str(tmp_path / "a" / "archive_2024.pst"), FakeOutlookFolder("Root")
            ),
            FakeOutlookStore(
                str(tmp_path / "b" / "archive_2024.pst"),
                FakeOutlookFolder("Root", [item]),
            ),
        ]
    )

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "failed"
    assert "2" in (result.detail or "")
    assert not any((tmp_path / "exports").glob("*.msg"))


class _UnreadableFolders:
    Count = 1

    def Item(self, index: int) -> object:  # noqa: N802
        raise RuntimeError("folder offline")


def test_unreadable_folder_on_recorded_path_fails_instead_of_not_found(
    tmp_path: Path,
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path, pst_path=pst_file, entry_id="entry-gone"
    )
    root = FakeOutlookFolder("Root", [])
    root.Folders = _UnreadableFolders()  # type: ignore[assignment]
    namespace = FakeOutlookNamespace([FakeOutlookStore(pst_file, root)])

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "failed"
    assert "could not be read" in (result.detail or "")


# --- Route after the review cap (#53 rulings 4-5) -----------------------------


def test_pst_without_message_id_is_not_searched_beyond_its_entry_id(
    tmp_path: Path,
) -> None:
    # The stored EntryID is stale; a matching item sits in the recorded folder.
    # Ruling 4: no search by subject/sender/time, report failed (never
    # not_found), keep subject/sender/date for a manual search.
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-stale",
        message_id="",
        subject="Shared Subject",
        sent_at=_NO_ID_SENT_AT,
    )
    look_alike = FakeOutlookItem(
        "entry-other", "Shared Subject", sent_on=_NO_ID_SENT_WALL_CLOCK
    )
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(FakeOutlookFolder("Inbox", [look_alike]))
    namespace = FakeOutlookNamespace([FakeOutlookStore(pst_file, root)])

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "failed"
    assert "by hand" in (result.detail or "")
    assert result.subject == "Shared Subject"
    assert not any((tmp_path / "exports").glob("*.msg"))


def test_pst_root_folder_message_is_found_after_entry_id_goes_stale(
    tmp_path: Path,
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path, pst_path=pst_file, folder_path="/", entry_id="entry-stale"
    )
    item = FakeOutlookItem(
        "entry-new", "PST Message Subject", message_id="<msg101@example.com>"
    )
    # A same-id item in a subfolder would make the store-wide scan ambiguous;
    # the root pass must win first.
    other = FakeOutlookItem(
        "entry-sub", "PST Message Subject", message_id="<msg101@example.com>"
    )
    root = FakeOutlookFolder("Root", [item])
    root.Folders = FakeCollection(FakeOutlookFolder("Sub", [other]))
    namespace = FakeOutlookNamespace([FakeOutlookStore(pst_file, root)])

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "exported"
    assert item.saved_calls


def test_pst_without_message_id_exchange_sender_matches_by_smtp_property(
    tmp_path: Path,
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-ex",
        message_id="",
        subject="Shared Subject",
        sender_email="alice@example.com",
        sent_at=_NO_ID_SENT_AT,
    )
    item = FakeOutlookItem("entry-ex", "Shared Subject", sent_on=_NO_ID_SENT_WALL_CLOCK)
    item.SenderEmailType = "EX"
    item.SenderEmailAddress = "/o=ExchangeLabs/ou=Exchange/cn=Recipients/cn=alice"
    item.PropertyAccessor = FakeAccessor(
        {"http://schemas.microsoft.com/mapi/proptag/0x5D01001E": "alice@example.com"}
    )
    root = FakeOutlookFolder("Root", [item])
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore(pst_file, root)], items_by_id={"entry-ex": item}
    )

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "exported"


def test_imap_without_message_id_encoded_subject_matches(tmp_path: Path) -> None:
    db_path, db_id = _seed_imap_message(
        tmp_path,
        uid=42,
        message_id="",
        subject="報價單 Quote",
        sender_email="charlie@example.com",
        sent_at="2026-06-15T09:00:00+08:00",
    )
    raw = (
        b"From: Charlie <charlie@example.com>\r\n"
        b"Subject: =?utf-8?b?5aCx5YO55Zau?= Quote\r\n"
        b"Date: Mon, 15 Jun 2026 09:00:00 +0800\r\n"
        b"\r\nBody"
    )
    client = FakeImapClient(FakeImapConnection(rfc822_by_uid={42: raw}))

    result = export_original_message(
        db_path, db_id, destination_dir=tmp_path / "exports", imap_client=client
    )

    assert result.status == "exported"


def test_imap_message_found_twice_is_ambiguous(tmp_path: Path) -> None:
    db_path, db_id = _seed_imap_message(
        tmp_path, uid=999, message_id="<dup@example.com>"
    )
    raw = b"Message-ID: <dup@example.com>\r\nSubject: x\r\n\r\nBody"
    client = FakeImapClient(
        FakeImapConnection(
            rfc822_by_uid={7: raw, 8: raw},
            uids_by_header={"dup@example.com": [7, 8]},
        )
    )

    result = export_original_message(
        db_path, db_id, destination_dir=tmp_path / "exports", imap_client=client
    )

    assert result.status == "failed"
    assert "Ambiguous" in (result.detail or "")


class _RaisingReader:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @property
    def namespace(self) -> object:
        raise self._exc


def test_outlook_not_running_is_reported_as_unavailable(tmp_path: Path) -> None:
    db_path, db_id = _seed_pst_message(tmp_path)

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=_RaisingReader(RuntimeError("Outlook is not running.")),
    )

    assert result.status == "failed"
    assert "Outlook is not running" in (result.detail or "")


def test_outlook_adapter_defect_is_not_relabelled_unavailable(
    tmp_path: Path,
) -> None:
    db_path, db_id = _seed_pst_message(tmp_path)

    with pytest.raises(AttributeError):
        export_original_message(
            db_path,
            db_id,
            destination_dir=tmp_path / "exports",
            outlook_reader=_RaisingReader(AttributeError("adapter bug")),
        )


# --- Review after route change -----------------------------------------------


def test_cli_export_original_failed_prints_subject_sender_and_date(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mailattic import original_export
    from mailattic.cli import main

    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst_file,
        entry_id="entry-stale",
        message_id="",
        subject="Missing Quotation",
        sender_email="vendor@example.com",
        sent_at="2026-04-01T10:00:00+08:00",
    )
    reader = FakeOutlookReader(
        FakeOutlookNamespace([FakeOutlookStore(pst_file, FakeOutlookFolder("Root"))])
    )
    monkeypatch.setattr(original_export, "_load_default_outlook_reader", lambda: reader)

    exit_code = main(
        [
            "export-original",
            str(db_id),
            "--database",
            str(db_path),
            "--copy-to",
            str(tmp_path / "cli_exports"),
        ]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Export failed" in err
    assert "Missing Quotation" in err
    assert "vendor@example.com" in err
    assert "2026-04-01T10:00:00+08:00" in err


class _BrokenIdentityAccessor:
    def GetProperty(self, name: str) -> object:  # noqa: N802
        raise RuntimeError("property store offline")


def test_direct_hit_whose_identity_cannot_be_read_is_not_not_found(
    tmp_path: Path,
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path, pst_path=pst_file, entry_id="entry-unreadable"
    )
    item = FakeOutlookItem("entry-unreadable", "PST Message Subject")
    item.PropertyAccessor = _BrokenIdentityAccessor()  # type: ignore[assignment]
    # The item is not in any folder the scan reaches, so only the direct hit
    # saw it; its read error must still keep the result away from not_found.
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore(pst_file, FakeOutlookFolder("Root"))],
        items_by_id={"entry-unreadable": item},
    )

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "failed"
    assert "could not be read" in (result.detail or "")


# --- #53 ruling 6: not_found only when the message is really not there --------


def test_pst_not_open_in_outlook_fails_with_instruction(tmp_path: Path) -> None:
    pst_file = str(tmp_path / "missing_archive.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path, pst_path=pst_file, subject="Old Deleted Memo"
    )

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(FakeOutlookNamespace([], items_by_id={})),
    )

    assert result.status == "failed"
    assert "missing_archive.pst" in (result.detail or "")
    assert "Open" in (result.detail or "")
    assert result.subject == "Old Deleted Memo"


def test_web_archive_message_fails_with_reason(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "mail.db")
    conn = connect_database(db_path)
    try:
        conn.execute(
            "INSERT INTO messages (subject, sender_name, sender_email, sent_at, "
            "body_text, message_id, pst_source, folder_path, logical_locator, "
            "source_input_mode, source_requested_from, dedup_key, dedup_basis) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "Scraped Notice",
                "Vendor",
                "vendor@example.com",
                "2019-03-01T10:00:00+08:00",
                "body",
                "<scraped@example.com>",
                "archive:webmail",
                "Inbox",
                "archive-1",
                "archive",
                "archive:webmail",
                "message-id:<scraped@example.com>",
                "message-id",
            ),
        )
        conn.commit()
        db_id = conn.execute("SELECT id FROM messages").fetchone()[0]
    finally:
        conn.close()

    result = export_original_message(
        db_path, db_id, destination_dir=tmp_path / "exports"
    )

    assert result.status == "failed"
    assert "archive" in (result.detail or "").lower()
    assert result.subject == "Scraped Notice"


@pytest.mark.parametrize("missing", ["Items", "Folders"])
def test_folder_without_items_or_subfolders_is_unreadable_not_empty(
    tmp_path: Path, missing: str
) -> None:
    pst_file = str(tmp_path / "archive_2024.pst")
    db_path, db_id = _seed_pst_message(
        tmp_path, pst_path=pst_file, entry_id="entry-stale"
    )
    odd = FakeOutlookFolder("Odd", [])
    setattr(odd, missing, None)
    root = FakeOutlookFolder("Root", [])
    root.Folders = FakeCollection(odd)
    namespace = FakeOutlookNamespace([FakeOutlookStore(pst_file, root)])

    result = export_original_message(
        db_path,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "failed"
    assert "could not be read" in (result.detail or "")


@pytest.mark.parametrize(
    ("case", "expected", "selected"),
    [
        ("root_prefix", "exported", "recorded"),
        ("same_name_child", "exported", "recorded"),
        ("moved", "exported", "other"),
        ("ambiguous_store", "failed", None),
        ("ambiguous_recorded", "failed", None),
        ("unreadable", "failed", None),
        ("verification", "failed", None),
        ("normalized", "exported", "recorded"),
    ],
)
def test_pst_bulk_lookup_cases(
    tmp_path: Path, case: str, expected: str, selected: str | None
) -> None:
    pst = str(tmp_path / "archive.pst")
    path = "Root/Inbox" if case in {"root_prefix", "same_name_child"} else "Inbox"
    db, db_id = _seed_pst_message(
        tmp_path,
        pst_path=pst,
        folder_path=path,
        entry_id="stale",
        message_id="<target@example.com>",
    )
    recorded_item = FakeOutlookItem("recorded", "Target", "<target@example.com>")
    other_item = FakeOutlookItem("other", "Target", "<target@example.com>")
    recorded = FakeOutlookFolder(
        "Inbox",
        [] if case in {"moved", "ambiguous_store", "unreadable"} else [recorded_item],
    )
    other = FakeOutlookFolder(
        "Other",
        [other_item]
        if case in {"root_prefix", "same_name_child", "moved", "ambiguous_store"}
        else [],
    )
    root = FakeOutlookFolder("Root")
    if case == "same_name_child":
        child = FakeOutlookFolder("Root")
        child.Folders = FakeCollection(recorded)
        root.Folders = FakeCollection(child, other)
    else:
        root.Folders = FakeCollection(recorded, other)
    if case == "ambiguous_store":
        third = FakeOutlookFolder(
            "Third", [FakeOutlookItem("third", "Target", "<target@example.com>")]
        )
        root.Folders = FakeCollection(recorded, other, third)
    if case == "ambiguous_recorded":
        recorded._items.append(other_item)
    if case == "unreadable":
        other.GetTable = lambda: (_ for _ in ()).throw(RuntimeError("unreadable"))
    if case == "normalized":
        recorded_item.PropertyAccessor._values[PR_INTERNET_MESSAGE_ID] = (
            "target@example.com"
        )
    namespace = FakeOutlookNamespace([FakeOutlookStore(pst, root)])
    if case == "verification":
        namespace._items_by_id["recorded"] = FakeOutlookItem(
            "wrong", "Wrong", "<wrong@example.com>"
        )
    result = export_original_message(
        db,
        db_id,
        destination_dir=tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )
    assert result.status == expected
    if selected:
        assert result.dest_path is not None
        assert result.dest_path.read_bytes().endswith(selected.encode())
    else:
        assert result.dest_path is None
        if case.startswith("ambiguous"):
            assert "Ambiguous" in (result.detail or "")


def _table_case(
    tmp_path: Path,
    items: list[FakeOutlookItem],
    message_id: str = "<target@example.com>",
):
    pst = str(tmp_path / "archive.pst")
    db, db_id = _seed_pst_message(
        tmp_path, pst_path=pst, entry_id="stale", message_id=message_id
    )
    folder = FakeOutlookFolder("Inbox", items)
    root = FakeOutlookFolder("Root")
    root.Folders = FakeCollection(folder)
    namespace = FakeOutlookNamespace([FakeOutlookStore(pst, root)])

    def run():
        return export_original_message(
            db,
            db_id,
            destination_dir=tmp_path / "exports",
            outlook_reader=FakeOutlookReader(namespace),
        )

    return folder, namespace, run


def test_table_columns_and_multiple_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(original_export, "_TABLE_BATCH_ROWS", 1)
    folder, _, run = _table_case(
        tmp_path,
        [
            FakeOutlookItem("a", "Other", "<other@example.com>"),
            FakeOutlookItem("b", "Target", "<target@example.com>"),
        ],
    )
    result = run()
    assert result.status == "exported"
    assert folder.table.columns == [PR_INTERNET_MESSAGE_ID, "EntryID"]
    assert folder.table.position == 2
    assert folder.table.calls == 2


@pytest.mark.parametrize("failure", ["empty", "partial"])
def test_recorded_table_incomplete_fails(tmp_path: Path, failure: str) -> None:
    folder, _, run = _table_case(
        tmp_path,
        [
            FakeOutlookItem("a", "Target", "<target@example.com>"),
            FakeOutlookItem("b", "Target", "<target@example.com>"),
        ],
    )
    original = folder.GetTable

    def get_table():
        table = original()
        get_array = table.GetArray
        calls = 0

        def get_batch(count):
            nonlocal calls
            calls += 1
            if failure == "empty":
                return []
            if calls > 1:
                raise RuntimeError("table failure")
            return get_array(1)

        table.GetArray = get_batch
        return table

    folder.GetTable = get_table
    result = run()
    assert result.status == "failed"
    assert result.dest_path is None


@pytest.mark.parametrize("case", ["hidden", "error", "missing"])
def test_none_table_row_fallback(tmp_path: Path, case: str) -> None:
    item = FakeOutlookItem(
        "a", "Target", "<target@example.com>" if case == "hidden" else None
    )
    item.table_message_id = None
    if case == "error":

        def fail(name):
            raise RuntimeError("unreadable property")

        item.PropertyAccessor.GetProperty = fail
    _, _, run = _table_case(tmp_path, [item])
    result = run()
    assert (
        result.status
        == {"hidden": "exported", "error": "failed", "missing": "not_found"}[case]
    )


@pytest.mark.parametrize("match", [True, False])
def test_truncated_table_id_requires_full_verification(
    tmp_path: Path, match: bool
) -> None:
    full = "<" + "a" * 260 + "@example.com>"
    item = FakeOutlookItem("a", "Target", full if match else "<wrong@example.com>")
    item.table_message_id = full[:255]
    _, _, run = _table_case(tmp_path, [item], full)
    result = run()
    assert result.status == ("exported" if match else "failed")
    if not match:
        assert result.dest_path is None
        assert "could not be confirmed" in (result.detail or "")


def test_fake_namespace_only_opens_emitted_rows(tmp_path: Path) -> None:
    item = FakeOutlookItem("a", "Target", "<target@example.com>")
    folder, namespace, _ = _table_case(tmp_path, [item])
    with pytest.raises(RuntimeError):
        namespace.GetItemFromID("a")
    table = folder.GetTable()
    table.Columns.RemoveAll()
    table.Columns.Add(PR_INTERNET_MESSAGE_ID)
    table.Columns.Add("EntryID")
    table.GetArray(1)
    assert namespace.GetItemFromID("a") is item


def test_malformed_table_row_propagates(tmp_path: Path) -> None:
    folder, _, run = _table_case(
        tmp_path, [FakeOutlookItem("a", "Target", "<target@example.com>")]
    )
    original = folder.GetTable

    def get_table():
        table = original()

        def malformed(count):
            table.EndOfTable = True
            return [("bad",)]

        table.GetArray = malformed
        return table

    folder.GetTable = get_table
    with pytest.raises(ValueError):
        run()


def test_table_hit_without_store_id_is_confirmed(tmp_path: Path) -> None:
    _, namespace, run = _table_case(
        tmp_path, [FakeOutlookItem("a", "Target", "<target@example.com>")]
    )
    del namespace.Stores.Item(1).StoreID
    assert run().status == "exported"

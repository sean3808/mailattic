"""Tests for issue #53: Original export (匯出原信).

Retrieves one stored message's original from its source:
- PST source via Outlook COM -> `.msg`
- IMAP source via IMAP server -> `.eml`
- Neither found -> result carries subject, sender, sent_at without error
- Ambiguous/failed location never exports a different message
- Read/write failures are reported as failures, not success.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mailattic.models import MaildirSource, PstSource, build_normalized_message
from mailattic.original_export import (
    export_original_message,
)
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
    ) -> None:
        self.Class = 43
        self.EntryID = entry_id
        self.Subject = subject
        self.Body = body
        self.SenderEmailAddress = sender_email
        self.SenderEmailType = "SMTP"
        self.SentOn = None
        self.ReceivedTime = None
        props: dict[str, object] = {}
        if message_id:
            props["http://schemas.microsoft.com/mapi/proptag/0x1035001E"] = message_id
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

    def GetItemFromID(
        self, entry_id: str, store_id: str | None = None
    ) -> FakeOutlookItem | None:
        if entry_id in self._items_by_id:
            return self._items_by_id[entry_id]
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

    def select_folder(self, folder_name: str) -> None:
        self._conn.select(folder_name, readonly=True)

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

    # Empty namespace with no stores matching the PST
    namespace = FakeOutlookNamespace([], items_by_id={})
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
    namespace = FakeOutlookNamespace([], items_by_id={})
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

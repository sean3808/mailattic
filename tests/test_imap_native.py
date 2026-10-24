"""Tests for NativeImapClient + write_rfc822_to_maildir + sync_native.

Groups 2 and 3: IMAP client unit tests and sync_native integration tests.

Task 3.1 is the TDD red-green anchor: test_per_uid_watermark_chain_is_crash_safe
was written first (RED), then sync_native was implemented to make it pass (GREEN).
"""

from __future__ import annotations

import sqlite3
import ssl
from pathlib import Path
from typing import Any

import pytest

from mailattic import imap_native, imap_sync
from mailattic.imap_native import (
    ImapFolder,
    NativeImapClient,
    _parse_list_entry,
    sync_native,
    write_rfc822_to_maildir,
)
from mailattic.imap_state import ImapSyncState
from mailattic.models import ImportSummary
from mailattic.storage import initialize_database

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_EML = (
    b"From: sender@example.com\r\n"
    b"To: rcpt@example.com\r\n"
    b"Subject: Test Message {uid}\r\n"
    b"Message-ID: <test-{uid}@example.com>\r\n"
    b"\r\n"
    b"Body of message {uid}\r\n"
)

_TEST_ENV = {
    "IMAP_HOST": "mail.example.com",
    "IMAP_PORT": "993",
    "IMAP_USER": "user@example.com",
    "IMAP_PASSWORD": "password",
    "IMAP_ACCOUNT_NAME": "testaccount",
}


def _make_eml(uid: int) -> bytes:
    return _MINIMAL_EML.replace(b"{uid}", str(uid).encode())


class _MockImapConn:
    """Injectable mock for imaplib.IMAP4_SSL in unit tests."""

    def __init__(
        self,
        *,
        list_data: list[bytes] | None = None,
        status_map: dict[str, int] | None = None,
        search_map: dict[str, list[int]] | None = None,
        fetch_map: dict[int, bytes] | None = None,
        select_ok: bool = True,
        append_ok: bool = True,
    ) -> None:
        self._list_data = list_data or []
        self._status_map = status_map or {}
        self._search_map = search_map or {}
        self._fetch_map = fetch_map or {}
        self._select_ok = select_ok
        self._append_ok = append_ok
        self._selected: str | None = None
        self.close_called = False
        self.logout_called = False
        self.login_calls: list[tuple[str, str]] = []
        self.fetch_calls: list[tuple[str, str]] = []
        self.append_calls: list[tuple[str, str, Any, bytes]] = []

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.login_calls.append((user, password))
        return "OK", [b"LOGIN completed"]

    def list(self) -> tuple[str, list[bytes | None]]:
        return "OK", self._list_data

    def status(self, folder: str, items: str) -> tuple[str, list[bytes]]:
        name = folder.strip('"')
        uidval = self._status_map.get(name, 99999)
        return "OK", [f"* STATUS {folder} (UIDVALIDITY {uidval})".encode()]

    def select(self, folder: str, readonly: bool = True) -> tuple[str, list[bytes]]:
        self._selected = folder.strip('"')
        if self._select_ok:
            return "OK", [b"1"]
        return "NO", [b"permission denied"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        cmd = command.upper()
        if cmd == "SEARCH":
            folder = self._selected or ""
            uids = self._search_map.get(folder, [])
            # Respect UID range criterion "UID N:*" the way a real server
            # does (RFC 9051 section 6.4.9): "*" is the mailbox's current
            # max UID, and a range's endpoints are unordered — if N exceeds
            # that max UID, the effective range is reversed to
            # max_uid:N, which still matches max_uid itself. Production
            # code (search_uids_after) is responsible for excluding
            # anything <= watermark from that answer; this mock must not
            # do that filtering for it.
            if args and str(args[0]).startswith("UID "):
                min_uid_str = str(args[0])[4:].split(":")[0]
                try:
                    min_uid = int(min_uid_str)
                    if uids:
                        star = max(uids)
                        low, high = sorted((min_uid, star))
                        uids = [u for u in uids if low <= u <= high]
                    else:
                        uids = []
                except ValueError:
                    pass
            return "OK", [" ".join(str(u) for u in uids).encode()]
        if cmd == "FETCH":
            uid_str = args[0]
            self.fetch_calls.append((uid_str, args[1] if len(args) > 1 else ""))
            uid = int(uid_str)
            body = self._fetch_map.get(uid, _make_eml(uid))
            return "OK", [(b"1 (BODY[] {10}", body), b")"]
        return "OK", [b""]

    def append(
        self, mailbox: str, flags: str, date_time: Any, message: bytes
    ) -> tuple[str, list[bytes]]:
        self.append_calls.append((mailbox, flags, date_time, message))
        if not self._append_ok:
            return "NO", [b"APPEND rejected"]
        return "OK", [b"APPEND completed"]

    def close(self) -> None:
        self.close_called = True

    def logout(self) -> None:
        self.logout_called = True


class _FakeNativeClient:
    """High-level fake that mimics NativeImapClient's public interface.

    ``folders`` keeps its historical 2-tuple (name, delimiter) shape for
    backward-compatible call sites; ``list_folders()`` wraps each pair into an
    ``ImapFolder`` with display_name == wire_name == name, which is exactly
    correct for the ASCII-only folder names every existing test here uses.
    """

    def __init__(
        self,
        *,
        folders: list[tuple[str, str]] | None = None,
        uidvalidity_map: dict[str, int] | None = None,
        search_map: dict[str, list[int]] | None = None,
        fetch_map: dict[int, bytes] | None = None,
        fail_on_uid: int | None = None,
        list_folders_raises: Exception | None = None,
    ) -> None:
        self._folders = folders or [("INBOX", "/")]
        self._uidvalidity_map = uidvalidity_map or {n: 12345 for n, _ in self._folders}
        self._search_map = search_map or {}
        self._fetch_map = fetch_map or {}
        self._fail_on_uid = fail_on_uid
        self._list_folders_raises = list_folders_raises
        self._selected: str | None = None
        self.closed = False

    def list_folders(self) -> list[ImapFolder]:
        # Folder resolution runs outside sync_native's per-folder try/except,
        # so making it raise is how tests reach the "run died before its end
        # point" path (tests/test_sync_state.py).
        if self._list_folders_raises is not None:
            raise self._list_folders_raises
        return [
            ImapFolder(display_name=name, wire_name=name, delimiter=delimiter)
            for name, delimiter in self._folders
        ]

    def get_uidvalidity(self, folder_name: str) -> int:
        return self._uidvalidity_map.get(folder_name, 99999)

    def select_folder(self, folder_name: str) -> None:
        self._selected = folder_name

    def search_uids_after(self, watermark: int | None) -> list[int]:
        all_uids = sorted(self._search_map.get(self._selected or "", []))
        if watermark is None:
            return all_uids
        return [u for u in all_uids if u > watermark]

    def fetch_rfc822(self, uid: int) -> bytes:
        if self._fail_on_uid is not None and uid == self._fail_on_uid:
            raise RuntimeError(f"Simulated fetch failure for UID {uid}")
        return self._fetch_map.get(uid, _make_eml(uid))

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Group 2: NativeImapClient unit tests
# ---------------------------------------------------------------------------


def test_parse_list_entry_standard_quoted_delimiter() -> None:
    line = r'(\HasNoChildren) "/" INBOX'
    result = _parse_list_entry(line)
    assert result == ("INBOX", "/")


def test_parse_list_entry_dot_delimiter() -> None:
    line = r'(\HasNoChildren) "." INBOX.Sent'
    result = _parse_list_entry(line)
    assert result == ("INBOX.Sent", ".")


def test_parse_list_entry_nil_delimiter() -> None:
    line = r"(\Noselect) NIL [Gmail]"
    result = _parse_list_entry(line)
    assert result == ("[Gmail]", "")


def test_parse_list_entry_quoted_folder_name() -> None:
    line = r'(\HasNoChildren) "/" "Sent Items"'
    result = _parse_list_entry(line)
    assert result == ("Sent Items", "/")


@pytest.mark.parametrize("security", [None, "tls"])
def test_native_imap_client_uses_implicit_tls_by_default_and_when_explicit(
    monkeypatch,
    security: str | None,
) -> None:
    conn = _MockImapConn()
    calls: dict[str, object] = {}
    ssl_context = object()

    def open_ssl_connection(**kwargs):
        calls.update(kwargs)
        return conn

    def reject_plain_connection(**_kwargs):
        pytest.fail("default IMAP transport must not open a plaintext connection")

    monkeypatch.setattr(imap_native.imaplib, "IMAP4_SSL", open_ssl_connection)
    monkeypatch.setattr(imap_native.imaplib, "IMAP4", reject_plain_connection)
    monkeypatch.setattr(
        imap_sync,
        "_build_legacy_ssl_context",
        lambda: ssl_context,
    )

    env = _TEST_ENV if security is None else _TEST_ENV | {"IMAP_SECURITY": security}
    NativeImapClient(env, timeout=12.5)

    assert calls == {
        "host": "mail.example.com",
        "port": 993,
        "timeout": 12.5,
        "ssl_context": ssl_context,
    }
    assert conn.login_calls == [("user@example.com", "password")]


def test_native_imap_client_never_falls_back_to_plain_after_tls_failure(
    monkeypatch,
) -> None:
    plain_calls: list[dict[str, object]] = []

    def fail_ssl_connection(**_kwargs):
        raise ssl.SSLError("WRONG_VERSION_NUMBER")

    def record_plain_connection(**kwargs):
        plain_calls.append(kwargs)
        return _MockImapConn()

    monkeypatch.setattr(imap_native.imaplib, "IMAP4_SSL", fail_ssl_connection)
    monkeypatch.setattr(imap_native.imaplib, "IMAP4", record_plain_connection)
    monkeypatch.setattr(
        imap_sync,
        "_build_legacy_ssl_context",
        lambda: object(),
    )

    with pytest.raises(ssl.SSLError, match="WRONG_VERSION_NUMBER"):
        NativeImapClient(_TEST_ENV)

    assert plain_calls == []


def test_native_imap_client_uses_plain_connection_when_explicit(monkeypatch) -> None:
    conn = _MockImapConn()
    calls: dict[str, object] = {}

    def open_plain_connection(**kwargs):
        calls.update(kwargs)
        return conn

    def reject_ssl_connection(**_kwargs):
        pytest.fail("IMAP_SECURITY=plain must not open an implicit TLS connection")

    monkeypatch.setattr(imap_native.imaplib, "IMAP4", open_plain_connection)
    monkeypatch.setattr(imap_native.imaplib, "IMAP4_SSL", reject_ssl_connection)
    env = _TEST_ENV | {"IMAP_SECURITY": "plain", "IMAP_PORT": "143"}

    NativeImapClient(env, timeout=8.0)

    assert calls == {
        "host": "mail.example.com",
        "port": 143,
        "timeout": 8.0,
    }
    assert conn.login_calls == [("user@example.com", "password")]


def test_parse_list_entry_returns_none_for_malformed() -> None:
    assert _parse_list_entry("totally wrong") is None


def test_native_imap_client_list_folders_parses_responses() -> None:
    conn = _MockImapConn(
        list_data=[
            b'(\\HasNoChildren) "/" INBOX',
            b'(\\HasNoChildren) "/" Sent',
        ]
    )
    client = NativeImapClient(_TEST_ENV, _connection=conn)
    folders = client.list_folders()
    assert ImapFolder(display_name="INBOX", wire_name="INBOX", delimiter="/") in folders
    assert ImapFolder(display_name="Sent", wire_name="Sent", delimiter="/") in folders


def test_native_imap_client_get_uidvalidity() -> None:
    conn = _MockImapConn(status_map={"INBOX": 1234567890})
    client = NativeImapClient(_TEST_ENV, _connection=conn)
    assert client.get_uidvalidity("INBOX") == 1234567890


def test_native_imap_client_search_uids_after_watermark() -> None:
    conn = _MockImapConn(
        search_map={"INBOX": [1, 2, 3]},
        status_map={"INBOX": 1},
    )
    client = NativeImapClient(_TEST_ENV, _connection=conn)
    client.select_folder("INBOX")
    uids = client.search_uids_after(1)
    assert uids == [2, 3]


def test_native_imap_client_search_uids_all_when_no_watermark() -> None:
    conn = _MockImapConn(
        search_map={"INBOX": [10, 20, 30]},
    )
    client = NativeImapClient(_TEST_ENV, _connection=conn)
    client.select_folder("INBOX")
    uids = client.search_uids_after(None)
    assert uids == [10, 20, 30]


@pytest.mark.parametrize("server_reported_uid", [99, 100])
def test_native_imap_client_search_uids_after_excludes_reversed_range_endpoint(
    server_reported_uid: int,
) -> None:
    """RFC 9051 section 6.4.9: ``UID 101:*`` has its endpoints reversed to
    ``max_uid:101`` when 101 exceeds the mailbox's current max UID, so a
    server may legally answer with a UID at or below the watermark. The
    client must filter the server's answer itself rather than trust it.
    """

    class _RawConn:
        def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
            assert command == "SEARCH"
            return "OK", [str(server_reported_uid).encode()]

    client = NativeImapClient(_TEST_ENV, _connection=_RawConn())
    uids = client.search_uids_after(100)
    assert uids == []


def test_native_imap_client_fetch_uses_body_peek() -> None:
    """Verify BODY.PEEK[] (not RFC822) is passed so \\Seen is never set (D5)."""
    conn = _MockImapConn(fetch_map={5: b"From: x\r\n\r\nbody"})
    client = NativeImapClient(_TEST_ENV, _connection=conn)
    result = client.fetch_rfc822(5)
    assert result == b"From: x\r\n\r\nbody"
    assert conn.fetch_calls, "fetch_rfc822 should call uid('FETCH', ...)"
    _, fetch_args = conn.fetch_calls[0]
    assert "BODY.PEEK" in fetch_args


def test_native_imap_client_append_message_sends_wire_mailbox_and_seen_flag() -> None:
    """Task 3.1: APPEND carries the wire name and the \\Seen flag."""
    conn = _MockImapConn()
    client = NativeImapClient(_TEST_ENV, _connection=conn)

    client.append_message("&W8RO9lCZTv0-", b"raw rfc822 bytes")

    assert len(conn.append_calls) == 1
    mailbox, flags, _date_time, message = conn.append_calls[0]
    assert mailbox.strip('"') == "&W8RO9lCZTv0-"
    assert flags == "(\\Seen)"
    assert message == b"raw rfc822 bytes"


def test_native_imap_client_append_message_raises_on_non_ok_response() -> None:
    conn = _MockImapConn(append_ok=False)
    client = NativeImapClient(_TEST_ENV, _connection=conn)

    with pytest.raises(RuntimeError):
        client.append_message("&W8RO9lCZTv0-", b"raw rfc822 bytes")


# ---------------------------------------------------------------------------
# write_rfc822_to_maildir
# ---------------------------------------------------------------------------


def test_write_rfc822_to_maildir_creates_file_in_new(tmp_path: Path) -> None:
    rfc822 = b"From: a@b.com\r\nSubject: Hi\r\n\r\nHello"
    out = write_rfc822_to_maildir(
        maildir_root=tmp_path / "maildir",
        account_name="acct",
        folder_name="INBOX",
        uid=7,
        uidvalidity=1,
        rfc822=rfc822,
    )
    assert out.parent.name == "new"
    assert out.name == "uid-7.eml"
    assert out.read_bytes() == rfc822


def test_write_rfc822_to_maildir_no_tmp_leftover(tmp_path: Path) -> None:
    write_rfc822_to_maildir(
        maildir_root=tmp_path / "maildir",
        account_name="acct",
        folder_name="INBOX",
        uid=3,
        uidvalidity=1,
        rfc822=b"data",
    )
    tmp_file = tmp_path / "maildir" / "acct" / "INBOX" / "tmp" / "uid-3.eml"
    assert not tmp_file.exists()


def test_write_rfc822_to_maildir_is_idempotent(tmp_path: Path) -> None:
    """Writing the same UID twice should overwrite silently (crash-recovery path)."""
    maildir = tmp_path / "maildir"
    write_rfc822_to_maildir(
        maildir_root=maildir,
        account_name="acct",
        folder_name="INBOX",
        uid=1,
        uidvalidity=1,
        rfc822=b"v1",
    )
    write_rfc822_to_maildir(
        maildir_root=maildir,
        account_name="acct",
        folder_name="INBOX",
        uid=1,
        uidvalidity=1,
        rfc822=b"v1",
    )
    new_dir = maildir / "acct" / "INBOX" / "new"
    assert [p.name for p in new_dir.iterdir()] == ["uid-1.eml"]
    assert (new_dir / "uid-1.eml").read_bytes() == b"v1"


def test_write_rfc822_to_maildir_keeps_old_raw_when_uid_is_reused(
    tmp_path: Path,
) -> None:
    """#17: different bytes under a used UID name never replace it."""
    maildir = tmp_path / "maildir"
    kwargs = {
        "maildir_root": maildir,
        "account_name": "acct",
        "folder_name": "INBOX",
        "uid": 1,
    }
    write_rfc822_to_maildir(rfc822=b"old", uidvalidity=100, **kwargs)
    reused = write_rfc822_to_maildir(rfc822=b"new", uidvalidity=200, **kwargs)
    again = write_rfc822_to_maildir(rfc822=b"new", uidvalidity=200, **kwargs)
    new_dir = maildir / "acct" / "INBOX" / "new"
    assert (new_dir / "uid-1.eml").read_bytes() == b"old"
    assert reused == again == new_dir / "uid-1.v200.eml"
    assert reused.read_bytes() == b"new"
    # even the generation name is taken by other bytes: hash suffix fallback
    third = write_rfc822_to_maildir(rfc822=b"third", uidvalidity=200, **kwargs)
    assert third.name.startswith("uid-1.v200.") and third.name != reused.name
    assert reused.read_bytes() == b"new"


# ---------------------------------------------------------------------------
# Group 3: sync_native integration tests
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    return db_path


def test_per_uid_watermark_chain_is_crash_safe(tmp_path: Path) -> None:
    """TDD 3.1 (RED→GREEN): watermark advances after each ingest, not in bulk.

    Simulate 3 UIDs.  After each ingest the watermark file must reflect the
    newly processed UID so a crash between UID N and N+1 is recoverable.
    """
    db_path = _make_db(tmp_path)
    maildir_root = tmp_path / "maildir"
    state_path = tmp_path / ".imap_watermarks.json"
    log_dir = tmp_path / "logs"

    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 100},
        search_map={"INBOX": [1, 2, 3]},
        fetch_map={1: _make_eml(1), 2: _make_eml(2), 3: _make_eml(3)},
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=maildir_root,
        state_path=state_path,
        log_dir=log_dir,
        client_factory=lambda env: fake_client,
    )

    assert result.pulled_messages == 3
    assert result.exit_code == 0

    # Watermark must be at UID 3 after successful run
    state = ImapSyncState(state_path)
    assert state.get_watermark("INBOX", "/", 100) == 3


def test_sync_native_increments_watermark_per_uid(tmp_path: Path) -> None:
    """Each UID's watermark is written before moving to the next (D3 atomicity)."""
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"
    watermarks_after_each_uid: list[int | None] = []

    class _TrackingState(ImapSyncState):
        def set_watermark(self, folder, delim, uidval, last_uid):
            super().set_watermark(folder, delim, uidval, last_uid)
            watermarks_after_each_uid.append(last_uid)

    def _patched_factory(env):
        return _FakeNativeClient(
            folders=[("INBOX", "/")],
            uidvalidity_map={"INBOX": 1},
            search_map={"INBOX": [10, 20]},
            fetch_map={10: _make_eml(10), 20: _make_eml(20)},
        )

    import mailattic.imap_native as _mod

    _original_state_cls = _mod.ImapSyncState
    _mod.ImapSyncState = _TrackingState  # type: ignore[assignment]
    try:
        sync_native(
            env=_TEST_ENV,
            database_path=db_path,
            maildir_root=tmp_path / "maildir",
            state_path=state_path,
            log_dir=tmp_path / "logs",
            client_factory=_patched_factory,
        )
    finally:
        _mod.ImapSyncState = _original_state_cls  # type: ignore[assignment]

    assert watermarks_after_each_uid == [10, 20]


def test_sync_native_ingests_single_written_file_and_reuses_connection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Native sync must not re-scan the whole maildir on each UID.

    add-mcp-sync-status wiring opens two additional short-lived connections
    (sync-state start/end bookkeeping) around the per-folder loop; the fake
    ``_Connection`` here has no ``execute()``, so those bookkeeping writes
    raise and are swallowed (per "A failure to record sync state SHALL NOT
    alter the outcome of the sync"), leaving this test's own commit/watermark
    event trace from the per-UID ingest loop untouched. The assertion below
    identifies the folder-ingest connection via ``ingest_calls`` rather than
    assuming it is the first connection opened.
    """
    state_path = tmp_path / ".imap_watermarks.json"
    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 1},
        search_map={"INBOX": [1, 2, 3]},
        fetch_map={1: _make_eml(1), 2: _make_eml(2), 3: _make_eml(3)},
    )
    events: list[tuple[str, int | None]] = []
    connections: list[Any] = []
    ingest_calls: list[tuple[Any, Path, Path]] = []

    class _Connection:
        def __init__(self) -> None:
            self.commit_count = 0
            self.close_count = 0

        def execute(self, *_args) -> None:
            # #223: the loop writes progress rows on this connection.
            pass

        def commit(self) -> None:
            self.commit_count += 1
            events.append(("commit", None))

        def close(self) -> None:
            self.close_count += 1

    class _TrackingState(ImapSyncState):
        def set_watermark(self, folder, delim, uidval, last_uid):
            events.append(("watermark", last_uid))
            super().set_watermark(folder, delim, uidval, last_uid)

    def _connect_database(database_path):
        connection = _Connection()
        connections.append(connection)
        return connection

    def _import_maildir_message(
        *,
        connection,
        source,
        message_path,
        attachment_root,
    ):
        path = Path(message_path)
        uid = int(path.stem.removeprefix("uid-"))
        events.append(("ingest", uid))
        ingest_calls.append((connection, source.path, path))
        return ImportSummary(imported_messages=1)

    import mailattic.imap_native as _mod

    monkeypatch.setattr(_mod, "ImapSyncState", _TrackingState)
    monkeypatch.setattr(_mod, "connect_database", _connect_database)
    monkeypatch.setattr(_mod, "import_maildir_message", _import_maildir_message)
    # This test's subject is the folder-ingest connection and the ordering of
    # the ingest/commit/watermark chain. Sync-state bookkeeping opens its own
    # short-lived connections either side of that chain, which is noise here
    # and is covered on its own in tests/test_sync_state.py. Stub it out so
    # the counts and the event list describe only what this test is about --
    # `_Connection` deliberately has no `execute`, so leaving the real
    # bookkeeping in place would merely have it fail silently.
    monkeypatch.setattr(_mod, "_write_sync_state_start", lambda *_args: True)
    monkeypatch.setattr(_mod, "_write_sync_state_end", lambda *_args: None)

    result = sync_native(
        env=_TEST_ENV,
        database_path=tmp_path / "test.db",
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: fake_client,
    )

    assert result.pulled_messages == 3
    assert result.inserted_messages == 3
    assert result.failure_count == 0

    folder_connection = ingest_calls[0][0]
    # One connection for the whole folder loop -- that reuse is the point of
    # this test. Sync-state bookkeeping is stubbed out above, so its own
    # connections are not counted here.
    assert len(connections) == 1
    assert folder_connection.commit_count == 3
    assert folder_connection.close_count == 1
    assert [path.name for _, _, path in ingest_calls] == [
        "uid-1.eml",
        "uid-2.eml",
        "uid-3.eml",
    ]
    assert all(
        source_path != message_path for _, source_path, message_path in ingest_calls
    )
    assert all(connection is folder_connection for connection, _, _ in ingest_calls)
    assert events == [
        ("ingest", 1),
        ("commit", None),
        ("watermark", 1),
        ("ingest", 2),
        ("commit", None),
        ("watermark", 2),
        ("ingest", 3),
        ("commit", None),
        ("watermark", 3),
    ]


def test_sync_native_stops_folder_on_uid_failure(tmp_path: Path) -> None:
    """D3 fail-stop: if UID N's fetch fails, UIDs N+1..end are not processed."""
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"

    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 1},
        search_map={"INBOX": [1, 2, 3]},
        fetch_map={1: _make_eml(1), 2: _make_eml(2)},
        fail_on_uid=2,  # UID 2 fetch raises
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: fake_client,
    )

    # UID 1 succeeded; UID 2 failed → folder stopped → failure_count=1
    assert result.failure_count == 1
    # Watermark must be at UID 1 (last successful), not 2 or 3
    state = ImapSyncState(state_path)
    assert state.get_watermark("INBOX", "/", 1) == 1


def test_sync_native_preserves_completed_counts_on_uid_fetch_failure(
    tmp_path: Path,
) -> None:
    """#194: UID 1 durably stored, UID 2's FETCH fails -- the folder's

    completed work (UID 1) must still be reflected in the returned counts,
    not zeroed out just because the folder as a whole raised.
    """
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"

    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 12345},
        search_map={"INBOX": [101, 102, 103]},
        fetch_map={101: _make_eml(101)},
        fail_on_uid=102,  # UID 102 fetch raises; 103 never attempted
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: fake_client,
    )

    with sqlite3.connect(db_path) as connection:
        stored = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    assert result.exit_code == 1
    assert ImapSyncState(state_path).get_watermark("INBOX", "/", 12345) == 101
    assert result.pulled_messages == 1
    assert result.inserted_messages == stored == 1
    # #194 rework: the original exception's type + message (what an operator
    # needs to diagnose the real cause) must survive the counts-carrying wrap.
    assert result.failure_stage == "sync"
    assert "RuntimeError: Simulated fetch failure for UID 102" in (
        result.failure_message or ""
    )


_EML_WITH_ATTACHMENT = (
    b"From: sender@example.com\r\n"
    b"To: rcpt@example.com\r\n"
    b"Subject: Test Message %(uid)d with attachment\r\n"
    b"Message-ID: <test-%(uid)d@example.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="%(boundary)s"\r\n'
    b"\r\n"
    b"--%(boundary)s\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"Body of message %(uid)d\r\n"
    b"--%(boundary)s\r\n"
    b"Content-Type: application/octet-stream\r\n"
    b'Content-Disposition: attachment; filename="file%(uid)d.bin"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"AAAA\r\n"
    b"--%(boundary)s--\r\n"
)


def _make_eml_with_attachment(uid: int) -> bytes:
    return _EML_WITH_ATTACHMENT % {
        b"uid": uid,
        b"boundary": f"BOUNDARY{uid}".encode(),
    }


class _CommitFailsOnSecondCallConnection:
    """Wraps a real sqlite3.Connection; ``commit()`` raises on its 2nd call.

    Captures ``in_transaction`` at the moment of that 2nd call so the test
    can assert the precondition an implicit transaction was genuinely open
    (the row is not yet durable) when the synthetic failure hits -- this is
    the nested-SAVEPOINT case (an earlier `ingest_failures` INSERT already
    opened the transaction), not the outermost-SAVEPOINT-already-committed
    case from the withdrawn first #194 follow-up attempt.
    """

    def __init__(self, real_connection: Any) -> None:
        self._real = real_connection
        self._commit_calls = 0
        self.in_transaction_at_second_commit: bool | None = None

    def commit(self) -> None:
        self._commit_calls += 1
        if self._commit_calls == 2:
            self.in_transaction_at_second_commit = self._real.in_transaction
            raise sqlite3.OperationalError("simulated commit failure for UID 102")
        self._real.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_sync_native_excludes_uid_whose_commit_never_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#194 follow-up (Codex round 2 Track B): UID 102 carries an attachment
    CAS-store failure, recorded (via ``record_ingest_failure``, an INSERT)
    BEFORE UID 102's own message SAVEPOINT. That prior INSERT opens an
    implicit transaction, so the message SAVEPOINT is nested rather than
    outermost and RELEASE does not commit it -- durability is decided only
    by the outer per-UID ``connection.commit()``. When that commit itself
    raises, ``inserted_messages`` must reflect only what actually persisted
    (UID 101; UID 102's message + failure rows both roll back on close).
    """
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"

    import mailattic.parser as parser_mod

    def _raising_write_attachment(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated CAS store failure")

    monkeypatch.setattr(
        parser_mod.attachment_store, "write_attachment", _raising_write_attachment
    )

    real_connect_database = imap_native.connect_database
    created_connections: list[_CommitFailsOnSecondCallConnection] = []

    def _fake_connect_database(path: Path) -> _CommitFailsOnSecondCallConnection:
        wrapped = _CommitFailsOnSecondCallConnection(real_connect_database(path))
        created_connections.append(wrapped)
        return wrapped

    monkeypatch.setattr(imap_native, "connect_database", _fake_connect_database)

    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 12345},
        search_map={"INBOX": [101, 102, 103]},
        fetch_map={101: _make_eml(101), 102: _make_eml_with_attachment(102)},
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        attachment_root=tmp_path / "cas",
        client_factory=lambda env: fake_client,
    )

    # connect_database is also called by the sync-state start/end bookkeeping
    # connections (unrelated to per-folder ingest); find the one whose
    # commit() call sequence actually reached the injected 2nd-call failure.
    folder_connections = [
        conn
        for conn in created_connections
        if conn.in_transaction_at_second_commit is not None
    ]
    assert len(folder_connections) == 1
    # Precondition evidence: the implicit transaction opened by the
    # attachment-failure INSERT was still open when the synthetic commit
    # failure hit -- i.e. UID 102's message row was not yet durable.
    assert folder_connections[0].in_transaction_at_second_commit is True

    with sqlite3.connect(db_path) as connection:
        stored = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    assert result.exit_code == 1
    assert result.failure_stage == "sync"
    assert "simulated commit failure for UID 102" in (result.failure_message or "")
    assert ImapSyncState(state_path).get_watermark("INBOX", "/", 12345) == 101
    assert result.inserted_messages == stored == 1


def test_sync_native_bulk_guard_refuses_without_confirm_fn(tmp_path: Path) -> None:
    """D7: new folder with > threshold UIDs and no confirm_fn → failure."""
    db_path = _make_db(tmp_path)
    many_uids = list(range(1, 502))
    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 1},
        search_map={"INBOX": many_uids},
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        bulk_threshold=500,
        confirm_fn=None,
        client_factory=lambda env: fake_client,
    )

    assert result.failure_count == 1
    assert result.pulled_messages == 0


def test_sync_native_bulk_guard_failure_message_is_captured(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 1},
        search_map={"INBOX": list(range(1, 502))},
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        bulk_threshold=500,
        confirm_fn=None,
        client_factory=lambda env: fake_client,
    )

    assert result.failure_stage == "bulk_guard"
    assert result.failure_message is not None
    assert "bulk" in result.failure_message.lower() or "500" in result.failure_message
    assert result.exit_code == 1


def test_sync_native_bulk_guard_proceeds_with_confirm_fn(tmp_path: Path) -> None:
    """D7: confirm_fn returning True allows bulk fetch."""
    db_path = _make_db(tmp_path)
    many_uids = list(range(1, 502))
    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 1},
        search_map={"INBOX": many_uids},
        fetch_map={u: _make_eml(u) for u in many_uids},
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        bulk_threshold=500,
        confirm_fn=lambda n: True,
        client_factory=lambda env: fake_client,
    )

    assert result.failure_count == 0
    assert result.pulled_messages == 501


def test_sync_native_uidvalidity_change_rescans_folder(tmp_path: Path) -> None:
    """D4: UIDVALIDITY change → None watermark → full rescan from UID 1."""
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"

    # Set watermark for old UIDVALIDITY
    state = ImapSyncState(state_path)
    state.set_watermark("INBOX", "/", 111, 50)

    # Server now has new UIDVALIDITY = 222 with UIDs 1-3
    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/")],
        uidvalidity_map={"INBOX": 222},
        search_map={"INBOX": [1, 2, 3]},
        fetch_map={1: _make_eml(1), 2: _make_eml(2), 3: _make_eml(3)},
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        confirm_fn=lambda n: True,
        client_factory=lambda env: fake_client,
    )

    assert result.pulled_messages == 3
    new_state = ImapSyncState(state_path)
    # New generation watermark written; old generation watermark untouched
    assert new_state.get_watermark("INBOX", "/", 222) == 3
    assert new_state.get_watermark("INBOX", "/", 111) == 50


def test_sync_native_returns_correct_result_shape(tmp_path: Path) -> None:
    """smoke: SyncExecutionResult fields are correctly populated."""
    db_path = _make_db(tmp_path)
    fake_client = _FakeNativeClient(
        folders=[("INBOX", "/"), ("Sent", "/")],
        uidvalidity_map={"INBOX": 1, "Sent": 2},
        search_map={"INBOX": [1, 2], "Sent": [1]},
        fetch_map={1: _make_eml(1), 2: _make_eml(2)},
    )

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        client_factory=lambda env: fake_client,
    )

    assert result.pulled_messages == 3  # 2 + 1
    assert result.folder_count == 2
    # Each UID import is scoped to its written file; only Sent UID-1 is a
    # cross-folder dedup (same message-id as INBOX UID-1).
    assert result.inserted_messages == 2
    assert result.deduplicated_messages == 1
    assert result.failure_count == 0
    assert result.exit_code == 0


def test_sync_native_idle_second_round_does_not_refetch_or_regress_watermark(
    tmp_path: Path,
) -> None:
    """Issue #192: with no new mail, ``UID <watermark+1>:*`` can legally have
    its range endpoints reversed by the server (RFC 9051 section 6.4.9) and
    answer with the last synced UID. The second sync round must not re-fetch
    that UID, and the watermark must not regress.
    """
    db_path = _make_db(tmp_path)
    state_path = tmp_path / ".imap_watermarks.json"
    list_data = [b'(\\HasNoChildren) "/" INBOX']
    fetch_map = {99: _make_eml(99), 100: _make_eml(100)}

    first_conn = _MockImapConn(
        list_data=list_data,
        status_map={"INBOX": 1},
        search_map={"INBOX": [99, 100]},
        fetch_map=fetch_map,
    )
    first_result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=first_conn),
    )
    assert first_result.inserted_messages == 2
    assert ImapSyncState(state_path).get_watermark("INBOX", "/", 1) == 100

    # Second round: mailbox unchanged, so the server's real max UID is still
    # 100 while the client asks for "UID 101:*" -- exactly the reversed-range
    # shape from the issue.
    second_conn = _MockImapConn(
        list_data=list_data,
        status_map={"INBOX": 1},
        search_map={"INBOX": [99, 100]},
        fetch_map=fetch_map,
    )
    second_result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=second_conn),
    )

    assert second_conn.fetch_calls == []
    assert second_result.inserted_messages == 0
    assert ImapSyncState(state_path).get_watermark("INBOX", "/", 1) == 100

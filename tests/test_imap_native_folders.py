"""Folder identity split: display name vs. wire name (add-mutf7-folder-codec).

Written first per TDD — these cases run red against
the pre-change code (single-string folder identity) and green after tasks
2.2/2.3 land the display/wire pair.

Design reference: openspec/changes/add-mutf7-folder-codec/design.md Decision 1
and Decision 2; spec reference:
openspec/changes/add-mutf7-folder-codec/specs/imap-sync/spec.md.

Mocking is done at the low-level IMAP connection (``_connection=``) so the
real ``NativeImapClient.list_folders()`` — which is what performs the mUTF-7
decode — is exercised, not bypassed.
"""

from __future__ import annotations

import imaplib
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mailattic.imap_native import (
    ImapFolder,
    NativeImapClient,
    resolve_folders,
    status_uidvalidity,
    sync_native,
)
from mailattic.imap_state import ImapSyncState
from mailattic.imap_sync import estimate_sync_run
from mailattic.storage import initialize_database

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

# Codec example from specs/imap-sync/spec.md
_SENT_WIRE = "&W8RO9lCZTv0-"
_SENT_DISPLAY = "寄件備份"


def _make_eml(uid: int) -> bytes:
    return _MINIMAL_EML.replace(b"{uid}", str(uid).encode())


class _TrackingMockConn:
    """Injectable mock for imaplib.IMAP4_SSL that records mailbox arguments.

    Mirrors the shape of ``_MockImapConn`` in tests/test_imap_native.py, with
    added call-tracking for STATUS and SELECT mailbox arguments so tests can
    assert the exact wire string transmitted to the server.
    """

    def __init__(
        self,
        *,
        list_data: list[bytes],
        status_map: dict[str, int] | None = None,
        search_map: dict[str, list[int]] | None = None,
        fetch_map: dict[int, bytes] | None = None,
    ) -> None:
        self._list_data = list_data
        self._status_map = status_map or {}
        self._search_map = search_map or {}
        self._fetch_map = fetch_map or {}
        self._selected: str | None = None
        self.status_calls: list[str] = []
        self.select_calls: list[str] = []

    def list(self) -> tuple[str, list[bytes | None]]:
        return "OK", self._list_data

    def status(self, folder: str, items: str) -> tuple[str, list[bytes]]:
        name = folder.strip('"')
        self.status_calls.append(name)
        uidval = self._status_map.get(name, 1)
        return "OK", [f"* STATUS {folder} (UIDVALIDITY {uidval})".encode()]

    def select(self, folder: str, readonly: bool = True) -> tuple[str, list[bytes]]:
        name = folder.strip('"')
        self.select_calls.append(name)
        self._selected = name
        return "OK", [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        cmd = command.upper()
        if cmd == "SEARCH":
            uids = self._search_map.get(self._selected or "", [])
            if args and str(args[0]).startswith("UID "):
                min_uid_str = str(args[0])[4:].split(":")[0]
                try:
                    min_uid = int(min_uid_str)
                    uids = [u for u in uids if u >= min_uid]
                except ValueError:
                    pass
            return "OK", [" ".join(str(u) for u in uids).encode()]
        if cmd == "FETCH":
            uid_str = args[0]
            uid = int(uid_str)
            body = self._fetch_map.get(uid, _make_eml(uid))
            return "OK", [(b"1 (BODY[] {10}", body), b")"]
        return "OK", [b""]

    def close(self) -> None:
        pass

    def logout(self) -> None:
        pass


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    initialize_database(db_path)
    return db_path


def _fetch_one_message(db_path: Path) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT folder_path, logical_locator, pst_source FROM messages LIMIT 1"
        ).fetchone()
        assert row is not None, "expected exactly one persisted message row"
        return row
    finally:
        conn.close()


def test_chinese_folder_persists_display_name_but_transmits_wire_name(
    tmp_path: Path,
) -> None:
    """Scenario: Chinese folder name is persisted in readable form.

    LIST response carries the wire form ``&W8RO9lCZTv0-``. After sync:
      - persisted folder_path == 寄件備份 (display name)
      - logical_locator starts with 寄件備份/ (display name)
      - SELECT/STATUS mailbox args are the wire form, never the display form
      - the maildir staging directory is named after the wire form
    """
    conn = _TrackingMockConn(
        list_data=[f'(\\HasNoChildren) "/" {_SENT_WIRE}'.encode()],
        status_map={_SENT_WIRE: 100},
        search_map={_SENT_WIRE: [1]},
        fetch_map={1: _make_eml(1)},
    )
    db_path = _make_db(tmp_path)
    maildir_root = tmp_path / "maildir"

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=maildir_root,
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=conn),
    )

    assert result.inserted_messages == 1
    assert result.failure_count == 0

    row = _fetch_one_message(db_path)
    assert row["folder_path"] == _SENT_DISPLAY
    assert row["logical_locator"].startswith(f"{_SENT_DISPLAY}/")
    assert _SENT_DISPLAY in row["pst_source"]
    assert _SENT_WIRE not in row["pst_source"]

    # The wire form, never the display form, was transmitted to the server.
    assert conn.status_calls == [_SENT_WIRE]
    assert conn.select_calls == [_SENT_WIRE]

    # The maildir staging directory is named after the wire form.
    staged_dir = maildir_root / "testaccount" / _SENT_WIRE
    assert staged_dir.exists()
    assert (staged_dir / "new" / "uid-1.eml").exists()
    display_named_dir = maildir_root / "testaccount" / _SENT_DISPLAY
    assert not display_named_dir.exists()


def test_malformed_folder_name_is_transmitted_verbatim_not_re_encoded(
    tmp_path: Path,
) -> None:
    """Scenario: malformed folder name does not stop a sync and is not re-encoded.

    ``&AB`` is not a well-formed mUTF-7 shift sequence. It must be used as-is
    on SELECT/STATUS — specifically NOT re-encoded to ``&-AB`` (which is what
    naively encoding the pass-through display string would produce).
    """
    malformed = "&AB"
    conn = _TrackingMockConn(
        list_data=[f'(\\HasNoChildren) "/" {malformed}'.encode()],
        status_map={malformed: 1},
        search_map={malformed: []},
    )
    db_path = _make_db(tmp_path)

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=conn),
    )

    assert result.failure_count == 0
    assert conn.status_calls == [malformed]
    assert conn.select_calls == [malformed]
    assert "&-AB" not in conn.select_calls
    assert "&-AB" not in conn.status_calls


def test_existing_wire_keyed_watermark_is_honored(tmp_path: Path) -> None:
    """Scenario: existing watermarks survive the change.

    A watermark recorded under the wire name before this change must still
    be found, so the folder is not re-searched from UID 1.
    """
    state_path = tmp_path / ".imap_watermarks.json"
    uidvalidity = 100
    delimiter = "/"
    # Pre-existing watermark, keyed on the wire name (as it always has been).
    ImapSyncState(state_path).set_watermark(_SENT_WIRE, delimiter, uidvalidity, 5)

    conn = _TrackingMockConn(
        list_data=[f'(\\HasNoChildren) "/" {_SENT_WIRE}'.encode()],
        status_map={_SENT_WIRE: uidvalidity},
        search_map={_SENT_WIRE: [6, 7]},
        fetch_map={6: _make_eml(6), 7: _make_eml(7)},
    )
    db_path = _make_db(tmp_path)

    result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=tmp_path / "maildir",
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=conn),
    )

    assert result.failure_count == 0
    # Only UIDs 6 and 7 were pulled — the folder was NOT re-searched from UID 1
    # (which would also have re-fetched a hypothetical UID <= 5).
    assert result.pulled_messages == 2

    new_state = ImapSyncState(state_path)
    assert new_state.get_watermark(_SENT_WIRE, delimiter, uidvalidity) == 7


def test_reingesting_existing_message_is_dedup_noop(tmp_path: Path) -> None:
    """Scenario: dedup performs no UPDATE (design Decision 2).

    Re-ingesting an already-stored message (same Message-ID) inserts no
    second row and leaves that row's folder_path untouched. This asserts
    pre-existing dedup behavior is unchanged by the folder-identity split —
    it does not modify dedup logic.
    """
    db_path = _make_db(tmp_path)
    maildir_root = tmp_path / "maildir"
    state_path = tmp_path / ".imap_watermarks.json"

    # First sync run: ingest UID 1 from the Chinese Sent folder.
    conn1 = _TrackingMockConn(
        list_data=[f'(\\HasNoChildren) "/" {_SENT_WIRE}'.encode()],
        status_map={_SENT_WIRE: 100},
        search_map={_SENT_WIRE: [1]},
        fetch_map={1: _make_eml(1)},
    )
    first_result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=maildir_root,
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=conn1),
    )
    assert first_result.inserted_messages == 1

    row_before = _fetch_one_message(db_path)
    assert row_before["folder_path"] == _SENT_DISPLAY

    # Force a full resync (watermark reset) so the same UID is re-fetched and
    # re-ingested through the dedup gate.
    ImapSyncState(state_path).clear_all()
    conn2 = _TrackingMockConn(
        list_data=[f'(\\HasNoChildren) "/" {_SENT_WIRE}'.encode()],
        status_map={_SENT_WIRE: 100},
        search_map={_SENT_WIRE: [1]},
        fetch_map={1: _make_eml(1)},
    )
    second_result = sync_native(
        env=_TEST_ENV,
        database_path=db_path,
        maildir_root=maildir_root,
        state_path=state_path,
        log_dir=tmp_path / "logs",
        client_factory=lambda env: NativeImapClient(env, _connection=conn2),
    )

    assert second_result.inserted_messages == 0
    assert second_result.deduplicated_messages == 1

    conn = sqlite3.connect(str(db_path))
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    finally:
        conn.close()
    assert count == 1, "re-ingesting an existing message must not insert a second row"

    row_after = _fetch_one_message(db_path)
    assert row_after["folder_path"] == _SENT_DISPLAY


# ---------------------------------------------------------------------------
# resolve_folders: --folder resolution must not pick the wrong mailbox on a
# display-name collision (Codex semantic review, Fix 2).
# ---------------------------------------------------------------------------


class _StaticFoldersClient:
    """Fake client exposing a fixed list_folders() result to resolve_folders."""

    def __init__(self, folders: list[ImapFolder]) -> None:
        self._folders = folders

    def list_folders(self) -> list[ImapFolder]:
        return self._folders


def test_resolve_folders_wire_name_selects_exact_folder() -> None:
    target = ImapFolder(display_name="草稿", wire_name="&g0l6Pw-", delimiter="/")
    other = ImapFolder(display_name="INBOX", wire_name="INBOX", delimiter="/")
    client = _StaticFoldersClient([other, target])

    resolved = resolve_folders(client=client, folder="&g0l6Pw-")

    assert resolved == [target]


def test_resolve_folders_wire_match_wins_over_display_collision() -> None:
    # Two folders share the display name "&AB": one well-formed (wire "&-AB"
    # decodes to "&AB"), one malformed (wire "&AB" passes through as "&AB").
    # Requesting "&AB" must select the folder whose WIRE name is "&AB" --
    # an exact wire match always wins outright.
    well_formed = ImapFolder(display_name="&AB", wire_name="&-AB", delimiter="/")
    malformed = ImapFolder(display_name="&AB", wire_name="&AB", delimiter="/")
    client = _StaticFoldersClient([well_formed, malformed])

    resolved = resolve_folders(client=client, folder="&AB")

    assert resolved == [malformed]


def test_resolve_folders_ambiguous_display_collision_raises() -> None:
    # Two folders share a display name and neither matches by wire name --
    # genuinely ambiguous; must raise rather than silently choosing one.
    folder_a = ImapFolder(display_name="草稿", wire_name="&g0l6Pw-", delimiter="/")
    folder_b = ImapFolder(display_name="草稿", wire_name="&g0l6Pw-2", delimiter="/")
    client = _StaticFoldersClient([folder_a, folder_b])

    with pytest.raises(RuntimeError, match="ambiguously"):
        resolve_folders(client=client, folder="草稿")


# ---------------------------------------------------------------------------
# #281: STATUS failure on a --folder value LIST never showed must say so.
# ---------------------------------------------------------------------------


class _NoStatusConn(_TrackingMockConn):
    """STATUS answers NO for every mailbox not in ``ok_names``."""

    def __init__(self, *, ok_names: set[str] = frozenset(), **kw: Any) -> None:  # type: ignore[assignment]
        super().__init__(**kw)
        self._ok_names = ok_names

    def status(self, folder: str, items: str) -> tuple[str, list[bytes]]:
        if folder.strip('"') in self._ok_names:
            return super().status(folder, items)
        return "NO", [b"STATUS failed: no such mailbox"]


def _list_line(wire: str) -> bytes:
    return f'(\\HasNoChildren) "/" "{wire}"'.encode()


def _client(conn: _TrackingMockConn) -> NativeImapClient:
    return NativeImapClient(_TEST_ENV, _connection=conn)


def _run_real(tmp_path: Path, conn: _TrackingMockConn, folder: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return sync_native(
        env=_TEST_ENV,
        database_path=_make_db(tmp_path),
        maildir_root=tmp_path / "maildir",
        state_path=tmp_path / ".imap_watermarks.json",
        log_dir=tmp_path / "logs",
        folder=folder,
        client_factory=lambda env: _client(conn),
    )


def _run_dry(tmp_path: Path, conn: _TrackingMockConn, folder: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    env_file = tmp_path / "mailbox.env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in _TEST_ENV.items()) + "\n", encoding="utf-8"
    )
    return estimate_sync_run(
        env_file=env_file,
        maildir_root=tmp_path / "maildir",
        folder=folder,
        client_factory=lambda env: _client(conn),
    )


def _unmatched_conn() -> _NoStatusConn:
    return _NoStatusConn(
        list_data=[_list_line("INBOX"), _list_line("Parent/Leaf"), _list_line("A/leaf")]
    )


def test_status_failure_message_carries_server_response() -> None:
    # Name matches LIST (listed) but STATUS says NO: only the server response is added.
    conn = _NoStatusConn(list_data=[_list_line("Parent/Leaf")])
    client = _client(conn)
    (folder,) = resolve_folders(client=client, folder="Parent/Leaf")
    with pytest.raises(RuntimeError) as ei:
        status_uidvalidity(client, folder)
    text = str(ei.value)
    assert "NO" in text and "no such mailbox" in text
    assert "did not match" not in text and "ending in that name" not in text


def test_unmatched_folder_status_failure_says_no_match_and_asks_for_full_path() -> None:
    client = _client(_unmatched_conn())
    (folder,) = resolve_folders(client=client, folder="Nowhere")
    with pytest.raises(RuntimeError) as ei:
        status_uidvalidity(client, folder)
    text = str(ei.value)
    assert "did not match any folder the server listed" in text
    assert "full path" in text and "delimiter" in text
    assert "no such mailbox" in text
    assert "ending in that name" not in text


def test_unmatched_folder_lists_leaf_candidates_case_insensitively() -> None:
    client = _client(_unmatched_conn())
    (folder,) = resolve_folders(client=client, folder="LEAF")
    with pytest.raises(RuntimeError) as ei:
        status_uidvalidity(client, folder)
    text = str(ei.value)
    assert "'Parent/Leaf'" in text and "'A/leaf'" in text
    assert "INBOX" not in text.split("ending in that name")[1]


def test_real_sync_and_dry_run_report_the_same_message(tmp_path: Path) -> None:
    real = _run_real(tmp_path / "r", _unmatched_conn(), "leaf")
    with pytest.raises(RuntimeError) as ei:
        _run_dry(tmp_path / "d", _unmatched_conn(), "leaf")
    dry_text = str(ei.value)
    assert real.failure_message is not None
    assert real.failure_message.endswith(dry_text)
    assert "did not match any folder the server listed" in dry_text
    assert "'Parent/Leaf'" in dry_text


def test_unmatched_folder_with_ok_status_still_syncs_and_estimates(
    tmp_path: Path,
) -> None:
    def conn() -> _NoStatusConn:
        return _NoStatusConn(
            list_data=[_list_line("INBOX")],
            ok_names={"Hidden"},
            search_map={"Hidden": [1]},
        )

    summary = _run_dry(tmp_path / "d", conn(), "Hidden")
    assert summary.estimated_new_messages == 1
    result = _run_real(tmp_path / "r", conn(), "Hidden")
    assert result.failure_count == 0 and result.inserted_messages == 1


class _BadStatusConn(_TrackingMockConn):
    """STATUS answers BAD, which imaplib raises instead of returning."""

    def status(self, folder: str, items: str) -> tuple[str, list[Any]]:
        raise imaplib.IMAP4.error("STATUS command error: BAD [b'invalid mailbox']")


def test_unmatched_folder_bad_status_reply_also_says_no_match() -> None:
    client = _client(_BadStatusConn(list_data=[_list_line("Parent/Leaf")]))
    (folder,) = resolve_folders(client=client, folder="leaf")
    with pytest.raises(RuntimeError) as ei:
        status_uidvalidity(client, folder)
    text = str(ei.value)
    assert "BAD" in text and "invalid mailbox" in text
    assert "did not match any folder the server listed" in text
    assert "'Parent/Leaf'" in text


def test_dropped_connection_during_status_is_not_relabelled_as_no_match() -> None:
    class _AbortConn(_TrackingMockConn):
        def status(self, folder: str, items: str) -> tuple[str, list[Any]]:
            raise imaplib.IMAP4.abort("socket error: EOF")

    client = _client(_AbortConn(list_data=[_list_line("Parent/Leaf")]))
    (folder,) = resolve_folders(client=client, folder="leaf")
    with pytest.raises(imaplib.IMAP4.abort):
        status_uidvalidity(client, folder)


def test_candidate_lookup_failure_keeps_the_status_diagnosis() -> None:
    class _SecondListFailsConn(_NoStatusConn):
        calls = 0

        def list(self) -> tuple[str, list[bytes | None]]:
            self.calls += 1
            if self.calls > 1:
                return "NO", [b"LIST unavailable"]
            return super().list()

    client = _client(_SecondListFailsConn(list_data=[_list_line("Parent/Leaf")]))
    (folder,) = resolve_folders(client=client, folder="leaf")
    with pytest.raises(RuntimeError) as ei:
        status_uidvalidity(client, folder)
    text = str(ei.value)
    assert "no such mailbox" in text
    assert "did not match any folder the server listed" in text
    assert "ending in that name" not in text

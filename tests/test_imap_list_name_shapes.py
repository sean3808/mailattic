"""Folder names in a LIST response: atom, quoted string and literal (#300).

A real ``imaplib`` client talks to a throwaway IMAP server bound to
127.0.0.1 on an ephemeral port (``IMAP_SECURITY=plain``), so the shapes the
parser sees are the ones ``imaplib`` really produces -- notably the literal
spelling ``[(b'() "/" {7}', b'Archive'), b'']``.  The same server records the
mailbox name it receives in STATUS / EXAMINE, which proves the name the
parser returns survives the trip back to the server.

Names are invented; ``mUTF-7`` ``&W8RO9lCZTv0-`` is the codec example used by
``tests/test_imap_native_folders.py``.
"""

from __future__ import annotations

import socketserver
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from mailattic.imap_native import NativeImapClient, _parse_list_entry, sync_native
from mailattic.imap_state import ImapSyncState
from mailattic.imap_sync import estimate_sync_run
from mailattic.storage import initialize_database

_EML = (
    b"From: sender@example.com\r\n"
    b"To: rcpt@example.com\r\n"
    b"Subject: Shape test\r\n"
    b"Message-ID: <shape-1@example.com>\r\n"
    b"\r\n"
    b"Body\r\n"
)
_UIDVALIDITY = 7


def _server_unquote(arg: str) -> str | None:
    """The server's own reading of a mailbox argument (atom or quoted)."""
    if not arg.startswith('"'):
        return arg
    out: list[str] = []
    i = 1
    while i < len(arg):
        c = arg[i]
        if c == "\\" and i + 1 < len(arg):
            out.append(arg[i + 1])
            i += 2
        elif c == '"':
            return "".join(out) if i == len(arg) - 1 else None
        else:
            out.append(c)
            i += 1
    return None


class _Handler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self) -> None:
        fake: _FakeImap = self.server.fake  # type: ignore[attr-defined]
        self.wfile.write(b"* OK ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            tag, _, rest = line.decode("ascii").rstrip("\r\n").partition(" ")
            cmd, _, args = rest.partition(" ")
            cmd = cmd.upper()
            ok = f"{tag} OK done\r\n".encode()
            if cmd == "CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1\r\n" + ok)
            elif cmd == "LOGIN":
                self.wfile.write(ok)
            elif cmd == "LIST":
                self.wfile.write(fake.list_payload + ok)
            elif cmd == "STATUS":
                name_arg = args.rpartition(" (")[0]
                name = _server_unquote(name_arg)
                fake.received.append(("STATUS", name))
                if name != fake.folder:
                    self.wfile.write(f"{tag} NO no such mailbox\r\n".encode())
                else:
                    reply = f"* STATUS {name_arg} (UIDVALIDITY {_UIDVALIDITY})\r\n"
                    self.wfile.write(reply.encode() + ok)
            elif cmd in ("EXAMINE", "SELECT"):
                name = _server_unquote(args)
                fake.received.append((cmd, name))
                if name != fake.folder:
                    self.wfile.write(f"{tag} NO no such mailbox\r\n".encode())
                else:
                    self.wfile.write(b"* 1 EXISTS\r\n" + ok)
            elif cmd == "UID" and args.upper().startswith("SEARCH ALL"):
                self.wfile.write(b"* SEARCH 1\r\n" + ok)
            elif cmd == "UID" and args.upper().startswith("SEARCH"):
                self.wfile.write(b"* SEARCH\r\n" + ok)
            elif cmd == "UID" and args.upper().startswith("FETCH"):
                head = f"* 1 FETCH (UID 1 BODY[] {{{len(_EML)}}}\r\n".encode()
                self.wfile.write(head + _EML + b")\r\n" + ok)
            elif cmd == "CLOSE":
                self.wfile.write(ok)
            elif cmd == "LOGOUT":
                self.wfile.write(b"* BYE bye\r\n" + ok)
                return
            else:
                self.wfile.write(f"{tag} BAD unknown\r\n".encode())


class _FakeImap:
    def __init__(self, folder: str, list_payload: bytes) -> None:
        self.folder = folder
        self.list_payload = list_payload
        self.received: list[tuple[str, str | None]] = []


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextmanager
def _fake_imap(folder: str, list_payload: bytes) -> Iterator[tuple[_FakeImap, dict]]:
    fake = _FakeImap(folder, list_payload)
    server = _Server(("127.0.0.1", 0), _Handler)
    server.fake = fake  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        "IMAP_HOST": "127.0.0.1",
        "IMAP_PORT": str(server.server_address[1]),
        "IMAP_USER": "user@example.com",
        "IMAP_PASSWORD": "password",
        "IMAP_ACCOUNT_NAME": "shapes",
        "IMAP_SECURITY": "plain",
    }
    try:
        yield fake, env
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _list_folders(payload: bytes, folder: str):
    with _fake_imap(folder, payload) as (_fake, env):
        client = NativeImapClient(env, timeout=10.0)
        try:
            return client.list_folders()
        finally:
            client.close()


def _sync(tmp_path: Path, folder: str, payload: bytes):
    db_path = tmp_path / "t.db"
    initialize_database(db_path)
    state_path = tmp_path / "wm.json"
    with _fake_imap(folder, payload) as (fake, env):
        result = sync_native(
            env=env,
            database_path=db_path,
            maildir_root=tmp_path / "maildir",
            state_path=state_path,
            log_dir=tmp_path / "logs",
        )
    return result, fake, db_path, state_path


def _message_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


def _lit(name: str) -> bytes:
    raw = name.encode()
    return b'* LIST (\\HasNoChildren) "/" {%d}\r\n' % len(raw) + raw + b"\r\n"


def _quoted(escaped_name: str) -> bytes:
    return f'* LIST (\\HasNoChildren) "/" "{escaped_name}"\r\n'.encode()


# --- the same folder, three spellings ---------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        b'* LIST (\\HasNoChildren) "/" Archive\r\n',
        b'* LIST (\\HasNoChildren) "/" "Archive"\r\n',
        _lit("Archive"),
    ],
    ids=["atom", "quoted", "literal"],
)
def test_plain_folder_same_name_and_syncs_in_each_spelling(
    tmp_path: Path, payload: bytes
) -> None:
    folders = _list_folders(payload, "Archive")
    assert [(f.wire_name, f.delimiter) for f in folders] == [("Archive", "/")]

    result, fake, db_path, state_path = _sync(tmp_path, "Archive", payload)
    assert result.failure_count == 0, result.failure_message
    assert result.inserted_messages == 1
    assert _message_count(db_path) == 1
    assert ("STATUS", "Archive") in fake.received
    assert ImapSyncState(state_path).get_watermark("Archive", "/", _UIDVALIDITY) == 1


def _round_trip(payload: bytes, name: str) -> list[str]:
    """LIST, then STATUS + EXAMINE the listed wire name through real imaplib.

    Returns the listed wire names; asserts the server saw *name* in both
    commands (so the name survived being quoted back).  Used for names a
    Windows maildir directory cannot hold (those cannot go through sync;
    Windows-illegal folder names are out of scope, #284 3a).
    """
    with _fake_imap(name, payload) as (fake, env):
        client = NativeImapClient(env, timeout=10.0)
        try:
            wires = [f.wire_name for f in client.list_folders()]
            assert wires == [name]
            assert client.get_uidvalidity(wires[0]) == _UIDVALIDITY
            client.select_folder(wires[0])
        finally:
            client.close()
        assert fake.received == [("STATUS", name), ("EXAMINE", name)]
        return wires


_SPACE_UTF7 = "Projects A &W8RO9lCZTv0-"


@pytest.mark.parametrize(
    "payload",
    [_lit(_SPACE_UTF7), _quoted(_SPACE_UTF7)],
    ids=["literal", "quoted"],
)
def test_name_with_space_and_mutf7_syncs(tmp_path: Path, payload: bytes) -> None:
    folders = _list_folders(payload, _SPACE_UTF7)
    assert folders[0].display_name == "Projects A 寄件備份"

    result, fake, _db, _state = _sync(tmp_path, _SPACE_UTF7, payload)
    assert result.failure_count == 0, result.failure_message
    assert result.inserted_messages == 1
    assert ("STATUS", _SPACE_UTF7) in fake.received


def test_literal_name_with_space_quote_and_mutf7_round_trips() -> None:
    name = 'Projects "A" &W8RO9lCZTv0-'
    _round_trip(_lit(name), name)


# --- quoted-string escapes ---------------------------------------------------


@pytest.mark.parametrize(
    ("escaped", "expected"),
    [
        (r"a\\b", "a\\b"),
        (r"say \"hi\"", 'say "hi"'),
        (r"ends with quote\"", 'ends with quote"'),
        ('\\"', '"'),
        (r"back\\", "back\\"),
    ],
)
def test_quoted_escapes_are_restored_and_round_trip(
    escaped: str, expected: str
) -> None:
    _round_trip(_quoted(escaped), expected)


@pytest.mark.parametrize("name", ["a\\b", 'x"y', "tail\\", 'q"'])
def test_literal_name_with_special_characters_round_trips(name: str) -> None:
    _round_trip(_lit(name), name)


# --- mixed responses and loud failure ----------------------------------------


def test_mixed_literal_and_plain_entries_all_parse() -> None:
    payload = (
        b'* LIST () "/" INBOX\r\n'
        + _lit("Has Space")
        + b'* LIST () "/" "Quoted One"\r\n'
        + _lit("Last")
    )
    folders = _list_folders(payload, "INBOX")
    assert [f.wire_name for f in folders] == [
        "INBOX",
        "Has Space",
        "Quoted One",
        "Last",
    ]


# #308: one unreadable folder name costs only that folder.
_BAD_LITERAL = b'* LIST () "/" {3}\r\n\xff\xfe\xfd\r\n'


def test_undecodable_literal_is_skipped_and_reported_not_garbled() -> None:
    with _fake_imap("Archive", _lit("Archive") + _BAD_LITERAL) as (_fake, env):
        client = NativeImapClient(env, timeout=10.0)
        try:
            folders = client.list_folders()
            skipped = list(client.skipped_listings)
        finally:
            client.close()
    assert [f.wire_name for f in folders] == ["Archive"]
    assert len(skipped) == 1
    assert "not ASCII" in skipped[0]


def test_sync_skips_undecodable_folder_records_failure_and_syncs_the_rest(
    tmp_path: Path,
) -> None:
    result, _fake, db_path, state_path = _sync(
        tmp_path, "Archive", _lit("Archive") + _BAD_LITERAL
    )
    assert result.inserted_messages == 1
    assert _message_count(db_path) == 1
    assert ImapSyncState(state_path).get_watermark("Archive", "/", _UIDVALIDITY) == 1
    assert result.failure_count == 1
    assert result.exit_code == 1
    assert result.failure_stage == "list"
    assert result.failure_message is not None
    assert "not ASCII" in result.failure_message


def test_sync_names_every_skipped_folder_in_failure_message(tmp_path: Path) -> None:
    second_bad = b'* LIST () "/" {3}\r\n\xfa\xfb\xfc\r\n'
    result, _fake, _db, _state = _sync(
        tmp_path, "Archive", _lit("Archive") + _BAD_LITERAL + second_bad
    )
    assert result.failure_count == 2
    assert result.failure_message is not None
    assert r"\xff\xfe\xfd" in result.failure_message
    assert r"\xfa\xfb\xfc" in result.failure_message


def test_folder_scoped_sync_ignores_unreadable_name_elsewhere(tmp_path: Path) -> None:
    """--folder X did not ask for the other folders; their bad name is not X's
    failure."""
    db_path = tmp_path / "t.db"
    initialize_database(db_path)
    with _fake_imap("Archive", _lit("Archive") + _BAD_LITERAL) as (_fake, env):
        result = sync_native(
            env=env,
            database_path=db_path,
            maildir_root=tmp_path / "maildir",
            state_path=tmp_path / "wm.json",
            log_dir=tmp_path / "logs",
            folder="Archive",
        )
    assert result.inserted_messages == 1
    assert result.failure_count == 0
    assert result.exit_code == 0


def test_dry_run_reports_the_same_skipped_folder(tmp_path: Path) -> None:
    with _fake_imap("Archive", _lit("Archive") + _BAD_LITERAL) as (_fake, env):
        env_file = tmp_path / "env"
        env_file.write_text(
            "".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8"
        )
        summary = estimate_sync_run(
            env_file=env_file, maildir_root=tmp_path / "maildir"
        )
    assert summary.estimated_new_messages == 1
    assert summary.folder_count == 1
    assert len(summary.skipped_folders) == 1
    assert "not ASCII" in summary.skipped_folders[0]


# --- names that parse correctly today must not move --------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (r'(\HasNoChildren) "/" INBOX', ("INBOX", "/")),
        (r'(\HasNoChildren) "." INBOX.Sent', ("INBOX.Sent", ".")),
        (r"(\Noselect) NIL [Gmail]", ("[Gmail]", "")),
        (r'(\HasNoChildren) "/" "Sent Items"', ("Sent Items", "/")),
        (r'(\HasNoChildren) "/" &W8RO9lCZTv0-', ("&W8RO9lCZTv0-", "/")),
        (r'(\HasNoChildren) "/" "&W8RO9lCZTv0- 2"', ("&W8RO9lCZTv0- 2", "/")),
        (r'(\HasNoChildren) "/" &AB', ("&AB", "/")),
        (r'(\Noselect \HasChildren) "/" "A B/C"', ("A B/C", "/")),
        (r"(\HasNoChildren) NIL Plain", ("Plain", "")),
        (r'(\HasNoChildren) "/" "Archive"  ', ("Archive", "/")),
    ],
)
def test_parse_list_entry_pinned_pre_existing_results(
    line: str, expected: tuple[str, str]
) -> None:
    assert _parse_list_entry(line) == expected


@pytest.mark.parametrize(
    "line",
    [r'(\HasNoChildren) "/" "unterminated', r'(\HasNoChildren) "/" "a" "b"'],
)
def test_malformed_quoted_name_is_skipped_not_reshaped(line: str) -> None:
    assert _parse_list_entry(line) is None

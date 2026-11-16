"""Unparseable LIST lines land in ``skipped_listings`` (#95).

Raw server bytes go through the real ``imaplib`` response parser (the throwaway
server from ``tests/test_imap_list_name_shapes.py``).  The trailing bytes
element imaplib emits right after a literal tuple stays ignored, and only
there: a lone ``)`` line elsewhere is a malformed complete response.
"""

from __future__ import annotations

import pytest

from mailattic.imap_native import NativeImapClient
from tests.test_imap_list_name_shapes import _fake_imap, _lit


def _list(payload: bytes) -> tuple[list[str], list[str]]:
    with _fake_imap("INBOX", payload) as (_fake, env):
        client = NativeImapClient(env, timeout=10.0)
        try:
            folders = client.list_folders()
            skipped = list(client.skipped_listings)
        finally:
            client.close()
    return [f.wire_name for f in folders], skipped


def test_literal_with_empty_trailer_is_listed_without_skip() -> None:
    assert _list(_lit("Archive")) == (["Archive"], [])


def test_literal_with_close_paren_trailer_is_listed_without_skip() -> None:
    payload = b'* LIST () "/" {7}\r\nArchive)\r\n'
    assert _list(payload) == (["Archive"], [])


@pytest.mark.parametrize(
    "payload",
    [b"* LIST () NIL Plain\r\n", b'* LIST () "/" Plain\r\n'],
    ids=["nil-delimiter", "unquoted-atom"],
)
def test_nil_delimiter_and_unquoted_atom_are_listed(payload: bytes) -> None:
    assert _list(payload) == (["Plain"], [])


def test_missing_close_paren_is_recorded_and_others_still_listed() -> None:
    names, skipped = _list(b'* LIST (\\Noselect "/" Broken\r\n' + _lit("Archive"))
    assert names == ["Archive"]
    assert len(skipped) == 1
    assert "could not be parsed" in skipped[0]
    assert "Broken" in skipped[0]


def test_missing_name_is_recorded() -> None:
    names, skipped = _list(b'* LIST () "/"\r\n')
    assert names == []
    assert len(skipped) == 1


def test_trailer_after_undecodable_literal_is_ignored() -> None:
    # The undecodable literal is reported once (#308); its trailer is not a
    # second, unparseable listing.
    names, skipped = _list(b'* LIST () "/" {3}\r\n\xff\xfe\xfd\r\n' + _lit("Archive"))
    assert names == ["Archive"]
    assert len(skipped) == 1
    assert "could not be parsed" not in skipped[0]


def test_lone_close_paren_not_after_literal_is_recorded() -> None:
    names, skipped = _list(b"* LIST )\r\n" + b'* LIST () "/" INBOX\r\n')
    assert names == ["INBOX"]
    assert len(skipped) == 1


def test_skip_reason_carries_only_a_capped_prefix_of_the_line() -> None:
    long_name = "X" * 500
    _names, skipped = _list(f'* LIST (\\Noselect "/" {long_name}\r\n'.encode())
    assert len(skipped) == 1
    assert "X" * 30 in skipped[0]
    assert "X" * 81 not in skipped[0]
    assert skipped[0].endswith("...")
    assert len(skipped[0]) < 200

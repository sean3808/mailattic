"""Tests for ImapSyncState — Group 1 watermark management."""

from __future__ import annotations

from pathlib import Path

from mailattic.imap_state import ImapSyncState


def test_get_watermark_returns_none_for_unknown_folder(tmp_path: Path) -> None:
    state = ImapSyncState(tmp_path / ".imap_watermarks.json")
    assert state.get_watermark("INBOX", "/", 1234567890) is None


def test_set_and_get_watermark_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / ".imap_watermarks.json"
    state = ImapSyncState(state_path)
    state.set_watermark("INBOX", "/", 1234567890, 42)
    assert state.get_watermark("INBOX", "/", 1234567890) == 42


def test_watermark_persists_across_instances(tmp_path: Path) -> None:
    state_path = tmp_path / ".imap_watermarks.json"
    ImapSyncState(state_path).set_watermark("INBOX", "/", 99, 100)
    assert ImapSyncState(state_path).get_watermark("INBOX", "/", 99) == 100


def test_different_uidvalidity_is_different_key(tmp_path: Path) -> None:
    state_path = tmp_path / ".imap_watermarks.json"
    state = ImapSyncState(state_path)
    state.set_watermark("INBOX", "/", 111, 50)
    # Different UIDVALIDITY → separate generation → unknown
    assert state.get_watermark("INBOX", "/", 222) is None


def test_set_watermark_is_atomic_no_tmp_leftover(tmp_path: Path) -> None:
    state_path = tmp_path / ".imap_watermarks.json"
    state = ImapSyncState(state_path)
    state.set_watermark("INBOX", "/", 1, 10)
    assert not (tmp_path / ".imap_watermarks.json.tmp").exists()
    assert state_path.exists()


def test_clear_all_removes_state_file_and_resets_memory(tmp_path: Path) -> None:
    state_path = tmp_path / ".imap_watermarks.json"
    state = ImapSyncState(state_path)
    state.set_watermark("INBOX", "/", 1, 10)
    assert state_path.exists()
    state.clear_all()
    assert not state_path.exists()
    assert state.get_watermark("INBOX", "/", 1) is None


def test_clear_all_on_nonexistent_file_does_not_raise(tmp_path: Path) -> None:
    state = ImapSyncState(tmp_path / ".imap_watermarks.json")
    state.clear_all()  # Must not raise


def test_encoded_folder_name_as_key(tmp_path: Path) -> None:
    state_path = tmp_path / ".imap_watermarks.json"
    state = ImapSyncState(state_path)
    folder = "INBOX/Sub&APY-folder"
    state.set_watermark(folder, ".", 9999, 77)
    assert state.get_watermark(folder, ".", 9999) == 77
    # Different folder with same prefix does not match
    assert state.get_watermark("INBOX/Sub", ".", 9999) is None


def test_key_uses_null_byte_separator_to_prevent_collision(tmp_path: Path) -> None:
    """Folder 'A/B' with delim '/' must not collide with folder 'A' with delim '/B'."""
    state_path = tmp_path / ".imap_watermarks.json"
    state = ImapSyncState(state_path)
    state.set_watermark("A/B", "/", 1, 10)
    # Key for ('A', '/B', 1) is 'A\x00/B\x001', different from 'A/B\x00/\x001'
    assert state.get_watermark("A", "/B", 1) is None


def test_corrupted_state_file_loads_empty(tmp_path: Path) -> None:
    state_path = tmp_path / ".imap_watermarks.json"
    state_path.write_text("not valid json {{{", encoding="utf-8")
    state = ImapSyncState(state_path)
    assert state.get_watermark("INBOX", "/", 1) is None

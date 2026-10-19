from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import pytest

from mailattic.attachment_store import (
    is_cas_entry_shape,
    prune_cas,
    verify_cas,
)
from mailattic.cli import _handle_db_prune_cas, _handle_db_verify_cas
from mailattic.storage import initialize_database


def _write_good_file(
    attachment_root: Path, payload: bytes, suffix: str = ".bin"
) -> Path:
    digest = hashlib.sha256(payload).hexdigest()
    path = attachment_root / digest[0:2] / digest[2:4] / f"{digest}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _write_tampered_file(
    attachment_root: Path,
    original_payload: bytes,
    tampered_payload: bytes,
    suffix: str = ".bin",
) -> Path:
    digest = hashlib.sha256(original_payload).hexdigest()
    path = attachment_root / digest[0:2] / digest[2:4] / f"{digest}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original_payload)
    path.write_bytes(tampered_payload)
    return path


def test_verify_cas_counts_correct(tmp_path: Path) -> None:
    """verify_cas reports correct total/ok/mismatched counts for a fixture store."""
    attachment_root = tmp_path / "attachments"
    _write_good_file(attachment_root, b"good payload")
    bad_file = _write_tampered_file(
        attachment_root,
        original_payload=b"original payload",
        tampered_payload=b"tampered payload",
    )

    result = verify_cas(attachment_root)

    assert result.total == 2
    assert result.ok == 1
    assert result.mismatched == 1
    assert result.read_error == 0
    assert result.bad_files == [bad_file]


def test_verify_cas_read_error_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_cas counts files that raise OSError as read_error."""
    attachment_root = tmp_path / "attachments"
    good_file = _write_good_file(attachment_root, b"good payload")
    unreadable_file = _write_good_file(attachment_root, b"will raise")
    original_read_bytes = Path.read_bytes

    def _read_bytes(self: Path) -> bytes:
        if self == unreadable_file:
            raise OSError("simulated read failure")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)

    result = verify_cas(attachment_root)

    assert result.total == 2
    assert result.ok == 1
    assert result.mismatched == 0
    assert result.read_error == 1
    assert result.bad_files == [unreadable_file]
    assert good_file.exists()
    assert unreadable_file.exists()


def test_verify_cas_is_readonly(tmp_path: Path) -> None:
    """verify_cas does not delete or modify any file."""
    attachment_root = tmp_path / "attachments"
    good_file = _write_good_file(attachment_root, b"good payload")
    bad_file = _write_tampered_file(
        attachment_root,
        original_payload=b"original payload",
        tampered_payload=b"tampered payload",
    )
    before_good = good_file.read_bytes()
    before_bad = bad_file.read_bytes()

    verify_cas(attachment_root)

    assert good_file.exists()
    assert bad_file.exists()
    assert good_file.read_bytes() == before_good
    assert bad_file.read_bytes() == before_bad


def test_prune_cas_deletes_only_bad_files(tmp_path: Path) -> None:
    """prune_cas deletes only the bad_files list, leaves good files intact."""
    attachment_root = tmp_path / "attachments"
    good_file = _write_good_file(attachment_root, b"good payload")
    bad_file = _write_tampered_file(
        attachment_root,
        original_payload=b"original payload",
        tampered_payload=b"tampered payload",
    )

    deleted, failed = prune_cas([bad_file])

    assert deleted == 1
    assert failed == 0
    assert not bad_file.exists()
    assert good_file.exists()


def test_prune_cas_empty_list() -> None:
    """prune_cas with empty list returns (0, 0)."""
    deleted, failed = prune_cas([])

    assert deleted == 0
    assert failed == 0


def test_verify_cas_nonexistent_root(tmp_path: Path) -> None:
    """verify_cas on non-existent root returns zero counts."""
    result = verify_cas(tmp_path / "nonexistent")

    assert result.total == 0
    assert result.ok == 0
    assert result.mismatched == 0
    assert result.read_error == 0
    assert result.bad_files == []


def test_prune_cas_cli_refuses_without_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI prune-cas exits non-zero without --yes and without interactive TTY."""
    attachment_root = tmp_path / "attachments"
    good_file = _write_good_file(attachment_root, b"good payload")
    bad_file = _write_tampered_file(
        attachment_root,
        original_payload=b"original payload",
        tampered_payload=b"tampered payload",
    )
    args = argparse.Namespace(
        database=tmp_path / "mail.db",
        attachment_root=attachment_root,
        yes=False,
    )

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    rc = _handle_db_prune_cas(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert "confirmation required" in captured.err
    assert bad_file.exists()
    assert good_file.exists()


def test_verify_cas_cli_reports_bad_summary_and_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI verify-cas prints the bad count and exits non-zero when corruption exists."""
    attachment_root = tmp_path / "attachments"
    _write_good_file(attachment_root, b"good payload")
    bad_file = _write_tampered_file(
        attachment_root,
        original_payload=b"original payload",
        tampered_payload=b"tampered payload",
    )
    args = argparse.Namespace(
        database=initialize_database(tmp_path / "mail.db"),
        attachment_root=attachment_root,
    )

    rc = _handle_db_verify_cas(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert "total:           2" in captured.out
    assert "ok:              1" in captured.out
    assert "bad:             1" in captured.out
    assert "mismatched:      1" in captured.out
    assert "read_error:      0" in captured.out
    assert str(bad_file) in captured.out


def test_prune_cas_cli_refuses_on_eof_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI prune-cas treats EOF during confirmation as a refusal."""
    attachment_root = tmp_path / "attachments"
    good_file = _write_good_file(attachment_root, b"good payload")
    bad_file = _write_tampered_file(
        attachment_root,
        original_payload=b"original payload",
        tampered_payload=b"tampered payload",
    )
    args = argparse.Namespace(
        database=tmp_path / "mail.db",
        attachment_root=attachment_root,
        yes=False,
    )

    def _raise_eof(_: str) -> str:
        raise EOFError()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _raise_eof)

    rc = _handle_db_prune_cas(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert "confirmation required" in captured.err
    assert bad_file.exists()
    assert good_file.exists()


def _seed_mixed_home_dir(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    home = tmp_path / "mailhome"
    home.mkdir(parents=True, exist_ok=True)
    db = initialize_database(home / "mail.db")
    backup_file = home / "backups" / "snap.db"
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    backup_file.write_bytes(b"backup payload")
    notes_file = home / "notes.txt"
    notes_file.write_text("operator notes", encoding="utf-8")
    normal_attachment = _write_good_file(home, b"normal attachment")
    return home, db, backup_file, notes_file, normal_attachment


def test_prune_cas_cli_refuses_when_root_contains_non_cas_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI prune-cas --yes refuses when root contains non-CAS files."""
    home, db, backup_file, notes_file, normal_attachment = _seed_mixed_home_dir(
        tmp_path
    )

    args = argparse.Namespace(
        database=db,
        attachment_root=home,
        yes=True,
    )

    rc = _handle_db_prune_cas(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert "refusing to prune" in captured.err
    assert "do not match CAS entry shape" in captured.err
    assert str(db) in captured.err
    assert str(backup_file) in captured.err
    assert str(notes_file) in captured.err

    # None of the files (CAS or non-CAS) were deleted
    assert db.exists()
    assert backup_file.exists()
    assert notes_file.exists()
    assert normal_attachment.exists()


def test_verify_cas_cli_reports_non_cas_files_separately(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI verify-cas reports non-CAS files separately and exits non-zero."""
    home, db, backup_file, notes_file, normal_attachment = _seed_mixed_home_dir(
        tmp_path
    )

    args = argparse.Namespace(
        database=db,
        attachment_root=home,
    )

    rc = _handle_db_verify_cas(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert "total:           4" in captured.out
    assert "ok:              1" in captured.out
    assert "bad:             0" in captured.out
    assert "mismatched:      0" in captured.out
    assert "read_error:      0" in captured.out
    assert "non_cas:         3" in captured.out
    assert "non-CAS files:" in captured.out
    assert str(db) in captured.out
    assert str(backup_file) in captured.out
    assert str(notes_file) in captured.out
    assert "bad files:" not in captured.out
    assert normal_attachment.exists()


def test_is_cas_entry_shape_rules(tmp_path: Path) -> None:
    """is_cas_entry_shape correctly validates CAS paths and rejects non-CAS paths."""
    root = tmp_path / "attachments"
    good_file = _write_good_file(root, b"good payload", suffix="")
    good_with_suffix = _write_good_file(root, b"another payload", suffix=".bin")

    assert is_cas_entry_shape(good_file, root)
    assert is_cas_entry_shape(good_with_suffix, root)
    assert is_cas_entry_shape(good_file)

    # Wrong depth
    assert not is_cas_entry_shape(root / "mail.db", root)
    assert not is_cas_entry_shape(root / "backups" / "snap.db", root)
    assert not is_cas_entry_shape(root / "nested" / "aa" / "bb" / good_file.name, root)

    # Wrong filename shape
    assert not is_cas_entry_shape(root / "aa" / "bb" / "Thumbs.db", root)
    assert not is_cas_entry_shape(root / "aa" / "bb" / "short.bin", root)

    # Bucket mismatch (file starts with 'cc' but placed in 'aa/bb')
    digest = hashlib.sha256(b"mismatch").hexdigest()
    mismatched_bucket = root / "aa" / "bb" / digest
    assert not is_cas_entry_shape(mismatched_bucket, root)


def test_prune_cas_refuses_non_cas_file(tmp_path: Path) -> None:
    """prune_cas unlinks only files conforming to CAS shape, skipping foreign files."""
    foreign_file = tmp_path / "mail.db"
    foreign_file.write_text("not a cas file", encoding="utf-8")

    deleted, failed = prune_cas([foreign_file])

    assert deleted == 0
    assert failed == 1
    assert foreign_file.exists()

"""Tests for mailattic.export_files."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from mailattic.export_files import EXPORT_TMP_PREFIX, land_exported_file


def test_land_exported_file_requires_exactly_one_writer(tmp_path: Path) -> None:
    dest = tmp_path / "out.bin"

    with pytest.raises(ValueError):
        land_exported_file(dest, overwrite=False)

    with pytest.raises(ValueError):
        land_exported_file(
            dest,
            overwrite=False,
            payload=b"x",
            save_to_tmp=lambda p: p.write_bytes(b"y"),
        )


def test_save_to_tmp_failure_cleans_up_temp_file(tmp_path: Path) -> None:
    dest = tmp_path / "out.msg"
    seen_tmp: list[Path] = []

    def failing_save(tmp_file: Path) -> None:
        seen_tmp.append(tmp_file)
        tmp_file.write_bytes(b"partial")
        raise RuntimeError("SaveAs failed")

    with pytest.raises(RuntimeError, match="SaveAs failed"):
        land_exported_file(dest, overwrite=False, save_to_tmp=failing_save)

    assert len(seen_tmp) == 1
    assert not seen_tmp[0].exists()
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_rename_race_leaves_destination_untouched_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "out.msg"
    foreign_bytes = b"foreign destination content"
    real_rename = Path.rename
    planted = {"done": False}

    def racing_rename(self: Path, target: Path) -> Path:
        if not planted["done"] and self.name.startswith(EXPORT_TMP_PREFIX):
            planted["done"] = True
            Path(target).write_bytes(foreign_bytes)
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", racing_rename)

    with pytest.raises(FileExistsError):
        land_exported_file(dest, overwrite=False, payload=b"our content")

    assert planted["done"], "race hook never fired"
    assert dest.read_bytes() == foreign_bytes
    assert list(tmp_path.iterdir()) == [dest]


def test_overwrite_uses_temp_file_and_preserves_dest_on_write_failure(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"original")

    def failing_save(tmp_file: Path) -> None:
        tmp_file.write_bytes(b"partial")
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        land_exported_file(dest, overwrite=True, save_to_tmp=failing_save)

    assert dest.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [dest]

    land_exported_file(dest, overwrite=True, payload=b"updated")
    assert dest.read_bytes() == b"updated"
    assert list(tmp_path.iterdir()) == [dest]


def test_payload_open_xb_collision_never_deletes_foreign_tmp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "out.bin"
    fixed_uuid = uuid.UUID(int=1)
    monkeypatch.setattr(uuid, "uuid4", lambda: fixed_uuid)
    planted_tmp = tmp_path / f"{EXPORT_TMP_PREFIX}{fixed_uuid.hex}.tmp"
    planted_tmp.write_bytes(b"foreign temp")

    with pytest.raises(FileExistsError):
        land_exported_file(dest, overwrite=False, payload=b"our content")

    assert planted_tmp.read_bytes() == b"foreign temp"
    assert not dest.exists()

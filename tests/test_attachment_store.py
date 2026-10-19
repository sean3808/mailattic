from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import pytest

from mailattic.attachment_store import (
    AttachmentStoreError,
    read_attachment,
    resolve_store_root,
    write_attachment,
)


def test_cas_retry_does_not_accept_partial_write(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "cas"
    payload = b"complete attachment payload"
    real_write = Path.write_bytes

    def partial_write(path: Path, data: bytes) -> int:
        real_write(path, data[:3])
        raise OSError("simulated disk-full after 3 bytes")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_bytes", partial_write)
        with pytest.raises(OSError, match="disk-full"):
            write_attachment(payload, "quote.pdf", "application/pdf", store)

    leftover = [path for path in store.rglob("*") if path.is_file()]
    assert leftover == [], f"partial write left debris behind: {leftover}"

    record = write_attachment(payload, "quote.pdf", "application/pdf", store)
    stored = read_attachment(record.storage_path, store)
    assert stored == payload, {
        "reported_size": record.size_bytes,
        "actual_size": len(stored),
        "actual": stored,
    }


def test_existing_truncated_file_is_repaired_on_rewrite(tmp_path: Path) -> None:
    store = tmp_path / "cas"
    payload = b"a full attachment payload that used to be truncated on disk"
    digest = hashlib.sha256(payload).hexdigest()
    bucket_dir = store / digest[0:2] / digest[2:4]
    bucket_dir.mkdir(parents=True)
    corrupt_path = bucket_dir / digest
    corrupt_path.write_bytes(payload[:5])  # simulates a pre-existing short file

    record = write_attachment(payload, "quote.pdf", "application/pdf", store)

    stored = read_attachment(record.storage_path, store)
    assert stored == payload
    assert corrupt_path.stat().st_size == len(payload)


def test_existing_same_size_corrupted_file_is_repaired_on_rewrite(
    tmp_path: Path,
) -> None:
    store = tmp_path / "cas"
    payload = b"a full attachment payload with a same-size corrupted twin"
    digest = hashlib.sha256(payload).hexdigest()
    bucket_dir = store / digest[0:2] / digest[2:4]
    bucket_dir.mkdir(parents=True)
    corrupt_path = bucket_dir / digest
    corrupted = bytearray(payload)
    corrupted[0] ^= 0xFF  # flip one byte, same length as payload
    corrupt_path.write_bytes(bytes(corrupted))
    assert len(corrupted) == len(payload)

    record = write_attachment(payload, "quote.pdf", "application/pdf", store)

    stored = read_attachment(record.storage_path, store)
    assert stored == payload
    assert record.size_bytes == len(payload)
    assert record.content_hash == digest


def test_identical_bytes_with_different_names_share_hash_only_path(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "attachments"
    payload = b"same bytes under different names"
    digest = hashlib.sha256(payload).hexdigest()

    named = write_attachment(payload, "quotation.pdf", "application/pdf", store_root)
    unnamed = write_attachment(
        payload, "attachment", "application/octet-stream", store_root
    )

    expected_path = store_root / digest[0:2] / digest[2:4] / digest
    assert named.storage_path == unnamed.storage_path
    assert named.storage_path == f"attachments/{digest[0:2]}/{digest[2:4]}/{digest}"
    assert [path for path in store_root.rglob("*") if path.is_file()] == [expected_path]
    assert expected_path.read_bytes() == payload


class AttachmentStoreTests(unittest.TestCase):
    def test_write_attachment_layout_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store_root = Path(temp_dir) / "attachments"
            payload = b"x" * 100
            digest = hashlib.sha256(payload).hexdigest()

            record1 = write_attachment(
                payload, "test.bin", "application/octet-stream", store_root
            )
            record2 = write_attachment(
                payload, "test.bin", "application/octet-stream", store_root
            )

            expected_path = store_root / digest[0:2] / digest[2:4] / digest
            self.assertTrue(expected_path.exists())
            self.assertEqual(expected_path.read_bytes(), payload)
            self.assertEqual(record1, record2)
            self.assertEqual(record1.content_hash, digest)
            self.assertEqual(record1.size_bytes, 100)
            self.assertEqual(
                record1.storage_path,
                f"attachments/{digest[0:2]}/{digest[2:4]}/{digest}",
            )

    def test_read_attachment_roundtrip_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store_root = Path(temp_dir) / "attachments"
            payload = b"hello attachment"
            record = write_attachment(payload, "doc.pdf", "application/pdf", store_root)

            result = read_attachment(record.storage_path, store_root)
            self.assertEqual(result, payload)

            with self.assertRaises(AttachmentStoreError):
                read_attachment("attachments/00/00/nonexistent.pdf", store_root)

    def test_store_root_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "sub" / "evidence.db"

            default_root = resolve_store_root(db_path)
            self.assertEqual(default_root, Path(temp_dir) / "sub" / "attachments")

            cli_root = Path(temp_dir) / "custom_attachments"
            cli_result = resolve_store_root(db_path, cli_override=cli_root)
            self.assertEqual(cli_result, cli_root.resolve())

            env_root = str(Path(temp_dir) / "env_attachments")
            env_result = resolve_store_root(db_path, env_override=env_root)
            self.assertEqual(env_result, Path(env_root).resolve())

"""Tests for the one-time suffix-bearing CAS migration.

Every fixture lives under pytest's managed ``tmp_path`` and uses an explicit
database plus attachment root.  The production database and attachment store
are never resolved or opened.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from mailattic.attachment_store import verify_cas
from mailattic.cli import main as bes_main
from mailattic.storage import initialize_database

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@dataclass(frozen=True, slots=True)
class LegacyFixture:
    database_path: Path
    attachment_root: Path
    digests: tuple[str, ...]
    legacy_paths: tuple[str, ...]


def _migration_module():
    return importlib.import_module("migrate_attachment_cas")


def _storage_path(digest: str, suffix: str = "") -> str:
    return f"attachments/{digest[:2]}/{digest[2:4]}/{digest}{suffix}"


def _physical_path(root: Path, storage_path: str) -> Path:
    return root / Path(storage_path).relative_to("attachments")


def _seed_legacy_fixture(tmp_path: Path) -> LegacyFixture:
    database_path = tmp_path / "fixture.db"
    attachment_root = tmp_path / "attachments"
    initialize_database(database_path)

    payloads = (b"shared legacy payload", b"second legacy payload")
    digests = tuple(hashlib.sha256(payload).hexdigest() for payload in payloads)
    shared_pdf = _storage_path(digests[0], ".pdf")
    shared_bin = _storage_path(digests[0], ".bin")
    second_docx = _storage_path(digests[1], ".docx")
    legacy_paths = (shared_pdf, shared_bin, second_docx)

    for storage_path, payload in (
        (shared_pdf, payloads[0]),
        (shared_bin, payloads[0]),
        (second_docx, payloads[1]),
    ):
        physical_path = _physical_path(attachment_root, storage_path)
        physical_path.parent.mkdir(parents=True, exist_ok=True)
        physical_path.write_bytes(payload)

    connection = sqlite3.connect(database_path)
    try:
        message_id = connection.execute(
            """
            INSERT INTO messages (
                pst_source, folder_path, logical_locator, dedup_key
            ) VALUES ('fixture.pst', 'Inbox', 'Inbox/1', 'fixture-dedup')
            """
        ).lastrowid
        assert message_id is not None
        connection.executemany(
            """
            INSERT INTO message_attachments (
                message_id, attachment_name, content_hash, size_bytes,
                mime_type, storage_path
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    message_id,
                    "shared.pdf",
                    digests[0],
                    len(payloads[0]),
                    "application/pdf",
                    shared_pdf,
                ),
                (
                    message_id,
                    "attachment",
                    digests[0],
                    len(payloads[0]),
                    "application/pdf",
                    shared_bin,
                ),
                (
                    message_id,
                    "second.docx",
                    digests[1],
                    len(payloads[1]),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    second_docx,
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    return LegacyFixture(
        database_path=database_path,
        attachment_root=attachment_root,
        digests=digests,
        legacy_paths=legacy_paths,
    )


def _seed_twenty_row_fixture(tmp_path: Path) -> LegacyFixture:
    database_path = tmp_path / "twenty.db"
    attachment_root = tmp_path / "attachments"
    initialize_database(database_path)

    connection = sqlite3.connect(database_path)
    digests: list[str] = []
    legacy_paths: list[str] = []
    try:
        message_id = connection.execute(
            """
            INSERT INTO messages (
                subject, sender_email, sent_at, pst_source, folder_path,
                logical_locator, dedup_key
            ) VALUES (
                'Twenty attachment migration fixture', 'fixture@example.com',
                '2026-08-11T12:00:00+08:00', 'fixture.pst', 'Inbox',
                'Inbox/twenty', 'fixture-twenty-dedup'
            )
            """
        ).lastrowid
        assert message_id is not None

        for index in range(10):
            payload = f"fixture payload {index}".encode()
            digest = hashlib.sha256(payload).hexdigest()
            digests.append(digest)
            pdf_path = _storage_path(digest, ".pdf")
            bin_path = _storage_path(digest, ".bin")
            legacy_paths.extend((pdf_path, bin_path))
            for storage_path in (pdf_path, bin_path):
                physical_path = _physical_path(attachment_root, storage_path)
                physical_path.parent.mkdir(parents=True, exist_ok=True)
                physical_path.write_bytes(payload)
            connection.executemany(
                """
                INSERT INTO message_attachments (
                    message_id, attachment_name, content_hash, size_bytes,
                    mime_type, storage_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        message_id,
                        f"quotation_{index}.pdf",
                        digest,
                        len(payload),
                        "application/pdf",
                        pdf_path,
                    ),
                    (
                        message_id,
                        "attachment",
                        digest,
                        len(payload),
                        "application/pdf",
                        bin_path,
                    ),
                ],
            )
        connection.commit()
    finally:
        connection.close()

    return LegacyFixture(
        database_path=database_path,
        attachment_root=attachment_root,
        digests=tuple(digests),
        legacy_paths=tuple(legacy_paths),
    )


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _stored_paths(database_path: Path) -> list[str]:
    connection = sqlite3.connect(database_path)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT storage_path FROM message_attachments ORDER BY id"
            )
        ]
    finally:
        connection.close()


def _assert_all_rows_resolve(database_path: Path, attachment_root: Path) -> None:
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT content_hash, storage_path FROM message_attachments "
            "WHERE storage_path IS NOT NULL"
        ).fetchall()
    finally:
        connection.close()
    for content_hash, storage_path in rows:
        physical_path = _physical_path(attachment_root, str(storage_path))
        assert physical_path.is_file()
        assert hashlib.sha256(physical_path.read_bytes()).hexdigest() == content_hash


def test_dry_run_lists_deletions_reconciles_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    tree_before = _tree_snapshot(fixture.attachment_root)
    rows_before = _stored_paths(fixture.database_path)

    exit_code = _migration_module().run_migration(
        fixture.database_path,
        fixture.attachment_root,
        confirm=False,
    )

    assert exit_code == 0
    assert _tree_snapshot(fixture.attachment_root) == tree_before
    assert _stored_paths(fixture.database_path) == rows_before
    output = capsys.readouterr().out
    for legacy_path in fixture.legacy_paths:
        assert f"delete: {legacy_path}" in output
    assert "physical_files_before: 3" in output
    assert "canonical_payloads_retained: 2" in output
    assert "redundant_payload_copies: 1" in output
    assert "file_reconciliation_difference: 0" in output
    assert "storage_path_rows: 3" in output
    assert "rows_to_rewrite: 3" in output
    assert "rows_unchanged: 0" in output
    assert "row_reconciliation_difference: 0" in output
    assert "multi_path_hash_groups_before: 1" in output


def test_preflight_rejects_missing_referenced_file_without_writing(
    tmp_path: Path,
) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    missing_path = _physical_path(fixture.attachment_root, fixture.legacy_paths[0])
    missing_path.unlink()
    tree_before = _tree_snapshot(fixture.attachment_root)
    rows_before = _stored_paths(fixture.database_path)

    with pytest.raises(
        _migration_module().MigrationError,
        match="referenced attachment is missing",
    ):
        _migration_module().build_migration_plan(
            fixture.database_path,
            fixture.attachment_root,
        )

    assert _tree_snapshot(fixture.attachment_root) == tree_before
    assert _stored_paths(fixture.database_path) == rows_before


def test_preflight_rejects_payload_digest_mismatch_without_writing(
    tmp_path: Path,
) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    corrupted_path = _physical_path(fixture.attachment_root, fixture.legacy_paths[0])
    corrupted_path.write_bytes(b"corrupted bytes")
    tree_before = _tree_snapshot(fixture.attachment_root)
    rows_before = _stored_paths(fixture.database_path)

    with pytest.raises(
        _migration_module().MigrationError,
        match="CAS payload digest mismatch",
    ):
        _migration_module().build_migration_plan(
            fixture.database_path,
            fixture.attachment_root,
        )

    assert _tree_snapshot(fixture.attachment_root) == tree_before
    assert _stored_paths(fixture.database_path) == rows_before


def test_preflight_rejects_escaping_row_path_without_writing(tmp_path: Path) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    connection = sqlite3.connect(fixture.database_path)
    try:
        connection.execute(
            "UPDATE message_attachments SET storage_path = ? WHERE id = 1",
            ["attachments/../outside"],
        )
        connection.commit()
    finally:
        connection.close()
    tree_before = _tree_snapshot(fixture.attachment_root)
    rows_before = _stored_paths(fixture.database_path)

    with pytest.raises(
        _migration_module().MigrationError,
        match="attachment storage path escapes root",
    ):
        _migration_module().build_migration_plan(
            fixture.database_path,
            fixture.attachment_root,
        )

    assert _tree_snapshot(fixture.attachment_root) == tree_before
    assert _stored_paths(fixture.database_path) == rows_before


def test_cli_requires_explicit_database_and_attachment_root() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _migration_module().main([])

    assert exc_info.value.code == 2


def test_confirm_consolidates_rows_and_files_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    module = _migration_module()

    first_exit_code = module.run_migration(
        fixture.database_path,
        fixture.attachment_root,
        confirm=True,
    )

    assert first_exit_code == 0
    expected_paths = [_storage_path(digest) for digest in fixture.digests]
    assert _stored_paths(fixture.database_path) == [
        expected_paths[0],
        expected_paths[0],
        expected_paths[1],
    ]
    assert sorted(_tree_snapshot(fixture.attachment_root)) == sorted(
        str(Path(path).relative_to("attachments")).replace("\\", "/")
        for path in expected_paths
    )
    _assert_all_rows_resolve(fixture.database_path, fixture.attachment_root)
    first_output = capsys.readouterr().out
    assert "rows_rewritten: 3" in first_output
    assert "legacy_files_deleted: 3" in first_output
    assert "multi_path_hash_groups_after: 0" in first_output

    second_exit_code = module.run_migration(
        fixture.database_path,
        fixture.attachment_root,
        confirm=True,
    )

    assert second_exit_code == 0
    _assert_all_rows_resolve(fixture.database_path, fixture.attachment_root)
    second_output = capsys.readouterr().out
    assert "rows_to_rewrite: 0" in second_output
    assert "rows_rewritten: 0" in second_output
    assert "legacy_files_deleted: 0" in second_output
    assert "multi_path_hash_groups_after: 0" in second_output


def test_confirm_rejects_plan_changes_before_locked_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    module = _migration_module()
    tree_before = _tree_snapshot(fixture.attachment_root)
    original_build = module.build_migration_plan

    def build_then_add_row(database_path: Path, attachment_root: Path):
        plan = original_build(database_path, attachment_root)
        connection = sqlite3.connect(database_path)
        try:
            source = connection.execute(
                """
                SELECT message_id, attachment_name, content_hash, size_bytes,
                       mime_type, storage_path
                FROM message_attachments
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            assert source is not None
            connection.execute(
                """
                INSERT INTO message_attachments (
                    message_id, attachment_name, content_hash, size_bytes,
                    mime_type, storage_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                source,
            )
            connection.commit()
        finally:
            connection.close()
        return plan

    monkeypatch.setattr(module, "build_migration_plan", build_then_add_row)

    with pytest.raises(
        module.MigrationError,
        match="migration plan changed before locked execution",
    ):
        module.run_migration(
            fixture.database_path,
            fixture.attachment_root,
            confirm=True,
        )

    assert _tree_snapshot(fixture.attachment_root) == tree_before
    assert _stored_paths(fixture.database_path) == [
        *fixture.legacy_paths,
        fixture.legacy_paths[0],
    ]
    _assert_all_rows_resolve(fixture.database_path, fixture.attachment_root)


def test_staging_failure_rolls_back_rows_and_keeps_legacy_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    module = _migration_module()
    rows_before = _stored_paths(fixture.database_path)
    original_stage = getattr(module, "_stage_canonical_file", None)
    calls = 0

    def fail_on_second_stage(source: Path, target: Path, digest: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise module.MigrationError("simulated staging failure")
        assert original_stage is not None
        original_stage(source, target, digest)

    monkeypatch.setattr(
        module,
        "_stage_canonical_file",
        fail_on_second_stage,
        raising=False,
    )

    with pytest.raises(module.MigrationError, match="simulated staging failure"):
        module.run_migration(
            fixture.database_path,
            fixture.attachment_root,
            confirm=True,
        )

    assert _stored_paths(fixture.database_path) == rows_before
    for legacy_path in fixture.legacy_paths:
        assert _physical_path(fixture.attachment_root, legacy_path).is_file()
    _assert_all_rows_resolve(fixture.database_path, fixture.attachment_root)


def test_copy_fallback_stages_outside_cas_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    module = _migration_module()
    digest = fixture.digests[0]
    source = _physical_path(fixture.attachment_root, fixture.legacy_paths[0])
    target = fixture.attachment_root / digest[:2] / digest[2:4] / digest
    copied_to: list[Path] = []
    synced_files: list[Path] = []
    synced_directories: list[Path] = []
    original_copyfile = module.shutil.copyfile
    original_sync_file = module._sync_file
    original_sync_directory = module._sync_directory

    def reject_hard_link(_source: Path, _target: Path) -> None:
        raise OSError("hard links unavailable")

    def record_copy(source_path: Path, temporary_path: Path) -> Path:
        copied_to.append(Path(temporary_path))
        return original_copyfile(source_path, temporary_path)

    def record_file_sync(path: Path) -> None:
        synced_files.append(Path(path))
        original_sync_file(path)

    def record_directory_sync(path: Path) -> None:
        synced_directories.append(Path(path))
        original_sync_directory(path)

    monkeypatch.setattr(module.os, "link", reject_hard_link)
    monkeypatch.setattr(module.shutil, "copyfile", record_copy)
    monkeypatch.setattr(module, "_sync_file", record_file_sync)
    monkeypatch.setattr(module, "_sync_directory", record_directory_sync)

    module._stage_canonical_file(source, target, digest)

    assert target.read_bytes() == source.read_bytes()
    assert len(copied_to) == 1
    assert not copied_to[0].is_relative_to(fixture.attachment_root)
    assert ".project-runtime" in copied_to[0].parts
    assert len(copied_to[0].name) <= 40
    assert synced_files == [copied_to[0], target]
    assert synced_directories[-1] == target.parent


def test_staging_syncs_canonical_file_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    module = _migration_module()
    digest = fixture.digests[0]
    source = _physical_path(fixture.attachment_root, fixture.legacy_paths[0])
    target = fixture.attachment_root / digest[:2] / digest[2:4] / digest
    sync_calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(
        module,
        "_sync_file",
        lambda path: sync_calls.append(("file", Path(path))),
    )
    monkeypatch.setattr(
        module,
        "_sync_directory",
        lambda path: sync_calls.append(("directory", Path(path))),
    )

    module._stage_canonical_file(source, target, digest)

    assert sync_calls == [("file", target), ("directory", target.parent)]


def test_cleanup_failure_leaves_canonical_rows_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    module = _migration_module()
    original_remove = getattr(module, "_remove_legacy_file", None)
    failed_paths: list[Path] = []
    failed_once = False

    def fail_first_remove(path: Path) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            failed_paths.append(path)
            raise OSError("simulated cleanup failure")
        assert original_remove is not None
        original_remove(path)

    monkeypatch.setattr(
        module,
        "_remove_legacy_file",
        fail_first_remove,
        raising=False,
    )

    first_exit_code = module.run_migration(
        fixture.database_path,
        fixture.attachment_root,
        confirm=True,
    )

    assert first_exit_code == 1
    assert len(failed_paths) == 1
    failed_path = failed_paths[0]
    assert failed_path.is_file()
    assert all(not Path(path).suffix for path in _stored_paths(fixture.database_path))
    _assert_all_rows_resolve(fixture.database_path, fixture.attachment_root)
    assert "simulated cleanup failure" in capsys.readouterr().err

    monkeypatch.setattr(module, "_remove_legacy_file", original_remove)
    second_exit_code = module.run_migration(
        fixture.database_path,
        fixture.attachment_root,
        confirm=True,
    )

    assert second_exit_code == 0
    assert not failed_path.exists()
    _assert_all_rows_resolve(fixture.database_path, fixture.attachment_root)
    assert "multi_path_hash_groups_after: 0" in capsys.readouterr().out


def test_confirm_fails_when_post_migration_scan_finds_legacy_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _seed_legacy_fixture(tmp_path)
    module = _migration_module()

    monkeypatch.setattr(module, "_remove_legacy_file", lambda _path: None)

    exit_code = module.run_migration(
        fixture.database_path,
        fixture.attachment_root,
        confirm=True,
    )

    assert exit_code == 1
    _assert_all_rows_resolve(fixture.database_path, fixture.attachment_root)
    output = capsys.readouterr()
    assert "legacy_files_after: 3" in output.out
    assert "multi_path_hash_groups_after: 2" in output.out
    assert "multi-path hash groups remain after migration: 2" in output.err


def test_twenty_row_fixture_migrates_verifies_and_exports_with_metadata_extensions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _seed_twenty_row_fixture(tmp_path)
    module = _migration_module()

    exit_code = module.run_migration(
        fixture.database_path,
        fixture.attachment_root,
        confirm=True,
    )

    assert exit_code == 0
    migration_output = capsys.readouterr().out
    assert "physical_files_before: 20" in migration_output
    assert "canonical_payloads_retained: 10" in migration_output
    assert "redundant_payload_copies: 10" in migration_output
    assert "file_reconciliation_difference: 0" in migration_output
    assert "storage_path_rows: 20" in migration_output
    assert "rows_rewritten: 20" in migration_output
    assert "row_reconciliation_difference: 0" in migration_output
    assert "multi_path_hash_groups_after: 0" in migration_output
    _assert_all_rows_resolve(fixture.database_path, fixture.attachment_root)

    verify_result = verify_cas(fixture.attachment_root)
    assert verify_result.total == 10
    assert verify_result.ok == 10
    assert verify_result.mismatched == 0
    assert verify_result.read_error == 0

    export_dir = tmp_path / "exports"
    export_exit_code = bes_main(
        [
            "attachments",
            "--database",
            str(fixture.database_path),
            "--attachment-root",
            str(fixture.attachment_root),
            "--copy-to",
            str(export_dir),
        ]
    )

    assert export_exit_code == 0
    assert "copied: 20" in capsys.readouterr().out
    exported_files = sorted(export_dir.iterdir())
    assert len(exported_files) == 20
    assert all(path.suffix == ".pdf" for path in exported_files)
    assert (
        sum(
            re.search(r"_[0-9a-f]{64}\.pdf$", path.name) is not None
            for path in exported_files
        )
        == 10
    )

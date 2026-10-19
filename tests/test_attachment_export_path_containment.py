"""Issue #306: Attachment export path containment verification and regression pin.

Verifies that attachment names stored verbatim from mail headers containing
directory traversal components (../, ..\\), absolute paths (C:\\Windows, /etc),
newlines (\\r\\n), or names that sanitises to empty (..) NEVER escape the designated
export directory during export, either via the CLI (bes attachments --copy-to)
or via MCP export (mcp_writer.MailWriter.export_attachments).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

import pytest

from mailattic.attachment_store import (
    build_attachment_export_filename,
    export_attachments,
)
from mailattic.cli import main as cli_main
from mailattic.mcp_writer import MailWriter
from mailattic.models import PstSource, build_normalized_message
from mailattic.parser import _parse_email_message
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)


@dataclass(frozen=True, slots=True)
class ContainmentCase:
    label: str
    header_filename: str
    expected_db_name: str
    payload: bytes


CONTAINMENT_CASES: tuple[ContainmentCase, ...] = (
    ContainmentCase(
        label="parent_dir_slash",
        header_filename="../escape.txt",
        expected_db_name="../escape.txt",
        payload=b"payload content for parent_dir_slash",
    ),
    ContainmentCase(
        label="parent_dir_backslash",
        header_filename=r"..\escape.txt",
        expected_db_name=r"..\escape.txt",
        payload=b"payload content for parent_dir_backslash",
    ),
    ContainmentCase(
        label="sub_parent_dir",
        header_filename="sub/../../escape.txt",
        expected_db_name="sub/../../escape.txt",
        payload=b"payload content for sub_parent_dir",
    ),
    ContainmentCase(
        label="absolute_windows",
        header_filename=r"C:\Windows\escape.txt",
        expected_db_name=r"C:\Windows\escape.txt",
        payload=b"payload content for absolute_windows",
    ),
    ContainmentCase(
        label="absolute_posix",
        header_filename="/etc/escape.txt",
        expected_db_name="/etc/escape.txt",
        payload=b"payload content for absolute_posix",
    ),
    ContainmentCase(
        label="crlf_in_name",
        # RFC 2047 encoded-word decodes to 'evil\r\nname.txt':
        header_filename="=?utf-8?B?ZXZpbA0KbmFtZS50eHQ=?=",
        expected_db_name="evil\r\nname.txt",
        payload=b"payload content for crlf_in_name",
    ),
    ContainmentCase(
        label="sanitises_to_empty",
        header_filename="..",
        expected_db_name="..",
        payload=b"payload content for sanitises_to_empty",
    ),
)


def _seed_synthetic_messages(
    db_path: Path,
    store_root: Path,
    cases: Sequence[ContainmentCase],
) -> list[dict[str, object]]:
    """Synthesise and ingest in-memory emails into a temp database and CAS."""
    conn = connect_database(db_path)
    try:
        for index, case in enumerate(cases, 1):
            msg = EmailMessage()
            msg["From"] = f"sender-{index}@example.test"
            msg["To"] = f"recipient-{index}@example.test"
            msg["Subject"] = f"Subject {case.label}"
            msg["Date"] = f"Thu, 24 Jul 2026 09:{index:02d}:00 +08:00"
            msg["Message-ID"] = f"<msg-{index}@example.test>"
            msg.set_content(f"Body text for {case.label}")
            msg.add_attachment(
                case.payload,
                maintype="text",
                subtype="plain",
                filename=case.header_filename,
            )
            parsed = _parse_email_message(msg, attachment_root=store_root)
            source = PstSource(
                path=Path(f"source_{index}.eml"),
                input_mode="file",
                requested_from=Path(f"source_{index}.eml"),
            )
            normalized = build_normalized_message(
                parsed,
                source=source,
                folder_path="Inbox",
                logical_locator=f"Inbox/{index}",
            )
            persist_message(conn, normalized)
        conn.commit()

        cursor = conn.execute(
            """
            SELECT
                ma.id AS attachment_id,
                ma.attachment_name,
                ma.content_hash,
                ma.size_bytes,
                ma.mime_type,
                ma.storage_path,
                m.sent_at
            FROM message_attachments ma
            JOIN messages m ON m.id = ma.message_id
            ORDER BY ma.id ASC
            """
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _all_files_under(root: Path) -> set[Path]:
    return {p.resolve() for p in root.rglob("*") if p.is_file()}


def _assert_export_contained(
    export_dir: Path,
    files_before: set[Path],
    cases: Sequence[ContainmentCase],
    rows: Sequence[Mapping[str, object]],
    all_files_root: Path,
) -> None:
    """Assert all files land inside export_dir and no files escape under
    all_files_root.
    """
    resolved_export_dir = export_dir.resolve()
    assert resolved_export_dir.is_dir()

    files_after = _all_files_under(all_files_root)
    new_files = files_after - files_before

    assert len(new_files) == len(cases)
    for new_file in new_files:
        assert new_file.is_relative_to(resolved_export_dir), (
            f"File escaped export directory: {new_file}"
        )
        assert new_file.parent == resolved_export_dir

    for row, case in zip(rows, cases, strict=True):
        expected_filename = build_attachment_export_filename(row, export_dir)
        landing_path = export_dir / expected_filename
        assert landing_path.exists(), f"Expected {expected_filename} to exist"
        assert landing_path.resolve().is_relative_to(resolved_export_dir)
        assert landing_path.read_bytes() == case.payload


def test_attachment_names_stored_verbatim_in_database(tmp_path: Path) -> None:
    """Issue #306: Stored attachment names in DB stay verbatim (issue '不做')."""
    db_path = initialize_database(tmp_path / "mail.db")
    store_root = tmp_path / "attachments"

    rows = _seed_synthetic_messages(db_path, store_root, CONTAINMENT_CASES)
    assert len(rows) == len(CONTAINMENT_CASES)

    for row, case in zip(rows, CONTAINMENT_CASES, strict=True):
        assert row["attachment_name"] == case.expected_db_name
        assert row["storage_path"] is not None
        assert row["content_hash"] is not None


def test_cli_export_attachments_path_containment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #306: CLI export path containment verification.

    Asserts that `bes attachments --copy-to <export_dir>` writes every file
    strictly inside the designated export directory, and no files escape
    anywhere else under tmp_path.
    """
    db_path = initialize_database(tmp_path / "mail.db")
    store_root = tmp_path / "attachments"
    cli_export_dir = tmp_path / "cli_export"

    rows = _seed_synthetic_messages(db_path, store_root, CONTAINMENT_CASES)
    files_before = _all_files_under(tmp_path)

    exit_code = cli_main(
        [
            "attachments",
            "--database",
            str(db_path),
            "--attachment-root",
            str(store_root),
            "--copy-to",
            str(cli_export_dir),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"copied: {len(CONTAINMENT_CASES)}" in captured.out

    _assert_export_contained(
        export_dir=cli_export_dir,
        files_before=files_before,
        cases=CONTAINMENT_CASES,
        rows=rows,
        all_files_root=tmp_path,
    )


def test_mcp_export_attachments_path_containment(tmp_path: Path) -> None:
    """Issue #306: MCP export path containment verification.

    Asserts that `MailWriter.export_attachments` writes every file
    strictly inside the designated export directory, and no files escape
    anywhere else under tmp_path.
    """
    db_path = initialize_database(tmp_path / "mail.db")
    store_root = tmp_path / "attachments"
    mcp_export_dir = tmp_path / "mcp_export"
    draft_dir = tmp_path / "mcp_drafts"

    rows = _seed_synthetic_messages(db_path, store_root, CONTAINMENT_CASES)
    files_before = _all_files_under(tmp_path)

    writer = MailWriter(
        attachment_root=store_root,
        database_path=db_path,
        draft_dir=draft_dir,
    )
    items = writer.export_attachments(rows, mcp_export_dir)

    assert len(items) == len(CONTAINMENT_CASES)
    resolved_export_dir = mcp_export_dir.resolve()
    assert resolved_export_dir.is_dir()

    for item in items:
        assert item.status == "exported", f"Export item failed: {item}"
        assert item.destination_path is not None
        assert item.destination_path.resolve().is_relative_to(resolved_export_dir), (
            f"Item path escaped export directory: {item.destination_path}"
        )
        assert item.destination_path.parent == resolved_export_dir

    _assert_export_contained(
        export_dir=mcp_export_dir,
        files_before=files_before,
        cases=CONTAINMENT_CASES,
        rows=rows,
        all_files_root=tmp_path,
    )


def test_shared_export_function_containment_pin(tmp_path: Path) -> None:
    """Direct pin on the shared `export_attachments` entry point."""
    db_path = initialize_database(tmp_path / "mail.db")
    store_root = tmp_path / "attachments"
    direct_export_dir = tmp_path / "direct_export"

    rows = _seed_synthetic_messages(db_path, store_root, CONTAINMENT_CASES)
    files_before = _all_files_under(tmp_path)

    result = export_attachments(
        attachment_rows=rows,
        store_root=store_root,
        destination_dir=direct_export_dir,
    )

    assert result.copied == len(CONTAINMENT_CASES)
    assert result.failed == 0

    _assert_export_contained(
        export_dir=direct_export_dir,
        files_before=files_before,
        cases=CONTAINMENT_CASES,
        rows=rows,
        all_files_root=tmp_path,
    )

from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from mailattic.cli import main
from mailattic.discovery import discover_pst_sources
from mailattic.ingest import import_pst_sources
from mailattic.models import (
    ExtractedMessage,
    ExtractionResult,
    IngestFailure,
    NormalizedAttachment,
    PstSource,
    build_normalized_message,
)
from mailattic.outlook_pst import PstExtractionAccount
from mailattic.parser import ReadpstParser, _parse_eml_file
from mailattic.query import SearchFilters, get_message, search_messages
from mailattic.storage import (
    connect_database,
    initialize_database,
)
from mailattic.storage import (
    persist_message as storage_persist_message,
)


def _write_stub_readpst(root: Path) -> Path:
    """Create an inert readpst stand-in under *root* and return its path.

    `resolve_readpst_path` accepts the first candidate that exists on disk and
    otherwise falls through to the repository default
    `.tools/libpst/tool/bin/readpst.exe`, which is a gitignored vendored binary
    present only in a developer checkout. A test that only inspects how the
    readpst command line is assembled must not depend on whether that binary is
    on the host, so it supplies a file that does exist. The stub is never
    executed: only `_build_readpst_command` is called.

    Tests using this helper also assert that the built command starts with this
    exact path. Without that assertion the tests would still pass on a host that
    happens to have the vendored binary, which is the silent host dependency
    they exist to rule out.
    """
    stub_path = root / "stub-readpst.exe"
    stub_path.write_text("", encoding="utf-8")
    return stub_path


class _AccountingParser:
    """Adapt existing parser fixtures to the Outlook parser's accounting seam.

    Mirrors `OutlookPstParser.extract`: returns the account together with
    its result stream rather than publishing it through a mutable
    out-parameter. `recipients_seen` defaults to the emitted count (matching
    every existing fixture, which has no unresolved recipients), but a
    fixture may set `raw_message["_raw_recipient_count"]` to a different
    value to exercise a genuine accounting mismatch -- without that seam
    `validate()` could never trip in these tests.
    """

    def __init__(self, results_factory) -> None:
        self._results_factory = results_factory

    def extract(self, source: PstSource):
        account = PstExtractionAccount(source_path=source.source_path)
        return account, self._generate(source, account)

    def _generate(self, source: PstSource, account: PstExtractionAccount):
        for result in self._results_factory(source):
            account.messages_seen += 1
            if result.failure is not None:
                account.parse_failures += 1
            else:
                account.messages_emitted += 1
                raw_message = result.message.raw_message
                emitted_count = sum(
                    len(raw_message.get(role, [])) for role in ("to", "cc", "bcc")
                )
                account.recipients_emitted += emitted_count
                account.recipients_seen += raw_message.get(
                    "_raw_recipient_count", emitted_count
                )
            yield result
        account.finish(elapsed_seconds=0.0, completed=True)
        account.validate()


def _legacy_fixture_parser(
    *, readpst_path: Path, work_root: Path, attachment_root: Path | None = None
) -> _AccountingParser:
    work_root.mkdir(parents=True, exist_ok=True)
    delegate = ReadpstParser(
        readpst_path=readpst_path,
        work_root=work_root,
        attachment_root=attachment_root,
    )
    return _AccountingParser(delegate.iter_extraction_results)


def _accounting_parser_source() -> PstSource:
    return PstSource(
        path=Path("C:/mail/archive.pst"),
        input_mode="file",
        requested_from=Path("C:/mail/archive.pst"),
    )


def test_accounting_parser_validates_when_recipient_count_matches() -> None:
    """The fixture accounting seam must carry real counters -- a fake that
    always sets `recipients_seen` equal to `recipients_emitted` can never
    catch a real accounting regression (issue #155 review)."""

    def _balanced(_source):
        yield ExtractionResult(
            message=ExtractedMessage(
                raw_message={
                    "subject": "Balanced",
                    "to": [{"email": "bob@example.com"}],
                },
                folder_path="Inbox",
                logical_locator="Inbox/1",
            )
        )

    account, results = _AccountingParser(_balanced).extract(_accounting_parser_source())
    list(results)

    assert account.recipients_seen == 1
    assert account.recipients_emitted == 1


def test_accounting_parser_trips_validate_when_recipient_count_is_wrong() -> None:
    """Same fixture, deliberately wrong production-shaped accounting: the
    seam must let `validate()` trip instead of tautologically balancing."""

    def _undercounted(_source):
        yield ExtractionResult(
            message=ExtractedMessage(
                raw_message={
                    "subject": "Undercounted",
                    "to": [{"email": "bob@example.com"}],
                    "_raw_recipient_count": 3,
                },
                folder_path="Inbox",
                logical_locator="Inbox/1",
            )
        )

    account, results = _AccountingParser(_undercounted).extract(
        _accounting_parser_source()
    )

    raised = False
    try:
        list(results)
    except RuntimeError as exc:
        raised = True
        assert "recipient reconciliation difference" in str(exc)
    assert raised, "expected validate() to trip on the recipient mismatch"
    assert account.recipients_seen == 3
    assert account.recipients_emitted == 1


class BootstrapTests(unittest.TestCase):
    def test_init_db_creates_expected_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "evidence.db"

            exit_code = main(["init-db", "--database", str(database_path)])

            self.assertEqual(exit_code, 0)
            self.assertTrue(database_path.exists())

            connection = sqlite3.connect(database_path)
            try:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                    )
                }
            finally:
                connection.close()

            expected_tables = {
                "messages",
                "message_recipients",
                "message_attachments",
                "ingest_failures",
            }
            self.assertTrue(expected_tables <= table_names)
            self.assertIn("message_fts", table_names)

    def test_schema_migration_adds_attachment_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "evidence.db"
            conn = sqlite3.connect(database_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY,
                        subject TEXT NOT NULL DEFAULT '',
                        sender_name TEXT,
                        sender_email TEXT,
                        sent_at TEXT,
                        received_at TEXT,
                        body_text TEXT NOT NULL DEFAULT '',
                        message_id TEXT,
                        in_reply_to TEXT,
                        references_header TEXT,
                        pst_source TEXT NOT NULL,
                        folder_path TEXT NOT NULL,
                        logical_locator TEXT NOT NULL,
                        source_input_mode TEXT NOT NULL DEFAULT '',
                        source_requested_from TEXT NOT NULL DEFAULT '',
                        dedup_key TEXT NOT NULL,
                        dedup_basis TEXT NOT NULL DEFAULT '',
                        recipient_search_text TEXT NOT NULL DEFAULT '',
                        attachment_search_text TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE message_attachments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id INTEGER NOT NULL,
                        attachment_name TEXT NOT NULL
                    );
                    CREATE TABLE message_recipients (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id INTEGER NOT NULL,
                        recipient_type TEXT NOT NULL,
                        display_name TEXT,
                        email TEXT
                    );
                    CREATE TABLE ingest_failures (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pst_source TEXT NOT NULL,
                        folder_path TEXT,
                        logical_locator TEXT,
                        source_input_mode TEXT NOT NULL DEFAULT '',
                        source_requested_from TEXT NOT NULL DEFAULT '',
                        error_stage TEXT NOT NULL,
                        error_type TEXT NOT NULL,
                        error_message TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE VIRTUAL TABLE message_fts USING fts5(
                        message_rowid UNINDEXED,
                        subject,
                        sender,
                        recipients,
                        body_text,
                        attachment_names
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            initialize_database(database_path)

            conn = sqlite3.connect(database_path)
            try:
                col_names = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(message_attachments)")
                }
                index_names = {
                    row[1]
                    for row in conn.execute(
                        "SELECT * FROM sqlite_master WHERE type='index'"
                    )
                }
            finally:
                conn.close()

            self.assertIn("content_hash", col_names)
            self.assertIn("size_bytes", col_names)
            self.assertIn("mime_type", col_names)
            self.assertIn("storage_path", col_names)
            self.assertIn("idx_attachments_hash", index_names)

            initialize_database(database_path)

    def test_normalized_attachment_shape(self) -> None:
        att1 = NormalizedAttachment(attachment_name="file.pdf")
        self.assertIsNone(att1.content_hash)
        self.assertIsNone(att1.size_bytes)
        self.assertIsNone(att1.mime_type)
        self.assertIsNone(att1.storage_path)

        att2 = NormalizedAttachment(
            attachment_name="report.xlsx",
            content_hash="abc123",
            size_bytes=1024,
            mime_type="application/vnd.ms-excel",
            storage_path="attachments/ab/c1/abc123.xlsx",
        )
        self.assertEqual(att2.content_hash, "abc123")
        self.assertEqual(att2.size_bytes, 1024)
        self.assertEqual(att2.mime_type, "application/vnd.ms-excel")
        self.assertEqual(att2.storage_path, "attachments/ab/c1/abc123.xlsx")

    def test_discovery_supports_directory_and_explicit_file_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "Archive_2024" / "Odd Folder Name"
            nested.mkdir(parents=True)
            first_pst = nested / "mailbox.PST"
            second_pst = root / "manual-review.pst"
            first_pst.write_text("placeholder", encoding="utf-8")
            second_pst.write_text("placeholder", encoding="utf-8")
            (root / "notes.txt").write_text("ignore me", encoding="utf-8")

            sources = discover_pst_sources(
                directory_inputs=[root],
                file_inputs=[second_pst],
            )

            self.assertEqual(
                [source.path for source in sources],
                [first_pst.resolve(), second_pst.resolve()],
            )
            self.assertEqual(sources[0].input_mode, "directory")
            self.assertEqual(sources[0].requested_from, root.resolve())
            self.assertEqual(sources[1].input_mode, "file")
            self.assertEqual(sources[1].requested_from, second_pst.resolve())

    def test_build_normalized_message_preserves_required_metadata(self) -> None:
        source = PstSource(
            path=Path("C:/mail/archive.pst"),
            input_mode="file",
            requested_from=Path("C:/mail/archive.pst"),
        )

        message = build_normalized_message(
            {
                "subject": " Contract Update ",
                "sender": {"name": "Alice", "email": "alice@example.com"},
                "to": [{"name": "Bob", "email": "bob@example.com"}],
                "cc": ["carol@example.com"],
                "bcc": [],
                "sent_at": "2024-01-02T03:04:05",
                "received_at": datetime(2024, 1, 2, 3, 4, 10),
                "body_text": "Line one\r\nLine two",
                "attachment_names": ["draft.pdf"],
                "message_id": "<msg@example.com>",
                "in_reply_to": "<thread@example.com>",
                "references": "<thread@example.com>  <root@example.com>",
            },
            source=source,
            folder_path="Inbox/Contracts",
            logical_locator="Inbox/Contracts/42",
        )

        self.assertEqual(message.subject, "Contract Update")
        self.assertEqual(message.sender.email, "alice@example.com")
        self.assertEqual(
            message.recipient_search_text,
            "Bob bob@example.com carol@example.com",
        )
        self.assertEqual(message.body_text, "Line one\nLine two")
        self.assertEqual(message.references, "<thread@example.com>  <root@example.com>")
        self.assertEqual(message.pst_source, str(source.path))
        self.assertEqual(message.folder_path, "Inbox/Contracts")
        self.assertEqual(message.logical_locator, "Inbox/Contracts/42")
        self.assertEqual(message.source_input_mode, "file")
        self.assertEqual(message.source_requested_from, str(source.requested_from))
        self.assertEqual(message.dedup_key, "message-id:<msg@example.com>")

    def test_blank_message_id_uses_fallback_dedup_key(self) -> None:
        message = build_normalized_message(
            {
                "subject": "No stable id",
                "sender": {"email": "owner@example.com"},
                "message_id": "   ",
                "sent_at": "2024-01-02T03:04:05",
                "body_text": "Evidence body",
            },
            source=PstSource(
                path=Path("C:/mail/archive.pst"),
                input_mode="file",
                requested_from=Path("C:/mail/archive.pst"),
            ),
            folder_path="Inbox",
            logical_locator="Inbox/91",
        )

        self.assertTrue(message.dedup_key.startswith("fallback:"))

    def test_build_normalized_message_uses_fallback_dedup_key_without_message_id(
        self,
    ) -> None:
        first_source = PstSource(
            path=Path("C:/mail/archive-a.pst"),
            input_mode="file",
            requested_from=Path("C:/mail/archive-a.pst"),
        )
        second_source = PstSource(
            path=Path("C:/mail/archive-b.pst"),
            input_mode="file",
            requested_from=Path("C:/mail/archive-b.pst"),
        )

        raw_message = {
            "subject": "No message id",
            "sender": {"name": "Owner", "email": "owner@example.com"},
            "sent_at": "2024-01-02T03:04:05",
            "body_text": "Evidence body",
        }
        first_message = build_normalized_message(
            raw_message,
            source=first_source,
            folder_path="Inbox",
            logical_locator="Inbox/77",
        )
        second_message = build_normalized_message(
            raw_message,
            source=second_source,
            folder_path="Recovered",
            logical_locator="Recovered/12",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            connection = connect_database(database_path)
            try:
                first_result = storage_persist_message(connection, first_message)
                second_result = storage_persist_message(connection, second_message)
                stored_count = connection.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertTrue(first_result.inserted)
        self.assertFalse(second_result.inserted)
        self.assertEqual(stored_count, 1)
        self.assertEqual(first_message.dedup_key, second_message.dedup_key)
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            connection = connect_database(database_path)
            try:
                storage_persist_message(connection, first_message)
                dedup_basis = connection.execute(
                    "SELECT dedup_basis FROM messages"
                ).fetchone()[0]
            finally:
                connection.close()

        dedup_payload = json.loads(dedup_basis)
        self.assertEqual(dedup_payload["method"], "fallback")
        self.assertEqual(dedup_payload["key_fields"]["subject"], "No message id")
        self.assertEqual(dedup_payload["debug_context"]["logical_locator"], "Inbox/77")

    def test_parse_eml_file_extracts_received_timestamp_from_trace_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            eml_path = Path(temp_dir) / "message.eml"
            eml_path.write_text(
                "\n".join(
                    [
                        "From: Alice <alice@example.com>",
                        "To: Bob <bob@example.com>",
                        "Subject: Trace header",
                        "Date: Tue, 02 Jan 2024 03:04:05 +0000",
                        (
                            "Received: from mx.example.com by mail.example.com with "
                            "ESMTP id 12345; Tue, 02 Jan 2024 03:04:10 +0000"
                        ),
                        "",
                        "Body text",
                    ]
                ),
                encoding="utf-8",
            )

            parsed_message = _parse_eml_file(eml_path)

        self.assertEqual(
            parsed_message["received_at"].isoformat(),
            "2024-01-02T03:04:10+00:00",
        )

    def test_parse_eml_file_uses_html_body_when_plain_part_is_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            eml_path = Path(temp_dir) / "message.eml"
            eml_path.write_text(
                "\n".join(
                    [
                        "From: Alice <alice@example.com>",
                        "To: Bob <bob@example.com>",
                        "Subject: HTML fallback",
                        "Date: Tue, 02 Jan 2024 03:04:05 +0000",
                        'Content-Type: multipart/alternative; boundary="ALT"',
                        "",
                        "--ALT",
                        "Content-Type: text/plain; charset=utf-8",
                        "",
                        "   ",
                        "--ALT",
                        "Content-Type: text/html; charset=utf-8",
                        "",
                        "<html><body><p>Rendered html body</p></body></html>",
                        "--ALT--",
                    ]
                ),
                encoding="utf-8",
            )

            parsed_message = _parse_eml_file(eml_path)

        self.assertEqual(parsed_message["body_text"], "Rendered html body")

    def test_parse_eml_file_treats_inline_named_part_as_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            eml_path = Path(temp_dir) / "message.eml"
            eml_path.write_text(
                "\n".join(
                    [
                        "From: Alice <alice@example.com>",
                        "To: Bob <bob@example.com>",
                        "Subject: Inline attachment",
                        "Date: Tue, 02 Jan 2024 03:04:05 +0000",
                        'Content-Type: multipart/mixed; boundary="ALT"',
                        "",
                        "--ALT",
                        "Content-Type: text/plain; charset=utf-8",
                        "",
                        "Message body",
                        "--ALT",
                        'Content-Type: text/plain; name="inline-proof.txt"',
                        'Content-Disposition: inline; filename="inline-proof.txt"',
                        "",
                        "Attachment text should not become body text",
                        "--ALT--",
                    ]
                ),
                encoding="utf-8",
            )

            parsed_message = _parse_eml_file(eml_path)

        self.assertEqual(parsed_message["body_text"], "Message body")
        self.assertEqual(parsed_message["attachment_names"], ["inline-proof.txt"])

    def test_parser_emits_attachment_binary_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store_root = Path(temp_dir) / "attachments"
            eml_path = Path(temp_dir) / "msg.eml"
            eml_path.write_text(
                "\n".join(
                    [
                        "From: Alice <alice@example.com>",
                        "To: Bob <bob@example.com>",
                        "Subject: Test attachment",
                        "Date: Tue, 02 Jan 2024 03:04:05 +0000",
                        "MIME-Version: 1.0",
                        'Content-Type: multipart/mixed; boundary="B"',
                        "",
                        "--B",
                        "Content-Type: text/plain",
                        "",
                        "body",
                        "--B",
                        "Content-Type: application/pdf",
                        'Content-Disposition: attachment; filename="test.pdf"',
                        "Content-Transfer-Encoding: base64",
                        "",
                        "UEZG",
                        "--B--",
                    ]
                ),
                encoding="utf-8",
            )

            result = _parse_eml_file(eml_path, attachment_root=store_root)

        self.assertIn("attachments", result)
        self.assertEqual(len(result["attachments"]), 1)
        att = result["attachments"][0]
        self.assertEqual(att["attachment_name"], "test.pdf")
        self.assertIsNotNone(att["content_hash"])
        self.assertIsNotNone(att["size_bytes"])
        self.assertIsNotNone(att["storage_path"])
        self.assertEqual(att["mime_type"], "application/pdf")

    def test_import_keeps_partial_failures_non_fatal(self) -> None:
        source = PstSource(
            path=Path("C:/mail/archive.pst"),
            input_mode="file",
            requested_from=Path("C:/mail/archive.pst"),
        )
        extraction_results = [
            ExtractionResult(
                message=ExtractedMessage(
                    raw_message={
                        "subject": "Imported",
                        "sender": {"email": "alice@example.com"},
                        "to": [{"email": "bob@example.com"}],
                        "sent_at": "2024-01-02T03:04:05",
                        "body_text": "Evidence body",
                        "attachment_names": ["proof.txt"],
                        "message_id": "<imported@example.com>",
                    },
                    folder_path="Inbox",
                    logical_locator="Inbox/1",
                )
            ),
            ExtractionResult(
                failure=IngestFailure(
                    pst_source=source.source_path,
                    folder_path="Inbox",
                    logical_locator="Inbox/2",
                    source_input_mode=source.input_mode,
                    source_requested_from=source.requested_from_path,
                    error_stage="parse-eml",
                    error_type="ValueError",
                    error_message="broken message",
                )
            ),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            with patch(
                "mailattic.ingest.OutlookPstParser",
                side_effect=lambda **_kwargs: _AccountingParser(
                    lambda _source: iter(extraction_results)
                ),
            ):
                summary = import_pst_sources(
                    database_path=database_path,
                    sources=[source],
                    summary_root=Path(temp_dir) / "recipient-extraction",
                )

            connection = connect_database(database_path)
            try:
                stored_count = connection.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]
                failure_count = connection.execute(
                    "SELECT COUNT(*) FROM ingest_failures"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(summary.imported_messages, 1)
        self.assertEqual(summary.failure_count, 1)
        self.assertEqual(stored_count, 1)
        self.assertEqual(failure_count, 1)

    def test_attachment_failure_isolated_from_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            database_path = temp_root / "evidence.db"
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_text("fake", encoding="utf-8")
            store_root = temp_root / "attachments"

            fixture_dir = Path(__file__).resolve().parent / "fixtures" / "readpst-eml"
            fake_readpst = temp_root / "fake-readpst.cmd"
            fake_readpst.write_text(
                "\n".join(
                    [
                        "@echo off",
                        "setlocal EnableExtensions",
                        'set "out="',
                        ":parse",
                        'if "%~1"=="" goto done',
                        'if /I "%~1"=="-o" (',
                        '  set "out=%~2"',
                        "  shift",
                        "  shift",
                        "  goto parse",
                        ")",
                        "shift",
                        "goto parse",
                        ":done",
                        "if not defined out exit /b 2",
                        (
                            f'robocopy "{fixture_dir}" "%out%" '
                            "/E /NFL /NDL /NJH /NJS /NP >nul"
                        ),
                        "if %ERRORLEVEL% GEQ 8 exit /b %ERRORLEVEL%",
                        "exit /b 0",
                    ]
                ),
                encoding="utf-8",
            )

            initialize_database(database_path)
            with (
                patch(
                    "mailattic.attachment_store.write_attachment",
                    side_effect=OSError("disk full"),
                ),
                patch(
                    "mailattic.ingest.OutlookPstParser",
                    side_effect=lambda **_kwargs: _legacy_fixture_parser(
                        readpst_path=fake_readpst,
                        work_root=temp_root / "readpst-work",
                        attachment_root=store_root,
                    ),
                ),
            ):
                source = PstSource(
                    path=fake_pst,
                    input_mode="file",
                    requested_from=fake_pst,
                )
                import_pst_sources(
                    database_path=database_path,
                    sources=[source],
                    attachment_root=store_root,
                    summary_root=temp_root / "recipient-extraction",
                )

            connection = connect_database(database_path)
            try:
                msg_count = connection.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]
                failure_stages = [
                    row[0]
                    for row in connection.execute(
                        "SELECT error_stage FROM ingest_failures ORDER BY id"
                    )
                ]
                att_rows = connection.execute(
                    "SELECT attachment_name, content_hash, size_bytes, "
                    "mime_type, storage_path FROM message_attachments"
                ).fetchall()
            finally:
                connection.close()

        self.assertGreater(msg_count, 0)
        self.assertIn("attachment-store", failure_stages)
        self.assertGreater(len(att_rows), 0)
        att = att_rows[0]
        self.assertIsNotNone(att[0])
        self.assertIsNone(att[1])
        self.assertIsNone(att[2])
        self.assertIsNone(att[3])
        self.assertIsNone(att[4])

    def test_import_keeps_persist_failures_non_fatal(self) -> None:
        source = PstSource(
            path=Path("C:/mail/archive.pst"),
            input_mode="file",
            requested_from=Path("C:/mail/archive.pst"),
        )
        extraction_results = [
            ExtractionResult(
                message=ExtractedMessage(
                    raw_message={
                        "subject": "Broken write",
                        "sender": {"email": "alice@example.com"},
                        "to": [{"email": "bob@example.com"}],
                        "sent_at": "2024-01-02T03:04:05",
                        "body_text": "First body",
                        "attachment_names": ["first.txt"],
                        "message_id": "<first@example.com>",
                    },
                    folder_path="Inbox",
                    logical_locator="Inbox/1",
                )
            ),
            ExtractionResult(
                message=ExtractedMessage(
                    raw_message={
                        "subject": "Second write",
                        "sender": {"email": "carol@example.com"},
                        "to": [{"email": "dave@example.com"}],
                        "sent_at": "2024-01-03T03:04:05",
                        "body_text": "Second body",
                        "attachment_names": ["second.txt"],
                        "message_id": "<second@example.com>",
                    },
                    folder_path="Inbox",
                    logical_locator="Inbox/2",
                )
            ),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            with (
                patch(
                    "mailattic.ingest.OutlookPstParser",
                    side_effect=lambda **_kwargs: _AccountingParser(
                        lambda _source: iter(extraction_results)
                    ),
                ),
                patch("mailattic.ingest.persist_message") as persist_mock,
            ):
                call_count = 0

                def persist_side_effect(connection, message):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        raise sqlite3.IntegrityError("simulated write failure")
                    return storage_persist_message(connection, message)

                persist_mock.side_effect = persist_side_effect

                summary = import_pst_sources(
                    database_path=database_path,
                    sources=[source],
                    summary_root=Path(temp_dir) / "recipient-extraction",
                )

            connection = connect_database(database_path)
            try:
                stored_subjects = [
                    row[0]
                    for row in connection.execute(
                        "SELECT subject FROM messages ORDER BY id"
                    )
                ]
                failures = list(
                    connection.execute(
                        "SELECT error_stage, error_type "
                        "FROM ingest_failures ORDER BY id"
                    )
                )
            finally:
                connection.close()

        self.assertEqual(summary.imported_messages, 1)
        self.assertEqual(summary.failure_count, 1)
        self.assertEqual(stored_subjects, ["Second write"])
        self.assertEqual(
            [(row[0], row[1]) for row in failures],
            [("persist-message", "IntegrityError")],
        )

    def test_search_supports_recipient_date_and_attachment_filters(self) -> None:
        database_path: Path
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            connection = connect_database(database_path)
            try:
                source = PstSource(
                    path=Path("C:/mail/archive.pst"),
                    input_mode="file",
                    requested_from=Path("C:/mail/archive.pst"),
                )
                for raw_message, folder_path, logical_locator in (
                    (
                        {
                            "subject": "Contract review",
                            "sender": {"email": "alice@example.com"},
                            "to": [{"email": "bob@example.com"}],
                            "sent_at": "2024-01-02T03:04:05",
                            "body_text": "Discuss final contract",
                            "attachment_names": ["final contract v2.pdf"],
                            "message_id": "<contract@example.com>",
                        },
                        "Inbox",
                        "Inbox/1",
                    ),
                    (
                        {
                            "subject": "Other evidence",
                            "sender": {"email": "carol@example.com"},
                            "to": [{"email": "eve@example.com"}],
                            "sent_at": "2024-03-01T10:00:00",
                            "body_text": "Irrelevant",
                            "attachment_names": ["other.pdf"],
                            "message_id": "<other@example.com>",
                        },
                        "Archive",
                        "Archive/2",
                    ),
                ):
                    storage_persist_message(
                        connection,
                        build_normalized_message(
                            raw_message,
                            source=source,
                            folder_path=folder_path,
                            logical_locator=logical_locator,
                        ),
                    )
                connection.commit()
            finally:
                connection.close()

            results = search_messages(
                database_path=database_path,
                filters=SearchFilters(
                    recipient="bob@example.com",
                    sent_from=datetime(2024, 1, 1, 0, 0, 0),
                    sent_to=datetime(2024, 1, 31, 23, 59, 59),
                    attachment_name="contract v2.pdf",
                ),
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["subject"], "Contract review")
        self.assertEqual(results[0]["attachment_names"], ["final contract v2.pdf"])

    def test_persist_message_writes_attachment_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            source = PstSource(
                path=Path("C:/mail/archive.pst"),
                input_mode="file",
                requested_from=Path("C:/mail/archive.pst"),
            )

            msg = build_normalized_message(
                {
                    "subject": "Attachment test",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-02T03:04:05",
                    "body_text": "Test body",
                    "message_id": "<attach-test@example.com>",
                    "attachments": [
                        {
                            "attachment_name": "report.pdf",
                            "content_hash": (
                                "abc123def456abc123def456abc123def456abc123def456"
                                "abc123def456abc1"
                            ),
                            "size_bytes": 1024,
                            "mime_type": "application/pdf",
                            "storage_path": (
                                "attachments/ab/c1/abc123def456abc123def456"
                                "abc123def456abc123def456abc123def456abc1.pdf"
                            ),
                        }
                    ],
                },
                source=source,
                folder_path="Inbox",
                logical_locator="Inbox/1",
            )
            msg2 = build_normalized_message(
                {
                    "subject": "Legacy attachment",
                    "sender": {"email": "carol@example.com"},
                    "to": [{"email": "dave@example.com"}],
                    "sent_at": "2024-01-03T03:04:05",
                    "body_text": "Legacy body",
                    "message_id": "<legacy@example.com>",
                    "attachments": [
                        {
                            "attachment_name": "old.doc",
                            "content_hash": None,
                            "size_bytes": None,
                            "mime_type": None,
                            "storage_path": None,
                        }
                    ],
                },
                source=source,
                folder_path="Inbox",
                logical_locator="Inbox/2",
            )

            connection = connect_database(database_path)
            try:
                storage_persist_message(connection, msg)
                storage_persist_message(connection, msg2)
                connection.commit()
                rows = connection.execute(
                    "SELECT attachment_name, content_hash, size_bytes, mime_type, "
                    "storage_path FROM message_attachments ORDER BY id"
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "report.pdf")
        self.assertIsNotNone(rows[0][1])
        self.assertEqual(rows[0][2], 1024)
        self.assertEqual(rows[0][3], "application/pdf")
        self.assertIsNotNone(rows[0][4])
        self.assertEqual(rows[1][0], "old.doc")
        self.assertIsNone(rows[1][1])
        self.assertIsNone(rows[1][2])
        self.assertIsNone(rows[1][3])
        self.assertIsNone(rows[1][4])

    def test_search_escapes_like_wildcards_in_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            connection = connect_database(database_path)
            try:
                source = PstSource(
                    path=Path("C:/mail/archive.pst"),
                    input_mode="file",
                    requested_from=Path("C:/mail/archive.pst"),
                )
                for raw_message, logical_locator in (
                    (
                        {
                            "subject": "Exact wildcard match",
                            "sender": {"email": "john_doe@example.com"},
                            "to": [{"email": "reviewer@example.com"}],
                            "sent_at": "2024-01-02T03:04:05",
                            "body_text": "Body",
                            "attachment_names": ["100%real.pdf"],
                            "message_id": "<exact@example.com>",
                        },
                        "Inbox/1",
                    ),
                    (
                        {
                            "subject": "Wildcard false positive",
                            "sender": {"email": "johnXdoe@example.com"},
                            "to": [{"email": "reviewer@example.com"}],
                            "sent_at": "2024-01-02T03:04:06",
                            "body_text": "Body",
                            "attachment_names": ["100Xreal.pdf"],
                            "message_id": "<false-positive@example.com>",
                        },
                        "Inbox/2",
                    ),
                ):
                    storage_persist_message(
                        connection,
                        build_normalized_message(
                            raw_message,
                            source=source,
                            folder_path="Inbox",
                            logical_locator=logical_locator,
                        ),
                    )
                connection.commit()
            finally:
                connection.close()

            results = search_messages(
                database_path=database_path,
                filters=SearchFilters(
                    sender="john_doe@example.com",
                    attachment_name="100%real.pdf",
                ),
            )

        self.assertEqual(
            [result["subject"] for result in results], ["Exact wildcard match"]
        )

    def test_search_date_range_compares_timezone_aware_instants(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            connection = connect_database(database_path)
            try:
                source = PstSource(
                    path=Path("C:/mail/archive.pst"),
                    input_mode="file",
                    requested_from=Path("C:/mail/archive.pst"),
                )
                for raw_message, logical_locator in (
                    (
                        {
                            "subject": "Included",
                            "sender": {"email": "alice@example.com"},
                            "sent_at": "2024-01-02T02:30:00+00:00",
                            "body_text": "Earlier instant",
                            "message_id": "<included@example.com>",
                        },
                        "Inbox/1",
                    ),
                    (
                        {
                            "subject": "Excluded",
                            "sender": {"email": "alice@example.com"},
                            "sent_at": "2024-01-02T03:30:00+00:00",
                            "body_text": "Later instant",
                            "message_id": "<excluded@example.com>",
                        },
                        "Inbox/2",
                    ),
                ):
                    storage_persist_message(
                        connection,
                        build_normalized_message(
                            raw_message,
                            source=source,
                            folder_path="Inbox",
                            logical_locator=logical_locator,
                        ),
                    )
                connection.commit()
            finally:
                connection.close()

            results = search_messages(
                database_path=database_path,
                filters=SearchFilters(
                    sent_to=datetime.fromisoformat("2024-01-02T04:00:00+01:00")
                ),
            )

        self.assertEqual([result["subject"] for result in results], ["Included"])

    def test_search_special_character_keyword_does_not_error(self) -> None:
        """A lone quote now reports invalid FTS instead of sanitizing to zero.

        Boolean-aware keyword parsing treats " as phrase syntax. An unmatched
        phrase delimiter should therefore use the existing friendly invalid-query
        path instead of silently returning zero rows.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "evidence.db"
            self.assertEqual(main(["init-db", "--database", str(database_path)]), 0)

            captured_out = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured_out
            try:
                exit_code = main(
                    [
                        "search",
                        "--database",
                        str(database_path),
                        "--keyword",
                        '"',
                    ]
                )
            finally:
                sys.stdout = old_stdout

        self.assertEqual(exit_code, 2)
        self.assertIn("Invalid FTS keyword query", captured_out.getvalue())

    def test_cli_search_full_body_json_returns_ingestion_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            connection = connect_database(database_path)
            try:
                message = build_normalized_message(
                    {
                        "subject": "CLI full body",
                        "sender": {"email": "alice@example.com"},
                        "to": [{"name": "Bob", "email": "bob@example.com"}],
                        "sent_at": "2024-01-02T03:04:05",
                        "body_text": "<p>Hello <b>team</b></p>",
                        "message_id": "<cli-full-body@example.com>",
                        "in_reply_to": "<parent@example.com>",
                        "references": "<root@example.com> <parent@example.com>",
                    },
                    source=PstSource(
                        path=Path("C:/mail/archive.pst"),
                        input_mode="file",
                        requested_from=Path("C:/mail/archive.pst"),
                    ),
                    folder_path="Inbox",
                    logical_locator="Inbox/1",
                )
                storage_persist_message(connection, message)
                connection.commit()
            finally:
                connection.close()

            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                exit_code = main(
                    [
                        "search",
                        "--database",
                        str(database_path),
                        "--sender",
                        "alice@example.com",
                        "--full-body",
                        "--json",
                    ]
                )
            finally:
                sys.stdout = old_stdout

        self.assertEqual(exit_code, 0)
        data = json.loads(captured.getvalue())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["rendered_body_text"], "Hello team")
        self.assertEqual(data[0]["dedup_key"], "message-id:<cli-full-body@example.com>")
        self.assertEqual(
            data[0]["references"], "<root@example.com> <parent@example.com>"
        )
        self.assertEqual(data[0]["to"], [{"name": "Bob", "email": "bob@example.com"}])

    def test_cli_search_full_body_requires_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")

            captured_err = io.StringIO()
            old_stderr = sys.stderr
            sys.stderr = captured_err
            try:
                exit_code = main(
                    [
                        "search",
                        "--database",
                        str(database_path),
                        "--full-body",
                    ]
                )
            finally:
                sys.stderr = old_stderr

        self.assertEqual(exit_code, 2)
        self.assertIn("full-body mode requires --json", captured_err.getvalue())

    def test_import_search_and_show_flow_via_fake_readpst(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            database_path = temp_root / "evidence.db"
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_text("fake pst placeholder", encoding="utf-8")

            fixture_dir = Path(__file__).resolve().parent / "fixtures" / "readpst-eml"
            fake_readpst = temp_root / "fake-readpst.cmd"
            fake_readpst.write_text(
                "\n".join(
                    [
                        "@echo off",
                        "setlocal EnableExtensions",
                        'set "out="',
                        ":parse",
                        'if "%~1"=="" goto done',
                        'if /I "%~1"=="-o" (',
                        '  set "out=%~2"',
                        "  shift",
                        "  shift",
                        "  goto parse",
                        ")",
                        "shift",
                        "goto parse",
                        ":done",
                        "if not defined out exit /b 2",
                        (
                            f'robocopy "{fixture_dir}" "%out%" '
                            "/E /NFL /NDL /NJH /NJS /NP >nul"
                        ),
                        "if %ERRORLEVEL% GEQ 8 exit /b %ERRORLEVEL%",
                        "exit /b 0",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(main(["init-db", "--database", str(database_path)]), 0)
            with patch(
                "mailattic.ingest.OutlookPstParser",
                side_effect=lambda **_kwargs: _legacy_fixture_parser(
                    readpst_path=fake_readpst,
                    work_root=temp_root / "readpst-work",
                ),
            ):
                self.assertEqual(
                    main(
                        [
                            "import",
                            "--database",
                            str(database_path),
                            "--file",
                            str(fake_pst),
                        ]
                    ),
                    0,
                )

            keyword_results = search_messages(
                database_path=database_path,
                filters=SearchFilters(keyword="maplecrest"),
            )
            self.assertEqual(len(keyword_results), 1)
            self.assertEqual(
                keyword_results[0]["subject"],
                "maplecrest contract discussion",
            )

            attachment_results = search_messages(
                database_path=database_path,
                filters=SearchFilters(
                    sender="alice@example.com",
                    attachment_name="evidence.pdf",
                ),
            )
            self.assertEqual(len(attachment_results), 1)

            message = get_message(database_path, keyword_results[0]["id"])
            self.assertEqual(message["attachment_names"], ["evidence.pdf"])
            self.assertEqual(message["to"][0]["email"], "bob@example.com")
            self.assertEqual(message["pst_source"], str(fake_pst.resolve()))
            self.assertEqual(message["source_input_mode"], "file")
            self.assertEqual(message["source_requested_from"], str(fake_pst.resolve()))

    def test_cli_json_attachment_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "evidence.db"
            initialize_database(database_path)

            connection = connect_database(database_path)
            try:
                msg = build_normalized_message(
                    {
                        "subject": "CLI JSON test",
                        "sender": {"email": "alice@example.com"},
                        "to": [{"email": "bob@example.com"}],
                        "sent_at": "2024-01-02T03:04:05",
                        "body_text": "body",
                        "message_id": "<cli-json@example.com>",
                        "attachments": [
                            {
                                "attachment_name": "test.pdf",
                                "content_hash": (
                                    "deadbeef1234deadbeef1234deadbeef1234"
                                    "deadbeef1234deadbeef1234dead"
                                ),
                                "size_bytes": 256,
                                "mime_type": "application/pdf",
                                "storage_path": (
                                    "attachments/de/ad/deadbeef1234deadbeef1234"
                                    "deadbeef1234deadbeef1234deadbeef1234dead.pdf"
                                ),
                            }
                        ],
                    },
                    source=PstSource(
                        path=Path("C:/mail/archive.pst"),
                        input_mode="file",
                        requested_from=Path("C:/mail/archive.pst"),
                    ),
                    folder_path="Inbox",
                    logical_locator="Inbox/1",
                )
                result = storage_persist_message(connection, msg)
                connection.commit()
                msg_id = result.stored_message_id
            finally:
                connection.close()

            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                exit_code = main(
                    ["show", "--database", str(database_path), str(msg_id), "--json"]
                )
            finally:
                sys.stdout = old_stdout

        self.assertEqual(exit_code, 0)
        data = json.loads(captured.getvalue())
        self.assertIn("attachments", data)
        self.assertEqual(len(data["attachments"]), 1)
        att = data["attachments"][0]
        self.assertIn("content_hash", att)
        self.assertIn("size_bytes", att)
        self.assertIn("mime_type", att)
        self.assertIn("storage_path", att)
        self.assertEqual(att["attachment_name"], "test.pdf")
        self.assertEqual(
            att["content_hash"],
            "deadbeef1234deadbeef1234deadbeef1234deadbeef1234deadbeef1234dead",
        )

    def test_end_to_end_import_with_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            database_path = temp_root / "evidence.db"
            store_root = temp_root / "attachments"
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_text("fake pst placeholder", encoding="utf-8")
            fixture_dir = Path(__file__).resolve().parent / "fixtures" / "readpst-eml"
            fake_readpst = temp_root / "fake-readpst.cmd"
            fake_readpst.write_text(
                "\n".join(
                    [
                        "@echo off",
                        "setlocal EnableExtensions",
                        'set "out="',
                        ":parse",
                        'if "%~1"=="" goto done',
                        'if /I "%~1"=="-o" (',
                        '  set "out=%~2"',
                        "  shift",
                        "  shift",
                        "  goto parse",
                        ")",
                        "shift",
                        "goto parse",
                        ":done",
                        "if not defined out exit /b 2",
                        (
                            f'robocopy "{fixture_dir}" "%out%" '
                            "/E /NFL /NDL /NJH /NJS /NP >nul"
                        ),
                        "if %ERRORLEVEL% GEQ 8 exit /b %ERRORLEVEL%",
                        "exit /b 0",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(main(["init-db", "--database", str(database_path)]), 0)
            with patch(
                "mailattic.ingest.OutlookPstParser",
                side_effect=lambda **_kwargs: _legacy_fixture_parser(
                    readpst_path=fake_readpst,
                    work_root=temp_root / "readpst-work",
                    attachment_root=store_root,
                ),
            ):
                self.assertEqual(
                    main(
                        [
                            "import",
                            "--database",
                            str(database_path),
                            "--file",
                            str(fake_pst),
                            "--attachment-root",
                            str(store_root),
                        ]
                    ),
                    0,
                )

            message = get_message(database_path, 1)
            self.assertEqual(len(message["attachments"]), 1)
            attachment = message["attachments"][0]
            payload = base64.b64decode("UEZG")
            self.assertEqual(attachment["size_bytes"], len(payload))
            content_hash = attachment["content_hash"]
            self.assertIsInstance(content_hash, str)
            expected_file = (
                store_root / content_hash[0:2] / content_hash[2:4] / content_hash
            )
            self.assertTrue(expected_file.exists())
            file_bytes = expected_file.read_bytes()
            self.assertEqual(hashlib.sha256(file_bytes).hexdigest(), content_hash)

            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                exit_code = main(
                    ["show", "--database", str(database_path), "1", "--json"]
                )
            finally:
                sys.stdout = old_stdout

        self.assertEqual(exit_code, 0)
        data = json.loads(captured.getvalue())
        self.assertEqual(len(data["attachments"]), 1)
        for key in ("content_hash", "size_bytes", "mime_type", "storage_path"):
            self.assertIn(key, data["attachments"][0])

    def test_readpst_parser_uses_recursive_output_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub_readpst = _write_stub_readpst(Path(temp_dir))
            parser = ReadpstParser(
                readpst_path=stub_readpst,
                work_root=Path(temp_dir) / "work",
            )

            command = parser._build_readpst_command(
                Path("C:/mail/archive.pst"),
                Path("D:/tmp/out"),
            )

            self.assertEqual(command[0], str(stub_readpst.resolve()))
            self.assertIn("-r", command)
            self.assertNotIn("-e", command)

    def test_import_supports_readpst_mbox_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            database_path = temp_root / "evidence.db"
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_text("fake pst placeholder", encoding="utf-8")
            fixture_dir = Path(__file__).resolve().parent / "fixtures" / "readpst-mbox"

            fake_readpst = temp_root / "fake-readpst-mbox.cmd"
            fake_readpst.write_text(
                "\n".join(
                    [
                        "@echo off",
                        "setlocal EnableExtensions",
                        'set "out="',
                        ":parse",
                        'if "%~1"=="" goto done',
                        'if /I "%~1"=="-o" (',
                        '  set "out=%~2"',
                        "  shift",
                        "  shift",
                        "  goto parse",
                        ")",
                        "shift",
                        "goto parse",
                        ":done",
                        "if not defined out exit /b 2",
                        (
                            f'robocopy "{fixture_dir}" "%out%" '
                            "/E /NFL /NDL /NJH /NJS /NP >nul"
                        ),
                        "if %ERRORLEVEL% GEQ 8 exit /b %ERRORLEVEL%",
                        "exit /b 0",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(main(["init-db", "--database", str(database_path)]), 0)
            with patch(
                "mailattic.ingest.OutlookPstParser",
                side_effect=lambda **_kwargs: _legacy_fixture_parser(
                    readpst_path=fake_readpst,
                    work_root=temp_root / "readpst-work",
                ),
            ):
                exit_code = main(
                    [
                        "import",
                        "--database",
                        str(database_path),
                        "--file",
                        str(fake_pst),
                    ]
                )

            connection = connect_database(database_path)
            try:
                message_count = connection.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]
                subject = connection.execute("SELECT subject FROM messages").fetchone()[
                    0
                ]
            finally:
                connection.close()

        self.assertEqual(exit_code, 0)
        self.assertEqual(message_count, 1)
        self.assertEqual(subject, "imported from mbox")

    def test_console_entrypoint_is_available_via_uv_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            uv_cache_dir = Path(temp_dir) / "uv-cache"
            completed = subprocess.run(
                [
                    "uv",
                    "run",
                    "--cache-dir",
                    str(uv_cache_dir),
                    "mailattic",
                    "--help",
                ],
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                text=True,
                cwd=Path(__file__).resolve().parents[1],
            )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn(
            "Local PST email evidence ingestion and query tool.", completed.stdout
        )

    def test_search_returns_controlled_error_for_invalid_date_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "evidence.db"
            self.assertEqual(main(["init-db", "--database", str(database_path)]), 0)

            exit_code = main(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--from-date",
                    "not-a-date",
                ]
            )

        self.assertEqual(exit_code, 2)

    def test_search_returns_controlled_error_for_negative_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "evidence.db"
            self.assertEqual(main(["init-db", "--database", str(database_path)]), 0)

            exit_code = main(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--limit",
                    "-1",
                ]
            )

        self.assertEqual(exit_code, 2)

    def test_show_returns_controlled_error_for_missing_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "evidence.db"
            self.assertEqual(main(["init-db", "--database", str(database_path)]), 0)

            exit_code = main(
                [
                    "show",
                    "--database",
                    str(database_path),
                    "999",
                ]
            )

        self.assertEqual(exit_code, 2)

    def test_search_missing_database_returns_controlled_error_without_creating_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "missing.db"

            exit_code = main(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--keyword",
                    "test",
                ]
            )

            self.assertFalse(database_path.exists())

        self.assertEqual(exit_code, 2)

    def test_readpst_parser_omits_j_flag_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub_readpst = _write_stub_readpst(Path(temp_dir))
            parser = ReadpstParser(
                readpst_path=stub_readpst,
                work_root=Path(temp_dir) / "work",
            )

            command = parser._build_readpst_command(
                Path("C:/mail/archive.pst"),
                Path("D:/tmp/out"),
            )

            self.assertEqual(command[0], str(stub_readpst.resolve()))
            self.assertNotIn("-j", command)

    def test_readpst_parser_includes_j_flag_when_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub_readpst = _write_stub_readpst(Path(temp_dir))
            parser = ReadpstParser(
                readpst_path=stub_readpst,
                work_root=Path(temp_dir) / "work",
                parallel_jobs=4,
            )

            command = parser._build_readpst_command(
                Path("C:/mail/archive.pst"),
                Path("D:/tmp/out"),
            )

            self.assertEqual(command[0], str(stub_readpst.resolve()))

            # Verify exact flag order: readpst_path -r -q -j 4 -o <out> <src>
            j_index = command.index("-j")
            self.assertGreater(j_index, 0, "-j must not be first")
            self.assertEqual(command[j_index - 1], "-q", "-j must follow -q")
            self.assertEqual(command[j_index + 1], "4", "-j value must be '4'")
            self.assertEqual(command[j_index + 2], "-o", "-o must follow -j N")
            self.assertIn("-r", command)
            self.assertIn("-j", command)
            self.assertIn("4", command)

    def test_readpst_parser_rejects_invalid_parallel_jobs(self) -> None:
        with self.assertRaises(ValueError):
            ReadpstParser(
                readpst_path=Path("C:/tools/readpst.exe"),
                parallel_jobs=0,
            )

        with self.assertRaises(ValueError):
            ReadpstParser(
                readpst_path=Path("C:/tools/readpst.exe"),
                parallel_jobs=-1,
            )


if __name__ == "__main__":
    unittest.main()

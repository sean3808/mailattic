from __future__ import annotations

import mailbox
import sqlite3
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from mailattic.attachment_store import write_attachment
from mailattic.models import PstSource, build_normalized_message
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message
from scripts.backfill_attachments import main as backfill_main


class BackfillTests(unittest.TestCase):
    def test_backfill_idempotent_on_fixture_pst(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            database_path = initialize_database(temp_root / "evidence.db")
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_text("fake pst placeholder", encoding="utf-8")
            attachment_root = temp_root / "attachments"

            fixture_dir = temp_root / "fixture"
            inbox_dir = fixture_dir / "Inbox"
            inbox_dir.mkdir(parents=True)
            mbox_path = inbox_dir / "mbox"
            _write_fixture_mbox(mbox_path)
            fake_readpst = _write_fake_readpst(temp_root, fixture_dir)

            source = PstSource(
                path=fake_pst.resolve(),
                input_mode="file",
                requested_from=fake_pst.resolve(),
            )
            message = build_normalized_message(
                {
                    "subject": "Backfill fixture",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-02T03:04:05",
                    "body_text": "body",
                    "message_id": "<backfill@example.com>",
                    "attachment_names": ["evidence.pdf"],
                },
                source=source,
                folder_path="Inbox",
                logical_locator="Inbox/1",
            )
            connection = connect_database(database_path)
            try:
                storage_persist_message(connection, message)
                connection.commit()
            finally:
                connection.close()

            first_exit = backfill_main(
                [
                    "--database",
                    str(database_path),
                    "--readpst-path",
                    str(fake_readpst),
                    "--pst",
                    str(fake_pst),
                    "--attachment-root",
                    str(attachment_root),
                ]
            )
            first_rows = _attachment_rows(database_path)
            message_count = _message_count(database_path)

            second_exit = backfill_main(
                [
                    "--database",
                    str(database_path),
                    "--readpst-path",
                    str(fake_readpst),
                    "--pst",
                    str(fake_pst),
                    "--attachment-root",
                    str(attachment_root),
                ]
            )
            second_rows = _attachment_rows(database_path)

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(message_count, 1)
        self.assertEqual(len(first_rows), 1)
        self.assertIsNotNone(first_rows[0]["content_hash"])
        self.assertIsNotNone(first_rows[0]["size_bytes"])
        self.assertIsNotNone(first_rows[0]["mime_type"])
        self.assertIsNotNone(first_rows[0]["storage_path"])
        self.assertEqual(dict(first_rows[0]), dict(second_rows[0]))


class BackfillMappingTests(unittest.TestCase):
    """Issue #26: placeholders must map to the right original part."""

    def _run(
        self,
        parts: list[tuple[str, bytes]],
        db_names: list[str],
        complete: dict[str, bytes],
    ) -> tuple[int, list[sqlite3.Row], Path]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            database_path = initialize_database(temp_root / "evidence.db")
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_text("fake pst placeholder", encoding="utf-8")
            attachment_root = temp_root / "attachments"
            fixture_dir = temp_root / "fixture"
            inbox_dir = fixture_dir / "Inbox"
            inbox_dir.mkdir(parents=True)
            _write_fixture_mbox(inbox_dir / "mbox", parts)
            fake_readpst = _write_fake_readpst(temp_root, fixture_dir)
            source = PstSource(
                path=fake_pst.resolve(),
                input_mode="file",
                requested_from=fake_pst.resolve(),
            )
            message = build_normalized_message(
                {
                    "subject": "Backfill fixture",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-02T03:04:05",
                    "body_text": "body",
                    "message_id": "<backfill@example.com>",
                    "attachment_names": db_names,
                },
                source=source,
                folder_path="Inbox",
                logical_locator="Inbox/1",
            )
            connection = connect_database(database_path)
            try:
                storage_persist_message(connection, message)
                for name, data in complete.items():
                    record = write_attachment(
                        data, name, "application/pdf", attachment_root
                    )
                    connection.execute(
                        "UPDATE message_attachments SET content_hash = ?, "
                        "size_bytes = ?, mime_type = ?, storage_path = ? "
                        "WHERE attachment_name = ?",
                        [
                            record.content_hash,
                            record.size_bytes,
                            record.mime_type,
                            record.storage_path,
                            name,
                        ],
                    )
                connection.commit()
            finally:
                connection.close()
            exit_code = backfill_main(
                [
                    "--database",
                    str(database_path),
                    "--readpst-path",
                    str(fake_readpst),
                    "--pst",
                    str(fake_pst),
                    "--attachment-root",
                    str(attachment_root),
                ]
            )
            rows = _attachment_rows(database_path)
            payloads = {}
            for row in rows:
                if row["storage_path"]:
                    stored = Path(row["storage_path"])
                    if not stored.is_absolute():
                        stored = attachment_root.parent / stored
                    payloads[row["attachment_name"]] = stored.read_bytes()
        return exit_code, rows, payloads  # type: ignore[return-value]

    def test_complete_a_pending_b_gets_b_bytes(self) -> None:
        exit_code, _rows, payloads = self._run(
            [("a.pdf", b"A-data"), ("b.pdf", b"B-data")],
            ["a.pdf", "b.pdf"],
            {"a.pdf": b"A-data"},
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(payloads, {"a.pdf": b"A-data", "b.pdf": b"B-data"})

    def test_reordered_original_does_not_mismatch(self) -> None:
        exit_code, _rows, payloads = self._run(
            [("b.pdf", b"B-data"), ("a.pdf", b"A-data")],
            ["a.pdf", "b.pdf"],
            {"a.pdf": b"A-data"},
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(payloads, {"a.pdf": b"A-data", "b.pdf": b"B-data"})

    def test_duplicate_names_refused_without_writing(self) -> None:
        exit_code, rows, _payloads = self._run(
            [("x.pdf", b"one"), ("x.pdf", b"two")],
            ["x.pdf", "x.pdf"],
            {},
        )
        self.assertNotEqual(exit_code, 0)
        self.assertTrue(all(row["storage_path"] is None for row in rows))

    def test_count_mismatch_refused_without_writing(self) -> None:
        exit_code, rows, _payloads = self._run(
            [("a.pdf", b"A-data")],
            ["a.pdf", "b.pdf"],
            {},
        )
        self.assertNotEqual(exit_code, 0)
        self.assertTrue(all(row["storage_path"] is None for row in rows))


def _write_fixture_mbox(
    mbox_path: Path, parts: list[tuple[str, bytes]] | None = None
) -> None:
    message = EmailMessage()
    message["From"] = "Alice <alice@example.com>"
    message["To"] = "Bob <bob@example.com>"
    message["Subject"] = "Backfill fixture"
    message["Date"] = "Tue, 02 Jan 2024 03:04:05 +0000"
    message["Message-ID"] = "<backfill@example.com>"
    message.set_content("body")
    for name, data in parts or [("evidence.pdf", b"PFF")]:
        message.add_attachment(
            data, maintype="application", subtype="pdf", filename=name
        )
    mbox = mailbox.mbox(mbox_path)
    try:
        mbox.add(message)
        mbox.flush()
    finally:
        mbox.close()


def _write_fake_readpst(temp_root: Path, fixture_dir: Path) -> Path:
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
                f'robocopy "{fixture_dir}" "%out%" /E /NFL /NDL /NJH /NJS /NP >nul',
                "if %ERRORLEVEL% GEQ 8 exit /b %ERRORLEVEL%",
                "exit /b 0",
            ]
        ),
        encoding="utf-8",
    )
    return fake_readpst


def _attachment_rows(database_path: Path) -> list[sqlite3.Row]:
    connection = connect_database(database_path)
    try:
        return connection.execute(
            "SELECT attachment_name, content_hash, size_bytes, mime_type, "
            "storage_path FROM message_attachments ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def _message_count(database_path: Path) -> int:
    connection = connect_database(database_path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    finally:
        connection.close()

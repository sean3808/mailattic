from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from mailattic.cli import main
from mailattic.mcp_server import create_server
from mailattic.original_export import _matches_without_message_id
from mailattic.storage import connect_database, initialize_database


def _seed_raw_messages(database_path: Path) -> None:
    """Seed a scratch DB with naive, offset, null, and unparseable sent_at rows."""
    initialize_database(database_path)
    conn = connect_database(database_path)
    try:
        rows = [
            # id=1: naive sent_at (from Date: ... -0000)
            (
                1,
                "Naive UTC Mail",
                "Alice",
                "alice@example.com",
                "2025-03-26T06:42:33",
                "<naive@example.com>",
                "dedup-naive",
                "thread-1",
            ),
            # id=2: +08:00 offset-carrying sent_at
            (
                2,
                "Taipei Offset Mail",
                "Bob",
                "bob@example.com",
                "2026-05-10T14:30:00+08:00",
                "<taipei@example.com>",
                "dedup-taipei",
                "thread-1",
            ),
            # id=3: Z offset-carrying sent_at (must stay byte-identical 'Z')
            (
                3,
                "Zulu Offset Mail",
                "Carol",
                "carol@example.com",
                "2026-05-10T06:30:00Z",
                "<zulu@example.com>",
                "dedup-zulu",
                "thread-1",
            ),
            # id=4: NULL sent_at
            (
                4,
                "Null SentAt Mail",
                "Dave",
                "dave@example.com",
                None,
                "<null@example.com>",
                "dedup-null",
                "thread-2",
            ),
            # id=5: unparseable sent_at
            (
                5,
                "Bad SentAt Mail",
                "Eve",
                "eve@example.com",
                "not-a-date",
                "<bad@example.com>",
                "dedup-bad",
                "thread-2",
            ),
        ]
        for (
            msg_id,
            subject,
            sender_name,
            sender_email,
            sent_at,
            rfc_id,
            dedup_key,
            thread_key,
        ) in rows:
            conn.execute(
                """
                INSERT INTO messages (
                    id, subject, sender_name, sender_email, sent_at, received_at,
                    body_text, message_id, in_reply_to, references_header,
                    pst_source, folder_path, logical_locator,
                    source_input_mode, source_requested_from,
                    dedup_key, dedup_basis, recipient_search_text,
                    attachment_search_text, thread_key
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, NULL, NULL,
                    'C:/mail/archive.pst', 'Inbox', ?,
                    'file', 'C:/mail/archive.pst',
                    ?, 'message_id', '',
                    'spec.pdf', ?
                )
                """,
                (
                    msg_id,
                    subject,
                    sender_name,
                    sender_email,
                    sent_at,
                    sent_at,
                    f"Body for {subject}",
                    rfc_id,
                    f"Inbox/{msg_id}",
                    dedup_key,
                    thread_key,
                ),
            )
            conn.execute(
                """
                INSERT INTO message_attachments (
                    message_id, attachment_name, content_hash, size_bytes,
                    mime_type, storage_path
                ) VALUES (?, 'spec.pdf', 'shared-hash', 128, 'application/pdf', NULL)
                """,
                (msg_id,),
            )
        conn.commit()
    finally:
        conn.close()


def _run_cli_json(argv: list[str]) -> object:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = main(argv)
    if exit_code != 0:
        raise AssertionError(f"CLI exited with {exit_code}: {argv}")
    return json.loads(stdout.getvalue())


class SentAtEpochAndOffsetCliTests(unittest.TestCase):
    def test_bes_search_json_naive_offset_and_null_sent_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            _seed_raw_messages(db_path)

            conn = sqlite3.connect(db_path)
            try:
                expected_naive_epoch = conn.execute(
                    "SELECT unixepoch('2025-03-26T06:42:33')"
                ).fetchone()[0]
                expected_taipei_epoch = conn.execute(
                    "SELECT unixepoch('2026-05-10T14:30:00+08:00')"
                ).fetchone()[0]
            finally:
                conn.close()

            rows = _run_cli_json(
                [
                    "search",
                    "--database",
                    str(db_path),
                    "--sender",
                    "example.com",
                    "--json",
                ]
            )
            assert isinstance(rows, list)
            by_id = {row["id"]: row for row in rows}

            # Naive gets +00:00 and exact unixepoch
            self.assertEqual(by_id[1]["sent_at"], "2025-03-26T06:42:33+00:00")
            self.assertEqual(by_id[1]["sent_at_epoch"], expected_naive_epoch)

            # Offset-carrying values print byte-identical
            self.assertEqual(by_id[2]["sent_at"], "2026-05-10T14:30:00+08:00")
            self.assertEqual(by_id[2]["sent_at_epoch"], expected_taipei_epoch)
            self.assertEqual(by_id[3]["sent_at"], "2026-05-10T06:30:00Z")
            self.assertEqual(by_id[3]["sent_at_epoch"], expected_taipei_epoch)

            # Null and unparseable emit sent_at_epoch: null (not -62135596800)
            self.assertIsNone(by_id[4]["sent_at"])
            self.assertIsNone(by_id[4]["sent_at_epoch"])
            self.assertEqual(by_id[5]["sent_at"], "not-a-date")
            self.assertIsNone(by_id[5]["sent_at_epoch"])

    def test_bes_show_and_thread_json_carry_sent_at_epoch_and_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            _seed_raw_messages(db_path)

            shown_naive = _run_cli_json(
                ["show", "--database", str(db_path), "1", "--format", "json"]
            )
            assert isinstance(shown_naive, dict)
            self.assertEqual(shown_naive["sent_at"], "2025-03-26T06:42:33+00:00")
            self.assertEqual(shown_naive["sent_at_epoch"], 1742971353)

            shown_null = _run_cli_json(
                ["show", "--database", str(db_path), "4", "--format", "json"]
            )
            assert isinstance(shown_null, dict)
            self.assertIsNone(shown_null["sent_at"])
            self.assertIsNone(shown_null["sent_at_epoch"])

            thread_rows = _run_cli_json(
                ["thread", "--database", str(db_path), "1", "--format", "json"]
            )
            assert isinstance(thread_rows, list)
            by_id = {row["id"]: row for row in thread_rows}
            self.assertEqual(by_id[1]["sent_at"], "2025-03-26T06:42:33+00:00")
            self.assertEqual(by_id[1]["sent_at_epoch"], 1742971353)
            self.assertEqual(by_id[2]["sent_at"], "2026-05-10T14:30:00+08:00")
            self.assertEqual(by_id[3]["sent_at"], "2026-05-10T06:30:00Z")

    def test_bes_attachments_json_and_dedupe_carry_sent_at_epoch_and_offset(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            _seed_raw_messages(db_path)

            rows = _run_cli_json(
                [
                    "attachments",
                    "--database",
                    str(db_path),
                    "--name-contains",
                    "spec.pdf",
                    "--json",
                ]
            )
            assert isinstance(rows, list)
            by_msg = {row["message_db_id"]: row for row in rows}
            self.assertEqual(by_msg[1]["sent_at"], "2025-03-26T06:42:33+00:00")
            self.assertEqual(by_msg[1]["sent_at_epoch"], 1742971353)
            self.assertEqual(by_msg[2]["sent_at"], "2026-05-10T14:30:00+08:00")
            self.assertEqual(by_msg[3]["sent_at"], "2026-05-10T06:30:00Z")
            self.assertIsNone(by_msg[4]["sent_at_epoch"])
            self.assertIsNone(by_msg[5]["sent_at_epoch"])

            deduped = _run_cli_json(
                [
                    "attachments",
                    "--database",
                    str(db_path),
                    "--name-contains",
                    "spec.pdf",
                    "--dedupe",
                    "content-hash",
                    "--json",
                ]
            )
            assert isinstance(deduped, list)
            self.assertEqual(len(deduped), 1)
            self.assertIn("sent_at_epoch", deduped[0])
            carrying = {m["message_db_id"]: m for m in deduped[0]["carrying_messages"]}
            self.assertEqual(carrying[1]["sent_at"], "2025-03-26T06:42:33+00:00")
            self.assertEqual(carrying[1]["sent_at_epoch"], 1742971353)
            self.assertIsNone(carrying[4]["sent_at_epoch"])
            self.assertIsNone(carrying[5]["sent_at_epoch"])

    def test_bes_export_original_json_reports_normalized_naive_sent_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            _seed_raw_messages(db_path)
            # Mark id=1 as outbound so export-original returns not_found without
            # needing Outlook COM, while still emitting OriginalExportResult JSON.
            conn = connect_database(db_path)
            try:
                conn.execute(
                    "UPDATE messages SET source_input_mode = 'outbound' WHERE id = 1"
                )
                conn.commit()
            finally:
                conn.close()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(
                    [
                        "export-original",
                        "--database",
                        str(db_path),
                        "1",
                        "--copy-to",
                        str(Path(tmp_dir) / "out"),
                        "--json",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "not_found")
            self.assertEqual(payload["sent_at"], "2025-03-26T06:42:33+00:00")


class SentAtEpochAndOffsetMcpTests(unittest.TestCase):
    def test_mcp_read_tools_emit_sent_at_epoch_and_explicit_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            _seed_raw_messages(db_path)
            server = create_server(database_path=db_path)

            search_res = server._tool_manager.get_tool("mail_search").fn(  # type: ignore[attr-defined]
                sender="example.com", limit=10
            )
            search_rows = search_res.structured_content["result"]
            by_id = {row["id"]: row for row in search_rows}
            self.assertEqual(by_id[1]["sent_at"], "2025-03-26T06:42:33+00:00")
            self.assertEqual(by_id[1]["sent_at_epoch"], 1742971353)
            self.assertEqual(by_id[2]["sent_at"], "2026-05-10T14:30:00+08:00")
            self.assertEqual(by_id[3]["sent_at"], "2026-05-10T06:30:00Z")
            self.assertIsNone(by_id[4]["sent_at_epoch"])
            self.assertIsNone(by_id[5]["sent_at_epoch"])

            show_res = server._tool_manager.get_tool("mail_show").fn(message_id=1)  # type: ignore[attr-defined]
            show_payload = show_res.structured_content
            self.assertEqual(show_payload["sent_at"], "2025-03-26T06:42:33+00:00")
            self.assertEqual(show_payload["sent_at_epoch"], 1742971353)

            thread_res = server._tool_manager.get_tool("mail_thread").fn(identifier=1)  # type: ignore[attr-defined]
            thread_msgs = {
                m["id"]: m for m in thread_res.structured_content["messages"]
            }
            self.assertEqual(thread_msgs[1]["sent_at"], "2025-03-26T06:42:33+00:00")
            self.assertEqual(thread_msgs[1]["sent_at_epoch"], 1742971353)
            self.assertEqual(thread_msgs[3]["sent_at"], "2026-05-10T06:30:00Z")

            att_res = server._tool_manager.get_tool("mail_attachments").fn(  # type: ignore[attr-defined]
                name_contains="spec.pdf",
                dedupe="content-hash",
                carrying_messages_limit=10,
            )
            att_entry = att_res.structured_content["attachments"][0]
            self.assertIn("sent_at_epoch", att_entry)
            carrying = {m["message_db_id"]: m for m in att_entry["carrying_messages"]}
            self.assertEqual(carrying[1]["sent_at"], "2025-03-26T06:42:33+00:00")
            self.assertEqual(carrying[1]["sent_at_epoch"], 1742971353)
            self.assertIsNone(carrying[4]["sent_at_epoch"])


class OriginalExportNaiveSentAtTests(unittest.TestCase):
    def test_matches_without_message_id_treats_naive_stored_sent_at_as_utc(
        self,
    ) -> None:
        stored = {
            "subject": "Shared Subject",
            "sender_email": "alice@example.com",
            # Naive stored value (from Date: ... -0000), representing 06:30:00 UTC
            "sent_at": "2026-05-10T06:30:00",
        }
        # Outlook/IMAP aware timestamp at the same UTC instant (14:30:00+08:00)
        aware_candidate = datetime.fromisoformat("2026-05-10T14:30:00+08:00")
        self.assertTrue(
            _matches_without_message_id(
                stored,
                subject="Shared Subject",
                sender="alice@example.com",
                sent_at=aware_candidate,
            )
        )
        # Naive IMAP Date: ... -0000 candidate at the same UTC instant
        naive_candidate = datetime(2026, 5, 10, 6, 30, 0)
        self.assertTrue(
            _matches_without_message_id(
                stored,
                subject="Shared Subject",
                sender="alice@example.com",
                sent_at=naive_candidate,
            )
        )
        # Wrong instant must still fail
        wrong_candidate = datetime(2026, 5, 10, 14, 30, 0, tzinfo=UTC)
        self.assertFalse(
            _matches_without_message_id(
                stored,
                subject="Shared Subject",
                sender="alice@example.com",
                sent_at=wrong_candidate,
            )
        )

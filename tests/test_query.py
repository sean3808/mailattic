from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from mailattic.cli import main
from mailattic.datetime_bounds import TAIPEI
from mailattic.models import PstSource, build_normalized_message
from mailattic.query import (
    SearchFilters,
    get_message,
    get_sync_status,
    resolve_thread_message_ids,
    search_attachments,
    search_messages,
    walk_thread,
)
from mailattic.storage import (
    _recipient_domain_match,
    connect_database,
    initialize_database,
    record_sync_run_end,
    record_sync_run_start,
)
from mailattic.storage import persist_message as storage_persist_message

_TEST_SOURCE = PstSource(
    path=Path("C:/mail/archive.pst"),
    input_mode="file",
    requested_from=Path("C:/mail/archive.pst"),
)


def _seed_single_message(
    database_path: Path, *, label: str, sender_email: str, sent_at: str, locator: str
) -> None:
    connection = connect_database(database_path)
    try:
        msg = build_normalized_message(
            {
                "subject": label,
                "sender": {"email": sender_email},
                "sent_at": sent_at,
                "body_text": "body",
                "message_id": f"<{label}@test>",
            },
            source=_TEST_SOURCE,
            folder_path="Inbox",
            logical_locator=locator,
        )
        storage_persist_message(connection, msg)
        connection.commit()
    finally:
        connection.close()


def _seed_messages(
    temp_dir: str, filename: str, fixtures: list[tuple[str, str, str]]
) -> Path:
    """Seed a fresh database with (label, sender_email, sent_at) fixtures.

    Each caller seeds its own database file, so the locator index only
    needs to be unique within that one database -- a plain enumerate is
    enough, with no offset required.
    """
    database_path = initialize_database(Path(temp_dir) / filename)
    for index, (label, sender_email, sent_at) in enumerate(fixtures):
        _seed_single_message(
            database_path,
            label=label,
            sender_email=sender_email,
            sent_at=sent_at,
            locator=f"Inbox/{index}",
        )
    return database_path


def _seed_taipei_boundary_messages(temp_dir: str) -> Path:
    """design.md > timezone-boundary inclusion table (from-date = to-date = 2026-07-21).

    Six rows, one per row of the spec's
    ``##### Example: Timezone-boundary inclusion`` table, labeled by
    sender email so a test can assert set membership without depending on
    stored row ids.
    """
    return _seed_messages(
        temp_dir,
        "taipei_boundary.db",
        [
            (
                "included-start",
                "included-start@example.com",
                "2026-07-21T00:30:00+08:00",
            ),
            ("included-end", "included-end@example.com", "2026-07-21T23:59:00+08:00"),
            (
                "included-utc-cross",
                "included-utc-cross@example.com",
                "2026-07-20T17:00:00+00:00",
            ),
            (
                "excluded-before",
                "excluded-before@example.com",
                "2026-07-20T23:30:00+08:00",
            ),
            (
                "excluded-after",
                "excluded-after@example.com",
                "2026-07-22T00:30:00+08:00",
            ),
            (
                "included-naive-cross",
                "included-naive-cross@example.com",
                "2026-07-20T17:00:00",
            ),
        ],
    )


def _seed_instant_messages(temp_dir: str) -> Path:
    """Two messages an hour either side of a 2026-07-21T12:00:00+08:00 bound.

    Used by the offset-aware and naive-timestamp instant-selection tests
    (design.md > "Keep every input form every surface already accepts, and
    define what each one means").
    """
    return _seed_messages(
        temp_dir,
        "instant.db",
        [
            ("eleven", "eleven@example.com", "2026-07-21T11:00:00+08:00"),
            ("thirteen", "thirteen@example.com", "2026-07-21T13:00:00+08:00"),
        ],
    )


def _persist_query_message(
    connection,
    *,
    subject: str,
    sender_email: str,
    sent_at: str,
    message_id: str,
    locator: str,
    to: list[dict[str, str]] | None = None,
    attachments: list[dict[str, object | None]] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> int:
    message = build_normalized_message(
        {
            "subject": subject,
            "sender": {"email": sender_email},
            "to": to or [{"email": "recipient@example.com"}],
            "sent_at": sent_at,
            "body_text": f"Body of {subject}",
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "references": references,
            "attachments": attachments or [],
        },
        source=_TEST_SOURCE,
        folder_path="Inbox",
        logical_locator=locator,
    )
    return storage_persist_message(connection, message).stored_message_id


def _seed_linear_thread(temp_dir: str, *, count: int) -> tuple[Path, list[int]]:
    database_path = initialize_database(Path(temp_dir) / "thread_read_only.db")
    connection = connect_database(database_path)
    try:
        stored_ids: list[int] = []
        previous_message_id: str | None = None
        for index in range(count):
            sent_at = (
                datetime(2024, 1, 1, 8, 0, 0) + timedelta(minutes=index)
            ).isoformat()
            message_id = f"<thread-{index}@example.com>"
            stored_ids.append(
                _persist_query_message(
                    connection,
                    subject=f"Thread message {index}",
                    sender_email="thread@example.com",
                    sent_at=sent_at,
                    message_id=message_id,
                    in_reply_to=previous_message_id,
                    references=previous_message_id,
                    locator=f"Inbox/thread/{index}",
                )
            )
            previous_message_id = message_id
        connection.commit()
    finally:
        connection.close()
    return database_path, stored_ids


class _ReadOnlyProbeConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.write_attempted = False
        self.write_error: sqlite3.OperationalError | None = None

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        if not self.write_attempted:
            self.write_attempted = True
            try:
                self._connection.execute(
                    """
                    INSERT INTO messages (
                        subject,
                        sender_name,
                        sender_email,
                        sent_at,
                        received_at,
                        body_text,
                        message_id,
                        in_reply_to,
                        references_header,
                        pst_source,
                        folder_path,
                        logical_locator,
                        source_input_mode,
                        source_requested_from,
                        dedup_key,
                        dedup_basis,
                        recipient_search_text,
                        attachment_search_text,
                        raw_headers
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        "probe",
                        "",
                        "probe@example.com",
                        "2024-01-01T00:00:00",
                        None,
                        "probe",
                        f"<probe-{id(self)}@example.com>",
                        None,
                        None,
                        "probe.pst",
                        "Inbox",
                        f"Inbox/probe/{id(self)}",
                        "file",
                        "probe",
                        f"probe:{id(self)}",
                        "message-id",
                        "",
                        "",
                        None,
                    ],
                )
            except sqlite3.OperationalError as exc:
                self.write_error = exc
        return self._connection.execute(sql, parameters)

    def create_function(self, *args: object, **kwargs: object) -> None:
        self._connection.create_function(*args, **kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        self._connection.close()


def _make_query_connect_probe() -> tuple[
    list[bool],
    list[_ReadOnlyProbeConnection],
    object,
]:
    read_only_calls: list[bool] = []
    probes: list[_ReadOnlyProbeConnection] = []

    def _connect(database_path, *, create=True, read_only=False):
        read_only_calls.append(read_only)
        probe = _ReadOnlyProbeConnection(
            connect_database(database_path, create=create, read_only=read_only)
        )
        probes.append(probe)
        return probe

    return read_only_calls, probes, _connect


def _assert_read_only_probes(
    testcase: unittest.TestCase,
    read_only_calls: list[bool],
    probes: list[_ReadOnlyProbeConnection],
) -> None:
    testcase.assertTrue(read_only_calls)
    testcase.assertTrue(all(read_only_calls))
    testcase.assertTrue(probes)
    testcase.assertTrue(all(probe.write_attempted for probe in probes))
    for probe in probes:
        testcase.assertIsInstance(probe.write_error, sqlite3.OperationalError)


def _search_cli_sender_emails(argv: list[str], capsys) -> set[str]:
    exit_code = main(argv)
    captured = capsys.readouterr()
    assert exit_code == 0, captured.out
    return {row["sender_email"] for row in json.loads(captured.out)}


class TestSearchTaipeiDateBound:
    """Task 2.1/2.2 — `search` subcommand Taipei whole-day + instant semantics.

    design.md > "Keep every input form every surface already accepts, and
    define what each one means"; specs/message-query-surface/spec.md
    boundary-table and instant scenarios.
    """

    def test_taipei_boundary_table_via_cli(self, capsys) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _seed_taipei_boundary_messages(temp_dir)
            sender_emails = _search_cli_sender_emails(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--from-date",
                    "2026-07-21",
                    "--to-date",
                    "2026-07-21",
                    "--limit",
                    "50",
                    "--json",
                ],
                capsys,
            )

        assert "included-start@example.com" in sender_emails
        assert "included-end@example.com" in sender_emails
        assert "included-utc-cross@example.com" in sender_emails
        assert "included-naive-cross@example.com" in sender_emails
        assert "excluded-before@example.com" not in sender_emails
        assert "excluded-after@example.com" not in sender_emails

    def test_lower_bound_admits_early_morning_taipei_message(self, capsys) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "early_morning.db")
            _seed_single_message(
                database_path,
                label="morning",
                sender_email="morning@example.com",
                sent_at="2026-07-15T06:33:58+08:00",
                locator="Inbox/600",
            )
            sender_emails = _search_cli_sender_emails(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--from-date",
                    "2026-07-15",
                    "--json",
                ],
                capsys,
            )

        assert "morning@example.com" in sender_emails

    def test_upper_bound_admits_afternoon_taipei_message(self, capsys) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "afternoon.db")
            _seed_single_message(
                database_path,
                label="afternoon",
                sender_email="afternoon@example.com",
                sent_at="2026-07-22T15:56:01+08:00",
                locator="Inbox/601",
            )
            sender_emails = _search_cli_sender_emails(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--to-date",
                    "2026-07-22",
                    "--json",
                ],
                capsys,
            )

        assert "afternoon@example.com" in sender_emails

    def test_offset_aware_timestamp_bound_selects_by_instant(self, capsys) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _seed_instant_messages(temp_dir)
            sender_emails = _search_cli_sender_emails(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--from-date",
                    "2026-07-21T12:00:00+08:00",
                    "--json",
                ],
                capsys,
            )

        assert "eleven@example.com" not in sender_emails
        assert "thirteen@example.com" in sender_emails

    def test_naive_timestamp_bound_denotes_taipei_instant_not_utc(self, capsys) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _seed_instant_messages(temp_dir)

            naive_result = _search_cli_sender_emails(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--from-date",
                    "2026-07-21T12:00:00",
                    "--json",
                ],
                capsys,
            )
            taipei_result = _search_cli_sender_emails(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--from-date",
                    "2026-07-21T12:00:00+08:00",
                    "--json",
                ],
                capsys,
            )
            utc_result = _search_cli_sender_emails(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--from-date",
                    "2026-07-21T12:00:00+00:00",
                    "--json",
                ],
                capsys,
            )

        assert naive_result == taipei_result
        assert naive_result != utc_result

    def test_non_canonical_date_forms_match_canonical_form(self, capsys) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _seed_taipei_boundary_messages(temp_dir)

            def _result_for(value: str) -> set[str]:
                return _search_cli_sender_emails(
                    [
                        "search",
                        "--database",
                        str(database_path),
                        "--from-date",
                        value,
                        "--to-date",
                        value,
                        "--limit",
                        "50",
                        "--json",
                    ],
                    capsys,
                )

            canonical = _result_for("2026-07-21")
            basic = _result_for("20260721")
            week = _result_for("2026-W30-2")

        assert basic == canonical
        assert week == canonical

    def test_malformed_date_bound_is_rejected_with_diagnostic(self, capsys) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "malformed.db")
            exit_code = main(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--from-date",
                    "2026-201",
                    "--json",
                ]
            )
            captured = capsys.readouterr()

        assert exit_code != 0
        assert "2026-201" in captured.out
        assert "--from-date" in captured.out

    def test_blank_date_bound_is_unsupplied_filter_not_an_error(self, capsys) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _seed_taipei_boundary_messages(temp_dir)
            exit_code_blank = main(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--from-date",
                    "   ",
                    "--limit",
                    "50",
                    "--json",
                ]
            )
            out_blank = capsys.readouterr().out
            exit_code_omitted = main(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--limit",
                    "50",
                    "--json",
                ]
            )
            out_omitted = capsys.readouterr().out

        assert exit_code_blank == 0
        assert exit_code_omitted == 0
        assert out_blank == out_omitted

    def test_padded_date_bound_is_rejected_not_stripped(self, capsys) -> None:
        """A whitespace-padded value must be rejected, exactly as it was
        before ``normalize_optional_date_text`` existed -- ``search`` never
        strips before parsing (unlike ``mail_search``, which does)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "padded.db")
            exit_code = main(
                [
                    "search",
                    "--database",
                    str(database_path),
                    "--from-date",
                    "  2026-07-21  ",
                    "--json",
                ]
            )
            captured = capsys.readouterr()

        assert exit_code != 0
        assert "--from-date" in captured.out


class SearchMessagesNaiveSendTimeFilterTests(unittest.TestCase):
    """design.md > "an aware bound is used as given, a naive bound is
    interpreted as Asia/Taipei" for the Python query API's send-time
    filter fields (SearchFilters.sent_from / sent_to)."""

    def test_naive_datetime_filter_matches_equivalent_taipei_aware_filter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _seed_instant_messages(temp_dir)

            naive_results = search_messages(
                database_path=database_path,
                filters=SearchFilters(sent_from=datetime(2026, 7, 21, 12, 0, 0)),
                limit=50,
            )
            aware_results = search_messages(
                database_path=database_path,
                filters=SearchFilters(
                    sent_from=datetime(2026, 7, 21, 12, 0, 0, tzinfo=TAIPEI)
                ),
                limit=50,
            )

        self.assertEqual(
            {row["id"] for row in naive_results},
            {row["id"] for row in aware_results},
        )
        # Sanity: the instant fixture actually discriminates (non-empty,
        # excludes "eleven", includes "thirteen") rather than both being
        # trivially empty.
        sender_emails = {row["sender_email"] for row in naive_results}
        self.assertNotIn("eleven@example.com", sender_emails)
        self.assertIn("thirteen@example.com", sender_emails)


class QueryTests(unittest.TestCase):
    def test_get_message_exposes_attachment_binary_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            source = PstSource(
                path=Path("C:/mail/archive.pst"),
                input_mode="file",
                requested_from=Path("C:/mail/archive.pst"),
            )

            msg_with_binary = build_normalized_message(
                {
                    "subject": "With binary",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-02T03:04:05",
                    "body_text": "body",
                    "message_id": "<with-binary@example.com>",
                    "attachments": [
                        {
                            "attachment_name": "report.pdf",
                            "content_hash": (
                                "abcdef1234567890abcdef1234567890abcdef1234567890"
                                "abcdef1234567890"
                            ),
                            "size_bytes": 512,
                            "mime_type": "application/pdf",
                            "storage_path": (
                                "attachments/ab/cd/abcdef1234567890abcdef1234567890"
                                "abcdef1234567890abcdef1234567890.pdf"
                            ),
                        }
                    ],
                },
                source=source,
                folder_path="Inbox",
                logical_locator="Inbox/1",
            )
            msg_legacy = build_normalized_message(
                {
                    "subject": "Legacy null",
                    "sender": {"email": "carol@example.com"},
                    "to": [{"email": "dave@example.com"}],
                    "sent_at": "2024-01-03T03:04:05",
                    "body_text": "body2",
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
                result1 = storage_persist_message(connection, msg_with_binary)
                result2 = storage_persist_message(connection, msg_legacy)
                connection.commit()
            finally:
                connection.close()

            msg1 = get_message(database_path, result1.stored_message_id)
            msg2 = get_message(database_path, result2.stored_message_id)

        self.assertEqual(len(msg1["attachments"]), 1)
        att1 = msg1["attachments"][0]
        self.assertEqual(att1["attachment_name"], "report.pdf")
        self.assertIsInstance(att1["content_hash"], str)
        self.assertIsInstance(att1["size_bytes"], int)
        self.assertEqual(att1["mime_type"], "application/pdf")
        self.assertIsInstance(att1["storage_path"], str)

        self.assertEqual(len(msg2["attachments"]), 1)
        att2 = msg2["attachments"][0]
        self.assertEqual(att2["attachment_name"], "old.doc")
        self.assertIsNone(att2["content_hash"])
        self.assertIsNone(att2["size_bytes"])
        self.assertIsNone(att2["mime_type"])
        self.assertIsNone(att2["storage_path"])

        self.assertEqual(msg1["attachment_names"], ["report.pdf"])
        self.assertEqual(msg2["attachment_names"], ["old.doc"])

    def test_search_messages_full_body_returns_rendered_body_and_thread_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            source = PstSource(
                path=Path("C:/mail/archive.pst"),
                input_mode="file",
                requested_from=Path("C:/mail/archive.pst"),
            )

            message = build_normalized_message(
                {
                    "subject": "HTML body",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"name": "Bob", "email": "bob@example.com"}],
                    "cc": [{"name": "Carol", "email": "carol@example.com"}],
                    "sent_at": "2024-01-02T03:04:05",
                    "body_text": (
                        "<style>p{color:red}</style><p>Hello <b>team</b></p>"
                    ),
                    "message_id": "<html-body@example.com>",
                    "in_reply_to": "<parent@example.com>",
                    "references": "<root@example.com> <parent@example.com>",
                },
                source=source,
                folder_path="Inbox",
                logical_locator="Inbox/7",
            )

            connection = connect_database(database_path)
            try:
                storage_persist_message(connection, message)
                connection.commit()
            finally:
                connection.close()

            results = search_messages(
                database_path=database_path,
                filters=SearchFilters(sender="alice@example.com"),
                full_body=True,
            )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["rendered_body_text"], "Hello team")
        self.assertEqual(result["message_id"], "<html-body@example.com>")
        self.assertEqual(result["dedup_key"], "message-id:<html-body@example.com>")
        self.assertEqual(result["in_reply_to"], "<parent@example.com>")
        self.assertEqual(
            result["references"], "<root@example.com> <parent@example.com>"
        )
        self.assertEqual(result["to"], [{"name": "Bob", "email": "bob@example.com"}])
        self.assertEqual(
            result["cc"], [{"name": "Carol", "email": "carol@example.com"}]
        )
        self.assertEqual(result["bcc"], [])

    def test_search_messages_full_body_uses_dedup_fallback_and_empty_buckets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            source = PstSource(
                path=Path("C:/mail/archive.pst"),
                input_mode="file",
                requested_from=Path("C:/mail/archive.pst"),
            )

            message = build_normalized_message(
                {
                    "subject": "Fallback dedup",
                    "sender": {"email": "fallback@example.com"},
                    "sent_at": "2024-01-02T03:04:05",
                    "body_text": "",
                },
                source=source,
                folder_path="Inbox",
                logical_locator="Inbox/8",
            )

            connection = connect_database(database_path)
            try:
                storage_persist_message(connection, message)
                connection.commit()
            finally:
                connection.close()

            results = search_messages(
                database_path=database_path,
                filters=SearchFilters(sender="fallback@example.com"),
                full_body=True,
            )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIsNone(result["message_id"])
        self.assertTrue(result["dedup_key"].startswith("fallback:"))
        self.assertIsNone(result["in_reply_to"])
        self.assertIsNone(result["references"])
        self.assertEqual(result["rendered_body_text"], "")
        self.assertEqual(result["to"], [])
        self.assertEqual(result["cc"], [])
        self.assertEqual(result["bcc"], [])

    def test_search_messages_default_shape_excludes_full_body_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            source = PstSource(
                path=Path("C:/mail/archive.pst"),
                input_mode="file",
                requested_from=Path("C:/mail/archive.pst"),
            )

            message = build_normalized_message(
                {
                    "subject": "Default shape",
                    "sender": {"email": "shape@example.com"},
                    "sent_at": "2024-01-02T03:04:05",
                    "body_text": "snippet body",
                    "message_id": "<shape@example.com>",
                },
                source=source,
                folder_path="Inbox",
                logical_locator="Inbox/9",
            )

            connection = connect_database(database_path)
            try:
                storage_persist_message(connection, message)
                connection.commit()
            finally:
                connection.close()

            results = search_messages(
                database_path=database_path,
                filters=SearchFilters(sender="shape@example.com"),
            )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIn("snippet", result)
        self.assertNotIn("rendered_body_text", result)
        self.assertNotIn("dedup_key", result)
        self.assertNotIn("references", result)
        self.assertNotIn("to", result)

    # ------------------------------------------------------------------
    # Multi-value domain filters (OR semantics) — task 3.1–3.5
    # ------------------------------------------------------------------

    def _make_db_with_domain_fixtures(self, temp_dir: str) -> Path:
        """Build a temporary DB with four messages across three sender domains.

        Rows inserted:
          - msg-a1: sender alpha@alpha.com, recipient nobody@other.com
          - msg-a2: sender alice2@alpha.com, recipient nobody@other.com
          - msg-b1: sender bob@beta.com, recipient nobody@other.com
          - msg-g1: sender carol@gamma.com, recipient nobody@other.com
          - msg-r1: sender sean@self.com, recipient rx@alpha.com
            (recipient in alpha.com)
          - msg-r2: sender sean@self.com, recipient rx@beta.com
            (recipient in beta.com)
          - msg-r3: sender sean@self.com, recipient rx@delta.com
            (recipient only in delta.com)
        """
        database_path = initialize_database(Path(temp_dir) / "domain_test.db")
        source = PstSource(
            path=Path("C:/mail/archive.pst"),
            input_mode="file",
            requested_from=Path("C:/mail/archive.pst"),
        )

        fixtures = [
            {
                "subject": "Alpha 1",
                "sender": {"email": "alpha@alpha.com"},
                "to": [{"email": "nobody@other.com"}],
                "sent_at": "2024-01-01T00:00:00",
                "body_text": "body",
                "message_id": "<msg-a1@test>",
            },
            {
                "subject": "Alpha 2",
                "sender": {"email": "alice2@alpha.com"},
                "to": [{"email": "nobody@other.com"}],
                "sent_at": "2024-01-02T00:00:00",
                "body_text": "body",
                "message_id": "<msg-a2@test>",
            },
            {
                "subject": "Beta 1",
                "sender": {"email": "bob@beta.com"},
                "to": [{"email": "nobody@other.com"}],
                "sent_at": "2024-01-03T00:00:00",
                "body_text": "body",
                "message_id": "<msg-b1@test>",
            },
            {
                "subject": "Gamma 1",
                "sender": {"email": "carol@gamma.com"},
                "to": [{"email": "nobody@other.com"}],
                "sent_at": "2024-01-04T00:00:00",
                "body_text": "body",
                "message_id": "<msg-g1@test>",
            },
            {
                "subject": "Recv alpha",
                "sender": {"email": "sean@self.com"},
                "to": [{"email": "rx@alpha.com"}],
                "sent_at": "2024-01-05T00:00:00",
                "body_text": "body",
                "message_id": "<msg-r1@test>",
            },
            {
                "subject": "Recv beta",
                "sender": {"email": "sean@self.com"},
                "to": [{"email": "rx@beta.com"}],
                "sent_at": "2024-01-06T00:00:00",
                "body_text": "body",
                "message_id": "<msg-r2@test>",
            },
            {
                "subject": "Recv delta",
                "sender": {"email": "sean@self.com"},
                "to": [{"email": "rx@delta.com"}],
                "sent_at": "2024-01-07T00:00:00",
                "body_text": "body",
                "message_id": "<msg-r3@test>",
            },
        ]

        connection = connect_database(database_path)
        try:
            for i, raw in enumerate(fixtures):
                msg = build_normalized_message(
                    raw,
                    source=source,
                    folder_path="Inbox",
                    logical_locator=f"Inbox/{i + 100}",
                )
                storage_persist_message(connection, msg)
            connection.commit()
        finally:
            connection.close()

        return database_path

    def test_multi_value_sender_domain_or_returns_union_excludes_third(self) -> None:
        """Task 3.1 — sender_domain list → OR union; third domain excluded."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._make_db_with_domain_fixtures(temp_dir)
            results = search_messages(
                database_path=db,
                filters=SearchFilters(sender_domain=["alpha.com", "beta.com"]),
                limit=50,
            )

        sender_emails = {r["sender_email"] for r in results}
        # Both alpha.com rows and the one beta.com row must be present
        self.assertIn("alpha@alpha.com", sender_emails)
        self.assertIn("alice2@alpha.com", sender_emails)
        self.assertIn("bob@beta.com", sender_emails)
        # gamma.com message must NOT appear
        self.assertNotIn("carol@gamma.com", sender_emails)
        # Exactly 3 rows (2 alpha + 1 beta)
        self.assertEqual(len(results), 3)

    def test_str_and_single_element_list_produce_identical_result_set(self) -> None:
        """Task 3.2 — str argument and list[str] with one value are equivalent."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._make_db_with_domain_fixtures(temp_dir)
            results_str = search_messages(
                database_path=db,
                filters=SearchFilters(sender_domain="alpha.com"),
                limit=50,
            )
            results_list = search_messages(
                database_path=db,
                filters=SearchFilters(sender_domain=["alpha.com"]),
                limit=50,
            )

        ids_str = {r["id"] for r in results_str}
        ids_list = {r["id"] for r in results_list}
        self.assertEqual(ids_str, ids_list)
        # Sanity: both return the 2 alpha.com messages
        self.assertEqual(len(results_str), 2)

    def test_multi_flag_and_semantics_sender_and_recipient_domain(self) -> None:
        """Task 3.3 — multi-value sender_domain AND multi-value recipient_domain."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._make_db_with_domain_fixtures(temp_dir)
            # sender must be in {alpha.com, beta.com} AND recipient must be
            # in {alpha.com, beta.com}.
            # Only msg-r1 (sender=sean@self.com) and msg-r2 have recipients in
            # alpha/beta, but their sender is self.com — none of alpha/beta-sender
            # rows send to alpha/beta.
            # So result must be empty (AND of disjoint sets).
            results = search_messages(
                database_path=db,
                filters=SearchFilters(
                    sender_domain=["alpha.com", "beta.com"],
                    recipient_domain=["alpha.com", "beta.com"],
                ),
                limit=50,
            )

        # No message has sender in {alpha,beta} AND recipient in {alpha,beta}
        self.assertEqual(len(results), 0)

    def test_multi_flag_and_semantics_with_matching_row(self) -> None:
        """Task 3.3 (positive case) — row that satisfies both OR groups is returned."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "and_semantics.db")
            source = PstSource(
                path=Path("C:/mail/archive.pst"),
                input_mode="file",
                requested_from=Path("C:/mail/archive.pst"),
            )
            # msg-cross: sender in alpha.com AND recipient in beta.com — should match
            # msg-alpha-only: sender in alpha.com, recipient in gamma.com
            #   — should NOT match
            for i, raw in enumerate(
                [
                    {
                        "subject": "Cross match",
                        "sender": {"email": "alice@alpha.com"},
                        "to": [{"email": "bob@beta.com"}],
                        "sent_at": "2024-02-01T00:00:00",
                        "body_text": "body",
                        "message_id": "<cross@test>",
                    },
                    {
                        "subject": "Alpha only",
                        "sender": {"email": "alice2@alpha.com"},
                        "to": [{"email": "carol@gamma.com"}],
                        "sent_at": "2024-02-02T00:00:00",
                        "body_text": "body",
                        "message_id": "<alpha-only@test>",
                    },
                ]
            ):
                msg = build_normalized_message(
                    raw,
                    source=source,
                    folder_path="Inbox",
                    logical_locator=f"Inbox/{i + 200}",
                )
                connection = connect_database(database_path)
                try:
                    storage_persist_message(connection, msg)
                    connection.commit()
                finally:
                    connection.close()

            results = search_messages(
                database_path=database_path,
                filters=SearchFilters(
                    sender_domain=["alpha.com", "zz.com"],
                    recipient_domain=["beta.com", "yy.com"],
                ),
                limit=50,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["sender_email"], "alice@alpha.com")

    def test_empty_and_whitespace_domain_values_stripped_not_full_match(self) -> None:
        """Task 3.4 — empty strings and whitespace are discarded;
        non-alpha.com row excluded."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._make_db_with_domain_fixtures(temp_dir)
            # Pass ["alpha.com", "", "  "] — the empty values must be discarded,
            # not treated as a wildcard that matches everything.
            results = search_messages(
                database_path=db,
                filters=SearchFilters(sender_domain=["alpha.com", "", "  "]),
                limit=50,
            )

        sender_emails = {r["sender_email"] for r in results}
        # Only alpha.com rows returned
        self.assertIn("alpha@alpha.com", sender_emails)
        self.assertIn("alice2@alpha.com", sender_emails)
        # Non-alpha senders must NOT appear (proves no degrade to full match)
        self.assertNotIn("bob@beta.com", sender_emails)
        self.assertNotIn("carol@gamma.com", sender_emails)
        self.assertEqual(len(results), 2)

    # ------------------------------------------------------------------
    # participants_domain filter — multi-value OR across sender+recipient
    # ------------------------------------------------------------------

    def _make_db_with_participants_fixtures(self, temp_dir: str) -> Path:
        """Build a temporary DB with messages for participants_domain testing.

        Rows inserted:
          msg-pa1: sender only@a.com  → to nobody@c.com
            → participates in a.com (via sender)
          msg-pb1: sender only@c.com  → to recv@b.com
            → participates in b.com (via recipient), NOT sender
          msg-pc1: sender only@c.com  → to nobody@c.com
            → participates in c.com only (counter-example — never a or b)
          msg-pa2: sender alpha@a.com → to recv@b.com
            → participates in BOTH a.com (sender) AND b.com (recipient)
        """
        database_path = initialize_database(Path(temp_dir) / "participants_test.db")
        source = PstSource(
            path=Path("C:/mail/archive.pst"),
            input_mode="file",
            requested_from=Path("C:/mail/archive.pst"),
        )

        fixtures = [
            {
                "subject": "Sender a.com only",
                "sender": {"email": "only@a.com"},
                "to": [{"email": "nobody@c.com"}],
                "sent_at": "2024-03-01T00:00:00",
                "body_text": "body",
                "message_id": "<msg-pa1@test>",
            },
            {
                "subject": "Recipient b.com only",
                "sender": {"email": "only@c.com"},
                "to": [{"email": "recv@b.com"}],
                "sent_at": "2024-03-02T00:00:00",
                "body_text": "body",
                "message_id": "<msg-pb1@test>",
            },
            {
                "subject": "Neither a nor b",
                "sender": {"email": "nobody@c.com"},
                "to": [{"email": "also@c.com"}],
                "sent_at": "2024-03-03T00:00:00",
                "body_text": "body",
                "message_id": "<msg-pc1@test>",
            },
            {
                "subject": "Both sender a and recipient b",
                "sender": {"email": "alpha@a.com"},
                "to": [{"email": "recv@b.com"}],
                "sent_at": "2024-03-04T00:00:00",
                "body_text": "body",
                "message_id": "<msg-pa2@test>",
            },
        ]

        connection = connect_database(database_path)
        try:
            for i, raw in enumerate(fixtures):
                msg = build_normalized_message(
                    raw,
                    source=source,
                    folder_path="Inbox",
                    logical_locator=f"Inbox/{i + 300}",
                )
                storage_persist_message(connection, msg)
            connection.commit()
        finally:
            connection.close()

        return database_path

    def test_participants_domain_multi_value_or_dual_column_hit(self) -> None:
        """participants_domain list → OR union across sender+recipient columns.

        Guards the 2N-params path: 2 LIKE params per domain (sender suffix +
        recipient substring). Verifies both sender-side and recipient-side hits
        are returned while c.com-only row is excluded.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._make_db_with_participants_fixtures(temp_dir)
            results = search_messages(
                database_path=db,
                filters=SearchFilters(participants_domain=["a.com", "b.com"]),
                limit=50,
            )

        subjects = {r["subject"] for r in results}

        # msg-pa1: sender only@a.com — must match (sender side of a.com)
        self.assertIn("Sender a.com only", subjects)

        # msg-pb1: recipient recv@b.com, sender c.com — must match (recipient
        # side of b.com); guards that recipient column is not silently ignored
        self.assertIn("Recipient b.com only", subjects)

        # msg-pa2: sender a.com + recipient b.com — must match (both columns)
        self.assertIn("Both sender a and recipient b", subjects)

        # msg-pc1: sender c.com, recipient c.com — must NOT appear (counter-
        # example; would surface if params are misordered or column omitted)
        self.assertNotIn("Neither a nor b", subjects)

        self.assertEqual(len(results), 3)

    def test_participants_domain_str_and_single_element_list_equivalent(
        self,
    ) -> None:
        """participants_domain str vs list[str] with one value produce same result.

        Also verifies the result set is exactly the messages where sender OR
        recipient belongs to a.com — not a superset (no b.com or c.com creep).
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._make_db_with_participants_fixtures(temp_dir)
            results_str = search_messages(
                database_path=db,
                filters=SearchFilters(participants_domain="a.com"),
                limit=50,
            )
            results_list = search_messages(
                database_path=db,
                filters=SearchFilters(participants_domain=["a.com"]),
                limit=50,
            )

        subjects_str = {r["subject"] for r in results_str}
        subjects_list = {r["subject"] for r in results_list}
        self.assertEqual(subjects_str, subjects_list)

        # msg-pa1 (sender a.com) and msg-pa2 (sender a.com) must appear
        self.assertIn("Sender a.com only", subjects_str)
        self.assertIn("Both sender a and recipient b", subjects_str)

        # msg-pb1 (recipient b.com, sender c.com) must NOT appear —
        # proves filter is a.com-only, not a wildcard
        self.assertNotIn("Recipient b.com only", subjects_str)
        self.assertNotIn("Neither a nor b", subjects_str)

        self.assertEqual(len(results_str), 2)

    def test_participants_domain_multi_value_combined_with_keyword_filter(
        self,
    ) -> None:
        """participants_domain multi-value AND-ed with keyword filter.

        The participants OR-group must be parenthesised so that it combines
        correctly with other WHERE conditions via AND.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(
                Path(temp_dir) / "participants_and_keyword.db"
            )
            source = PstSource(
                path=Path("C:/mail/archive.pst"),
                input_mode="file",
                requested_from=Path("C:/mail/archive.pst"),
            )

            fixtures = [
                {
                    "subject": "Matching keyword and domain",
                    "sender": {"email": "alice@a.com"},
                    "to": [{"email": "bob@other.com"}],
                    "sent_at": "2024-04-01T00:00:00",
                    "body_text": "uniquetoken present here",
                    "message_id": "<kw-hit@test>",
                },
                {
                    "subject": "Domain match but no keyword",
                    "sender": {"email": "alice2@a.com"},
                    "to": [{"email": "bob@other.com"}],
                    "sent_at": "2024-04-02T00:00:00",
                    "body_text": "irrelevant content",
                    "message_id": "<kw-miss@test>",
                },
                {
                    "subject": "Keyword present but wrong domain",
                    "sender": {"email": "wrong@z.com"},
                    "to": [{"email": "also@z.com"}],
                    "sent_at": "2024-04-03T00:00:00",
                    "body_text": "uniquetoken present here",
                    "message_id": "<domain-miss@test>",
                },
            ]

            connection = connect_database(database_path)
            try:
                for i, raw in enumerate(fixtures):
                    msg = build_normalized_message(
                        raw,
                        source=source,
                        folder_path="Inbox",
                        logical_locator=f"Inbox/{i + 400}",
                    )
                    storage_persist_message(connection, msg)
                connection.commit()
            finally:
                connection.close()

            results = search_messages(
                database_path=database_path,
                filters=SearchFilters(
                    participants_domain=["a.com", "b.com"],
                    keyword="uniquetoken",
                ),
                limit=50,
            )

        subjects = {r["subject"] for r in results}

        # Only the row that satisfies BOTH the domain group AND the keyword
        self.assertIn("Matching keyword and domain", subjects)

        # Domain match but no keyword — AND must exclude this
        self.assertNotIn("Domain match but no keyword", subjects)

        # Keyword match but wrong domain — AND must exclude this
        self.assertNotIn("Keyword present but wrong domain", subjects)

        self.assertEqual(len(results), 1)


class RecipientDomainBoundaryTests(unittest.TestCase):
    """anchor-recipient-domain-filter: boundary-aware recipient_domain_match.

    Each assertion fails if the recipient-side condition reverts to the
    unanchored substring pattern ``LIKE '%@<domain>%'``.
    """

    def test_recipient_domain_match_function_matrix(self) -> None:
        """design.md > Implementation Contract acceptance criteria."""
        cases: list[tuple[str | None, str | None, bool]] = [
            ("alice@acme.com.evil", "acme.com", False),
            ("u@mail.acme.com", "acme.com", False),
            ("alice@acme.com海灣", "acme.com", False),
            ("bob@acme.com", "acme.com", True),
            ("'x@acme.com'; 'y@other.com'", "acme.com", True),
            ("bob@acme.com", "@acme.com", True),
            (None, "acme.com", False),
            ("", "acme.com", False),
            ("bob@acme.com", "", False),
            ("bob@acme.com", "@", False),
        ]
        for recipient_text, domain, expected in cases:
            with self.subTest(recipient_text=recipient_text, domain=domain):
                self.assertEqual(
                    bool(_recipient_domain_match(recipient_text, domain)), expected
                )

    def _make_boundary_fixture_db(self, temp_dir: str) -> Path:
        database_path = initialize_database(Path(temp_dir) / "domain_boundary.db")
        fixtures = [
            {
                "subject": "Overmatch only recipient",
                "sender": {"email": "someone1@other.com"},
                "to": [{"email": "alice@acme.com.evil"}],
                "sent_at": "2026-01-01T00:00:00",
                "body_text": "body",
                "message_id": "<overmatch@test>",
                "attachments": [{"attachment_name": "overmatch.pdf"}],
            },
            {
                "subject": "Packed blob genuine recipient",
                "sender": {"email": "someone2@other.com"},
                "to": [{"email": "'sally@acme.com'; '某人'; 'other@ext.com'"}],
                "sent_at": "2026-01-02T00:00:00",
                "body_text": "body",
                "message_id": "<packed@test>",
                "attachments": [{"attachment_name": "packed.pdf"}],
            },
            {
                "subject": "Non-ASCII overmatch only recipient",
                "sender": {"email": "someone3@other.com"},
                "to": [{"email": "alice@acme.com海灣"}],
                "sent_at": "2026-01-03T00:00:00",
                "body_text": "body",
                "message_id": "<nonascii@test>",
                "attachments": [{"attachment_name": "nonascii.pdf"}],
            },
            {
                "subject": "Sender only acme.com",
                "sender": {"email": "sender@acme.com"},
                "to": [{"email": "other@ext.com"}],
                "sent_at": "2026-01-04T00:00:00",
                "body_text": "body",
                "message_id": "<sender-only@test>",
                "attachments": [{"attachment_name": "sender-only.pdf"}],
            },
        ]
        connection = connect_database(database_path)
        try:
            for i, raw in enumerate(fixtures):
                msg = build_normalized_message(
                    raw,
                    source=_TEST_SOURCE,
                    folder_path="Inbox",
                    logical_locator=f"Inbox/domain/{i}",
                )
                storage_persist_message(connection, msg)
            connection.commit()
        finally:
            connection.close()
        return database_path

    def test_search_recipient_domain_excludes_overmatch_includes_packed_genuine(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._make_boundary_fixture_db(temp_dir)
            results = search_messages(
                database_path=db,
                filters=SearchFilters(recipient_domain="acme.com"),
                limit=50,
            )

        subjects = {r["subject"] for r in results}
        self.assertNotIn("Overmatch only recipient", subjects)
        self.assertIn("Packed blob genuine recipient", subjects)
        self.assertNotIn("Non-ASCII overmatch only recipient", subjects)
        self.assertNotIn("Sender only acme.com", subjects)
        self.assertEqual(len(results), 1)

    def test_search_recipient_domain_leading_at_is_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._make_boundary_fixture_db(temp_dir)
            plain = search_messages(
                database_path=db,
                filters=SearchFilters(recipient_domain="acme.com"),
                limit=50,
            )
            at_prefixed = search_messages(
                database_path=db,
                filters=SearchFilters(recipient_domain="@acme.com"),
                limit=50,
            )

        self.assertEqual({r["id"] for r in plain}, {r["id"] for r in at_prefixed})
        self.assertEqual(len(plain), 1)

    def test_search_participants_domain_recipient_side_boundary_sender_side_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._make_boundary_fixture_db(temp_dir)
            results = search_messages(
                database_path=db,
                filters=SearchFilters(participants_domain="acme.com"),
                limit=50,
            )

        subjects = {r["subject"] for r in results}
        self.assertNotIn("Overmatch only recipient", subjects)
        self.assertIn("Packed blob genuine recipient", subjects)
        self.assertNotIn("Non-ASCII overmatch only recipient", subjects)
        # Sender-side matching is unchanged: a message whose only acme.com
        # participant is the sender must still match.
        self.assertIn("Sender only acme.com", subjects)
        self.assertEqual(len(results), 2)

    def test_attachments_recipient_domain_and_participants_domain_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._make_boundary_fixture_db(temp_dir)
            recipient_domain_results = search_attachments(
                database_path=db,
                filters=SearchFilters(recipient_domain="acme.com"),
            )
            participants_domain_results = search_attachments(
                database_path=db,
                filters=SearchFilters(participants_domain="acme.com"),
            )

        recipient_names = {r["attachment_name"] for r in recipient_domain_results}
        self.assertNotIn("overmatch.pdf", recipient_names)
        self.assertIn("packed.pdf", recipient_names)
        self.assertNotIn("nonascii.pdf", recipient_names)
        self.assertNotIn("sender-only.pdf", recipient_names)
        self.assertEqual(len(recipient_domain_results), 1)

        participants_names = {r["attachment_name"] for r in participants_domain_results}
        self.assertNotIn("overmatch.pdf", participants_names)
        self.assertIn("packed.pdf", participants_names)
        self.assertNotIn("nonascii.pdf", participants_names)
        self.assertIn("sender-only.pdf", participants_names)
        self.assertEqual(len(participants_domain_results), 2)


class QueryReadOnlyTests(unittest.TestCase):
    def test_get_message_read_only_connection_rejects_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "get_message_ro.db")
            connection = connect_database(database_path)
            try:
                stored_message_id = _persist_query_message(
                    connection,
                    subject="Get message",
                    sender_email="reader@example.com",
                    sent_at="2024-01-01T10:00:00",
                    message_id="<get-message@example.com>",
                    locator="Inbox/get/1",
                )
                connection.commit()
            finally:
                connection.close()

            read_only_calls, probes, fake_connect = _make_query_connect_probe()
            with patch("mailattic.query.connect_database", new=fake_connect):
                message = get_message(database_path, stored_message_id, read_only=True)

        self.assertEqual(message["id"], stored_message_id)
        _assert_read_only_probes(self, read_only_calls, probes)

    def test_search_attachments_read_only_connection_rejects_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "attachments_ro.db")
            connection = connect_database(database_path)
            try:
                _persist_query_message(
                    connection,
                    subject="Attachment",
                    sender_email="attachments@example.com",
                    sent_at="2024-01-01T11:00:00",
                    message_id="<attachment@example.com>",
                    locator="Inbox/attachments/1",
                    attachments=[
                        {
                            "attachment_name": "report.pdf",
                            "content_hash": None,
                            "size_bytes": None,
                            "mime_type": None,
                            "storage_path": None,
                        }
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            read_only_calls, probes, fake_connect = _make_query_connect_probe()
            with patch("mailattic.query.connect_database", new=fake_connect):
                rows = search_attachments(
                    database_path=database_path,
                    filters=SearchFilters(attachment_name="report"),
                    read_only=True,
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attachment_name"], "report.pdf")
        _assert_read_only_probes(self, read_only_calls, probes)

    def test_resolve_thread_message_ids_read_only_connection_rejects_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path, stored_ids = _seed_linear_thread(temp_dir, count=3)
            read_only_calls, probes, fake_connect = _make_query_connect_probe()
            with patch("mailattic.query.connect_database", new=fake_connect):
                resolved_ids = resolve_thread_message_ids(
                    database_path,
                    "<thread-0@example.com>",
                    read_only=True,
                )

        self.assertEqual(resolved_ids, stored_ids)
        _assert_read_only_probes(self, read_only_calls, probes)

    def test_walk_thread_nested_hydration_inherits_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path, stored_ids = _seed_linear_thread(temp_dir, count=3)
            read_only_calls, probes, fake_connect = _make_query_connect_probe()
            with patch("mailattic.query.connect_database", new=fake_connect):
                messages = walk_thread(
                    database_path,
                    "<thread-2@example.com>",
                    read_only=True,
                )

        self.assertEqual([message["id"] for message in messages], stored_ids)
        self.assertGreaterEqual(len(probes), 4)
        _assert_read_only_probes(self, read_only_calls, probes)


def _seed_sync_run(
    database_path: Path,
    *,
    started_at: datetime,
    ended_at: datetime | None = None,
    outcome: str | None = None,
) -> None:
    """Put the sync_state row into a given state.

    Omit `ended_at` to leave the run in the `running` state; supply both
    `ended_at` and `outcome` for a terminal one.
    """
    connection = connect_database(database_path)
    try:
        record_sync_run_start(connection, started_at=started_at)
        if ended_at is not None:
            assert outcome is not None, "a terminal run needs an outcome"
            record_sync_run_end(connection, ended_at=ended_at, outcome=outcome)
    finally:
        connection.close()


class SyncStatusReadTests(unittest.TestCase):
    """Tests for the query-layer sync-freshness read (openspec/changes/
    add-mcp-sync-status task 2.1/2.2). Covers the four outcome states from
    the spec's "response fields by database state" table, plus the
    query-time-derivation guarantee for `latest_message_at`."""

    def test_never_synced_reports_null_run_timestamps_and_no_messages(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "never_synced.db")
            status = get_sync_status(database_path)

        self.assertEqual(
            status,
            {
                "last_sync_started_at": None,
                "last_sync_ended_at": None,
                "last_sync_status": "never_synced",
                "latest_message_at": None,
            },
        )

    def test_database_predating_the_feature_reports_never_synced(self) -> None:
        """A database with no sync_state table at all is reported, not raised.

        This is the state of every existing database the moment this ships:
        the MCP server opens read-only and can never create the table, so
        until the next sync run does, the tool would otherwise fail with a
        database error instead of answering the question it exists to answer.
        Removing the table-existence check turns this red.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "pre_migration.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "CREATE TABLE messages (id INTEGER PRIMARY KEY, sent_at TEXT)"
                )
                connection.commit()
            finally:
                connection.close()

            status = get_sync_status(database_path, read_only=True)

        self.assertEqual(status["last_sync_status"], "never_synced")
        self.assertIsNone(status["last_sync_started_at"])
        self.assertIsNone(status["last_sync_ended_at"])

    def test_never_synced_with_messages_still_reports_latest_message_timestamp(
        self,
    ) -> None:
        """Schema present, no sync_state row, but messages already stored
        (e.g. PST-imported) -- outcome is still never_synced, but the
        newest-message fact is independent of whether a sync ever ran."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(
                Path(temp_dir) / "never_synced_with_messages.db"
            )
            _seed_single_message(
                database_path,
                label="Pre-existing",
                sender_email="alice@example.com",
                sent_at="2026-07-01T09:00:00+08:00",
                locator="Inbox/1",
            )

            status = get_sync_status(database_path)

        self.assertEqual(status["last_sync_status"], "never_synced")
        self.assertIsNone(status["last_sync_started_at"])
        self.assertIsNone(status["last_sync_ended_at"])
        self.assertEqual(status["latest_message_at"], "2026-07-01T09:00:00+08:00")

    def test_running_outcome_reports_start_timestamp_and_null_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "running.db")
            _seed_sync_run(
                database_path, started_at=datetime(2026, 8, 4, 10, 0, tzinfo=TAIPEI)
            )

            status = get_sync_status(database_path)

        self.assertEqual(status["last_sync_status"], "running")
        self.assertIsNotNone(status["last_sync_started_at"])
        self.assertIsNone(status["last_sync_ended_at"])

    def test_completed_run_reports_both_timestamps_and_completed_outcome(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "completed.db")
            _seed_sync_run(
                database_path,
                started_at=datetime(2026, 8, 4, 10, 0, tzinfo=TAIPEI),
                ended_at=datetime(2026, 8, 4, 10, 5, tzinfo=TAIPEI),
                outcome="completed",
            )
            _seed_single_message(
                database_path,
                label="Newest",
                sender_email="bob@example.com",
                sent_at="2026-08-04T09:55:00+08:00",
                locator="Inbox/1",
            )

            status = get_sync_status(database_path)

        self.assertEqual(status["last_sync_status"], "completed")
        self.assertIsNotNone(status["last_sync_started_at"])
        self.assertIsNotNone(status["last_sync_ended_at"])
        self.assertEqual(status["latest_message_at"], "2026-08-04T09:55:00+08:00")

    def test_failed_run_reports_failed_outcome_with_both_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "failed.db")
            _seed_sync_run(
                database_path,
                started_at=datetime(2026, 8, 4, 10, 0, tzinfo=TAIPEI),
                ended_at=datetime(2026, 8, 4, 10, 5, tzinfo=TAIPEI),
                outcome="failed",
            )

            status = get_sync_status(database_path)

        self.assertEqual(status["last_sync_status"], "failed")
        self.assertIsNotNone(status["last_sync_started_at"])
        self.assertIsNotNone(status["last_sync_ended_at"])

    def test_latest_message_timestamp_derived_at_query_time_not_stored(
        self,
    ) -> None:
        """A message inserted after a completed run's end write moves
        `latest_message_at` even though the sync_state row is untouched --
        proving the value is computed live rather than cached at write time
        (design.md: "The newest-message timestamp is derived at query time
        from the stored messages, not maintained as a separate counter")."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "quiet_mailbox.db")
            _seed_sync_run(
                database_path,
                started_at=datetime(2026, 8, 4, 10, 0, tzinfo=TAIPEI),
                ended_at=datetime(2026, 8, 4, 10, 5, tzinfo=TAIPEI),
                outcome="completed",
            )
            before_status = get_sync_status(database_path)
            self.assertIsNone(before_status["latest_message_at"])

            _seed_single_message(
                database_path,
                label="Arrived after sync recorded",
                sender_email="carol@example.com",
                sent_at="2026-08-04T11:00:00+08:00",
                locator="Inbox/1",
            )

            after_status = get_sync_status(database_path)

        self.assertEqual(
            after_status["last_sync_ended_at"], before_status["last_sync_ended_at"]
        )
        self.assertEqual(after_status["latest_message_at"], "2026-08-04T11:00:00+08:00")

    def test_latest_message_respects_timezone_offset_not_string_order(self) -> None:
        """`latest_message_at` must be the latest instant, not the largest string.

        The production archive stores ten-plus distinct offsets -- nearly half
        its rows are +00:00, not +08:00 -- plus rows with no offset at all.
        The two rows below are chosen so lexical ordering and real time
        disagree: "10:00+08:00" sorts above "09:00+00:00" as text, while
        09:00 UTC is two hours after 10:00+08:00 as an instant. Reverting the
        query to MAX(sent_at) over the raw text turns this red.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "offsets.db")
            _seed_single_message(
                database_path,
                label="Lexically largest, earlier instant",
                sender_email="alice@example.com",
                sent_at="2026-08-04T10:00:00+08:00",
                locator="Inbox/1",
            )
            _seed_single_message(
                database_path,
                label="Lexically smaller, later instant",
                sender_email="bob@example.com",
                sent_at="2026-08-04T09:00:00+00:00",
                locator="Inbox/2",
            )

            status = get_sync_status(database_path)

        self.assertEqual(status["latest_message_at"], "2026-08-04T09:00:00+00:00")

    def test_get_sync_status_read_only_connection_rejects_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "sync_status_ro.db")
            _seed_sync_run(
                database_path, started_at=datetime(2026, 8, 4, 10, 0, tzinfo=TAIPEI)
            )

            read_only_calls, probes, fake_connect = _make_query_connect_probe()
            with patch("mailattic.query.connect_database", new=fake_connect):
                status = get_sync_status(database_path, read_only=True)

        self.assertEqual(status["last_sync_status"], "running")
        _assert_read_only_probes(self, read_only_calls, probes)


class ThreadTraversalSplitTests(unittest.TestCase):
    def test_resolve_thread_message_ids_returns_full_uncapped_ids_in_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path, stored_ids = _seed_linear_thread(temp_dir, count=150)
            resolved_ids = resolve_thread_message_ids(
                database_path, "<thread-149@example.com>"
            )

        self.assertEqual(resolved_ids, stored_ids)
        self.assertEqual(len(resolved_ids), 150)

    def test_resolve_thread_message_ids_raises_on_unknown_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path, _ = _seed_linear_thread(temp_dir, count=3)

            with self.assertRaises(KeyError):
                resolve_thread_message_ids(database_path, "<missing@example.com>")

    def test_walk_thread_keeps_earliest_100_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path, stored_ids = _seed_linear_thread(temp_dir, count=150)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                messages = walk_thread(database_path, stored_ids[-1])

        self.assertEqual(len(messages), 100)
        self.assertEqual([message["id"] for message in messages], stored_ids[:100])
        self.assertIn(
            "[warn] thread exceeded 100 messages, truncated to first 100 by sent_at",
            stderr.getvalue(),
        )

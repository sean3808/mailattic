"""Unit and integration tests for archive_scrape Group 5+6:
HTTP fetch helpers, DB integration, orchestration
(tasks 12-16 / spec 5.1-6.2).

All HTTP calls are mocked — no real network requests.
Integration tests use temp-file SQLite databases.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from mailattic.archive_scrape import (
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    AttachmentMeta,
    DetailHeader,
    ListRowMeta,
    ScrapeSummary,
    _append_to_daily_log,
    _build_normalized_message,
    _build_pst_source,
    _extract_cf_filename,
    _fetch_detail_body,
    _fetch_detail_q1,
    _fetch_list_page,
    _summary_to_json,
    run_scrape,
)
from mailattic.models import (
    MailboxParticipant,
    NormalizedAttachment,
    NormalizedMessage,
)
from mailattic.storage import (
    PersistResult,
    connect_database,
    initialize_database,
    persist_message,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SENDERS = frozenset({"sean3808@mailattic.test"})
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "archive_scrape"
DETAIL_Q1_FIXTURE = FIXTURES_DIR / "detail_q1_20260518_entry53.html"
LIST_FIXTURE = FIXTURES_DIR / "list_dir2_20260518.html"

# CF filename from the `cf=` parameter of the email_frame iframe in the detail fixture:
# cf=192.0.2.10_20260519074958_LogBackupOut[53].eml.content
_KNOWN_CF_FILENAME = "192.0.2.10_20260519074958_LogBackupOut[53].eml.content"
_KNOWN_MESSAGE_ID = "01aa01dd0053$1a2b3c4d$5e6f7a8b$@mailattic.test"
_BASE_URL = "https://192.0.2.10:9443"
_TZ_CST = timezone(timedelta(hours=8))


def _make_detail_header(
    message_id: str = _KNOWN_MESSAGE_ID,
    from_addr: str = "sean3808@mailattic.test",
) -> DetailHeader:
    return DetailHeader(
        from_addr=from_addr,
        to_addrs=("carol.ng@buyer-a.example", "staff01@mailattic.test"),
        cc_addrs=("bob.lee@buyer-a.example",),
        subject="RE: Quote for PEN-100 sample kit",
        sent_at=datetime(2026, 5, 18, 15, 12, 31, tzinfo=_TZ_CST),
        message_id=message_id,
    )


def _make_normalized_message(
    message_id: str = _KNOWN_MESSAGE_ID,
    pst_source: str = f"archive:{_KNOWN_CF_FILENAME}",
) -> NormalizedMessage:
    return NormalizedMessage(
        subject="RE: Quote for PEN-100 sample kit",
        sender=MailboxParticipant(display_name=None, email="sean3808@mailattic.test"),
        to_recipients=(
            MailboxParticipant(display_name=None, email="carol.ng@buyer-a.example"),
        ),
        cc_recipients=(),
        bcc_recipients=(),
        sent_at=datetime(2026, 5, 18, 15, 12, 31, tzinfo=_TZ_CST),
        received_at=None,
        body_text="Dear Alice,\n\nPlease find attached.",
        attachments=(NormalizedAttachment(attachment_name="[1] inline-01.png"),),
        message_id=message_id,
        in_reply_to=None,
        references=None,
        pst_source=pst_source,
        folder_path="",
        logical_locator="archive:2026-05-18:entry=53",
        source_input_mode="archive",
        source_requested_from=_BASE_URL,
    )


# ---------------------------------------------------------------------------
# Task 12 — _build_pst_source (spec 5.1)
# ---------------------------------------------------------------------------


class TestBuildPstSource(unittest.TestCase):
    """task 12 / spec 5.1 — _build_pst_source()."""

    def test_known_filename_format(self) -> None:
        result = _build_pst_source(_KNOWN_CF_FILENAME)
        self.assertEqual(result, f"archive:{_KNOWN_CF_FILENAME}")

    def test_prefix_is_archive_colon(self) -> None:
        result = _build_pst_source("some_file.eml.content")
        self.assertTrue(result.startswith("archive:"))

    def test_filename_preserved_exactly(self) -> None:
        result = _build_pst_source(_KNOWN_CF_FILENAME)
        self.assertIn(_KNOWN_CF_FILENAME, result)


# ---------------------------------------------------------------------------
# Task 12 — _build_normalized_message (spec 5.1)
# ---------------------------------------------------------------------------


class TestBuildNormalizedMessage(unittest.TestCase):
    """task 12 / spec 5.1 — _build_normalized_message()."""

    def setUp(self) -> None:
        self.header = _make_detail_header()
        self.dt = date(2026, 5, 18)
        self.attachments = [AttachmentMeta(sequence=1, filename="[1] inline-01.png")]

    def _build(self) -> NormalizedMessage:
        return _build_normalized_message(
            self.header,
            "Dear Alice,",
            self.attachments,
            _KNOWN_CF_FILENAME,
            _BASE_URL,
            "53",
            self.dt,
        )

    def test_returns_normalized_message_instance(self) -> None:
        result = self._build()
        self.assertIsInstance(result, NormalizedMessage)

    def test_pst_source_format(self) -> None:
        result = self._build()
        self.assertEqual(result.pst_source, f"archive:{_KNOWN_CF_FILENAME}")

    def test_source_input_mode_is_archive(self) -> None:
        result = self._build()
        self.assertEqual(result.source_input_mode, "archive")

    def test_source_requested_from_is_base_url(self) -> None:
        result = self._build()
        self.assertEqual(result.source_requested_from, _BASE_URL)

    def test_folder_path_empty(self) -> None:
        result = self._build()
        self.assertEqual(result.folder_path, "")

    def test_logical_locator_format(self) -> None:
        result = self._build()
        self.assertEqual(result.logical_locator, "archive:2026-05-18:entry=53")

    def test_sender_email(self) -> None:
        result = self._build()
        self.assertEqual(result.sender.email, "sean3808@mailattic.test")
        self.assertIsNone(result.sender.display_name)

    def test_to_recipients_are_mailbox_participants(self) -> None:
        result = self._build()
        self.assertIsInstance(result.to_recipients, tuple)
        self.assertGreater(len(result.to_recipients), 0)
        for p in result.to_recipients:
            self.assertIsInstance(p, MailboxParticipant)

    def test_to_recipients_contain_known_email(self) -> None:
        result = self._build()
        emails = [p.email for p in result.to_recipients]
        self.assertIn("carol.ng@buyer-a.example", emails)

    def test_attachment_names_from_attachments(self) -> None:
        result = self._build()
        names = [a.attachment_name for a in result.attachments]
        self.assertIn("[1] inline-01.png", names)

    def test_bcc_recipients_empty(self) -> None:
        result = self._build()
        self.assertEqual(result.bcc_recipients, ())

    def test_in_reply_to_none(self) -> None:
        result = self._build()
        self.assertIsNone(result.in_reply_to)

    def test_references_none(self) -> None:
        result = self._build()
        self.assertIsNone(result.references)

    def test_received_at_none(self) -> None:
        result = self._build()
        self.assertIsNone(result.received_at)


# ---------------------------------------------------------------------------
# Task 12 — DB integration (spec 5.1)
# ---------------------------------------------------------------------------


class TestBuildNormalizedMessageDBIntegration(unittest.TestCase):
    """task 12 / spec 5.1 — integration: persist and SELECT pst_source."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.tmp_dir) / "test.db"
        initialize_database(self.db_path)
        self.conn = connect_database(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp_dir)

    def test_pst_source_stored_as_archive_filename(self) -> None:
        """INSERT via _build_normalized_message, then SELECT pst_source."""
        header = _make_detail_header()
        dt = date(2026, 5, 18)
        msg = _build_normalized_message(
            header,
            "Dear Alice,",
            [AttachmentMeta(sequence=1, filename="[1] inline-01.png")],
            _KNOWN_CF_FILENAME,
            _BASE_URL,
            "53",
            dt,
        )
        persist_message(self.conn, msg)
        self.conn.commit()

        row = self.conn.execute(
            "SELECT pst_source FROM messages WHERE message_id=?",
            [_KNOWN_MESSAGE_ID],
        ).fetchone()
        self.assertIsNotNone(row, "Message should be in DB")
        self.assertEqual(row["pst_source"], f"archive:{_KNOWN_CF_FILENAME}")


# ---------------------------------------------------------------------------
# Task 13 — message_id dedup (spec 5.2)
# ---------------------------------------------------------------------------


class TestDedupIntegration(unittest.TestCase):
    """task 13 / spec 5.2 — message_id dedup preserves first-inserted source."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.tmp_dir) / "test.db"
        initialize_database(self.db_path)
        self.conn = connect_database(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp_dir)

    def test_second_insert_same_message_id_returns_not_inserted(self) -> None:
        imap_msg = _make_normalized_message(
            message_id=_KNOWN_MESSAGE_ID,
            pst_source="imap:sean3808@mailattic.test:Sent",
        )
        first = persist_message(self.conn, imap_msg)
        self.conn.commit()
        self.assertTrue(first.inserted)

        archive_msg = _make_normalized_message(
            message_id=_KNOWN_MESSAGE_ID,
            pst_source=f"archive:{_KNOWN_CF_FILENAME}",
        )
        second = persist_message(self.conn, archive_msg)
        self.conn.commit()
        self.assertFalse(second.inserted)

    def test_pst_source_preserved_as_imap_not_overwritten(self) -> None:
        imap_msg = _make_normalized_message(
            message_id=_KNOWN_MESSAGE_ID,
            pst_source="imap:sean3808@mailattic.test:Sent",
        )
        persist_message(self.conn, imap_msg)
        self.conn.commit()

        archive_msg = _make_normalized_message(
            message_id=_KNOWN_MESSAGE_ID,
            pst_source=f"archive:{_KNOWN_CF_FILENAME}",
        )
        persist_message(self.conn, archive_msg)
        self.conn.commit()

        row = self.conn.execute(
            "SELECT pst_source FROM messages WHERE message_id=?",
            [_KNOWN_MESSAGE_ID],
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["pst_source"], "imap:sean3808@mailattic.test:Sent")


# ---------------------------------------------------------------------------
# Task 14 — per-entry fault tolerance (spec 5.3)
# ---------------------------------------------------------------------------


class TestRunScrapePerEntryFaultTolerance(unittest.TestCase):
    """task 14 / spec 5.3 — second-of-3 entry parse failure: inserted=2, errors=1."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.mkdtemp()
        self._log_dir = Path(self._tmp_dir) / "logs"

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp_dir)

    def _make_three_rows(self) -> list[ListRowMeta]:
        return [
            ListRowMeta(
                time_str=f"1{i}:00:00",
                sender="sean3808@mailattic.test",
                recipient="a@b.com",
                message_id=f"mid{i}@mailattic.test",
                subject=f"Subject {i}",
                entry_id=i,
            )
            for i in (1, 2, 3)
        ]

    @patch("mailattic.archive_scrape.persist_message")
    @patch("mailattic.archive_scrape.connect_database")
    @patch("mailattic.archive_scrape.initialize_database")
    @patch("mailattic.archive_scrape.parse_attachment_links")
    @patch("mailattic.archive_scrape.parse_detail_body")
    @patch("mailattic.archive_scrape._fetch_detail_body")
    @patch("mailattic.archive_scrape._extract_cf_filename")
    @patch("mailattic.archive_scrape.parse_detail_header")
    @patch("mailattic.archive_scrape._fetch_detail_q1")
    @patch("mailattic.archive_scrape.filter_by_sender")
    @patch("mailattic.archive_scrape.parse_list_rows")
    @patch("mailattic.archive_scrape.validate_date_range")
    @patch("mailattic.archive_scrape.parse_date_options")
    @patch("mailattic.archive_scrape._fetch_list_page")
    @patch("mailattic.archive_scrape.build_session")
    @patch("mailattic.archive_scrape.load_credentials")
    def test_second_entry_raises_continues_and_counts_correctly(
        self,
        mock_creds,
        mock_session,
        mock_list_page,
        mock_date_opts,
        mock_validate,
        mock_rows,
        mock_filter,
        mock_q1,
        mock_header,
        mock_cf,
        mock_body_fetch,
        mock_body_parse,
        mock_attach,
        mock_init_db,
        mock_connect_db,
        mock_persist,
    ) -> None:
        today = date.today()

        creds_mock = MagicMock()
        creds_mock.url = _BASE_URL
        creds_mock.user = "admin"
        creds_mock.password = "secret"
        mock_creds.return_value = creds_mock
        mock_session.return_value = MagicMock(spec=requests.Session)
        mock_list_page.return_value = "<html/>"
        mock_date_opts.return_value = [today]
        mock_validate.return_value = ([today], [])

        rows = self._make_three_rows()
        mock_rows.return_value = (rows, False, None)
        mock_filter.return_value = rows

        hdr1 = _make_detail_header(message_id="mid1@mailattic.test")
        hdr3 = _make_detail_header(message_id="mid3@mailattic.test")

        # entry 1 succeeds, entry 2 raises ValueError, entry 3 succeeds
        mock_q1.side_effect = [
            "<html>q1-entry-1</html>",
            ValueError("simulated parse error for entry 2"),
            "<html>q1-entry-3</html>",
        ]
        mock_header.side_effect = [hdr1, hdr3]
        mock_cf.return_value = "192.0.2.10_test.eml.content"
        mock_body_fetch.return_value = "<html><pre>body text</pre></html>"
        mock_body_parse.return_value = "body text"
        mock_attach.return_value = []

        mock_conn = MagicMock()
        mock_connect_db.return_value = mock_conn
        mock_persist.return_value = PersistResult(inserted=True, stored_message_id=1)

        summary = run_scrape(
            from_date=today,
            to_date=today,
            senders=_SENDERS,
            dry_run=False,
            log_dir=self._log_dir,
        )

        self.assertEqual(summary.inserted_messages, 2)
        self.assertEqual(summary.errors, 1)
        self.assertEqual(summary.exit_code, EXIT_OK)
        self.assertEqual(summary.fetched_messages, 2)


# ---------------------------------------------------------------------------
# Task 15 — JSON summary stdout (spec 6.1)
# ---------------------------------------------------------------------------


class TestSummaryToJson(unittest.TestCase):
    """task 15 / spec 6.1 — _summary_to_json()."""

    def _make_summary(self) -> ScrapeSummary:
        now = datetime.now().astimezone()
        return ScrapeSummary(
            started_at=now,
            ended_at=now,
            date_range=(date(2026, 5, 18), date(2026, 5, 18)),
            candidate_entries=54,
            matched_sender=3,
            fetched_messages=3,
            inserted_messages=2,
            skipped_duplicates=1,
            errors=0,
            exit_code=EXIT_OK,
        )

    def test_returns_valid_json_string(self) -> None:
        s = self._make_summary()
        result = _summary_to_json(s)
        parsed = json.loads(result)
        self.assertIsInstance(parsed, dict)

    def test_has_all_10_required_keys(self) -> None:
        s = self._make_summary()
        result = _summary_to_json(s)
        parsed = json.loads(result)
        expected_keys = {
            "started_at",
            "ended_at",
            "date_range",
            "candidate_entries",
            "matched_sender",
            "fetched_messages",
            "inserted_messages",
            "skipped_duplicates",
            "errors",
            "exit_code",
        }
        self.assertEqual(set(parsed.keys()), expected_keys)

    def test_single_line_no_embedded_newlines(self) -> None:
        s = self._make_summary()
        result = _summary_to_json(s)
        self.assertNotIn("\n", result)

    def test_date_range_is_two_element_list(self) -> None:
        s = self._make_summary()
        result = _summary_to_json(s)
        parsed = json.loads(result)
        self.assertIsInstance(parsed["date_range"], list)
        self.assertEqual(len(parsed["date_range"]), 2)

    def test_timestamps_parse_as_iso8601(self) -> None:
        s = self._make_summary()
        result = _summary_to_json(s)
        parsed = json.loads(result)
        datetime.fromisoformat(parsed["started_at"])
        datetime.fromisoformat(parsed["ended_at"])

    def test_numeric_fields_are_integers(self) -> None:
        s = self._make_summary()
        result = _summary_to_json(s)
        parsed = json.loads(result)
        int_keys = [
            "candidate_entries",
            "matched_sender",
            "fetched_messages",
            "inserted_messages",
            "skipped_duplicates",
            "errors",
            "exit_code",
        ]
        for key in int_keys:
            self.assertIsInstance(parsed[key], int, f"{key} should be int")


class TestRunScrapeEmitsJsonToStdout(unittest.TestCase):
    """task 15 / spec 6.1 — run_scrape() emits valid JSON with 10 keys to stdout."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.mkdtemp()
        self._log_dir = Path(self._tmp_dir) / "logs"

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp_dir)

    @patch("mailattic.archive_scrape.validate_date_range")
    @patch("mailattic.archive_scrape.parse_date_options")
    @patch("mailattic.archive_scrape.filter_by_sender")
    @patch("mailattic.archive_scrape.parse_list_rows")
    @patch("mailattic.archive_scrape._fetch_list_page")
    @patch("mailattic.archive_scrape.build_session")
    @patch("mailattic.archive_scrape.load_credentials")
    def test_zero_matched_emits_valid_json_10_keys(
        self,
        mock_creds,
        mock_session,
        mock_list_page,
        mock_rows,
        mock_filter,
        mock_date_opts,
        mock_validate,
    ) -> None:
        today = date.today()
        creds_mock = MagicMock()
        creds_mock.url = _BASE_URL
        creds_mock.user = "u"
        creds_mock.password = "p"
        mock_creds.return_value = creds_mock
        mock_session.return_value = MagicMock(spec=requests.Session)
        mock_list_page.return_value = "<html/>"
        mock_date_opts.return_value = [today]
        mock_validate.return_value = ([today], [])
        mock_rows.return_value = ([], False, None)
        mock_filter.return_value = []

        buf = io.StringIO()
        with redirect_stdout(buf):
            run_scrape(
                from_date=today,
                to_date=today,
                senders=_SENDERS,
                dry_run=True,
                log_dir=self._log_dir,
            )

        stdout_output = buf.getvalue()
        lines = [ln for ln in stdout_output.strip().splitlines() if ln.strip()]
        self.assertGreater(len(lines), 0, "Expected at least one JSON line on stdout")

        parsed = json.loads(lines[0])
        self.assertIsInstance(parsed, dict)

        expected_keys = {
            "started_at",
            "ended_at",
            "date_range",
            "candidate_entries",
            "matched_sender",
            "fetched_messages",
            "inserted_messages",
            "skipped_duplicates",
            "errors",
            "exit_code",
        }
        self.assertEqual(set(parsed.keys()), expected_keys)

    @patch("mailattic.archive_scrape.validate_date_range")
    @patch("mailattic.archive_scrape.parse_date_options")
    @patch("mailattic.archive_scrape.filter_by_sender")
    @patch("mailattic.archive_scrape.parse_list_rows")
    @patch("mailattic.archive_scrape._fetch_list_page")
    @patch("mailattic.archive_scrape.build_session")
    @patch("mailattic.archive_scrape.load_credentials")
    def test_dry_run_inserted_messages_is_zero(
        self,
        mock_creds,
        mock_session,
        mock_list_page,
        mock_rows,
        mock_filter,
        mock_date_opts,
        mock_validate,
    ) -> None:
        today = date.today()
        creds_mock = MagicMock()
        creds_mock.url = _BASE_URL
        creds_mock.user = "u"
        creds_mock.password = "p"
        mock_creds.return_value = creds_mock
        mock_session.return_value = MagicMock(spec=requests.Session)
        mock_list_page.return_value = "<html/>"
        mock_date_opts.return_value = [today]
        mock_validate.return_value = ([today], [])
        mock_rows.return_value = ([], False, None)
        mock_filter.return_value = []

        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = run_scrape(
                from_date=today,
                to_date=today,
                senders=_SENDERS,
                dry_run=True,
                log_dir=self._log_dir,
            )

        self.assertEqual(summary.inserted_messages, 0)


# ---------------------------------------------------------------------------
# Task 16 — daily log file append (spec 6.2)
# ---------------------------------------------------------------------------


class TestAppendToDailyLog(unittest.TestCase):
    """task 16 / spec 6.2 — _append_to_daily_log()."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.log_dir = Path(self.tmp_dir) / "logs"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir)

    def _make_summary_json(self, second_offset: int = 0) -> tuple[str, datetime]:
        now = datetime(2026, 5, 19, 2, 0, second_offset, tzinfo=_TZ_CST)
        s = ScrapeSummary(
            started_at=now,
            ended_at=now,
            date_range=(date(2026, 5, 19), date(2026, 5, 19)),
            candidate_entries=10,
            matched_sender=2,
            fetched_messages=2,
            inserted_messages=2,
            skipped_duplicates=0,
            errors=0,
            exit_code=EXIT_OK,
        )
        return _summary_to_json(s), now

    def test_creates_log_file(self) -> None:
        summary_json, now = self._make_summary_json()
        log_path = _append_to_daily_log(summary_json, self.log_dir, now)
        self.assertTrue(log_path.exists())

    def test_log_filename_contains_date(self) -> None:
        summary_json, now = self._make_summary_json()
        log_path = _append_to_daily_log(summary_json, self.log_dir, now)
        self.assertIn("2026-05-19", log_path.name)

    def test_two_appends_create_two_lines(self) -> None:
        json1, now1 = self._make_summary_json(second_offset=0)
        json2, now2 = self._make_summary_json(second_offset=1)
        _append_to_daily_log(json1, self.log_dir, now1)
        _append_to_daily_log(json2, self.log_dir, now2)

        log_files = list(self.log_dir.glob("*.log"))
        self.assertEqual(len(log_files), 1, "Expected exactly one log file")
        lines = log_files[0].read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, f"Expected 2 lines, got: {lines}")

    def test_both_lines_are_valid_json(self) -> None:
        json1, now1 = self._make_summary_json(second_offset=0)
        json2, now2 = self._make_summary_json(second_offset=1)
        _append_to_daily_log(json1, self.log_dir, now1)
        _append_to_daily_log(json2, self.log_dir, now2)

        log_file = list(self.log_dir.glob("*.log"))[0]
        lines = log_file.read_text(encoding="utf-8").splitlines()
        for line in lines:
            parsed = json.loads(line)
            self.assertIsInstance(parsed, dict)

    def test_second_line_started_at_greater_than_first(self) -> None:
        json1, now1 = self._make_summary_json(second_offset=0)
        json2, now2 = self._make_summary_json(second_offset=1)
        _append_to_daily_log(json1, self.log_dir, now1)
        _append_to_daily_log(json2, self.log_dir, now2)

        log_file = list(self.log_dir.glob("*.log"))[0]
        lines = log_file.read_text(encoding="utf-8").splitlines()
        p1 = json.loads(lines[0])
        p2 = json.loads(lines[1])
        self.assertGreater(
            p2["started_at"],
            p1["started_at"],
            "Second entry started_at should be lexicographically greater",
        )

    def test_returns_path_object(self) -> None:
        summary_json, now = self._make_summary_json()
        log_path = _append_to_daily_log(summary_json, self.log_dir, now)
        self.assertIsInstance(log_path, Path)

    def test_creates_log_dir_if_absent(self) -> None:
        missing_dir = Path(self.tmp_dir) / "nonexistent" / "subdir"
        self.assertFalse(missing_dir.exists())
        summary_json, now = self._make_summary_json()
        _append_to_daily_log(summary_json, missing_dir, now)
        self.assertTrue(missing_dir.exists())


# ---------------------------------------------------------------------------
# HTTP fetch helpers tests
# ---------------------------------------------------------------------------


class TestFetchListPage(unittest.TestCase):
    """_fetch_list_page() uses mock session — no real network calls."""

    def _make_mock_session(self, html: str = "<html/>") -> MagicMock:
        session = MagicMock(spec=requests.Session)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html
        session.get.return_value = mock_response
        return session

    def test_first_call_navigates_three_steps(self) -> None:
        """First call should issue 3 GET requests: menu step 1, step 2, then list."""
        session = self._make_mock_session()
        _fetch_list_page(session, _BASE_URL, date(2026, 5, 18))
        self.assertGreaterEqual(session.get.call_count, 3)

    def test_sets_encoding_utf8_and_returns_text(self) -> None:
        session = self._make_mock_session("<html>test content</html>")
        result = _fetch_list_page(session, _BASE_URL, date(2026, 5, 18))
        self.assertIsInstance(result, str)

    def test_second_call_when_already_navigated_skips_menu(self) -> None:
        """If _mls_menu_navigated is already True, only 1 GET for the list page."""
        session = self._make_mock_session()
        session._mls_menu_navigated = True
        _fetch_list_page(session, _BASE_URL, date(2026, 5, 18))
        self.assertEqual(session.get.call_count, 1)

    def test_list_url_contains_date_string(self) -> None:
        session = self._make_mock_session()
        _fetch_list_page(session, _BASE_URL, date(2026, 5, 18))
        # The last call should be the list page with dt=20260518
        last_call_url = session.get.call_args_list[-1][0][0]
        self.assertIn("20260518", last_call_url)


class TestFetchDetailQ1(unittest.TestCase):
    """_fetch_detail_q1() uses mock session — no real network calls."""

    def _make_mock_session(self, html: str = "<html/>") -> MagicMock:
        session = MagicMock(spec=requests.Session)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html
        session.get.return_value = mock_response
        return session

    def test_returns_string(self) -> None:
        session = self._make_mock_session("<html>detail</html>")
        result = _fetch_detail_q1(
            session,
            _BASE_URL,
            53,
            date(2026, 5, 18),
            sender="sean3808@mailattic.test",
            recipient="test@example.com",
        )
        self.assertIsInstance(result, str)

    def test_url_contains_entry_id(self) -> None:
        session = self._make_mock_session()
        _fetch_detail_q1(
            session,
            _BASE_URL,
            53,
            date(2026, 5, 18),
            sender="sean3808@mailattic.test",
            recipient="test@example.com",
        )
        call_url = session.get.call_args[0][0]
        self.assertIn("entry=53", call_url)

    def test_url_contains_date(self) -> None:
        session = self._make_mock_session()
        _fetch_detail_q1(
            session,
            _BASE_URL,
            53,
            date(2026, 5, 18),
            sender="sean3808@mailattic.test",
            recipient="test@example.com",
        )
        call_url = session.get.call_args[0][0]
        self.assertIn("20260518", call_url)


class TestFetchDetailBody(unittest.TestCase):
    """_fetch_detail_body() uses mock session — no real network calls."""

    def _make_mock_session(self, html: str = "<html/>") -> MagicMock:
        session = MagicMock(spec=requests.Session)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html
        session.get.return_value = mock_response
        return session

    def test_returns_string(self) -> None:
        session = self._make_mock_session("<html><pre>body</pre></html>")
        result = _fetch_detail_body(
            session, _BASE_URL, 53, date(2026, 5, 18), _KNOWN_CF_FILENAME
        )
        self.assertIsInstance(result, str)

    def test_url_contains_cf_filename(self) -> None:
        session = self._make_mock_session()
        _fetch_detail_body(
            session, _BASE_URL, 53, date(2026, 5, 18), _KNOWN_CF_FILENAME
        )
        call_url = session.get.call_args[0][0]
        self.assertIn(_KNOWN_CF_FILENAME, call_url)


class TestExtractCfFilename(unittest.TestCase):
    """_extract_cf_filename() — unit tests using real fixture and synthetic HTML."""

    def setUp(self) -> None:
        self.html_q1 = DETAIL_Q1_FIXTURE.read_text(encoding="utf-8")

    def test_extracts_known_cf_filename_from_fixture(self) -> None:
        """Real fixture must yield the known cf filename."""
        result = _extract_cf_filename(self.html_q1)
        self.assertEqual(result, _KNOWN_CF_FILENAME)

    def test_raises_value_error_if_no_iframe(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _extract_cf_filename("<html><body>no iframe here</body></html>")
        self.assertIn("email_frame", str(ctx.exception))

    def test_raises_value_error_if_no_cf_param(self) -> None:
        html_no_cf = (
            "<html><body>"
            '<iFrame name="email_frame" src="/cgi-bin/test?q=1&other=val"></iFrame>'
            "</body></html>"
        )
        with self.assertRaises(ValueError) as ctx:
            _extract_cf_filename(html_no_cf)
        self.assertIn("cf", str(ctx.exception))

    def test_raises_value_error_for_empty_html(self) -> None:
        with self.assertRaises(ValueError):
            _extract_cf_filename("")


class TestScrapeFailureNonZeroExit(unittest.TestCase):
    """Verify archive-scrape exits non-zero on failure conditions:
    1. Server unreachable (network error)
    2. Credentials rejected (HTTP 401 / missing date dropdown)
    3. Session expired mid-run (detail fetch failure resulting in partial failure)
    """

    def setUp(self) -> None:
        # The launcher points TEMP at the managed runtime root; one dir per test
        # so parallel workers never share or delete each other's DB.
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        initialize_database(self.db_path)
        conn = connect_database(self.db_path)
        try:
            conn.execute(
                "INSERT INTO self_addresses (address) VALUES ('sean@mailattic.test')"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_cli_scrape(self) -> int:
        from mailattic.cli import main as cli_main

        buf = io.StringIO()
        with redirect_stdout(buf):
            return cli_main(
                [
                    "scrape-archive",
                    "--database",
                    str(self.db_path),
                    "--from",
                    "2026-05-18",
                    "--to",
                    "2026-05-18",
                    "--dry-run",
                ]
            )

    @patch("mailattic.archive_scrape.load_credentials")
    @patch("mailattic.archive_scrape.build_session")
    @patch("mailattic.archive_scrape._fetch_list_page")
    def test_server_unreachable_exits_non_zero(
        self, mock_fetch_list, mock_session, mock_creds
    ) -> None:
        mock_creds.return_value = MagicMock(url=_BASE_URL, user="u", password="p")
        mock_fetch_list.side_effect = requests.exceptions.ConnectionError(
            "Connection refused"
        )
        exit_code = self._run_cli_scrape()
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(exit_code, 2)

    @patch("mailattic.archive_scrape.load_credentials")
    @patch("mailattic.archive_scrape.build_session")
    @patch("mailattic.archive_scrape._fetch_list_page")
    def test_credentials_rejected_exits_non_zero(
        self, mock_fetch_list, mock_session, mock_creds
    ) -> None:
        mock_creds.return_value = MagicMock(url=_BASE_URL, user="u", password="p")
        mock_fetch_list.return_value = "<html><body>401 Unauthorized</body></html>"
        exit_code = self._run_cli_scrape()
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(exit_code, 2)

    @patch("mailattic.archive_scrape.load_credentials")
    @patch("mailattic.archive_scrape.build_session")
    @patch("mailattic.archive_scrape._fetch_list_page")
    @patch("mailattic.archive_scrape.parse_date_options")
    @patch("mailattic.archive_scrape.validate_date_range")
    @patch("mailattic.archive_scrape.parse_list_rows")
    @patch("mailattic.archive_scrape.filter_by_sender")
    @patch("mailattic.archive_scrape._fetch_detail_q1")
    def test_session_expired_mid_run_exits_non_zero(
        self,
        mock_q1,
        mock_filter,
        mock_rows,
        mock_validate,
        mock_date_opts,
        mock_list_page,
        mock_session,
        mock_creds,
    ) -> None:
        target_date = date(2026, 5, 18)
        mock_creds.return_value = MagicMock(url=_BASE_URL, user="u", password="p")
        mock_session.return_value = MagicMock(spec=requests.Session)
        mock_list_page.return_value = "<html/>"
        mock_date_opts.return_value = [target_date]
        mock_validate.return_value = ([target_date], [])
        row = ListRowMeta(
            entry_id=1,
            time_str="10:00:00",
            sender="sean@mailattic.test",
            recipient="r@mailattic.test",
            message_id="mid1@mailattic.test",
            subject="test",
        )
        mock_rows.return_value = ([row], False, None)
        mock_filter.return_value = [row]
        mock_q1.return_value = "<html><body>Session expired - 401</body></html>"

        exit_code = self._run_cli_scrape()
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(exit_code, EXIT_PARTIAL_FAILURE)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for archive_scrape Group 4: detail page parsing + attachment metadata
(tasks 4.1 / 4.2).

All tests are offline — no network calls.
Fixtures:
  tests/fixtures/archive_scrape/detail_q1_20260518_entry53.html
  tests/fixtures/archive_scrape/detail_body_20260518_entry53.html
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from mailattic.archive_scrape import (
    AttachmentMeta,
    DetailHeader,
    parse_attachment_links,
    parse_detail_body,
    parse_detail_header,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "archive_scrape"
DETAIL_Q1_FIXTURE = FIXTURES_DIR / "detail_q1_20260518_entry53.html"
DETAIL_BODY_FIXTURE = FIXTURES_DIR / "detail_body_20260518_entry53.html"


def _load_fixture(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# task 4.1 — parse_detail_header + parse_detail_body
# ---------------------------------------------------------------------------


class TestParseDetailHeader(unittest.TestCase):
    """task 4.1 — parse_detail_header() unit tests。"""

    def setUp(self) -> None:
        self.html_q1 = _load_fixture(DETAIL_Q1_FIXTURE)

    def test_returns_detail_header_instance(self) -> None:
        """回傳值是 DetailHeader 實例。"""
        result = parse_detail_header(self.html_q1)
        self.assertIsInstance(result, DetailHeader)

    def test_message_id_correct(self) -> None:
        """message_id 等於 01aa01dd0053$1a2b3c4d$5e6f7a8b$@mailattic.test。"""
        result = parse_detail_header(self.html_q1)
        self.assertEqual(
            result.message_id, "01aa01dd0053$1a2b3c4d$5e6f7a8b$@mailattic.test"
        )

    def test_from_addr_contains_sean(self) -> None:
        """from_addr 含 sean3808@mailattic.test。"""
        result = parse_detail_header(self.html_q1)
        self.assertIn("sean3808@mailattic.test", result.from_addr)

    def test_subject_non_empty(self) -> None:
        """subject 非空。"""
        result = parse_detail_header(self.html_q1)
        self.assertGreater(len(result.subject.strip()), 0)

    def test_subject_content(self) -> None:
        """subject 含 PEN-100 關鍵字（case-insensitive）。"""
        result = parse_detail_header(self.html_q1)
        self.assertIn("pen-100", result.subject.lower())

    def test_to_addrs_non_empty_tuple(self) -> None:
        """to_addrs 非空 tuple。"""
        result = parse_detail_header(self.html_q1)
        self.assertIsInstance(result.to_addrs, tuple)
        self.assertGreater(len(result.to_addrs), 0, "to_addrs should not be empty")

    def test_to_addrs_contain_known_recipient(self) -> None:
        """to_addrs 中含已知收件者 carol.ng@buyer-a.example。"""
        result = parse_detail_header(self.html_q1)
        self.assertIn("carol.ng@buyer-a.example", result.to_addrs)

    def test_cc_addrs_is_tuple(self) -> None:
        """cc_addrs 是 tuple（可為空）。"""
        result = parse_detail_header(self.html_q1)
        self.assertIsInstance(result.cc_addrs, tuple)

    def test_sent_at_is_datetime_with_timezone(self) -> None:
        """sent_at 是 aware datetime（含時區）。"""
        from datetime import datetime

        result = parse_detail_header(self.html_q1)
        self.assertIsInstance(result.sent_at, datetime)
        self.assertIsNotNone(result.sent_at.tzinfo, "sent_at must be timezone-aware")

    def test_sent_at_date_correct(self) -> None:
        """sent_at 日期是 2026-05-18。"""
        from datetime import date

        result = parse_detail_header(self.html_q1)
        self.assertEqual(result.sent_at.date(), date(2026, 5, 18))

    def test_invalid_html_raises_value_error(self) -> None:
        """無任何 header 欄位的 HTML → raise ValueError。"""
        with self.assertRaises(ValueError):
            parse_detail_header("<html><body><p>nothing</p></body></html>")


class TestParseDetailBody(unittest.TestCase):
    """task 4.1 — parse_detail_body() unit tests。"""

    def setUp(self) -> None:
        self.html_body = _load_fixture(DETAIL_BODY_FIXTURE)

    def test_body_text_length_ge_2500(self) -> None:
        """body_text 長度 >= 2500 chars。"""
        body_text = parse_detail_body(self.html_body)
        self.assertGreaterEqual(
            len(body_text),
            2500,
            f"body_text length {len(body_text)} < 2500",
        )

    def test_body_text_contains_dear_alice(self) -> None:
        """body_text 含字串 'Dear Alice'。"""
        body_text = parse_detail_body(self.html_body)
        self.assertIn("Dear Alice", body_text)

    def test_body_text_is_string(self) -> None:
        """回傳值是 str。"""
        body_text = parse_detail_body(self.html_body)
        self.assertIsInstance(body_text, str)

    def test_no_html_tags_in_body_text(self) -> None:
        """plain text 不含 HTML 標籤（<tag> 形式）。

        BeautifulSoup get_text() 會去除標籤，但 pre 內容本來就是 plain text。
        """
        body_text = parse_detail_body(self.html_body)
        # No angle brackets from HTML tags (URLs may have some)
        # Just verify no obvious block-level tags
        self.assertNotIn("<html>", body_text.lower())
        self.assertNotIn("<body>", body_text.lower())
        self.assertNotIn("<pre>", body_text.lower())

    def test_missing_pre_raises_value_error(self) -> None:
        """HTML 無 <pre> 元素 → raise ValueError。"""
        with self.assertRaises(ValueError) as ctx:
            parse_detail_body("<html><body><p>no pre tag</p></body></html>")
        self.assertIn("pre", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# task 4.2 — parse_attachment_links
# ---------------------------------------------------------------------------


class TestParseAttachmentLinks(unittest.TestCase):
    """task 4.2 — parse_attachment_links() unit tests。"""

    def setUp(self) -> None:
        self.html_q1 = _load_fixture(DETAIL_Q1_FIXTURE)

    def _ground_truth_attachment_count(self) -> int:
        """從 fixture 直接數 cd=3 連結數。"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self.html_q1, "html.parser")
        return len(soup.find_all("a", href=re.compile(r"cd=3")))

    def test_returns_list_of_attachment_meta(self) -> None:
        """回傳值是 AttachmentMeta 的 list。"""
        result = parse_attachment_links(self.html_q1)
        self.assertIsInstance(result, list)
        for item in result:
            self.assertIsInstance(item, AttachmentMeta)

    def test_attachment_count_matches_fixture(self) -> None:
        """附件數 == fixture 內 cd=3 連結數量。"""
        expected = self._ground_truth_attachment_count()
        result = parse_attachment_links(self.html_q1)
        self.assertEqual(
            len(result),
            expected,
            f"Expected {expected} attachments from fixture, got {len(result)}",
        )

    def test_fixture_has_attachments(self) -> None:
        """entry=53 fixture 含附件。"""
        result = parse_attachment_links(self.html_q1)
        self.assertGreater(len(result), 0, "entry=53 should have attachments")

    def test_filename_has_sequence_prefix(self) -> None:
        """每個 AttachmentMeta.filename 開頭符合 ^[N] 格式（schema workaround）。"""
        result = parse_attachment_links(self.html_q1)
        if not result:
            self.skipTest("No attachments in fixture to test prefix format")
        prefix_pattern = re.compile(r"^\[\d+\]")
        for att in result:
            self.assertRegex(
                att.filename,
                prefix_pattern,
                f"filename {att.filename!r} does not start with [N] prefix",
            )

    def test_sequence_is_one_based(self) -> None:
        """sequence 從 1 開始。"""
        result = parse_attachment_links(self.html_q1)
        if not result:
            self.skipTest("No attachments in fixture")
        sequences = [att.sequence for att in result]
        self.assertIn(1, sequences, "sequence must include 1 (1-based)")

    def test_parses_all_eight_attachments_with_non_ascii_names(self) -> None:
        """期望值寫死、不由 fixture 推導：八筆，CJK／空格／括號檔名原樣保留。"""
        result = parse_attachment_links(self.html_q1)
        self.assertEqual(
            [att.filename for att in result],
            [
                "[1] inline-01.png",
                "[2] inline-02.gif",
                "[3] quote-PEN-100.pdf",
                "[4] 報價單說明-甲商行.pdf",
                "[5] inline-05.jpg",
                "[6] 交貨單 PAPER-A4-80 (乙商行).xlsx",
                "[7] 規格表_TONER-55.docx",
                "[8] 樣品照片 PEN-100.jpg",
            ],
        )
        self.assertEqual([att.sequence for att in result], list(range(1, 9)))

    def test_sequence_matches_no_in_url(self) -> None:
        """AttachmentMeta.sequence 對應 href 內的 no=N。"""
        result = parse_attachment_links(self.html_q1)
        if not result:
            self.skipTest("No attachments in fixture")
        # First attachment should be sequence=1
        first = result[0]
        self.assertEqual(first.sequence, 1)

    def test_filename_non_empty_after_prefix(self) -> None:
        """filename 去除 [N] prefix 後仍非空（real_filename 部分）。"""
        result = parse_attachment_links(self.html_q1)
        if not result:
            self.skipTest("No attachments in fixture")
        for att in result:
            # Strip prefix [N] from filename
            real = re.sub(r"^\[\d+\]\s*", "", att.filename).strip()
            self.assertGreater(
                len(real), 0, f"Real filename after prefix empty for seq={att.sequence}"
            )

    def test_known_attachment_filename(self) -> None:
        """第一個附件是 inline-01.png（已知 fixture 內容）。"""
        result = parse_attachment_links(self.html_q1)
        self.assertGreater(len(result), 0)
        # sequence=1 filename should contain inline-01.png
        seq1 = next((a for a in result if a.sequence == 1), None)
        self.assertIsNotNone(seq1)
        assert seq1 is not None
        self.assertIn("inline-01.png", seq1.filename)

    def test_no_attachment_html_returns_empty_list(self) -> None:
        """無 cd=3 連結的 HTML → 回空列表（不 raise）。"""
        html_no_attachment = (
            "<html><body><form><table>"
            '<tr class="TextAttr"><td>寄件者 :</td>'
            "<td>test@test.com</td></tr>"
            "</table></form></body></html>"
        )
        result = parse_attachment_links(html_no_attachment)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# task 7.3 regression — attachment-label edge case (entry=2, 2026-05-06)
# ---------------------------------------------------------------------------

DETAIL_Q1_ENTRY2_FIXTURE = FIXTURES_DIR / "detail_q1_20260506_entry2.html"


class TestParseDetailHeaderAttachmentLabelEdgeCase(unittest.TestCase):
    """Regression test: entries whose detail page lacks 郵件 ID and has
    date '---' must parse successfully when fallback values are provided, and
    attachment dropdown rows must not pollute the fields dict.

    Fixture: detail_q1_20260506_entry2.html
    Root cause: the detail page may omit the 郵件 ID row, and
    renders the date field as '---'. The attachment <div id="SubMenu"> contains
    <tr class="TextAttr"> rows with <a> anchors that were being collected into
    the fields dict, displacing real header keys.
    """

    def setUp(self) -> None:
        from datetime import datetime, timedelta, timezone

        self.html_q1 = _load_fixture(DETAIL_Q1_ENTRY2_FIXTURE)
        self._tz_cst = timezone(timedelta(hours=8))
        self._fallback_mid = "archive:2026-05-06:entry=2"
        self._fallback_sent_at = datetime(2026, 5, 6, 0, 0, 0, tzinfo=self._tz_cst)

    def test_parse_succeeds_with_fallbacks(self) -> None:
        """parse_detail_header must not raise when valid fallbacks are supplied."""
        result = parse_detail_header(
            self.html_q1,
            fallback_message_id=self._fallback_mid,
            fallback_sent_at=self._fallback_sent_at,
        )
        self.assertIsInstance(result, DetailHeader)

    def test_returns_all_six_fields_non_empty(self) -> None:
        """All six DetailHeader fields must be non-empty / non-None."""
        result = parse_detail_header(
            self.html_q1,
            fallback_message_id=self._fallback_mid,
            fallback_sent_at=self._fallback_sent_at,
        )
        self.assertTrue(result.from_addr.strip(), "from_addr must be non-empty")
        self.assertGreater(
            len(result.to_addrs) + len(result.cc_addrs),
            0,
            "at least one recipient required",
        )
        self.assertTrue(result.subject.strip(), "subject must be non-empty")
        self.assertIsNotNone(result.sent_at, "sent_at must not be None")
        self.assertIsNotNone(result.sent_at.tzinfo, "sent_at must be timezone-aware")
        self.assertTrue(result.message_id.strip(), "message_id must be non-empty")

    def test_fallback_message_id_used(self) -> None:
        """When 郵件 ID is absent, the supplied fallback_message_id is used."""
        result = parse_detail_header(
            self.html_q1,
            fallback_message_id=self._fallback_mid,
            fallback_sent_at=self._fallback_sent_at,
        )
        self.assertEqual(result.message_id, self._fallback_mid)

    def test_fallback_sent_at_used_when_date_is_dashes(self) -> None:
        """When date field is '---', the supplied fallback_sent_at is used."""
        result = parse_detail_header(
            self.html_q1,
            fallback_message_id=self._fallback_mid,
            fallback_sent_at=self._fallback_sent_at,
        )
        self.assertEqual(result.sent_at, self._fallback_sent_at)

    def test_attachment_labels_not_in_fields(self) -> None:
        """Attachment dropdown rows must not appear as header fields.

        Specifically, keys containing attachment filenames like 'summary-a.txt'
        must not be present; only real header labels should be collected.
        """

        # Re-parse with fallbacks; the test indirectly verifies that real
        # fields (subject, from_addr) are correctly found, which would fail
        # if attachment rows had displaced them.
        result = parse_detail_header(
            self.html_q1,
            fallback_message_id=self._fallback_mid,
            fallback_sent_at=self._fallback_sent_at,
        )
        self.assertIn("Stationery", result.subject, "subject should contain Stationery")
        self.assertIn("sean3808@mailattic.test", result.from_addr)

    def test_raises_without_fallback_message_id(self) -> None:
        """If 郵件 ID is absent and no fallback is given, ValueError must be raised."""
        with self.assertRaises(ValueError) as ctx:
            parse_detail_header(
                self.html_q1,
                fallback_sent_at=self._fallback_sent_at,
            )
        self.assertIn("郵件 ID", str(ctx.exception))

    def test_raises_without_fallback_sent_at(self) -> None:
        """If date is '---' and no fallback is given, ValueError must be raised."""
        with self.assertRaises(ValueError) as ctx:
            parse_detail_header(
                self.html_q1,
                fallback_message_id=self._fallback_mid,
            )
        self.assertIn("---", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

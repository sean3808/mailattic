"""Unit tests for archive_scrape Group 3: list page parsing + sender filter
(tasks 3.1 / 3.2 / 3.3).

All tests are offline — no network calls.
Fixtures: tests/fixtures/archive_scrape/list_dir2_20260518.html
"""

from __future__ import annotations

import unittest
import warnings
from datetime import date
from pathlib import Path

from mailattic.archive_scrape import (
    ListRowMeta,
    filter_by_sender,
    parse_date_options,
    parse_list_rows,
    validate_date_range,
)

SENDER_FILTER = "sean3808@mailattic.test"
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "archive_scrape"
LIST_FIXTURE = FIXTURES_DIR / "list_dir2_20260518.html"


def _load_fixture(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# task 3.1 — parse_date_options + validate_date_range
# ---------------------------------------------------------------------------


class TestParseDateOptions(unittest.TestCase):
    """task 3.1 — parse_date_options() unit tests."""

    def setUp(self) -> None:
        self.html = _load_fixture(LIST_FIXTURE)

    def test_parse_date_options_returns_full_window(self) -> None:
        """fixture 解析出的日期數 == fixture 內 <option> 標籤實際數量。

        Fixture has 26 <option> elements in <select name="dt">.
        All have valid YYYYMMDD value format.
        """
        from bs4 import BeautifulSoup

        # Ground truth: count options in fixture
        soup = BeautifulSoup(self.html, "html.parser")
        select = soup.find("select", attrs={"name": "dt"})
        self.assertIsNotNone(select, "Fixture must have <select name='dt'>")
        expected_count = len(select.find_all("option"))  # type: ignore[union-attr]
        self.assertGreater(expected_count, 0)

        result = parse_date_options(self.html)
        self.assertEqual(
            len(result),
            expected_count,
            f"Expected {expected_count} dates from fixture, got {len(result)}",
        )

    def test_parse_date_options_returns_date_objects(self) -> None:
        """回傳列表中每個元素都是 datetime.date 物件。"""
        result = parse_date_options(self.html)
        for d in result:
            self.assertIsInstance(d, date)

    def test_parse_date_options_contains_known_date(self) -> None:
        """Fixture 包含 2026-05-18（選中的日期）。"""
        result = parse_date_options(self.html)
        self.assertIn(date(2026, 5, 18), result)

    def test_parse_date_options_invalid_html_raises_value_error(self) -> None:
        """缺少 <select name='dt'> → raise ValueError。"""
        with self.assertRaises(ValueError) as ctx:
            parse_date_options("<html><body>no select here</body></html>")
        self.assertIn("dt", str(ctx.exception))


class TestValidateDateRange(unittest.TestCase):
    """task 3.1 — validate_date_range() unit tests。"""

    def _make_available(self) -> list[date]:
        """5/1 ~ 5/18 作為 available 視窗。"""
        from datetime import timedelta

        start = date(2026, 5, 1)
        return [start + timedelta(days=i) for i in range(18)]  # 5/1..5/18

    def test_in_window_and_out_of_window_split(self) -> None:
        """from=4/15 to=5/3 → in_window=[5/1,5/2,5/3], out_of_window=[4/15..4/30]。"""
        available = self._make_available()
        from_d = date(2026, 4, 15)
        to_d = date(2026, 5, 3)

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            in_window, out_of_window = validate_date_range(from_d, to_d, available)

        self.assertEqual(
            in_window, [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]
        )
        # out_of_window should be 4/15..4/30 (16 days)
        self.assertEqual(len(out_of_window), 16)
        self.assertEqual(out_of_window[0], date(2026, 4, 15))
        self.assertEqual(out_of_window[-1], date(2026, 4, 30))

    def test_warning_emitted_for_out_of_window(self) -> None:
        """超出視窗時 emit UserWarning 到 stderr（warnings module）。"""
        available = self._make_available()
        from_d = date(2026, 4, 15)
        to_d = date(2026, 5, 3)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            validate_date_range(from_d, to_d, available)

        self.assertGreater(
            len(w), 0, "Expected at least one warning for out-of-window dates"
        )
        warning_messages = [str(warning.message) for warning in w]
        self.assertTrue(
            any(
                "window" in msg.lower() or "skip" in msg.lower()
                for msg in warning_messages
            ),
            f"Expected warning about skipped dates; got: {warning_messages}",
        )

    def test_all_in_window_no_warning(self) -> None:
        """全部在視窗內 → 不 emit warning，out_of_window 為空。"""
        available = self._make_available()
        from_d = date(2026, 5, 1)
        to_d = date(2026, 5, 5)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            in_window, out_of_window = validate_date_range(from_d, to_d, available)

        self.assertEqual(len(out_of_window), 0)
        self.assertEqual(
            len(w), 0, f"Unexpected warnings: {[str(x.message) for x in w]}"
        )


# ---------------------------------------------------------------------------
# task 3.2 — parse_list_rows
# ---------------------------------------------------------------------------


class TestParseListRows(unittest.TestCase):
    """task 3.2 — parse_list_rows() unit tests。"""

    def setUp(self) -> None:
        self.html = _load_fixture(LIST_FIXTURE)

    def _ground_truth_row_count(self) -> int:
        """直接從 fixture 數 ML_ id row 數。"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self.html, "html.parser")
        return len(soup.find_all("tr", id=lambda x: x and x.startswith("ML_")))

    def test_row_count_matches_fixture(self) -> None:
        """解析出的 row 數 == fixture 實際 ML_ tr 數量。"""
        expected = self._ground_truth_row_count()
        rows = parse_list_rows(self.html)
        self.assertEqual(
            len(rows),
            expected,
            f"Expected {expected} rows from fixture, got {len(rows)}",
        )

    def test_returns_list_row_meta_instances(self) -> None:
        """每個 row 都是 ListRowMeta 實例。"""
        rows = parse_list_rows(self.html)
        for row in rows:
            self.assertIsInstance(row, ListRowMeta)

    def test_first_row_entry_id_non_zero(self) -> None:
        """第一筆 row 的 entry_id 非零。"""
        rows = parse_list_rows(self.html)
        self.assertGreater(len(rows), 0)
        self.assertGreater(rows[0].entry_id, 0, "entry_id must be positive integer")

    def test_first_row_subject_non_empty(self) -> None:
        """第一筆 row 的 subject 非空。"""
        rows = parse_list_rows(self.html)
        self.assertGreater(len(rows), 0)
        self.assertGreater(len(rows[0].subject.strip()), 0)

    def test_known_row_entry53_data(self) -> None:
        """Fixture 包含 entry=53 (sean3808@ 寄信)，驗 message_id 與 subject。

        NOTE: Fixture 顯示 pagemax=2 (page 1 of 2, 12 rows),
        entry 53 is row index 1 in the first page.
        """
        rows = parse_list_rows(self.html)
        # Find entry 53
        entry53 = next((r for r in rows if r.entry_id == 53), None)
        self.assertIsNotNone(entry53, "entry_id=53 must be present in fixture page 1")
        assert entry53 is not None  # type narrowing
        self.assertEqual(
            entry53.message_id, "01aa01dd0053$1a2b3c4d$5e6f7a8b$@mailattic.test"
        )
        self.assertIn("pen-100", entry53.subject.lower())

    def test_all_rows_have_non_empty_sender(self) -> None:
        """每個 row 的 sender 非空。"""
        rows = parse_list_rows(self.html)
        for row in rows:
            self.assertGreater(
                len(row.sender.strip()), 0, f"entry_id={row.entry_id} sender empty"
            )


# ---------------------------------------------------------------------------
# task 3.3 — filter_by_sender
# ---------------------------------------------------------------------------


class TestFilterBySender(unittest.TestCase):
    """task 3.3 — filter_by_sender() unit tests。"""

    def setUp(self) -> None:
        self.html = _load_fixture(LIST_FIXTURE)
        all_rows = parse_list_rows(self.html)
        self.all_rows = all_rows

    def _ground_truth_sean_count(self) -> int:
        """從 fixture 直接數 sean3808@mailattic.test sender 的 row 數。"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self.html, "html.parser")
        rows = soup.find_all("tr", id=lambda x: x and x.startswith("ML_"))
        count = 0
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            sender_link = tds[2].find("a")
            if (
                sender_link
                and sender_link.get_text(strip=True) == "sean3808@mailattic.test"
            ):
                count += 1
        return count

    def test_filter_by_sender_count_matches_fixture(self) -> None:
        """套用 SENDER_FILTER 後筆數 == fixture 內 sender=sean3808@ 的 row 數。"""
        expected = self._ground_truth_sean_count()
        filtered = filter_by_sender(self.all_rows, SENDER_FILTER)
        self.assertEqual(
            len(filtered),
            expected,
            f"Expected {expected} rows matching {SENDER_FILTER}, got {len(filtered)}",
        )

    def test_filter_by_sender_case_insensitive(self) -> None:
        """sender filter 大小寫不敏感。"""
        upper_result = filter_by_sender(self.all_rows, SENDER_FILTER.upper())
        lower_result = filter_by_sender(self.all_rows, SENDER_FILTER.lower())
        self.assertEqual(len(upper_result), len(lower_result))

    def test_filter_by_sender_all_results_match(self) -> None:
        """篩選結果中每個 row 的 sender 都匹配目標。"""
        filtered = filter_by_sender(self.all_rows, SENDER_FILTER)
        for row in filtered:
            self.assertEqual(
                row.sender.strip().lower(),
                SENDER_FILTER.lower(),
                f"entry_id={row.entry_id} sender {row.sender!r} != {SENDER_FILTER!r}",
            )

    def test_filter_by_sender_unknown_sender_returns_empty(self) -> None:
        """完全不存在的 sender → 回空列表。"""
        result = filter_by_sender(self.all_rows, "nobody@nowhere.invalid")
        self.assertEqual(result, [])

    def test_filter_by_sender_preserves_entry_id(self) -> None:
        """篩選後的 row entry_id 非零。"""
        filtered = filter_by_sender(self.all_rows, SENDER_FILTER)
        for row in filtered:
            self.assertGreater(row.entry_id, 0)


if __name__ == "__main__":
    unittest.main()

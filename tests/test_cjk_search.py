"""Tests for CJK FTS5 segmentation: Tasks 1.1 / 2.1 / 3.1 / 3.2 / 5.1.

Coverage map
============
Task 1.1 — unit tests for segment_cjk():
    TestCjkSegmentation.*

Task 2.1 — storage.py FTS index applies segmentation:
    TestStorageSegmentation.*

Task 3.1 + 3.2 — query.py keyword path: _build_fts_match_expr + integration:
    TestBuildFtsMatchExpr.*
    TestCjkSearchIntegration.*

Task 5.1 — search results snippet/preview from original text:
    TestSnippetNotSegmented.*
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import mailattic.db_maintenance as db_maintenance_module
from mailattic.cjk_segmentation import segment_cjk
from mailattic.db_maintenance import reindex_fts
from mailattic.models import PstSource, build_normalized_message
from mailattic.query import (
    SearchFilters,
    _build_fts_match_expr,
    search_messages,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

_SOURCE = PstSource(
    path=Path("C:/mail/fixture.pst"),
    input_mode="file",
    requested_from=Path("C:/mail/fixture.pst"),
)


def _make_db(tmp: str) -> Path:
    return initialize_database(Path(tmp) / "test.db")


def _insert(db: Path, **kwargs: object) -> int:
    """Insert a single message and return its stored_message_id."""
    msg = build_normalized_message(
        {
            "subject": kwargs.get("subject", "Test subject"),
            "sender": {"email": kwargs.get("sender_email", "sender@example.com")},
            "to": [{"email": kwargs.get("to_email", "to@example.com")}],
            "sent_at": kwargs.get("sent_at", "2024-01-01T00:00:00"),
            "body_text": kwargs.get("body_text", "body"),
            "message_id": kwargs.get("message_id", "<test@example.com>"),
        },
        source=_SOURCE,
        folder_path="Inbox",
        logical_locator=str(kwargs.get("logical_locator", "Inbox/1")),
    )
    conn = connect_database(db)
    try:
        result = persist_message(conn, msg)
        conn.commit()
    finally:
        conn.close()
    return result.stored_message_id


# ===========================================================================
# Task 1.1 — unit tests for segment_cjk()
# ===========================================================================


class TestCjkSegmentation(unittest.TestCase):
    """Six required categories: pure CJK, mixed, email, domain, empty, single."""

    # --- Pure CJK ---
    def test_pure_cjk_inserts_spaces_between_chars(self) -> None:
        self.assertEqual(segment_cjk("甲乙發票"), "甲 乙 發 票")

    def test_pure_cjk_two_chars(self) -> None:
        self.assertEqual(segment_cjk("發票"), "發 票")

    def test_pure_cjk_single_char_unchanged(self) -> None:
        # Single CJK char: no adjacent pair, no space inserted.
        self.assertEqual(segment_cjk("票"), "票")

    def test_subject_papyra_mixed(self) -> None:
        # Mixed ASCII brand + CJK + punctuation subject.
        result = segment_cjk("PAPYRA: 甲乙發票&對帳單")
        # CJK chars only get spaces between adjacent CJK pairs.
        # Non-CJK chars (:, space, &) are passed through unchanged.
        self.assertEqual(result, "PAPYRA: 甲 乙 發 票&對 帳 單")

    # --- Mixed CJK / Latin ---
    def test_mixed_latin_and_cjk(self) -> None:
        # CJK chars adjacent to ASCII alphanumeric get separated by spaces,
        # so each CJK ideograph becomes an independent FTS token.
        result = segment_cjk("ABC甲乙")
        self.assertEqual(result, "ABC 甲 乙")

    def test_mixed_cjk_then_latin(self) -> None:
        result = segment_cjk("甲乙ABC")
        self.assertEqual(result, "甲 乙 ABC")

    def test_mixed_cjk_latin_interleaved(self) -> None:
        # 對 and 帳單 separated by '&' (non-CJK).
        result = segment_cjk("對帳&帳單")
        self.assertEqual(result, "對 帳&帳 單")

    # --- Email ---
    def test_email_unchanged(self) -> None:
        self.assertEqual(segment_cjk("a@b.com"), "a@b.com")

    def test_email_with_dots_unchanged(self) -> None:
        self.assertEqual(segment_cjk("alice.bob@example.com"), "alice.bob@example.com")

    # --- Domain name ---
    def test_domain_unchanged(self) -> None:
        self.assertEqual(segment_cjk("mailattic.test"), "mailattic.test")

    def test_domain_with_subdomain_unchanged(self) -> None:
        self.assertEqual(segment_cjk("mail.mailattic.test"), "mail.mailattic.test")

    # --- Empty string ---
    def test_empty_string_returns_empty(self) -> None:
        self.assertEqual(segment_cjk(""), "")

    def test_none_returns_empty(self) -> None:
        self.assertEqual(segment_cjk(None), "")

    def test_whitespace_only_unchanged(self) -> None:
        self.assertEqual(segment_cjk("   "), "   ")

    # --- Single CJK character ---
    def test_single_cjk_char_unchanged(self) -> None:
        self.assertEqual(segment_cjk("嶽"), "嶽")

    # --- CJK boundary: only CJK↔CJK gets space, not CJK↔non-CJK ---
    def test_cjk_non_cjk_boundary_no_space(self) -> None:
        # The '&' between 票 and 對 should not cause extra spaces.
        result = segment_cjk("發票&對帳單")
        # CJK pairs: 發↔票, 對↔帳, 帳↔單
        self.assertEqual(result, "發 票&對 帳 單")

    def test_number_between_cjk_gets_separated(self) -> None:
        result = segment_cjk("第2號")
        # 第/號 each border the ASCII digit 2; the new rule inserts spaces at
        # every alphanumeric↔CJK boundary, so 第, 2, and 號 each become
        # independent FTS tokens — sub-word queries can reach all three.
        self.assertEqual(result, "第 2 號")

    # --- ASCII alphanumeric runs untouched ---
    def test_ascii_alnum_unchanged(self) -> None:
        self.assertEqual(segment_cjk("PAPYRA2026"), "PAPYRA2026")

    def test_pure_ascii_sentence_unchanged(self) -> None:
        self.assertEqual(segment_cjk("Hello world"), "Hello world")


# ===========================================================================
# Task 2.1 — storage: FTS index content is segmented; messages original is not
# ===========================================================================


class TestStorageSegmentation(unittest.TestCase):
    def _fts_row(self, db: Path, rowid: int) -> dict[str, str | None]:
        conn = connect_database(db, create=False)
        try:
            row = conn.execute(
                "SELECT subject, sender, recipients, body_text, attachment_names "
                "FROM message_fts WHERE rowid = ?",
                [rowid],
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise KeyError(rowid)
        return dict(row)

    def _messages_row(self, db: Path, rowid: int) -> dict[str, str | None]:
        conn = connect_database(db, create=False)
        try:
            row = conn.execute(
                "SELECT subject, body_text, recipient_search_text,"
                " attachment_search_text FROM messages WHERE id = ?",
                [rowid],
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise KeyError(rowid)
        return dict(row)

    def test_fts_subject_is_segmented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            mid = _insert(
                db,
                subject="甲乙發票",
                body_text="body",
                message_id="<seg-subj@example.com>",
                logical_locator="Inbox/s1",
            )
            fts = self._fts_row(db, mid)
            # FTS subject must have spaces between CJK chars
            self.assertEqual(fts["subject"], "甲 乙 發 票")

    def test_messages_subject_is_not_segmented(self) -> None:
        """Original messages table must retain unsegmented text."""
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            mid = _insert(
                db,
                subject="甲乙發票",
                body_text="body",
                message_id="<orig-subj@example.com>",
                logical_locator="Inbox/s2",
            )
            orig = self._messages_row(db, mid)
            self.assertEqual(orig["subject"], "甲乙發票")

    def test_fts_body_text_is_segmented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            mid = _insert(
                db,
                subject="Subject",
                body_text="PAPYRA: 甲乙發票&對帳單",
                message_id="<seg-body@example.com>",
                logical_locator="Inbox/s3",
            )
            fts = self._fts_row(db, mid)
            self.assertIn("甲 乙 發 票", fts["body_text"])
            self.assertIn("對 帳 單", fts["body_text"])

    def test_messages_body_text_is_not_segmented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            mid = _insert(
                db,
                subject="Subject",
                body_text="PAPYRA: 甲乙發票&對帳單",
                message_id="<orig-body@example.com>",
                logical_locator="Inbox/s4",
            )
            orig = self._messages_row(db, mid)
            self.assertEqual(orig["body_text"], "PAPYRA: 甲乙發票&對帳單")

    def test_fts_sender_is_NOT_segmented(self) -> None:
        """sender FTS column must NOT be segmented (task 2.1 contract)."""
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            mid = _insert(
                db,
                subject="Subject",
                body_text="body",
                sender_email="sender@example.com",
                message_id="<sender-noseg@example.com>",
                logical_locator="Inbox/s5",
            )
            fts = self._fts_row(db, mid)
            # sender should be the raw search_text, not segmented
            # (no CJK in this test, but the column must remain unchanged)
            self.assertIn("sender@example.com", fts["sender"])


# ===========================================================================
# Task 3.1 + 3.2 — _build_fts_match_expr unit tests
# ===========================================================================


class TestBuildFtsMatchExpr(unittest.TestCase):
    def test_empty_keyword_returns_none(self) -> None:
        self.assertIsNone(_build_fts_match_expr(""))

    def test_whitespace_only_returns_none(self) -> None:
        self.assertIsNone(_build_fts_match_expr("   "))

    def test_pure_cjk_becomes_phrase(self) -> None:
        # 發票 → "發 票"
        result = _build_fts_match_expr("發票")
        self.assertEqual(result, '"發 票"')

    def test_pure_cjk_three_chars(self) -> None:
        result = _build_fts_match_expr("對帳單")
        self.assertEqual(result, '"對 帳 單"')

    def test_ascii_alnum_becomes_quoted_bareword(self) -> None:
        result = _build_fts_match_expr("PAPYRA")
        self.assertEqual(result, '"PAPYRA"')

    def test_mixed_latin_cjk_produces_two_phrases(self) -> None:
        # "PAPYRA: 甲乙發票&對帳單" → "PAPYRA" "甲 乙 發 票" "對 帳 單"
        result = _build_fts_match_expr("PAPYRA: 甲乙發票&對帳單")
        self.assertEqual(result, '"PAPYRA" "甲 乙 發 票" "對 帳 單"')

    def test_fts5_special_chars_only_returns_impossible_sentinel(self) -> None:
        # ":" → zero runs → impossible sentinel
        result = _build_fts_match_expr(":")
        self.assertIsNotNone(result)
        # Must not be None (not treated as empty), and must be the sentinel
        from mailattic.query import _FTS_MATCH_IMPOSSIBLE

        self.assertEqual(result, _FTS_MATCH_IMPOSSIBLE)

    def test_ampersand_only_returns_impossible_sentinel(self) -> None:
        from mailattic.query import _FTS_MATCH_IMPOSSIBLE

        result = _build_fts_match_expr("&")
        self.assertEqual(result, _FTS_MATCH_IMPOSSIBLE)

    def test_mixed_punctuation_only_returns_impossible_sentinel(self) -> None:
        from mailattic.query import _FTS_MATCH_IMPOSSIBLE

        result = _build_fts_match_expr(":&()")
        self.assertEqual(result, _FTS_MATCH_IMPOSSIBLE)

    def test_papyra_colon_space_cjk(self) -> None:
        # "PAPYRA: 甲乙發票" → "PAPYRA" "甲 乙 發 票"
        result = _build_fts_match_expr("PAPYRA: 甲乙發票")
        self.assertEqual(result, '"PAPYRA" "甲 乙 發 票"')

    def test_invoice_ampersand_payment(self) -> None:
        # "發票&帳款" → "發 票" "帳 款"
        result = _build_fts_match_expr("發票&帳款")
        self.assertEqual(result, '"發 票" "帳 款"')

    def test_boolean_operator_contracts(self) -> None:
        cases = [
            (
                '"PEN-591" OR "INKWELL BLACK GEL PEN"',
                '"PEN-591" OR "INKWELL BLACK GEL PEN"',
            ),
            ('"發票" OR "對帳單"', '"發 票" OR "對 帳 單"'),
            (
                'PAPYRA AND (PEN-591 OR "INKWELL BLACK GEL PEN")',
                '"PAPYRA" AND ("PEN" "591" OR "INKWELL BLACK GEL PEN")',
            ),
            ("or and", '"or" "and"'),
            ("PAPYRA NOT 發票", '"PAPYRA" NOT "發 票"'),
            ("發票&帳款", '"發 票" "帳 款"'),
        ]
        for keyword, expected in cases:
            with self.subTest(keyword=keyword):
                self.assertEqual(_build_fts_match_expr(keyword), expected)

    def test_literal_double_quote_escaped(self) -> None:
        """A lone quote now opens a phrase and leaves invalid FTS5 syntax.

        Boolean-aware parsing treats " as phrase syntax instead of discarding
        every special character, so search must surface the FTS5 error.
        """
        result = _build_fts_match_expr('say"hi')
        self.assertEqual(result, '"say" "hi')

    def test_term_bearing_unmatched_grouping_is_left_invalid(self) -> None:
        result = _build_fts_match_expr("PAPYRA (")
        self.assertEqual(result, '"PAPYRA" (')

    def test_number_only(self) -> None:
        result = _build_fts_match_expr("2026")
        self.assertEqual(result, '"2026"')


# ===========================================================================
# Task 3.1 integration — CJK substring keywords hit the right rows
# ===========================================================================


class TestCjkSearchIntegration(unittest.TestCase):
    """Integration tests: insert a message with CJK content, verify search hits."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(self._tmp.name)
        # Insert the canonical test message: subject "PAPYRA: 甲乙發票&對帳單"
        self.msg_id = _insert(
            self.db,
            subject="PAPYRA: 甲乙發票&對帳單",
            body_text="請見附件發票與對帳單，甲乙帳款如下。",
            sender_email="vendor@papyra.example",
            to_email="sean@example.com",
            message_id="<papyra-invoice@example.com>",
            logical_locator="Inbox/papyra1",
        )
        # Insert a decoy message with no CJK content
        _insert(
            self.db,
            subject="Plain English subject",
            body_text="No Chinese content here.",
            sender_email="english@example.com",
            to_email="sean@example.com",
            message_id="<english@example.com>",
            logical_locator="Inbox/en1",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _search(self, keyword: str) -> list[dict[str, object]]:
        return search_messages(
            database_path=self.db,
            filters=SearchFilters(keyword=keyword),
            limit=20,
        )

    def _hits_target(self, keyword: str) -> bool:
        results = self._search(keyword)
        return any(r["id"] == self.msg_id for r in results)

    def test_keyword_fapiao_hits(self) -> None:
        """'發票' (substring of 甲乙發票) must hit."""
        self.assertTrue(self._hits_target("發票"))

    def test_keyword_prefix_hits(self) -> None:
        """'甲乙' (prefix of 甲乙發票) must hit."""
        self.assertTrue(self._hits_target("甲乙"))

    def test_keyword_zhangdan_hits(self) -> None:
        """'帳單' (suffix of 對帳單) must hit."""
        self.assertTrue(self._hits_target("帳單"))

    def test_keyword_duizhangdan_hits(self) -> None:
        """'對帳單' (full CJK run in subject) must hit."""
        self.assertTrue(self._hits_target("對帳單"))

    def test_keyword_papyra_latin_hits(self) -> None:
        """'PAPYRA' (ASCII in subject) must hit."""
        self.assertTrue(self._hits_target("PAPYRA"))

    def test_keyword_papyra_mixed_hits(self) -> None:
        """'PAPYRA: 甲乙發票' (mixed Latin+CJK, special char) must not error and hit."""
        self.assertTrue(self._hits_target("PAPYRA: 甲乙發票"))

    def test_keyword_fapiao_ampersand_zhangkuan_no_error(self) -> None:
        """'發票&帳款' must not raise an error, and must hit the fixture message.

        _build_fts_match_expr splits on '&' to produce '"發 票" "帳 款"' (implicit
        AND).  The fixture body "請見附件發票與對帳單，甲乙帳款如下。" segments to
        "… 發 票 … 帳 款 …", so both phrases are present in the same document
        and FTS5 returns exactly one hit.
        """
        try:
            results = self._search("發票&帳款")
        except Exception as exc:
            self.fail(f"search raised unexpectedly: {exc}")
        # Both "發 票" and "帳 款" exist in the fixture body → definite hit.
        self.assertTrue(
            any(r["id"] == self.msg_id for r in results),
            "Expected fixture message to be hit by '發票&帳款' query",
        )

    def test_punctuation_only_returns_zero_rows(self) -> None:
        """':' keyword must return 0 rows, not fall back to unconditional search."""
        results = self._search(":")
        self.assertEqual(len(results), 0)

    def test_ampersand_only_returns_zero_rows(self) -> None:
        results = self._search("&")
        self.assertEqual(len(results), 0)

    def test_grouping_punctuation_only_returns_zero_rows(self) -> None:
        results = self._search(":&()")
        self.assertEqual(len(results), 0)

    def test_unbalanced_grouping_with_term_raises_friendly_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid FTS keyword query"):
            self._search("PAPYRA (")

    def test_empty_keyword_applies_no_filter(self) -> None:
        """Empty keyword must not filter; should return all rows."""
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(keyword=""),
            limit=20,
        )
        # Both messages should be returned
        self.assertGreaterEqual(len(results), 2)

    def test_decoy_not_hit_by_cjk_keyword(self) -> None:
        """The English-only decoy must not be returned for a CJK keyword."""
        results = self._search("發票")
        decoy_ids = [
            r["id"] for r in results if r["subject"] == "Plain English subject"
        ]
        self.assertEqual(decoy_ids, [])


# ===========================================================================
# Task 5.1 — snippet/preview comes from original text (no segmentation leak)
# ===========================================================================


class TestSnippetNotSegmented(unittest.TestCase):
    """Verify that search result snippet field contains no inserted CJK spaces."""

    def test_snippet_has_no_injected_cjk_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            _insert(
                db,
                subject="PAPYRA: 甲乙發票&對帳單",
                body_text="請見附件發票與對帳單，甲乙帳款如下。",
                sender_email="vendor@papyra.example",
                to_email="sean@example.com",
                message_id="<snippet-test@example.com>",
                logical_locator="Inbox/snip1",
            )

            results = search_messages(
                database_path=db,
                filters=SearchFilters(keyword="發票"),
                limit=5,
            )
            self.assertEqual(len(results), 1)
            result = results[0]

            snippet = result.get("snippet", "")
            subject = result.get("subject", "")

            # The snippet must not contain the inserted segmentation space
            # pattern: two CJK chars with a single space between them where
            # the original had no space.
            # Original body: "請見附件發票與對帳單，甲乙帳款如下。"
            # If snippet leaked segmentation, we'd see "發 票" / "對 帳 單" etc.
            self.assertNotIn("發 票", snippet)
            self.assertNotIn("對 帳 單", snippet)
            self.assertNotIn("甲 乙", snippet)

            # Subject must also be the original (taken from messages table)
            self.assertEqual(subject, "PAPYRA: 甲乙發票&對帳單")

    def test_snippet_preserves_original_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            body = "請見附件發票與對帳單，甲乙帳款如下。"
            _insert(
                db,
                subject="Subject",
                body_text=body,
                sender_email="test@example.com",
                to_email="to@example.com",
                message_id="<snip2@example.com>",
                logical_locator="Inbox/snip2",
            )

            results = search_messages(
                database_path=db,
                filters=SearchFilters(keyword="帳單"),
                limit=5,
            )
            self.assertEqual(len(results), 1)
            snippet = results[0].get("snippet", "")
            # Snippet is a substr of the original body (no segmentation)
            # The original chars should appear intact
            self.assertIn("發票", snippet)
            self.assertIn("對帳單", snippet)


# ===========================================================================
# Task 4.2 — reindex_fts: idempotent + failure-injection rollback safety
# ===========================================================================


class TestReindexFts(unittest.TestCase):
    """Task 4.2: reindex_fts() idempotency and rollback-safety on mid-flight failure."""

    def _fts_count(self, db: Path) -> int:
        conn = connect_database(db, create=False)
        try:
            return conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0]
        finally:
            conn.close()

    def _messages_count(self, db: Path) -> int:
        conn = connect_database(db, create=False)
        try:
            return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()

    def _fts_subjects(self, db: Path) -> list[str]:
        conn = connect_database(db, create=False)
        try:
            rows = conn.execute(
                "SELECT subject FROM message_fts ORDER BY rowid"
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def test_reindex_fts_count_equals_messages_count(self) -> None:
        """After reindex, message_fts row count == messages row count."""
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            _insert(
                db,
                subject="PAPYRA: 甲乙發票",
                body_text="甲乙帳款",
                message_id="<reindex-a1@example.com>",
                logical_locator="Inbox/r1",
            )
            _insert(
                db,
                subject="Plain subject",
                body_text="No CJK here",
                message_id="<reindex-a2@example.com>",
                logical_locator="Inbox/r2",
            )
            result = reindex_fts(db)
            self.assertEqual(result.messages_count, 2)
            self.assertEqual(result.fts_count, 2)
            self.assertEqual(self._fts_count(db), 2)

    def test_reindex_fts_is_idempotent(self) -> None:
        """Running reindex twice produces the same row count and content."""
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            _insert(
                db,
                subject="發票對帳",
                body_text="請見附件",
                message_id="<reindex-b1@example.com>",
                logical_locator="Inbox/r3",
            )
            result1 = reindex_fts(db)
            subjects_after_first = self._fts_subjects(db)

            result2 = reindex_fts(db)
            subjects_after_second = self._fts_subjects(db)

            self.assertEqual(result1.fts_count, result2.fts_count)
            self.assertEqual(subjects_after_first, subjects_after_second)

    def test_reindex_fts_failure_preserves_existing_index(self) -> None:
        """If reindex fails mid-flight, the pre-existing index is preserved intact.

        Scenario:
        1. Build a complete index via normal reindex_fts().
        2. Monkeypatch segment_cjk to raise RuntimeError on its 2nd call,
           simulating a failure after DELETE but during repopulate.
        3. Assert reindex_fts() raises.
        4. Assert message_fts still has the same row count and content as before
           the failed attempt (rollback preserved the previous complete index).
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            _insert(
                db,
                subject="甲乙發票",
                body_text="帳款如下",
                message_id="<rollback-c1@example.com>",
                logical_locator="Inbox/rb1",
            )
            _insert(
                db,
                subject="Plain subject",
                body_text="No CJK",
                message_id="<rollback-c2@example.com>",
                logical_locator="Inbox/rb2",
            )

            # Step 1: normal reindex to build initial complete index
            reindex_fts(db)
            count_before = self._fts_count(db)
            subjects_before = self._fts_subjects(db)
            self.assertEqual(count_before, 2)

            # Step 2: inject failure — segment_cjk raises on 2nd call
            # (after DELETE has already cleared the FTS table)
            original_segment_cjk = db_maintenance_module.segment_cjk
            call_count = {"n": 0}

            def failing_segment_cjk(text: object) -> str:
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    raise RuntimeError("Injected failure during repopulate")
                return original_segment_cjk(text)

            db_maintenance_module.segment_cjk = failing_segment_cjk
            try:
                # Step 3: failed reindex must raise
                with self.assertRaises((RuntimeError, Exception)):
                    reindex_fts(db)
            finally:
                db_maintenance_module.segment_cjk = original_segment_cjk

            # Step 4: index must be identical to pre-failure state
            count_after = self._fts_count(db)
            subjects_after = self._fts_subjects(db)
            self.assertEqual(
                count_after,
                count_before,
                msg=(
                    f"FTS row count changed after rollback: "
                    f"before={count_before}, after={count_after}"
                ),
            )
            self.assertEqual(
                subjects_after,
                subjects_before,
                msg="FTS content changed after rollback — previous index not preserved",
            )


# ===========================================================================
# CJK-glued-to-ASCII recall — new boundary rule integration tests
# ===========================================================================


class TestCjkGluedAsciiRecall(unittest.TestCase):
    """End-to-end recall tests for the new CJK↔ASCII alphanumeric boundary rule.

    When a CJK ideograph is glued to an ASCII digit or letter (e.g. "2026年度",
    "PDF檔案", "第2號"), the segmenter now inserts spaces at every
    alphanumeric↔CJK boundary.  This class verifies that the resulting FTS
    tokens allow sub-word CJK queries that would have been missed before the fix.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(self._tmp.name)

        # Message 1: year glued to CJK
        self.year_cjk_id = _insert(
            self.db,
            subject="2026年度報告",
            body_text="本年度報告摘要如下。",
            message_id="<year-cjk@example.com>",
            logical_locator="Inbox/yc1",
        )

        # Message 2: ASCII prefix glued to CJK
        self.pdf_cjk_id = _insert(
            self.db,
            subject="PDF檔案說明",
            body_text="請下載 PDF 檔案。",
            message_id="<pdf-cjk@example.com>",
            logical_locator="Inbox/pc1",
        )

        # Message 3: CJK sandwiching a digit
        self.num_cjk_id = _insert(
            self.db,
            subject="第2號公告",
            body_text="依第2號規定辦理。",
            message_id="<num-cjk@example.com>",
            logical_locator="Inbox/nc1",
        )

        # Decoy: pure ASCII, must never hit CJK-only queries
        self.decoy_id = _insert(
            self.db,
            subject="Unrelated English message",
            body_text="Nothing Chinese here.",
            message_id="<decoy-glued@example.com>",
            logical_locator="Inbox/dg1",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _hits(self, keyword: str) -> set[int]:
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(keyword=keyword),
            limit=20,
        )
        return {r["id"] for r in results}

    # --- 2026年度 ---
    def test_year_cjk_keyword_nian_hits(self) -> None:
        """Single CJK char '年' must hit message with '2026年度'."""
        self.assertIn(self.year_cjk_id, self._hits("年"))

    def test_year_cjk_keyword_niandu_hits(self) -> None:
        """CJK bigram '年度' must hit message with '2026年度'."""
        self.assertIn(self.year_cjk_id, self._hits("年度"))

    def test_year_cjk_keyword_2026_hits(self) -> None:
        """ASCII '2026' glued to CJK must still be findable after segmentation."""
        self.assertIn(self.year_cjk_id, self._hits("2026"))

    # --- PDF檔案 ---
    def test_pdf_cjk_keyword_dang_hits(self) -> None:
        """Single CJK char '檔' must hit message with 'PDF檔案'."""
        self.assertIn(self.pdf_cjk_id, self._hits("檔"))

    def test_pdf_cjk_keyword_dangán_hits(self) -> None:
        """CJK bigram '檔案' must hit message with 'PDF檔案'."""
        self.assertIn(self.pdf_cjk_id, self._hits("檔案"))

    # --- 第2號 ---
    def test_num_cjk_keyword_hao_hits(self) -> None:
        """Single CJK char '號' must hit message with '第2號'."""
        self.assertIn(self.num_cjk_id, self._hits("號"))

    def test_num_cjk_keyword_di2hao_hits(self) -> None:
        """Full run '第2號' as keyword must hit message with '第2號'."""
        self.assertIn(self.num_cjk_id, self._hits("第2號"))

    # --- Negative: unknown CJK sub-word must not match ---
    def test_nonexistent_cjk_keyword_does_not_hit(self) -> None:
        """A CJK keyword not present in any fixture must return 0 results."""
        hits = self._hits("龍鳳")
        self.assertEqual(len(hits), 0, "Unexpected hit for absent CJK keyword '龍鳳'")


# ===========================================================================
# Task 6.1 — regression: non-CJK behaviour unchanged after CJK segmentation
# ===========================================================================


def _insert_with_attachments(
    db: Path, attachment_names: list[str], **kwargs: object
) -> int:
    """Insert a message with attachment names and return its stored_message_id."""
    msg = build_normalized_message(
        {
            "subject": kwargs.get("subject", "Test subject"),
            "sender": {"email": kwargs.get("sender_email", "sender@example.com")},
            "to": [{"email": kwargs.get("to_email", "to@example.com")}],
            "sent_at": kwargs.get("sent_at", "2024-01-01T00:00:00"),
            "body_text": kwargs.get("body_text", "body"),
            "message_id": kwargs.get("message_id", "<test@example.com>"),
            "attachment_names": attachment_names,
        },
        source=_SOURCE,
        folder_path="Inbox",
        logical_locator=str(kwargs.get("logical_locator", "Inbox/1")),
    )
    conn = connect_database(db)
    try:
        result = persist_message(conn, msg)
        conn.commit()
    finally:
        conn.close()
    return result.stored_message_id


class TestAsciiKeywordRegression(unittest.TestCase):
    """Task 6.1 — scenario "Plain ASCII alphanumeric keyword behavior is unchanged".

    Verifies that introducing CJK segmentation does NOT degrade pure-ASCII
    keyword search: messages with ASCII/numeric content in subject/body are still
    found by matching keywords and still NOT found for unrelated keywords.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(self._tmp.name)

        # Message A: contains "Maple Desk" in subject + body
        self.maple_desk_id = _insert(
            self.db,
            subject="Maple Desk supply quotation",
            body_text="Please find the Maple Desk quote attached. Model DSK-2026.",
            sender_email="vendor@mapledesk-hq.example",
            to_email="sean@example.com",
            message_id="<maple-desk-1@example.com>",
            logical_locator="Inbox/md1",
        )

        # Message B: contains "PAPYRA" in subject (ASCII brand name)
        self.papyra_id = _insert(
            self.db,
            subject="PAPYRA stationery order",
            body_text="Order confirmed for PAPYRA PEN-100 products.",
            sender_email="orders@papyra.example",
            to_email="sean@example.com",
            message_id="<papyra-order@example.com>",
            logical_locator="Inbox/papyra1",
        )

        # Message C: contains year "2026" in body
        self.year_id = _insert(
            self.db,
            subject="Annual review schedule",
            body_text="The annual review will be held in 2026 Q2.",
            sender_email="hr@company.com",
            to_email="sean@example.com",
            message_id="<annual-2026@example.com>",
            logical_locator="Inbox/yr1",
        )

        # Decoy: no ASCII keyword overlap with A/B/C
        self.decoy_id = _insert(
            self.db,
            subject="Unrelated message",
            body_text="Nothing relevant here.",
            sender_email="other@example.com",
            to_email="sean@example.com",
            message_id="<decoy@example.com>",
            logical_locator="Inbox/decoy1",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _search(self, keyword: str) -> list[dict[str, object]]:
        return search_messages(
            database_path=self.db,
            filters=SearchFilters(keyword=keyword),
            limit=20,
        )

    def _ids(self, keyword: str) -> set[int]:
        return {r["id"] for r in self._search(keyword)}

    # --- "Maple" hits message A, not decoy ---
    def test_ascii_keyword_maple_hits_target(self) -> None:
        """keyword='Maple' must hit the Maple Desk message."""
        self.assertIn(self.maple_desk_id, self._ids("Maple"))

    def test_ascii_keyword_maple_does_not_hit_decoy(self) -> None:
        """keyword='Maple' must NOT hit the unrelated decoy message."""
        self.assertNotIn(self.decoy_id, self._ids("Maple"))

    # --- "PAPYRA" hits message B, not decoy ---
    def test_ascii_keyword_papyra_hits_target(self) -> None:
        """keyword='PAPYRA' must hit the PAPYRA message."""
        self.assertIn(self.papyra_id, self._ids("PAPYRA"))

    def test_ascii_keyword_papyra_does_not_hit_decoy(self) -> None:
        """keyword='PAPYRA' must NOT hit the unrelated decoy message."""
        self.assertNotIn(self.decoy_id, self._ids("PAPYRA"))

    # --- "2026" hits message C, not decoy ---
    def test_numeric_keyword_hits_target(self) -> None:
        """keyword='2026' must hit the message containing '2026'."""
        self.assertIn(self.year_id, self._ids("2026"))

    def test_numeric_keyword_does_not_hit_decoy(self) -> None:
        """keyword='2026' must NOT hit the unrelated decoy message."""
        self.assertNotIn(self.decoy_id, self._ids("2026"))

    # --- "Desk" hits message A (via body) ---
    def test_ascii_keyword_desk_hits_maple_desk_message(self) -> None:
        """keyword='Desk' must hit the message with 'Maple Desk' in subject/body."""
        self.assertIn(self.maple_desk_id, self._ids("Desk"))

    # --- Verify decoy only appears for empty-keyword (baseline check) ---
    def test_empty_keyword_returns_all_messages(self) -> None:
        """Empty keyword must return all 4 inserted messages (no filter applied)."""
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(keyword=""),
            limit=20,
        )
        ids = {r["id"] for r in results}
        self.assertGreaterEqual(len(ids), 4)
        self.assertIn(self.decoy_id, ids)


class TestStructuredFilterRegression(unittest.TestCase):
    """Task 6.1 — structured field filters are unaffected by keyword segmentation.

    Verifies that sender / recipient / sender_domain / recipient_domain /
    attachment_name structured filters return correct results independently of
    the CJK FTS keyword MATCH builder path.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _make_db(self._tmp.name)

        # Message A: sender is vendor@mapledesk-hq.example, has a CJK subject
        self.msg_a_id = _insert(
            self.db,
            subject="甲乙發票 Maple Desk",  # CJK + ASCII in subject
            body_text="See attached invoice.",
            sender_email="vendor@mapledesk-hq.example",
            to_email="sean@example.com",
            message_id="<struct-a@example.com>",
            logical_locator="Inbox/st1",
        )

        # Message B: recipient is client@papyra.example, has CJK body
        self.msg_b_id = _insert(
            self.db,
            subject="Order confirmation",
            body_text="請見附件確認單。",  # CJK body
            sender_email="orders@supplier.com",
            to_email="client@papyra.example",
            message_id="<struct-b@example.com>",
            logical_locator="Inbox/sb1",
        )

        # Message C: sender_domain = acme.com, has attachment "invoice_Q1.pdf"
        self.msg_c_id = _insert_with_attachments(
            self.db,
            attachment_names=["invoice_Q1.pdf"],
            subject="Invoice Q1",
            body_text="Please find invoice Q1 attached.",
            sender_email="billing@acme.com",
            to_email="sean@example.com",
            message_id="<struct-c@example.com>",
            logical_locator="Inbox/sc1",
        )

        # Message D: recipient_domain = partner.org
        self.msg_d_id = _insert(
            self.db,
            subject="Partnership update",
            body_text="Please review the attached update.",
            sender_email="sean@example.com",
            to_email="contact@partner.org",
            message_id="<struct-d@example.com>",
            logical_locator="Inbox/sd1",
        )

        # Decoy: sender is nobody@random.net, no special attachments
        self.decoy_id = _insert(
            self.db,
            subject="Unrelated",
            body_text="Nothing to see.",
            sender_email="nobody@random.net",
            to_email="sean@example.com",
            message_id="<struct-decoy@example.com>",
            logical_locator="Inbox/sdecoy1",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sender_filter_hits_correct_message(self) -> None:
        """sender filter 'vendor@mapledesk-hq.example' must hit message A only."""
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(sender="vendor@mapledesk-hq.example"),
            limit=20,
        )
        ids = {r["id"] for r in results}
        self.assertIn(self.msg_a_id, ids)
        self.assertNotIn(self.decoy_id, ids)

    def test_sender_filter_does_not_hit_decoy(self) -> None:
        """sender filter 'vendor@mapledesk-hq.example' must NOT hit the decoy."""
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(sender="vendor@mapledesk-hq.example"),
            limit=20,
        )
        ids = {r["id"] for r in results}
        self.assertNotIn(self.decoy_id, ids)

    def test_recipient_filter_hits_correct_message(self) -> None:
        """recipient filter 'client@papyra.example' must hit message B."""
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(recipient="client@papyra.example"),
            limit=20,
        )
        ids = {r["id"] for r in results}
        self.assertIn(self.msg_b_id, ids)

    def test_recipient_filter_does_not_hit_other_messages(self) -> None:
        """recipient filter 'client@papyra.example' must NOT hit message A or decoy."""
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(recipient="client@papyra.example"),
            limit=20,
        )
        ids = {r["id"] for r in results}
        self.assertNotIn(self.msg_a_id, ids)
        self.assertNotIn(self.decoy_id, ids)

    def test_sender_domain_filter_hits_correct_message(self) -> None:
        """sender_domain='acme.com' must hit message C (billing@acme.com)."""
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(sender_domain="acme.com"),
            limit=20,
        )
        ids = {r["id"] for r in results}
        self.assertIn(self.msg_c_id, ids)
        self.assertNotIn(self.decoy_id, ids)

    def test_recipient_domain_filter_hits_correct_message(self) -> None:
        """recipient_domain='partner.org' must hit message D."""
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(recipient_domain="partner.org"),
            limit=20,
        )
        ids = {r["id"] for r in results}
        self.assertIn(self.msg_d_id, ids)
        self.assertNotIn(self.decoy_id, ids)

    def test_attachment_name_filter_hits_correct_message(self) -> None:
        """attachment_name='invoice_Q1' must hit message C (has invoice_Q1.pdf)."""
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(attachment_name="invoice_Q1"),
            limit=20,
        )
        ids = {r["id"] for r in results}
        self.assertIn(self.msg_c_id, ids)
        self.assertNotIn(self.decoy_id, ids)

    def test_structured_filter_with_cjk_subject_message_unaffected(self) -> None:
        """sender filter must still find message A even though its subject has CJK.

        This confirms that CJK segmentation in the FTS index does NOT interfere
        with structured sender-based LIKE filter going through a different code path.
        """
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(sender="vendor@mapledesk-hq.example"),
            limit=20,
        )
        ids = {r["id"] for r in results}
        # Message A has CJK in subject — structured filter must still find it
        self.assertIn(self.msg_a_id, ids)
        # Subject must be returned as original (not segmented)
        matched = next(r for r in results if r["id"] == self.msg_a_id)
        self.assertEqual(matched["subject"], "甲乙發票 Maple Desk")

    def test_sender_filter_combined_with_no_keyword_returns_correctly(self) -> None:
        """sender filter without keyword must work independently of FTS path."""
        results = search_messages(
            database_path=self.db,
            filters=SearchFilters(sender="nobody@random.net"),
            limit=20,
        )
        ids = {r["id"] for r in results}
        # Decoy is the only message from nobody@random.net
        self.assertIn(self.decoy_id, ids)
        self.assertNotIn(self.msg_a_id, ids)
        self.assertNotIn(self.msg_b_id, ids)
        self.assertNotIn(self.msg_c_id, ids)
        self.assertNotIn(self.msg_d_id, ids)


if __name__ == "__main__":
    unittest.main()

"""Tests for strip_quote_tail() and the --new-only CLI flag.

Covers all 6 ranked boundary markers, the conservative fallback, data-loss
guards, and CLI wiring for both 'show' and 'thread' subcommands.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from mailattic.cli import main
from mailattic.models import PstSource, build_normalized_message
from mailattic.query import SearchFilters, search_messages, strip_quote_tail
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

# ---------------------------------------------------------------------------
# DB / CLI helpers (mirrored from test_show_format.py)
# ---------------------------------------------------------------------------

_PLAIN_BODY_WITH_EN_QUOTE = """\
Please review the attached report.

From: Alice Chen <alice@example.com>
Sent: Monday, December 1, 2025 9:00 AM
To: Bob Lee <bob@example.com>
Subject: Re: Q4 Budget Review

Original message body — should be stripped with --new-only.
"""

_PLAIN_BODY_WITH_GT_QUOTE = """\
Thanks for the update.

> Original quoted line one
> Original quoted line two
"""


def _make_db_with_message(
    temp_dir: str, body: str = _PLAIN_BODY_WITH_EN_QUOTE
) -> tuple[Path, int]:
    """Create a temporary DB with a single test message. Returns (db_path, db_id)."""
    db_path = initialize_database(Path(temp_dir) / "test.db")
    source = PstSource(
        path=Path("C:/mail/archive.pst"),
        input_mode="file",
        requested_from=Path("C:/mail/archive.pst"),
    )
    msg = build_normalized_message(
        {
            "subject": "Test --new-only",
            "sender": {"name": "Alice", "email": "alice@example.com"},
            "to": [{"name": "Bob", "email": "bob@example.com"}],
            "sent_at": "2025-12-01T09:00:00",
            "body_text": body,
            "message_id": "<new-only-test@example.com>",
            "attachments": [],
        },
        source=source,
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    conn = connect_database(db_path)
    try:
        result = storage_persist_message(conn, msg)
        conn.commit()
        db_id = result.stored_message_id
    finally:
        conn.close()
    return db_path, db_id


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    """Run main() capturing stdout/stderr. Returns (exit_code, stdout, stderr)."""
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = StringIO()
    sys.stderr = StringIO()
    try:
        exit_code = main(args)
    except SystemExit as e:
        exit_code = int(e.code) if e.code is not None else 0
    finally:
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        sys.stdout, sys.stderr = old_stdout, old_stderr
    return exit_code, out, err


# ---------------------------------------------------------------------------
# Unit tests — strip_quote_tail() directly
# ---------------------------------------------------------------------------


class TestStripQuoteTailUnit(unittest.TestCase):
    """Direct unit tests for strip_quote_tail() — one per marker type."""

    # Rank 1: > quote line ------------------------------------------------

    def test_rank1_gt_quote_line_cut(self) -> None:
        """A line starting with '>' is a boundary; content before it is kept."""
        text = "New content here.\n\n> Quoted text\n> More quoted content\n"
        result = strip_quote_tail(text)
        self.assertEqual(result, "New content here.")
        self.assertNotIn(">", result)

    def test_rank1_gt_quote_with_leading_space(self) -> None:
        """render_text may leave one leading space — still detected."""
        text = "New content.\n\n >quoted with space\n"
        result = strip_quote_tail(text)
        self.assertEqual(result, "New content.")

    # Rank 2: EN Outlook header block --------------------------------------

    def test_rank2_en_header_block_cut(self) -> None:
        """≥2 consecutive English Outlook header lines form a boundary."""
        text = (
            "See reply history below.\n\n"
            "From: Alice Chen <alice@example.com>\n"
            "Sent: Monday, December 1, 2025 9:00 AM\n"
            "To: Bob Lee <bob@example.com>\n"
            "Subject: Re: Q4 Budget\n\n"
            "Original message body.\n"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, "See reply history below.")
        self.assertNotIn("From:", result)

    def test_rank2_single_en_header_line_not_cut(self) -> None:
        """A single EN header line is NOT a boundary (need ≥2 consecutive)."""
        text = "Body text.\n\nFrom: Alice <alice@example.com>\n\nMore body text.\n"
        result = strip_quote_tail(text)
        # Not cut — single header line
        self.assertIn("From:", result)
        self.assertIn("More body text.", result)

    # Rank 3: CJK header block --------------------------------------------

    def test_rank3_cjk_anchor_block_cut(self) -> None:
        """≥2 CJK header lines with ≥1 anchor token form a boundary."""
        text = (
            "以下為回覆內容，請參考。\n\n"
            "寄件者: Alice Chen <alice@example.com>\n"
            "收件者: Bob Lee <bob@example.com>\n"
            "主旨: 重要通知\n\n"
            "Original CJK quoted content.\n"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, "以下為回覆內容，請參考。")
        self.assertNotIn("寄件者:", result)

    def test_rank3_cjk_context_only_not_cut(self) -> None:
        """CJK context-only tokens (日期/時間) alone must NOT trigger a boundary."""
        text = "Body text.\n\n日期: 2025年12月1日\n時間: 09:00\n\nMore content here.\n"
        result = strip_quote_tail(text)
        # Context tokens alone — not a boundary
        self.assertIn("日期:", result)
        self.assertIn("More content here.", result)

    def test_rank3_cjk_sc_anchor_detected(self) -> None:
        """Simplified Chinese anchor token 发件人 is also detected."""
        text = (
            "確認收到。\n\n"
            "发件人: Alice <alice@example.com>\n"
            "收件人: Bob <bob@example.com>\n"
            "主题: 测试邮件\n\n"
            "Quoted content.\n"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, "確認收到。")

    # Rank 4: -----Original Message----- / draft separator ----------------

    def test_rank4a_original_message_separator_cut(self) -> None:
        """-----Original Message----- is a boundary (case-insensitive)."""
        text = (
            "Please see the original below.\n\n"
            "-----Original Message-----\n"
            "From: Alice\n"
            "Subject: Test\n\n"
            "Original body text.\n"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, "Please see the original below.")
        self.assertNotIn("Original Message", result)

    def test_rank4a_original_message_case_insensitive(self) -> None:
        """-----original message----- (lowercase) is also matched."""
        text = "New content.\n\n-----original message-----\nFrom: Alice\n"
        result = strip_quote_tail(text)
        self.assertEqual(result, "New content.")

    def test_rank4a_original_message_crlf_line_endings(self) -> None:
        """Real DB bodies arrive with CRLF; the marker must still cut.

        Regression: render_text does not strip ``\\r``, so an end-anchored
        ``-----Original Message-----\\r\\n`` previously failed to match and the
        marker line leaked into the output. The quoted tail here is CJK-only,
        so no EN header block can mask the bug — the marker itself must cut.
        """
        text = (
            "Reply content above.\r\n\r\n"
            "-----Original Message-----\r\n"
            "原始郵件內容在此。\r\n"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, "Reply content above.")
        self.assertNotIn("Original Message", result)
        self.assertNotIn("原始郵件", result)

    def test_rank4b_draft_underscore_separator_cut(self) -> None:
        """≥32 consecutive underscores on a line form a draft separator boundary."""
        sep = "_" * 64
        text = f"New message content here.\n\n{sep}\nFrom: Alice\nSubject: Draft\n"
        result = strip_quote_tail(text)
        self.assertEqual(result, "New message content here.")

    def test_rank4b_short_underscore_run_not_cut(self) -> None:
        """A short underscore run (< 32) is NOT a draft separator."""
        sep = "_" * 10  # too short
        text = f"Body content.\n\n{sep}\n\nMore content.\n"
        result = strip_quote_tail(text)
        self.assertIn(sep, result)
        self.assertIn("More content.", result)

    # Rank 5: On … wrote: -------------------------------------------------

    def test_rank5_on_wrote_cut(self) -> None:
        """'On ... wrote:' at end of a line is a boundary."""
        text = (
            "Thanks for the update.\n\n"
            "On Monday, December 1, 2025, Alice Chen <alice@example.com> wrote:\n\n"
            "Original message here.\n"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, "Thanks for the update.")

    def test_rank5_on_wrote_false_positive_not_cut(self) -> None:
        """A prose 'On X I wrote:' line with no date/email must NOT cut.

        Red test: 'On the invoice I wrote:' (no digit, no < or @) must be
        retained as normal content.
        Green test: a real attribution line still cuts correctly.
        """
        # --- Red: prose sentence with "On ... wrote:" — must NOT cut ---
        prose_text = (
            "Please see the details below.\n\n"
            "On the invoice I wrote: payment due in 30 days.\n\n"
            "Let me know if you have questions.\n"
        )
        prose_result = strip_quote_tail(prose_text)
        self.assertIn("On the invoice I wrote:", prose_result)
        self.assertIn("Let me know if you have questions.", prose_result)

        # --- Red: line ending exactly in "wrote:" with a stray digit but no
        # year (e.g. an invoice number) must NOT cut — requires a 4-digit year.
        digit_text = (
            "Intro paragraph.\n\nOn invoice 123 I wrote:\nThis is still new content.\n"
        )
        digit_result = strip_quote_tail(digit_text)
        self.assertIn("On invoice 123 I wrote:", digit_result)
        self.assertIn("This is still new content.", digit_result)

        # --- Red: prose with a stray 4-digit year but no email must NOT cut
        # (a real attribution line carries both a year AND the sender's @).
        year_text = (
            "Summary follows.\n\n"
            "On the 2026 forecast I wrote:\n"
            "Detailed projections remain new content.\n"
        )
        year_result = strip_quote_tail(year_text)
        self.assertIn("On the 2026 forecast I wrote:", year_result)
        self.assertIn("Detailed projections remain new content.", year_result)

        # --- Green: real attribution line still cuts ---
        real_text = (
            "Thanks for the update.\n\n"
            "On Mon, Jun 30, 2026 at 9:01 AM, John <j@x.com> wrote:\n\n"
            "Original quoted content.\n"
        )
        real_result = strip_quote_tail(real_text)
        self.assertEqual(real_result, "Thanks for the update.")
        self.assertNotIn("Original quoted content", real_result)

    # Rank 6: External banner ---------------------------------------------

    def test_rank6_banner_midbody_false_positive_not_cut(self) -> None:
        """[EXTERNAL] appearing mid-line inside real content must NOT cut.

        Gateways inject banner tokens at line start; prose mentioning
        '[EXTERNAL]' as part of a sentence should be retained intact.
        """
        # mid-sentence: "[EXTERNAL]" is NOT at line start
        text = (
            "We integrate with the [EXTERNAL] vendor API for all imports.\n\n"
            "Please review the attached specification.\n\n"
            "Kind regards,\nAlice"
        )
        result = strip_quote_tail(text)
        self.assertIn("[EXTERNAL]", result)
        self.assertIn("Please review the attached specification.", result)
        self.assertIn("Kind regards,", result)

    def test_rank6_banner_start_token_cut(self) -> None:
        """ZjQcmQRYFpfptBannerStart with content before it is a boundary."""
        text = (
            "Real new content in this email.\n\n"
            "ZjQcmQRYFpfptBannerStart\n"
            "[EXTERNAL] This message originated outside the organization.\n"
            "ZjQcmQRYFpfptBannerEnd\n\n"
            "Quoted message body would follow here.\n"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, "Real new content in this email.")

    def test_rank6_external_bracket_token_cut(self) -> None:
        """[EXTERNAL] token with content before it is a boundary."""
        text = (
            "Approved — please proceed.\n\n"
            "[EXTERNAL] This email originated from outside.\n\n"
            "> Quoted original message\n"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, "Approved — please proceed.")

    def test_rank6_caution_token_cut(self) -> None:
        """'CAUTION: This email' with content before it is a boundary."""
        text = (
            "Got it.\n\n"
            "CAUTION: This email came from an external source.\n\n"
            "From: Alice\nSubject: External mail\n"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, "Got it.")

    # Data-loss guards ----------------------------------------------------

    def test_top_banner_not_emptied(self) -> None:
        """Banner at the very top of the text must NOT empty the message.

        Gateway injects the banner before the actual body content.  The real
        new content lives AFTER the banner, so cutting at the top-banner would
        lose the whole message.
        """
        text = (
            "ZjQcmQRYFpfptBannerStart\n"
            "[EXTERNAL] This message is from outside.\n"
            "ZjQcmQRYFpfptBannerEnd\n\n"
            "Actual new content from the sender.\n\n"
            "> Quoted text from previous message\n"
        )
        result = strip_quote_tail(text)
        # Top-banner guard: banner NOT used as boundary
        # The > line IS the boundary
        self.assertIn("Actual new content from the sender.", result)
        self.assertNotIn("> Quoted text", result)

    def test_no_boundary_unchanged(self) -> None:
        """Conservative fallback: no boundary → full text returned unchanged."""
        text = (
            "Simple email with no reply history.\n\n"
            "Just two paragraphs of new content.\n\n"
            "Kind regards,\nAlice"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, text)

    def test_nonwhitelisted_base64_not_cut(self) -> None:
        """A generic base64-looking line must NOT trigger any boundary.

        Only the exact whitelist tokens (ZjQcmQRYFpfptBannerStart, [EXTERNAL],
        [External], 'CAUTION: This email') are cut anchors.
        """
        text = (
            "New content here.\n\n"
            "dGhpcyBpcyBiYXNlNjQgZW5jb2RlZCBjb250ZW50\n"
            "More new content here.\n"
        )
        result = strip_quote_tail(text)
        self.assertIn("dGhpcyBpcyBiYXNlNjQgZW5jb2RlZCBjb250ZW50", result)
        self.assertIn("More new content here.", result)

    def test_empty_string_unchanged(self) -> None:
        """Empty string input → empty string output."""
        self.assertEqual(strip_quote_tail(""), "")

    def test_whitespace_only_unchanged(self) -> None:
        """Whitespace-only text → returned unchanged (nothing to strip)."""
        text = "   \n\n  "
        # strip_quote_tail returns early — no candidates → returns original
        self.assertEqual(strip_quote_tail(text), text)

    # Earliest-wins property ----------------------------------------------

    def test_earliest_boundary_wins(self) -> None:
        """When multiple markers are present, the earliest position wins."""
        text = (
            "New content.\n\n"
            "> Quoted line (rank 1, earlier)\n\n"
            "-----Original Message-----\n"
            "From: Alice\nSubject: Test\n"
        )
        result = strip_quote_tail(text)
        self.assertEqual(result, "New content.")
        # Both markers present but > fires first
        self.assertNotIn("Original Message", result)
        self.assertNotIn(">", result)

    # Multi-message thread ≥5× shrink acceptance --------------------------

    def test_multi_message_thread_five_x_shrink(self) -> None:
        """Multi-reply thread with --new-only achieves ≥5× total size shrink.

        Simulates a 5-message thread where each reply includes all previous
        messages as quoted content via an EN Outlook header block.
        Acceptance criterion per design.md: ≥~5× smaller total size.
        """

        def wrap_with_quote(new_content: str, previous: str) -> str:
            return (
                f"{new_content}\n\n"
                f"From: Alice Chen <alice@example.com>\n"
                f"Sent: Monday, December 1, 2025, 9:00 AM\n"
                f"To: Bob Lee <bob@example.com>\n"
                f"Subject: Re: Q4 Budget Discussion\n\n"
                f"{previous}"
            )

        msg1 = (
            "Let us discuss the Q4 budget. Please review the attached spreadsheet "
            "and share your comments by end of week."
        )
        msg2 = wrap_with_quote(
            "Agreed, I will review it today and send comments.", msg1
        )
        msg3 = wrap_with_quote(
            "I have reviewed the spreadsheet. My comments are attached.", msg2
        )
        msg4 = wrap_with_quote(
            "Thank you for the comments. Scheduling a follow-up meeting.", msg3
        )
        msg5 = wrap_with_quote(
            "Meeting confirmed for Friday at 2 PM. Please prepare a summary.", msg4
        )

        thread = [msg1, msg2, msg3, msg4, msg5]
        original_total = sum(len(m) for m in thread)
        stripped_total = sum(len(strip_quote_tail(m)) for m in thread)

        self.assertGreater(
            original_total,
            stripped_total * 5,
            f"Expected ≥5× shrink; got ratio={original_total / stripped_total:.1f}× "
            f"(original={original_total}, stripped={stripped_total})",
        )


# ---------------------------------------------------------------------------
# CLI integration tests — show subcommand
# ---------------------------------------------------------------------------


class TestShowNewOnlyCli(unittest.TestCase):
    """CLI integration tests for 'bes show --new-only'."""

    def test_show_new_only_format_json_exits_nonzero(self) -> None:
        """--new-only + --format json must exit non-zero with a stderr diagnostic."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            code, _, err = _run_cli(
                [
                    "show",
                    "--database",
                    str(db_path),
                    "--format",
                    "json",
                    "--new-only",
                    str(db_id),
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertIn("--new-only", err.lower())

    def test_show_new_only_legacy_json_flag_exits_nonzero(self) -> None:
        """--new-only + legacy --json flag must also exit non-zero."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            code, _, err = _run_cli(
                [
                    "show",
                    "--database",
                    str(db_path),
                    "--json",
                    "--new-only",
                    str(db_id),
                ]
            )
        self.assertNotEqual(code, 0)

    def test_show_new_only_format_raw_exits_nonzero(self) -> None:
        """--new-only + --format raw must exit non-zero."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            code, _, err = _run_cli(
                [
                    "show",
                    "--database",
                    str(db_path),
                    "--format",
                    "raw",
                    "--new-only",
                    str(db_id),
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertIn("--new-only", err.lower())

    def test_show_new_only_with_headers_exits_nonzero(self) -> None:
        """--new-only + --headers must exit non-zero (headers is non-text mode)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            code, _, err = _run_cli(
                [
                    "show",
                    "--database",
                    str(db_path),
                    "--headers",
                    "--new-only",
                    str(db_id),
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertIn("--new-only", err.lower())

    def test_show_format_text_without_new_only_has_quoted_content(self) -> None:
        """Without --new-only, the EN header quoted block appears in text output."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            code, out, _ = _run_cli(
                [
                    "show",
                    "--database",
                    str(db_path),
                    "--format",
                    "text",
                    str(db_id),
                ]
            )
        self.assertEqual(code, 0)
        # The EN header block from the quoted part must still be present
        self.assertIn("From: Alice Chen", out)
        self.assertIn("Original message body", out)

    def test_show_new_only_strips_quoted_content(self) -> None:
        """--new-only strips the quoted EN header block, keeps only new content."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            code, out, _ = _run_cli(
                [
                    "show",
                    "--database",
                    str(db_path),
                    "--format",
                    "text",
                    "--new-only",
                    str(db_id),
                ]
            )
        self.assertEqual(code, 0)
        # The new content before the EN header should be present
        self.assertIn("Please review the attached report", out)
        # The quoted EN header block body should be stripped
        self.assertNotIn("Original message body", out)

    def test_show_without_new_only_byte_identical_to_baseline(self) -> None:
        """Without --new-only, show --format text output is byte-identical twice."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            args = [
                "show",
                "--database",
                str(db_path),
                "--format",
                "text",
                str(db_id),
            ]
            _, out1, _ = _run_cli(args)
            _, out2, _ = _run_cli(args)
        self.assertEqual(out1, out2)


# ---------------------------------------------------------------------------
# CLI integration tests — thread subcommand
# ---------------------------------------------------------------------------


class TestThreadNewOnlyCli(unittest.TestCase):
    """CLI integration tests for 'bes thread --new-only'."""

    def test_thread_new_only_format_json_exits_nonzero(self) -> None:
        """--new-only + --format json must exit non-zero with a stderr diagnostic."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp, body=_PLAIN_BODY_WITH_GT_QUOTE)
            code, _, err = _run_cli(
                [
                    "thread",
                    "--database",
                    str(db_path),
                    "--format",
                    "json",
                    "--new-only",
                    str(db_id),
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertIn("--new-only", err.lower())

    def test_thread_new_only_format_raw_exits_nonzero(self) -> None:
        """--new-only + --format raw must exit non-zero."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp, body=_PLAIN_BODY_WITH_GT_QUOTE)
            code, _, err = _run_cli(
                [
                    "thread",
                    "--database",
                    str(db_path),
                    "--format",
                    "raw",
                    "--new-only",
                    str(db_id),
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertIn("--new-only", err.lower())

    def test_thread_format_text_without_new_only_has_quoted_content(self) -> None:
        """Without --new-only, the > quoted block appears in thread text output."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp, body=_PLAIN_BODY_WITH_GT_QUOTE)
            code, out, _ = _run_cli(
                [
                    "thread",
                    "--database",
                    str(db_path),
                    "--format",
                    "text",
                    str(db_id),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("> Original quoted line one", out)

    def test_thread_new_only_strips_gt_quoted_content(self) -> None:
        """--new-only strips the > quoted block in thread --format text output."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp, body=_PLAIN_BODY_WITH_GT_QUOTE)
            code, out, _ = _run_cli(
                [
                    "thread",
                    "--database",
                    str(db_path),
                    "--format",
                    "text",
                    "--new-only",
                    str(db_id),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("Thanks for the update.", out)
        self.assertNotIn("> Original quoted line one", out)

    def test_thread_without_new_only_byte_identical_to_baseline(self) -> None:
        """Without --new-only, thread --format text output is byte-identical twice."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp, body=_PLAIN_BODY_WITH_GT_QUOTE)
            args = [
                "thread",
                "--database",
                str(db_path),
                "--format",
                "text",
                str(db_id),
            ]
            _, out1, _ = _run_cli(args)
            _, out2, _ = _run_cli(args)
        self.assertEqual(out1, out2)


# ---------------------------------------------------------------------------
# Bottom / interleaved replies and quote-shape defect tests (issue #290)
# ---------------------------------------------------------------------------


class TestBottomAndInterleavedReplies(unittest.TestCase):
    """Tests for bottom replies, interleaved replies, and '>' false positives.

    Covers issue #290:
    - Bottom reply (On … wrote: + > quote + reply below)
    - Chinese attribution line (某某 於 … 寫道：) with reply below
    - Interleaved reply (answers between quoted lines)
    - New content with lines starting with > (>>> URGENT <<< and wrapped >= 500 boxes)
    - Undecidable cut returns full text (never empty for non-empty body)
    """

    def test_bottom_reply_on_wrote_quote_reply_below(self) -> None:
        """Bottom reply: sender's text below quote survives, match_in is 'new'."""
        body = (
            "On Mon, Mar 2, 2026 at 9:00 AM Owner <owner@example.org> wrote:\n"
            "> Can you ship 20 boxes by Friday?\n"
            "> Also what is the price?\n\n"
            "Yes, we ship Friday. The price is 100 USD/box.\n"
        )
        stripped = strip_quote_tail(body)
        self.assertIn("Yes, we ship Friday. The price is 100 USD/box.", stripped)
        self.assertNotIn("20 boxes", stripped)
        self.assertNotIn("wrote:", stripped)

        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _make_db_with_message(tmp, body=body)
            hits_new_price = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword='"100 USD"'),
            )
            self.assertEqual(len(hits_new_price), 1)
            self.assertEqual(hits_new_price[0]["match_field"], "body")
            self.assertEqual(hits_new_price[0]["match_in"], "new")

            hits_quoted_qty = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword='"20 boxes"'),
            )
            self.assertEqual(len(hits_quoted_qty), 1)
            self.assertEqual(hits_quoted_qty[0]["match_field"], "body")
            self.assertEqual(hits_quoted_qty[0]["match_in"], "quoted")

    def test_chinese_attribution_reply_below(self) -> None:
        """Chinese attribution line + reply below survives, match_in is 'new'."""
        body = (
            "某某 於 2026年3月2日 上午9:00 寫道：\n"
            "> 可以週五出貨 20 boxes 嗎？\n"
            "> 另外價格是多少？\n\n"
            "可以，週五出貨。價格是 100 USD/box。\n"
        )
        stripped = strip_quote_tail(body)
        self.assertIn("可以，週五出貨。價格是 100 USD/box。", stripped)
        self.assertNotIn("20 boxes", stripped)
        self.assertNotIn("寫道", stripped)

        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _make_db_with_message(tmp, body=body)
            hits_new_price = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword='"100 USD"'),
            )
            self.assertEqual(len(hits_new_price), 1)
            self.assertEqual(hits_new_price[0]["match_field"], "body")
            self.assertEqual(hits_new_price[0]["match_in"], "new")

            hits_quoted_qty = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword='"20 boxes"'),
            )
            self.assertEqual(len(hits_quoted_qty), 1)
            self.assertEqual(hits_quoted_qty[0]["match_field"], "body")
            self.assertEqual(hits_quoted_qty[0]["match_in"], "quoted")

    def test_interleaved_inline_reply(self) -> None:
        """Interleaved reply: sender's answers inline survive, match_in is 'new'."""
        body = (
            "Hi, answers inline.\n\n"
            "> Can you ship 20 boxes by Friday?\n"
            "Correction: only Monday is possible.\n\n"
            "> Also what is the price?\n"
            "Revised price is 120 USD/box.\n\n"
            "Ana\n"
        )
        stripped = strip_quote_tail(body)
        self.assertIn("Hi, answers inline.", stripped)
        self.assertIn("Correction: only Monday is possible.", stripped)
        self.assertIn("Revised price is 120 USD/box.", stripped)
        self.assertIn("Ana", stripped)
        self.assertNotIn("20 boxes", stripped)

        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _make_db_with_message(tmp, body=body)
            hits_monday = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword="Monday"),
            )
            self.assertEqual(len(hits_monday), 1)
            self.assertEqual(hits_monday[0]["match_field"], "body")
            self.assertEqual(hits_monday[0]["match_in"], "new")

            hits_revised_price = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword='"120 USD"'),
            )
            self.assertEqual(len(hits_revised_price), 1)
            self.assertEqual(hits_revised_price[0]["match_field"], "body")
            self.assertEqual(hits_revised_price[0]["match_in"], "new")

            hits_quoted_qty = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword='"20 boxes"'),
            )
            self.assertEqual(len(hits_quoted_qty), 1)
            self.assertEqual(hits_quoted_qty[0]["match_field"], "body")
            self.assertEqual(hits_quoted_qty[0]["match_in"], "quoted")

    def test_new_content_with_gt_lines_retained(self) -> None:
        """New content with lines starting with > (callouts, >=) is retained."""
        body = (
            ">>> URGENT <<<\n"
            "Please confirm the order quantity:\n"
            ">= 500 boxes\n"
            "Thank you for your assistance.\n"
        )
        stripped = strip_quote_tail(body)
        self.assertIn(">>> URGENT <<<", stripped)
        self.assertIn(">= 500 boxes", stripped)
        self.assertIn("Please confirm the order quantity:", stripped)
        self.assertIn("Thank you for your assistance.", stripped)

        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _make_db_with_message(tmp, body=body)
            hits_urgent = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword="URGENT"),
            )
            self.assertEqual(len(hits_urgent), 1)
            self.assertEqual(hits_urgent[0]["match_field"], "body")
            self.assertEqual(hits_urgent[0]["match_in"], "new")

            hits_500 = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword='"500 boxes"'),
            )
            self.assertEqual(len(hits_500), 1)
            self.assertEqual(hits_500[0]["match_field"], "body")
            self.assertEqual(hits_500[0]["match_in"], "new")

    def test_undecidable_cut_returns_full_text_never_empty(self) -> None:
        """Undecidable cut returns full text: never empty for a non-empty body."""
        body = "> Quoted line one\n> Quoted line two\n"
        stripped = strip_quote_tail(body)
        self.assertEqual(stripped, body)
        self.assertNotEqual(stripped, "")

        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _make_db_with_message(tmp, body=body)
            hits = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword='"Quoted line"'),
            )
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["match_field"], "body")
            self.assertEqual(hits[0]["match_in"], "new")

    def test_chinese_wrote_prose_without_quote_is_not_a_cut_point(self) -> None:
        """「…於…寫道：」 in ordinary prose (no '>' after it) keeps the text below."""
        body = (
            "您好，\n如同我於合約第三條寫道：\n交期為四週，價格 100 USD/box。\n謝謝\n"
        )
        self.assertEqual(strip_quote_tail(body), body)

        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _make_db_with_message(tmp, body=body)
            hits = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword='"100 USD"'),
            )
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["match_in"], "new")

    def test_chinese_attribution_above_quote_in_top_posted_reply_is_cut(self) -> None:
        body = (
            "可以，價格 105 USD/box。\n\n"
            "某某 於 2026年3月2日 上午9:00 寫道：\n"
            "> 週五可以出貨嗎？\n"
        )
        self.assertEqual(strip_quote_tail(body), "可以，價格 105 USD/box。")


if __name__ == "__main__":
    unittest.main()

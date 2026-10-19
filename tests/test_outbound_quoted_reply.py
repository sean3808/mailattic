"""Tests for build_quoted_reply_bodies (task groups 4 & 5).

Group 4: unit tests for the pure quoted-body assembly function.
Group 5: integration tests — multipart draft assembly and _extract_body_text.
"""

from __future__ import annotations

from email import message_from_bytes, policy
from pathlib import Path

import pytest

from mailattic.cli import build_parser
from mailattic.models import PstSource, build_normalized_message
from mailattic.outbound import (
    _extract_body_text,
    build_quoted_reply_bodies,
)
from mailattic.outbound_commands import _handle_draft_reply
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)
from tests.test_outbound_draft import PLACEHOLDER_SMTP_SETTINGS

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_original(
    *,
    cc: list[dict] | None = None,
    sent_at: str | None = "2026-05-26T09:03:07",
    subject: str = "Test subject",
    body_text: str = "Original body text",
) -> dict:
    return {
        "sender_name": "Alice Chen",
        "sender_email": "alice@example.com",
        "sent_at": sent_at,
        "subject": subject,
        "body_text": body_text,
        "to": [{"name": "Sean Example", "email": "sean3808@mailattic.test"}],
        "cc": cc if cc is not None else [],
    }


def _seed_db(tmp_path: Path) -> Path:
    db_path = initialize_database(tmp_path / "evidence.db")
    message = build_normalized_message(
        {
            "subject": "Test subject",
            "sender": {"name": "Alice Chen", "email": "alice@example.com"},
            "to": [{"name": "Sean Example", "email": "sean3808@mailattic.test"}],
            "cc": [{"name": "Bob Lee", "email": "bob@example.com"}],
            "message_id": "<quoted-reply-test@mailattic.test>",
            "references": None,
            "body_text": "Original body text",
        },
        source=PstSource(
            path=Path("C:/archive/mailbox.pst"),
            input_mode="file",
            requested_from=Path("C:/archive/mailbox.pst"),
        ),
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    connection = connect_database(db_path)
    try:
        persist_message(connection, message)
        connection.commit()
    finally:
        connection.close()
    return db_path


def _placeholder_env_file(tmp_path: Path) -> Path:
    # Just enough settings to name the sending account (the draft's From);
    # these tests are unrelated to SMTP config resolution.
    env_file = tmp_path / "placeholder.env"
    env_file.write_text(PLACEHOLDER_SMTP_SETTINGS, encoding="utf-8")
    return env_file


# ---------------------------------------------------------------------------
# Group 4: unit tests — build_quoted_reply_bodies
# ---------------------------------------------------------------------------


class TestBuildQuotedReplyBodies:
    """4.1 — basic fields present and reply_text precedes the quoted block."""

    def test_plain_contains_reply_text_and_all_header_fields(self) -> None:
        original = _make_original()
        plain, _ = build_quoted_reply_bodies(original, "My reply")

        assert "My reply" in plain
        assert "Alice Chen" in plain
        assert "alice@example.com" in plain
        assert "sean3808@mailattic.test" in plain
        assert "Test subject" in plain
        assert "Original body text" in plain

    def test_html_contains_reply_text_and_all_header_fields(self) -> None:
        original = _make_original()
        _, html_body = build_quoted_reply_bodies(original, "My reply")

        assert "My reply" in html_body
        assert "alice@example.com" in html_body
        assert "sean3808@mailattic.test" in html_body
        assert "Test subject" in html_body
        assert "Original body text" in html_body

    def test_cjk_display_names_are_readable_in_both_renderings(self) -> None:
        original = _make_original(
            cc=[
                {
                    "name": "Bob 王小明 (ACME)",
                    "email": "bob.lund@acme-co.example",
                }
            ]
        )
        original["sender_name"] = "=?utf-8?b?Q2Fyb2wg6Zmz5aSn6I+v?="
        original["to"] = [{"name": "丙商行丁先生", "email": "chen@org-d.example"}]

        plain, html_body = build_quoted_reply_bodies(original, "reply")

        for rendered_body in (plain, html_body):
            assert "Carol 陳大華" in rendered_body
            assert "丙商行丁先生" in rendered_body
            assert "Bob 王小明 (ACME)" in rendered_body
            assert "=?" not in rendered_body

    def test_reply_text_precedes_separator(self) -> None:
        original = _make_original()
        plain, _ = build_quoted_reply_bodies(original, "REPLY_MARKER")

        # The reply text must come before the underscored separator
        separator_idx = plain.find("____")
        reply_idx = plain.find("REPLY_MARKER")
        assert reply_idx >= 0
        assert separator_idx > reply_idx

    def test_separator_is_underscores(self) -> None:
        original = _make_original()
        plain, _ = build_quoted_reply_bodies(original, "reply")

        assert "_" * 32 in plain


class TestCcBehavior:
    """4.2 — Cc present when original has CC recipients, absent when empty."""

    def test_cc_line_present_when_original_has_cc(self) -> None:
        original = _make_original(cc=[{"name": "Bob Lee", "email": "bob@example.com"}])
        plain, html_body = build_quoted_reply_bodies(original, "reply")

        assert "Cc:" in plain
        assert "bob@example.com" in plain
        assert "Cc:" in html_body
        assert "bob@example.com" in html_body

    def test_cc_line_absent_when_original_has_no_cc(self) -> None:
        original = _make_original(cc=[])
        plain, html_body = build_quoted_reply_bodies(original, "reply")

        assert "Cc:" not in plain
        assert "Cc:" not in html_body


class TestSentFormatting:
    """4.3 — Sent field formatting."""

    @pytest.mark.parametrize(
        "sent_at",
        [
            "2026-05-30T06:09:03+00:00",
            "2026-05-30T14:09:03+08:00",
            "2026-05-30T06:09:03",
        ],
    )
    def test_sent_is_taipei_in_plain_and_html(self, sent_at: str) -> None:
        plain, html_body = build_quoted_reply_bodies(
            _make_original(sent_at=sent_at), "reply"
        )

        expected = "Saturday, May 30, 2026 02:09:03 PM"
        assert f"Sent: {expected}\n" in plain
        assert f"<b>Sent:</b> {expected}<br>" in html_body

    def test_valid_iso_formatted_as_outlook_style_english(self) -> None:
        original = _make_original(sent_at="2026-05-26T09:03:07")
        plain, _ = build_quoted_reply_bodies(original, "reply")

        # 2026-05-26 is a Tuesday; full Outlook pattern
        assert "Tuesday" in plain
        assert "May" in plain
        assert "2026" in plain
        assert "05:03:07 PM" in plain

    def test_invalid_sent_at_passes_through_verbatim(self) -> None:
        original = _make_original(sent_at="not-a-date")
        plain, _ = build_quoted_reply_bodies(original, "reply")

        assert "not-a-date" in plain

    def test_invalid_sent_at_does_not_raise(self) -> None:
        original = _make_original(sent_at="not-a-date")
        # must not raise
        build_quoted_reply_bodies(original, "reply")

    def test_none_sent_at_does_not_raise(self) -> None:
        original = _make_original(sent_at=None)
        # must not raise
        plain, _ = build_quoted_reply_bodies(original, "reply")
        # Sent line exists but value is empty
        assert "Sent:" in plain


class TestHtmlEscaping:
    """4.4 — HTML escaping and bold labels."""

    def test_lt_in_subject_is_escaped_in_html(self) -> None:
        original = _make_original(subject="Quote <urgent>")
        _, html_body = build_quoted_reply_bodies(original, "reply")

        assert "&lt;" in html_body
        assert "<urgent>" not in html_body

    def test_amp_in_subject_is_escaped_in_html(self) -> None:
        original = _make_original(subject="terms & conditions")
        _, html_body = build_quoted_reply_bodies(original, "reply")

        assert "&amp;" in html_body
        assert "terms & conditions" not in html_body  # raw & must not appear

    def test_lt_in_body_text_is_escaped_in_html(self) -> None:
        original = _make_original(body_text="See <attached>")
        _, html_body = build_quoted_reply_bodies(original, "reply")

        assert "&lt;" in html_body

    def test_html_has_bold_from_label(self) -> None:
        original = _make_original()
        _, html_body = build_quoted_reply_bodies(original, "reply")

        assert "<b>From:</b>" in html_body

    def test_html_has_bold_sent_label(self) -> None:
        original = _make_original()
        _, html_body = build_quoted_reply_bodies(original, "reply")

        assert "<b>Sent:</b>" in html_body

    def test_html_has_bold_to_label(self) -> None:
        original = _make_original()
        _, html_body = build_quoted_reply_bodies(original, "reply")

        assert "<b>To:</b>" in html_body

    def test_html_has_bold_subject_label(self) -> None:
        original = _make_original()
        _, html_body = build_quoted_reply_bodies(original, "reply")

        assert "<b>Subject:</b>" in html_body

    def test_html_has_hr_separator(self) -> None:
        original = _make_original()
        _, html_body = build_quoted_reply_bodies(original, "reply")

        assert "<hr>" in html_body


# ---------------------------------------------------------------------------
# Group 5: integration tests — multipart draft + _extract_body_text
# ---------------------------------------------------------------------------


class TestMultipartDraftAssembly:
    """5.1 — _handle_draft_reply produces multipart/alternative with two parts."""

    def test_draft_eml_is_multipart_alternative(self, tmp_path: Path) -> None:
        db_path = _seed_db(tmp_path)
        body_file = tmp_path / "body.txt"
        body_file.write_text("My reply text", encoding="utf-8")
        args = build_parser().parse_args(
            [
                "draft-reply",
                "<quoted-reply-test@mailattic.test>",
                "--body-from",
                str(body_file),
                "--database",
                str(db_path),
                "--env-file",
                str(_placeholder_env_file(tmp_path)),
            ]
        )

        assert _handle_draft_reply(args) == 0

        eml_files = list((tmp_path / "outbound" / "drafts").glob("*.eml"))
        assert eml_files, "no draft .eml produced"
        raw = eml_files[0].read_bytes()
        parsed = message_from_bytes(raw, policy=policy.default)

        assert parsed.get_content_type() == "multipart/alternative"

    def test_draft_eml_has_plain_and_html_parts(self, tmp_path: Path) -> None:
        db_path = _seed_db(tmp_path)
        body_file = tmp_path / "body.txt"
        body_file.write_text("My reply text", encoding="utf-8")
        args = build_parser().parse_args(
            [
                "draft-reply",
                "<quoted-reply-test@mailattic.test>",
                "--body-from",
                str(body_file),
                "--database",
                str(db_path),
                "--env-file",
                str(_placeholder_env_file(tmp_path)),
            ]
        )
        _handle_draft_reply(args)

        raw = next((tmp_path / "outbound" / "drafts").glob("*.eml")).read_bytes()
        parsed = message_from_bytes(raw, policy=policy.default)
        part_types = [p.get_content_type() for p in parsed.iter_parts()]

        assert "text/plain" in part_types
        assert "text/html" in part_types
        assert len(part_types) == 2


class TestExtractBodyTextMultipart:
    """5.2 — _extract_body_text on multipart returns text/plain content."""

    def test_extract_body_text_returns_plain_part_not_bytes(
        self, tmp_path: Path
    ) -> None:
        db_path = _seed_db(tmp_path)
        body_file = tmp_path / "body.txt"
        body_file.write_text("Unique reply marker", encoding="utf-8")
        args = build_parser().parse_args(
            [
                "draft-reply",
                "<quoted-reply-test@mailattic.test>",
                "--body-from",
                str(body_file),
                "--database",
                str(db_path),
                "--env-file",
                str(_placeholder_env_file(tmp_path)),
            ]
        )
        _handle_draft_reply(args)

        raw = next((tmp_path / "outbound" / "drafts").glob("*.eml")).read_bytes()
        parsed = message_from_bytes(raw, policy=policy.default)
        extracted = _extract_body_text(parsed)

        # Must be a non-empty string containing the reply text
        assert isinstance(extracted, str)
        assert extracted.strip()
        assert "Unique reply marker" in extracted
        # Must NOT be the raw multipart bytes representation
        assert "MIME-Version" not in extracted
        assert "Content-Type: multipart" not in extracted

    def test_extract_body_text_equals_plain_body(self, tmp_path: Path) -> None:
        """Extracted content matches the plain_body that went into set_content."""
        original = {
            "sender_name": "Alice Chen",
            "sender_email": "alice@example.com",
            "sent_at": "2026-05-26T09:03:07",
            "subject": "Test",
            "body_text": "Original body",
            "to": [{"name": "Sean", "email": "sean3808@mailattic.test"}],
            "cc": [],
        }
        reply_text = "My reply"
        plain_body, html_body = build_quoted_reply_bodies(original, reply_text)

        from email.message import EmailMessage as _EM

        msg = _EM()
        msg.set_content(plain_body, charset="utf-8")
        msg.add_alternative(html_body, subtype="html", charset="utf-8")
        raw = msg.as_bytes()

        from email import message_from_bytes as _mfb
        from email import policy as _policy

        parsed = _mfb(raw, policy=_policy.default)
        extracted = _extract_body_text(parsed)

        # get_content() may add a trailing newline; strip both sides for comparison
        assert extracted.rstrip("\n") == plain_body.rstrip("\n")

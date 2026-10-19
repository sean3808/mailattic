from __future__ import annotations

import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from mailattic.cli import main
from mailattic.models import PstSource, build_normalized_message
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HTML_BODY = (
    "<html><head>"
    "<style>@font-face { font-family: Calibri; } p { color: red; }</style>"
    "</head><body>"
    "<p>Dear Customer,</p>"
    "<p>Price &amp; Terms attached.</p>"
    "<p>Best regards,<br>Alice</p>"
    "</body></html>"
)


def _make_db_with_message(temp_dir: str, body: str = _HTML_BODY) -> tuple[Path, int]:
    """Create a temporary DB with a single test message. Returns (db_path, db_id)."""
    db_path = initialize_database(Path(temp_dir) / "test.db")
    source = PstSource(
        path=Path("C:/mail/archive.pst"),
        input_mode="file",
        requested_from=Path("C:/mail/archive.pst"),
    )
    msg = build_normalized_message(
        {
            "subject": "Test show format",
            "sender": {"name": "Alice", "email": "alice@example.com"},
            "to": [{"name": "Bob", "email": "bob@example.com"}],
            "sent_at": "2025-11-14T10:00:00",
            "body_text": body,
            "message_id": "<show-format-test@example.com>",
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
# Tests
# ---------------------------------------------------------------------------


class ShowFormatTests(unittest.TestCase):
    def test_default_raw_output_matches_explicit_format_raw(self) -> None:
        """Default (no --format) output must be byte-identical to --format raw."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            db_str = str(db_path)
            id_str = str(db_id)

            _, out_default, _ = _run_cli(["show", "--database", db_str, id_str])
            _, out_raw, _ = _run_cli(
                ["show", "--database", db_str, "--format", "raw", id_str]
            )

        self.assertEqual(out_default, out_raw)

    def test_format_json_and_legacy_json_flag_are_byte_identical(self) -> None:
        """--format json and --json must produce byte-identical stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            db_str = str(db_path)
            id_str = str(db_id)

            code_fmt, out_fmt, _ = _run_cli(
                ["show", "--database", db_str, "--format", "json", id_str]
            )
            code_legacy, out_legacy, _ = _run_cli(
                ["show", "--database", db_str, "--json", id_str]
            )

        self.assertEqual(code_fmt, 0)
        self.assertEqual(code_legacy, 0)
        self.assertEqual(out_fmt, out_legacy)
        # Verify it is valid JSON
        parsed = json.loads(out_fmt)
        self.assertIsInstance(parsed, dict)
        self.assertIn("id", parsed)

    def test_format_text_no_html_has_metadata_headers(self) -> None:
        """--format text must not contain HTML artefacts and must have
        From/Sent headers.

        The metadata header lines (From/Sent/To) legitimately contain angle
        brackets around email addresses — the HTML-cleanliness requirement
        applies to the rendered body portion only (after the separator line).
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            db_str = str(db_path)
            id_str = str(db_id)

            code, out, _ = _run_cli(
                ["show", "--database", db_str, "--format", "text", id_str]
            )

        self.assertEqual(code, 0)
        self.assertIn("From", out)
        self.assertIn("Sent", out)
        self.assertIn("Dear Customer", out)

        # HTML artefacts must not appear anywhere in the output
        self.assertNotIn("@font-face", out)
        self.assertNotIn("<style>", out)

        # The body portion (after the separator line "---...") must have no HTML tags.
        # Split on the separator to isolate body from metadata header.
        separator = "-" * 40
        body_part = out.split(separator, 1)[1] if separator in out else out
        self.assertNotIn("<p>", body_part)
        self.assertNotIn("</p>", body_part)
        self.assertNotIn("<html>", body_part)
        self.assertNotIn("<body>", body_part)

    def test_conflicting_json_and_format_text_exits_nonzero_with_diagnostic(
        self,
    ) -> None:
        """--json --format text must exit non-zero and stderr must contain
        'conflicting'."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            db_str = str(db_path)
            id_str = str(db_id)

            code, _, err = _run_cli(
                ["show", "--database", db_str, "--json", "--format", "text", id_str]
            )

        self.assertNotEqual(code, 0)
        self.assertIn("conflicting", err.lower())

    def test_legacy_json_flag_prints_deprecation_hint_to_stderr(self) -> None:
        """--json alone must print a hint containing 'alias' to stderr."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            db_str = str(db_path)
            id_str = str(db_id)

            code, out_legacy, err_legacy = _run_cli(
                ["show", "--database", db_str, "--json", id_str]
            )
            _, out_fmt, _ = _run_cli(
                ["show", "--database", db_str, "--format", "json", id_str]
            )

        self.assertEqual(code, 0)
        self.assertIn("alias", err_legacy)
        self.assertEqual(out_legacy, out_fmt)

    def test_show_nonexistent_id_exits_nonzero(self) -> None:
        """A missing db_id must exit non-zero."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _make_db_with_message(tmp)
            db_str = str(db_path)

            code, _, err = _run_cli(["show", "--database", db_str, "999999"])

        self.assertNotEqual(code, 0)

    def test_json_with_format_raw_is_conflict(self) -> None:
        """--json --format raw must exit 2 and stderr must contain 'conflicting'."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            db_str = str(db_path)
            id_str = str(db_id)

            code, _, err = _run_cli(
                ["show", "--database", db_str, "--json", "--format", "raw", id_str]
            )

        self.assertEqual(code, 2)
        self.assertIn("conflicting", err.lower())

    def test_json_with_format_json_is_not_conflict(self) -> None:
        """--json --format json must succeed (same value, not a conflict)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = _make_db_with_message(tmp)
            db_str = str(db_path)
            id_str = str(db_id)

            code, out, _ = _run_cli(
                ["show", "--database", db_str, "--json", "--format", "json", id_str]
            )

        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertIsInstance(parsed, dict)
        self.assertIn("id", parsed)


if __name__ == "__main__":
    unittest.main()

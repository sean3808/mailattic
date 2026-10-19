"""Search snippets come from the rendered body, not the raw one (issue #35).

A PST-sourced message starts with its HTML ``<style>`` block, so the raw
first-160-characters snippet was pure CSS and carried zero signal -- the
search result could not be used to rank what was worth opening.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mailattic.models import PstSource, build_normalized_message
from mailattic.query import SearchFilters, search_messages
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

_SOURCE = PstSource(
    path=Path("C:/mail/archive.pst"),
    input_mode="file",
    requested_from=Path("C:/mail/archive.pst"),
)

# Synthetic PST-style body: a style block long enough that the whole of
# the old 160-character raw window fell inside it.
_CSS_PREAMBLE = (
    "<!-- /* Font Definitions */\n"
    '@font-face {font-family:"新細明體"; panose-1:2 2 5 0 0 0 0 0 0 0;}\n'
    '@font-face {font-family:"Segoe UI"; panose-1:2 11 5 2 4 2 4 2 2 3;}\n'
    "p.MsoNormal, li.MsoNormal, div.MsoNormal "
    "{margin:0cm; font-size:11.0pt; font-family:'Calibri',sans-serif;}\n"
    "-->\n"
    "<style>\n"
    "a:link, span.MsoHyperlink {color:#0563C1; text-decoration:underline;}\n"
    "span.EmailStyle19 {font-family:'Calibri',sans-serif; color:windowtext;}\n"
    "</style>\n"
)
_BODY_TEXT = "Dear Sean, 甲商行 SKU-88 的英文品名已由 BLUE GEL PEN 改為 OCEAN GEL PEN。"


def _seed(database_path: Path, body_text: str) -> None:
    message = build_normalized_message(
        {
            "subject": "SKU-88 品名變更",
            "sender": {"email": "quill@stationer.example"},
            "sent_at": "2025-05-28T09:00:00",
            "body_text": body_text,
            "message_id": "<snippet-style@example.com>",
        },
        source=_SOURCE,
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    connection = connect_database(database_path)
    try:
        storage_persist_message(connection, message)
        connection.commit()
    finally:
        connection.close()


class SearchSnippetRenderingTests(unittest.TestCase):
    def _snippet_for(self, body_text: str) -> str:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "evidence.db")
            _seed(database_path, body_text)
            results = search_messages(
                database_path=database_path,
                filters=SearchFilters(sender="quill@stationer.example"),
            )
        self.assertEqual(len(results), 1)
        return str(results[0]["snippet"])

    def test_style_block_body_yields_real_text_not_css(self) -> None:
        snippet = self._snippet_for(_CSS_PREAMBLE + "<p>" + _BODY_TEXT + "</p>")
        self.assertIn("OCEAN GEL PEN", snippet)
        for css_marker in ("@font-face", "<style", "panose-1", "MsoNormal", "{"):
            self.assertNotIn(css_marker, snippet)

    def test_snippet_is_capped(self) -> None:
        snippet = self._snippet_for("<p>" + ("x" * 500) + "</p>")
        self.assertEqual(len(snippet), 160)

    def test_empty_body_yields_empty_snippet(self) -> None:
        self.assertEqual(self._snippet_for(""), "")

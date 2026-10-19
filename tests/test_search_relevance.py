"""Keyword-search relevance and match-evidence trip-wires (issue #177).

The reported failure: a keyword search for a price quoted in a long reply
chain returned the newest messages in the chain and never the message the
price originated in, and nothing in the result said why any row matched.
Two causes, one test fixture:

1. `search_messages` ordered by `sent_at DESC` whether or not a keyword was
   given, so `limit` cut the candidate set by recency. The originating
   message is by definition the *oldest* one carrying the terms.
2. `snippet` was the head of the body, which on a reply chain is a greeting.
   Nothing distinguished a hit in the message's own new content from a hit
   inside its quoted history -- the distinction the "which one is the
   original?" question is made of.

The fixture is three messages of one thread: the oldest states the price in
its own new content, the two newer ones only quote it. Recency ordering puts
the oldest last, so `limit=2` is what makes this a trip-wire -- restore
`ORDER BY sent_at DESC` and the oldest drops out of the result entirely
(mailattic/PC-096: a guard whose removal leaves the test green
is not a guard).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mailattic.models import PstSource, build_normalized_message
from mailattic.query import SearchFilters, search_messages
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

_PRICE_LINE = (
    "Papyra Desk PAPER-02 @12 per box (100 sheets/pack) / 15 per box "
    "(minimum for bulk order: 30 boxes)"
)

#: An earlier mention carrying only ONE of the query's terms. Its presence is
#: what makes the snippet assertions discriminating: centring on the earliest
#: occurrence lands here, and this paragraph does not contain the price. It is
#: kept well over two context windows away from the price line so the two
#: cannot be covered by one snippet.
_DECOY_PARAGRAPH = (
    "It will be good to start with the Papyra Desk "
    "PAPER-02 plus a 1% add-on of Gel Pen PEN-100 via the monthly bundle. The bundle "
    "could cover the office needs without an extra "
    "stapler order, which is what the front desk asked about last week."
)

#: The originating message: the terms appear in its own new content.
_ORIGINAL_BODY = (
    f"Hi Alan,\n\n{_DECOY_PARAGRAPH}\n\nPrices,\n\n{_PRICE_LINE}\n"
    "Gel Pen PEN-100 @200 per box\n\nCarol"
)

#: Filler that makes the quoting replies genuinely longer than the original,
#: so the ranking under test is not decided by a length accident.
_FILLER = (
    "We checked the sample pack and the paper feel is acceptable. "
    "Approval is still pending on the dark covers. "
) * 6


def _reply_body(new_content: str) -> str:
    return (
        f"{new_content}\n\n{_FILLER}\n\n"
        "-----Original Message-----\n"
        "From: Carol Roe\n"
        "Sent: Tuesday, July 22, 2026 1:04 PM\n"
        "Subject: RE: Folder | Desk enquiry\n\n"
        f"{_ORIGINAL_BODY}"
    )


_FIXTURES: list[dict[str, object]] = [
    {
        "subject": "RE: Folder | Desk enquiry",
        "sender": {"name": "Carol Roe", "email": "carol@example.com"},
        "to": [{"email": "alan@example.com"}],
        "sent_at": "2026-07-22T13:04:00+08:00",
        "body_text": _ORIGINAL_BODY,
        "message_id": "<original@example.com>",
    },
    {
        "subject": "RE: Folder | Desk enquiry",
        "sender": {"name": "Alan", "email": "alan@example.com"},
        "to": [{"email": "carol@example.com"}],
        "sent_at": "2026-07-24T09:00:00+08:00",
        "body_text": _reply_body("Hi Carol,\n\nWhat other quality concerns are known?"),
        "message_id": "<reply-one@example.com>",
    },
    {
        "subject": "RE: Folder | Desk enquiry",
        "sender": {"name": "Alan", "email": "alan@example.com"},
        "to": [{"email": "carol@example.com"}],
        "sent_at": "2026-08-14T09:00:00+08:00",
        "body_text": _reply_body("Hi Carol,\n\nWe just finished the stock count."),
        "message_id": "<reply-two@example.com>",
    },
    {
        # Carries `zzqrecipient` in its recipients and nowhere in its text --
        # `message_fts` indexes recipients too, so this is the row that
        # matches with the literal absent from both subject and body.
        "subject": "Delivery schedule",
        "sender": {"name": "Carol Roe", "email": "carol@example.com"},
        "to": [{"email": "zzqrecipient@example.com"}],
        "sent_at": "2026-08-20T09:00:00+08:00",
        "body_text": "Order confirmed for next week.",
        "message_id": "<delivery@example.com>",
    },
]


def _create_db(root: Path, name: str, fixtures: list[dict[str, object]]) -> Path:
    database_path = initialize_database(root / name)
    source = PstSource(
        path=Path("C:/mail/archive.pst"),
        input_mode="file",
        requested_from=Path("C:/mail/archive.pst"),
    )
    connection = connect_database(database_path)
    try:
        for index, raw in enumerate(fixtures):
            storage_persist_message(
                connection,
                build_normalized_message(
                    raw,
                    source=source,
                    folder_path="Inbox",
                    logical_locator=f"Inbox/{index + 1}",
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return database_path


def _create_thread_fixture_db(root: Path) -> Path:
    return _create_db(root, "relevance.db", _FIXTURES)


class KeywordRelevanceTests(unittest.TestCase):
    def test_originating_message_outranks_the_newer_messages_quoting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_thread_fixture_db(Path(temp_dir))
            rows = search_messages(
                database_path=database_path,
                filters=SearchFilters(keyword="PAPER-02 15"),
                limit=2,
            )

        # All three messages carry both terms, so this asserts ranking, not
        # filtering: under recency ordering the two 2026-07-24 / 2026-08-14
        # replies fill both slots and the original never appears.
        self.assertEqual(len(rows), 2)
        subjects = [row["sent_at"] for row in rows]
        self.assertIn(
            "2026-07-22T13:04:00+08:00",
            subjects,
            msg=(
                "the message the price originated in must survive a limit of 2; "
                f"got {subjects}"
            ),
        )

    def test_snippet_centres_on_the_hit_and_match_in_separates_new_from_quoted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_thread_fixture_db(Path(temp_dir))
            rows = search_messages(
                database_path=database_path,
                filters=SearchFilters(keyword="PAPER-02 15"),
                limit=10,
            )

        by_sent_at = {row["sent_at"]: row for row in rows}
        self.assertEqual(len(by_sent_at), 3)

        original = by_sent_at["2026-07-22T13:04:00+08:00"]
        # The head of this body is "Hi Alan," -- a head-anchored snippet
        # carries neither term. And `15` specifically is the co-occurrence
        # assertion: the body's FIRST `PAPER-02` is in _DECOY_PARAGRAPH, so
        # centring on the earliest occurrence produces a snippet without it.
        self.assertIn("15", str(original["snippet"]))
        self.assertIn("PAPER-02", str(original["snippet"]))
        self.assertEqual(original["match_field"], "body")
        self.assertEqual(original["match_in"], "new")

        for sent_at in ("2026-07-24T09:00:00+08:00", "2026-08-14T09:00:00+08:00"):
            reply = by_sent_at[sent_at]
            self.assertEqual(
                reply["match_in"],
                "quoted",
                msg=f"{sent_at}: the terms appear only in its quoted history",
            )
            self.assertIn("15", str(reply["snippet"]))

    def test_without_a_keyword_snippet_and_order_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_thread_fixture_db(Path(temp_dir))
            rows = search_messages(
                database_path=database_path,
                filters=SearchFilters(sender="alan@example.com"),
                limit=10,
            )

        # No keyword -> newest first, and the snippet is still the head of the
        # body. Relevance ordering has nothing to rank on here.
        self.assertEqual(
            [row["sent_at"] for row in rows],
            ["2026-08-14T09:00:00+08:00", "2026-07-24T09:00:00+08:00"],
        )
        self.assertTrue(str(rows[0]["snippet"]).startswith("Hi Carol,"))
        self.assertIsNone(rows[0]["match_field"])
        self.assertIsNone(rows[0]["match_in"])

    def test_a_stray_term_in_the_quoted_tail_does_not_demote_the_source(self) -> None:
        """The originating message usually replies to something. That enquiry
        names the product, so the quoted tail carries one of the query's
        terms while the new content carries the whole query. Rolling the
        answer up per region ("some term appears on each side") labels this
        message as carrying quoted evidence -- and the skill tells callers to
        filter on the message whose evidence is its own. It has to read
        `new`."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_db(
                Path(temp_dir),
                "stray.db",
                [
                    {
                        "subject": "RE: Folder | Desk enquiry",
                        "sender": {"name": "Carol Roe", "email": "carol@example.com"},
                        "to": [{"email": "alan@example.com"}],
                        "sent_at": "2026-07-22T13:04:00+08:00",
                        "body_text": (
                            f"Hi Alan,\n\nPrices,\n\n{_PRICE_LINE}\n\nCarol\n\n"
                            "-----Original Message-----\n"
                            "From: Alan\nSubject: Folder | Desk enquiry\n\n"
                            "Could you quote Papyra Desk PAPER-02 for the folder "
                            "programme? We need it for the spring catalog."
                        ),
                        "message_id": "<stray@example.com>",
                    }
                ],
            )
            rows = search_messages(
                database_path=database_path,
                filters=SearchFilters(keyword="PAPER-02 15"),
                limit=10,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["match_in"], "new")
        self.assertIn("15", str(rows[0]["snippet"]))

    def test_a_term_inside_a_longer_word_is_not_a_body_hit(self) -> None:
        """The index fuses runs of alphanumerics into one token, so `art`
        does not match `partial` there. A plain substring scan would say it
        does and report `match_field: body` for a row that actually matched
        on its recipients -- pointing the snippet at an unrelated word."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_db(
                Path(temp_dir),
                "boundary.db",
                [
                    {
                        "subject": "Weekly update",
                        "sender": {"name": "Carol Roe", "email": "carol@example.com"},
                        "to": [{"email": "art@example.com"}],
                        "sent_at": "2026-07-22T13:04:00+08:00",
                        "body_text": (
                            "The partial delivery was confirmed and the "
                            "particulars are attached."
                        ),
                        "message_id": "<boundary@example.com>",
                    }
                ],
            )
            rows = search_messages(
                database_path=database_path,
                filters=SearchFilters(keyword="art"),
                limit=10,
            )

        self.assertEqual(len(rows), 1)
        self.assertIsNone(
            rows[0]["match_field"],
            msg=(
                "`art` occurs only inside `partial`/`particulars`; the row "
                "matched on its recipient, and saying `body` would aim the "
                "snippet at a word the caller never asked about"
            ),
        )
        self.assertIsNone(rows[0]["match_in"])

    def test_a_quoted_part_number_locates_its_separator_variant(self) -> None:
        """The skill tells callers to phrase-quote hyphenated part numbers,
        and FTS matches that phrase against `PEN 100` as well as `PEN-100` --
        the separator is only a token boundary there. Scanning for the raw
        string would find one form and report `match_field: null` for the
        other, reproducing "it matched but I cannot see why" on the query
        form the skill actively recommends."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_db(
                Path(temp_dir),
                "partno.db",
                [
                    {
                        "subject": "Order confirmation",
                        "sender": {"name": "Carol Roe", "email": "carol@example.com"},
                        "to": [{"email": "alan@example.com"}],
                        "sent_at": "2026-07-22T13:04:00+08:00",
                        "body_text": (
                            "Hi Alan,\n\nThe order is confirmed. Please note the "
                            "item is written PEN 100 on the delivery note even "
                            "though the catalog spells it differently.\n\nCarol"
                        ),
                        "message_id": "<partno@example.com>",
                    }
                ],
            )
            rows = search_messages(
                database_path=database_path,
                filters=SearchFilters(keyword='"PEN-100"'),
                limit=10,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["match_field"], "body")
        self.assertIn("PEN 100", str(rows[0]["snippet"]))

    def test_a_subject_carrying_the_whole_query_beats_one_stray_body_term(
        self,
    ) -> None:
        """Coverage is compared before any field preference. A body hit on one
        stray term must not win over a subject carrying the whole query --
        the tool's description promises the position covering the most of
        what was asked for."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_db(
                Path(temp_dir),
                "subject.db",
                [
                    {
                        "subject": "Folder PAPER-02 quotation 15",
                        "sender": {"name": "Carol Roe", "email": "carol@example.com"},
                        "to": [{"email": "alan@example.com"}],
                        "sent_at": "2026-07-22T13:04:00+08:00",
                        "body_text": (
                            "Hi Alan,\n\nSee the attached catalog for PAPER-02. "
                            "The sample is still pending and the supplier will "
                            "confirm the delivery schedule separately.\n\nCarol"
                        ),
                        "message_id": "<subjectwin@example.com>",
                    }
                ],
            )
            rows = search_messages(
                database_path=database_path,
                filters=SearchFilters(keyword="PAPER-02 15"),
                limit=10,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["match_field"], "subject")
        self.assertIn("15", str(rows[0]["snippet"]))
        # A subject hit says nothing about quoted history.
        self.assertIsNone(rows[0]["match_in"])

    def test_an_operator_glued_to_a_parenthesis_still_yields_its_terms(self) -> None:
        """The FTS parser ends a bareword at `(` with no whitespace needed, so
        `PAPER-02 OR(15)` is legal and names two terms. Treating parentheses as
        characters to strip off the ends instead of as delimiters yields the
        term `OR(15`, which matches nothing -- and a row that hit on `15`
        would then report no evidence at all."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_db(
                Path(temp_dir),
                "parens.db",
                [
                    {
                        "subject": "Quotation",
                        "sender": {"name": "Carol Roe", "email": "carol@example.com"},
                        "to": [{"email": "alan@example.com"}],
                        "sent_at": "2026-07-22T13:04:00+08:00",
                        "body_text": (
                            "Hi Alan,\n\nThe bulk rate lands at 15 per box for "
                            "the pack size you asked about.\n\nCarol"
                        ),
                        "message_id": "<parens@example.com>",
                    }
                ],
            )
            rows = search_messages(
                database_path=database_path,
                filters=SearchFilters(keyword="PAPER-02 OR(15)"),
                limit=10,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["match_field"], "body")
        self.assertIn("15", str(rows[0]["snippet"]))

    def test_keyword_matching_only_a_recipient_reports_no_text_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_thread_fixture_db(Path(temp_dir))
            rows = search_messages(
                database_path=database_path,
                filters=SearchFilters(keyword="zzqrecipient"),
                limit=10,
            )

        # `message_fts` also indexes recipients and attachment names, so a row
        # can match with the literal absent from subject and body. Saying so
        # is the point: a null here tells the caller the hit is not in the text
        # rather than leaving them to wonder why the snippet looks unrelated.
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["match_field"])
        self.assertIsNone(rows[0]["match_in"])
        self.assertTrue(str(rows[0]["snippet"]).startswith("Order confirmed"))


if __name__ == "__main__":
    unittest.main()

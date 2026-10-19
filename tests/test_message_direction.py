"""`direction` field on search/thread/message output (issue #169).

Covers what the ticket promises: `compute_direction` itself (unit level,
including the red-green tripwire for the dirty `Display Name<email>` sender
shape), that `search_messages` / `get_message` (and therefore `walk_thread`,
which is built on `get_message`) actually carry the field end to end, and
the external contract the ticket names explicitly: `bes search --json` and
`bes thread --format json` each carry `direction` on every item with a
closed value domain of {outbound, inbound, null}.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from mailattic.cli import main
from mailattic.identity import add_self_address, compute_direction
from mailattic.models import PstSource, build_normalized_message
from mailattic.query import (
    SearchFilters,
    get_message,
    search_messages,
    walk_thread,
)
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

_SOURCE = PstSource(
    path=Path("C:/mail/archive.pst"),
    input_mode="file",
    requested_from=Path("C:/mail/archive.pst"),
)

_SELF = frozenset({"sean3808@mailattic.test"})

# The closed value domain the ticket names (issue #166 Testing Decisions /
# issue #169 acceptance criteria): "值域 {outbound, inbound, null}".
_DIRECTION_DOMAIN = {"outbound", "inbound", None}


def _seed(
    db: Path,
    *,
    subject: str,
    sender_email: str | None,
    message_id: str,
    locator: str,
    in_reply_to: str | None = None,
) -> int:
    connection = connect_database(db)
    try:
        add_self_address(connection, "sean3808@mailattic.test")
        raw_message: dict[str, object] = {
            "subject": subject,
            "sender": {"email": sender_email},
            "to": [{"email": "someone@example.com"}],
            "sent_at": "2024-03-01T10:00:00",
            "body_text": "body",
            "message_id": message_id,
        }
        if in_reply_to is not None:
            raw_message["in_reply_to"] = in_reply_to
        message = build_normalized_message(
            raw_message,
            source=_SOURCE,
            folder_path="Inbox",
            logical_locator=locator,
        )
        stored_id = storage_persist_message(connection, message).stored_message_id
        connection.commit()
        return stored_id
    finally:
        connection.close()


def _run_cli(*argv: str) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(list(argv))
    return code, buffer.getvalue()


class ComputeDirectionUnitTests(unittest.TestCase):
    """The four shapes the ticket's acceptance criteria names."""

    def test_self_sender_is_outbound(self) -> None:
        self.assertEqual(
            compute_direction("sean3808@mailattic.test", _SELF), "outbound"
        )

    def test_self_sender_case_insensitive(self) -> None:
        self.assertEqual(
            compute_direction("Sean3808@Mailattic.TEST", _SELF), "outbound"
        )

    def test_non_self_sender_is_inbound(self) -> None:
        self.assertEqual(compute_direction("alice@example.com", _SELF), "inbound")

    def test_null_sender_is_null(self) -> None:
        self.assertIsNone(compute_direction(None, _SELF))
        self.assertIsNone(compute_direction("", _SELF))
        self.assertIsNone(compute_direction("   ", _SELF))

    def test_dirty_name_angle_email_value_is_extracted_and_classified(self) -> None:
        """Stored shape with no space before the angle bracket:
        `sean3808<sean3808@mailattic.test>`."""
        self.assertEqual(
            compute_direction("sean3808<sean3808@mailattic.test>", _SELF), "outbound"
        )

    def test_dirty_value_from_a_non_self_sender_is_inbound(self) -> None:
        self.assertEqual(
            compute_direction("Alice<alice@example.com>", _SELF), "inbound"
        )

    def test_dirty_value_needs_extraction_not_plain_lowercasing(self) -> None:
        """Contrast test for the normalisation (issue #169).

        This one always passes -- it is not the red-green tripwire its old
        name claimed to be. It pins *why* the extraction is needed by
        asserting the naive comparison would miss, next to the real call
        that does not. The actual red observation belongs to
        `test_dirty_name_angle_email_value_is_extracted_and_classified`,
        which goes red when `extract_address` is removed.

        Without extracting the angle-bracket email, the naive
        `strip().lower()` comparison the ticket explicitly warns against
        never matches `_SELF`, which is the whole reason the extraction
        exists.
        """
        dirty = "sean3808<sean3808@mailattic.test>"
        naive = dirty.strip().lower()
        self.assertNotIn(naive, _SELF)  # naive comparison would misclassify
        self.assertEqual(
            compute_direction(dirty, _SELF), "outbound"
        )  # real fix does not


class SearchMessagesDirectionTests(unittest.TestCase):
    def test_search_messages_carries_direction_for_self_and_non_self(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = initialize_database(Path(tmp) / "test.db")
            _seed(
                db,
                subject="outbound one",
                sender_email="sean3808@mailattic.test",
                message_id="<out-1@test>",
                locator="Inbox/0",
            )
            _seed(
                db,
                subject="inbound one",
                sender_email="alice@example.com",
                message_id="<in-1@test>",
                locator="Inbox/1",
            )

            results = search_messages(
                database_path=db,
                filters=SearchFilters(keyword=None),
                limit=10,
            )

        by_subject = {r["subject"]: r["direction"] for r in results}
        self.assertEqual(by_subject["outbound one"], "outbound")
        self.assertEqual(by_subject["inbound one"], "inbound")

    def test_search_messages_direction_null_when_sender_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = initialize_database(Path(tmp) / "test.db")
            _seed(
                db,
                subject="no sender",
                sender_email=None,
                message_id="<no-sender@test>",
                locator="Inbox/0",
            )

            results = search_messages(
                database_path=db,
                filters=SearchFilters(keyword=None),
                limit=10,
            )

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["direction"])


class GetMessageAndThreadDirectionTests(unittest.TestCase):
    def test_get_message_carries_direction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = initialize_database(Path(tmp) / "test.db")
            stored_id = _seed(
                db,
                subject="self sent",
                sender_email="sean3808@mailattic.test",
                message_id="<self-1@test>",
                locator="Inbox/0",
            )

            message = get_message(db, stored_id)

        self.assertEqual(message["direction"], "outbound")

    def test_walk_thread_carries_direction_per_message(self) -> None:
        """Real two-message thread (Gap 2, preferred route).

        The reply's `in_reply_to` matches the first message's `message_id`,
        which `_assign_thread_key_for_new_message` (storage.py) resolves at
        insert time into a shared `thread_key` -- the same connected
        component `walk_thread` reads back. This replaces the old fixture,
        whose two rows never shared a `thread_key` and whose test body said
        so in a comment while only asserting the seed message's own
        direction. Mixed senders (non-self then self) so both outbound and
        inbound values are exercised across the same thread.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = initialize_database(Path(tmp) / "test.db")
            first_id = _seed(
                db,
                subject="thread start",
                sender_email="alice@example.com",
                message_id="<thread-1@test>",
                locator="Inbox/0",
            )
            second_id = _seed(
                db,
                subject="thread reply",
                sender_email="sean3808@mailattic.test",
                message_id="<thread-2@test>",
                locator="Inbox/1",
                in_reply_to="<thread-1@test>",
            )

            messages = walk_thread(db, first_id)

        # Confirm the fixture actually formed one connected component
        # before trusting the direction assertions below -- if this goes
        # red, the rest of the test would silently be checking a
        # singleton, not a thread.
        self.assertEqual({m["id"] for m in messages}, {first_id, second_id})

        directions = {m["subject"]: m["direction"] for m in messages}
        self.assertEqual(directions["thread start"], "inbound")
        self.assertEqual(directions["thread reply"], "outbound")
        self.assertEqual(set(directions.values()), {"inbound", "outbound"})


class CliJsonContractTests(unittest.TestCase):
    """External behaviour: `bes search --json` and `bes thread --format json`.

    Issue #166 Testing Decisions: "JSON 契約測試驗欄位存在與值域" and "外部行為
    優先：測 CLI JSON 輸出 ... 不測內部函式細節". Drives the real `main()`
    entry point so the assertion is on what a caller actually receives on
    stdout -- the JSON serialisation path -- not a re-implementation of it.
    """

    def test_search_json_direction_present_with_closed_value_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            initialize_database(db)
            _seed(
                db,
                subject="cli outbound",
                sender_email="sean3808@mailattic.test",
                message_id="<cli-out@test>",
                locator="Inbox/0",
            )
            _seed(
                db,
                subject="cli inbound",
                sender_email="alice@example.com",
                message_id="<cli-in@test>",
                locator="Inbox/1",
            )
            _seed(
                db,
                subject="cli null",
                sender_email=None,
                message_id="<cli-null@test>",
                locator="Inbox/2",
            )

            code, out = _run_cli("search", "--json", "--database", str(db))

        self.assertEqual(code, 0)
        results = json.loads(out)
        self.assertEqual(len(results), 3)
        for item in results:
            self.assertIn("direction", item)
            self.assertIn(item["direction"], _DIRECTION_DOMAIN)
        # Not just present -- the domain is actually exercised, not
        # collapsed to a single value (e.g. always null / always outbound).
        self.assertEqual({item["direction"] for item in results}, _DIRECTION_DOMAIN)

    def test_thread_json_direction_present_with_closed_value_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            initialize_database(db)
            first_id = _seed(
                db,
                subject="cli thread start",
                sender_email="alice@example.com",
                message_id="<cli-thread-1@test>",
                locator="Inbox/0",
            )
            _seed(
                db,
                subject="cli thread reply",
                sender_email="sean3808@mailattic.test",
                message_id="<cli-thread-2@test>",
                locator="Inbox/1",
                in_reply_to="<cli-thread-1@test>",
            )

            code, out = _run_cli(
                "thread", str(first_id), "--format", "json", "--database", str(db)
            )

        self.assertEqual(code, 0)
        messages = json.loads(out)
        self.assertEqual(len(messages), 2)
        for item in messages:
            self.assertIn("direction", item)
            self.assertIn(item["direction"], _DIRECTION_DOMAIN)
        self.assertEqual(
            {item["direction"] for item in messages}, {"inbound", "outbound"}
        )


if __name__ == "__main__":
    unittest.main()

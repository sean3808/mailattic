"""`mail_search` attachment-locator trip-wires (issue #178).

Two properties, each with its own failure mode:

1. `mail_search` result items carry attachment *identity* (id, name, mime,
   size) but not the *locators* (`content_hash`, `storage_path`) -- the tool
   finds candidate messages; `mail_show` and `mail_attachments` are what
   locate the bytes, and they still carry both fields.
2. Dropping the locators is what makes a 30-message summary fit the delivery
   budget. The budget test is written as a *paired* comparison -- the same
   payload with the locators put back must exceed the budget -- because a
   one-sided "still under budget" assertion stays green when the fields come
   back (mailattic/PC-096: a guard whose removal does not turn
   the test red is not a guard).

The oracle that re-adds the locators regenerates them from this file's own
fixture generator, never from the production shaping code, so the two sides
of the comparison do not derive from one call.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

import mcp.types as mcp_types

from mailattic.mcp_delivery import (
    MCP_RESPONSE_BUDGET_CHARS,
    build_call_tool_result,
    measure_call_tool_result_chars,
)
from mailattic.mcp_server import create_server
from mailattic.models import PstSource, build_normalized_message
from mailattic.query import SNIPPET_MAX_CHARS
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

#: Synthetic shape matching the report this issue came from: a signature
#: block's worth of inline images on every message in a long reply chain.
_MESSAGE_COUNT = 30
_ATTACHMENTS_PER_MESSAGE = 5

_SENDER = "carol@example.com"


def _attachment_locators(message_index: int, attachment_index: int) -> tuple[str, str]:
    """Content hash and CAS path for one fixture attachment.

    A real `content_hash` is 64 hex characters and the `storage_path` embeds
    it a second time -- the duplication is the point, so the fixture
    reproduces it exactly rather than using a short stand-in.
    """
    digest = hashlib.sha256(f"{message_index}:{attachment_index}".encode()).hexdigest()
    return digest, f"attachments/{digest[:2]}/{digest[2:4]}/{digest}.png"


def _fixture_attachments(message_index: int) -> list[dict[str, object]]:
    attachments: list[dict[str, object]] = []
    for attachment_index in range(_ATTACHMENTS_PER_MESSAGE):
        digest, storage_path = _attachment_locators(message_index, attachment_index)
        attachments.append(
            {
                "attachment_name": f"image{attachment_index + 1:03d}.png",
                "content_hash": digest,
                "size_bytes": 9700 + attachment_index,
                "mime_type": "image/png",
                "storage_path": storage_path,
            }
        )
    return attachments


def _create_budget_fixture_db(root: Path) -> Path:
    database_path = initialize_database(root / "attachment-budget.db")
    source = PstSource(
        path=Path("C:/mail/archive.pst"),
        input_mode="file",
        requested_from=Path("C:/mail/archive.pst"),
    )
    connection = connect_database(database_path)
    try:
        for index in range(_MESSAGE_COUNT):
            raw = {
                "subject": (
                    f"RE: Folder | Desk enquiry - Introduction and follow-up "
                    f"({index + 1})"
                ),
                "sender": {"name": "Carol", "email": _SENDER},
                "to": [{"email": "reader@example.com"}],
                "sent_at": f"2026-07-{(index % 28) + 1:02d}T09:00:00+08:00",
                # The keyword sits in the MIDDLE, not at the head: a hit at
                # offset 0 yields a half-window shorter than the 160-char
                # head-anchored snippet, and the keyword-path budget test
                # below would then compare two identical payloads and pass
                # for the wrong reason.
                "body_text": (
                    ("quoted history line. " * 100)
                    + "budgetkeyword "
                    + ("later history line. " * 100)
                ),
                "message_id": f"<budget-{index}@example.com>",
                "attachments": _fixture_attachments(index),
            }
            message = build_normalized_message(
                raw,
                source=source,
                folder_path="Inbox",
                logical_locator=f"Inbox/{index + 1}",
            )
            storage_persist_message(connection, message)
        connection.commit()
    finally:
        connection.close()
    return database_path


class MailSearchAttachmentBudgetTests(unittest.TestCase):
    def _call_tool(
        self, server, name: str, arguments: dict[str, object]
    ) -> mcp_types.CallToolResult:
        result = asyncio.run(server.call_tool(name, arguments))
        assert isinstance(result, mcp_types.CallToolResult)
        return result

    def _structured_list(self, delivered: mcp_types.CallToolResult) -> list[dict]:
        structured = delivered.structured_content
        assert structured is not None
        return structured["result"]

    def test_search_items_drop_attachment_locators_while_show_keeps_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_budget_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            search_rows = self._structured_list(
                self._call_tool(server, "mail_search", {"sender": _SENDER, "limit": 3})
            )
            shown = self._call_tool(server, "mail_show", {"message_id": 1})

        self.assertEqual(len(search_rows), 3)
        for row in search_rows:
            self.assertTrue(row["attachments"])
            for attachment in row["attachments"]:
                self.assertEqual(
                    set(attachment),
                    {"id", "attachment_name", "size_bytes", "mime_type"},
                )

        # The locators are dropped from `mail_search` only. Deleting them
        # everywhere would strand every caller that needs to fetch bytes, so
        # `mail_show` is asserted in the same test rather than a separate one.
        shown_structured = shown.structured_content
        assert shown_structured is not None
        for attachment in shown_structured["attachments"]:
            self.assertEqual(
                set(attachment),
                {
                    "id",
                    "attachment_name",
                    "content_hash",
                    "size_bytes",
                    "mime_type",
                    "storage_path",
                },
            )

    def test_thirty_message_summary_fits_budget_only_without_locators(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_budget_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            delivered = self._call_tool(
                server,
                "mail_search",
                {"sender": _SENDER, "limit": _MESSAGE_COUNT},
            )
            rows = self._structured_list(delivered)
            tool = server._tool_manager.get_tool("mail_search")
            assert tool is not None
            with_locators = build_call_tool_result(_restore_locators(rows), tool=tool)

        self.assertEqual(len(rows), _MESSAGE_COUNT)
        without = measure_call_tool_result_chars(delivered)
        with_ = measure_call_tool_result_chars(with_locators)

        self.assertLessEqual(
            without,
            MCP_RESPONSE_BUDGET_CHARS,
            msg=f"{_MESSAGE_COUNT} summaries must fit the budget: {without} chars",
        )
        # The paired half: putting the two locator fields back must push the
        # very same result over. Without this the assertion above would stay
        # green with the fields present, which is the state the issue reports.
        self.assertGreater(
            with_,
            MCP_RESPONSE_BUDGET_CHARS,
            msg=(
                "restoring content_hash/storage_path must exceed the budget, "
                f"otherwise this test cannot detect their return: {with_} chars"
            ),
        )


class KeywordPayloadBudgetTests(unittest.TestCase):
    """The keyword path costs more per row than the no-keyword path: its
    snippet is hit-centred and capped higher (300 vs 160), and it carries two
    extra scalars (issue #177). The budget test above exercises the
    no-keyword path only, so nothing was holding that growth to a bound --
    which is what this asserts, as a ratio rather than an absolute, so it
    stays meaningful as the fixture changes."""

    #: Ceiling on keyword-path cost relative to the same rows without a
    #: keyword. Measured at 1.08 on this fixture and ~1.11 across three
    #: queries on the live archive when this landed, so the headroom here is
    #: for fixture drift, not for another round of growth. A change that
    #: pushes past it is regrowing per-row payload and should be argued for,
    #: not discovered in production as an over-budget refusal.
    _MAX_RATIO = 1.15

    def test_keyword_path_stays_within_a_bounded_multiple_of_the_plain_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = _create_budget_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            with_keyword = asyncio.run(
                server.call_tool(
                    "mail_search",
                    {
                        "sender": _SENDER,
                        "keyword": "budgetkeyword",
                        "limit": _MESSAGE_COUNT,
                    },
                )
            )
            without_keyword = asyncio.run(
                server.call_tool(
                    "mail_search", {"sender": _SENDER, "limit": _MESSAGE_COUNT}
                )
            )

        keyed = measure_call_tool_result_chars(with_keyword)
        plain = measure_call_tool_result_chars(without_keyword)
        self.assertLessEqual(
            keyed,
            MCP_RESPONSE_BUDGET_CHARS,
            msg=f"keyword search of {_MESSAGE_COUNT} rows: {keyed} chars",
        )
        self.assertLessEqual(
            keyed / plain,
            self._MAX_RATIO,
            msg=(
                f"keyword path costs {keyed / plain:.2f}x the plain path "
                f"({keyed} vs {plain} chars), over the {self._MAX_RATIO}x bound"
            ),
        )
        # The ratio is only meaningful if the keyword path actually produced
        # WIDER snippets. A hit near the head yields a half-window shorter
        # than the head-anchored snippet, and the comparison then passes by
        # measuring two near-identical payloads. Verified once with the
        # keyword at offset 0: ratio 0.98, bound 1.25, test blind.
        keyed_rows = with_keyword.structured_content["result"]
        self.assertTrue(
            any(row["match_field"] == "body" for row in keyed_rows),
            msg="fixture produced no body hits; the ratio would be vacuous",
        )
        self.assertTrue(
            any(len(row["snippet"] or "") > SNIPPET_MAX_CHARS for row in keyed_rows),
            msg=(
                "no row exceeded the head-anchored snippet cap, so this "
                "fixture does not exercise the payload growth being bounded"
            ),
        )


def _restore_locators(rows: list[dict]) -> list[dict]:
    """Re-add the locator fields the shaping layer drops.

    Regenerated from this module's fixture generator -- deliberately not
    read back out of the shaped payload, which no longer carries them, and
    not obtained from the production shaping code. Search orders by sent_at,
    so a row's position need not match the fixture index it was generated
    from; every digest is 64 hex characters and every path is the same
    length, so the size comparison this feeds does not depend on the pairing.
    """
    restored = copy.deepcopy(rows)
    for message_index, row in enumerate(restored):
        for attachment_index, attachment in enumerate(row["attachments"]):
            digest, storage_path = _attachment_locators(message_index, attachment_index)
            attachment["content_hash"] = digest
            attachment["storage_path"] = storage_path
    return restored


if __name__ == "__main__":
    unittest.main()

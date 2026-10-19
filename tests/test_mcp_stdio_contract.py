"""Wire-level MCP contract tests: spawn the real server and speak JSON-RPC.

Every other MCP test (``tests/test_mcp_server.py``) drives the tool functions
with Python objects, so a value whose type changes in transit -- exactly what
happened when the Claude Desktop client stringified an integer ``message_id``
-- is invisible to that suite by construction. This module closes that gap by
launching ``mailattic.mcp_server`` as a real subprocess over
stdio and issuing ``tools/call`` requests through the MCP SDK's stdio client
transport, so arguments genuinely cross a JSON boundary before the server
ever sees them.

Design: ``openspec/changes/fix-mail-show-bridge-coercion/design.md`` >
"Prove the defect on the wire before fixing it", "Scope the stdio layer to
identifier round-tripping".

Scope: identifier round-tripping for ``mail_show`` and ``mail_thread`` only,
plus the smoke cases that establish the harness works and resolves the
fixture database. Payload shape, allowlists, truncation, and delivery-budget
behaviour stay with the in-process suite (design.md, same section).

The fixture database is built fresh per test class run from synthetic
messages via the same normalize-and-persist path
``tests/test_mcp_server.py`` uses (``_persist_messages``/``build_normalized_
message``/``persist_message``). No case in this module may reference the
default/production database or any production database id recorded as
evidence in the change's design doc -- every id used in an assertion is read
back from this module's own fixture, so the module passes on a checkout that
has no production database at all.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mailattic.models import PstSource, build_normalized_message
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

# Bounded so a mis-sequenced handshake or a hung tool call fails this test
# instead of hanging the whole pytest route (design.md > "A subprocess test
# can hang if the handshake is mis-sequenced, taking the route with it").
_READ_TIMEOUT_SECONDS = 20.0

# A nonce unlikely to appear in any real mailbox, used both as the smoke
# search term and as proof the server resolved *this* temporary database --
# a hit on this exact token could not come from anywhere else.
_DISTINCTIVE_KEYWORD = "zzqvortexstdiofixture7734"


def _persist_fixture_messages(
    database_path: Path, fixtures: list[dict[str, object]]
) -> list[int]:
    """Persist *fixtures* and return their stored ids, insertion order.

    Mirrors ``tests/test_mcp_server.py``'s ``_persist_messages`` helper
    (same normalize-and-persist call sequence) but also captures each
    ``PersistResult.stored_message_id`` -- this module's assertions need the
    real stored id back, not just a side effect on the connection.
    """
    source = PstSource(
        path=Path("C:/mail/archive.pst"),
        input_mode="file",
        requested_from=Path("C:/mail/archive.pst"),
    )
    connection = connect_database(database_path)
    stored_ids: list[int] = []
    try:
        for index, raw in enumerate(fixtures):
            message = build_normalized_message(
                raw,
                source=source,
                folder_path="Inbox",
                logical_locator=f"Inbox/{index + 1}",
            )
            result = storage_persist_message(connection, message)
            stored_ids.append(result.stored_message_id)
        connection.commit()
    finally:
        connection.close()
    return stored_ids


def _build_fixture_database(root: Path) -> tuple[Path, dict[str, int]]:
    """Build a temporary database with a distinctive-keyword message and a
    two-message thread, returning ``(database_path, {name: stored_id})``.
    """
    database_path = initialize_database(root / "stdio-contract-fixture.db")
    base = datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    fixtures: list[dict[str, object]] = [
        {
            "subject": "Stdio contract smoke message",
            "sender": {"email": "alice@example.com"},
            "to": [{"email": "bob@example.com"}],
            "sent_at": base.isoformat(),
            "body_text": f"fixture body containing {_DISTINCTIVE_KEYWORD}",
            "message_id": "<stdio-contract-smoke@example.com>",
        },
        {
            "subject": "Stdio contract thread root",
            "sender": {"email": "alice@example.com"},
            "to": [{"email": "bob@example.com"}],
            "sent_at": (base + timedelta(minutes=1)).isoformat(),
            "body_text": "thread root body",
            "message_id": "<stdio-contract-thread-0@example.com>",
        },
        {
            "subject": "Stdio contract thread reply",
            "sender": {"email": "bob@example.com"},
            "to": [{"email": "alice@example.com"}],
            "sent_at": (base + timedelta(minutes=2)).isoformat(),
            "body_text": "thread reply body",
            "message_id": "<stdio-contract-thread-1@example.com>",
            "in_reply_to": "<stdio-contract-thread-0@example.com>",
        },
    ]
    stored_ids = _persist_fixture_messages(database_path, fixtures)
    ids = {
        "smoke_message": stored_ids[0],
        "thread_root": stored_ids[1],
        "thread_reply": stored_ids[2],
    }
    return database_path, ids


@asynccontextmanager
async def _stdio_session(database_path: Path):
    """Spawn the real MCP server over stdio, handshake, yield a live session.

    Launches ``uv run``'s own interpreter (``sys.executable``) running
    ``python -m mailattic.mcp_server --database <path>`` --
    verified directly against this checkout's venv (the ``bes-mcp`` console
    script resolves to the same entry point; ``-m`` was used here because it
    needs no separate resolution of the installed script's location).
    ``MAILATTIC_DB`` is defensively cleared from the child's environment
    so a set/stale value can never override the explicit ``--database`` flag
    this module relies on for isolation from the production database.

    Teardown (subprocess terminate-then-kill, stream close) is owned by
    ``stdio_client``'s and ``ClientSession``'s own ``__aexit__`` -- both are
    entered as ``async with`` here, so it runs whether the caller's body
    raises or returns normally.
    """
    env = dict(os.environ)
    env.pop("MAILATTIC_DB", None)
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "mailattic.mcp_server",
            "--database",
            str(database_path),
        ],
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(
            read_stream, write_stream, read_timeout_seconds=_READ_TIMEOUT_SECONDS
        ) as session,
    ):
        await session.initialize()
        yield session


async def _call_tool(
    session: ClientSession, name: str, arguments: dict[str, object]
) -> mcp_types.CallToolResult:
    result = await session.call_tool(
        name, arguments, read_timeout_seconds=_READ_TIMEOUT_SECONDS
    )
    assert isinstance(result, mcp_types.CallToolResult), (
        f"{name}: call_tool returned {type(result).__name__}, not a "
        "CallToolResult -- InputRequiredResult is never expected from these "
        "read-only tools"
    )
    return result


def _error_text(result: mcp_types.CallToolResult) -> str:
    if not result.content:
        return ""
    first = result.content[0]
    return getattr(first, "text", "")


def _structured_payload(result: mcp_types.CallToolResult) -> Any:
    """Unwrap the tool-manager's ``{"result": ...}`` structured-content
    envelope the way ``tests/test_mcp_server.py``'s
    ``_unwrap_tool_manager_result`` does, for a successful result.
    """
    structured = result.structured_content
    if structured is None:
        return result
    if set(structured) == {"result"}:
        return structured["result"]
    return structured


class StdioContractTests(unittest.TestCase):
    """One subprocess per test: slower than the in-process suite by design
    (design.md > "The stdio test spawns a subprocess and is slower and more
    failure-prone than the in-process suite"), scoped small in exchange.
    """

    _tempdir: tempfile.TemporaryDirectory[str]
    database_path: Path
    ids: dict[str, int]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tempdir = tempfile.TemporaryDirectory()
        cls.database_path, cls.ids = _build_fixture_database(Path(cls._tempdir.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tempdir.cleanup()

    def _run(self, scenario):
        async def _wrapped():
            async with _stdio_session(self.database_path) as session:
                return await scenario(session)

        return asyncio.run(_wrapped())

    # -- Task 1.1: smoke -----------------------------------------------

    def test_mail_search_smoke_finds_the_fixture_message(self) -> None:
        """A small-``limit`` ``mail_search`` call over the wire finds the
        synthetic fixture message by its distinctive keyword, and the
        payload content matches the fixture exactly -- proving both that the
        harness works end to end and that the server resolved *this*
        temporary database (the distinctive keyword and stored id could not
        have come from anywhere else). Also satisfies task 1.1's second
        verification target ("a case asserting the resolved database path is
        the temporary one") without referencing a production database that
        may not exist on this checkout.
        """

        async def scenario(session: ClientSession) -> mcp_types.CallToolResult:
            return await _call_tool(
                session,
                "mail_search",
                {"keyword": _DISTINCTIVE_KEYWORD, "limit": 5},
            )

        result = self._run(scenario)
        self.assertFalse(result.is_error, _error_text(result))
        items = _structured_payload(result)
        self.assertEqual(len(items), 1, f"expected exactly one hit, got: {items!r}")
        self.assertEqual(items[0]["id"], self.ids["smoke_message"])
        self.assertEqual(items[0]["subject"], "Stdio contract smoke message")
        self.assertIn(_DISTINCTIVE_KEYWORD, items[0]["snippet"])

    # -- Task 1.2: identifier round-tripping ----------------------------

    def test_mail_show_identifier_round_trip(self) -> None:
        """``mail_show``'s ``message_id`` called with the stored integer id
        and with its ASCII decimal string form MUST return the same record.

        This is expected to FAIL against the unfixed tool body: the string
        call currently raises the blanket "mail_show only accepts an integer
        stored id" redirect (task 1.3's red-first evidence). A green run
        here is the fix's own verification target (task 2.1).
        """

        async def scenario(session: ClientSession):
            int_result = await _call_tool(
                session, "mail_show", {"message_id": self.ids["smoke_message"]}
            )
            str_result = await _call_tool(
                session,
                "mail_show",
                {"message_id": str(self.ids["smoke_message"])},
            )
            return int_result, str_result

        int_result, str_result = self._run(scenario)
        self.assertFalse(int_result.is_error, _error_text(int_result))
        self.assertFalse(
            str_result.is_error,
            "mail_show rejected the ASCII decimal string form of a stored id "
            "it accepted as an integer -- this is the defect this module "
            f"exists to prove. Server response: {_error_text(str_result)}",
        )
        self.assertEqual(
            _structured_payload(int_result), _structured_payload(str_result)
        )

    def test_mail_thread_identifier_round_trip(self) -> None:
        """``mail_thread``'s ``identifier`` already accepts a numeric string
        (design.md > "the `mail_thread` round-trip case passes -- that tool
        already accepts numeric strings"), so this case is expected to pass
        today and serves as the contrast to the failing ``mail_show`` case.
        """

        async def scenario(session: ClientSession):
            int_result = await _call_tool(
                session, "mail_thread", {"identifier": self.ids["thread_root"]}
            )
            str_result = await _call_tool(
                session,
                "mail_thread",
                {"identifier": str(self.ids["thread_root"])},
            )
            return int_result, str_result

        int_result, str_result = self._run(scenario)
        self.assertFalse(int_result.is_error, _error_text(int_result))
        self.assertFalse(str_result.is_error, _error_text(str_result))
        self.assertEqual(
            _structured_payload(int_result), _structured_payload(str_result)
        )
        payload = _structured_payload(int_result)
        returned_ids = {message["id"] for message in payload["messages"]}
        self.assertEqual(
            returned_ids, {self.ids["thread_root"], self.ids["thread_reply"]}
        )


if __name__ == "__main__":
    unittest.main()

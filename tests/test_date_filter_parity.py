from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

import mailattic.mcp_delivery as mcp_delivery_module
from mailattic.cli import main
from mailattic.contact_history import aggregate_contact_history
from mailattic.datetime_bounds import TAIPEI
from mailattic.identity import add_self_address
from mailattic.mcp_server import create_server
from mailattic.models import PstSource, build_normalized_message
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

_SOURCE = PstSource(
    path=Path("C:/mail/archive.pst"),
    input_mode="file",
    requested_from=Path("C:/mail/archive.pst"),
)

# Each entry: (raw_input, expected_lower_bound, expected_upper_bound)
_PARITY_CASES: list[tuple[str, datetime, datetime]] = [
    (
        "2026-07-01T00:00:00Z",
        datetime(2026, 7, 1, 8, 0, 0, tzinfo=TAIPEI),
        datetime(2026, 7, 1, 8, 0, 0, tzinfo=TAIPEI),
    ),
    (
        "2026-07-01T09:00:00+08:00",
        datetime(2026, 7, 1, 9, 0, 0, tzinfo=TAIPEI),
        datetime(2026, 7, 1, 9, 0, 0, tzinfo=TAIPEI),
    ),
    (
        "2026-07-01T09:00:00",
        datetime(2026, 7, 1, 9, 0, 0, tzinfo=TAIPEI),
        datetime(2026, 7, 1, 9, 0, 0, tzinfo=TAIPEI),
    ),
    (
        "2026-07-01",
        datetime(2026, 7, 1, 0, 0, 0, tzinfo=TAIPEI),
        datetime(2026, 7, 1, 23, 59, 59, tzinfo=TAIPEI),
    ),
    (
        "20260721",
        datetime(2026, 7, 21, 0, 0, 0, tzinfo=TAIPEI),
        datetime(2026, 7, 21, 23, 59, 59, tzinfo=TAIPEI),
    ),
    (
        "2026-W30-2",
        datetime(2026, 7, 21, 0, 0, 0, tzinfo=TAIPEI),
        datetime(2026, 7, 21, 23, 59, 59, tzinfo=TAIPEI),
    ),
]


def _build_parity_db(db_path: Path) -> None:
    initialize_database(db_path)
    conn = connect_database(db_path)
    try:
        add_self_address(conn, "sean3808@mailattic.test")
        # Unique boundary instants across all _PARITY_CASES (with -1s, 0s, +1s)
        instants: set[datetime] = set()
        for _, lower, upper in _PARITY_CASES:
            for anchor in (lower, upper):
                instants.add(anchor - timedelta(seconds=1))
                instants.add(anchor)
                instants.add(anchor + timedelta(seconds=1))

        for idx, instant in enumerate(sorted(instants), start=1):
            msg = build_normalized_message(
                {
                    "subject": f"Msg {instant.isoformat()}",
                    "sender": {"email": "alice@parity.example"},
                    "to": [{"email": "sean3808@mailattic.test"}],
                    "sent_at": instant.isoformat(),
                    "body_text": "parity body",
                    "message_id": f"<parity-{idx}@parity.example>",
                },
                source=_SOURCE,
                folder_path="Inbox",
                logical_locator=f"Inbox/{idx}",
            )
            storage_persist_message(conn, msg)
        conn.commit()
    finally:
        conn.close()


def _run_cli_json(argv: list[str]) -> dict[str, object]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(argv)
    if code != 0:
        raise AssertionError(f"CLI {argv!r} exited with {code}")
    return json.loads(stdout.getvalue())


def _call_mcp_tool(server: object, name: str, arguments: dict[str, object]) -> object:
    result = asyncio.run(server.call_tool(name, arguments))  # type: ignore[attr-defined]
    if getattr(result, "is_error", False):
        raise AssertionError(f"MCP {name}({arguments!r}) failed: {result}")
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        if isinstance(structured, dict):
            return structured.get("result", structured)
        return structured
    return json.loads(result.content[0].text)


class DateFilterParityTests(unittest.TestCase):
    def test_all_surfaces_produce_identical_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "parity.db"
            _build_parity_db(db_path)
            server = create_server(database_path=db_path)

            conn = connect_database(db_path)
            try:
                all_epochs = [
                    row["sent_at"]
                    for row in conn.execute("SELECT sent_at FROM messages").fetchall()
                ]
            finally:
                conn.close()

            for raw_input, expected_lower, expected_upper in _PARITY_CASES:
                for bound_kind in ("lower", "upper"):
                    with self.subTest(input=raw_input, bound=bound_kind):
                        if bound_kind == "lower":
                            expected_count = sum(
                                1
                                for ts in all_epochs
                                if datetime.fromisoformat(ts) >= expected_lower
                            )
                            search_args = ["--from-date", raw_input]
                            ch_cli_args = ["--date-from", raw_input]
                            ch_api_kwargs: dict[str, str] = {"date_from": raw_input}
                            mcp_args: dict[str, object] = {
                                "sender": "alice@parity.example",
                                "from_date": raw_input,
                                "limit": 50,
                            }
                        else:
                            expected_count = sum(
                                1
                                for ts in all_epochs
                                if datetime.fromisoformat(ts) <= expected_upper
                            )
                            search_args = ["--to-date", raw_input]
                            ch_cli_args = ["--date-to", raw_input]
                            ch_api_kwargs = {"date_to": raw_input}
                            mcp_args = {
                                "sender": "alice@parity.example",
                                "to_date": raw_input,
                                "limit": 50,
                            }

                        bes_search_res = _run_cli_json(
                            [
                                "search",
                                "--sender",
                                "alice@parity.example",
                                *search_args,
                                "--limit",
                                "50",
                                "--json",
                                "--database",
                                str(db_path),
                            ]
                        )
                        bes_search_count = len(bes_search_res)  # type: ignore[arg-type]

                        mcp_res = _call_mcp_tool(server, "mail_search", mcp_args)
                        mcp_count = len(mcp_res)  # type: ignore[arg-type]

                        ch_cli_res = _run_cli_json(
                            [
                                "contact-history",
                                "--domain",
                                "parity.example",
                                *ch_cli_args,
                                "--format",
                                "json",
                                "--database",
                                str(db_path),
                            ]
                        )
                        ch_cli_count = ch_cli_res["total_messages"]

                        ch_api_res = aggregate_contact_history(
                            database_path=db_path,
                            domain="parity.example",
                            **ch_api_kwargs,
                        )
                        ch_api_count = ch_api_res["total_messages"]

                        self.assertEqual(bes_search_count, expected_count)
                        self.assertEqual(mcp_count, bes_search_count)
                        self.assertEqual(ch_cli_count, bes_search_count)
                        self.assertEqual(ch_api_count, bes_search_count)

    def test_mail_contact_history_over_budget_timestamp_range_is_narrowable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "budget.db"
            _build_parity_db(db_path)
            server = create_server(database_path=db_path)

            with patch.object(mcp_delivery_module, "MCP_RESPONSE_BUDGET_CHARS", 100):
                # Sub-day timestamp range: narrowable (form 2), not terminal
                with self.assertRaises(ToolError) as subday_ctx:
                    asyncio.run(
                        server.call_tool(
                            "mail_contact_history",
                            {
                                "domain": "parity.example",
                                "date_from": "2026-07-01T08:00:00+08:00",
                                "date_to": "2026-07-01T10:00:00+08:00",
                            },
                        )
                    )
                subday_text = str(subday_ctx.exception)
                self.assertIn("Narrow the query", subday_text)
                self.assertNotIn("narrowest range this tool accepts", subday_text)

                # Same-instant timestamp range: terminal (form 3)
                with self.assertRaises(ToolError) as same_ctx:
                    asyncio.run(
                        server.call_tool(
                            "mail_contact_history",
                            {
                                "domain": "parity.example",
                                "date_from": "2026-07-01T00:00:00Z",
                                "date_to": "2026-07-01T08:00:00+08:00",
                            },
                        )
                    )
                same_text = str(same_ctx.exception)
                self.assertIn("narrowest range this tool accepts", same_text)

"""The ``mail_send`` success receipt is measured against the response budget (#299).

Drives the real MCP ``mail_send`` with an in-process SMTP double (no mail is
sent). A partial-refusal receipt used to list every refused recipient and
reason verbatim, so 180 refusals produced ~204k characters against the 120,000
contract. The receipt must stay a success -- the mail is already out -- and keep
its outcome, counts, ids and the do-not-resend instruction.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from mailattic.mcp_delivery import (
    MCP_RESPONSE_BUDGET_CHARS,
    build_call_tool_result,
    measure_call_tool_result_chars,
)
from mailattic.outbound import _read_marker

from . import test_mcp_send_error_budget as base
from .test_mcp_send_error_budget import Ctx, _call

MAX_REASON = "r" * 300


ctx = base.ctx  # reuse the sibling file's real-server fixture


def _refusals(count: int, reason: str = MAX_REASON) -> dict[str, tuple[int, bytes]]:
    return {
        f"{'u' * 50}{i:05d}@{'h' * 20}.example.com": (550, reason.encode())
        for i in range(count)
    }


def _send(c: Ctx):
    return _call(
        c.server, "mail_send", {"draft_id": c.draft_id, "fingerprint": c.fingerprint}
    )


@pytest.mark.parametrize("count", [180, 1000, 5000])
def test_many_refusals_stay_within_budget_as_a_partial_success(
    ctx: Ctx, count: int
) -> None:
    ctx.smtp.sendmail.return_value = _refusals(count)

    res = _send(ctx)

    assert not res.is_error
    assert measure_call_tool_result_chars(res) <= MCP_RESPONSE_BUDGET_CHARS
    sc = res.structured_content
    assert sc is not None
    assert sc["status"] == "partial"
    assert sc["response_truncated"] is True
    assert sc["refused_count"] == count
    assert sc["accepted_count"] == len(sc["accepted"]) == 2
    assert sc["message_id"] == ctx.message_id
    assert sc["draft_id"] == ctx.draft_id
    assert "do not resend" in sc["warning"].lower()
    assert "do not treat the listed refused addresses" in sc["warning"]
    ctx.smtp.sendmail.assert_called_once()
    marker = _read_marker(ctx.db_path, ctx.message_id)
    assert marker is not None
    assert marker["status"] == "sent"
    assert len(marker["refused"]) == count  # full list is recoverable here


def test_worst_case_receipt_with_long_close_warning_fits(ctx: Ctx) -> None:
    with patch(
        "mailattic.outbound._smtp_submit",
        return_value=(
            [
                {"address": a, "code": c, "reason": r.decode()}
                for a, (c, r) in _refusals(5000).items()
            ],
            "q" * 3_000,
        ),
    ):
        res = _send(ctx)

    assert not res.is_error
    assert measure_call_tool_result_chars(res) <= MCP_RESPONSE_BUDGET_CHARS
    sc = res.structured_content
    assert sc is not None
    assert sc["status"] == "partial"
    assert "Do NOT resend" in sc["close_warning"]


def _expected(payload: dict[str, object], ctx: Ctx) -> tuple[str, object]:
    tool = ctx.server._tool_manager.get_tool("mail_send")  # type: ignore[attr-defined]
    built = build_call_tool_result(payload, tool=tool)
    return built.content[0].text, built.structured_content  # type: ignore[union-attr]


def test_all_accepted_receipt_is_unchanged(ctx: Ctx) -> None:
    ctx.smtp.sendmail.return_value = {}

    res = _send(ctx)

    expected = {
        "status": "sent",
        "message_id": ctx.message_id,
        "draft_id": ctx.draft_id,
    }
    assert (res.content[0].text, res.structured_content) == _expected(expected, ctx)  # type: ignore[union-attr]


def test_few_refusals_receipt_is_unchanged(ctx: Ctx) -> None:
    refusals = _refusals(3, "mailbox unavailable")
    ctx.smtp.sendmail.return_value = refusals

    res = _send(ctx)

    sc = res.structured_content
    assert sc is not None
    expected = {
        "status": "partial",
        "message_id": ctx.message_id,
        "draft_id": ctx.draft_id,
        "accepted": sc["accepted"],
        "refused": [
            {"address": a, "code": c, "reason": r.decode()}
            for a, (c, r) in refusals.items()
        ],
        "warning": sc["warning"],
    }
    assert "response_truncated" not in sc
    assert "shortened to fit" not in sc["warning"]
    assert (res.content[0].text, res.structured_content) == _expected(expected, ctx)  # type: ignore[union-attr]
    assert json.loads(res.content[0].text) == expected  # type: ignore[union-attr]

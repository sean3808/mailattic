from __future__ import annotations

import functools
import string
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import Tool
from mcp.types import CallToolResult

MCP_RESPONSE_BUDGET_CHARS = 120_000

#: Cap applied to every dynamic fragment an error message this server builds
#: quotes back -- a caller-supplied value it echoes, or diagnostic detail
#: from a SQLite engine failure or any other unexpected exception (design.md
#: > "Bound error results by construction rather than by measuring", the
#: "Caller echoes are capped" and "Diagnostic detail from an underlying
#: component is capped" fragment classes). Chosen small enough that no
#: combination of these fragments in a single message can approach
#: MCP_RESPONSE_BUDGET_CHARS, regardless of how large the original value was.
ERROR_ECHO_CAP_CHARS = 200
_ERROR_ECHO_ELISION_MARKER = "...(truncated"


def cap_error_echo(value: str) -> str:
    """Cap *value* at :data:`ERROR_ECHO_CAP_CHARS`, prefix preserved.

    Keeps the leading ``ERROR_ECHO_CAP_CHARS`` characters unchanged and
    appends an explicit elision marker naming the untruncated length when
    *value* exceeds the cap -- so a caller recognizes the value it sent from
    its own leading characters, at a cost the message was never carrying
    (design.md > "An echo is a presentation string, not an identifier").

    Not applied to the resolved database path in an environmental error,
    which is the one fragment class this construction admits uncapped
    (design.md > "The resolved database path is admitted uncapped, as its
    own class").
    """
    if len(value) <= ERROR_ECHO_CAP_CHARS:
        return value
    return (
        f"{value[:ERROR_ECHO_CAP_CHARS]}"
        f"{_ERROR_ECHO_ELISION_MARKER}, {len(value)} chars total)"
    )


def _render_error_template(
    template: str,
    fragments: dict[str, object],
    *,
    uncapped: frozenset[str] = frozenset(),
) -> str:
    fields: list[str] = []
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"Invalid error template: {exc}") from exc

    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name.isidentifier() or format_spec or conversion:
            raise ValueError(
                "Error templates accept only simple named placeholders without "
                "conversion or format specifiers."
            )
        fields.append(field_name)

    expected = set(fields)
    supplied = set(fragments)
    if expected != supplied:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        raise ValueError(
            "Error template fragments do not match placeholders: "
            f"missing={missing!r}, extra={extra!r}."
        )

    rendered_fragments = {
        name: str(value) if name in uncapped else cap_error_echo(str(value))
        for name, value in fragments.items()
    }
    return template.format(**rendered_fragments)


@functools.lru_cache(maxsize=32)
def _as_tool_error_type[ExceptionT: Exception](
    exception_type: type[ExceptionT],
) -> Callable[[str], ExceptionT]:
    if issubclass(exception_type, ToolError):
        return exception_type
    combined_name = f"Tool{exception_type.__name__}"
    return cast(
        Callable[[str], ExceptionT],
        type(combined_name, (ToolError, exception_type), {}),
    )


def build_tool_error[ExceptionT: Exception](
    exception_type: type[ExceptionT], template: str, **fragments: object
) -> ExceptionT:
    """Build a bounded tool error from capped named fragments."""

    target_type = _as_tool_error_type(exception_type)
    return target_type(_render_error_template(template, fragments))


def build_environment_error[ExceptionT: Exception](
    exception_type: type[ExceptionT],
    resolved_database_path: object,
    template: str,
    **fragments: object,
) -> ExceptionT:
    """Build an environmental error with only the database path uncapped."""

    if "database_path" in fragments:
        raise ValueError(
            "database_path is reserved for the resolved database path argument."
        )
    target_type = _as_tool_error_type(exception_type)
    return target_type(
        _render_error_template(
            template,
            {"database_path": resolved_database_path, **fragments},
            uncapped=frozenset({"database_path"}),
        )
    )


def _serialize_call_tool_result(delivered: CallToolResult) -> str:
    """Measure the assembled result as an UPPER BOUND on the stdio wire.

    This is deliberately not a mirror of the wire, and describing it as one
    would be false (spec > "The delivered response is measured at the
    assembly boundary"). On the mcp 2.x generation the outbound path is:
    the runner dumps the result with ``by_alias=True, mode="json",
    exclude_none=True``, then applies a **per-protocol-version sieve** that
    re-validates against the negotiated version's surface model and drops
    every field that version does not carry, and only then does the stdio
    transport serialize the JSON-RPC envelope.

    A stdio session negotiates at most ``2025-11-25`` -- the modern
    ``2026-07-28`` wire is reachable only through header-routed transports
    -- and that version has no ``resultType``, so the sieve strips it.
    Measured on this repository's resolved version against a real stdio
    session: formula 31,049 chars vs wire 31,025 chars, an excess of
    exactly 24 -- the length of ``,"resultType":"complete"`` -- with every
    other field identical and nothing present on the wire that the formula
    missed.

    Over-reporting is the safe direction: it can only refuse a result that
    would have fitted, never admit one that would not. Reclaiming the 24
    would mean threading the negotiated protocol version into a path whose
    only job is to be a conservative gate, so it is not done.

    ``by_alias=True`` is still required -- 2.x renamed the model fields to
    snake_case while keeping the camelCase wire aliases, so a dump without
    it would measure names that never reach the wire. ``exclude_none=True``
    keeps unset optionals uncounted. Calling ``model_dump_json`` directly
    reuses the same pydantic-core encoder the SDK itself uses rather than
    reimplementing its settings via ``json.dumps``.
    """
    return delivered.model_dump_json(by_alias=True, exclude_none=True)


def measure_call_tool_result_chars(delivered: CallToolResult) -> int:
    return len(_serialize_call_tool_result(delivered))


def build_call_tool_result(payload: object, *, tool: Tool) -> CallToolResult:
    """Delegate the two-channel construction to the framework's conversion.

    The conversion returns a finished ``CallToolResult`` on the mcp 2.x
    generation, so the delegation is total and there is nothing left to
    wrap (design.md > "Delete the unreachable wrapping branches and let
    the shape assertion carry the pin"). The 1.x branches for a
    ``(content, structured_content)`` tuple and for a bare content
    sequence are deleted rather than kept defensively: no input reaches
    them, so they would be untested code that reads as handled.

    The fact this depends on -- that the conversion returns a finished
    result -- is asserted per tool by the delivery shape test, so a
    future generation reintroducing a second shape fails there instead
    of silently degrading delivery here.
    """
    converted = tool.fn_metadata.convert_result(payload)
    if not isinstance(converted, CallToolResult):
        raise TypeError(
            "convert_result returned "
            f"{type(converted).__name__}, not a CallToolResult; the "
            "delivery construction depends on the framework returning a "
            "finished result."
        )
    return converted


class LadderExhausted(Exception):
    """No state in a degradation ladder produced a result within budget.

    Kept distinct from ``None`` (design.md > "Give each tool its own
    degradation ladder") so a caller can never mistake "nothing fit" for a
    legitimate empty result -- the two require different handling.
    """

    def __init__(self, state_count: int) -> None:
        super().__init__(
            f"No ladder state fit within {MCP_RESPONSE_BUDGET_CHARS} "
            f"characters after trying {state_count} state(s)."
        )
        self.state_count = state_count


@dataclass(frozen=True)
class LadderOutcome:
    """The delivered result of a ladder walk, plus which state produced it.

    ``state_index`` is 0 for the unreduced (first) state and greater than 0
    for any reduced state, so a caller derives ``reduced`` without
    re-measuring the delivered result -- this is what task 3.1's
    ``response_truncated`` flag is derived from.
    """

    delivered: CallToolResult
    state_index: int

    @property
    def reduced(self) -> bool:
        return self.state_index > 0


def walk_ladder(states: Sequence[Callable[[], object]], *, tool: Tool) -> LadderOutcome:
    """Walk an ordered sequence of payload-producing *states*.

    Re-assembles and re-measures at each step (via :func:`build_call_tool_result`
    and :func:`measure_call_tool_result_chars`) and returns the
    :class:`LadderOutcome` for the first state whose delivered result fits
    within :data:`MCP_RESPONSE_BUDGET_CHARS`.

    Each *state* is a zero-argument callable producing that step's logical
    payload. States are evaluated lazily, in order: an in-budget first
    state is measured exactly once and no later state is ever called.
    Raises :class:`LadderExhausted` when no state fits -- never returns
    ``None``, so exhaustion can never be mistaken for a legitimate empty
    result.

    Carries no tool-specific ladder content: *states* are supplied entirely
    by the caller (design.md > "Give each tool its own degradation
    ladder"); this module owns only the generic walk-and-measure driver.
    """
    for index, state in enumerate(states):
        payload = state()
        delivered = build_call_tool_result(payload, tool=tool)
        if measure_call_tool_result_chars(delivered) <= MCP_RESPONSE_BUDGET_CHARS:
            return LadderOutcome(delivered=delivered, state_index=index)
    raise LadderExhausted(len(states))

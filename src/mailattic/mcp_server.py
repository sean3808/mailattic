from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, TypedDict, cast

from mcp.server import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import StrictBool, StrictInt, StrictStr

from .contact_history import parse_date_bound
from .datetime_bounds import (
    format_invalid_date_message,
    normalize_optional_date_text,
    whole_day_bound,
)
from .mcp_delivery import (
    ERROR_ECHO_CAP_CHARS,
    MCP_RESPONSE_BUDGET_CHARS,
    LadderExhausted,
    build_call_tool_result,
    build_environment_error,
    build_tool_error,
    cap_error_echo,
    measure_call_tool_result_chars,
    walk_ladder,
)
from .mcp_readonly import ReadOnlyMailReader
from .mcp_writer import MailWriter
from .query import SearchFilters, ThreadIdentifierError, render_text, strip_quote_tail
from .storage import resolve_database_default

_SERVER_NAME = "mailattic"


class _ContactHistoryAttachments(TypedDict):
    total_count: int
    recent: list[str]


class _ContactHistorySummary(TypedDict):
    query: str
    query_type: str
    date_from: str | None
    date_to: str | None
    first_message_at: str | None
    last_message_at: str | None
    total_messages: int
    inbound_count: int
    outbound_count: int
    unknown_count: int
    distinct_contacts: int
    contacts: list[dict[str, object]]
    attachments: _ContactHistoryAttachments


# ---------------------------------------------------------------------------
# Shared payload shaping (spec: "Read-only single-message retrieval over
# MCP", "Conservative bounds keep responses small, without claiming a size
# guarantee"). Every tool that returns a message, a recipient, or an
# attachment builds its payload through these helpers -- field by field from
# an explicit allowlist, never by copying a query-layer dict and adding to
# it -- so the allowlist stays the actual security boundary rather than a
# convention that erodes the first time a query function gains a field.
# ---------------------------------------------------------------------------

# Default body budget, shared by every tool that renders a message body.
BODY_MAX_CHARS_DEFAULT = 20000
_BODY_MAX_CHARS_RANGE = (1000, 100000)

# Collection bound range, shared by every tool that returns recipients or
# attachments. Defaults are tool-specific and therefore NOT hardcoded here --
# callers (mail_show, mail_thread, mail_search) each supply their own
# default per design.md > "Bound the collections inside a message, not only
# the message count" (mail_show: 25 / 20; mail_thread and mail_search: 5 / 5).
_RECIPIENTS_LIMIT_RANGE = (1, 200)
_ATTACHMENTS_LIMIT_RANGE = (1, 200)

# Per-tool sizing defaults. Each comment is intentionally next to the constants
# it explains so a later change to one default does not require re-deriving the
# duplicate-delivery arithmetic from the design doc first.
# `mail_show`: one message is still delivered twice (content + structured), but
# its per-message cost stays low enough that 25 recipients/20 attachments fit.
_MAIL_SHOW_RECIPIENTS_LIMIT_DEFAULT = 25
_MAIL_SHOW_ATTACHMENTS_LIMIT_DEFAULT = 20
# `mail_thread`: bodies and nested collections multiply by message count and are
# then duplicated on delivery, so thread view defaults to 10 messages with 5/5.
_MAIL_THREAD_LIMIT_DEFAULT = 10
_MAIL_THREAD_LIMIT_RANGE = (1, 100)
_MAIL_THREAD_RECIPIENTS_LIMIT_DEFAULT = 5
_MAIL_THREAD_ATTACHMENTS_LIMIT_DEFAULT = 5
# `mail_search`: up to 20 rows already double on delivery, so each row keeps
# the same tighter 5/5 per-message collection defaults as `mail_thread`.
_MAIL_SEARCH_LIMIT_DEFAULT = 20
_MAIL_SEARCH_LIMIT_RANGE = (1, 100)
_MAIL_SEARCH_RECIPIENTS_LIMIT_DEFAULT = 5
_MAIL_SEARCH_ATTACHMENTS_LIMIT_DEFAULT = 5
# `mail_contact_history`: one aggregate object still doubles on delivery, but
# only one contacts list and one recent-attachments list are present, so 50/20.
_MAIL_CONTACT_HISTORY_CONTACTS_LIMIT_DEFAULT = 50
_MAIL_CONTACT_HISTORY_CONTACTS_LIMIT_RANGE = (1, 200)
_MAIL_CONTACT_HISTORY_ATTACHMENTS_LIMIT_DEFAULT = 20
_MAIL_CONTACT_HISTORY_ATTACHMENTS_LIMIT_RANGE = (1, 100)
# `mail_attachments`: each attachment row doubles on delivery and may carry
# nested message summaries, so keep 20 rows with 5 carrying messages each.
_MAIL_ATTACHMENTS_LIMIT_DEFAULT = 20
_MAIL_ATTACHMENTS_LIMIT_RANGE = (1, 100)
_MAIL_ATTACHMENTS_CARRYING_MESSAGES_LIMIT_DEFAULT = 5
_MAIL_ATTACHMENTS_CARRYING_MESSAGES_LIMIT_RANGE = (1, 50)
_CONTACT_HISTORY_SELECTOR_MAX_CHARS = 320

# Presentation-string caps (spec table under "Conservative bounds keep
# responses small, without claiming a size guarantee"). Identifiers and
# locators -- id, message_id, dedup_key, rfc_message_id, in_reply_to,
# references, content_hash, storage_path, attachment_id, message_db_id,
# sender_email, and recipient addresses -- are never capped and never
# appear in a record's `truncated_fields` map.
_SUBJECT_CAP = 500
_DISPLAY_NAME_CAP = 200
_ATTACHMENT_NAME_CAP = 300
_FOLDER_PATH_CAP = 500
# `snippet` belongs only to `mail_search`'s item shape (task 9.5, out of
# this group's scope); the cap is defined here so the whole presentation-
# string cap table lives in one place rather than split across groups.
_SNIPPET_CAP = 300

_READ_ONLY_HINTS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

_MAIL_SEARCH_DESCRIPTION = (
    "Find message candidates by keyword and structured filters, returning a bare "
    "list of summary items or full-body items. Use mail_show for one stored id "
    "and mail_thread for a whole conversation or an RFC822 Message-ID string. "
    "keyword syntax: terms separated by spaces are ANDed; uppercase AND / OR / "
    "NOT, parentheses, and double-quoted phrases are honoured, and lowercase "
    "and/or/not are ordinary search terms. Results with a keyword are ordered "
    "by relevance, not by date, and each item reports match_field (subject, "
    "body, or null when the hit is in an indexed field this item does not "
    "show, such as recipients or attachment names) and match_in (new or "
    "quoted -- whether the hit the snippet shows is in the message's own new "
    "content or in its quoted history). snippet is centred on the hit when "
    "there is one, on the position covering the most of what was asked for."
)
_MAIL_SHOW_DESCRIPTION = (
    "Return one stored message object by integer stored id, including bounded "
    "body and collection fields. Use mail_search to find valid ids and "
    "mail_thread when the identifier is an RFC822 Message-ID string or the full "
    "conversation is needed."
)
_MAIL_THREAD_DESCRIPTION = (
    "Return one conversation object with messages plus total/returned counts, "
    "using either a stored id or an RFC822 Message-ID string. Use mail_search "
    "to find identifiers and mail_show when you only need one stored message."
)
# design.md > "Map the exception at each entry to that entry's native error
# idiom" (MCP mail_thread bullet): a static template, not the resolver's
# exception message, so the AST guard's static-template requirement holds
# and the message stays independent of what query.py happens to say. States
# the accepted identifier forms and never embeds the rejected value -- an
# over-limit int cannot be safely rendered to decimal text.
_THREAD_IDENTIFIER_ERROR_TEMPLATE = (
    "Thread identifier must be an integer stored id within the signed "
    "64-bit range, its decimal string form, or an RFC822 Message-ID string."
)
_MAIL_CONTACT_HISTORY_DESCRIPTION = (
    "Return one aggregated correspondence-summary object for exactly one contact "
    "address or one domain. This is not a message search or thread tool; use "
    "mail_search, mail_show, or mail_thread for individual messages."
)
_MAIL_ATTACHMENTS_DESCRIPTION = (
    "Return one attachment-metadata result object with attachment rows, totals, "
    "and bounded carrying-message summaries. This tool never returns attachment "
    "content bytes; use mail_search or mail_show for message content."
)
_MAIL_SYNC_STATUS_DESCRIPTION = (
    "Report the state of the most recent synchronization run together with "
    "the timestamp of the newest stored message, in one response. Accepts no "
    "parameters and never starts or affects a synchronization run. The four "
    "fields -- last_sync_started_at, last_sync_ended_at, last_sync_status "
    "(never_synced, running, completed, or failed), and latest_message_at -- "
    "are always present; a field whose value is unknown is null rather than "
    "omitted. Reports facts only: no freshness verdict, staleness duration, "
    "or composite score is computed."
)
_MAIL_ATTACHMENT_EXPORT_DESCRIPTION = (
    "Copy stored attachments into a host directory and report where each one "
    "landed. attachment_ids are the attachment_id values from mail_attachments "
    "(or attachments[].id from mail_show / mail_thread), 1 to 50 per call; a "
    "repeated id is reported once. dest_dir must be an absolute host path of at "
    "most 200 characters, inside a folder you can read back; it is created if "
    "missing. Never overwrites: a file already at the "
    "destination is left untouched and reported as skipped-existing. Never "
    "exports a file type Windows runs when opened (.exe, .bat, .cmd, .lnk, "
    ".vbs, .js, .ps1, .msi, ...): those are reported as skipped-blocked-type "
    "and the user must save them from Outlook. A dest_dir inside the Startup "
    "folder or an Office startup folder is refused. Each item "
    "reports status exported, skipped-existing, skipped-non-exportable (no "
    "stored payload, metadata only), skipped-blocked-type, or failed, with the "
    "absolute dest_path. Returns paths, never file content."
)
_MAIL_DRAFT_REPLY_DESCRIPTION = (
    "Generate an RFC5322 reply-all draft for a stored message and write it to the "
    "MCP-dedicated draft folder. message_id is the RFC Message-ID "
    "(e.g. '<abc@example.com>') from mail_show, not the numeric id from "
    "mail_search. Returns its draft_id, content fingerprint "
    "(SHA-256), and full draft text for review. The draft is NOT sent. Present "
    "the complete draft (Subject, To, Cc, Bcc, body, attachments, draft_id, and "
    "fingerprint) to the user for conversational review before calling mail_send."
)
_MAIL_SEND_DESCRIPTION = (
    "Send a reviewed draft from the MCP-dedicated draft folder. Requires the "
    "draft_id and the matching SHA-256 fingerprint obtained from mail_draft_reply. "
    "Requires MAILATTIC_SMTP_ENV_FILE environment variable to point to a valid "
    "SMTP configuration file. Only call this after the user has reviewed the draft "
    "and explicitly confirmed to send. status='partial' means SMTP refused some "
    "recipients (see refused: address/code/reason): the message WAS sent to the "
    "accepted ones, so never call mail_send again for the same draft; create a "
    "new draft for the refused recipients only. An error does not always mean the "
    "mail was not sent: when it says the outcome is unknown or that the mail may "
    "already be delivered, verify delivery status before sending again; never "
    "re-draft and resend on your own."
)
# Issue #28: the ids bound and the dest_dir bound together keep the 50-item
# result inside the delivery budget by construction (dest_path is at most
# ~240 characters per attachment_store's own export-path cap).
_MAIL_ATTACHMENT_EXPORT_MAX_IDS = 50
_MAIL_ATTACHMENT_EXPORT_DEST_DIR_MAX_CHARS = 200
_EXPORT_DETAIL_CAP = 80
_CONTACT_HISTORY_LEGAL_INVOCATIONS = (
    "Use exactly one of contact='<email>' or domain='<example.com>'."
)
_MAIL_ATTACHMENTS_FILTER_HINT = (
    "Provide at least one filter: name_contains, ext, sender_domain, "
    "recipient_domain, participants_domain, from_date, or to_date."
)
_KEYWORD_SYNTAX_HINT = (
    "Accepted syntax: uppercase AND / OR / NOT, parentheses, and double-quoted phrases."
)
_ISO_DATE_ONLY_EXPECTED = "a YYYY-MM-DD date (canonical, basic, or ISO week form)"

# ---------------------------------------------------------------------------
# Over-budget refusals (spec: "An over-budget refusal offers a correction
# where one exists and declares a dead end where none does"; design.md >
# "Make the refusal name a correction the caller can actually apply"). Three
# forms, chosen by what inputs the refusing tool actually accepts rather
# than by symmetry across the tool set -- see that design decision for the
# three failure modes each form exists to avoid.
# ---------------------------------------------------------------------------


def _mail_search_lowerable_sizing_params(
    *,
    limit: int,
    full_body: bool,
    body_max_chars: int,
    recipients_limit: int,
    attachments_limit: int,
) -> list[str]:
    """Sizing-parameter descriptions that affect *this* invocation and are
    still reducible -- the set form 1 (below) names, in the spec's declared
    order: `limit`, `full_body`, `body_max_chars`, `recipients_limit`,
    `attachments_limit` (spec: "Form 1 -- lower a sizing parameter").

    ``full_body`` is boolean rather than ranged, so it is named on its own
    when true rather than folded into the numeric
    ``(currently X, accepted range low..high)`` shape -- turning it off is
    itself the correction. ``body_max_chars`` and ``recipients_limit`` have
    no effect at all when ``full_body`` is false (the query loads neither a
    body nor recipients in summary mode), so they are excluded there rather
    than named as a dead end. A numeric parameter already at its floor is
    excluded the same way -- naming it would be advice the caller cannot
    take.
    """
    descriptions: list[str] = []

    def _describe(name: str, value: int, low: int, high: int) -> None:
        if value > low:
            descriptions.append(
                f"{name} (currently {value}, accepted range {low}..{high})"
            )

    _describe("limit", limit, *_MAIL_SEARCH_LIMIT_RANGE)
    if full_body:
        descriptions.append("full_body (currently true; set it to false)")
        _describe("body_max_chars", body_max_chars, *_BODY_MAX_CHARS_RANGE)
        _describe("recipients_limit", recipients_limit, *_RECIPIENTS_LIMIT_RANGE)
    _describe("attachments_limit", attachments_limit, *_ATTACHMENTS_LIMIT_RANGE)
    return descriptions


def _mail_search_over_budget_error(
    *,
    limit: int,
    full_body: bool,
    body_max_chars: int,
    recipients_limit: int,
    attachments_limit: int,
) -> ValueError:
    """Task 3.2 form 1: `mail_search` never degrades, so its sizing
    parameters really are untried (design.md > "Make the refusal name a
    correction the caller can actually apply"). Falls back to a
    narrow-the-query correction when every applicable parameter is already
    at its floor -- `mail_search` always has a free-form filter to fall
    back on and never reaches form 3 (design.md > form 2/3 boundary rule).
    """
    lowerable = _mail_search_lowerable_sizing_params(
        limit=limit,
        full_body=full_body,
        body_max_chars=body_max_chars,
        recipients_limit=recipients_limit,
        attachments_limit=attachments_limit,
    )
    if lowerable:
        params_desc = "; ".join(lowerable)
        params_desc_prefix = params_desc[:ERROR_ECHO_CAP_CHARS]
        params_desc_suffix = params_desc[ERROR_ECHO_CAP_CHARS:]
        return build_tool_error(
            ValueError,
            "mail_search result exceeds the delivery budget of {budget} "
            "characters and cannot be delivered as a partial list. Lower "
            "{params_desc_prefix}{params_desc_suffix}, or narrow the query "
            "with additional filters or a tighter date range, and try "
            "again.",
            budget=MCP_RESPONSE_BUDGET_CHARS,
            params_desc_prefix=params_desc_prefix,
            params_desc_suffix=params_desc_suffix,
        )
    return build_tool_error(
        ValueError,
        "mail_search result exceeds the delivery budget of {budget} characters "
        "and cannot be delivered as a partial list. Every sizing parameter for "
        "this invocation is already at its minimum. Narrow the query with "
        "additional filters or a tighter date range and try again.",
        budget=MCP_RESPONSE_BUDGET_CHARS,
    )


def _mail_show_terminal_refusal(message_db_id: object) -> ValueError:
    """Task 3.2 form 3 for `mail_show`: no parameter, filter, or tool is
    named -- the single record cannot be shrunk further because of its own
    unbounded identifier and locator fields (design.md > form 3).
    """
    return build_tool_error(
        ValueError,
        "Stored message id {message_db_id} cannot be delivered: even with "
        "its rendered body, recipients, and attachments reduced to their "
        "minimum, the record exceeds the delivery budget of "
        "{budget} characters because of its own "
        "unbounded identifier and locator fields. This record has no "
        "further reduction available and no in-bridge fallback.",
        message_db_id=message_db_id,
        budget=MCP_RESPONSE_BUDGET_CHARS,
    )


def _mail_sync_status_terminal_refusal() -> ValueError:
    """Task 3.2 form 3 for `mail_sync_status`: the tool takes no parameters,
    so there is nothing the caller could narrow, and its four fields carry no
    reducible collection -- there is no ladder to walk. Reaching this means a
    stored timestamp is itself pathological, which the caller cannot fix from
    here (design.md > form 3).
    """
    return build_tool_error(
        ValueError,
        "mail_sync_status cannot be delivered: its four timestamp and status "
        "fields exceed the delivery budget of {budget} characters. The tool "
        "takes no parameters and its fields carry no reducible collection, so "
        "there is no narrower request and no in-bridge fallback; a stored "
        "timestamp value is itself malformed.",
        budget=MCP_RESPONSE_BUDGET_CHARS,
    )


def _mail_thread_terminal_refusal(message_db_id: object) -> ValueError:
    """Task 3.2 form 3 for `mail_thread`: same reasoning as `mail_show`,
    applied to the single most-recent message a thread ladder converges to.
    """
    return build_tool_error(
        ValueError,
        "mail_thread cannot be delivered: message {message_db_id}, the "
        "most recent in this thread, still exceeds the delivery budget of "
        "{budget} characters even with the thread "
        "reduced to that single message and its body, recipients, and "
        "attachments reduced to their minimum, because of its own "
        "unbounded identifier and locator fields. This record has no "
        "further reduction available and no in-bridge fallback.",
        message_db_id=message_db_id,
        budget=MCP_RESPONSE_BUDGET_CHARS,
    )


_MAIL_CONTACT_HISTORY_NARROWING_FIELDS = (
    "date_from and date_to (each accepts a single YYYY-MM-DD day)"
)


def _mail_contact_history_over_budget_error(
    *, date_from: str | None, date_to: str | None
) -> ValueError:
    """Task 3.2 forms 2/3 for `mail_contact_history`.

    `mail_contact_history` accepts only date bounds as a narrowing input,
    which have a finite floor -- a single Taipei day. Both bounds are
    optional and the default invocation supplies neither, so the boundary
    is a three-case rule rather than "above the floor or at it": form 3
    only when both bounds are present and name the same Taipei day; form 2
    otherwise, including when either bound is absent, since an absent
    bound is the widest range and therefore always narrowable (design.md >
    form 2/3 boundary rule). Never names a record either way -- the
    summary's scalars are an aggregate over the matched set, not a row.
    """
    parsed_from = parse_date_bound(date_from, parameter_name="date_from")
    parsed_to = parse_date_bound(date_to, parameter_name="date_to")
    if parsed_from is not None and parsed_to is not None and parsed_from == parsed_to:
        return build_tool_error(
            ValueError,
            "mail_contact_history result exceeds the delivery budget of {budget} "
            "characters even after reducing the contacts and attachments lists "
            "to their minimum. date_from and date_to already name a single day "
            "({day}), the narrowest range this tool "
            "accepts, so the required summary payload alone exceeds the "
            "budget. There is no further reduction available and no in-bridge "
            "fallback.",
            budget=MCP_RESPONSE_BUDGET_CHARS,
            day=parsed_from.isoformat(),
        )
    return build_tool_error(
        ValueError,
        "mail_contact_history result exceeds the delivery budget of {budget} "
        "characters even after reducing the contacts and attachments lists to "
        "their minimum. Narrow the query using {narrowing_fields} and try again.",
        budget=MCP_RESPONSE_BUDGET_CHARS,
        narrowing_fields=_MAIL_CONTACT_HISTORY_NARROWING_FIELDS,
    )


_MAIL_ATTACHMENTS_NARROWING_FILTERS = (
    "name_contains, ext, sender_domain, recipient_domain, "
    "participants_domain, from_date, or to_date"
)


def _mail_attachments_over_budget_error(
    selected: Sequence[dict[str, object]],
) -> ValueError:
    """Task 3.2 form 2 for `mail_attachments`: it accepts free-form content
    filters and date bounds, so a narrower query that excludes the
    offending record always exists -- it never reaches form 3 (design.md >
    form 2/3 boundary rule). Names the single remaining match by numeric
    `attachment_id` when the ladder's own entry-count reduction has already
    converged to one entry, since that is the offending record.
    """
    if len(selected) >= 1:
        offending_id = selected[0].get("attachment_id")
        return build_tool_error(
            ValueError,
            "mail_attachments result exceeds the delivery budget of {budget} "
            "characters even after reducing every carrying-message collection "
            "and dropping every attachment entry this tool's ladder could drop. "
            "The single remaining match (attachment_id {offending_id}) alone "
            "exceeds the budget. Narrow the query using {narrowing_filters} "
            "to exclude it, and try again.",
            budget=MCP_RESPONSE_BUDGET_CHARS,
            offending_id=offending_id,
            narrowing_filters=_MAIL_ATTACHMENTS_NARROWING_FILTERS,
        )
    return build_tool_error(
        ValueError,
        "mail_attachments result exceeds the delivery budget of {budget} "
        "characters even after reducing every carrying-message collection and "
        "dropping every attachment entry this tool's ladder could drop. Narrow "
        "the query using {narrowing_filters} and try again.",
        budget=MCP_RESPONSE_BUDGET_CHARS,
        narrowing_filters=_MAIL_ATTACHMENTS_NARROWING_FILTERS,
    )


def _validate_int_range(value: int, param_name: str, low: int, high: int) -> None:
    """Reject *value* outside ``[low, high]`` as a tool error naming both."""
    if not low <= value <= high:
        raise build_tool_error(
            ValueError,
            "Invalid {param_name}: {value}. Expected {low}..{high}.",
            param_name=param_name,
            value=value,
            low=low,
            high=high,
        )


def _validate_message_sizing_bounds(
    *,
    recipients_limit: int,
    attachments_limit: int,
    body_max_chars: int,
) -> None:
    _validate_int_range(recipients_limit, "recipients_limit", *_RECIPIENTS_LIMIT_RANGE)
    _validate_int_range(
        attachments_limit, "attachments_limit", *_ATTACHMENTS_LIMIT_RANGE
    )
    _validate_int_range(body_max_chars, "body_max_chars", *_BODY_MAX_CHARS_RANGE)


# design.md > "Define the canonical stored-id form as a bounded string
# test, not an integer conversion": the accepted upper bound is the
# largest signed SQLite INTEGER, not "19 digits" -- a value can be
# 19 digits and still overflow SQLite's column type.
_SQLITE_MAX_INTEGER_STR = "9223372036854775807"


def _is_canonical_stored_id_string(value: str) -> bool:
    """Decide whether *value* is a stored id's canonical string form.

    Deliberately decidable without calling ``int()``. Two measured
    failures make a bare digit-test-then-convert unsafe: Python 3.13
    caps integer parsing at ``sys.get_int_max_str_digits()`` (4300 on
    this runtime), so a 4301-digit all-ASCII-digit string passes a
    character test and then raises ``ValueError`` on conversion; and
    ``9223372036854775808`` is 19 digits and converts cleanly, but
    binding it to a SQLite query raises
    ``OverflowError: Python int too large to convert to SQLite INTEGER``.
    The magnitude clause below closes both by string inspection alone,
    comparing lexicographically against the known maximum -- sound
    because among equal-length digit strings with no leading zero,
    lexicographic order and numeric order coincide.
    """
    if not value.isascii() or not value.isdigit():
        return False
    if value != "0" and value.startswith("0"):
        return False
    if len(value) < len(_SQLITE_MAX_INTEGER_STR):
        return True
    if len(value) == len(_SQLITE_MAX_INTEGER_STR):
        return value <= _SQLITE_MAX_INTEGER_STR
    return False


def _read_only_annotations(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, **_READ_ONLY_HINTS)


def _missing_database_error(resolved_database_path: Path) -> FileNotFoundError:
    return build_environment_error(
        FileNotFoundError,
        resolved_database_path,
        "Mail database was not found: {database_path}",
    )


def _database_runtime_error(
    resolved_database_path: Path, exc: sqlite3.Error
) -> RuntimeError:
    return build_environment_error(
        RuntimeError,
        resolved_database_path,
        "Mail database query failed for {database_path}: {detail}",
        detail=str(exc),
    )


def _unexpected_tool_error(
    context: str,
    exc: BaseException,
    *,
    message_id: object | None = None,
    identifier: object | None = None,
) -> RuntimeError:
    """Cap the diagnostic detail of an exception this server did not build.

    Covers the third fragment class alongside caller echoes and engine
    diagnostics (design.md > "Diagnostic detail from an underlying
    component is capped"): neither caller input nor a stored field, so it
    gets the same bound rather than being left to propagate unbounded.
    """
    if message_id is not None:
        return build_tool_error(
            RuntimeError,
            "Unexpected error retrieving message {message_id}: {detail}",
            message_id=message_id,
            detail=str(exc),
        )
    if identifier is not None:
        return build_tool_error(
            RuntimeError,
            "Unexpected error resolving thread identifier {identifier}: {detail}",
            identifier=repr(identifier),
            detail=str(exc),
        )
    return build_tool_error(
        RuntimeError,
        "Unexpected error {context}: {detail}",
        context=context,
        detail=str(exc),
    )


def _invalid_keyword_syntax_error(original: ValueError) -> ValueError:
    return build_tool_error(
        ValueError,
        "{original}. {syntax_hint}",
        original=str(original),
        syntax_hint=_KEYWORD_SYNTAX_HINT,
    )


def _cap_presentation_string(
    value: object,
    cap: int,
    field_key: str,
    truncated_fields: dict[str, int],
    *,
    extra_field_keys: Sequence[str] = (),
) -> object:
    """Cap a presentation string at *cap* characters, uninterpreted otherwise.

    Non-string values (``None`` included) pass through unchanged -- capping
    is a string-length rule, not a type coercion. When *value* exceeds
    *cap*, ``truncated_fields[field_key]`` records the untruncated length
    and the retained prefix is returned byte-identical to the stored value.
    """
    if not isinstance(value, str) or len(value) <= cap:
        return value
    truncated_fields[field_key] = len(value)
    for extra_field_key in extra_field_keys:
        truncated_fields[extra_field_key] = len(value)
    return value[:cap]


def _bound_collection[CollectionItem](
    items: Sequence[CollectionItem],
    limit: int,
    *,
    param_name: str,
    low: int,
    high: int,
) -> tuple[list[CollectionItem], int, bool]:
    """Bound *items* to *limit* entries, validating *limit* as a tool error.

    Returns ``(bounded_items, untruncated_total, was_truncated)``.
    """
    _validate_int_range(limit, param_name, low, high)
    total = len(items)
    return list(items[:limit]), total, total > limit


def _ladder_halving_steps(start: int, floor: int) -> list[int]:
    """Halving reduction sequence from *start* down to *floor*.

    Excludes *start* itself -- the caller's own unreduced value is a
    separate, prior ladder state -- and always ends exactly at *floor* when
    any reduction is possible. Empty when *start* is already at or below
    *floor*: nothing left to shrink at that level.

    Each step halves the previous one and clamps at *floor*, so a
    pathological payload converges to the floor in O(log n) states rather
    than one unit at a time (design.md > Risks, "the ladders are finite and
    converge by halving"). Shared by every per-tool ladder: `mail_show`
    walks it for `body_max_chars`, then `recipients_limit`, then
    `attachments_limit`; `mail_thread` walks it once more for the number of
    returned messages before handing the single remaining one to
    `mail_show`'s own within-record order.
    """
    steps: list[int] = []
    current = start
    while current > floor:
        current = max(floor, current // 2)
        if not steps or steps[-1] != current:
            steps.append(current)
    return steps


def _shape_recipient(
    entry: dict[str, object],
    *,
    role: str,
    index: int,
    truncated_fields: dict[str, int],
) -> dict[str, object]:
    """Shape one recipient entry to exactly ``name`` and ``email``.

    ``email`` is an identifier and is never capped. ``name`` is a
    presentation string and is capped, reported under a positional key
    (for example ``to[0].name``) so the caller can tell which recipient
    was shortened.
    """
    return {
        "name": _cap_presentation_string(
            entry.get("name"),
            _DISPLAY_NAME_CAP,
            f"{role}[{index}].name",
            truncated_fields,
        ),
        "email": entry.get("email"),
    }


def _shape_attachment(
    entry: dict[str, object],
    *,
    index: int,
    truncated_fields: dict[str, int],
    include_locators: bool = True,
) -> dict[str, object]:
    """Shape one message-attachment entry to its declared nested allowlist.

    ``id``, ``content_hash``, ``size_bytes``, ``mime_type``, and
    ``storage_path`` are identifiers/locators and are never capped.
    ``attachment_name`` is a presentation string and is capped, reported
    under a positional key (for example ``attachments[0].attachment_name``).

    ``include_locators=False`` omits ``content_hash`` and ``storage_path``
    (issue #178). They are the two largest fields per attachment -- a
    64-character digest that ``storage_path`` then embeds a second time --
    and on a mailbox whose signature blocks carry five inline images per
    message they were a third of a `mail_search` summary's whole delivery
    budget. `mail_search` finds candidate *messages*; the tools that locate
    bytes (`mail_show`, `mail_attachments`) keep both fields.
    """
    shaped: dict[str, object] = {
        "id": entry.get("id"),
        "attachment_name": _cap_presentation_string(
            entry.get("attachment_name"),
            _ATTACHMENT_NAME_CAP,
            f"attachments[{index}].attachment_name",
            truncated_fields,
            extra_field_keys=(f"attachment_names[{index}]",),
        ),
        "size_bytes": entry.get("size_bytes"),
        "mime_type": entry.get("mime_type"),
    }
    if include_locators:
        shaped["content_hash"] = entry.get("content_hash")
        shaped["storage_path"] = entry.get("storage_path")
    return shaped


def _shape_recipients(
    message: dict[str, object],
    *,
    recipients_limit: int,
    truncated_fields: dict[str, int],
) -> tuple[dict[str, list[dict[str, object]]], dict[str, int], bool]:
    recipients_truncated = False
    recipients_total: dict[str, int] = {}
    shaped_recipients: dict[str, list[dict[str, object]]] = {}
    for role in ("to", "cc", "bcc"):
        # query.get_message and query.search_messages produce each recipient role
        # as a list of mapping entries.
        role_entries = cast(list[dict[str, object]], message.get(role) or [])
        bounded, total, role_truncated = _bound_collection(
            role_entries,
            recipients_limit,
            param_name="recipients_limit",
            low=_RECIPIENTS_LIMIT_RANGE[0],
            high=_RECIPIENTS_LIMIT_RANGE[1],
        )
        shaped_recipients[role] = [
            _shape_recipient(
                entry, role=role, index=i, truncated_fields=truncated_fields
            )
            for i, entry in enumerate(bounded)
        ]
        recipients_total[role] = total
        recipients_truncated = recipients_truncated or role_truncated
    return shaped_recipients, recipients_total, recipients_truncated


def _shape_message_attachments(
    attachments: object,
    *,
    attachments_limit: int,
    truncated_fields: dict[str, int],
    include_locators: bool = True,
) -> tuple[list[dict[str, object]], int, bool]:
    # query.get_message and query.search_messages produce attachments as a list
    # of mapping entries.
    attachment_entries = cast(list[dict[str, object]], attachments or [])
    attachments_bounded, attachments_total, attachments_truncated = _bound_collection(
        attachment_entries,
        attachments_limit,
        param_name="attachments_limit",
        low=_ATTACHMENTS_LIMIT_RANGE[0],
        high=_ATTACHMENTS_LIMIT_RANGE[1],
    )
    shaped_attachments = [
        _shape_attachment(
            entry,
            index=i,
            truncated_fields=truncated_fields,
            include_locators=include_locators,
        )
        for i, entry in enumerate(attachments_bounded)
    ]
    return shaped_attachments, attachments_total, attachments_truncated


def _finalize_rendered_body(
    rendered_text: object,
    *,
    new_only: bool,
    body_max_chars: int = BODY_MAX_CHARS_DEFAULT,
) -> tuple[str, bool, int]:
    _validate_int_range(body_max_chars, "body_max_chars", *_BODY_MAX_CHARS_RANGE)
    rendered = rendered_text if isinstance(rendered_text, str) else ""
    if new_only:
        rendered = strip_quote_tail(rendered)
    total_chars = len(rendered)
    if total_chars > body_max_chars:
        return rendered[:body_max_chars], True, total_chars
    return rendered, False, total_chars


def render_message_body(
    body_text: object,
    *,
    new_only: bool,
    body_max_chars: int = BODY_MAX_CHARS_DEFAULT,
) -> tuple[str, bool, int]:
    """Render a raw stored ``body_text`` value to the capped text payload.

    Reuses the CLI text format's own rendering (:func:`render_text`) and
    quote-tail stripping (:func:`strip_quote_tail`, conservative fallback:
    full text when no reliable boundary is found) rather than a second
    implementation. Returns ``(rendered_text, body_truncated,
    body_total_chars)`` where ``body_total_chars`` is the untruncated
    rendered length.

    Raises ``ValueError`` naming ``body_max_chars`` and its accepted range
    when *body_max_chars* is outside 1000 through 100000 inclusive.
    """
    return _finalize_rendered_body(
        render_text(body_text if isinstance(body_text, str) else None),
        new_only=new_only,
        body_max_chars=body_max_chars,
    )


def shape_message_payload(
    message: dict[str, object],
    *,
    include_body: bool,
    recipients_limit: int,
    attachments_limit: int,
    new_only: bool = False,
    body_max_chars: int = BODY_MAX_CHARS_DEFAULT,
) -> dict[str, object]:
    """Build the ``mail_show``-shaped payload from a :func:`get_message` dict.

    Assembles the allowlisted fields one at a time from *message* -- never
    by copying *message* and adding to or deleting from it -- so
    ``body_text``, ``raw_headers``, ``logical_locator``,
    ``source_input_mode``, and ``source_requested_from`` can never leak
    through a later change to the query layer's dict shape. Used directly
    by ``mail_show``, and per-entry by ``mail_thread`` (with
    *include_body* following that tool's ``full_body`` flag).

    *recipients_limit* and *attachments_limit* carry no default here --
    callers supply their own tool-specific default (``mail_show``: 25 / 20;
    ``mail_thread`` and ``mail_search``: 5 / 5) rather than inheriting one
    hardcoded pair, per design.md > "Bound the collections inside a
    message, not only the message count".

    Raises ``ValueError`` naming the offending parameter and its accepted
    range for an out-of-range *recipients_limit*, *attachments_limit*, or
    *body_max_chars* -- validated even when *include_body* is ``False``,
    because every declared sizing parameter is validated rather than
    silently clamped or ignored.

    ``truncated_fields`` is added to the returned dict only when at least
    one presentation-string cap actually bit; an untouched record carries
    no such key.
    """
    _validate_message_sizing_bounds(
        recipients_limit=recipients_limit,
        attachments_limit=attachments_limit,
        body_max_chars=body_max_chars,
    )

    truncated_fields: dict[str, int] = {}

    payload: dict[str, object] = {
        "id": message.get("id"),
        "subject": _cap_presentation_string(
            message.get("subject"), _SUBJECT_CAP, "subject", truncated_fields
        ),
        "sender_name": _cap_presentation_string(
            message.get("sender_name"),
            _DISPLAY_NAME_CAP,
            "sender_name",
            truncated_fields,
        ),
        "sender_email": message.get("sender_email"),
        "direction": message.get("direction"),
        "sent_at": message.get("sent_at"),
        "received_at": message.get("received_at"),
        "message_id": message.get("message_id"),
        "in_reply_to": message.get("in_reply_to"),
        "references": message.get("references"),
        "pst_source": message.get("pst_source"),
        "folder_path": _cap_presentation_string(
            message.get("folder_path"),
            _FOLDER_PATH_CAP,
            "folder_path",
            truncated_fields,
        ),
    }

    shaped_recipients, recipients_total, recipients_truncated = _shape_recipients(
        message, recipients_limit=recipients_limit, truncated_fields=truncated_fields
    )
    for role, entries in shaped_recipients.items():
        payload[role] = entries
    payload["recipients_total"] = recipients_total
    payload["recipients_truncated"] = recipients_truncated

    shaped_attachments, attachments_total, attachments_truncated = (
        _shape_message_attachments(
            message.get("attachments"),
            attachments_limit=attachments_limit,
            truncated_fields=truncated_fields,
        )
    )
    payload["attachments"] = shaped_attachments
    payload["attachment_names"] = [
        entry["attachment_name"] for entry in shaped_attachments
    ]
    payload["attachments_total"] = attachments_total
    payload["attachments_truncated"] = attachments_truncated

    if include_body:
        rendered, body_truncated, body_total_chars = render_message_body(
            message.get("body_text"),
            new_only=new_only,
            body_max_chars=body_max_chars,
        )
        payload["rendered_body_text"] = rendered
        payload["body_truncated"] = body_truncated
        payload["body_total_chars"] = body_total_chars

    if truncated_fields:
        payload["truncated_fields"] = truncated_fields

    return payload


def _message_record_reduction_states(
    message: dict[str, object],
    *,
    include_body: bool,
    recipients_limit: int,
    attachments_limit: int,
    new_only: bool,
    body_max_chars: int,
) -> list[Callable[[], dict[str, object]]]:
    """Ordered within-record reduction *tail* for one message record.

    Implements `mail_show`'s within-record ladder order (spec: "Each tool
    degrades along a ladder that fits its payload shape" -- the single
    record is never dropped, only shrunk within itself): the rendered body
    first, down to `_BODY_MAX_CHARS_RANGE`'s low end, then the recipient
    collections, then the attachment collection, each converging via
    :func:`_ladder_halving_steps`.

    Returns only the *reduction* states -- the caller's own unreduced
    bounds are a separate, prior ladder state that this helper does not
    build, so `mail_thread` can reuse this exact tail once its own
    message-count reduction has left a single message, on top of its own
    unreduced state, without duplicating this order a second time.

    Every state still goes through :func:`shape_message_payload`, whose
    identifier and locator fields are always passed through uncapped
    regardless of these bounds -- no state here ever truncates one to fit.
    """

    def make_state(*, bmc: int, rl: int, al: int) -> Callable[[], dict[str, object]]:
        def state() -> dict[str, object]:
            return shape_message_payload(
                message,
                include_body=include_body,
                recipients_limit=rl,
                attachments_limit=al,
                new_only=new_only,
                body_max_chars=bmc,
            )

        return state

    states: list[Callable[[], dict[str, object]]] = []

    body_floor = body_max_chars
    if include_body:
        for reduced_body_max_chars in _ladder_halving_steps(
            body_max_chars, _BODY_MAX_CHARS_RANGE[0]
        ):
            states.append(
                make_state(
                    bmc=reduced_body_max_chars,
                    rl=recipients_limit,
                    al=attachments_limit,
                )
            )
            body_floor = reduced_body_max_chars

    recipients_floor = recipients_limit
    for reduced_recipients_limit in _ladder_halving_steps(
        recipients_limit, _RECIPIENTS_LIMIT_RANGE[0]
    ):
        states.append(
            make_state(
                bmc=body_floor, rl=reduced_recipients_limit, al=attachments_limit
            )
        )
        recipients_floor = reduced_recipients_limit

    for reduced_attachments_limit in _ladder_halving_steps(
        attachments_limit, _ATTACHMENTS_LIMIT_RANGE[0]
    ):
        states.append(
            make_state(
                bmc=body_floor, rl=recipients_floor, al=reduced_attachments_limit
            )
        )

    return states


def shape_search_result_item(
    message: dict[str, object],
    *,
    full_body: bool,
    body_max_chars: int,
    recipients_limit: int,
    attachments_limit: int,
) -> dict[str, object]:
    _validate_message_sizing_bounds(
        recipients_limit=recipients_limit,
        attachments_limit=attachments_limit,
        body_max_chars=body_max_chars,
    )
    truncated_fields: dict[str, int] = {}
    shaped_attachments, attachments_total, attachments_truncated = (
        _shape_message_attachments(
            message.get("attachments"),
            attachments_limit=attachments_limit,
            truncated_fields=truncated_fields,
            # issue #178: search items carry attachment identity, not
            # locators -- see _shape_attachment's docstring.
            include_locators=False,
        )
    )
    payload: dict[str, object] = {
        "id": message.get("id"),
        "subject": _cap_presentation_string(
            message.get("subject"), _SUBJECT_CAP, "subject", truncated_fields
        ),
        "sender_name": _cap_presentation_string(
            message.get("sender_name"),
            _DISPLAY_NAME_CAP,
            "sender_name",
            truncated_fields,
        ),
        "sender_email": message.get("sender_email"),
        "direction": message.get("direction"),
        "sent_at": message.get("sent_at"),
        "received_at": message.get("received_at"),
        "pst_source": message.get("pst_source"),
        "folder_path": _cap_presentation_string(
            message.get("folder_path"),
            _FOLDER_PATH_CAP,
            "folder_path",
            truncated_fields,
        ),
        "attachments": shaped_attachments,
        "attachment_names": [
            attachment["attachment_name"] for attachment in shaped_attachments
        ],
        "snippet": _cap_presentation_string(
            message.get("snippet"), _SNIPPET_CAP, "snippet", truncated_fields
        ),
        # issue #177: two short scalars saying why the row matched. Both are
        # None without a keyword, and `match_field` is None when the literal
        # is in neither subject nor body -- the row matched an indexed field
        # this item does not show (recipients, attachment names). Enumerated
        # from the query layer's own values rather than validated here; the
        # allowlist is a field boundary, not a value boundary.
        "match_field": message.get("match_field"),
        "match_in": message.get("match_in"),
        "attachments_total": attachments_total,
        "attachments_truncated": attachments_truncated,
    }
    if full_body:
        shaped_recipients, recipients_total, recipients_truncated = _shape_recipients(
            message,
            recipients_limit=recipients_limit,
            truncated_fields=truncated_fields,
        )
        rendered, body_truncated, body_total_chars = _finalize_rendered_body(
            message.get("rendered_body_text"),
            new_only=False,
            body_max_chars=body_max_chars,
        )
        payload.update(
            {
                "message_id": message.get("message_id"),
                "dedup_key": message.get("dedup_key"),
                "in_reply_to": message.get("in_reply_to"),
                "references": message.get("references"),
                "to": shaped_recipients["to"],
                "cc": shaped_recipients["cc"],
                "bcc": shaped_recipients["bcc"],
                "rendered_body_text": rendered,
                "body_truncated": body_truncated,
                "body_total_chars": body_total_chars,
                "recipients_total": recipients_total,
                "recipients_truncated": recipients_truncated,
            }
        )
    if truncated_fields:
        payload["truncated_fields"] = truncated_fields
    return payload


def _shape_contact_history_contact(
    entry: dict[str, object],
    *,
    index: int,
    truncated_fields: dict[str, int],
) -> dict[str, object]:
    return {
        "email": entry.get("email"),
        "display_name": _cap_presentation_string(
            entry.get("display_name"),
            _DISPLAY_NAME_CAP,
            f"contacts[{index}].display_name",
            truncated_fields,
        ),
        "msg_count": entry.get("msg_count"),
        "last_seen": entry.get("last_seen"),
    }


def _contact_history_summary_with_reduced_recent(
    summary: _ContactHistorySummary, *, recent_count: int
) -> _ContactHistorySummary:
    """Return a shallow copy of *summary* with its attachments overview's
    ``recent`` list sliced to *recent_count* items.

    ``attachments.total_count`` is carried over unchanged -- it is the
    aggregate's own scalar and this reduction never touches it -- so
    :func:`shape_contact_history_summary`'s own ``attachments_truncated``
    formula (``total_count > len(recent)``) reports a budget-driven drop
    the same way it reports a caller-driven one.
    """
    # contact_history._run_aggregation produces the nested attachment mapping
    # with an int total_count and list[str] recent.
    attachments = cast(
        _ContactHistoryAttachments, dict(summary.get("attachments") or {})
    )
    recent = list(attachments.get("recent") or [])
    reduced_summary = dict(summary)
    reduced_summary["attachments"] = {**attachments, "recent": recent[:recent_count]}
    # The copied producer summary retains every required key and replaces only
    # attachments with the same _run_aggregation shape.
    return cast(_ContactHistorySummary, reduced_summary)


def shape_contact_history_summary(
    summary: _ContactHistorySummary, *, contacts_limit: int
) -> dict[str, object]:
    _validate_int_range(
        contacts_limit, "contacts_limit", *_MAIL_CONTACT_HISTORY_CONTACTS_LIMIT_RANGE
    )
    truncated_fields: dict[str, int] = {}
    contacts = list(summary.get("contacts") or [])
    bounded_contacts, _, contacts_truncated = _bound_collection(
        contacts,
        contacts_limit,
        param_name="contacts_limit",
        low=_MAIL_CONTACT_HISTORY_CONTACTS_LIMIT_RANGE[0],
        high=_MAIL_CONTACT_HISTORY_CONTACTS_LIMIT_RANGE[1],
    )
    # contact_history._run_aggregation produces the nested attachment mapping
    # with an int total_count and list[str] recent.
    attachments = cast(
        _ContactHistoryAttachments, dict(summary.get("attachments") or {})
    )
    recent_attachments = list(attachments.get("recent") or [])
    shaped_recent = [
        _cap_presentation_string(
            name,
            _ATTACHMENT_NAME_CAP,
            f"attachments.recent[{index}]",
            truncated_fields,
        )
        for index, name in enumerate(recent_attachments)
    ]
    total_attachment_count = int(attachments.get("total_count") or 0)
    payload: dict[str, object] = {
        "query": summary.get("query"),
        "query_type": summary.get("query_type"),
        "date_from": summary.get("date_from"),
        "date_to": summary.get("date_to"),
        "first_message_at": summary.get("first_message_at"),
        "last_message_at": summary.get("last_message_at"),
        "total_messages": summary.get("total_messages"),
        "inbound_count": summary.get("inbound_count"),
        "outbound_count": summary.get("outbound_count"),
        "unknown_count": summary.get("unknown_count"),
        "distinct_contacts": summary.get("distinct_contacts"),
        "contacts": [
            _shape_contact_history_contact(
                entry, index=index, truncated_fields=truncated_fields
            )
            for index, entry in enumerate(bounded_contacts)
        ],
        "attachments": {
            "total_count": total_attachment_count,
            "recent": shaped_recent,
        },
        "contacts_truncated": contacts_truncated,
        "attachments_truncated": total_attachment_count > len(shaped_recent),
    }
    if truncated_fields:
        payload["truncated_fields"] = truncated_fields
    return payload


def _shape_attachment_carrying_message(
    entry: dict[str, object],
    *,
    index: int,
    truncated_fields: dict[str, int],
) -> dict[str, object]:
    return {
        "message_db_id": entry.get("message_db_id"),
        "rfc_message_id": entry.get("rfc_message_id"),
        "dedup_key": entry.get("dedup_key"),
        "sent_at": entry.get("sent_at"),
        "sender_email": entry.get("sender_email"),
        "subject": _cap_presentation_string(
            entry.get("subject"),
            _SUBJECT_CAP,
            f"carrying_messages[{index}].subject",
            truncated_fields,
        ),
    }


def _attachment_search_payload(
    entries: Sequence[dict[str, object]],
    *,
    carrying_messages_limit: int,
    total_matched: int,
) -> dict[str, object]:
    """Build the ``mail_attachments``-shaped payload from *entries*.

    ``total_matched`` is always the full, unreduced match count, passed in
    by the caller rather than derived here -- neither the caller's own
    ``limit`` nor a later budget-driven entry drop ever changes it.
    ``returned`` and ``truncated`` are derived from *entries* alone, so the
    same formula reports a caller-bound truncation and a budget-driven one
    the same way (spec: "A budget-driven entry drop SHALL leave
    total_matched reporting the full match count, SHALL update returned,
    and SHALL set truncated true").
    """
    shaped = [
        shape_attachment_search_item(
            entry, carrying_messages_limit=carrying_messages_limit
        )
        for entry in entries
    ]
    return {
        "attachments": shaped,
        "total_matched": total_matched,
        "returned": len(shaped),
        "truncated": total_matched > len(shaped),
    }


def shape_attachment_search_item(
    entry: dict[str, object], *, carrying_messages_limit: int
) -> dict[str, object]:
    _validate_int_range(
        carrying_messages_limit,
        "carrying_messages_limit",
        *_MAIL_ATTACHMENTS_CARRYING_MESSAGES_LIMIT_RANGE,
    )
    truncated_fields: dict[str, int] = {}
    # query.search_attachments produces carrying_messages as a list of
    # message-summary mappings for both deduplicated and passthrough results.
    carrying_message_entries = cast(
        list[dict[str, object]], entry.get("carrying_messages") or []
    )
    carrying_messages, carrying_messages_total, carrying_messages_truncated = (
        _bound_collection(
            list(carrying_message_entries),
            carrying_messages_limit,
            param_name="carrying_messages_limit",
            low=_MAIL_ATTACHMENTS_CARRYING_MESSAGES_LIMIT_RANGE[0],
            high=_MAIL_ATTACHMENTS_CARRYING_MESSAGES_LIMIT_RANGE[1],
        )
    )
    payload: dict[str, object] = {
        "attachment_id": entry.get("attachment_id"),
        "message_db_id": entry.get("message_db_id"),
        "rfc_message_id": entry.get("rfc_message_id"),
        "dedup_key": entry.get("dedup_key"),
        "sent_at": entry.get("sent_at"),
        "sender_email": entry.get("sender_email"),
        "subject": _cap_presentation_string(
            entry.get("subject"), _SUBJECT_CAP, "subject", truncated_fields
        ),
        "attachment_name": _cap_presentation_string(
            entry.get("attachment_name"),
            _ATTACHMENT_NAME_CAP,
            "attachment_name",
            truncated_fields,
        ),
        "size_bytes": entry.get("size_bytes"),
        "mime_type": entry.get("mime_type"),
        "content_hash": entry.get("content_hash"),
        "storage_path": entry.get("storage_path"),
        "folder_path": _cap_presentation_string(
            entry.get("folder_path"),
            _FOLDER_PATH_CAP,
            "folder_path",
            truncated_fields,
        ),
        "exportable": entry.get("exportable"),
        "carrying_messages": [
            _shape_attachment_carrying_message(
                carrying_message, index=index, truncated_fields=truncated_fields
            )
            for index, carrying_message in enumerate(carrying_messages)
        ],
        "carrying_messages_total": carrying_messages_total,
        "carrying_messages_truncated": carrying_messages_truncated,
    }
    if truncated_fields:
        payload["truncated_fields"] = truncated_fields
    return payload


def _normalize_text_filter(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_day_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    """Parse a `mail_search` `from_date` / `to_date` bound.

    Uses this tool's own existing parser (`date.fromisoformat`), unchanged
    in what it accepts -- including the basic-date and ISO-week-date forms
    `20260721` and `2026-W30-2` it has always taken (design.md > "Move the
    MCP tool onto the shared bound builder without changing what it
    does"). The resulting date is handed to the shared, parse-free
    :func:`whole_day_bound` instead of a private Asia/Taipei construction.

    ``None``, empty, or whitespace-only *value* means the filter was not
    supplied and returns ``None`` -- not an error, unchanged from today.
    """
    checked = normalize_optional_date_text(value)
    if checked is None:
        return None
    # This tool strips before parsing on its own -- unchanged from before
    # the shared helper existed; the shared helper no longer does this.
    stripped = checked.strip()
    try:
        parsed = date.fromisoformat(stripped)
    except ValueError as exc:
        # The formatter is shared with the CLI, so the date value remains a
        # builder fragment here; the delivery builder caps it before filling
        # the formatter's placeholder.
        # ponytail: feeding the formatter a literal "{date_value}" token
        # borrows its !r quoting to get a static template, but the builder
        # fills the real value with str(), so a value containing quotes or
        # newlines loses the escaping the CLI path still applies. Ceiling
        # accepted: the alternatives are restructuring the CLI-shared
        # datetime_bounds formatter or duplicating its wording here. Upgrade
        # path: return structured fields from it, then cap the value alone.
        if end_of_day:
            raise build_tool_error(
                ValueError,
                format_invalid_date_message(
                    "{date_value}", "to_date", expected=_ISO_DATE_ONLY_EXPECTED
                ),
                date_value=value,
            ) from exc
        raise build_tool_error(
            ValueError,
            format_invalid_date_message(
                "{date_value}", "from_date", expected=_ISO_DATE_ONLY_EXPECTED
            ),
            date_value=value,
        ) from exc
    return whole_day_bound(parsed, end_of_day=end_of_day)


def _build_filters(
    *,
    keyword: str | None,
    sender: str | None,
    recipient: str | None,
    sender_domain: str | None,
    recipient_domain: str | None,
    participants_domain: str | None,
    from_date: str | None,
    to_date: str | None,
    attachment_name: str | None,
    attachment_ext: str | None = None,
) -> SearchFilters:
    return SearchFilters(
        keyword=_normalize_text_filter(keyword),
        sender=_normalize_text_filter(sender),
        recipient=_normalize_text_filter(recipient),
        sender_domain=_normalize_text_filter(sender_domain),
        recipient_domain=_normalize_text_filter(recipient_domain),
        participants_domain=_normalize_text_filter(participants_domain),
        sent_from=_normalize_day_bound(from_date, end_of_day=False),
        sent_to=_normalize_day_bound(to_date, end_of_day=True),
        attachment_name=_normalize_text_filter(attachment_name),
        attachment_ext=_normalize_text_filter(attachment_ext),
    )


def _has_any_filter(filters: SearchFilters) -> bool:
    return any(
        (
            filters.keyword,
            filters.sender,
            filters.recipient,
            filters.sender_domain,
            filters.recipient_domain,
            filters.participants_domain,
            filters.sent_from is not None,
            filters.sent_to is not None,
            filters.attachment_name,
            filters.attachment_ext,
        )
    )


def _halving_ladder_states(
    start: int, floor: int, builder: Callable[[int], dict[str, object]]
) -> tuple[list[Callable[[], dict[str, object]]], int]:
    """One halving-reduction phase of a ladder.

    Calls ``builder(reduced)`` for each step of :func:`_ladder_halving_steps`
    from *start* down to *floor*. Returns the ordered reduction states plus
    the floor value the phase actually converged to (*start* itself when no
    reduction was possible), so a later phase can build on it.
    """
    states: list[Callable[[], dict[str, object]]] = []
    floor_value = start
    for reduced in _ladder_halving_steps(start, floor):
        states.append(lambda reduced=reduced: builder(reduced))
        floor_value = reduced
    return states, floor_value


def _two_phase_ladder_states(
    first_range: tuple[int, int],
    first_builder: Callable[[int], dict[str, object]],
    second_range: tuple[int, int],
    second_builder: Callable[[int, int], dict[str, object]],
) -> list[Callable[[], dict[str, object]]]:
    """Two-phase halving ladder tail shared by `mail_contact_history` and
    `mail_attachments` (design.md > "Give each tool its own degradation
    ladder"): phase 1 halves *first_range* = ``(start, floor)``, calling
    ``first_builder(reduced)`` at each step and recording the floor it
    converges to; phase 2 halves *second_range* using that recorded floor,
    calling ``second_builder(reduced, first_floor)`` at each step -- every
    phase-2 state uses the *same* first-phase floor, since phase 1 has
    already finished converging by the time phase 2 starts.

    Returns only the *reduction* states -- the caller's own unreduced state
    is a separate, prior ladder state this helper does not build.
    """
    first_states, first_floor = _halving_ladder_states(*first_range, first_builder)
    second_states, _ = _halving_ladder_states(
        *second_range, lambda reduced: second_builder(reduced, first_floor)
    )
    return [*first_states, *second_states]


def _flag_ladder_states(
    states: Sequence[Callable[[], dict[str, object]]],
) -> list[Callable[[], dict[str, object]]]:
    """Make each ladder state stamp its own ``response_truncated``.

    The flag has to sit inside the payload the ladder *measures*, not be
    added to the winning state afterwards. Stamping it after the walk would
    make the delivered result larger than the object that was measured --
    by the flag's own length, in both channels -- which contradicts the
    assembly-boundary rule that the object a tool measures is the object
    the host receives. The delta is small, but the guarantee is exact or it
    is not a guarantee.

    The flag is true iff a state's *content* (everything but the flag
    itself) differs from index 0's content -- not merely because a state
    sits at a later index. A reduction step whose bound was already at or
    above the actual size of what it bounds is a no-op: its content is
    byte-identical to the unreduced state's, and stamping it `True` purely
    by position would let that state claim a budget-driven reduction that
    never happened (and, because `true` serializes one character shorter
    than `false`, such a state can even slip under budget where its
    identical-content predecessor did not). Index 0 is always evaluated
    first by `walk_ladder`, so its content is cached before any later
    state's content is ever compared against it.
    """

    unreduced_content: dict[str, object] | None = None

    def stamped(
        state: Callable[[], dict[str, object]], is_unreduced: bool
    ) -> Callable[[], dict[str, object]]:
        def build() -> dict[str, object]:
            nonlocal unreduced_content
            payload = state()
            if is_unreduced:
                unreduced_content = dict(payload)
                reduced = False
            else:
                reduced = payload != unreduced_content
            payload["response_truncated"] = reduced
            return payload

        return build

    return [stamped(state, index == 0) for index, state in enumerate(states)]


_SEND_RECEIPT_CLOSE_CAP = 1000
_SEND_RECEIPT_LIST_CAP = 25
_SEND_RECEIPT_REASON_CAP = 100
_SEND_RECEIPT_SHORTENED_NOTE = (
    " This receipt was shortened to fit the response budget "
    "(response_truncated=true): accepted_count and refused_count are exact, "
    "but the refused/accepted lists and refused reasons may be partial or "
    "absent -- do not treat the listed refused addresses as the complete set. "
    "The mail WAS sent; do NOT resend this draft. The full refused list is "
    "recorded in this message's send-once marker on the host."
)


def _shortened_send_receipt(
    payload: dict[str, object],
    *,
    reason_chars: int | None = None,
    list_cap: int | None = None,
) -> dict[str, object]:
    """One reduced ``mail_send`` receipt: still success-shaped, never an error.

    The message is already out, so unlike a read tool this ladder has no
    "refuse" end -- its last state is a counts-only receipt that always fits.
    """
    out = dict(payload)
    out["response_truncated"] = True
    close_warning = out.get("close_warning")
    if isinstance(close_warning, str) and len(close_warning) > _SEND_RECEIPT_CLOSE_CAP:
        out["close_warning"] = (
            f"{close_warning[:_SEND_RECEIPT_CLOSE_CAP]}... [truncated] "
            "The mail WAS sent. Do NOT resend the draft."
        )
    accepted = payload.get("accepted")
    refused = payload.get("refused")
    if isinstance(accepted, list) and isinstance(refused, list):
        out["accepted_count"] = len(accepted)
        out["refused_count"] = len(refused)
        items: list[dict[str, object]] = refused
        if reason_chars is not None:
            items = [
                {
                    k: (str(v)[:reason_chars] if k == "reason" else v)
                    for k, v in item.items()
                    if not (k == "reason" and reason_chars == 0)
                }
                for item in items
            ]
        out["refused"] = items if list_cap is None else items[:list_cap]
        out["accepted"] = accepted if list_cap is None else accepted[:list_cap]
        out["warning"] = f"{payload['warning']}{_SEND_RECEIPT_SHORTENED_NOTE}"
    return out


def _send_receipt_states(
    payload: dict[str, object],
) -> list[Callable[[], dict[str, object]]]:
    """Unreduced receipt first (byte-identical to the pre-ladder shape), then
    reductions in order: cap reasons, drop reasons, cap lists, counts only."""
    steps: list[dict[str, int | None]] = [{}]
    if "refused" in payload:
        steps = [
            {"reason_chars": _SEND_RECEIPT_REASON_CAP},
            {"reason_chars": 0},
            {"reason_chars": 0, "list_cap": _SEND_RECEIPT_LIST_CAP},
            {"reason_chars": 0, "list_cap": 0},
        ]
    return [
        lambda: payload,
        *(lambda s=s: _shortened_send_receipt(payload, **s) for s in steps),
    ]


def create_server(
    *,
    database_path: str | Path | None = None,
) -> MCPServer:
    """Build the server: six read tools plus write tools (host files
    and mail drafting/sending, through :class:`MailWriter`).

    Two couplings to the protocol framework below are deliberate, not
    oversights. Do not "clean them up" without reading
    ``docs/mcp-server-contract.md`` first — both are load-bearing.

    1. The tools stay plain ``def``, not ``async def``. The framework
       dispatches sync tools to a worker thread, so safety comes from each
       call opening its own database connection, never from sqlite3 being
       thread-safe: a shared connection crossing threads raises
       ``ProgrammingError``.
    2. Reaching into the private ``_tool_manager`` (here and at each tool's
       ladder site, five places total) is how the two-channel result assembly
       gets *delegated* to the framework instead of reimplemented. The public
       enumeration methods return protocol-level descriptions that lack the
       conversion metadata this needs. Delegating makes drift impossible;
       reimplementing would only let us detect drift afterwards. The price is
       that a framework generation change lands here as a spec-level change.
    """

    resolved_database_path = resolve_database_default(database_path)
    reader = ReadOnlyMailReader(resolved_database_path)
    writer = MailWriter.for_database(resolved_database_path)
    server = MCPServer(_SERVER_NAME)

    def _build_success_result(tool_name: str, payload: object) -> CallToolResult:
        tool = server._tool_manager.get_tool(tool_name)
        assert tool is not None
        return build_call_tool_result(payload, tool=tool)

    @server.tool(
        name="mail_search",
        title="Search mail messages",
        description=_MAIL_SEARCH_DESCRIPTION,
        annotations=_read_only_annotations("Search mail messages"),
    )
    def mail_search(
        keyword: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        sender_domain: str | None = None,
        recipient_domain: str | None = None,
        participants_domain: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        attachment_name: str | None = None,
        limit: StrictInt = _MAIL_SEARCH_LIMIT_DEFAULT,
        full_body: StrictBool = False,
        body_max_chars: StrictInt = BODY_MAX_CHARS_DEFAULT,
        recipients_limit: StrictInt = _MAIL_SEARCH_RECIPIENTS_LIMIT_DEFAULT,
        attachments_limit: StrictInt = _MAIL_SEARCH_ATTACHMENTS_LIMIT_DEFAULT,
    ) -> Annotated[CallToolResult, list[dict[str, object]]]:
        filters = _build_filters(
            keyword=keyword,
            sender=sender,
            recipient=recipient,
            sender_domain=sender_domain,
            recipient_domain=recipient_domain,
            participants_domain=participants_domain,
            from_date=from_date,
            to_date=to_date,
            attachment_name=attachment_name,
        )
        if not _has_any_filter(filters):
            raise build_tool_error(
                ValueError, "At least one mail_search filter is required."
            )
        _validate_int_range(limit, "limit", *_MAIL_SEARCH_LIMIT_RANGE)
        _validate_message_sizing_bounds(
            recipients_limit=recipients_limit,
            attachments_limit=attachments_limit,
            body_max_chars=body_max_chars,
        )
        try:
            results = reader.search(
                filters=filters,
                limit=limit,
                full_body=full_body,
            )
        except ValueError as exc:
            if str(exc).startswith("Invalid FTS keyword query:"):
                raise _invalid_keyword_syntax_error(exc) from exc
            raise build_tool_error(ValueError, "{detail}", detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise _missing_database_error(resolved_database_path) from exc
        except sqlite3.Error as exc:
            raise _database_runtime_error(resolved_database_path, exc) from exc
        except Exception as exc:
            raise _unexpected_tool_error("searching mail", exc) from exc
        payload = [
            shape_search_result_item(
                message,
                full_body=full_body,
                body_max_chars=body_max_chars,
                recipients_limit=recipients_limit,
                attachments_limit=attachments_limit,
            )
            for message in results
        ]
        delivered = _build_success_result("mail_search", payload)
        # design.md > "Give each tool its own degradation ladder":
        # `mail_search`'s frozen contract carries every row its filters and
        # `limit` selected and forbids both an application-level wrapper
        # and a silently shortened list -- a bare list has nowhere honest
        # to record an omission. It is therefore still measured against
        # the budget, but over budget it refuses rather than degrading.
        # Task 3.2 form 1: name the sizing parameters this invocation
        # actually left untried, per invocation.
        if measure_call_tool_result_chars(delivered) > MCP_RESPONSE_BUDGET_CHARS:
            raise _mail_search_over_budget_error(
                limit=limit,
                full_body=full_body,
                body_max_chars=body_max_chars,
                recipients_limit=recipients_limit,
                attachments_limit=attachments_limit,
            )
        return delivered

    @server.tool(
        name="mail_show",
        title="Show one stored message",
        description=_MAIL_SHOW_DESCRIPTION,
        annotations=_read_only_annotations("Show one stored message"),
    )
    def mail_show(
        message_id: StrictInt | StrictStr,
        new_only: StrictBool = False,
        body_max_chars: StrictInt = BODY_MAX_CHARS_DEFAULT,
        recipients_limit: StrictInt = _MAIL_SHOW_RECIPIENTS_LIMIT_DEFAULT,
        attachments_limit: StrictInt = _MAIL_SHOW_ATTACHMENTS_LIMIT_DEFAULT,
    ) -> Annotated[CallToolResult, dict[str, object]]:
        # design.md > "Declare mail_show's identifier as integer-or-string and
        # reject inside the tool": the schema accepts int | str so a Message-ID
        # string -- and a stringified integer, which is what the bridge client
        # actually sends -- reaches this body (an `int`-only schema would
        # reject it before this line ever ran). Classify into the three
        # ordered, mutually exclusive, exhaustive classes from design.md >
        # "Admit an ASCII decimal string as a stored id, redirect only a
        # Message-ID candidate", ahead of the sizing validation so an invalid
        # identifier still outranks an out-of-range sizing argument.
        if isinstance(message_id, str):
            if _is_canonical_stored_id_string(message_id):
                message_id = int(message_id)
            elif "@" in message_id:
                raise build_tool_error(
                    ValueError,
                    "mail_show only accepts an integer stored id. Use mail_thread "
                    "to resolve an RFC822 Message-ID string.",
                )
            else:
                raise build_tool_error(
                    ValueError,
                    "mail_show received {received} which is not a valid stored "
                    "id. Provide an integer, or its plain ASCII decimal string "
                    "form within the stored-id range.",
                    received=message_id,
                )
        _validate_message_sizing_bounds(
            recipients_limit=recipients_limit,
            attachments_limit=attachments_limit,
            body_max_chars=body_max_chars,
        )
        try:
            message = reader.get_message(message_id)
        except KeyError as exc:
            raise build_tool_error(
                ValueError,
                "Stored message id {message_id} was not found. Use mail_search "
                "to find valid ids.",
                message_id=message_id,
            ) from exc
        except FileNotFoundError as exc:
            raise _missing_database_error(resolved_database_path) from exc
        except sqlite3.Error as exc:
            raise _database_runtime_error(resolved_database_path, exc) from exc
        except Exception as exc:
            raise _unexpected_tool_error(
                "retrieving message", exc, message_id=message_id
            ) from exc

        def unreduced_state() -> dict[str, object]:
            return shape_message_payload(
                message,
                include_body=True,
                recipients_limit=recipients_limit,
                attachments_limit=attachments_limit,
                new_only=new_only,
                body_max_chars=body_max_chars,
            )

        # design.md > "Give each tool its own degradation ladder": the
        # single record is never dropped, only shrunk within itself (body,
        # then recipients, then attachments). Exhaustion (task 3.2 form 3)
        # means the record's own unbounded identifier and locator fields
        # keep it over budget no matter how far body/recipients/attachments
        # shrink -- there is no narrowing input to name.
        states: list[Callable[[], dict[str, object]]] = [
            unreduced_state,
            *_message_record_reduction_states(
                message,
                include_body=True,
                recipients_limit=recipients_limit,
                attachments_limit=attachments_limit,
                new_only=new_only,
                body_max_chars=body_max_chars,
            ),
        ]
        tool = server._tool_manager.get_tool("mail_show")
        assert tool is not None
        try:
            outcome = walk_ladder(_flag_ladder_states(states), tool=tool)
        except LadderExhausted as exc:
            raise _mail_show_terminal_refusal(message.get("id")) from exc
        # The ladder already built and measured the delivered result, with
        # `response_truncated` inside the payload that was measured, so
        # returning it directly keeps "the object measured is the object
        # delivered" literally true. Rebuilding from the winning state and
        # stamping the flag afterwards would deliver something larger than
        # what passed the budget check.
        return outcome.delivered

    @server.tool(
        name="mail_thread",
        title="Show one mail thread",
        description=_MAIL_THREAD_DESCRIPTION,
        annotations=_read_only_annotations("Show one mail thread"),
    )
    def mail_thread(
        identifier: StrictInt | StrictStr,
        limit: StrictInt = _MAIL_THREAD_LIMIT_DEFAULT,
        full_body: StrictBool = False,
        new_only: StrictBool = False,
        body_max_chars: StrictInt = BODY_MAX_CHARS_DEFAULT,
        recipients_limit: StrictInt = _MAIL_THREAD_RECIPIENTS_LIMIT_DEFAULT,
        attachments_limit: StrictInt = _MAIL_THREAD_ATTACHMENTS_LIMIT_DEFAULT,
    ) -> Annotated[CallToolResult, dict[str, object]]:
        _validate_int_range(limit, "limit", *_MAIL_THREAD_LIMIT_RANGE)
        _validate_message_sizing_bounds(
            recipients_limit=recipients_limit,
            attachments_limit=attachments_limit,
            body_max_chars=body_max_chars,
        )
        try:
            # design.md > "Extract thread traversal so the true message count
            # survives the cap": the resolver (not walk_thread) is used here so
            # `total_messages` reflects the whole thread, uncapped by the CLI's
            # 100-message ceiling.
            all_ids = reader.resolve_thread_message_ids(identifier)
        except ThreadIdentifierError as exc:
            raise build_tool_error(
                ValueError, _THREAD_IDENTIFIER_ERROR_TEMPLATE
            ) from exc
        except KeyError as exc:
            raise build_tool_error(
                ValueError,
                "No message found for identifier {identifier}. Use mail_search "
                "to find valid identifiers.",
                identifier=repr(identifier),
            ) from exc
        except FileNotFoundError as exc:
            raise _missing_database_error(resolved_database_path) from exc
        except sqlite3.Error as exc:
            raise _database_runtime_error(resolved_database_path, exc) from exc
        except Exception as exc:
            raise _unexpected_tool_error(
                "resolving thread identifier", exc, identifier=identifier
            ) from exc

        total_messages = len(all_ids)
        # `all_ids` is already ascending by sent_at (resolve_thread_message_ids
        # contract); design.md > "Return the most recent messages when a
        # thread is truncated": taking the trailing slice of an ascending list
        # keeps the returned messages in ascending order while selecting the
        # most recent ones.
        selected_ids = all_ids[-limit:] if total_messages > limit else all_ids

        message_cache: dict[int, dict[str, object]] = {}

        def cached_get_message(db_id: int) -> dict[str, object]:
            if db_id not in message_cache:
                try:
                    message_cache[db_id] = reader.get_message(db_id)
                except FileNotFoundError as exc:
                    raise _missing_database_error(resolved_database_path) from exc
                except sqlite3.Error as exc:
                    raise _database_runtime_error(resolved_database_path, exc) from exc
                except Exception as exc:
                    raise _unexpected_tool_error(
                        "retrieving message", exc, message_id=db_id
                    ) from exc
            return message_cache[db_id]

        def shape_with_original_bounds(db_id: int) -> dict[str, object]:
            return shape_message_payload(
                cached_get_message(db_id),
                include_body=full_body,
                recipients_limit=recipients_limit,
                attachments_limit=attachments_limit,
                new_only=new_only,
                body_max_chars=body_max_chars,
            )

        def build_thread_payload(
            messages: list[dict[str, object]],
        ) -> dict[str, object]:
            return {
                "messages": messages,
                "total_messages": total_messages,
                "returned_messages": len(messages),
                # A budget-driven drop is counted the same way as the
                # caller's own `limit` truncation (spec: "A budget-driven
                # message drop SHALL be counted into omitted_earlier_count
                # and SHALL leave total_messages reporting the true thread
                # size") -- both are just "how many earlier messages
                # weren't returned".
                "omitted_earlier_count": total_messages - len(messages),
            }

        def message_ids_state(ids: Sequence[int]) -> dict[str, object]:
            return build_thread_payload(
                [shape_with_original_bounds(db_id) for db_id in ids]
            )

        # design.md > "Give each tool its own degradation ladder": messages
        # drop from the oldest end first (already `selected_ids`'s own
        # order -- ascending, most recent last), converging by halving down
        # to a single message. No LadderExhausted handling here on purpose
        # -- task 3.2 (a later group) converts an exhausted ladder into the
        # terminal refusal.
        states: list[Callable[[], dict[str, object]]] = [
            lambda: message_ids_state(selected_ids)
        ]
        for reduced_count in _ladder_halving_steps(len(selected_ids), 1):
            reduced_ids = selected_ids[-reduced_count:]
            states.append(lambda ids=reduced_ids: message_ids_state(ids))

        # Once one message remains, apply mail_show's own within-record
        # order to it (body, then recipients, then attachments). Reusing
        # `_message_record_reduction_states` here, rather than a second
        # copy, is what keeps the two tools' within-record order identical
        # by construction. `final_message_id` is always part of every
        # message-count state above (it is the most recent id in every
        # trailing slice of `selected_ids`), so this lookup is a cache hit
        # once any of those states actually runs, and costs one read at
        # most.
        final_message_id = selected_ids[-1]
        for record_state in _message_record_reduction_states(
            cached_get_message(final_message_id),
            include_body=full_body,
            recipients_limit=recipients_limit,
            attachments_limit=attachments_limit,
            new_only=new_only,
            body_max_chars=body_max_chars,
        ):
            states.append(
                lambda record_state=record_state: build_thread_payload([record_state()])
            )

        tool = server._tool_manager.get_tool("mail_thread")
        assert tool is not None
        try:
            outcome = walk_ladder(_flag_ladder_states(states), tool=tool)
        except LadderExhausted as exc:
            # mail_thread accepts no narrowing input (an identifier and
            # sizing parameters only; design.md > form 2/3 boundary rule),
            # so exhaustion is always form 3, blaming the single message
            # the ladder converged to.
            raise _mail_thread_terminal_refusal(final_message_id) from exc
        # Same reasoning as mail_show: the measured object carries the flag
        # already, so the measured object is what ships.
        return outcome.delivered

    @server.tool(
        name="mail_contact_history",
        title="Summarize correspondence history",
        description=_MAIL_CONTACT_HISTORY_DESCRIPTION,
        annotations=_read_only_annotations("Summarize correspondence history"),
    )
    def mail_contact_history(
        contact: str | None = None,
        domain: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        attachments_limit: StrictInt = _MAIL_CONTACT_HISTORY_ATTACHMENTS_LIMIT_DEFAULT,
        contacts_limit: StrictInt = _MAIL_CONTACT_HISTORY_CONTACTS_LIMIT_DEFAULT,
    ) -> Annotated[CallToolResult, dict[str, object]]:
        trimmed_contact = _normalize_text_filter(contact)
        trimmed_domain = _normalize_text_filter(domain)
        if contact is not None and trimmed_contact is None:
            raise build_tool_error(
                ValueError,
                "contact must be non-blank. {legal_invocations}",
                legal_invocations=_CONTACT_HISTORY_LEGAL_INVOCATIONS,
            )
        if domain is not None and trimmed_domain is None:
            raise build_tool_error(
                ValueError,
                "domain must be non-blank. {legal_invocations}",
                legal_invocations=_CONTACT_HISTORY_LEGAL_INVOCATIONS,
            )
        if (trimmed_contact is None) == (trimmed_domain is None):
            raise build_tool_error(
                ValueError,
                "{legal_invocations}",
                legal_invocations=_CONTACT_HISTORY_LEGAL_INVOCATIONS,
            )
        selector_name = "contact" if trimmed_contact is not None else "domain"
        selector_value = (
            trimmed_contact if trimmed_contact is not None else trimmed_domain
        )
        assert selector_value is not None
        if len(selector_value) > _CONTACT_HISTORY_SELECTOR_MAX_CHARS:
            raise build_tool_error(
                ValueError,
                "Invalid {selector_name}: maximum length is {max_chars} "
                "characters. {legal_invocations}",
                selector_name=selector_name,
                max_chars=_CONTACT_HISTORY_SELECTOR_MAX_CHARS,
                legal_invocations=_CONTACT_HISTORY_LEGAL_INVOCATIONS,
            )
        _validate_int_range(
            attachments_limit,
            "attachments_limit",
            *_MAIL_CONTACT_HISTORY_ATTACHMENTS_LIMIT_RANGE,
        )
        _validate_int_range(
            contacts_limit,
            "contacts_limit",
            *_MAIL_CONTACT_HISTORY_CONTACTS_LIMIT_RANGE,
        )
        capped_date_from = cap_error_echo(date_from) if date_from is not None else None
        capped_date_to = cap_error_echo(date_to) if date_to is not None else None
        try:
            # aggregate_contact_history delegates to _run_aggregation, whose two
            # return branches construct the complete stable summary shape.
            summary = cast(
                _ContactHistorySummary,
                reader.contact_history(
                    contact=trimmed_contact,
                    domain=trimmed_domain,
                    date_from=capped_date_from,
                    date_to=capped_date_to,
                    attachments_limit=attachments_limit,
                ),
            )
        except ValueError:
            # Shared date/selector validation already carries its own bounded
            # fragment; do not cap the assembled correction at this boundary.
            raise
        except FileNotFoundError as exc:
            raise _missing_database_error(resolved_database_path) from exc
        except sqlite3.Error as exc:
            raise _database_runtime_error(resolved_database_path, exc) from exc
        except Exception as exc:
            raise _unexpected_tool_error("aggregating contact history", exc) from exc

        def unreduced_state() -> dict[str, object]:
            return shape_contact_history_summary(summary, contacts_limit=contacts_limit)

        # design.md > "Give each tool its own degradation ladder": the
        # summary scalars are never reduced -- shape_contact_history_summary
        # copies them straight from `summary` in every state below -- so the
        # only two things any state ever varies are `contacts_limit` (first)
        # and the attachments overview's `recent` slice (second, only once
        # `contacts_limit` has already converged to its floor). Exhaustion
        # is task 3.2 forms 2/3, decided below by whether `date_from` /
        # `date_to` already name a single Taipei day.
        recent_count = len((summary.get("attachments") or {}).get("recent") or [])
        states: list[Callable[[], dict[str, object]]] = [
            unreduced_state,
            *_two_phase_ladder_states(
                (contacts_limit, _MAIL_CONTACT_HISTORY_CONTACTS_LIMIT_RANGE[0]),
                lambda cl: shape_contact_history_summary(summary, contacts_limit=cl),
                (recent_count, _MAIL_CONTACT_HISTORY_ATTACHMENTS_LIMIT_RANGE[0]),
                lambda rc, cl: shape_contact_history_summary(
                    _contact_history_summary_with_reduced_recent(
                        summary, recent_count=rc
                    ),
                    contacts_limit=cl,
                ),
            ),
        ]

        tool = server._tool_manager.get_tool("mail_contact_history")
        assert tool is not None
        try:
            outcome = walk_ladder(_flag_ladder_states(states), tool=tool)
        except LadderExhausted as exc:
            raise _mail_contact_history_over_budget_error(
                date_from=date_from, date_to=date_to
            ) from exc
        # The ladder already built and measured the delivered result, with
        # `response_truncated` inside the payload that was measured, so
        # returning it directly keeps "the object measured is the object
        # delivered" literally true.
        return outcome.delivered

    @server.tool(
        name="mail_attachments",
        title="Search attachment metadata",
        description=_MAIL_ATTACHMENTS_DESCRIPTION,
        annotations=_read_only_annotations("Search attachment metadata"),
    )
    def mail_attachments(
        name_contains: str | None = None,
        ext: str | None = None,
        sender_domain: str | None = None,
        recipient_domain: str | None = None,
        participants_domain: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        dedupe: str | None = None,
        limit: StrictInt = _MAIL_ATTACHMENTS_LIMIT_DEFAULT,
        carrying_messages_limit: StrictInt = (
            _MAIL_ATTACHMENTS_CARRYING_MESSAGES_LIMIT_DEFAULT
        ),
    ) -> Annotated[CallToolResult, dict[str, object]]:
        filters = _build_filters(
            keyword=None,
            sender=None,
            recipient=None,
            sender_domain=sender_domain,
            recipient_domain=recipient_domain,
            participants_domain=participants_domain,
            from_date=from_date,
            to_date=to_date,
            attachment_name=name_contains,
            attachment_ext=ext,
        )
        if not _has_any_filter(filters):
            raise build_tool_error(
                ValueError,
                "{filter_hint}",
                filter_hint=_MAIL_ATTACHMENTS_FILTER_HINT,
            )
        _validate_int_range(limit, "limit", *_MAIL_ATTACHMENTS_LIMIT_RANGE)
        _validate_int_range(
            carrying_messages_limit,
            "carrying_messages_limit",
            *_MAIL_ATTACHMENTS_CARRYING_MESSAGES_LIMIT_RANGE,
        )
        if dedupe not in {None, "content-hash"}:
            raise build_tool_error(
                ValueError,
                "Unsupported dedupe value for dedupe: {dedupe}. Accepted values: "
                "content-hash.",
                dedupe=dedupe,
            )
        try:
            results = reader.search_attachments(
                filters=filters,
                dedupe=dedupe,
            )
        except FileNotFoundError as exc:
            raise _missing_database_error(resolved_database_path) from exc
        except sqlite3.Error as exc:
            raise _database_runtime_error(resolved_database_path, exc) from exc
        except Exception as exc:
            raise _unexpected_tool_error("searching attachments", exc) from exc
        total_matched = len(results)
        selected = results[:limit]

        def unreduced_state() -> dict[str, object]:
            return _attachment_search_payload(
                selected,
                carrying_messages_limit=carrying_messages_limit,
                total_matched=total_matched,
            )

        # design.md > "Give each tool its own degradation ladder":
        # `mail_attachments` reduces each entry's `carrying_messages`
        # collection first -- it is a content-hash de-duplication artifact
        # rather than a set of distinct matches -- and only then drops
        # attachment entries from the oldest end. `selected` is already
        # newest-first (search_attachments' own DESC-by-sent_at order), so
        # keeping a leading slice keeps the newest entries and drops the
        # oldest tail. Exhaustion is task 3.2 form 2 -- this tool accepts
        # free-form content filters and date bounds, so it never reaches
        # form 3 (design.md > form 2/3 boundary rule).
        states: list[Callable[[], dict[str, object]]] = [
            unreduced_state,
            *_two_phase_ladder_states(
                (
                    carrying_messages_limit,
                    _MAIL_ATTACHMENTS_CARRYING_MESSAGES_LIMIT_RANGE[0],
                ),
                lambda cml: _attachment_search_payload(
                    selected, carrying_messages_limit=cml, total_matched=total_matched
                ),
                (len(selected), 1),
                lambda reduced_entry_count, cml: _attachment_search_payload(
                    selected[:reduced_entry_count],
                    carrying_messages_limit=cml,
                    total_matched=total_matched,
                ),
            ),
        ]

        # Same reasoning as mail_show/mail_thread/mail_contact_history: the
        # measured object already carries the flag, so the measured object
        # is what ships.
        tool = server._tool_manager.get_tool("mail_attachments")
        assert tool is not None
        try:
            outcome = walk_ladder(_flag_ladder_states(states), tool=tool)
        except LadderExhausted as exc:
            raise _mail_attachments_over_budget_error(selected) from exc
        return outcome.delivered

    @server.tool(
        name="mail_sync_status",
        title="Show mail sync status",
        description=_MAIL_SYNC_STATUS_DESCRIPTION,
        annotations=_read_only_annotations("Show mail sync status"),
    )
    def mail_sync_status() -> Annotated[CallToolResult, dict[str, object]]:
        # design.md > "Report four flat fields and compute no verdict": no
        # ladder is needed here -- the payload is four short scalars, tiny
        # and fixed-size, so it is built once and delivered directly
        # through the same success-result helper `mail_search` shares
        # rather than adding a sixth `_tool_manager` reach site.
        try:
            status = reader.sync_status()
        except FileNotFoundError as exc:
            raise _missing_database_error(resolved_database_path) from exc
        except sqlite3.Error as exc:
            raise _database_runtime_error(resolved_database_path, exc) from exc
        except Exception as exc:
            raise _unexpected_tool_error("retrieving sync status", exc) from exc

        # Assembled field by field rather than by forwarding the query
        # layer's dict, so the delivered key set is fixed here and cannot
        # drift when that dict gains a field. Per design.md >
        # Implementation Contract, all four keys are always present and an
        # unknown value is reported as null rather than omitted.
        payload: dict[str, object] = {
            "last_sync_started_at": status.get("last_sync_started_at"),
            "last_sync_ended_at": status.get("last_sync_ended_at"),
            "last_sync_status": status.get("last_sync_status"),
            "latest_message_at": status.get("latest_message_at"),
        }
        # Measured at the assembly boundary like every other tool. There is no
        # ladder to walk -- four scalars carry nothing reducible -- but the
        # timestamp columns are unbounded TEXT, so "small by construction" is
        # an assumption about the data, not a guarantee. Measuring is what
        # turns it back into one.
        delivered = _build_success_result("mail_sync_status", payload)
        if measure_call_tool_result_chars(delivered) > MCP_RESPONSE_BUDGET_CHARS:
            raise _mail_sync_status_terminal_refusal()
        return delivered

    @server.tool(
        name="mail_attachment_export",
        title="Export attachments to a host folder",
        description=_MAIL_ATTACHMENT_EXPORT_DESCRIPTION,
        # Writes host files, but never overwrites and a repeat call only
        # reports skipped-existing -- so still non-destructive and idempotent.
        annotations=ToolAnnotations(
            title="Export attachments to a host folder",
            **{**_READ_ONLY_HINTS, "readOnlyHint": False},
        ),
    )
    def mail_attachment_export(
        attachment_ids: list[StrictInt],
        dest_dir: StrictStr,
    ) -> Annotated[CallToolResult, dict[str, object]]:
        if not 1 <= len(attachment_ids) <= _MAIL_ATTACHMENT_EXPORT_MAX_IDS:
            raise build_tool_error(
                ValueError,
                "attachment_ids must contain 1..{max_ids} ids; received {count}. "
                "Split the export into several calls.",
                max_ids=_MAIL_ATTACHMENT_EXPORT_MAX_IDS,
                count=len(attachment_ids),
            )
        if len(dest_dir) > _MAIL_ATTACHMENT_EXPORT_DEST_DIR_MAX_CHARS:
            raise build_tool_error(
                ValueError,
                "dest_dir is longer than {max_chars} characters. Choose a "
                "shorter absolute host path.",
                max_chars=_MAIL_ATTACHMENT_EXPORT_DEST_DIR_MAX_CHARS,
            )
        destination = Path(dest_dir)
        if not destination.is_absolute():
            raise build_tool_error(
                ValueError,
                "dest_dir must be an absolute host path; received {dest_dir}. "
                "Pass the host path of a folder you can read back.",
                dest_dir=dest_dir,
            )
        requested_ids = list(dict.fromkeys(attachment_ids))
        try:
            rows = reader.get_attachments_by_ids(requested_ids)
        except FileNotFoundError as exc:
            raise _missing_database_error(resolved_database_path) from exc
        except sqlite3.Error as exc:
            raise _database_runtime_error(resolved_database_path, exc) from exc
        except Exception as exc:
            raise _unexpected_tool_error("reading attachment metadata", exc) from exc
        rows_by_id = {cast(int, row["attachment_id"]): row for row in rows}
        try:
            outcomes = writer.export_attachments(
                [rows_by_id[i] for i in requested_ids if i in rows_by_id],
                destination,
            )
        except ValueError as exc:
            # The writer's refusals: inside the attachment store, or a folder
            # whose files open by themselves (#205).
            raise build_tool_error(
                ValueError,
                "dest_dir {dest_dir} is refused: {reason}",
                dest_dir=dest_dir,
                reason=str(exc),
            ) from exc
        except OSError as exc:
            # Only the up-front directory creation raises; per-file failures
            # come back as `failed` items.
            raise build_tool_error(
                ValueError,
                "dest_dir {dest_dir} could not be created: {detail}",
                dest_dir=dest_dir,
                detail=str(exc),
            ) from exc
        outcomes_by_id = {outcome.attachment_id: outcome for outcome in outcomes}

        truncated_fields: dict[str, int] = {}
        items: list[dict[str, object]] = []
        for index, attachment_id in enumerate(requested_ids):
            row = rows_by_id.get(attachment_id, {})
            outcome = outcomes_by_id.get(attachment_id)
            detail = outcome.detail if outcome else "attachment_id not found"
            items.append(
                {
                    "attachment_id": attachment_id,
                    "status": outcome.status if outcome else "failed",
                    "dest_path": (
                        str(outcome.destination_path)
                        if outcome and outcome.destination_path
                        else None
                    ),
                    "attachment_name": _cap_presentation_string(
                        row.get("attachment_name"),
                        _ATTACHMENT_NAME_CAP,
                        f"items[{index}].attachment_name",
                        truncated_fields,
                    ),
                    "size_bytes": row.get("size_bytes"),
                    "content_hash": row.get("content_hash"),
                    "detail": _cap_presentation_string(
                        detail,
                        _EXPORT_DETAIL_CAP,
                        f"items[{index}].detail",
                        truncated_fields,
                    ),
                }
            )
        payload: dict[str, object] = {"items": items}
        if truncated_fields:
            payload["truncated_fields"] = truncated_fields
        delivered = _build_success_result("mail_attachment_export", payload)
        if measure_call_tool_result_chars(delivered) > MCP_RESPONSE_BUDGET_CHARS:
            # Backstop only: the input bounds above keep this unreachable.
            # Files are already written, so say so -- a retry with fewer ids
            # reports them as skipped-existing with their paths.
            raise build_tool_error(
                ValueError,
                "mail_attachment_export result exceeds the delivery budget of "
                "{budget} characters. The files may already be written; call "
                "again with fewer attachment_ids to receive their paths.",
                budget=MCP_RESPONSE_BUDGET_CHARS,
            )
        return delivered

    @server.tool(
        name="mail_draft_reply",
        title="Draft a reply to a message",
        description=_MAIL_DRAFT_REPLY_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Draft a reply to a message",
            **{**_READ_ONLY_HINTS, "readOnlyHint": False, "idempotentHint": False},
        ),
    )
    def mail_draft_reply(
        message_id: StrictStr,
        body_text: StrictStr,
        to: list[StrictStr] | None = None,
        cc: list[StrictStr] | None = None,
        bcc: list[StrictStr] | None = None,
        exclude: list[StrictStr] | None = None,
        replace_recipients: StrictBool = False,
        attachments: list[StrictStr] | None = None,
    ) -> Annotated[CallToolResult, dict[str, object]]:
        try:
            result = writer.draft_reply(
                message_id=message_id,
                body_text=body_text,
                to=to,
                cc=cc,
                bcc=bcc,
                exclude=exclude,
                replace_recipients=replace_recipients,
                attachments=attachments,
            )
        except FileNotFoundError as exc:
            raise build_tool_error(
                ValueError,
                "Original message {message_id} not found in database",
                message_id=message_id,
            ) from exc
        except Exception as exc:
            raise build_tool_error(
                ValueError,
                "Failed to draft reply: {detail}",
                detail=str(exc),
            ) from exc

        attachments_payload: list[dict[str, object]] = [
            {
                "path": src_path,
                "name": name,
                "size_bytes": size,
            }
            for src_path, name, size in result.attachment_sources
        ]

        def _make_payload(body: str, *, truncated: bool = False) -> dict[str, object]:
            # A truncated body is not what the fingerprint covers, so the user
            # cannot have reviewed the draft being sent: withhold it (#189
            # "寄出的＝使用者看過的"). mail_send then has nothing to match.
            return {
                "draft_id": result.draft_id,
                "fingerprint": None if truncated else result.fingerprint,
                "message_id": result.message_id,
                "from": result.from_address,
                "subject": result.subject,
                "to": result.to,
                "cc": result.cc,
                "bcc": result.bcc,
                "body_text": body,
                "attachments": attachments_payload,
            }

        def _shrunk_payload(kept: int) -> dict[str, object]:
            # Keep head and tail halves (D-20260924-1 §5); built only when the
            # ladder actually reaches this rung.
            half = kept // 2
            omitted = len(result.body_text) - 2 * half
            return _make_payload(
                f"{result.body_text[:half]}\n\n"
                f"... [{omitted} characters truncated for delivery budget] ...\n\n"
                f"{result.body_text[-half:]}",
                truncated=True,
            )

        states: list[Callable[[], dict[str, object]]] = [
            lambda: _make_payload(result.body_text)
        ]
        kept = len(result.body_text)
        while kept > 1000:
            kept = max(1000, kept // 2)
            states.append(lambda kept=kept: _shrunk_payload(kept))

        tool = server._tool_manager.get_tool("mail_draft_reply")
        assert tool is not None
        try:
            outcome = walk_ladder(_flag_ladder_states(states), tool=tool)
        except LadderExhausted as exc:
            raise build_tool_error(
                ValueError,
                "mail_draft_reply result exceeds the delivery budget of "
                "{budget} characters",
                budget=MCP_RESPONSE_BUDGET_CHARS,
            ) from exc
        return outcome.delivered

    @server.tool(
        name="mail_send",
        title="Send a reviewed draft email",
        description=_MAIL_SEND_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Send a reviewed draft email",
            **{
                **_READ_ONLY_HINTS,
                "readOnlyHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
        ),
    )
    def mail_send(
        draft_id: StrictStr,
        fingerprint: StrictStr,
    ) -> Annotated[CallToolResult, dict[str, object]]:
        try:
            result = writer.send_draft(draft_id=draft_id, fingerprint=fingerprint)
        except FileNotFoundError as exc:
            raise build_tool_error(
                ValueError,
                "Not sent: draft not found in MCP drafts folder. Use the draft_id "
                "returned by mail_draft_reply. Draft: {draft_id}",
                draft_id=draft_id,
            ) from exc
        except Exception as exc:
            raise build_tool_error(
                ValueError,
                "Failed to send mail: {detail}",
                detail=str(exc),
            ) from exc

        payload: dict[str, object] = {
            "status": "sent",
            "message_id": result["message_id"],
            "draft_id": result["draft_id"],
        }
        if result.get("close_warning"):
            # #230: DATA accepted, only the closing QUIT failed -- still sent.
            payload["close_warning"] = result["close_warning"]
        if result.get("refused"):
            # #224: SMTP refused some RCPTs -- the message WAS sent to the rest.
            payload.update(
                status="partial",
                accepted=result["accepted"],
                refused=result["refused"],
                warning=result["warning"],
            )
        # The mail is already out: a receipt that cannot be shrunk into the
        # budget must still be delivered as a success, never an error.
        states = _send_receipt_states(payload)
        tool = server._tool_manager.get_tool("mail_send")
        assert tool is not None
        try:
            return walk_ladder(states, tool=tool).delivered
        except LadderExhausted:
            return _build_success_result("mail_send", states[-1]())

    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bes-mcp",
        description="MCP server for mailattic mail search and attachment export.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Optional SQLite database path override.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    create_server(
        database_path=args.database,
    ).run("stdio")


if __name__ == "__main__":
    main()

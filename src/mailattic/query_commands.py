"""The ``bes`` query command family: search / attachments / show / thread /
contact-history.

Owns the argparse declaration, the handlers, the output formats, the CLI date
input adapter (D-20260723-1: whole-day semantics, this surface's own input
syntax), and the exit codes. ``cli.py`` only
calls the two ``register_*`` entries; the domain rules stay in ``query.py``,
``contact_history.py``, and ``datetime_bounds.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

from .attachment_store import (
    AttachmentExportResult,
    export_attachments,
    resolve_store_root,
)
from .contact_history import aggregate_contact_history, serialise_summary
from .contact_history import parse_date_bound as _parse_contact_history_date_bound
from .datetime_bounds import (
    coerce_to_taipei_aware,
    format_invalid_date_message,
    normalize_optional_date_text,
    whole_day_bound,
)
from .query import (
    SearchFilters,
    ThreadIdentifierError,
    get_message,
    render_text,
    search_attachments,
    search_messages,
    strip_quote_tail,
    walk_thread,
)


def add_database_argument(parser: argparse.ArgumentParser) -> None:
    """Add the standard --database option to an ArgumentParser."""
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path.",
    )


def add_date_range_arguments(parser: argparse.ArgumentParser) -> None:
    """Add shared --from-date and --to-date filter options to a query parser."""
    parser.add_argument(
        "--from-date",
        dest="sent_from",
        default=None,
        help=(
            "Inclusive sent-at lower bound. A YYYY-MM-DD date means the "
            "whole Asia/Taipei calendar day; a timestamp with a UTC "
            "offset selects that instant; a timestamp without one is "
            "read as Asia/Taipei."
        ),
    )
    parser.add_argument(
        "--to-date",
        dest="sent_to",
        default=None,
        help=(
            "Inclusive sent-at upper bound. A YYYY-MM-DD date means the "
            "whole Asia/Taipei calendar day; a timestamp with a UTC "
            "offset selects that instant; a timestamp without one is "
            "read as Asia/Taipei."
        ),
    )


def register_message_commands(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register search, attachments, show and thread."""
    search_parser = subparsers.add_parser(
        "search",
        help="Search imported messages.",
    )
    add_database_argument(search_parser)
    search_parser.add_argument("--keyword", default=None, help="FTS keyword query.")
    search_parser.add_argument("--sender", default=None, help="Sender filter.")
    search_parser.add_argument("--recipient", default=None, help="Recipient filter.")
    add_date_range_arguments(search_parser)
    search_parser.add_argument(
        "--sender-domain",
        dest="sender_domain",
        action="append",
        default=None,
        help=(
            "Sender email domain filter; repeatable, values OR together "
            "(e.g. --sender-domain a.com --sender-domain b.com)."
        ),
    )
    search_parser.add_argument(
        "--recipient-domain",
        dest="recipient_domain",
        action="append",
        default=None,
        help=(
            "Recipient email domain filter (to/cc/bcc); repeatable, values OR together "
            "(e.g. --recipient-domain a.com --recipient-domain b.com)."
        ),
    )
    search_parser.add_argument(
        "--participants-domain",
        dest="participants_domain",
        action="append",
        default=None,
        help=(
            "Match if any participant (sender or recipient) has this domain; "
            "repeatable, values OR together "
            "(e.g. --participants-domain a.com --participants-domain b.com)."
        ),
    )
    search_parser.add_argument(
        "--attachment-name",
        dest="attachment_name",
        default=None,
        help="Attachment-name filter.",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of results to return.",
    )
    search_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit results as JSON.",
    )
    search_parser.add_argument(
        "--full-body",
        action="store_true",
        help=(
            "Emit ingestion-grade JSON fields including cleaned body text "
            "and thread metadata. Requires --json."
        ),
    )
    search_parser.set_defaults(handler=_handle_search)

    attachments_parser = subparsers.add_parser(
        "attachments",
        help="Search attachments and optionally export matching payloads.",
    )
    add_database_argument(attachments_parser)
    attachments_parser.add_argument(
        "--name-contains",
        dest="name_contains",
        default=None,
        help="Case-insensitive attachment-name substring filter.",
    )
    attachments_parser.add_argument(
        "--ext",
        default=None,
        help=(
            "Attachment extension filter (best-effort; attachment-name "
            "suffix with MIME-type fallback)."
        ),
    )
    attachments_parser.add_argument(
        "--participants-domain",
        dest="participants_domain",
        action="append",
        default=None,
        help=(
            "Match if any participant (sender or recipient) has this domain; "
            "repeatable, values OR together."
        ),
    )
    attachments_parser.add_argument(
        "--sender-domain",
        dest="sender_domain",
        action="append",
        default=None,
        help="Sender email domain filter; repeatable, values OR together.",
    )
    attachments_parser.add_argument(
        "--recipient-domain",
        dest="recipient_domain",
        action="append",
        default=None,
        help="Recipient email domain filter; repeatable, values OR together.",
    )
    add_date_range_arguments(attachments_parser)
    attachments_parser.add_argument(
        "--dedupe",
        choices=["content-hash"],
        default=None,
        help="Collapse duplicate payloads by non-null content hash.",
    )
    attachments_parser.add_argument(
        "--copy-to",
        dest="copy_to",
        type=Path,
        default=None,
        metavar="DIR",
        help="Copy each exportable match into this destination directory.",
    )
    attachments_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing destination files during --copy-to export.",
    )
    attachments_parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip already-existing destination files during --copy-to export.",
    )
    attachments_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit attachment rows as JSON when not exporting.",
    )
    attachments_parser.add_argument(
        "--attachment-root",
        type=Path,
        default=None,
        dest="attachment_root",
        metavar="DIR",
        help=(
            "Root directory for attachment CAS storage. "
            "Defaults to <db-dir>/attachments."
        ),
    )
    attachments_parser.set_defaults(handler=_handle_attachments)

    show_parser = subparsers.add_parser(
        "show",
        help="Show one imported message by stored identifier.",
    )
    show_parser.add_argument("message_id", type=int, help="Stored message identifier.")
    add_database_argument(show_parser)
    show_parser.add_argument(
        "--format",
        dest="format",
        choices=["raw", "json", "text"],
        default=None,
        help="Output format: raw (default), json, or text (HTML-cleaned).",
    )
    show_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_flag",
        help="[Deprecated] Alias for --format json. Use --format json instead.",
    )
    show_parser.add_argument(
        "--headers",
        action="store_true",
        dest="headers_flag",
        help=(
            "Output the raw RFC5322 header section verbatim and exit. "
            "Mutually exclusive with --format and --json."
        ),
    )
    show_parser.add_argument(
        "--new-only",
        action="store_true",
        dest="new_only",
        help=(
            "Strip the quoted-history tail; keep only the new content. "
            "Only valid with --format text. "
            "With --format raw/json exits non-zero."
        ),
    )
    show_parser.set_defaults(handler=_handle_show)

    thread_parser = subparsers.add_parser(
        "thread",
        help="Reconstruct a full conversation thread from a single message identifier.",
    )
    thread_parser.add_argument(
        "identifier",
        help=(
            "Stored message identifier: numeric db_id (int) or "
            "RFC822 Message-ID string (with or without angle brackets)."
        ),
    )
    add_database_argument(thread_parser)
    thread_parser.add_argument(
        "--format",
        dest="format",
        choices=["raw", "json", "text"],
        default=None,
        help="Output format: raw (default), json, or text (HTML-cleaned).",
    )
    thread_parser.add_argument(
        "--new-only",
        action="store_true",
        dest="new_only",
        help=(
            "Strip the quoted-history tail per message; keep only each message's "
            "new content. Only valid with --format text. "
            "With --format raw/json exits non-zero."
        ),
    )
    thread_parser.set_defaults(handler=_handle_thread)


def register_contact_history_command(
    subparsers: argparse._SubParsersAction,  # type: ignore[type-arg]
) -> None:
    """Register contact-history.

    A separate entry from `register_message_commands` only because argparse
    lists subcommands in registration order and `bes --help` must stay
    byte-identical: sync / outbound commands sit between the two groups.
    """
    contact_history_parser = subparsers.add_parser(
        "contact-history",
        help="Aggregate relationship summary for a contact or domain.",
    )
    selector_group = contact_history_parser.add_mutually_exclusive_group(required=True)
    selector_group.add_argument(
        "--contact",
        default=None,
        help="Exact email address to aggregate (sender or recipient).",
    )
    selector_group.add_argument(
        "--domain",
        default=None,
        help="Domain to aggregate (e.g. acme.com).",
    )
    contact_history_parser.add_argument(
        "--date-from",
        dest="date_from",
        default=None,
        help="Inclusive sent-at lower bound (YYYY-MM-DD).",
    )
    contact_history_parser.add_argument(
        "--date-to",
        dest="date_to",
        default=None,
        help="Inclusive sent-at upper bound (YYYY-MM-DD).",
    )
    contact_history_parser.add_argument(
        "--format",
        dest="format",
        choices=["table", "json"],
        default="table",
        help="Output format: table (default, TOON) or json.",
    )
    contact_history_parser.add_argument(
        "--attachments-limit",
        dest="attachments_limit",
        type=_non_negative_int,
        default=20,
        help="Max recent attachment filenames to include (default 20; non-negative).",
    )
    add_database_argument(contact_history_parser)
    contact_history_parser.set_defaults(handler=_handle_contact_history)


def _format_exception_message(exc: Exception) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def _handle_search(args: argparse.Namespace) -> int:
    if args.full_body and not args.json:
        print("error: full-body mode requires --json", file=sys.stderr)
        return 2
    try:
        filters = SearchFilters(
            keyword=args.keyword,
            sender=args.sender,
            recipient=args.recipient,
            sender_domain=args.sender_domain,
            recipient_domain=args.recipient_domain,
            participants_domain=args.participants_domain,
            sent_from=_parse_optional_datetime(args.sent_from, end_of_day=False),
            sent_to=_parse_optional_datetime(args.sent_to, end_of_day=True),
            attachment_name=args.attachment_name,
        )
        results = search_messages(
            database_path=args.database,
            filters=filters,
            limit=args.limit,
            full_body=args.full_body,
        )
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(_format_exception_message(exc))
        return 2
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(
                f"[{result['id']}] {result['subject']} | "
                f"{result['sender_email'] or result['sender_name'] or 'unknown sender'}"
                " | "
                f"{result['sent_at'] or 'no sent_at'} | "
                f"{result['folder_path']}"
            )
            if result["snippet"]:
                print(f"  {result['snippet']}")
    return 0


def _handle_attachments(args: argparse.Namespace) -> int:
    if args.overwrite and args.skip_existing:
        print(
            "error: conflicting flags: --overwrite cannot be combined "
            "with --skip-existing",
            file=sys.stderr,
        )
        return 2

    try:
        filters = SearchFilters(
            sender_domain=args.sender_domain,
            recipient_domain=args.recipient_domain,
            participants_domain=args.participants_domain,
            sent_from=_parse_optional_datetime(args.sent_from, end_of_day=False),
            sent_to=_parse_optional_datetime(args.sent_to, end_of_day=True),
            attachment_name=args.name_contains,
            attachment_ext=args.ext,
        )
        results = search_attachments(
            database_path=args.database,
            filters=filters,
            dedupe=args.dedupe,
        )
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2

    if args.copy_to is None:
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            _print_attachment_rows_table(results)
        return 0

    attachment_root = resolve_store_root(
        args.database,
        cli_override=args.attachment_root,
        env_override=os.environ.get("MAILATTIC_ATTACHMENT_ROOT"),
    )
    try:
        export_result = export_attachments(
            attachment_rows=results,
            store_root=attachment_root,
            destination_dir=args.copy_to,
            overwrite=args.overwrite,
            skip_existing=args.skip_existing,
        )
    except (OSError, ValueError) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2

    for line in export_result.report_lines:
        print(line)
    _print_attachment_export_summary(export_result)
    return 1 if export_result.failed > 0 else 0


def _resolve_show_format(args: argparse.Namespace) -> str | None:
    """Resolve --format and --json flag into a single format string.

    Returns the resolved format string ('raw', 'json', 'text'), or None
    if there is a conflict (caller should exit 2).

    Side effect: prints deprecation hint to stderr when --json alias is used.

    Sentinel logic (--format default=None):
    - Neither flag given          → "raw"
    - Only --json given           → "json"  (+ deprecation hint)
    - Only --format given         → format_arg value
    - Both given, format == json  → "json"  (+ deprecation hint; not a conflict)
    - Both given, format != json  → conflict → return None
    """
    json_flag = args.json_flag
    fmt = args.format  # None = not explicitly set by user

    if not json_flag and fmt is None:
        # Neither flag: default to raw
        return "raw"

    if not json_flag:
        # Only --format given
        return fmt

    # --json was given
    if fmt is None or fmt == "json":
        # --json alone, or --json --format json (same value, not a conflict)
        print(
            "[hint] --json is an alias for --format json; "
            "consider migrating callers to --format json",
            file=sys.stderr,
        )
        return "json"

    # --json + --format <something other than json> → conflict
    print(
        "error: conflicting flags: --json implies --format json "
        f"but --format {fmt} was also given",
        file=sys.stderr,
    )
    return None


def _print_message_raw(message: dict[str, object]) -> None:
    """Print a message in the default human-readable (raw) format."""
    print(f"ID: {message['id']}")
    print(f"Subject: {message['subject']}")
    print(f"Sender: {message['sender_name']} <{message['sender_email']}>")
    print(f"Sent: {message['sent_at']}")
    print(f"Received: {message['received_at']}")
    print(f"PST: {message['pst_source']}")
    print(f"Folder: {message['folder_path']}")
    print(f"Locator: {message['logical_locator']}")
    print(f"Input mode: {message['source_input_mode']}")
    print(f"Requested from: {message['source_requested_from']}")
    attachment_names = message["attachment_names"]
    if not isinstance(attachment_names, list):
        attachment_names = []
    print(f"Attachments: {', '.join(str(name) for name in attachment_names)}")
    print("Body:")
    print(message["body_text"])


def _print_message_text(message: dict[str, object], *, new_only: bool = False) -> None:
    """Print a message with HTML-cleaned body (text format).

    When *new_only* is ``True``, the rendered body is passed through
    :func:`strip_quote_tail` before printing, keeping only the new
    content before the first detected quote boundary.
    """
    print(f"From: {message['sender_name']} <{message['sender_email']}>")
    print(f"Sent: {message['sent_at']}")
    print(f"PST: {message['pst_source']}")
    print(f"Folder: {message['folder_path']}")
    for field, label in (("to", "To"), ("cc", "Cc")):
        recipients = message.get(field, [])
        if isinstance(recipients, list) and recipients:
            formatted = ", ".join(
                f"{r.get('name', '')} <{r.get('email', '')}>" for r in recipients
            )
            print(f"{label}: {formatted}")
    print("-" * 40)
    body = message.get("body_text")
    rendered = render_text(body if isinstance(body, str) else None)
    if new_only:
        rendered = strip_quote_tail(rendered)
    print(rendered)


def _print_attachment_rows_table(rows: list[dict[str, object]]) -> None:
    if not rows:
        print("No attachments found.")
        return

    headers = [
        "att_id",
        "sent_at",
        "exportable",
        "attachment_name",
        "sender_email",
        "subject",
        "rfc_message_id",
        "dedup_key",
    ]
    table_rows = [
        [
            str(row["attachment_id"]),
            str(row["sent_at"] or "unknown-date"),
            "yes" if row["exportable"] else "no",
            str(row["attachment_name"] or ""),
            str(row["sender_email"] or ""),
            str(row["subject"] or ""),
            str(row["rfc_message_id"] or ""),
            str(row["dedup_key"] or ""),
        ]
        for row in rows
    ]
    widths = [
        max(len(header), *(len(data_row[index]) for data_row in table_rows))
        for index, header in enumerate(headers)
    ]
    print(
        " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    )
    print("-+-".join("-" * width for width in widths))
    for data_row in table_rows:
        print(
            " | ".join(
                value.ljust(widths[index]) for index, value in enumerate(data_row)
            )
        )


def _print_attachment_export_summary(result: AttachmentExportResult) -> None:
    print(f"copied: {result.copied}")
    print(f"skipped-non-exportable: {result.skipped_non_exportable}")
    print(f"skipped-existing: {result.skipped_existing}")
    print(f"failed: {result.failed}")


def _handle_show(args: argparse.Namespace) -> int:
    headers_flag = args.headers_flag
    json_flag = args.json_flag
    fmt_arg = args.format

    # --headers is a mutually exclusive independent output mode.
    # It cannot be combined with --format (any value), --json, or --new-only.
    if headers_flag and (json_flag or fmt_arg is not None or args.new_only):
        conflicts = []
        if json_flag:
            conflicts.append("--json")
        if fmt_arg is not None:
            conflicts.append(f"--format {fmt_arg}")
        if args.new_only:
            conflicts.append("--new-only")
        print(
            f"error: --headers cannot be combined with {' and '.join(conflicts)}; "
            "they are mutually exclusive output modes",
            file=sys.stderr,
        )
        return 2

    if headers_flag:
        try:
            message = get_message(args.database, args.message_id)
        except (FileNotFoundError, KeyError, sqlite3.Error) as exc:
            print(_format_exception_message(exc), file=sys.stderr)
            return 2
        raw_headers = message.get("raw_headers")
        if raw_headers is None:
            print("No raw header data available for this message.")
        else:
            sys.stdout.write(raw_headers)
        return 0

    fmt = _resolve_show_format(args)
    if fmt is None:
        return 2

    # --new-only is only valid with --format text
    if args.new_only and fmt != "text":
        print(
            "error: --new-only is only valid with --format text",
            file=sys.stderr,
        )
        return 2

    try:
        message = get_message(args.database, args.message_id)
    except (FileNotFoundError, KeyError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2

    if fmt == "json":
        print(json.dumps(message, ensure_ascii=False, indent=2))
    elif fmt == "text":
        _print_message_text(message, new_only=args.new_only)
    else:
        _print_message_raw(message)
    return 0


def _handle_thread(args: argparse.Namespace) -> int:
    # design.md > "Map the exception at each entry to that entry's native
    # error idiom" (CLI bullet): the raw stripped identifier string is
    # passed straight to walk_thread -- the resolver applies the identical
    # shape test and conversion, so this is behavior-preserving for every
    # in-range identifier. The handler no longer pre-classifies (that used
    # to call int() on the raw string outside any try block, crashing on an
    # over-length digit string before the resolver's gate could run).
    identifier_str = args.identifier.strip()

    try:
        messages = walk_thread(args.database, identifier_str)
    except (ThreadIdentifierError, FileNotFoundError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2
    except KeyError as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 1

    fmt = args.format or "raw"

    # --new-only is only valid with --format text
    if args.new_only and fmt != "text":
        print(
            "error: --new-only is only valid with --format text",
            file=sys.stderr,
        )
        return 2

    total = len(messages)
    new_only = args.new_only

    if fmt == "json":
        print(json.dumps(messages, ensure_ascii=False, indent=2))
    else:
        for n, message in enumerate(messages, start=1):
            msg_id = message.get("message_id") or ""
            print(f"--- ({n} of {total}, msg-id={msg_id}) ---")
            if fmt == "text":
                _print_message_text(message, new_only=new_only)
            else:
                _print_message_raw(message)
            print()
    return 0


def _handle_contact_history(args: argparse.Namespace) -> int:
    try:
        # Pre-validate before aggregate_contact_history opens the multi-GB
        # database: the CLI is the layer that knows its own flag spelling
        # (--date-from / --date-to); aggregate_contact_history's own
        # internal re-parse names its own keyword arguments instead.
        _parse_contact_history_date_bound(args.date_from, parameter_name="--date-from")
        _parse_contact_history_date_bound(args.date_to, parameter_name="--date-to")
        summary = aggregate_contact_history(
            database_path=args.database,
            contact=args.contact,
            domain=args.domain,
            date_from=args.date_from,
            date_to=args.date_to,
            attachments_limit=args.attachments_limit,
        )
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2
    print(serialise_summary(summary, fmt=args.format))
    return 0


def _parse_optional_datetime(value: str | None, *, end_of_day: bool) -> datetime | None:
    """Parse a `search` / `attachments` date-or-timestamp bound argument.

    Branches on whether *value* carries a time component, mechanically
    rather than via a list of blessed spellings (design.md > "Keep every
    input form every surface already accepts, and define what each one
    means"): a date-only value -- any form ``date.fromisoformat`` accepts
    (canonical, basic, or ISO week date) -- becomes the whole Asia/Taipei
    calendar day via :func:`whole_day_bound`; a full ISO-8601 timestamp is
    passed through :func:`coerce_to_taipei_aware`, so an offset-aware
    instant is used as given and a naive one is interpreted as Asia/Taipei.

    ``None``, empty, or whitespace-only *value* denotes an unsupplied
    filter and returns ``None`` -- not an error (design.md > "Reject a
    malformed date instead of letting it match nothing, and treat a blank
    value as an absent filter").

    Raises ``ValueError`` naming both *value* and the parameter name --
    derived from *end_of_day* as ``--to-date`` / ``--from-date``, the
    fixed pair every call site here always passes -- when *value* is
    neither a date this surface accepts nor a valid ISO-8601 timestamp.
    """
    checked = normalize_optional_date_text(value)
    if checked is None:
        return None
    parameter_name = "--to-date" if end_of_day else "--from-date"
    with contextlib.suppress(ValueError):
        # whole_day_bound receives an already-valid date and cannot raise
        # ValueError, so this early return is safe inside the suppression.
        # date.fromisoformat does not tolerate surrounding whitespace, so a
        # padded value is rejected here exactly as it was before this
        # helper existed.
        parsed_date = date.fromisoformat(checked)
        return whole_day_bound(parsed_date, end_of_day=end_of_day)
    try:
        # datetime.fromisoformat likewise rejects surrounding whitespace.
        parsed_datetime = datetime.fromisoformat(checked)
    except ValueError as exc:
        raise ValueError(
            format_invalid_date_message(
                value,
                parameter_name,
                expected=(
                    "a YYYY-MM-DD date (canonical, basic, or ISO week form) "
                    "or an ISO-8601 timestamp"
                ),
            )
        ) from exc
    return coerce_to_taipei_aware(parsed_datetime)


def _non_negative_int(value: str) -> int:
    try:
        ivalue = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got '{value}'") from exc
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"must be non-negative, got {ivalue}")
    return ivalue

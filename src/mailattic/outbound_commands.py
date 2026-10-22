"""The ``bes`` outbound command family: draft-reply / send-draft / send-batch /
list-drafts.

Owns the argparse declaration, the SMTP settings-file handling and its
missing-file diagnostic (D-20260915-1), the review printing of a draft, the
``<draft_id>=<fingerprint>`` pair syntax and the exit-code mapping. ``cli.py``
only calls ``register_outbound_commands``; the send logic and its authorization
gate (behaviour contract #2) stay in ``outbound.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .imap_runtime import load_env_file
from .outbound import (
    DEFAULT_SMTP_ENV_FILE,
    ENV_VAR_SMTP_ENV_FILE,
    DraftReplyError,
    SendRefused,
    SmtpConfig,
    _sanitize_for_terminal,
    assemble_draft_reply,
    list_drafts,
    load_smtp_config,
    own_address_without_smtp_settings,
    reply_font_style,
    resolve_smtp_env_file,
    send_batch,
    send_draft,
)
from .query_commands import add_database_argument

_SMTP_ENV_FILE_KEYS = "SMTP_HOST, SMTP_PORT, SMTP_USER, and SMTP_PASSWORD"
_SMTP_ENV_FILE_HELP = (
    "Path to an SMTP settings .env file containing "
    f"{_SMTP_ENV_FILE_KEYS}. Defaults to the "
    f"{ENV_VAR_SMTP_ENV_FILE} environment variable, then the built-in path "
    f"{DEFAULT_SMTP_ENV_FILE}."
)


def _missing_smtp_env_file_error(resolved_path: Path) -> str:
    # #199: names every resolution layer so the caller knows which one to fix.
    return (
        f"SMTP settings file not found: {resolved_path}. Provide one of: "
        f"--env-file <path>, the {ENV_VAR_SMTP_ENV_FILE} environment variable, "
        f"or the built-in default at {DEFAULT_SMTP_ENV_FILE}. The file must "
        f"contain {_SMTP_ENV_FILE_KEYS}."
    )


def register_outbound_commands(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register draft-reply, send-draft, send-batch and list-drafts."""
    draft_reply_parser = subparsers.add_parser(
        "draft-reply",
        help="Generate an RFC5322 reply-all draft for a stored message.",
    )
    draft_reply_parser.add_argument(
        "message_id",
        type=str,
        help="RFC822 Message-ID string for the original message.",
    )
    draft_reply_parser.add_argument(
        "--body-from",
        required=True,
        type=Path,
        help="Path to a UTF-8 text file containing the reply body.",
    )
    draft_reply_parser.add_argument(
        "--attach",
        action="append",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to a local file attachment. Repeat to attach multiple files.",
    )
    for flag, bucket in (("--to", "To"), ("--cc", "Cc"), ("--bcc", "Bcc")):
        draft_reply_parser.add_argument(
            flag,
            action="append",
            default=None,
            metavar="ADDR",
            help=(
                f"Add an explicit {bucket} recipient. Repeat to add multiple "
                "recipients."
            ),
        )
    draft_reply_parser.add_argument(
        "--replace-recipients",
        action="store_true",
        help=(
            "Ignore reply-all recipient derivation and use explicit --to/--cc/--bcc "
            "values. Requires at least one --to."
        ),
    )
    draft_reply_parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="ADDR",
        help=(
            "Remove an address from reply-all-derived recipients. Repeat to exclude "
            "multiple addresses."
        ),
    )
    draft_reply_parser.add_argument(
        "--subject",
        type=str,
        default=None,
        metavar="TEXT",
        help=(
            'Use TEXT as the reply subject instead of "Re: <original subject>". '
            "Used as given (no Re: added); In-Reply-To/References still follow "
            "the anchor message."
        ),
    )
    draft_reply_parser.add_argument(
        "--allow-archive-anchor",
        action="store_true",
        help=(
            "Allow an archive-scraped outbound message as the thread anchor after "
            "manually verifying its recipients."
        ),
    )
    add_database_argument(draft_reply_parser)
    draft_reply_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=_SMTP_ENV_FILE_HELP,
    )
    draft_reply_parser.set_defaults(handler=_handle_draft_reply)

    send_draft_parser = subparsers.add_parser(
        "send-draft",
        help=(
            "Send a saved draft: with --fingerprint after the user approved it, "
            "or after typed confirmation in a real terminal."
        ),
    )
    send_draft_parser.add_argument(
        "draft_id", type=str, help="Draft identifier (UUID)."
    )
    send_draft_parser.add_argument(
        "--fingerprint",
        default=None,
        metavar="SHA256",
        help=(
            "The fingerprint draft-reply printed for this draft. Pass it only after "
            "the user reviewed the draft and said to send; the send is refused if "
            "the draft changed since. Without it, a real terminal is required."
        ),
    )
    add_database_argument(send_draft_parser)
    send_draft_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=_SMTP_ENV_FILE_HELP,
    )
    send_draft_parser.set_defaults(handler=_handle_send_draft)

    send_batch_parser = subparsers.add_parser(
        "send-batch",
        help="Send multiple saved drafts under a single authorization.",
    )
    send_batch_parser.add_argument(
        "draft_id",
        type=str,
        nargs="+",
        metavar="DRAFT_ID[=SHA256]",
        help=(
            "Draft identifier (UUID). Repeat to send multiple drafts as one batch. "
            "Give every draft as <draft_id>=<fingerprint> (from draft-reply) after "
            "the user approved the batch, or none of them to confirm in a real "
            "terminal. A draft whose fingerprint no longer matches is not sent."
        ),
    )
    add_database_argument(send_batch_parser)
    send_batch_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=_SMTP_ENV_FILE_HELP,
    )
    send_batch_parser.set_defaults(handler=_handle_send_batch)

    list_drafts_parser = subparsers.add_parser(
        "list-drafts",
        help="List pending (unsent) draft emails.",
    )
    list_drafts_parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path (used to locate the outbound/drafts/ directory).",
    )
    list_drafts_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the draft list as JSON. Drafts stuck at 'sending' carry "
        "status, marker_path and next_step.",
    )
    list_drafts_parser.set_defaults(handler=_handle_list_drafts)


def _handle_draft_reply(args: argparse.Namespace) -> int:
    resolved_env_file = resolve_smtp_env_file(args.env_file)
    env: dict[str, str] = {}
    try:
        env = load_env_file(resolved_env_file)
        own_address = load_smtp_config(env).user
    except FileNotFoundError:
        # #199: a missing file must not fall back to a sender the user never chose.
        print(_missing_smtp_env_file_error(resolved_env_file), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        # Unreadable or incomplete settings; anything else is a bug and
        # must not be relabelled as "set SMTP_USER". Fonts, like the MCP
        # path, apply only from settings that load (#13).
        env = {}
        try:
            own_address = own_address_without_smtp_settings(args.database)
        except DraftReplyError as refusal:
            print(refusal, file=sys.stderr)
            return 2

    def warn_archive_anchor() -> None:
        print(
            "WARNING: allowing an archive-scraped reply anchor whose recipients may "
            "be incomplete; --allow-archive-anchor asserts that the caller manually "
            "verified the final envelope.",
            file=sys.stderr,
        )

    try:
        result = assemble_draft_reply(
            database_path=args.database,
            message_id=args.message_id,
            own_address=own_address,
            body_from=args.body_from,
            to=args.to,
            cc=args.cc,
            bcc=args.bcc,
            exclude=args.exclude,
            replace_recipients=args.replace_recipients,
            attachments=args.attach,
            allow_archive_anchor=args.allow_archive_anchor,
            on_archive_anchor=warn_archive_anchor,
            subject=args.subject,
            font_style=reply_font_style(env),
        )
    except DraftReplyError as exc:
        print(exc, file=sys.stderr)
        return 2

    # This stdout is what the user reviews before a fingerprint send, so
    # data-influenced text must not carry terminal control sequences.
    safe = _sanitize_for_terminal
    print(f"draft_id: {result.draft_id}")
    print(f"From: {safe(result.from_address)}")
    print(f"Subject: {safe(result.subject)}")
    print(f"To: {safe(result.to)}")
    print(f"Cc: {safe(result.cc)}")
    print(f"Bcc: {safe(result.bcc)}")
    print(f"Message-ID: {result.message_id}")
    if result.attachments:
        print(f"Attachments: {len(result.attachments)}")
        for name, size in result.attachments:
            print(f"  - {safe(name)} ({size} bytes)")
    else:
        print("Attachments: none")
    print(f"fingerprint: {result.fingerprint}")
    return 0


def _handle_list_drafts(args: argparse.Namespace) -> int:
    drafts = list_drafts(args.database)
    if args.json:
        print(json.dumps(drafts, ensure_ascii=False, indent=2))
        return 0
    if not drafts:
        print("No pending drafts.")
        return 0
    for draft in drafts:
        line = (
            f"draft_id: {draft['draft_id']}  "
            f"subject: {draft['subject']}  "
            f"to: {draft['recipients']}  "
            f"created: {draft['created_at']}"
        )
        if "status" in draft:
            line += f"  status: {draft['status']}"
        print(line)
        if "next_step" in draft:
            print(
                "  A send was started and has not completed (it may still be "
                "running, or it failed); this mail may or may not have been "
                f"sent. {draft['next_step']}"
            )
    return 0


def _load_smtp_env(
    env_file: Path | None,
) -> tuple[SmtpConfig, dict[str, str]] | int:
    """Resolve and parse the SMTP env file once for SMTP config and the APPEND.

    ``send-draft`` / ``send-batch`` need the same env file for two things:
    the SMTP submission config, and the best-effort IMAP APPEND-to-Sent
    (design Decision 3, add-mutf7-folder-codec). Parsing it once and reusing
    the mapping for both avoids a redundant second file read. On failure the
    error is printed and the exit code (2) is returned instead.
    """
    resolved_env_file = resolve_smtp_env_file(env_file)
    try:
        env = load_env_file(resolved_env_file)
        smtp_config = load_smtp_config(env)
    except FileNotFoundError:
        print(_missing_smtp_env_file_error(resolved_env_file), file=sys.stderr)
        return 2
    except Exception:
        print("Could not load SMTP config; check --env-file", file=sys.stderr)
        return 2
    return smtp_config, env


def _format_partial_refusal(result: Mapping[str, Any]) -> str:
    refused = "; ".join(
        f"{item['address']} ({item['code']} {item['reason']})"
        for item in result["refused"]
    )
    return f"{result['warning']}\n  Refused: {refused}"


def _handle_send_draft(args: argparse.Namespace) -> int:
    loaded = _load_smtp_env(args.env_file)
    if isinstance(loaded, int):
        return loaded
    smtp_config, env = loaded

    try:
        result = send_draft(
            draft_id=args.draft_id,
            database_path=args.database,
            smtp_config=smtp_config,
            env=env,
            fingerprint=args.fingerprint,
        )
        if result.get("refused"):
            print(f"Partially sent: message_id={result['message_id']}")
            print(_format_partial_refusal(result), file=sys.stderr)
            return 3
        print(f"Sent: message_id={result['message_id']}")
        return 0
    except SendRefused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"Draft not found: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _handle_send_batch(args: argparse.Namespace) -> int:
    loaded = _load_smtp_env(args.env_file)
    if isinstance(loaded, int):
        return loaded
    smtp_config, env = loaded

    pairs = [item.partition("=") for item in args.draft_id]
    paired = [sep == "=" for _draft_id, sep, _sha in pairs]
    if any(paired) and not all(paired):
        print(
            "send-batch: give every draft as <draft_id>=<fingerprint>, or none of "
            "them (then a terminal confirmation is required)",
            file=sys.stderr,
        )
        return 2

    try:
        results = send_batch(
            draft_ids=[draft_id for draft_id, _sep, _sha in pairs],
            database_path=args.database,
            smtp_config=smtp_config,
            env=env,
            fingerprints=(
                {draft_id: sha for draft_id, _sep, sha in pairs}
                if all(paired)
                else None
            ),
        )
    except SendRefused as exc:
        # Whole-batch abort (duplicate ids / non-TTY / empty token): only the
        # refusal is printed -- there is no per-draft outcome to report.
        print(str(exc), file=sys.stderr)
        return 1

    any_failed = False
    for result in results:
        draft_id = result["draft_id"]
        outcome = result["outcome"]
        message_id_suffix = (
            f" message_id={message_id}"
            if (message_id := result.get("message_id"))
            else ""
        )
        if outcome == "sent":
            print(f"Sent: draft_id={draft_id} message_id={result.get('message_id')}")
        elif outcome == "partial":
            any_failed = True
            print(
                f"Partial: draft_id={draft_id}{message_id_suffix}\n"
                f"{_format_partial_refusal(result)}",
                file=sys.stderr,
            )
        elif outcome == "skipped":
            print(
                f"Skipped: draft_id={draft_id} ({result.get('reason', '')})"
                f"{message_id_suffix}"
            )
        else:
            any_failed = True
            print(
                f"Failed: draft_id={draft_id} ({result.get('reason', '')})"
                f"{message_id_suffix}",
                file=sys.stderr,
            )
    return 1 if any_failed else 0

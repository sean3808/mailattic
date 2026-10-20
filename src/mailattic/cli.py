from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path

from .archive_scrape import ScrapeSummary, run_scrape
from .attachment_store import (
    CasVerifyResult,
    prune_cas,
    resolve_store_root,
    verify_cas,
    verify_cas_references,
)
from .db_maintenance import backup_database, check_database, reindex_fts
from .identity import (
    add_self_address,
    list_self_addresses,
    remove_self_address,
    resolve_self_addresses,
)
from .imap_sync import (
    DEFAULT_ENV_FILE,
    DEFAULT_LOG_DIR,
    DEFAULT_MAILDIR_ROOT,
    estimate_sync_run,
    pre_task_sync_imap,
    run_sync_imap,
)
from .ingest import (
    import_maildir_sources,
    import_pst_sources,
    resolve_ingestion_sources,
    resolve_maildir_sources,
)
from .models import MaildirSource
from .outbound_commands import register_outbound_commands
from .parser import MAILDIR_BUCKETS, iter_maildir_message_paths
from .quarantine import (
    AbandonError,
    abandon_attachment_failure,
    count_quarantined_mail,
    default_quarantine_root,
    find_unresolved_attachment_failure,
    quarantined_files_freed_by_abandon,
    release_quarantined_locator,
    unresolved_attachment_failures,
)
from .query_commands import (
    add_database_argument,
    register_contact_history_command,
    register_message_commands,
)
from .rebuild import PHASE_SEQUENCE, run_rebuild
from .storage import (
    connect_database,
    initialize_database,
)
from .storage import (
    resolve_database_default as _resolve_database_default,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailattic",
        description="Local PST email evidence ingestion and query tool.",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser(
        "init-db",
        help="Create the SQLite schema used by the evidence database.",
    )
    init_parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path to create or upgrade.",
    )
    init_parser.set_defaults(handler=_handle_init_db)

    import_parser = subparsers.add_parser(
        "import",
        help="Import Outlook PST stores or a local maildir into the database.",
        description=(
            "Import PST files through the recipient table of an already-running "
            "Windows Outlook profile (each selected PST must already be mounted), "
            "or import a local maildir tree with --maildir, which needs neither "
            "Outlook nor readpst."
        ),
    )
    add_database_argument(import_parser)
    import_parser.add_argument(
        "--directory",
        dest="directories",
        action="append",
        default=[],
        type=Path,
        help="Directory containing PST files. Can be repeated.",
    )
    import_parser.add_argument(
        "--file",
        dest="files",
        action="append",
        default=[],
        type=Path,
        help="Explicit PST file path. Can be repeated.",
    )
    import_parser.add_argument(
        "--maildir",
        type=Path,
        default=None,
        help=(
            "Maildir account root whose subdirectories are folders holding "
            "cur/new/tmp. Mutually exclusive with --directory/--file."
        ),
    )
    import_parser.add_argument(
        "--account-email",
        dest="account_email",
        default=None,
        help="Account address recorded as provenance. Required with --maildir.",
    )
    import_parser.add_argument(
        "--attachment-root",
        type=Path,
        default=None,
        dest="attachment_root",
        help="Directory for attachment binary storage. Default: <db_dir>/attachments/.",
    )
    import_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the import summary as JSON.",
    )
    import_parser.set_defaults(handler=_handle_import)

    register_message_commands(subparsers)

    sync_parser = subparsers.add_parser(
        "sync-imap",
        help="Run IMAP synchronization.",
    )
    add_database_argument(sync_parser)
    sync_parser.add_argument(
        "--folder",
        default=None,
        help="Limit the sync to one IMAP folder.",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate how many IMAP messages would be fetched without side effects.",
    )
    sync_parser.add_argument(
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
    sync_parser.add_argument(
        "--full-resync",
        action="store_true",
        help=(
            "Delete local watermark state after a confirmation prompt, "
            "then run a full resync."
        ),
    )
    sync_parser.add_argument(
        "--pre-task",
        action="store_true",
        dest="pre_task",
        help=(
            "Pre-task mode: skip if last completed sync was within 15 minutes "
            "(the skip line says so when that sync failed or did not complete); "
            "otherwise run the normal sync. "
            "Intended for caller agents that want fresh mailbox state "
            "without redundant sync invocations."
        ),
    )
    sync_parser.add_argument(
        "--json",
        action="store_true",
        help="With --pre-task: emit the outcome as JSON (rejected without it).",
    )
    sync_parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=argparse.SUPPRESS,
    )
    sync_parser.add_argument(
        "--maildir-root",
        type=Path,
        default=DEFAULT_MAILDIR_ROOT,
        help=argparse.SUPPRESS,
    )
    sync_parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help=argparse.SUPPRESS,
    )
    sync_parser.set_defaults(handler=_handle_sync_imap)

    register_outbound_commands(subparsers)

    scrape_parser = subparsers.add_parser(
        "scrape-archive",
        help="Scrape outbound mail archive from the mail server admin UI.",
    )
    add_database_argument(scrape_parser)
    scrape_parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scrape pass and exit (no scheduling).",
    )
    scrape_parser.add_argument(
        "--from",
        dest="from_date",
        default=None,
        help="Inclusive lower bound date YYYY-MM-DD. Defaults to today.",
    )
    scrape_parser.add_argument(
        "--to",
        dest="to_date",
        default=None,
        help="Inclusive upper bound date YYYY-MM-DD. Defaults to today.",
    )
    scrape_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enumerate matching entries without writing to the database.",
    )
    scrape_parser.set_defaults(handler=_handle_scrape_archive)

    register_contact_history_command(subparsers)

    rebuild_parser = subparsers.add_parser(
        "rebuild",
        help="Rebuild a staging database from PST archives and IMAP backfill.",
    )
    rebuild_parser.add_argument(
        "phase",
        nargs="?",
        choices=list(PHASE_SEQUENCE),
        help=(
            "Single rebuild phase to run: "
            "preflight, coverage-report, import, backfill, verify-cas, or validate."
        ),
    )
    rebuild_parser.add_argument(
        "--all",
        action="store_true",
        dest="run_all",
        help="Run all rebuild phases in order (default when no phase is specified).",
    )
    rebuild_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the first incomplete phase recorded in the checkpoint.",
    )
    rebuild_parser.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help=(
            "Writable rebuild work directory on a local Windows disk. Rebuild "
            "reads PSTs through a running Outlook, so it runs on Windows."
        ),
    )
    rebuild_parser.add_argument(
        "--pst",
        action="append",
        default=[],
        type=Path,
        help="Explicit PST file path to import. Repeat for multiple PST files.",
    )
    rebuild_parser.set_defaults(handler=_handle_rebuild)

    # bes identity — operator-declared self addresses (issue #168)
    identity_parser = subparsers.add_parser(
        "identity",
        help=(
            "Declare which email addresses are you (add, remove, list). "
            "An empty set means undeclared: no message counts as sent by you."
        ),
    )
    identity_subparsers = identity_parser.add_subparsers(dest="identity_command")

    identity_add_parser = identity_subparsers.add_parser(
        "add",
        help="Declare an email address as self (idempotent).",
    )
    identity_add_parser.add_argument("address", help="Email address to declare.")
    add_database_argument(identity_add_parser)
    identity_add_parser.set_defaults(handler=_handle_identity_add)

    identity_remove_parser = identity_subparsers.add_parser(
        "remove",
        help="Undeclare an email address.",
    )
    identity_remove_parser.add_argument("address", help="Email address to undeclare.")
    add_database_argument(identity_remove_parser)
    identity_remove_parser.set_defaults(handler=_handle_identity_remove)

    identity_list_parser = identity_subparsers.add_parser(
        "list",
        help=(
            "Print the effective self-address set as JSON, with a source of "
            "`database` or `unset` (nothing declared)."
        ),
    )
    add_database_argument(identity_list_parser)
    identity_list_parser.set_defaults(handler=_handle_identity_list)

    identity_parser.set_defaults(
        handler=_handle_identity_no_subcommand, _identity_parser=identity_parser
    )

    # bes db — database maintenance subcommand group
    db_parser = subparsers.add_parser(
        "db",
        help=(
            "Database maintenance commands "
            "(backup, doctor, reindex-fts, verify-cas, prune-cas, "
            "abandon-attachment-failure)."
        ),
    )
    db_subparsers = db_parser.add_subparsers(dest="db_command")

    db_backup_parser = db_subparsers.add_parser(
        "backup",
        help=(
            "Produce a consistent WAL snapshot of the database via "
            "sqlite3.Connection.backup() (never copies .db directly)."
        ),
    )
    add_database_argument(db_backup_parser)
    db_backup_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        dest="output",
        help=(
            "Explicit output path for the backup file.  "
            "Default: <db_dir>/backups/<YYYYMMDD-HHMMSS>.db"
        ),
    )
    db_backup_parser.set_defaults(handler=_handle_db_backup)

    db_doctor_parser = db_subparsers.add_parser(
        "doctor",
        help=(
            "Run PRAGMA integrity_check / quick_check / journal_mode / "
            "foreign_key_check and report results."
        ),
    )
    add_database_argument(db_doctor_parser)
    db_doctor_parser.add_argument(
        "--quarantine-root",
        type=Path,
        default=None,
        dest="quarantine_root",
        help=(
            "Quarantine directory for raw mail with an unresolved attachment "
            "failure. Default: .mailattic-work/quarantine/<database file name> "
            "beside the database."
        ),
    )
    db_doctor_parser.set_defaults(handler=_handle_db_doctor)

    db_reindex_fts_parser = db_subparsers.add_parser(
        "reindex-fts",
        help=(
            "Rebuild the entire message_fts index from messages with CJK segmentation, "
            "in a single rollback-safe transaction."
        ),
    )
    add_database_argument(db_reindex_fts_parser)
    db_reindex_fts_parser.set_defaults(handler=_handle_db_reindex_fts)

    db_verify_cas_parser = db_subparsers.add_parser(
        "verify-cas",
        help=(
            "Verify content-addressable attachment store integrity. "
            "Read-only: reports mismatched and unreadable files."
        ),
    )
    db_verify_cas_parser.add_argument(
        "--attachment-root",
        type=Path,
        default=None,
        dest="attachment_root",
        help=(
            "Root directory of the CAS attachment store. "
            "Default: <db_dir>/attachments/."
        ),
    )
    db_verify_cas_parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path (used to resolve default attachment root).",
    )
    db_verify_cas_parser.set_defaults(handler=_handle_db_verify_cas)

    db_prune_cas_parser = db_subparsers.add_parser(
        "prune-cas",
        help=(
            "Delete corrupted files from the content-addressable attachment store. "
            "Destructive: requires explicit confirmation."
        ),
    )
    db_prune_cas_parser.add_argument(
        "--attachment-root",
        type=Path,
        default=None,
        dest="attachment_root",
        help=(
            "Root directory of the CAS attachment store. "
            "Default: <db_dir>/attachments/."
        ),
    )
    db_prune_cas_parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path (used to resolve default attachment root).",
    )
    db_prune_cas_parser.add_argument(
        "--yes",
        action="store_true",
        dest="yes",
        help="Skip interactive confirmation (non-interactive explicit confirmation).",
    )
    db_prune_cas_parser.set_defaults(handler=_handle_db_prune_cas)

    db_abandon_parser = db_subparsers.add_parser(
        "abandon-attachment-failure",
        help=(
            "Give up on one unresolved attachment failure (human decision; the "
            "system never abandons on its own). Destructive: requires explicit "
            "confirmation."
        ),
    )
    db_abandon_parser.add_argument(
        "--id",
        type=int,
        required=True,
        dest="failure_id",
        help="ingest_failures id, as listed by `bes db doctor`.",
    )
    db_abandon_parser.add_argument(
        "--reason", required=True, help="Why it is given up (recorded)."
    )
    add_database_argument(db_abandon_parser)
    db_abandon_parser.add_argument(
        "--quarantine-root",
        type=Path,
        default=None,
        dest="quarantine_root",
        help=(
            "Quarantine directory. Default: .mailattic-work/quarantine/"
            "<database file name> beside the database."
        ),
    )
    db_abandon_parser.add_argument(
        "--yes",
        action="store_true",
        dest="yes",
        help="Skip interactive confirmation (non-interactive explicit confirmation).",
    )
    db_abandon_parser.set_defaults(handler=_handle_db_abandon_attachment_failure)

    db_parser.set_defaults(handler=_handle_db_no_subcommand, _db_parser=db_parser)

    return parser


def _handle_db_backup(args: argparse.Namespace) -> int:
    db = args.database  # already resolved by main() three-layer fallback
    try:
        out = backup_database(db, args.output)
    except (
        FileNotFoundError,
        FileExistsError,
        ValueError,
        OSError,
        sqlite3.Error,
    ) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2
    print(out)
    print(
        "This is a staging snapshot — copy it to external storage "
        "for disaster recovery."
    )
    return 0


def _handle_db_doctor(args: argparse.Namespace) -> int:
    db = args.database  # already resolved by main() three-layer fallback
    try:
        result = check_database(db)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2
    print(f"db_path:            {result['db_path']}")
    print(f"journal_mode:       {result['journal_mode']}")
    integrity = result["integrity_check"]
    print(f"integrity_check:    {integrity}")
    quick = result["quick_check"]
    print(f"quick_check:        {quick}")
    fk = result["foreign_key_check"]
    print(f"foreign_key_check:  {fk if fk else '(none — OK)'}")
    passed = result["passed"]
    print(f"passed:             {passed}")
    # #256: attachment failures that nobody has resolved or abandoned yet.
    # Same convention as the checks above: a finding -> exit 1 (2 = could not run).
    quarantine_root = args.quarantine_root or default_quarantine_root(db)
    try:
        connection = connect_database(db, create=False, read_only=True)
        try:
            unresolved = unresolved_attachment_failures(connection)
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2
    print(f"unresolved_attachment_failures: {len(unresolved)}")
    for row in unresolved:
        size = row["attachment_size"]
        print(
            f"  id={row['id']} {row['logical_locator'] or '(no locator)'} "
            f"attachment={row['attachment_name'] or '(unknown)'} "
            f"size={'(unknown)' if size is None else size}"
        )
    print(f"quarantined_mail:   {count_quarantined_mail(quarantine_root)}")
    return 0 if passed and not unresolved else 1


def _handle_db_abandon_attachment_failure(args: argparse.Namespace) -> int:
    db = args.database  # already resolved by main() three-layer fallback
    quarantine_root = args.quarantine_root or default_quarantine_root(db)
    if not args.reason.strip():
        print(
            "abandon-attachment-failure: --reason must not be empty.", file=sys.stderr
        )
        return 2
    connection = None
    try:
        # Read-only until confirmed: a refused abandon leaves the file untouched,
        # and a pre-#255/#256 database is read without being migrated.
        connection = connect_database(db, create=False, read_only=True)
        row = find_unresolved_attachment_failure(connection, args.failure_id)
        if row is None:
            print(
                f"abandon-attachment-failure: no unresolved attachment failure "
                f"with id {args.failure_id}.",
                file=sys.stderr,
            )
            return 2
        locator = row["logical_locator"]
        print(
            f"Abandon failure id={row['id']} {locator or '(no locator)'}"
            f" attachment={row['attachment_name'] or '(unknown)'}"
        )
        if locator:
            for path in quarantined_files_freed_by_abandon(
                connection,
                quarantine_root,
                locator=locator,
                ignore_failure_id=row["id"],
            ):
                print(f"Quarantined raw mail that will be removed: {path}")
        connection.close()
        connection = None
        confirmed = False
        if args.yes:
            confirmed = True
        elif sys.stdin.isatty() and sys.stdout.isatty():
            try:
                answer = input("Type 'yes' to abandon it for good: ").strip()
            except EOFError:
                answer = ""
            confirmed = answer == "yes"
        if not confirmed:
            print(
                "abandon-attachment-failure: confirmation required. "
                "Use --yes flag or run interactively and type 'yes'.",
                file=sys.stderr,
            )
            return 1
        initialize_database(db)  # idempotent: adds the missing ledger columns
        connection = connect_database(db, create=False)
        closed = abandon_attachment_failure(connection, args.failure_id, args.reason)
        removed = 0
        if locator:
            try:
                removed = release_quarantined_locator(
                    connection, quarantine_root, locator
                )
            except (OSError, sqlite3.Error) as exc:
                print(f"quarantine cleanup failed: {exc}", file=sys.stderr)
        print(f"abandoned: id={closed['id']}")
        print(f"quarantined_files_removed: {removed}")
        return 0
    except (FileNotFoundError, AbandonError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()


def _handle_db_reindex_fts(args: argparse.Namespace) -> int:
    db = args.database  # already resolved by main() three-layer fallback
    try:
        result = reindex_fts(db)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2
    print(f"messages: {result.messages_count}")
    print(f"message_fts: {result.fts_count}")
    print("reindex-fts: completed successfully.")
    return 0


def _handle_db_verify_cas(args: argparse.Namespace) -> int:
    db = args.database  # already resolved by main() three-layer fallback
    attachment_root = resolve_store_root(
        db,
        cli_override=args.attachment_root,
        env_override=os.environ.get("MAILATTIC_ATTACHMENT_ROOT"),
    )
    result: CasVerifyResult = verify_cas(attachment_root)
    try:
        refs = verify_cas_references(db, attachment_root)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2
    print(f"attachment_root: {attachment_root}")
    print(f"total:           {result.total}")
    print(f"ok:              {result.ok}")
    print(f"bad:             {len(result.bad_files)}")
    print(f"mismatched:      {result.mismatched}")
    print(f"read_error:      {result.read_error}")
    print(f"non_cas:         {len(result.non_cas_files)}")
    print(f"referenced:      {refs.referenced}")
    print(f"missing_refs:    {len(refs.missing)}")
    if result.bad_files:
        print("bad files:")
        for path in result.bad_files:
            print(f"  {path}")
    if result.non_cas_files:
        print("non-CAS files:")
        for path in result.non_cas_files:
            print(f"  {path}")
    if refs.missing:
        print("missing references:")
        for ref in refs.missing:
            print(
                f"  attachment_id={ref.attachment_id} db_id={ref.db_id} "
                f"path={ref.storage_path}"
            )
    if (
        result.mismatched > 0
        or result.read_error > 0
        or len(result.non_cas_files) > 0
        or refs.missing
    ):
        return 1
    return 0


def _handle_db_prune_cas(args: argparse.Namespace) -> int:
    db = args.database  # already resolved by main() three-layer fallback
    attachment_root = resolve_store_root(
        db,
        cli_override=args.attachment_root,
        env_override=os.environ.get("MAILATTIC_ATTACHMENT_ROOT"),
    )
    result: CasVerifyResult = verify_cas(attachment_root)
    if result.non_cas_files:
        print(f"attachment_root: {attachment_root}", file=sys.stderr)
        print(
            f"prune-cas: refusing to prune because {len(result.non_cas_files)} "
            "file(s) do not match CAS entry shape (<root>/<aa>/<bb>/<64-hex>):",
            file=sys.stderr,
        )
        for path in result.non_cas_files:
            print(f"  {path}", file=sys.stderr)
        return 1

    if not result.bad_files:
        print(f"verify-cas: no bad files found in {attachment_root}")
        print("prune-cas: nothing to delete.")
        return 0

    print(f"attachment_root: {attachment_root}")
    print(f"Bad files to delete: {len(result.bad_files)}")
    for path in result.bad_files:
        print(f"  {path}")

    confirmed = False
    if getattr(args, "yes", False):
        confirmed = True
    elif sys.stdin.isatty() and sys.stdout.isatty():
        try:
            answer = input(
                f"Type 'yes' to delete {len(result.bad_files)} bad file(s): "
            ).strip()
        except EOFError:
            answer = ""
        confirmed = answer == "yes"

    if not confirmed:
        print(
            "prune-cas: confirmation required. "
            "Use --yes flag or run interactively and type 'yes'.",
            file=sys.stderr,
        )
        return 1

    deleted, failed = prune_cas(result.bad_files)
    print(f"deleted: {deleted}")
    print(f"failed:  {failed}")
    if failed > 0:
        return 1
    return 0


def _handle_db_no_subcommand(args: argparse.Namespace) -> int:
    db_parser = getattr(args, "_db_parser", None)
    if db_parser is not None:
        db_parser.print_help()
    else:
        print(
            "Usage: bes db <backup|doctor|reindex-fts|verify-cas|prune-cas|"
            "abandon-attachment-failure>",
            file=sys.stderr,
        )
    return 2


def _handle_identity_no_subcommand(args: argparse.Namespace) -> int:
    identity_parser = getattr(args, "_identity_parser", None)
    if identity_parser is not None:
        identity_parser.print_help()
    else:
        print("Usage: bes identity <add|remove|list>", file=sys.stderr)
    return 2


def _identity_report(connection: sqlite3.Connection, **extra: object) -> str:
    stored = list_self_addresses(connection)
    payload: dict[str, object] = {
        **extra,
        "addresses": stored,
        "source": "database" if stored else "unset",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _handle_identity_list(args: argparse.Namespace) -> int:
    try:
        connection = connect_database(args.database, create=False)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2
    try:
        print(_identity_report(connection))
    finally:
        connection.close()
    return 0


def _handle_identity_mutation(args: argparse.Namespace, *, removing: bool) -> int:
    # initialize_database is what creates self_addresses on a database that
    # predates the table -- the write path is also the migration path.
    try:
        database_path = initialize_database(args.database)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2
    connection = connect_database(database_path)
    try:
        mutate = remove_self_address if removing else add_self_address
        try:
            changed = mutate(connection, args.address)
        except ValueError as exc:
            print(_format_exception_message(exc), file=sys.stderr)
            return 2
        connection.commit()
        print(
            _identity_report(
                connection,
                action="remove" if removing else "add",
                address=args.address,
                changed=changed,
            )
        )
    finally:
        connection.close()
    return 0


def _handle_identity_add(args: argparse.Namespace) -> int:
    return _handle_identity_mutation(args, removing=False)


def _handle_identity_remove(args: argparse.Namespace) -> int:
    return _handle_identity_mutation(args, removing=True)


def _handle_init_db(args: argparse.Namespace) -> int:
    database_path = initialize_database(args.database)
    print(f"Initialized database at {database_path}")
    return 0


def _handle_rebuild(args: argparse.Namespace) -> int:
    if args.phase is not None and getattr(args, "run_all", False):
        print(
            "error: choose either --all or a single rebuild phase, not both",
            file=sys.stderr,
        )
        return 2

    try:
        return run_rebuild(
            phase=args.phase,
            run_all=bool(getattr(args, "run_all", False) or args.phase is None),
            resume=bool(getattr(args, "resume", False)),
            work_dir=args.work_dir,
            pst_paths=args.pst,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(_format_exception_message(exc), file=sys.stderr)
        return 2


def _unreachable_message_files(
    account_root: Path, sources: Sequence[MaildirSource]
) -> list[Path]:
    """Message-looking files under *account_root* the maildir reader cannot see.

    Reachability is not restated here -- it is whatever
    `iter_maildir_message_paths` yields, the same call the reader itself makes.
    Anything else under the tree that looks like a message is therefore
    unreachable by construction, including files under a dot-prefixed directory
    (`resolve_maildir_sources` never returns those as folders) and files beside
    a `cur` that turned out not to be a directory.

    Dot-prefixed *files* are maildir bookkeeping (`.uidvalidity`,
    `.mbsyncstate`, this project's `.imap_watermarks.json`), never messages.
    """
    reachable = {
        path.resolve()
        for source in sources
        for path in iter_maildir_message_paths(source.path)
    }
    return sorted(
        path
        for path in account_root.rglob("*")
        if path.is_file()
        and not path.name.startswith(".")
        and path.resolve() not in reachable
    )


def _handle_import_maildir(args: argparse.Namespace) -> int:
    """Import a local maildir tree through the same path `sync-imap` uses.

    Nothing here parses mail: `resolve_maildir_sources` / `import_maildir_sources`
    are the functions the IMAP runtime calls, so the dedup gate, the attachment
    CAS and `ingest_failures` behave identically whichever entry point ran.
    """
    if args.directories or args.files:
        print(
            "error: --maildir cannot be combined with --directory/--file.",
            file=sys.stderr,
        )
        return 2
    if not args.account_email:
        print("error: --maildir requires --account-email.", file=sys.stderr)
        return 2

    # Every filesystem touch lives inside the exception boundary: resolving,
    # listing folders and probing buckets all stat paths the operator named, so
    # an unreadable directory -- or one removed between two of these calls --
    # must reach the CLI's exit-2 convention. `main()` has no top-level handler,
    # so anything escaping here surfaces as a traceback instead.
    try:
        account_root = args.maildir.expanduser().resolve()
        if not account_root.is_dir():
            print(
                f"error: maildir account root not found: {account_root}",
                file=sys.stderr,
            )
            return 2

        sources = resolve_maildir_sources(
            maildir_root=account_root.parent,
            account_email=args.account_email,
            account_name=account_root.name,
        )
        # Count, do not predict. Three separate attempts to guess which files
        # the parser would read (whole tree, then per folder, then per folder
        # root) each left a different gap, because the guess and the parser are
        # two implementations of one fact. This instead measures the operator's
        # own directory: anything that looks like a message but sits outside a
        # bucket would be dropped with no row, no `ingest_failures` entry and no
        # failure count -- indistinguishable from an archive that never held it.
        unreachable = _unreachable_message_files(account_root, sources)
        if unreachable:
            listed = ", ".join(str(path) for path in unreachable[:10])
            more = "" if len(unreachable) <= 10 else f" (+{len(unreachable) - 10} more)"
            print(
                f"error: {len(unreachable)} file(s) under {account_root} sit "
                f"outside a {'/'.join(MAILDIR_BUCKETS)} directory and would be "
                f"imported as nothing: {listed}{more}. Expected layout: "
                "<account-root>/<folder>/cur/<message>.",
                file=sys.stderr,
            )
            return 2

        # A separate question from reachability: does this tree even look like
        # a maildir? Without it an account root of empty folders reports
        # "imported 0 messages" at exit 0, which reads exactly like an archive
        # that is genuinely empty. Nothing is lost either way -- the cost is
        # that the operator cannot tell "wrong directory" from "nothing here".
        shapeless = [
            source.path.name
            for source in sources
            if not any((source.path / bucket).is_dir() for bucket in MAILDIR_BUCKETS)
        ]
        if shapeless or not sources:
            listed = ", ".join(sorted(shapeless)) if shapeless else "(no folders)"
            print(
                f"error: {account_root} does not look like a maildir account "
                f"root -- {listed} has no {'/'.join(MAILDIR_BUCKETS)} "
                "directory. Expected layout: "
                "<account-root>/<folder>/cur/<message>.",
                file=sys.stderr,
            )
            return 2

        initialize_database(args.database)
        attachment_root = resolve_store_root(
            args.database.resolve(),
            cli_override=args.attachment_root,
            env_override=os.environ.get("MAILATTIC_ATTACHMENT_ROOT"),
        )
        summary = import_maildir_sources(
            database_path=args.database,
            sources=sources,
            attachment_root=attachment_root,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        # sqlite3.Error is not an OSError subclass, so a database path that
        # points at a directory escapes an OSError-only tuple. Every other
        # database-touching handler in this file lists it; this one inherited
        # the omission from the PST branch.
        print(_format_exception_message(exc), file=sys.stderr)
        return 2

    payload = {
        "discovered_maildir_folders": len(sources),
        "imported_messages": summary.imported_messages,
        "duplicate_messages": summary.duplicate_messages,
        "failure_count": summary.failure_count,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "Imported "
            f"{summary.imported_messages} messages "
            f"({summary.duplicate_messages} duplicates, "
            f"{summary.failure_count} failures) from "
            f"{len(sources)} maildir folders."
        )
    if (
        summary.failure_count > 0
        and summary.imported_messages == 0
        and summary.duplicate_messages == 0
    ):
        return 2
    return 0


def _handle_import(args: argparse.Namespace) -> int:
    if args.maildir is not None:
        return _handle_import_maildir(args)
    try:
        initialize_database(args.database)
        sources = resolve_ingestion_sources(
            directory_inputs=args.directories,
            file_inputs=args.files,
        )
        attachment_root = resolve_store_root(
            args.database.resolve(),
            cli_override=args.attachment_root,
            env_override=os.environ.get("MAILATTIC_ATTACHMENT_ROOT"),
        )
        summary = import_pst_sources(
            database_path=args.database,
            sources=sources,
            attachment_root=attachment_root,
        )
    except (
        FileNotFoundError,
        NotADirectoryError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(_format_exception_message(exc))
        return 2
    payload = {
        "discovered_psts": summary.discovered_psts,
        "imported_messages": summary.imported_messages,
        "duplicate_messages": summary.duplicate_messages,
        "failure_count": summary.failure_count,
        "recipient_extraction_summary_path": (
            summary.recipient_extraction_summary_path
        ),
        "recipient_extraction_complete": summary.recipient_extraction_complete,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "Imported "
            f"{summary.imported_messages} messages "
            f"({summary.duplicate_messages} duplicates, "
            f"{summary.failure_count} failures) from "
            f"{summary.discovered_psts} PST files."
        )
        if summary.recipient_extraction_summary_path:
            print(
                "Recipient extraction summary: "
                f"{summary.recipient_extraction_summary_path}"
            )
    # The account decides completeness (issue #151 D6), not the incident
    # count: a per-message parse failure alone must not make this non-zero.
    if not summary.recipient_extraction_complete:
        return 2
    if (
        summary.failure_count > 0
        and summary.imported_messages == 0
        and summary.duplicate_messages == 0
    ):
        return 2
    return 0


def _handle_sync_imap(args: argparse.Namespace) -> int:
    # --pre-task: enforce 15-minute minimum interval before running a live sync.
    # The scheduled daily sync path is a separate process (Windows Task Scheduler
    # → docker compose run) and never sets --pre-task, so it is completely
    # unaffected by this gate.
    if getattr(args, "json", False) and not getattr(args, "pre_task", False):
        print("sync-imap: --json is only supported with --pre-task", file=sys.stderr)
        return 2
    if getattr(args, "pre_task", False):
        try:
            attachment_root = resolve_store_root(
                args.database.resolve(),
                cli_override=args.attachment_root,
                env_override=os.environ.get("MAILATTIC_ATTACHMENT_ROOT"),
            )
            outcome = pre_task_sync_imap(
                database_path=args.database,
                env_file=args.env_file,
                maildir_root=args.maildir_root,
                log_dir=args.log_dir,
                attachment_root=attachment_root,
                folder=args.folder,
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            if getattr(args, "json", False):
                payload = {
                    "status": "failed",
                    "exit_code": 1,
                    "error": _format_exception_message(exc),
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 1
            print(f"mbsync: ERROR config: {_format_exception_message(exc)}")
            print("ingest: SKIPPED (mbsync failed)")
            print("exit code: 1")
            return 1

        if getattr(args, "json", False):
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
            return int(outcome.get("exit_code", 0))

        if outcome["status"] == "skipped":
            if outcome.get("last_sync_failed"):
                # unfinished: a later run never recorded completion (failed
                # before logging, or still running) -- "failed" would be false.
                marker = {
                    "unfinished": "last sync did not complete",
                    "state_unreadable": "last sync status unknown",
                }.get(str(outcome.get("last_sync_failure_stage")), "last sync failed")
                print(
                    f"sync-imap: skipped (last sync: {outcome['last_sync_at']}; "
                    f"elapsed: {outcome['elapsed_seconds']:.0f}s < 900s interval; "
                    f"{marker})"
                )
            else:
                print(
                    f"sync-imap: skipped (last sync: {outcome['last_sync_at']}; "
                    f"elapsed: {outcome['elapsed_seconds']:.0f}s < 900s interval)"
                )
            print("exit code: 0")
            return 0

        # status == "synced" — print the standard summary
        exit_code = int(outcome.get("exit_code", 0))
        if exit_code != 0:
            print(
                f"mbsync: ERROR {outcome.get('failure_stage') or 'sync'}: "
                f"{outcome.get('failure_message') or 'sync failed'}"
            )
            inserted = int(outcome.get("inserted_messages", 0))
            deduped = int(outcome.get("deduplicated_messages", 0))
            failures = int(outcome.get("failure_count", 0))
            if inserted > 0 or deduped > 0:
                print(
                    f"ingest: PARTIAL ({inserted} inserted, {deduped} deduped, "
                    f"{failures} folders failed) — exit_code={exit_code}"
                )
            else:
                print("ingest: SKIPPED (mbsync failed)")
            print(_attachment_retry_line(outcome))
            print(f"exit code: {exit_code}")
            return exit_code
        print(
            f"mbsync: pulled {outcome.get('pulled_messages', 0)} new messages from "
            f"{outcome.get('folder_count', 0)} folders"
        )
        print(
            f"ingest: {outcome.get('candidate_messages', 0)} candidates "
            f"→ {outcome.get('inserted_messages', 0)} inserted, "
            f"{outcome.get('deduplicated_messages', 0)} deduped"
        )
        print(_attachment_retry_line(outcome))
        print("exit code: 0")
        return 0

    if args.full_resync:
        response = input(
            "This will delete local watermark state and force a full resync. "
            "Continue? [y/N]: "
        ).strip()
        if response.lower() not in {"y", "yes"}:
            print("full resync cancelled")
            return 0

    try:
        if args.dry_run:
            summary = estimate_sync_run(
                env_file=args.env_file,
                maildir_root=args.maildir_root,
                folder=args.folder,
                database_path=args.database,
            )
            print(
                "mbsync: dry-run estimated "
                f"{summary.estimated_new_messages} new messages from "
                f"{summary.folder_count} folders"
            )
            if summary.bulk_guard_folders:
                for folder_name, uid_count in summary.bulk_guard_folders:
                    print(
                        f"  WARNING: {folder_name} has {uid_count} UIDs "
                        "(exceeds bulk threshold) -- use --full-resync to fetch"
                    )
            for skipped in summary.skipped_folders:
                print(f"  ERROR: skipped folder: {skipped}")
            print("ingest: SKIPPED (dry-run)")
            exit_code = 1 if summary.skipped_folders else 0
            print(f"exit code: {exit_code}")
            return exit_code

        attachment_root = resolve_store_root(
            args.database.resolve(),
            cli_override=args.attachment_root,
            env_override=os.environ.get("MAILATTIC_ATTACHMENT_ROOT"),
        )
        result = run_sync_imap(
            database_path=args.database,
            env_file=args.env_file,
            maildir_root=args.maildir_root,
            log_dir=args.log_dir,
            attachment_root=attachment_root,
            folder=args.folder,
            full_resync=args.full_resync,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"mbsync: ERROR config: {_format_exception_message(exc)}")
        print("ingest: SKIPPED (mbsync failed)")
        print("exit code: 1")
        return 1

    if result.exit_code != 0:
        print(
            "mbsync: ERROR "
            f"{result.failure_stage or 'sync'}: "
            f"{result.failure_message or 'sync failed'}"
        )
        if result.inserted_messages > 0 or result.deduplicated_messages > 0:
            print(
                f"ingest: PARTIAL ({result.inserted_messages} inserted, "
                f"{result.deduplicated_messages} deduped, "
                f"{result.failure_count} folders failed) — exit_code={result.exit_code}"
            )
        else:
            print("ingest: SKIPPED (mbsync failed)")
        print(_attachment_retry_line(result.to_dict()))
        print(f"exit code: {result.exit_code}")
        return result.exit_code

    print(
        f"mbsync: pulled {result.pulled_messages} new messages from "
        f"{result.folder_count} folders"
    )
    print(
        f"ingest: {result.candidate_messages} candidates "
        f"→ {result.inserted_messages} inserted, "
        f"{result.deduplicated_messages} deduped"
    )
    print(_attachment_retry_line(result.to_dict()))
    print("exit code: 0")
    return 0


def _attachment_retry_line(outcome: Mapping[str, object]) -> str:
    """#255: the three counters of the start-of-sync attachment retry."""
    return (
        "attachment-retry: "
        f"{outcome.get('attachments_recovered', 0)} recovered, "
        f"{outcome.get('attachments_still_failing', 0)} still failing, "
        f"{outcome.get('attachments_source_missing', 0)} source missing"
    )


def _handle_scrape_archive(args: argparse.Namespace) -> int:
    try:
        from_date = _parse_optional_date(args.from_date)
        to_date = _parse_optional_date(args.to_date)
        connection = connect_database(args.database, create=False)
        try:
            senders = resolve_self_addresses(connection)
        finally:
            connection.close()
        if not senders:
            print(
                "No self address is declared, so there is no sender to scrape "
                "for. Declare one with `bes identity add <email>`.",
                file=sys.stderr,
            )
            return 2
        summary: ScrapeSummary = run_scrape(
            from_date=from_date,
            to_date=to_date,
            senders=senders,
            dry_run=args.dry_run,
            database_path=args.database,
        )
    except NotImplementedError as exc:
        print(_format_exception_message(exc))
        return 2
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(_format_exception_message(exc))
        return 2
    return summary.exit_code


def _parse_optional_date(value: str | None) -> date | None:
    """Parse `scrape-archive`'s `--from` / `--to` window bound.

    Serves the dormant archive-scrape window only -- deliberately not a
    send-time DB bound. See
    :mod:`mailattic.datetime_bounds` for that rule and
    :func:`_parse_optional_datetime` for the surface that implements it.
    """
    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _format_exception_message(exc: Exception) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, io.UnsupportedOperation):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    # Apply three-layer database fallback only when the subcommand declared
    # `--database` and the user didn't pass it; subcommands without `--database`
    # are left untouched (no silent attribute injection).
    if hasattr(args, "database") and args.database is None:
        args.database = _resolve_database_default()
    return int(handler(args))

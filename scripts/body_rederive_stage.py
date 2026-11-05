"""body_rederive_stage.py — stage builder for re-deriving stored message bodies
(issue #14 phase 2, first half of a two-step migration).

Phase 1 fixed the HTML-to-text conversion for newly ingested mail only. Rows
already in the canonical database still carry the pre-table-fidelity
``messages.body_text``. This script re-derives the body of PST-imported rows
(``source_input_mode='file'``) into a separate *stage* SQLite database. It
never writes the canonical database; a later step applies the stage.

How a row is re-derived (``pst`` subcommand):
  1. every distinct ``pst_source`` of the PST rows is re-unpacked with
     ``parser.ReadpstParser`` -- the same reader the original import used --
     so the body comes from ``parser._extract_body_text`` (via
     ``_parse_email_message``) passed through ``models._normalize_body_text``,
     exactly what ingest stores today. Nothing here reimplements extraction;
  2. a canonical row is paired with the re-unpacked message by
     (``pst_source``, ``logical_locator`` = ``<folder>/mbox#N``) and the pair
     is then verified by Message-ID (``query.normalize_message_id``);
  3. rows with no Message-ID are NEVER staged (Sean ruling, issue #14: their
     ``dedup_key`` embeds a body hash, so rewriting the body would make a
     later re-import disagree with the stored key). They are counted, and so
     is how many of them would contain a data table after re-derivation.

"Contains a table" is phase 1's own decision: ``_HTMLTextExtractor.
has_data_table`` as returned by ``parser._html_to_text_and_table_flag`` for the
message's HTML part(s). ``_extract_body_text`` consumes that flag but does not
expose it, so this script wraps the function for the duration of a run to
observe what the unchanged phase-1 code decided (no second heuristic).

Stage DB (``body_rederive`` table): canonical ``messages.id``, ``source_kind``
(``pst``), ``pre_image_sha256`` (SHA-256 of the canonical ``body_text`` encoded
as UTF-8, at extraction time -- the pre-image fingerprint the apply step must
re-check), ``new_body_text`` and ``status`` (``rewrite``). Rows whose
re-derived body equals the canonical one are counted as ``unchanged`` and not
staged.

How IMAP rows are re-derived (``imap`` subcommand):
  * scope is ``source_input_mode='maildir'``; ``archive`` / ``outbound`` rows
    are out of scope and not counted;
  * ``logical_locator`` ``<display folder>/new/uid-N[.v<uidvalidity>[.<hash>]].eml``
    gives (display folder, UID). Old mbsync file names are counted as
    ``locator_unparsable`` and never fetched;
  * the display folder is mapped to the server's own wire name through
    ``NativeImapClient.list_folders()``; a folder no longer listed counts all
    its rows as ``folder_missing``;
  * per folder: SELECT readonly, then ``fetch_rfc822(uid)`` (BODY.PEEK). The
    body is what ingest derives for a maildir message (``_parse_email_message``
    + ``_normalize_body_text``). A UID the server no longer holds is
    ``not_on_server``. The derived Message-ID must equal the row's
    (UIDVALIDITY-change guard) or the row is ``message_id_mismatch``;
  * only LIST / STATUS / SELECT readonly / UID FETCH BODY.PEEK[] are issued;
  * a lost connection stops the run: what is staged is committed, the
    remaining rows are counted as ``fetch_error`` and the exit code is 2.

Usage:
    uv run python scripts/body_rederive_stage.py pst --database <canonical.db> \\
        --stage <stage.db> [--readpst-path P] [--pst <path> ...] [--overwrite]
    uv run python scripts/body_rederive_stage.py imap --database <canonical.db> \\
        --stage <stage.db> --env-file <mailbox settings> [--folder <display> ...] \\
        [--limit N] [--overwrite]

The canonical database is opened read-only (``mode=ro``). The only writes are
the stage database, a scratch work directory beside it (readpst output,
removed afterwards) and a JSON report ``<stage stem>-report.json`` next to the
stage. A stage that already exists is refused unless ``--overwrite`` is given.
A stage holds one source kind (``stage_meta.source_kind``).

Exit codes:
    0 — stage built, report written
    1 — refused (stage exists without --overwrite, stage is the canonical DB,
        canonical DB missing), readpst could not be located, or the IMAP
        settings / connection could not be established
    2 — imap run stopped part-way (connection lost) or had fetch errors
"""

from __future__ import annotations

import argparse
import hashlib
import imaplib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic import parser as mail_parser  # noqa: E402
from mailattic.imap_native import ImapFolder, NativeImapClient  # noqa: E402
from mailattic.imap_runtime import (  # noqa: E402
    load_env_file,
    validate_sync_environment,
)
from mailattic.models import PstSource, _normalize_body_text  # noqa: E402
from mailattic.parser import ReadpstParser  # noqa: E402
from mailattic.query import normalize_message_id  # noqa: E402
from mailattic.storage import connect_database, resolve_database_default  # noqa: E402

SOURCE_KIND_PST = "pst"
SOURCE_KIND_IMAP = "imap"
STATUS_REWRITE = "rewrite"

_STAGE_SCHEMA = """
CREATE TABLE body_rederive (
    message_id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    pre_image_sha256 TEXT NOT NULL,
    new_body_text TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE stage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_OUTCOMES = (
    "rewrite",
    "unchanged",
    "no_message_id",
    "no_message_id_with_table",
    "no_message_id_unpaired",
    "message_id_mismatch",
    "locator_not_found",
    "pst_missing",
    "parse_failure",
)

_IMAP_OUTCOMES = (
    "rewrite",
    "unchanged",
    "no_message_id",
    "message_id_mismatch",
    "locator_unparsable",
    "folder_missing",
    "not_on_server",
    "select_failed",
    "fetch_error",
    "parse_failure",
    # Informational: not an outcome of its own (the row also lands in one of
    # the outcomes above); the locator's UIDVALIDITY differs from the server's.
    "uidvalidity_changed",
)

_LOCATOR_RE = re.compile(
    r"^(?P<folder>.+)/new/uid-(?P<uid>\d+)"
    r"(?:\.v(?P<uidvalidity>\d+)(?:\.[0-9a-f]+)?)?\.eml$"
)

# A dropped / unreachable connection: stop the run (anything else is per row).
_CONNECTION_ERRORS = (imaplib.IMAP4.abort, OSError, EOFError)


class StageRefused(RuntimeError):
    """The run was refused before any write."""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _TableFlagRecorder:
    """Observes phase 1's data-table decision while messages are parsed."""

    def __init__(self) -> None:
        self.seen = False


@contextmanager
def _record_table_flag() -> Iterator[_TableFlagRecorder]:
    recorder = _TableFlagRecorder()
    original = mail_parser._html_to_text_and_table_flag

    def wrapper(html: str):
        result = original(html)
        if result[1]:
            recorder.seen = True
        return result

    mail_parser._html_to_text_and_table_flag = wrapper
    try:
        yield recorder
    finally:
        mail_parser._html_to_text_and_table_flag = original


def _same_path(left: str, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _report_path(stage: Path) -> Path:
    return stage.parent / f"{stage.stem}-report.json"


def _remove_stage_files(stage: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(stage) + suffix)
        if candidate.exists():
            candidate.unlink()


def _canonical_pst_sources(con: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in con.execute(
            "SELECT DISTINCT pst_source FROM messages "
            "WHERE source_input_mode = 'file' ORDER BY pst_source"
        )
    ]


def _canonical_rows_by_locator(
    con: sqlite3.Connection, pst_source: str
) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    cursor = con.execute(
        "SELECT id, message_id, logical_locator, body_text FROM messages "
        "WHERE source_input_mode = 'file' AND pst_source = ?",
        (pst_source,),
    )
    for row in cursor:
        rows.setdefault(row["logical_locator"], []).append(
            {
                "id": row["id"],
                "message_id": normalize_message_id(row["message_id"]),
                "body_sha256": _sha256_text(row["body_text"]),
            }
        )
    return rows


def _empty_counts(outcomes: tuple[str, ...] = _OUTCOMES) -> dict[str, int]:
    return dict.fromkeys(outcomes, 0)


def _stage_rewrite(
    stage_con: sqlite3.Connection,
    row: dict,
    new_body: str,
    source_kind: str,
) -> None:
    stage_con.execute(
        "INSERT INTO body_rederive "
        "(message_id, source_kind, pre_image_sha256, new_body_text, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (row["id"], source_kind, row["body_sha256"], new_body, STATUS_REWRITE),
    )


def _prepare_stage(database: Path, stage: Path, overwrite: bool) -> None:
    if database.resolve() == stage.resolve():
        raise StageRefused(f"--stage resolves to the canonical database ({database})")
    if not database.exists():
        raise StageRefused(f"canonical database not found: {database}")
    if stage.exists():
        if not overwrite:
            raise StageRefused(
                f"stage already exists: {stage} (use --overwrite to replace it)"
            )
        _remove_stage_files(stage)
    stage.parent.mkdir(parents=True, exist_ok=True)


def _classify_pair(
    row: dict,
    derived_message_id: str,
    new_body: str,
    has_table: bool,
    counts: dict[str, int],
    stage_con: sqlite3.Connection,
) -> None:
    if not row["message_id"]:
        # Never staged. Only count a table when the re-unpacked message is
        # also id-less, i.e. it is plausibly the same message.
        counts["no_message_id"] += 1
        if derived_message_id:
            counts["no_message_id_unpaired"] += 1
        elif has_table:
            counts["no_message_id_with_table"] += 1
        return
    if derived_message_id != row["message_id"]:
        counts["message_id_mismatch"] += 1
        return
    if _sha256_text(new_body) == row["body_sha256"]:
        counts["unchanged"] += 1
        return
    _stage_rewrite(stage_con, row, new_body, SOURCE_KIND_PST)
    counts["rewrite"] += 1


def _process_pst(
    pst_source: str,
    rows: dict[str, list[dict]],
    parser_factory,
    stage_con: sqlite3.Connection,
) -> dict[str, int]:
    counts = _empty_counts()
    if not Path(pst_source).exists():
        counts["pst_missing"] = sum(len(v) for v in rows.values())
        return counts

    source = PstSource(
        path=Path(pst_source), input_mode="file", requested_from=Path(pst_source)
    )
    failed_locators: set[str] = set()
    pst_level_failure = False
    parser = parser_factory()
    with _record_table_flag() as recorder:
        iterator = iter(parser.iter_extraction_results(source))
        while True:
            recorder.seen = False
            try:
                result = next(iterator)
            except StopIteration:
                break
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"{pst_source}: {exc}", file=sys.stderr)
                pst_level_failure = True
                break
            if result.failure is not None:
                if result.failure.logical_locator:
                    failed_locators.add(result.failure.logical_locator)
                continue
            extracted = result.message
            if extracted is None:
                continue
            matched = rows.pop(extracted.logical_locator, None)
            if matched is None:
                continue
            raw = extracted.raw_message
            derived_message_id = normalize_message_id(raw.get("message_id"))
            new_body = _normalize_body_text(raw.get("body_text"))
            for row in matched:
                _classify_pair(
                    row, derived_message_id, new_body, recorder.seen, counts, stage_con
                )

    for locator, leftover in rows.items():
        key = (
            "parse_failure"
            if pst_level_failure or locator in failed_locators
            else "locator_not_found"
        )
        counts[key] += len(leftover)
    return counts


def build_pst_stage(
    *,
    database: Path,
    stage: Path,
    readpst_path: Path | None,
    pst_filter: list[Path],
    overwrite: bool,
) -> dict:
    _prepare_stage(database, stage, overwrite)
    work_root = stage.parent / f"{stage.stem}-work"
    started = datetime.now(UTC)

    canonical = connect_database(database, read_only=True)
    try:
        pst_sources = _canonical_pst_sources(canonical)
        not_in_canonical: list[str] = []
        if pst_filter:
            wanted = [str(p) for p in pst_filter]
            selected = [
                s for s in pst_sources if any(_same_path(s, Path(w)) for w in wanted)
            ]
            not_in_canonical = [
                w
                for w in wanted
                if not any(_same_path(s, Path(w)) for s in pst_sources)
            ]
            pst_sources = selected

        parser_cache: list[ReadpstParser] = []

        def parser_factory() -> ReadpstParser:
            if not parser_cache:
                work_root.mkdir(parents=True, exist_ok=True)
                parser_cache.append(
                    ReadpstParser(readpst_path=readpst_path, work_root=work_root)
                )
            return parser_cache[0]

        stage_con = sqlite3.connect(stage)
        totals = _empty_counts()
        per_pst: list[dict] = []
        try:
            stage_con.executescript(_STAGE_SCHEMA)
            for pst_source in pst_sources:
                began = time.perf_counter()
                rows = _canonical_rows_by_locator(canonical, pst_source)
                row_count = sum(len(v) for v in rows.values())
                counts = _process_pst(pst_source, rows, parser_factory, stage_con)
                stage_con.commit()
                for key, value in counts.items():
                    totals[key] += value
                per_pst.append(
                    {
                        "pst_source": pst_source,
                        "rows": row_count,
                        "elapsed_seconds": round(time.perf_counter() - began, 3),
                        "counts": counts,
                    }
                )
            stage_con.executemany(
                "INSERT INTO stage_meta (key, value) VALUES (?, ?)",
                [
                    ("source_kind", SOURCE_KIND_PST),
                    ("canonical", str(database)),
                    ("created_at", started.isoformat()),
                ],
            )
            stage_con.commit()
        finally:
            stage_con.close()
    finally:
        canonical.close()
        with suppress(OSError):
            work_root.rmdir()

    return {
        "mode": "pst",
        "canonical": str(database),
        "stage": str(stage),
        "started_at": started.isoformat(),
        "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
        "rows_in_scope": sum(p["rows"] for p in per_pst),
        "counts": totals,
        "requested_psts_not_in_canonical": not_in_canonical,
        "per_pst": per_pst,
    }


def _canonical_maildir_rows(con: sqlite3.Connection) -> list[dict]:
    rows: list[dict] = []
    cursor = con.execute(
        "SELECT id, message_id, logical_locator, body_text FROM messages "
        "WHERE source_input_mode = 'maildir' ORDER BY id"
    )
    for row in cursor:
        rows.append(
            {
                "id": row["id"],
                "message_id": normalize_message_id(row["message_id"]),
                "locator": row["logical_locator"] or "",
                "body_sha256": _sha256_text(row["body_text"]),
            }
        )
    return rows


def _derive_imap_message(raw_bytes: bytes) -> tuple[str, str]:
    """(normalised Message-ID, normalised body) the way ingest derives a
    maildir message: ``_parse_eml_file`` is exactly these two calls on the
    file's bytes (no attachment_root, so nothing is written)."""
    message = BytesParser(policy=mail_parser._MAIL_POLICY).parsebytes(raw_bytes)
    raw = mail_parser._parse_email_message(message, raw_bytes=raw_bytes)
    return (
        normalize_message_id(raw.get("message_id")),
        _normalize_body_text(raw.get("body_text")),
    )


def _process_imap_folder(
    client,
    folder: ImapFolder,
    items: list[tuple[int, int | None, dict]],
    stage_con: sqlite3.Connection,
    counts: dict[str, int],
) -> bool:
    """Re-fetch *items* (uid, locator uidvalidity, row) of one folder.

    Returns True when the connection was lost (the unprocessed rows have
    already been counted as ``fetch_error``)."""
    current_uidvalidity: int | None = None
    if any(uv is not None for _, uv, _ in items):
        try:
            current_uidvalidity = client.get_uidvalidity(folder.wire_name)
        except _CONNECTION_ERRORS:
            counts["fetch_error"] += len(items)
            return True
        except (RuntimeError, imaplib.IMAP4.error):
            current_uidvalidity = None  # informational counter only
    try:
        client.select_folder(folder.wire_name)
    except _CONNECTION_ERRORS:
        counts["fetch_error"] += len(items)
        return True
    except (RuntimeError, imaplib.IMAP4.error):
        counts["select_failed"] += len(items)
        return False

    for index, (uid, locator_uidvalidity, row) in enumerate(items):
        if not row["message_id"]:
            counts["no_message_id"] += 1
            continue
        if (
            locator_uidvalidity is not None
            and current_uidvalidity is not None
            and locator_uidvalidity != current_uidvalidity
        ):
            counts["uidvalidity_changed"] += 1
        try:
            raw_bytes = client.fetch_rfc822(uid)
        except _CONNECTION_ERRORS:
            counts["fetch_error"] += len(items) - index
            return True
        except RuntimeError as exc:
            # OK reply without a body = the UID is no longer on the server.
            if str(exc).startswith("Could not extract RFC822 bytes"):
                counts["not_on_server"] += 1
            else:
                counts["fetch_error"] += 1
            continue
        except imaplib.IMAP4.error:
            counts["fetch_error"] += 1
            continue
        try:
            derived_message_id, new_body = _derive_imap_message(raw_bytes)
        except Exception as exc:
            print(f"{folder.display_name} uid {uid}: {exc}", file=sys.stderr)
            counts["parse_failure"] += 1
            continue
        if derived_message_id != row["message_id"]:
            counts["message_id_mismatch"] += 1
            continue
        if _sha256_text(new_body) == row["body_sha256"]:
            counts["unchanged"] += 1
            continue
        _stage_rewrite(stage_con, row, new_body, SOURCE_KIND_IMAP)
        counts["rewrite"] += 1
    return False


def build_imap_stage(
    *,
    database: Path,
    stage: Path,
    env: dict[str, str],
    folder_filter: list[str],
    overwrite: bool,
    limit: int | None,
    client_factory: Callable[[dict[str, str]], NativeImapClient] | None = None,
) -> dict:
    _prepare_stage(database, stage, overwrite)
    factory = client_factory or (lambda e: NativeImapClient(e))
    started = datetime.now(UTC)

    canonical = connect_database(database, read_only=True)
    try:
        rows = _canonical_maildir_rows(canonical)
    finally:
        canonical.close()
    if folder_filter:
        rows = [
            r
            for r in rows
            if any(r["locator"].startswith(f + "/") for f in folder_filter)
        ]
    if limit is not None:
        rows = rows[:limit]

    totals = _empty_counts(_IMAP_OUTCOMES)
    by_folder: dict[str, list[tuple[int, int | None, dict]]] = {}
    for row in rows:
        match = _LOCATOR_RE.match(row["locator"])
        if match is None:
            totals["locator_unparsable"] += 1
            continue
        uv = match["uidvalidity"]
        by_folder.setdefault(match["folder"], []).append(
            (int(match["uid"]), int(uv) if uv is not None else None, row)
        )

    per_folder: list[dict] = []
    aborted = False
    client = factory(env)
    try:
        listed: dict[str, ImapFolder] = {}
        for folder in client.list_folders():
            listed.setdefault(folder.display_name, folder)

        stage_con = sqlite3.connect(stage)
        try:
            stage_con.executescript(_STAGE_SCHEMA)
            for display, items in by_folder.items():
                began = time.perf_counter()
                counts = _empty_counts(_IMAP_OUTCOMES)
                folder = listed.get(display)
                if folder is None:
                    counts["folder_missing"] += len(items)
                elif aborted:
                    counts["fetch_error"] += len(items)
                else:
                    items.sort(key=lambda item: item[0])
                    aborted = _process_imap_folder(
                        client, folder, items, stage_con, counts
                    )
                stage_con.commit()
                for key, value in counts.items():
                    totals[key] += value
                per_folder.append(
                    {
                        "folder": display,
                        "wire_name": folder.wire_name if folder else None,
                        "rows": len(items),
                        "elapsed_seconds": round(time.perf_counter() - began, 3),
                        "counts": counts,
                    }
                )
            stage_con.executemany(
                "INSERT INTO stage_meta (key, value) VALUES (?, ?)",
                [
                    ("source_kind", SOURCE_KIND_IMAP),
                    ("canonical", str(database)),
                    ("created_at", started.isoformat()),
                ],
            )
            stage_con.commit()
        finally:
            stage_con.close()
    finally:
        client.close()

    return {
        "mode": "imap",
        "canonical": str(database),
        "stage": str(stage),
        "started_at": started.isoformat(),
        "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
        "rows_in_scope": len(rows),
        "aborted": aborted,
        "gone_from_server": totals["not_on_server"] + totals["folder_missing"],
        "counts": totals,
        "per_folder": per_folder,
    }


def _print_summary(report: dict, report_path: Path) -> None:
    counts = report["counts"]
    if report["mode"] == "imap":
        print(
            f"[stage] {report['rows_in_scope']} IMAP rows in scope "
            f"across {len(report['per_folder'])} folder(s), "
            f"{report['elapsed_seconds']}s"
        )
        outcomes = _IMAP_OUTCOMES
    else:
        print(
            f"[stage] {report['rows_in_scope']} PST rows in scope "
            f"across {len(report['per_pst'])} PST(s), "
            f"{report['elapsed_seconds']}s"
        )
        outcomes = _OUTCOMES
    for key in outcomes:
        print(f"  {key}: {counts[key]}")
    if report["mode"] == "imap":
        print(
            f"  gone_from_server (not_on_server + folder_missing): "
            f"{report['gone_from_server']}"
        )
        if report["aborted"]:
            print("[stage] ABORTED: connection lost; remaining rows are fetch_error")
    print(f"[stage] stage database: {report['stage']}")
    print(f"[stage] report: {report_path}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage re-derived message bodies (issue #14 phase 2)."
    )
    sub = parser.add_subparsers(dest="source", required=True)
    pst = sub.add_parser("pst", help="Re-derive PST-imported rows via readpst.")
    pst.add_argument("--database", type=Path, default=None)
    pst.add_argument("--stage", type=Path, required=True)
    pst.add_argument("--readpst-path", type=Path, default=None)
    pst.add_argument("--pst", type=Path, action="append", default=[])
    pst.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing stage database.",
    )
    imap = sub.add_parser(
        "imap", help="Re-derive IMAP-ingested (maildir) rows by re-fetching."
    )
    imap.add_argument("--database", type=Path, default=None)
    imap.add_argument("--stage", type=Path, required=True)
    imap.add_argument(
        "--env-file",
        type=Path,
        required=True,
        help="Mailbox settings file (same format as `bes sync-imap --env-file`).",
    )
    imap.add_argument(
        "--folder",
        action="append",
        default=[],
        help="Display folder name to limit the run to (repeatable).",
    )
    imap.add_argument("--limit", type=int, default=None, help="Max rows (trial run).")
    imap.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing stage database.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args(argv)
    database = resolve_database_default(args.database).expanduser().resolve()
    stage = Path(args.stage).expanduser().resolve()
    try:
        if args.source == "imap":
            env = load_env_file(args.env_file)
            validate_sync_environment(env)
            report = build_imap_stage(
                database=database,
                stage=stage,
                env=env,
                folder_filter=list(args.folder),
                overwrite=args.overwrite,
                limit=args.limit,
            )
        else:
            report = build_pst_stage(
                database=database,
                stage=stage,
                readpst_path=args.readpst_path,
                pst_filter=list(args.pst),
                overwrite=args.overwrite,
            )
    except StageRefused as exc:
        print(f"[refuse] {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, mail_parser.ParserUnavailableError) as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, imaplib.IMAP4.error, OSError) as exc:
        # imap only: settings missing/invalid, or the connection / LIST failed
        # before anything was staged. (A pst run keeps its own behaviour.)
        if args.source != "imap":
            raise
        print(f"[abort] {exc}", file=sys.stderr)
        return 1
    report_path = _report_path(stage)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_summary(report, report_path)
    if report["mode"] == "imap":
        counts = report["counts"]
        if report["aborted"] or counts["fetch_error"] or counts["select_failed"]:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

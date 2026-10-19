from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .attachment_store import (
    resolve_store_root,
    verify_cas,
    verify_cas_references,
)
from .db_maintenance import check_database
from .imap_sync import (
    DEFAULT_ENV_FILE,
    DEFAULT_LOG_DIR,
    DEFAULT_MAILDIR_ROOT,
    run_sync_imap,
)
from .ingest import import_pst_sources, resolve_ingestion_sources
from .outlook_pst import OutlookComReader
from .parser import ReadpstParser
from .query import SearchFilters, search_messages
from .storage import connect_database, initialize_database

PHASE_SEQUENCE: tuple[str, ...] = (
    "preflight",
    "coverage-report",
    "import",
    "backfill",
    "verify-cas",
    "validate",
)
CHECKPOINT_FILENAME = "rebuild-checkpoint.json"
CHECKPOINT_SCHEMA_VERSION = 1
DEFAULT_STAGE_DB_NAME = "stage.db"
SERVER_WINDOW_DAYS = 30
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class RebuildContext:
    work_dir: Path
    stage_db: Path
    attachment_root: Path
    pst_paths: tuple[Path, ...]
    readpst_path: Path | None
    env_file: Path
    maildir_root: Path
    log_dir: Path
    resume: bool = False


def checkpoint_path(work_dir: str | Path) -> Path:
    return Path(work_dir).expanduser().resolve() / CHECKPOINT_FILENAME


def load_checkpoint(work_dir: str | Path) -> dict[str, object]:
    path = checkpoint_path(work_dir)
    if not path.exists():
        return {
            "completed_phases": [],
            "imported_psts": [],
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
        }

    payload = json.loads(path.read_text(encoding="utf-8"))
    completed = payload.get("completed_phases", [])
    imported = payload.get("imported_psts", [])
    schema_version = int(payload.get("schema_version", CHECKPOINT_SCHEMA_VERSION))

    if not isinstance(completed, list) or not all(
        isinstance(value, str) for value in completed
    ):
        raise RuntimeError(f"Invalid checkpoint completed_phases: {path}")
    if not isinstance(imported, list) or not all(
        isinstance(value, str) for value in imported
    ):
        raise RuntimeError(f"Invalid checkpoint imported_psts: {path}")
    if schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeError(
            "Unsupported rebuild checkpoint schema_version: "
            f"{schema_version} (expected {CHECKPOINT_SCHEMA_VERSION})"
        )

    checkpoint: dict[str, object] = {
        "completed_phases": completed,
        "imported_psts": imported,
        "schema_version": schema_version,
    }
    # Round identity (additive, absent in checkpoints written before #236).
    for key, expected in (
        ("round", dict),
        ("staged_rows", dict),
        ("staged_total", int),
    ):
        if key in payload:
            if not isinstance(payload[key], expected):
                raise RuntimeError(f"Invalid checkpoint {key}: {path}")
            checkpoint[key] = payload[key]
    return checkpoint


def save_checkpoint(work_dir: str | Path, checkpoint: dict[str, object]) -> Path:
    path = checkpoint_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def run_rebuild(
    *,
    phase: str | None = None,
    run_all: bool = True,
    resume: bool = False,
    work_dir: str | Path,
    pst_paths: Sequence[str | Path] | None = None,
    stage_db: str | Path | None = None,
    readpst_path: str | Path | None = None,
    env_file: str | Path | None = None,
    maildir_root: str | Path = DEFAULT_MAILDIR_ROOT,
    log_dir: str | Path = DEFAULT_LOG_DIR,
) -> int:
    raw_work_dir_str = str(work_dir)
    if _is_forbidden_mnt_path(Path(raw_work_dir_str)):
        print(
            f"preflight: work dir under /mnt is forbidden: {raw_work_dir_str}",
            file=sys.stderr,
        )
        return 1

    resolved_work_dir = Path(work_dir).expanduser().resolve()
    resolved_stage_db = (
        Path(stage_db).expanduser().resolve()
        if stage_db is not None
        else resolved_work_dir / DEFAULT_STAGE_DB_NAME
    )
    resolved_env_file = (
        Path(env_file if env_file is not None else DEFAULT_ENV_FILE)
        .expanduser()
        .resolve()
    )
    context = RebuildContext(
        work_dir=resolved_work_dir,
        stage_db=resolved_stage_db,
        attachment_root=resolve_store_root(resolved_stage_db),
        pst_paths=tuple(
            Path(path).expanduser().resolve() for path in (pst_paths or [])
        ),
        readpst_path=(
            Path(readpst_path).expanduser().resolve()
            if readpst_path is not None
            else None
        ),
        env_file=resolved_env_file,
        maildir_root=Path(maildir_root).expanduser().resolve(),
        log_dir=Path(log_dir).expanduser().resolve(),
        resume=resume,
    )
    checkpoint = load_checkpoint(context.work_dir)
    identity = _round_identity(context)
    if resume:
        try:
            _verify_resume_claims(context, checkpoint, identity)
        except (RuntimeError, sqlite3.Error) as exc:
            print(f"[rebuild] resume: REFUSED {exc}", file=sys.stderr)
            return 1
    elif phase is None:
        # A fresh full run re-does every phase, so it is a new round: drop the
        # old round's claims instead of letting a later --resume trust them.
        checkpoint = {
            "completed_phases": [],
            "imported_psts": [],
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
        }
    # Stamp the round only on fresh claims; an unverified legacy claim
    # must not inherit this stage DB's identity.
    if not _has_banked_work(checkpoint):
        checkpoint["round"] = identity
    phases = determine_phases(
        phase=phase,
        run_all=run_all,
        resume=resume,
        checkpoint=checkpoint,
    )

    for phase_name in phases:
        print(f"[rebuild] {phase_name}: start")
        handler = _get_phase_handler(phase_name)
        try:
            handler_result = handler(context, checkpoint)
        except Exception as exc:
            print(f"[rebuild] {phase_name}: ERROR {exc}", file=sys.stderr)
            return 1

        completed = handler_result is not False
        if completed:
            _mark_phase_completed(checkpoint, phase_name)
            if context.stage_db.is_file():
                # A high-water mark: a single-phase run against a rolled-back
                # stage DB must not lower what a later --resume compares with.
                checkpoint["staged_total"] = max(
                    int(checkpoint.get("staged_total") or 0),
                    _count_messages(context.stage_db),
                )
        save_checkpoint(context.work_dir, checkpoint)
        print(f"[rebuild] {phase_name}: {'completed' if completed else 'incomplete'}")

        if phase_name == "coverage-report":
            print(
                "next step: prefer restoring a DB backup when one is available; "
                "coverage-report is only the deep fallback estimate."
            )
        elif phase_name == "validate":
            print(
                f"next step: review {context.stage_db} before swapping it into "
                "production."
            )

    return 0


def determine_phases(
    *,
    phase: str | None,
    run_all: bool,
    resume: bool,
    checkpoint: dict[str, object],
) -> list[str]:
    if phase is not None:
        return [phase]
    if not run_all:
        raise ValueError("run_all=False requires a single phase name")
    if not resume:
        return list(PHASE_SEQUENCE)

    completed = {
        value
        for value in checkpoint.get("completed_phases", [])
        if isinstance(value, str)
    }
    for phase_name in PHASE_SEQUENCE:
        if phase_name not in completed:
            remaining = list(PHASE_SEQUENCE[PHASE_SEQUENCE.index(phase_name) :])
            # preflight checks live state -- a running Outlook still holding
            # these PSTs -- so its verdict cannot be banked in a checkpoint the
            # way finished work can. An operator who closed Outlook between
            # runs has to fail here, not hours later inside import.
            if remaining[0] != "preflight":
                remaining.insert(0, "preflight")
            return remaining
    # Everything is banked, but the live preflight verdict never is: a finished
    # round still has to prove its inputs and Outlook are there now.
    return ["preflight"]


def _round_identity(context: RebuildContext) -> dict[str, object]:
    # PST size/mtime are deliberately not part of it: a PST mounted in Outlook
    # is touched by Outlook itself, which would refuse legitimate resumes.
    return {
        "stage_db": str(context.stage_db),
        "pst_inputs": sorted(str(path) for path in context.pst_paths),
    }


def _has_banked_work(checkpoint: dict[str, object]) -> bool:
    completed = checkpoint.get("completed_phases", [])
    return bool(checkpoint.get("imported_psts")) or any(
        value != "preflight" for value in completed
    )


def _verify_resume_claims(
    context: RebuildContext,
    checkpoint: dict[str, object],
    identity: dict[str, object],
) -> None:
    """Refuse a resume whose checkpoint claims work this round cannot show.

    A checkpoint is a claim; the claim is only skipped-over work if it still
    belongs to this stage DB and these PST inputs. Preflight-only checkpoints
    bank nothing and are always adoptable.
    """
    if not _has_banked_work(checkpoint):
        return
    recovery = (
        f"start a new round: delete {checkpoint_path(context.work_dir)} and the "
        f"stage database {context.stage_db} (CAS is idempotent, keep it), then "
        "rerun `bes rebuild` without --resume"
    )
    recorded = checkpoint.get("round")
    if not isinstance(recorded, dict):
        raise RuntimeError(
            "checkpoint claims completed work but records no round identity "
            f"(written before issue #236); {recovery}"
        )
    if recorded != identity:
        raise RuntimeError(
            "checkpoint belongs to a different round "
            f"(recorded stage_db={recorded.get('stage_db')}, "
            f"pst_inputs={recorded.get('pst_inputs')}; "
            f"this run: stage_db={identity['stage_db']}, "
            f"pst_inputs={identity['pst_inputs']}); {recovery}"
        )

    completed = checkpoint.get("completed_phases", [])
    imported = _imported_pst_set(checkpoint)
    if "import" not in completed and not imported:
        return
    if not context.stage_db.is_file():
        raise RuntimeError(
            f"checkpoint records imported work but stage database "
            f"{context.stage_db} does not exist; {recovery}"
        )
    staged_rows = checkpoint.get("staged_rows", {})
    if not isinstance(staged_rows, dict):
        raise RuntimeError(
            "checkpoint claims imported work but records no per-PST row counts "
            f"(written before issue #236); {recovery}"
        )
    for pst_text in sorted(imported):
        expected = staged_rows.get(pst_text)
        if expected is None:
            raise RuntimeError(
                f"checkpoint claims {pst_text} was imported but records no row count "
                f"(written before issue #236); {recovery}"
            )
        actual = _count_staged(context.stage_db, pst_text)
        if actual != expected:
            raise RuntimeError(
                f"stage database {context.stage_db} no longer holds the rows the "
                f"checkpoint accepted for {pst_text} (expected {expected}, "
                f"found {actual}); {recovery}"
            )
    # Per-PST counts cannot see mail from other sources (backfill). Mail is only
    # ever added within a round, so fewer messages than a completed phase
    # recorded means the stage DB was replaced or rolled back.
    recorded_total = checkpoint.get("staged_total")
    if recorded_total is not None:
        total = _count_messages(context.stage_db)
        if total < recorded_total:
            raise RuntimeError(
                f"stage database {context.stage_db} holds fewer messages than "
                f"the checkpoint recorded (expected at least {recorded_total}, "
                f"found {total}); {recovery}"
            )


def run_preflight_phase(
    context: RebuildContext,
    checkpoint: dict[str, object],
) -> bool:
    del checkpoint
    if _is_forbidden_mnt_path(context.work_dir):
        raise RuntimeError(
            f"preflight: work dir under /mnt is forbidden: {context.work_dir}"
        )
    if not context.pst_paths:
        raise RuntimeError("preflight: at least one --pst path is required")

    for pst_path in context.pst_paths:
        if not pst_path.exists() or not pst_path.is_file():
            raise FileNotFoundError(f"preflight: PST source not found: {pst_path}")

    ensure_psts_mounted_in_outlook(context.pst_paths)
    ensure_work_dir_writable(context.work_dir)
    print(f"stage_db: {context.stage_db}")
    print(f"attachment_root: {context.attachment_root}")
    return True


def ensure_psts_mounted_in_outlook(pst_paths: Sequence[str | Path]) -> None:
    """Refuse the rebuild unless every PST is mounted in a running Outlook.

    The import phase reads recipients through the Outlook recipient table
    (issue #151), so the thing worth checking before a multi-hour rebuild is
    whether Outlook is running with those stores mounted -- not whether this
    process can read the PST bytes, which it never does. Resolution goes
    through `OutlookComReader`, so the "GetActiveObject, never Dispatch"
    guarantee holds here too: a closed Outlook fails the phase instead of
    being launched under a profile that need not carry these PSTs.
    """
    reader = OutlookComReader()
    for source in resolve_ingestion_sources(file_inputs=list(pst_paths)):
        reader.resolve_store(source)
        print(f"outlook store mounted: {source.source_path}")


def ensure_work_dir_writable(work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    probe_path = work_dir / ".rebuild-write-probe"
    try:
        probe_path.write_text("ok", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"preflight: work dir is not writable: {work_dir}") from exc
    finally:
        probe_path.unlink(missing_ok=True)


def calculate_coverage_report(
    *,
    work_dir: str | Path,
    pst_paths: Sequence[str | Path],
    today: date | None = None,
    readpst_path: str | Path | None = None,
) -> dict[str, object]:
    coverage_until = scan_pst_coverage_until(
        work_dir=work_dir,
        pst_paths=pst_paths,
        readpst_path=readpst_path,
    )
    if coverage_until is None:
        raise RuntimeError("coverage-report: could not determine PST coverage date")

    effective_today = today or date.today()
    server_window_start = effective_today - timedelta(days=SERVER_WINDOW_DAYS)
    gap_days = max((server_window_start - coverage_until).days, 0)
    return {
        "pst_coverage_until": coverage_until.isoformat(),
        "server_window_start": server_window_start.isoformat(),
        "gap_days": gap_days,
        "primary_dr_hint": "首選為還原 DB 備份",
    }


def run_coverage_report_phase(
    context: RebuildContext,
    checkpoint: dict[str, object],
) -> bool:
    del checkpoint
    _guard_work_dir_not_mnt(context.work_dir)
    try:
        report = calculate_coverage_report(
            work_dir=context.work_dir,
            pst_paths=context.pst_paths,
            readpst_path=context.readpst_path,
        )
    except (RuntimeError, Exception) as exc:
        print(
            f"WARNING: coverage-report: could not produce coverage report ({exc}); "
            "rebuild will continue without gap estimate."
        )
        return True
    print("coverage-report:")
    print(f"  pst_coverage_until: {report['pst_coverage_until']}")
    print(f"  server_window_start: {report['server_window_start']} (estimated)")
    print(f"  gap_days: {report['gap_days']}")
    print(f"  primary_dr_hint: {report['primary_dr_hint']}")
    return True


def scan_pst_coverage_until(
    *,
    work_dir: str | Path,
    pst_paths: Sequence[str | Path],
    readpst_path: str | Path | None = None,
) -> date | None:
    sources = resolve_ingestion_sources(file_inputs=pst_paths)
    coverage_scan_root = Path(work_dir).expanduser().resolve() / ".coverage-scan"
    coverage_scan_root.mkdir(parents=True, exist_ok=True)
    parser = ReadpstParser(
        readpst_path=readpst_path,
        work_root=coverage_scan_root,
    )
    latest: date | None = None
    for source in sources:
        for extraction_result in parser.iter_extraction_results(source):
            if extraction_result.message is None:
                continue
            raw_message = extraction_result.message.raw_message
            candidate_values = (
                raw_message.get("sent_at"),
                raw_message.get("received_at"),
            )
            for candidate in candidate_values:
                if not isinstance(candidate, datetime):
                    continue
                candidate_date = candidate.date()
                if latest is None or candidate_date > latest:
                    latest = candidate_date
    return latest


def run_import_phase(
    context: RebuildContext,
    checkpoint: dict[str, object],
) -> bool:
    _guard_work_dir_not_mnt(context.work_dir)
    initialize_database(context.stage_db)
    imported_psts = _imported_pst_set(checkpoint)

    for pst_path in context.pst_paths:
        pst_text = str(pst_path)
        if context.resume and pst_text in imported_psts:
            print(f"import: skip already-imported PST {pst_path}")
            continue

        _refuse_partially_staged_pst(context.stage_db, pst_text)

        sources = resolve_ingestion_sources(file_inputs=[pst_path])
        summary = import_pst_sources(
            database_path=context.stage_db,
            sources=sources,
            backend="outlook",
            attachment_root=context.attachment_root,
        )
        print(
            f"import: {pst_path.name}: "
            f"{summary.imported_messages} imported, "
            f"{summary.duplicate_messages} duplicates, "
            f"{summary.failure_count} failures"
        )
        if (
            summary.failure_count > 0
            and summary.imported_messages == 0
            and summary.duplicate_messages == 0
        ):
            raise RuntimeError(f"import: PST import failed for {pst_path}")
        # Same verdict `bes import` reaches (issue #151 D6): an account whose
        # recipient extraction did not complete stages silently wrong
        # recipients, and no later phase can tell.
        if not summary.recipient_extraction_complete:
            raise RuntimeError(
                "import: recipient extraction incomplete for "
                f"{pst_path} (summary: {summary.recipient_extraction_summary_path}); "
                "rows already written for this PST are not repaired by a rerun "
                "-- start this PST against a fresh stage database"
            )
        if summary.failure_count > 0:
            print(
                "WARNING: import: "
                f"{pst_path.name}: {summary.failure_count} failures recorded in "
                "ingest_failures table"
            )

        if pst_text not in imported_psts:
            checkpoint.setdefault("imported_psts", []).append(pst_text)
            imported_psts.add(pst_text)
        # Binds the acceptance to this stage DB's actual rows; refreshed on
        # every acceptance so a re-import never leaves a stale count behind.
        checkpoint.setdefault("staged_rows", {})[pst_text] = _count_staged(
            context.stage_db, pst_text
        )
        save_checkpoint(context.work_dir, checkpoint)

    return True


def _count_messages(stage_db: Path) -> int:
    connection = connect_database(stage_db, create=False)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    finally:
        connection.close()


def _count_staged(stage_db: Path, pst_source: str) -> int:
    connection = connect_database(stage_db, create=False)
    try:
        row = connection.execute(
            "SELECT COUNT(*) AS staged FROM messages WHERE pst_source = ?",
            [pst_source],
        ).fetchone()
    finally:
        connection.close()
    return int(row["staged"] if row else 0)


def _refuse_partially_staged_pst(stage_db: Path, pst_source: str) -> None:
    """Stop a rerun that would silently bless a half-imported PST.

    `persist_message` treats an already-stored `message_id` / `dedup_key` as a
    duplicate and returns without updating it, so re-importing a PST whose
    previous attempt died mid-way never repairs the rows that attempt wrote.
    The second attempt's extraction accounting can nonetheless balance -- it
    counts what the extractor saw, not what reached the database -- and the PST
    would then be checkpointed with the first attempt's recipients still in
    place. Rows present for a PST the checkpoint never accepted are exactly
    that state, so refuse rather than repair: purging them here would strand
    `thread_key`, which is a connected-component minimum over ids from every
    PST, not just this one.
    """
    staged = _count_staged(stage_db, pst_source)
    if staged:
        raise RuntimeError(
            f"import: stage database already holds {staged} messages for "
            f"{pst_source}, but the checkpoint never accepted that PST -- a "
            "previous attempt failed part-way and a rerun cannot repair those "
            "rows. Start again against a fresh stage database."
        )


def run_backfill_phase(
    context: RebuildContext,
    checkpoint: dict[str, object],
) -> bool:
    del checkpoint
    _guard_work_dir_not_mnt(context.work_dir)
    isolated_maildir_root = context.work_dir / ".maildir-rebuild"
    isolated_log_dir = context.work_dir / ".sync-logs"
    isolated_maildir_root.mkdir(parents=True, exist_ok=True)
    isolated_log_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_sync_imap(
            database_path=context.stage_db,
            env_file=context.env_file,
            maildir_root=isolated_maildir_root,
            log_dir=isolated_log_dir,
            attachment_root=context.attachment_root,
            full_resync=True,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"WARNING: backfill skipped: {exc}")
        return False

    if result.exit_code != 0:
        message = result.failure_message or "sync failed"
        print(f"WARNING: backfill skipped: {message}")
        return False

    print(
        f"backfill: pulled {result.pulled_messages} new messages from "
        f"{result.folder_count} folders"
    )
    print(
        f"backfill: {result.candidate_messages} candidates -> "
        f"{result.inserted_messages} inserted, "
        f"{result.deduplicated_messages} deduped"
    )
    return True


def run_verify_cas_phase(
    context: RebuildContext,
    checkpoint: dict[str, object],
) -> bool:
    del checkpoint
    _guard_work_dir_not_mnt(context.work_dir)
    result = verify_cas(context.attachment_root)
    print(f"verify-cas: attachment_root={context.attachment_root}")
    print(f"verify-cas: total={result.total}")
    print(f"verify-cas: ok={result.ok}")
    print(f"verify-cas: bad={len(result.bad_files)}")
    print(f"verify-cas: mismatched={result.mismatched}")
    print(f"verify-cas: read_error={result.read_error}")
    if result.bad_files:
        for bad_file in result.bad_files:
            print(f"verify-cas: bad_file={bad_file}")
    refs = verify_cas_references(context.stage_db, context.attachment_root)
    print(f"verify-cas: referenced={refs.referenced}")
    print(f"verify-cas: missing_refs={len(refs.missing)}")
    for ref in refs.missing:
        print(
            f"verify-cas: missing_ref attachment_id={ref.attachment_id} "
            f"db_id={ref.db_id} path={ref.storage_path}"
        )
    if result.mismatched > 0 or result.read_error > 0 or refs.missing:
        raise RuntimeError("verify-cas: attachment CAS verification failed")
    return True


def run_validate_phase(
    context: RebuildContext,
    checkpoint: dict[str, object],
) -> bool:
    del checkpoint
    _guard_work_dir_not_mnt(context.work_dir)
    doctor_result = check_database(context.stage_db)
    if not bool(doctor_result.get("passed")):
        raise RuntimeError("validate: database health check failed")

    connection = connect_database(context.stage_db, create=False)
    try:
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM messages) AS messages_count,
                (SELECT COUNT(*) FROM message_fts) AS fts_count
            """
        ).fetchone()
        messages_count = int(row["messages_count"])
        fts_count = int(row["fts_count"])
        if messages_count != fts_count:
            raise RuntimeError(
                "validate: message_fts row count does not match messages row count "
                f"({fts_count} != {messages_count})"
            )
        if messages_count <= 0:
            raise RuntimeError("validate: rebuilt database contains no messages")

        keyword = _pick_spot_check_keyword(connection)
    finally:
        connection.close()

    if keyword is None:
        raise RuntimeError("validate: could not derive a keyword spot-check")

    results = search_messages(
        database_path=context.stage_db,
        filters=SearchFilters(keyword=keyword),
        limit=1,
    )
    if not results:
        raise RuntimeError(
            f"validate: keyword spot-check returned no results for keyword {keyword!r}"
        )

    failure_connection = connect_database(context.stage_db, create=False)
    try:
        row = failure_connection.execute(
            """
            SELECT COUNT(*) AS failure_count
            FROM ingest_failures
            """
        ).fetchone()
        ingest_failure_count = int(row["failure_count"] if row else 0)
    finally:
        failure_connection.close()

    print(f"validate: messages={messages_count}")
    print(f"validate: message_fts={fts_count}")
    print(f"validate: keyword_spot_check={keyword!r}")
    print(
        "validate: "
        f"{ingest_failure_count} ingest failures recorded in ingest_failures table"
    )
    if ingest_failure_count > 0:
        print(
            "validate: rebuild is not 100% complete — consider re-ingesting "
            "failed messages"
        )
    return True


def _pick_spot_check_keyword(connection: sqlite3.Connection) -> str | None:
    rows = connection.execute(
        """
        SELECT subject, body_text
        FROM messages
        ORDER BY id
        LIMIT 50
        """
    ).fetchall()
    for row in rows:
        for value in (row["subject"], row["body_text"]):
            text = str(value) if value else ""
            ascii_match = _ASCII_TOKEN_RE.search(text)
            if ascii_match:
                return ascii_match.group(0)
            cjk_chars: list[str] = []
            for char in text:
                codepoint = ord(char)
                if (
                    0x3400 <= codepoint <= 0x4DBF
                    or 0x4E00 <= codepoint <= 0x9FFF
                    or 0xF900 <= codepoint <= 0xFAFF
                    or 0x20000 <= codepoint <= 0x2A6DF
                ):
                    cjk_chars.append(char)
                elif cjk_chars:
                    break
            if cjk_chars:
                return "".join(cjk_chars)
    return None


def _get_phase_handler(phase_name: str):
    if phase_name == "preflight":
        return run_preflight_phase
    if phase_name == "coverage-report":
        return run_coverage_report_phase
    if phase_name == "import":
        return run_import_phase
    if phase_name == "backfill":
        return run_backfill_phase
    if phase_name == "verify-cas":
        return run_verify_cas_phase
    if phase_name == "validate":
        return run_validate_phase
    raise ValueError(f"Unknown rebuild phase: {phase_name}")


def _mark_phase_completed(checkpoint: dict[str, object], phase_name: str) -> None:
    completed = checkpoint.setdefault("completed_phases", [])
    if not isinstance(completed, list):
        raise RuntimeError("Invalid checkpoint completed_phases payload")
    if phase_name not in completed:
        completed.append(phase_name)


def _imported_pst_set(checkpoint: dict[str, object]) -> set[str]:
    imported = checkpoint.setdefault("imported_psts", [])
    if not isinstance(imported, list):
        raise RuntimeError("Invalid checkpoint imported_psts payload")
    return {value for value in imported if isinstance(value, str)}


def _is_forbidden_mnt_path(path: Path) -> bool:
    normalized = re.sub(r"/+", "/", str(path).replace("\\", "/"))
    return normalized == "/mnt" or normalized.startswith("/mnt/")


def _guard_work_dir_not_mnt(work_dir: Path) -> None:
    raw_path = Path(str(work_dir))
    for candidate in (raw_path, work_dir.expanduser().resolve()):
        if _is_forbidden_mnt_path(candidate):
            raise RuntimeError(
                f"work dir under /mnt is forbidden (storage safety): {work_dir}"
            )

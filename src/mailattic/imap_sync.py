from __future__ import annotations

import re
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .imap_native import (
    BULK_FETCH_THRESHOLD,
    NativeImapClient,
    resolve_folders,
    skipped_listings,
    status_uidvalidity,
    sync_native,
)
from .imap_runtime import (
    SyncExecutionResult,
    load_env_file,
    validate_sync_environment,
)
from .imap_state import ImapSyncState
from .query import get_sync_status


def _build_legacy_ssl_context() -> ssl.SSLContext:
    # server 的 TLS 設定與預設 SSL context 不相容，故放寬 TLS 檢查
    # （兩項成因見 outbound.build_smtp_ssl_context 的 docstring）。
    # Trust 來自「user 擁有並控制 mail server」，不依賴 PKI。
    # 若日後 server 改用正規設定，可改回 ssl.create_default_context()。
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


DEFAULT_COMPOSE_FILE = Path(r"C:\IT_Projects\mailattic\docker\docker-compose.yml")
DEFAULT_ENV_FILE = Path(r"C:\IT_Projects\mailattic\.env.sean3808")
DEFAULT_MAILDIR_ROOT = Path(r"C:\IT_Projects\mailattic\.mailattic-work\maildir")
DEFAULT_DATABASE_PATH = Path(r"C:\IT_Projects\mailattic\mailattic.db")
DEFAULT_LOG_DIR = Path(r"C:\IT_Projects\mailattic\.mailattic-work\logs")

# Minimum seconds between pre-task sync runs (15 minutes)
PRE_TASK_MIN_INTERVAL_SECONDS = 15 * 60

# Pattern to extract the 'end=' ISO-8601 timestamp from a mbsync log line.
# Log lines written by _append_sync_log look like:
#   start=2026-05-28T10:00:00+00:00 end=2026-05-28T10:01:30+00:00 new=5 deduped=0 exit=0
_LOG_END_RE = re.compile(r"\bend=(\S+)")
# Signed: a subprocess killed by a signal is logged with a negative returncode.
_LOG_EXIT_RE = re.compile(r"\bexit=(-?\d+)")
_LOG_STAGE_RE = re.compile(r"\bstage=(\S+)")
_LOG_MESSAGE_RE = re.compile(r"\bmessage=(.+)")


@dataclass(frozen=True, slots=True)
class LastSyncRecord:
    end_at: datetime
    exit_code: int = 0
    failure_stage: str | None = None
    failure_message: str | None = None

    @property
    def failed(self) -> bool:
        return self.exit_code != 0


@dataclass(frozen=True, slots=True)
class DryRunSummary:
    estimated_new_messages: int
    folder_count: int
    bulk_guard_folders: list[tuple[str, int]] = field(default_factory=list)
    skipped_folders: list[str] = field(default_factory=list)


def estimate_sync_run(
    *,
    env_file: str | Path = DEFAULT_ENV_FILE,
    maildir_root: str | Path = DEFAULT_MAILDIR_ROOT,
    folder: str | None = None,
    client_factory=None,
    database_path: str | Path | None = None,
) -> DryRunSummary:
    env = load_env_file(env_file)
    validate_sync_environment(env)
    account_name = env["IMAP_ACCOUNT_NAME"]
    maildir_path = Path(maildir_root).expanduser().resolve()
    state_path = maildir_path / account_name / ".imap_watermarks.json"
    state = ImapSyncState(state_path)
    factory = client_factory or (lambda e: NativeImapClient(e))
    client = factory(env)
    try:
        folders_to_check = resolve_folders(client=client, folder=folder)
        skipped_folders = skipped_listings(client, folder)
        estimated_new_messages = 0
        bulk_guard_folders: list[tuple[str, int]] = []
        for imap_folder in folders_to_check:
            uidvalidity = status_uidvalidity(client, imap_folder)
            if database_path is None:
                watermark = state.get_watermark(
                    imap_folder.wire_name, imap_folder.delimiter, uidvalidity
                )
            else:
                watermark = state.effective_watermark(
                    Path(database_path).expanduser().resolve(),
                    account_name,
                    env["IMAP_USER"],
                    imap_folder.wire_name,
                    imap_folder.delimiter,
                    uidvalidity,
                )
            client.select_folder(imap_folder.wire_name)
            uids = client.search_uids_after(watermark)
            count = len(uids)
            estimated_new_messages += count
            if watermark is None and count > BULK_FETCH_THRESHOLD:
                bulk_guard_folders.append((imap_folder.wire_name, count))
        return DryRunSummary(
            estimated_new_messages=estimated_new_messages,
            folder_count=len(folders_to_check),
            bulk_guard_folders=bulk_guard_folders,
            skipped_folders=skipped_folders,
        )
    finally:
        client.close()


def run_sync_imap(
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    compose_file: str | Path = DEFAULT_COMPOSE_FILE,  # kept for backward compat
    env_file: str | Path = DEFAULT_ENV_FILE,
    maildir_root: str | Path = DEFAULT_MAILDIR_ROOT,
    log_dir: str | Path = DEFAULT_LOG_DIR,
    attachment_root: Path | None = None,
    folder: str | None = None,
    full_resync: bool = False,
    project_root: str | Path | None = None,  # kept for backward compat
    subprocess_runner=None,  # kept for backward compat
) -> SyncExecutionResult:
    env = load_env_file(env_file)
    validate_sync_environment(env)
    account_name = env["IMAP_ACCOUNT_NAME"]
    state_path = (
        Path(maildir_root).expanduser().resolve()
        / account_name
        / ".imap_watermarks.json"
    )
    if full_resync:
        ImapSyncState(state_path).clear_all()
    return sync_native(
        env=env,
        database_path=Path(database_path).expanduser().resolve(),
        maildir_root=Path(maildir_root).expanduser().resolve(),
        state_path=state_path,
        log_dir=Path(log_dir).expanduser().resolve(),
        attachment_root=attachment_root,
        folder=folder,
        confirm_fn=(lambda _: True) if full_resync else None,
    )


# ---------------------------------------------------------------------------
# Pre-task sync: last-completed-sync timestamp + 15-min interval gate
# ---------------------------------------------------------------------------


def read_last_sync_record(
    log_dir: str | Path = DEFAULT_LOG_DIR,
) -> LastSyncRecord | None:
    """Return the record of the most recently completed sync run.

    Scans all ``mbsync-*.log`` files under *log_dir* and returns the latest
    sync record by ``end=<ISO-8601>`` timestamp across all log lines.
    Extracts ``exit``, ``stage``, and ``message`` fields when present.

    Returns ``None`` when no log files exist or no valid timestamp is found.
    """
    log_directory = Path(log_dir).expanduser().resolve()
    if not log_directory.exists():
        return None

    latest_record: LastSyncRecord | None = None
    for log_file in sorted(log_directory.glob("mbsync-*.log")):
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _LOG_END_RE.search(line)
            if not match:
                continue
            try:
                ts = datetime.fromisoformat(match.group(1))
            except ValueError:
                continue
            # Ensure the datetime is timezone-aware (UTC)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if latest_record is None or ts >= latest_record.end_at:
                exit_match = _LOG_EXIT_RE.search(line)
                exit_code = int(exit_match.group(1)) if exit_match else 0
                stage_match = _LOG_STAGE_RE.search(line)
                stage = stage_match.group(1) if stage_match else None
                msg_match = _LOG_MESSAGE_RE.search(line)
                msg = msg_match.group(1) if msg_match else None
                latest_record = LastSyncRecord(
                    end_at=ts,
                    exit_code=exit_code,
                    failure_stage=stage,
                    failure_message=msg,
                )

    return latest_record


def read_last_sync_time(
    log_dir: str | Path = DEFAULT_LOG_DIR,
) -> datetime | None:
    """Return the end timestamp of the most recently completed sync run.

    Scans all ``mbsync-*.log`` files under *log_dir* and returns the latest
    ``end=<ISO-8601>`` timestamp found across all log lines (regardless of
    exit code — the end timestamp is written unconditionally by
    ``_append_sync_log`` in imap_runtime.py).

    Returns ``None`` when no log files exist or no valid timestamp is found.

    Timestamp source rationale: ``_append_sync_log`` (imap_runtime.py) writes
    ``end=<UTC ISO-8601>`` for every sync run, including failures.  The field
    is written atomically after ``run_logged_sync_job`` completes, making it
    the most durable and accurate record of "last attempted/completed sync
    end time" available without adding a new state file.  If a stricter
    "last *successful* sync" definition is needed in future, add
    ``exit=0`` filtering here.
    """
    record = read_last_sync_record(log_dir)
    return record.end_at if record is not None else None


def _unfinished_run_after(
    database_path: str | Path, logged_end: datetime
) -> tuple[str, str] | None:
    """``(stage, message)`` when the DB says a run after *logged_end* may not
    have completed, else ``None``.

    A run that dies before writing its log line (e.g. the IMAP connection
    cannot be opened) still opened the ``sync_state`` row first -- that row
    exists to close exactly this blind spot -- so a row started after the
    newest log record and not ``completed`` means that run failed or is still
    running (stage ``unfinished``). A missing DB, or one predating
    ``sync_state``, is no evidence either way; any other read error is
    reported (stage ``state_unreadable``) rather than passed off as a clean
    skip, because this check exists to stop over-claiming freshness.
    """
    try:
        status = get_sync_status(database_path, read_only=True)
        started = status["last_sync_started_at"]
        if not isinstance(started, str) or status["last_sync_status"] == "completed":
            return None
        started_at = datetime.fromisoformat(started)
    except FileNotFoundError:
        return None
    except Exception as exc:
        return (
            "state_unreadable",
            f"sync state could not be read: {type(exc).__name__}: {exc}",
        )
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if started_at <= logged_end:
        return None
    return (
        "unfinished",
        f"sync started at {started} has no recorded completion "
        f"(status: {status['last_sync_status']})",
    )


def pre_task_sync_imap(
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    compose_file: str | Path = DEFAULT_COMPOSE_FILE,
    env_file: str | Path = DEFAULT_ENV_FILE,
    maildir_root: str | Path = DEFAULT_MAILDIR_ROOT,
    log_dir: str | Path = DEFAULT_LOG_DIR,
    attachment_root: Path | None = None,
    folder: str | None = None,
    project_root: str | Path | None = None,
    subprocess_runner=None,
    min_interval_seconds: int = PRE_TASK_MIN_INTERVAL_SECONDS,
    now: datetime | None = None,
    last_sync: datetime | LastSyncRecord | None = None,
    last_sync_exit_code: int | None = None,
    last_sync_failure_stage: str | None = None,
    last_sync_failure_message: str | None = None,
) -> dict[str, object]:
    """Run sync-imap with a 15-minute minimum-interval guard.

    If the last completed sync finished less than *min_interval_seconds* ago,
    skip the sync and return a skipped status dict.  Otherwise, run the normal
    sync and return the result.

    The *now*, *last_sync*, and *last_sync_exit_code* parameters are injectable
    for testing so tests do not depend on the wall clock or real log files.
    An injected *last_sync* stands in for all recorded state: the DB
    ``sync_state`` row is consulted only when the record comes from the logs.

    Returns a dict with at least:
      ``status``        - ``"skipped"`` or ``"synced"``
      ``last_sync_at``  - ISO-8601 string of last-sync end time (or None)
    When ``status == "skipped"`` and the last sync failed (non-zero exit),
    ``last_sync_failed=True`` and ``last_sync_exit_code`` are also included.
    When ``status == "synced"``, all :class:`SyncExecutionResult` fields are
    also included.
    """
    effective_now = now if now is not None else datetime.now(tz=UTC)
    effective_last_sync: datetime | None = None

    if isinstance(last_sync, LastSyncRecord):
        record: LastSyncRecord | None = last_sync
    elif isinstance(last_sync, datetime):
        record = LastSyncRecord(
            end_at=last_sync,
            exit_code=0 if last_sync_exit_code is None else last_sync_exit_code,
            failure_stage=last_sync_failure_stage,
            failure_message=last_sync_failure_message,
        )
    else:
        record = read_last_sync_record(log_dir)

    if record is not None:
        effective_last_sync = record.end_at
        if effective_last_sync.tzinfo is None:
            effective_last_sync = effective_last_sync.replace(tzinfo=UTC)
        elapsed = (effective_now - effective_last_sync).total_seconds()
        if elapsed < min_interval_seconds:
            # Fixed shape: every key always present, null when not applicable
            # (same convention as mail_sync_status), so callers never branch
            # on key presence.
            result_payload: dict[str, object] = {
                "status": "skipped",
                "last_sync_at": effective_last_sync.isoformat(),
                "elapsed_seconds": elapsed,
                "last_sync_failed": record.failed,
                "last_sync_exit_code": record.exit_code,
                "last_sync_failure_stage": record.failure_stage,
                "last_sync_failure_message": record.failure_message,
            }
            if not record.failed and last_sync is None:
                later = _unfinished_run_after(database_path, effective_last_sync)
                if later is not None:
                    result_payload["last_sync_failed"] = True
                    result_payload["last_sync_exit_code"] = None
                    result_payload["last_sync_failure_stage"] = later[0]
                    result_payload["last_sync_failure_message"] = later[1]
            return result_payload

    result = run_sync_imap(
        database_path=database_path,
        compose_file=compose_file,
        env_file=env_file,
        maildir_root=maildir_root,
        log_dir=log_dir,
        attachment_root=attachment_root,
        folder=folder,
        project_root=project_root,
        subprocess_runner=subprocess_runner,
    )
    last_sync_at = (
        effective_last_sync.isoformat() if effective_last_sync is not None else None
    )
    return {
        "status": "synced",
        "last_sync_at": last_sync_at,
        **result.to_dict(),
    }

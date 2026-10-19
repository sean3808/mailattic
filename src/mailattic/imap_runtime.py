from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

from .ingest import import_maildir_sources, resolve_maildir_sources
from .storage import initialize_database

_LOG_FILENAME_TZ = ZoneInfo("Asia/Taipei")

REQUIRED_SYNC_ENV = (
    "IMAP_HOST",
    "IMAP_PORT",
    "IMAP_USER",
    "IMAP_PASSWORD",
    "IMAP_ACCOUNT_NAME",
)

DEFAULT_IMAP_SECURITY = "tls"
SUPPORTED_IMAP_SECURITY = (DEFAULT_IMAP_SECURITY, "plain")


@dataclass(frozen=True, slots=True)
class SyncExecutionResult:
    pulled_messages: int
    folder_count: int
    candidate_messages: int
    inserted_messages: int
    deduplicated_messages: int
    failure_count: int
    exit_code: int
    failure_stage: str | None = None
    failure_message: str | None = None
    # #255: start-of-sync attachment retry; informational, never part of exit_code.
    attachments_recovered: int = 0
    attachments_still_failing: int = 0
    attachments_source_missing: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "pulled_messages": self.pulled_messages,
            "folder_count": self.folder_count,
            "candidate_messages": self.candidate_messages,
            "inserted_messages": self.inserted_messages,
            "deduplicated_messages": self.deduplicated_messages,
            "failure_count": self.failure_count,
            "exit_code": self.exit_code,
            "failure_stage": self.failure_stage,
            "failure_message": self.failure_message,
            "attachments_recovered": self.attachments_recovered,
            "attachments_still_failing": self.attachments_still_failing,
            "attachments_source_missing": self.attachments_source_missing,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SyncExecutionResult:
        return cls(
            pulled_messages=int(payload.get("pulled_messages", 0)),
            folder_count=int(payload.get("folder_count", 0)),
            candidate_messages=int(payload.get("candidate_messages", 0)),
            inserted_messages=int(payload.get("inserted_messages", 0)),
            deduplicated_messages=int(payload.get("deduplicated_messages", 0)),
            failure_count=int(payload.get("failure_count", 0)),
            exit_code=int(payload.get("exit_code", 1)),
            failure_stage=_coerce_optional_text(payload.get("failure_stage")),
            failure_message=_coerce_optional_text(payload.get("failure_message")),
            attachments_recovered=int(payload.get("attachments_recovered", 0)),
            attachments_still_failing=int(payload.get("attachments_still_failing", 0)),
            attachments_source_missing=int(
                payload.get("attachments_source_missing", 0)
            ),
        )


def load_env_file(env_file: str | Path) -> dict[str, str]:
    path = Path(env_file).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Environment file was not found: {path}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        normalized_key = key.strip()
        normalized_value = value.strip().strip("'\"")
        values[normalized_key] = normalized_value
    return values


def validate_sync_environment(env: Mapping[str, str]) -> None:
    missing = [name for name in REQUIRED_SYNC_ENV if not env.get(name)]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"Missing required environment variables: {missing_text}")
    resolve_imap_security(env)


def resolve_imap_security(env: Mapping[str, str]) -> str:
    """Return the selected IMAP transport, defaulting to implicit TLS."""
    value = env.get("IMAP_SECURITY", DEFAULT_IMAP_SECURITY).strip().lower()
    if value not in SUPPORTED_IMAP_SECURITY:
        supported = ", ".join(SUPPORTED_IMAP_SECURITY)
        raise ValueError(f"IMAP_SECURITY must be one of: {supported}; got {value!r}")
    return value


def render_mbsyncrc(
    template_text: str,
    *,
    env: Mapping[str, str],
    maildir_root: str | Path,
    folder: str | None = None,
) -> str:
    validate_sync_environment(env)
    substitutions = dict(env)
    substitutions["MAILDIR_ROOT"] = Path(maildir_root).as_posix()
    substitutions["MBSYNC_PATTERNS_LINE"] = (
        f'Patterns "{folder}"' if folder else "Patterns *"
    )
    return Template(template_text).substitute(substitutions)


def remove_sync_state_files(maildir_root: str | Path, account_name: str) -> int:
    account_root = Path(maildir_root).expanduser().resolve() / account_name
    if not account_root.exists():
        return 0

    removed = 0
    for candidate in account_root.rglob(".mbsyncstate*"):
        if candidate.is_file():
            candidate.unlink()
            removed += 1
    return removed


def run_logged_sync_job(
    *,
    database_path: str | Path,
    maildir_root: str | Path,
    template_path: str | Path,
    config_path: str | Path,
    log_dir: str | Path,
    attachment_root: Path | None = None,
    folder: str | None = None,
    full_resync: bool = False,
    env: Mapping[str, str] | None = None,
    command_runner=subprocess.run,
) -> SyncExecutionResult:
    start_at = datetime.now(UTC)
    try:
        result = run_sync_job(
            database_path=database_path,
            maildir_root=maildir_root,
            template_path=template_path,
            config_path=config_path,
            attachment_root=attachment_root,
            folder=folder,
            full_resync=full_resync,
            env=env,
            command_runner=command_runner,
        )
    except Exception as exc:
        result = SyncExecutionResult(
            pulled_messages=0,
            folder_count=0,
            candidate_messages=0,
            inserted_messages=0,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=1,
            failure_stage="runtime",
            failure_message=str(exc),
        )
    end_at = datetime.now(UTC)
    _append_sync_log(log_dir=log_dir, start_at=start_at, end_at=end_at, result=result)
    return result


def run_sync_job(
    *,
    database_path: str | Path,
    maildir_root: str | Path,
    template_path: str | Path,
    config_path: str | Path,
    attachment_root: Path | None = None,
    folder: str | None = None,
    full_resync: bool = False,
    env: Mapping[str, str] | None = None,
    command_runner=subprocess.run,
) -> SyncExecutionResult:
    runtime_env = dict(os.environ if env is None else env)
    validate_sync_environment(runtime_env)

    account_email = runtime_env["IMAP_USER"]
    account_name = runtime_env["IMAP_ACCOUNT_NAME"]
    template = Path(template_path).read_text(encoding="utf-8")
    rendered_config = render_mbsyncrc(
        template,
        env=runtime_env,
        maildir_root=maildir_root,
        folder=folder,
    )

    config_file = Path(config_path)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(rendered_config, encoding="utf-8")

    if full_resync:
        remove_sync_state_files(maildir_root, account_name)

    before_count = count_maildir_messages(
        maildir_root=maildir_root,
        account_name=account_name,
        folder=folder,
    )
    command = ["mbsync", "-c", str(config_file), "mailattic-sync"]
    process = command_runner(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.returncode != 0:
        failure_output = (process.stderr or process.stdout).strip()
        return SyncExecutionResult(
            pulled_messages=0,
            folder_count=1 if folder else 0,
            candidate_messages=0,
            inserted_messages=0,
            deduplicated_messages=0,
            failure_count=0,
            exit_code=process.returncode or 1,
            failure_stage=classify_sync_failure(failure_output),
            failure_message=failure_output or "mbsync exited non-zero",
        )

    initialize_database(database_path)
    sources = resolve_maildir_sources(
        maildir_root=maildir_root,
        account_email=account_email,
        account_name=account_name,
        folder=folder,
    )
    import_summary = import_maildir_sources(
        database_path=database_path,
        sources=sources,
        attachment_root=attachment_root,
    )
    after_count = count_maildir_messages(
        maildir_root=maildir_root,
        account_name=account_name,
        folder=folder,
    )
    candidate_messages = (
        import_summary.imported_messages
        + import_summary.duplicate_messages
        + import_summary.failure_count
    )
    folder_count = len(sources) if sources else (1 if folder else 0)
    return SyncExecutionResult(
        pulled_messages=max(after_count - before_count, 0),
        folder_count=folder_count,
        candidate_messages=candidate_messages,
        inserted_messages=import_summary.imported_messages,
        deduplicated_messages=import_summary.duplicate_messages,
        failure_count=import_summary.failure_count,
        exit_code=0,
    )


def count_maildir_messages(
    *,
    maildir_root: str | Path,
    account_name: str,
    folder: str | None = None,
) -> int:
    account_root = Path(maildir_root).expanduser().resolve() / account_name
    if folder is not None:
        folders = [account_root / folder]
    elif account_root.exists():
        folders = [
            path
            for path in account_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ]
    else:
        folders = []

    total = 0
    for folder_root in folders:
        for bucket_name in ("cur", "new"):
            bucket_root = folder_root / bucket_name
            if not bucket_root.exists():
                continue
            total += sum(1 for path in bucket_root.rglob("*") if path.is_file())
    return total


def classify_sync_failure(message: str) -> str:
    lowered = message.lower()
    if any(marker in lowered for marker in ("auth", "login", "authenticate")):
        return "auth"
    if any(
        marker in lowered
        for marker in (
            "no space left on device",
            "disk full",
            "permission denied",
            "read-only file system",
        )
    ):
        return "maildir"
    return "connection"


def _append_sync_log(
    *,
    log_dir: str | Path,
    start_at: datetime,
    end_at: datetime,
    result: SyncExecutionResult,
) -> None:
    directory = Path(log_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    log_path = (
        directory
        / f"mbsync-{start_at.astimezone(_LOG_FILENAME_TZ).date().isoformat()}.log"
    )
    fields = [
        f"start={start_at.isoformat()}",
        f"end={end_at.isoformat()}",
        f"new={result.inserted_messages}",
        f"deduped={result.deduplicated_messages}",
        f"exit={result.exit_code}",
    ]
    if result.failure_stage:
        fields.append(f"stage={result.failure_stage}")
    if result.failure_message:
        fields.append(f"message={result.failure_message}")
    log_path.open("a", encoding="utf-8").write(" ".join(fields) + "\n")


def _coerce_optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

"""Drive one Claude stream-json probe invocation through fail-closed stages.

This is a refusal boundary, not a convenience runner: one invocation carries
one canary turn and one target turn. There is no retry, batching, or fallback
for an output record whose shape is not recognized.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, TextIO

if __package__:
    from scripts import build_probe_fixture_db as fixture_builder
else:
    import build_probe_fixture_db as fixture_builder

CANARY_FILENAME = fixture_builder.CANARY_FILENAME
DATABASE_FILENAME = fixture_builder.DATABASE_FILENAME
MANIFEST_FILENAME = fixture_builder.MANIFEST_FILENAME
MANIFEST_SCHEMA_VERSION = fixture_builder.MANIFEST_SCHEMA_VERSION

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANARY_PATTERN = re.compile(r"^PROBE-CANARY-[A-Za-z0-9_-]{32}$")
PROBE_SYSTEM_PROMPT = (
    "You are running a read-only MCP probe against the configured synthetic "
    "fixture. Use only the configured probe MCP server for data. Do not seek "
    "or use any other data source."
)
PROBE_READ_ONLY_TOOLS = (
    "mail_search",
    "mail_show",
    "mail_thread",
    "mail_contact_history",
    "mail_attachments",
)


class LaunchConfigurationError(ValueError):
    """The requested client launch cannot satisfy the isolation contract."""


@dataclass(frozen=True, slots=True)
class PathPolicy:
    repository_root: Path
    external_root: Path | None = None
    outcome_root: Path | None = None

    @classmethod
    def production(cls, repository_root: Path = REPOSITORY_ROOT) -> PathPolicy:
        root = repository_root.resolve(strict=True)
        return cls(repository_root=root, outcome_root=root / ".dev" / "probe-trials")

    @classmethod
    def for_test(
        cls,
        *,
        repository_root: Path,
        external_root: Path,
    ) -> PathPolicy:
        external = external_root.resolve(strict=True)
        return cls(
            repository_root=repository_root.resolve(strict=True),
            external_root=external,
            outcome_root=external / "outcome-probe-records",
        )

    def permits_external_artifact(self, path: Path) -> bool:
        if self.external_root is not None:
            return path.is_relative_to(self.external_root)
        return not path.is_relative_to(self.repository_root)


@dataclass(frozen=True, slots=True)
class FixtureIdentity:
    database: Path
    canary_file: Path
    manifest: Path
    canary: str


def _is_reparse(path: Path) -> bool:
    attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_attribute)


def _require_regular_non_reparse(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise LaunchConfigurationError(f"{label} must be an absolute path: {path}")
    if not path.is_file() or _is_reparse(path):
        raise LaunchConfigurationError(
            f"{label} must be an existing regular non-reparse file: {path}"
        )
    return path.resolve(strict=True)


def _remove_client_working_directory(
    working_directory: Path,
    *,
    expected_parent: Path,
) -> bool:
    if not working_directory.exists():
        return True
    if (
        working_directory.parent != expected_parent
        or not working_directory.name.startswith(".probe-client-")
    ):
        raise RuntimeError(
            f"Refusing to clean an unexpected launch cwd: {working_directory}"
        )
    for attempt in range(5):
        try:
            shutil.rmtree(working_directory)
            return True
        except PermissionError:
            if attempt == 4:
                return False
            time.sleep(0.05 * (attempt + 1))
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_fixture_identity(
    canary_file: Path,
    path_policy: PathPolicy,
) -> FixtureIdentity:
    if not canary_file.is_absolute():
        raise LaunchConfigurationError("Canary file must be an absolute path.")
    canary_candidate = Path(os.path.abspath(canary_file))
    if canary_candidate.name != CANARY_FILENAME:
        raise LaunchConfigurationError(
            f"Canary file must be named {CANARY_FILENAME}: {canary_candidate}"
        )
    database_candidate = canary_candidate.parent / DATABASE_FILENAME
    manifest_candidate = canary_candidate.parent / MANIFEST_FILENAME

    for sidecar_suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{database_candidate}{sidecar_suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            raise LaunchConfigurationError(
                f"Fixture database has an unmanifested SQLite sidecar: {sidecar}"
            )

    canary_path = _require_regular_non_reparse(canary_candidate, label="Canary file")
    database_path = _require_regular_non_reparse(
        database_candidate, label="Fixture database"
    )
    manifest_path = _require_regular_non_reparse(
        manifest_candidate, label="Fixture manifest"
    )
    for artifact in (canary_path, database_path, manifest_path):
        if not path_policy.permits_external_artifact(artifact):
            raise LaunchConfigurationError(
                f"Production probe artifacts must be outside the repository: {artifact}"
            )

    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LaunchConfigurationError(
            "Fixture manifest is not valid UTF-8 JSON."
        ) from error
    expected_manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifacts": {
            "database": {
                "filename": DATABASE_FILENAME,
                "sha256": _sha256(database_path),
            },
            "canary": {
                "filename": CANARY_FILENAME,
                "sha256": _sha256(canary_path),
            },
        },
    }
    if (
        not isinstance(manifest_value, dict)
        or type(manifest_value.get("schema_version")) is not int
        or manifest_value != expected_manifest
    ):
        raise LaunchConfigurationError(
            "Fixture manifest schema or artifact digest mismatch."
        )

    try:
        canary = canary_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise LaunchConfigurationError(
            "Canary file is not readable UTF-8 text."
        ) from error
    if not CANARY_PATTERN.fullmatch(canary):
        raise LaunchConfigurationError(
            "Canary does not match the generated fixture shape."
        )

    try:
        with sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro", uri=True
        ) as connection:
            match = connection.execute(
                "SELECT 1 FROM messages WHERE subject = ? LIMIT 1", [canary]
            ).fetchone()
    except sqlite3.Error as error:
        raise LaunchConfigurationError(
            "Fixture database could not be opened read-only."
        ) from error
    if match is None:
        raise LaunchConfigurationError(
            "Fixture database does not contain the exact generated canary subject."
        )
    return FixtureIdentity(
        database=database_path,
        canary_file=canary_path,
        manifest=manifest_path,
        canary=canary,
    )


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    command: tuple[str, ...]
    client_environment: Mapping[str, str]
    client_working_directory: Path
    probe_config: Path
    fixture_database: Path
    fixture_manifest: Path
    raw_record: Path
    outcome_row: Path
    canary: str


_SAFE_RECORD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.jsonl$")
_PLACEHOLDER = re.compile(r"(?:__[A-Z0-9_]+__|\$\{[^}]+\}|<[^>]+>)")
_PROHIBITED_ENVIRONMENT_KEYS = {
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
    "__PYVENV_LAUNCHER__",
}


def _is_prohibited_environment_key(key: str) -> bool:
    normalized = key.upper()
    return (
        normalized.startswith("PYTHON")
        or normalized.startswith("UV_")
        or normalized in _PROHIBITED_ENVIRONMENT_KEYS
    )


def _sanitized_environment(parent_environment: Mapping[str, str]) -> Mapping[str, str]:
    sanitized = {
        key: value
        for key, value in parent_environment.items()
        if not _is_prohibited_environment_key(key)
    }
    if any(_is_prohibited_environment_key(key) for key in sanitized):
        raise LaunchConfigurationError(
            "Sanitized client environment retains a prohibited Python or uv override."
        )
    return MappingProxyType(sanitized)


def _walk_existing_components(
    trusted_parent: Path, target: Path, *, label: str
) -> None:
    try:
        relative = target.relative_to(trusted_parent)
    except ValueError as error:
        raise LaunchConfigurationError(
            f"{label} escapes its trusted parent."
        ) from error
    current = trusted_parent
    candidates = [current]
    for part in relative.parts:
        current /= part
        candidates.append(current)
    for candidate in candidates:
        if (candidate.exists() or candidate.is_symlink()) and _is_reparse(candidate):
            raise LaunchConfigurationError(
                f"{label} contains a symlink, junction, or reparse point: {candidate}"
            )


def _validate_record_target(
    target: Path,
    *,
    canonical_root: Path,
    trusted_parent: Path,
    label: str,
    must_be_absent: bool,
) -> Path:
    if not target.is_absolute():
        raise LaunchConfigurationError(f"{label} must be an absolute path.")
    lexical_target = Path(os.path.abspath(target))
    lexical_root = Path(os.path.abspath(canonical_root))
    lexical_parent = Path(os.path.abspath(trusted_parent))
    if lexical_target.parent != lexical_root:
        raise LaunchConfigurationError(
            f"{label} must be directly under {lexical_root}."
        )
    if not _SAFE_RECORD_NAME.fullmatch(lexical_target.name):
        raise LaunchConfigurationError(f"{label} must have a safe .jsonl filename.")
    if not lexical_root.is_dir():
        raise LaunchConfigurationError(
            f"Canonical record root is missing: {lexical_root}"
        )
    _walk_existing_components(lexical_parent, lexical_root, label=label)
    _walk_existing_components(lexical_root, lexical_target, label=label)
    root_resolved = lexical_root.resolve(strict=True)
    parent_resolved = lexical_parent.resolve(strict=True)
    if not root_resolved.is_relative_to(parent_resolved):
        raise LaunchConfigurationError(
            "Canonical record root escapes its trusted parent."
        )
    if lexical_target.exists() or lexical_target.is_symlink():
        if must_be_absent:
            raise LaunchConfigurationError(f"{label} already exists: {lexical_target}")
        if not lexical_target.is_file() or _is_reparse(lexical_target):
            raise LaunchConfigurationError(
                f"{label} must be a regular non-reparse file when present."
            )
        if lexical_target.stat().st_nlink > 1:
            raise LaunchConfigurationError(
                f"{label} must not be a filesystem hard link."
            )
        resolved_target = lexical_target.resolve(strict=True)
    else:
        resolved_target = root_resolved / lexical_target.name
    if not resolved_target.is_relative_to(root_resolved):
        raise LaunchConfigurationError(f"{label} escapes its canonical root.")
    return resolved_target


def _load_filled_config(
    probe_config: Path,
    *,
    probe_server: str,
    fixture: FixtureIdentity,
    path_policy: PathPolicy,
) -> tuple[Path, Path]:
    if not probe_config.is_absolute():
        raise LaunchConfigurationError(
            "Filled probe configuration must be an absolute path."
        )
    config_candidate = Path(os.path.abspath(probe_config))
    config_path = _require_regular_non_reparse(
        config_candidate, label="Filled probe configuration"
    )
    if not path_policy.permits_external_artifact(config_path):
        raise LaunchConfigurationError(
            f"Production probe artifacts must be outside the repository: {config_path}"
        )
    try:
        content = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LaunchConfigurationError(
            "Filled probe configuration is not readable UTF-8 text."
        ) from error
    if _PLACEHOLDER.search(content):
        raise LaunchConfigurationError(
            "Filled probe configuration contains a placeholder."
        )
    try:
        config = json.loads(content)
    except json.JSONDecodeError as error:
        raise LaunchConfigurationError(
            "Filled probe configuration is malformed JSON."
        ) from error
    if not isinstance(config, dict) or set(config) - {"_comment", "mcpServers"}:
        raise LaunchConfigurationError(
            "Filled probe configuration has unexpected root fields."
        )
    if "_comment" in config and not isinstance(config["_comment"], str):
        raise LaunchConfigurationError("Configuration _comment must be text.")
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != {probe_server}:
        raise LaunchConfigurationError(
            "Configuration must contain only the named probe server."
        )
    server = servers[probe_server]
    if not isinstance(server, dict) or set(server) != {"command", "args", "env"}:
        raise LaunchConfigurationError(
            "Probe server has unexpected or missing launch fields."
        )

    command_value = server["command"]
    if not isinstance(command_value, str) or not Path(command_value).is_absolute():
        raise LaunchConfigurationError(
            "Probe server uv command must be an absolute path."
        )
    uv_executable = _require_regular_non_reparse(
        Path(command_value), label="Probe server uv executable"
    )
    if uv_executable.name.lower() != "uv.exe":
        raise LaunchConfigurationError("Probe server command must name uv.exe.")

    args = server["args"]
    if (
        not isinstance(args, list)
        or len(args) != 6
        or args[:2] != ["run", "--project"]
        or args[3:] != ["python", "-m", "mailattic.mcp_server"]
        or not isinstance(args[2], str)
        or not Path(args[2]).is_absolute()
    ):
        raise LaunchConfigurationError(
            "Probe server arguments do not match the exact launch shape."
        )
    try:
        checkout = Path(args[2]).resolve(strict=True)
    except OSError as error:
        raise LaunchConfigurationError(
            "Configured project checkout does not exist."
        ) from error
    if checkout != path_policy.repository_root:
        raise LaunchConfigurationError(
            "Configured project checkout is not the supervisor checkout."
        )

    environment = server["env"]
    if not isinstance(environment, dict) or set(environment) != {
        "MAILATTIC_DB",
        "PYTHONIOENCODING",
    }:
        raise LaunchConfigurationError(
            "Probe server environment has missing or extra bindings."
        )
    database_value = environment["MAILATTIC_DB"]
    if (
        not isinstance(database_value, str)
        or not database_value.strip()
        or not Path(database_value).is_absolute()
    ):
        raise LaunchConfigurationError(
            "MAILATTIC_DB must be a non-empty absolute path."
        )
    try:
        configured_database = Path(database_value).resolve(strict=True)
    except OSError as error:
        raise LaunchConfigurationError(
            "Configured fixture database does not exist."
        ) from error
    if configured_database != fixture.database:
        raise LaunchConfigurationError(
            "MAILATTIC_DB does not name the generated fixture."
        )
    if environment["PYTHONIOENCODING"] != "utf-8":
        raise LaunchConfigurationError("PYTHONIOENCODING must be exactly utf-8.")
    return config_path, uv_executable


def prepare_launch(
    *,
    client_executable: str,
    probe_config: Path,
    probe_server: str,
    canary_file: Path,
    raw_record: Path,
    outcome_row: Path,
    model: str | None,
    path_policy: PathPolicy,
    parent_environment: Mapping[str, str] | None = None,
) -> LaunchPlan:
    """Validate and freeze every launch-affecting input before process creation."""
    fixture = _validate_fixture_identity(canary_file, path_policy)
    config_path, uv_executable = _load_filled_config(
        probe_config,
        probe_server=probe_server,
        fixture=fixture,
        path_policy=path_policy,
    )
    client_environment = _sanitized_environment(
        os.environ if parent_environment is None else parent_environment
    )

    raw_root = fixture.database.parent / "probe-records"
    outcome_root = path_policy.outcome_root
    if outcome_root is None:
        raise LaunchConfigurationError("Outcome record root is not configured.")
    raw_record_path = _validate_record_target(
        raw_record,
        canonical_root=raw_root,
        trusted_parent=fixture.database.parent,
        label="Raw record",
        must_be_absent=True,
    )
    outcome_row_path = _validate_record_target(
        outcome_row,
        canonical_root=outcome_root,
        trusted_parent=(
            path_policy.external_root
            if path_policy.external_root is not None
            else path_policy.repository_root
        ),
        label="Outcome row",
        must_be_absent=False,
    )
    protected_paths = {
        config_path,
        fixture.database,
        fixture.canary_file,
        fixture.manifest,
    }
    if (
        raw_record_path == outcome_row_path
        or raw_record_path in protected_paths
        or outcome_row_path in protected_paths
    ):
        raise LaunchConfigurationError(
            "Record destinations collide with launch inputs."
        )

    command = [
        client_executable,
        "-p",
        "--setting-sources",
        "",
        "--system-prompt",
        PROBE_SYSTEM_PROMPT,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
        "--tools",
        "",
        "--allowedTools",
        ",".join(f"mcp__{probe_server}__{tool}" for tool in PROBE_READ_ONLY_TOOLS),
        "--no-session-persistence",
    ]
    if model:
        command.extend(["--model", model])

    try:
        working_directory = Path(
            tempfile.mkdtemp(prefix=".probe-client-", dir=fixture.database.parent)
        ).resolve(strict=True)
    except OSError as error:
        raise LaunchConfigurationError(
            "Unable to create the isolated client cwd."
        ) from error
    try:
        if _is_reparse(working_directory) or any(working_directory.iterdir()):
            raise LaunchConfigurationError(
                "The isolated client cwd is not a new empty directory."
            )
        if working_directory in protected_paths | {
            raw_record_path,
            outcome_row_path,
        }:
            raise LaunchConfigurationError(
                "The isolated client cwd collides with another launch path."
            )
        return LaunchPlan(
            command=tuple(command),
            client_environment=client_environment,
            client_working_directory=working_directory,
            probe_config=config_path,
            fixture_database=fixture.database,
            fixture_manifest=fixture.manifest,
            raw_record=raw_record_path,
            outcome_row=outcome_row_path,
            canary=fixture.canary,
        )
    except BaseException:
        _remove_client_working_directory(
            working_directory,
            expected_parent=fixture.database.parent,
        )
        raise


def reserve_raw_record(plan: LaunchPlan) -> TextIO:
    """Exclusively reserve the validated raw transcript without truncation."""
    try:
        return plan.raw_record.open("x", encoding="utf-8", newline="\n")
    except FileExistsError as error:
        raise LaunchConfigurationError(
            f"Raw record already exists: {plan.raw_record}"
        ) from error


class SessionTransport(Protocol):
    def read_record(self) -> dict[str, Any] | None: ...

    def write_user_turn(self, text: str) -> None: ...

    def terminate(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    status: str
    session_id: str | None
    launch_isolation_enforced: bool
    probe_server_answered: bool
    canary_matched: bool
    failure_stage: str | None = None
    failure_reason: str | None = None

    def as_record(self) -> dict[str, object]:
        return asdict(self)


def _invalid(
    transport: SessionTransport,
    *,
    session_id: str | None,
    stage: str,
    reason: str,
    launch_isolation_enforced: bool = True,
    probe_server_answered: bool = False,
    canary_matched: bool = False,
) -> ProbeOutcome:
    transport.terminate()
    return ProbeOutcome(
        status="invalid",
        session_id=session_id,
        launch_isolation_enforced=launch_isolation_enforced,
        probe_server_answered=probe_server_answered,
        canary_matched=canary_matched,
        failure_stage=stage,
        failure_reason=reason,
    )


def _unsupervisable(
    transport: SessionTransport,
    *,
    session_id: str | None,
    stage: str,
    launch_isolation_enforced: bool,
    probe_server_answered: bool = False,
    canary_matched: bool = False,
) -> ProbeOutcome:
    transport.terminate()
    return ProbeOutcome(
        status="unsupervisable",
        session_id=session_id,
        launch_isolation_enforced=launch_isolation_enforced,
        probe_server_answered=probe_server_answered,
        canary_matched=canary_matched,
        failure_stage=stage,
        failure_reason="stream ended before the turn produced a result",
    )


def _session_id_of(record: dict[str, Any] | None) -> str | None:
    if not isinstance(record, dict):
        return None
    value = record.get("session_id")
    return value if isinstance(value, str) and value else None


def _subject_carries(value: Any, canary: str) -> bool:
    """Report whether a decoded tool result carries the canary as a message subject.

    Provenance, not presence: `mail_contact_history` echoes the caller's own `query`
    back even when nothing matched, so a canary found anywhere in the payload proves
    only that we asked for it. A `subject` comes from a stored row.
    """
    if isinstance(value, dict):
        subject = value.get("subject")
        if isinstance(subject, str) and subject == canary:
            return True
        return any(_subject_carries(item, canary) for item in value.values())
    if isinstance(value, list):
        return any(_subject_carries(item, canary) for item in value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return False
        return _subject_carries(decoded, canary)
    return False


def _message_content(record: dict[str, Any]) -> list[dict[str, Any]] | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list) or not all(
        isinstance(item, dict) for item in content
    ):
        return None
    return content


def _read_turn(
    transport: SessionTransport,
    *,
    probe_server: str,
    canary: str | None,
    allow_init: bool,
    expected_session_id: str | None = None,
) -> tuple[bool, bool, str | None, str | None]:
    probe_tool_ids: set[str] = set()
    probe_server_answered = False
    canary_matched = False
    init_seen = False
    turn_content_seen = False
    session_id = expected_session_id
    allowed_tool_names = {
        f"mcp__{probe_server}__{tool}" for tool in PROBE_READ_ONLY_TOOLS
    }
    while True:
        record = transport.read_record()
        if record is None:
            return (
                probe_server_answered,
                canary_matched,
                "stream ended before result",
                session_id,
            )
        record_type = record.get("type")
        if record_type == "rate_limit_event" or (
            record_type == "system" and record.get("subtype") == "thinking_tokens"
        ):
            continue
        if record_type == "tool_progress":
            if record.get("tool_name") in allowed_tool_names:
                continue
            return (
                probe_server_answered,
                canary_matched,
                "unrecognized session record",
                session_id,
            )
        if record_type == "system":
            if (
                not allow_init
                or init_seen
                or turn_content_seen
                or record.get("subtype") != "init"
            ):
                return (
                    probe_server_answered,
                    canary_matched,
                    "unrecognized session record",
                    session_id,
                )
            init_seen = True
            record_session_id = _session_id_of(record)
            if (
                expected_session_id is not None
                and record_session_id != expected_session_id
            ):
                return (
                    probe_server_answered,
                    canary_matched,
                    "session identity changed",
                    session_id,
                )
            session_id = record_session_id
            continue
        if record_type == "result":
            # A turn that ended in an error is a recognized shape, not a passed stage.
            if record.get("subtype") != "success" or record.get("is_error"):
                return (
                    probe_server_answered,
                    canary_matched,
                    "turn did not succeed",
                    session_id,
                )
            return probe_server_answered, canary_matched, None, session_id
        if record_type not in {"assistant", "user"}:
            return (
                probe_server_answered,
                canary_matched,
                "unrecognized session record",
                session_id,
            )
        turn_content_seen = True
        content = _message_content(record)
        if content is None:
            return (
                probe_server_answered,
                canary_matched,
                "unrecognized session record",
                session_id,
            )
        for item in content:
            # A tool result only counts as one when it arrives where the protocol puts
            # it. Accepting either block from either role lets a transcript assert an
            # answer no server ever gave.
            item_type = item.get("type")
            if (item_type == "tool_use" and record_type != "assistant") or (
                item_type == "tool_result" and record_type != "user"
            ):
                return (
                    probe_server_answered,
                    canary_matched,
                    "unrecognized session record",
                    session_id,
                )
            if item_type == "tool_use":
                tool_id = item.get("id")
                tool_name = item.get("name")
                if (
                    isinstance(tool_id, str)
                    and isinstance(tool_name, str)
                    and tool_name.startswith(f"mcp__{probe_server}__")
                ):
                    probe_tool_ids.add(tool_id)
            elif item_type == "tool_result":
                # A failed call echoes the request back, and the request carries the
                # canary — an error result is not evidence the fixture answered.
                if item.get("is_error"):
                    continue
                tool_use_id = item.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id in probe_tool_ids:
                    probe_server_answered = True
                    if canary is not None and _subject_carries(
                        item.get("content"), canary
                    ):
                        canary_matched = True


def supervise_session(
    transport: SessionTransport,
    *,
    probe_server: str,
    canary: str,
    target_question: str,
) -> ProbeOutcome:
    """Advance one invocation only when its preceding safety stage passed."""
    # An empty canary is a substring of every tool result, so it would pass the
    # stage it exists to fail. Refuse before the transport is touched.
    if not canary.strip():
        raise ValueError("The probe canary must be a non-empty value.")
    canary_envelope = (
        "Use only the probe MCP server. Call one probe mail tool and find a result "
        f"carrying this exact generated subject: {canary}"
    )
    transport.write_user_turn(canary_envelope)
    server_answered, canary_matched, error, session_id = _read_turn(
        transport,
        probe_server=probe_server,
        canary=canary,
        allow_init=True,
    )
    if error == "stream ended before result":
        return _unsupervisable(
            transport,
            session_id=session_id,
            stage="canary",
            launch_isolation_enforced=True,
            probe_server_answered=server_answered,
            canary_matched=canary_matched,
        )
    if error is not None:
        return _invalid(
            transport,
            session_id=session_id,
            stage="canary",
            reason=error,
            launch_isolation_enforced=True,
            probe_server_answered=server_answered,
            canary_matched=canary_matched,
        )
    if not canary_matched:
        return _invalid(
            transport,
            session_id=session_id,
            stage="canary",
            reason="no probe-server tool result carried the canary",
            launch_isolation_enforced=True,
            probe_server_answered=server_answered,
        )
    if session_id is None:
        return _invalid(
            transport,
            session_id=None,
            stage="canary",
            reason="missing session identity",
            launch_isolation_enforced=True,
            probe_server_answered=server_answered,
            canary_matched=True,
        )

    transport.write_user_turn(target_question)
    _, _, error, _ = _read_turn(
        transport,
        probe_server=probe_server,
        canary=None,
        allow_init=True,
        expected_session_id=session_id,
    )
    if error == "stream ended before result":
        # The canary stage already passed; a target-stage failure does not unmake it.
        return _unsupervisable(
            transport,
            session_id=session_id,
            stage="target",
            launch_isolation_enforced=True,
            probe_server_answered=True,
            canary_matched=True,
        )
    if error is not None:
        return _invalid(
            transport,
            session_id=session_id,
            stage="target",
            reason=error,
            launch_isolation_enforced=True,
            probe_server_answered=True,
            canary_matched=True,
        )
    transport.terminate()
    return ProbeOutcome(
        status="complete",
        session_id=session_id,
        launch_isolation_enforced=True,
        probe_server_answered=True,
        canary_matched=True,
    )


class ClaudeStreamTransport:
    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        raw_stream: TextIO,
    ) -> None:
        self.process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            # Undecodable bytes must not raise past the supervisor: replaced text fails
            # JSON parsing, which is already an unrecognized record and terminates.
            errors="replace",
            env=dict(env),
            cwd=cwd,
        )
        self.raw_stream = raw_stream

    def read_record(self) -> dict[str, Any] | None:
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        if not line:
            return None
        self.raw_stream.write(line)
        self.raw_stream.flush()
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return {"type": "unrecognized"}
        return record if isinstance(record, dict) else {"type": "unrecognized"}

    def write_user_turn(self, text: str) -> None:
        assert self.process.stdin is not None
        record = {"type": "user", "message": {"role": "user", "content": text}}
        self.process.stdin.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def terminate(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-executable", required=True)
    parser.add_argument("--probe-config", required=True, type=Path)
    parser.add_argument("--probe-server", required=True)
    parser.add_argument("--canary-file", required=True, type=Path)
    parser.add_argument("--target-question", required=True)
    parser.add_argument("--raw-record", required=True, type=Path)
    parser.add_argument("--outcome-row", required=True, type=Path)
    parser.add_argument("--model")
    return parser


def _run_probe(
    arguments: argparse.Namespace,
    *,
    path_policy: PathPolicy,
    transport_factory: Callable[..., SessionTransport] | None = None,
) -> int:
    plan = prepare_launch(
        client_executable=arguments.client_executable,
        probe_config=arguments.probe_config,
        probe_server=arguments.probe_server,
        canary_file=arguments.canary_file,
        raw_record=arguments.raw_record,
        outcome_row=arguments.outcome_row,
        model=arguments.model,
        path_policy=path_policy,
    )
    factory = ClaudeStreamTransport if transport_factory is None else transport_factory
    raw_stream: TextIO | None = None
    transport: SessionTransport | None = None
    outcome: ProbeOutcome | None = None
    try:
        raw_stream = reserve_raw_record(plan)
        transport = factory(
            plan.command,
            env=plan.client_environment,
            cwd=plan.client_working_directory,
            raw_stream=raw_stream,
        )
        outcome = supervise_session(
            transport,
            probe_server=arguments.probe_server,
            canary=plan.canary,
            target_question=arguments.target_question,
        )
    finally:
        # An unexpected failure must not leave the client alive holding a session.
        try:
            if transport is not None:
                transport.terminate()
        finally:
            try:
                if raw_stream is not None:
                    raw_stream.close()
            finally:
                cwd = plan.client_working_directory
                removed = _remove_client_working_directory(
                    cwd,
                    expected_parent=plan.fixture_database.parent,
                )
                if not removed:
                    print(
                        f"warning: client working directory is still locked: {cwd}",
                        file=sys.stderr,
                    )
    assert outcome is not None
    with plan.outcome_row.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(outcome.as_record(), ensure_ascii=False) + "\n")
    if outcome.status == "complete":
        return 0
    return 3 if outcome.status == "unsupervisable" else 2


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return _run_probe(arguments, path_policy=PathPolicy.production())


if __name__ == "__main__":
    raise SystemExit(main())

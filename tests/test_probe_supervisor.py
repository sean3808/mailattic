from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.probe_supervisor as probe_supervisor
from scripts.build_probe_fixture_db import (
    DATABASE_FILENAME,
    MANIFEST_FILENAME,
    build_probe_fixture,
)
from scripts.probe_supervisor import (
    ClaudeStreamTransport,
    LaunchConfigurationError,
    LaunchPlan,
    PathPolicy,
    _run_probe,
    build_parser,
    main,
    prepare_launch,
    reserve_raw_record,
    supervise_session,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_probe_supervisor_direct_script_help_is_importable() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/probe_supervisor.py", "--help"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )

    assert result.returncode == 0, result.stderr


def _test_path_policy(tmp_path: Path) -> PathPolicy:
    return PathPolicy.for_test(
        repository_root=REPOSITORY_ROOT,
        external_root=tmp_path,
    )


def _build_test_fixture(tmp_path: Path) -> tuple[Path, Path]:
    return build_probe_fixture(
        tmp_path.resolve(),
        allow_repository_output_for_test=True,
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_launch_inputs(tmp_path: Path) -> dict[str, object]:
    database_path, canary_path = _build_test_fixture(tmp_path)
    uv_executable = tmp_path / "uv.exe"
    uv_executable.write_bytes(b"test uv executable")
    config_path = tmp_path / "probe-config.json"
    config_path.write_text(
        json.dumps(
            {
                "_comment": "filled probe-only configuration",
                "mcpServers": {
                    "probe": {
                        "command": str(uv_executable),
                        "args": [
                            "run",
                            "--project",
                            str(REPOSITORY_ROOT),
                            "python",
                            "-m",
                            "mailattic.mcp_server",
                        ],
                        "env": {
                            "MAILATTIC_DB": str(database_path),
                            "PYTHONIOENCODING": "utf-8",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    raw_root = tmp_path / "probe-records"
    raw_root.mkdir()
    policy = _test_path_policy(tmp_path)
    assert policy.outcome_root is not None
    policy.outcome_root.mkdir()
    return {
        "client_executable": "claude.exe",
        "probe_config": config_path,
        "probe_server": "probe",
        "canary_file": canary_path,
        "raw_record": raw_root / "trial.jsonl",
        "outcome_row": policy.outcome_root / "trial.jsonl",
        "model": None,
        "path_policy": policy,
        "parent_environment": {"PATH": os.environ.get("PATH", "")},
    }


def _valid_cli_arguments(tmp_path: Path) -> tuple[list[str], PathPolicy, str]:
    launch_inputs = _valid_launch_inputs(tmp_path)
    canary_file = launch_inputs["canary_file"]
    assert isinstance(canary_file, Path)
    canary = canary_file.read_text(encoding="utf-8").strip()
    argv = [
        "--client-executable",
        str(launch_inputs["client_executable"]),
        "--probe-config",
        str(launch_inputs["probe_config"]),
        "--probe-server",
        str(launch_inputs["probe_server"]),
        "--canary-file",
        str(canary_file),
        "--target-question",
        "target question",
        "--raw-record",
        str(launch_inputs["raw_record"]),
        "--outcome-row",
        str(launch_inputs["outcome_row"]),
    ]
    policy = launch_inputs["path_policy"]
    assert isinstance(policy, PathPolicy)
    return argv, policy, canary


def _rewrite_config(
    launch_inputs: dict[str, object],
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    config_path = launch_inputs["probe_config"]
    assert isinstance(config_path, Path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    mutate(config)
    config_path.write_text(json.dumps(config), encoding="utf-8")


def _arguments_from_launch_inputs(
    launch_inputs: dict[str, object],
) -> SimpleNamespace:
    return SimpleNamespace(
        client_executable=launch_inputs["client_executable"],
        probe_config=launch_inputs["probe_config"],
        probe_server=launch_inputs["probe_server"],
        canary_file=launch_inputs["canary_file"],
        target_question="target question",
        raw_record=launch_inputs["raw_record"],
        outcome_row=launch_inputs["outcome_row"],
        model=launch_inputs["model"],
    )


def _assert_rejected_before_transport(
    launch_inputs: dict[str, object],
    *,
    match: str | None = None,
) -> None:
    constructed = False

    def fail_if_constructed(*args: object, **kwargs: object) -> None:
        nonlocal constructed
        constructed = True
        raise AssertionError("transport must not be constructed")

    policy = launch_inputs["path_policy"]
    assert isinstance(policy, PathPolicy)
    with pytest.raises(LaunchConfigurationError, match=match):
        _run_probe(
            _arguments_from_launch_inputs(launch_inputs),
            path_policy=policy,
            transport_factory=fail_if_constructed,
        )
    assert not constructed


def test_prepare_launch_builds_an_immutable_sanitized_plan(tmp_path: Path) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    launch_inputs["parent_environment"] = {
        "Path": "C:\\Windows",
        "PythonPath": "redirect",
        "PYTHONHOME": "redirect",
        "uv_working_dir": "redirect",
        "UV_PROJECT": "redirect",
        "UV_PYTHON": "redirect",
        "UV_CACHE_DIR": "redirect",
        "Virtual_Env": "redirect",
        "VIRTUAL_ENV_PROMPT": "redirect",
        "__PYVENV_LAUNCHER__": "redirect",
    }

    plan = prepare_launch(**launch_inputs)

    assert isinstance(plan, LaunchPlan)
    assert plan.client_environment == {"Path": "C:\\Windows"}
    assert all(
        not key.upper().startswith(("PYTHON", "UV_"))
        and key.upper()
        not in {"VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "__PYVENV_LAUNCHER__"}
        for key in plan.client_environment
    )
    with pytest.raises(TypeError):
        plan.client_environment["PYTHONPATH"] = "redirect"  # type: ignore[index]
    assert plan.client_working_directory.is_dir()
    assert list(plan.client_working_directory.iterdir()) == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda config: config["mcpServers"].update({"extra": {}}),
        lambda config: config["mcpServers"].__setitem__(
            "wrong", config["mcpServers"].pop("probe")
        ),
        lambda config: config["mcpServers"]["probe"].update({"cwd": "C:\\"}),
        lambda config: config["mcpServers"]["probe"].__setitem__("command", "uv"),
        lambda config: config["mcpServers"]["probe"]["args"].__setitem__(
            2, "C:\\wrong-checkout"
        ),
        lambda config: config["mcpServers"]["probe"]["env"].pop("MAILATTIC_DB"),
        lambda config: config["mcpServers"]["probe"]["env"].__setitem__(
            "MAILATTIC_DB", ""
        ),
        lambda config: config["mcpServers"]["probe"]["env"].__setitem__(
            "MAILATTIC_DB", "relative.db"
        ),
        lambda config: config["mcpServers"]["probe"]["env"].update({"EXTRA": "value"}),
        lambda config: config["mcpServers"]["probe"]["env"].__setitem__(
            "PYTHONIOENCODING", "cp950"
        ),
        lambda config: config["mcpServers"]["probe"]["env"].pop("PYTHONIOENCODING"),
    ],
    ids=[
        "extra-server",
        "wrong-server",
        "unexpected-field",
        "bare-uv",
        "wrong-checkout",
        "missing-db",
        "blank-db",
        "relative-db",
        "extra-env",
        "wrong-encoding",
        "missing-encoding",
    ],
)
def test_prepare_launch_rejects_invalid_filled_configuration(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    _rewrite_config(launch_inputs, mutation)

    _assert_rejected_before_transport(launch_inputs)


def test_prepare_launch_rejects_malformed_and_placeholder_configuration(
    tmp_path: Path,
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    config_path = launch_inputs["probe_config"]
    assert isinstance(config_path, Path)
    for content in ("{", '{"mcpServers":"__PROBE_CONFIG__"}'):
        config_path.write_text(content, encoding="utf-8")
        _assert_rejected_before_transport(launch_inputs)


@pytest.mark.parametrize("argument_name", ["canary_file", "probe_config"])
def test_prepare_launch_rejects_relative_artifact_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argument_name: str,
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    artifact = launch_inputs[argument_name]
    assert isinstance(artifact, Path)
    launch_inputs[argument_name] = Path(artifact.name)
    monkeypatch.chdir(tmp_path)

    _assert_rejected_before_transport(launch_inputs, match="absolute")


def test_prepare_launch_rejects_missing_uv_executable(tmp_path: Path) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    _rewrite_config(
        launch_inputs,
        lambda config: config["mcpServers"]["probe"].__setitem__(
            "command", str(tmp_path / "missing-uv.exe")
        ),
    )

    _assert_rejected_before_transport(launch_inputs, match="uv")


def test_prepare_launch_rejects_a_reparse_uv_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    uv_path = tmp_path / "uv.exe"
    original = probe_supervisor._is_reparse
    monkeypatch.setattr(
        probe_supervisor,
        "_is_reparse",
        lambda path: path == uv_path or original(path),
    )

    _assert_rejected_before_transport(launch_inputs, match="reparse")


def test_prepare_launch_rejects_manifest_valid_database_without_exact_canary(
    tmp_path: Path,
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    database_path = tmp_path / DATABASE_FILENAME
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE messages SET subject = 'not-the-canary' "
            "WHERE subject LIKE 'PROBE-CANARY-%'"
        )
        connection.commit()
    manifest_path = tmp_path / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["database"]["sha256"] = _digest(database_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _assert_rejected_before_transport(launch_inputs, match="exact generated canary")


def test_prepare_launch_rejects_output_escape_collision_and_existing_raw(
    tmp_path: Path,
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    invalid_raw_paths = [
        tmp_path / "outside.jsonl",
        launch_inputs["probe_config"],
    ]
    for invalid_raw in invalid_raw_paths:
        launch_inputs["raw_record"] = invalid_raw
        _assert_rejected_before_transport(launch_inputs)

    raw_record = tmp_path / "probe-records" / "existing.jsonl"
    raw_record.write_text("do not overwrite", encoding="utf-8")
    launch_inputs["raw_record"] = raw_record
    _assert_rejected_before_transport(launch_inputs, match="already exists")


def test_raw_record_is_reserved_exclusively_after_preflight(tmp_path: Path) -> None:
    plan = prepare_launch(**_valid_launch_inputs(tmp_path))

    with reserve_raw_record(plan) as stream:
        stream.write("first line\n")
        stream.flush()
        with pytest.raises(LaunchConfigurationError, match="already exists"):
            reserve_raw_record(plan)

    assert plan.raw_record.read_text(encoding="utf-8") == "first line\n"


def test_prepare_launch_rejects_an_outcome_hard_link(tmp_path: Path) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    outcome_row = launch_inputs["outcome_row"]
    assert isinstance(outcome_row, Path)
    unrelated = tmp_path / "unrelated.jsonl"
    unrelated.write_text("preserve", encoding="utf-8")
    os.link(unrelated, outcome_row)

    _assert_rejected_before_transport(launch_inputs, match="hard link")


def test_prepare_launch_rejects_redirected_record_root_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    raw_root = tmp_path / "probe-records"
    original = probe_supervisor._is_reparse
    monkeypatch.setattr(
        probe_supervisor,
        "_is_reparse",
        lambda path: path == raw_root or original(path),
    )
    _assert_rejected_before_transport(launch_inputs, match="reparse")

    monkeypatch.setattr(probe_supervisor, "_is_reparse", original)
    outcome_row = launch_inputs["outcome_row"]
    assert isinstance(outcome_row, Path)
    outcome_row.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(
        probe_supervisor,
        "_is_reparse",
        lambda path: path == outcome_row or original(path),
    )
    _assert_rejected_before_transport(launch_inputs, match="reparse")


def test_prepare_launch_rejects_redirected_outcome_root_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    policy = launch_inputs["path_policy"]
    assert isinstance(policy, PathPolicy)
    outcome_root = policy.outcome_root
    assert outcome_root is not None
    original = probe_supervisor._is_reparse
    monkeypatch.setattr(
        probe_supervisor,
        "_is_reparse",
        lambda path: path == outcome_root or original(path),
    )

    _assert_rejected_before_transport(launch_inputs, match="reparse")


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_fixture_manifest_schema_version_is_type_exact(
    tmp_path: Path, schema_version: object
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    manifest_path = tmp_path / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = schema_version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _assert_rejected_before_transport(launch_inputs, match="schema")


def test_prepare_launch_removes_cwd_when_late_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    original = probe_supervisor._is_reparse
    monkeypatch.setattr(
        probe_supervisor,
        "_is_reparse",
        lambda path: path.name.startswith(".probe-client-") or original(path),
    )

    with pytest.raises(LaunchConfigurationError, match="cwd"):
        prepare_launch(**launch_inputs)

    assert list(tmp_path.glob(".probe-client-*")) == []


def test_production_policy_rejects_repository_local_fixture_before_launch(
    tmp_path: Path,
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    launch_inputs["path_policy"] = PathPolicy.production(REPOSITORY_ROOT)

    with pytest.raises(LaunchConfigurationError, match="outside the repository"):
        prepare_launch(**launch_inputs)


def test_public_main_rejects_repository_local_artifacts_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, _, _ = _valid_cli_arguments(tmp_path)
    constructed = False

    def fail_if_constructed(*args: object, **kwargs: object) -> None:
        nonlocal constructed
        constructed = True
        raise AssertionError("transport must not be constructed")

    monkeypatch.setattr(probe_supervisor, "ClaudeStreamTransport", fail_if_constructed)
    with pytest.raises(LaunchConfigurationError, match="outside the repository"):
        main(argv)
    assert not constructed


def test_valid_test_policy_reaches_transport_with_the_frozen_plan(
    tmp_path: Path,
) -> None:
    argv, policy, canary = _valid_cli_arguments(tmp_path)
    arguments = build_parser().parse_args(argv)
    captured: dict[str, object] = {}

    class CapturingTransport(RecordedTransport):
        def __init__(
            self,
            command: tuple[str, ...],
            *,
            env: dict[str, str],
            cwd: Path,
            raw_stream: object,
        ) -> None:
            captured.update(command=command, env=env, cwd=cwd, raw_stream=raw_stream)
            super().__init__(
                [
                    _clean_init(),
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "canary-tool",
                                    "name": "mcp__probe__mail_search",
                                }
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "canary-tool",
                                    "content": json.dumps({"subject": canary}),
                                }
                            ]
                        },
                    },
                    {"type": "result", "subtype": "success"},
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "answer"}]},
                    },
                    {"type": "result", "subtype": "success"},
                ]
            )

    assert (
        _run_probe(arguments, path_policy=policy, transport_factory=CapturingTransport)
        == 0
    )
    command = captured["command"]
    assert isinstance(command, tuple)
    for expected in (
        "--setting-sources",
        "--system-prompt",
        "--strict-mcp-config",
        "--tools",
        "--allowedTools",
        "--no-session-persistence",
    ):
        assert expected in command
    assert "--bare" not in command
    assert "--permission-mode" not in command
    assert "bypassPermissions" not in command
    settings_index = command.index("--setting-sources")
    assert command[settings_index + 1] == ""
    prompt_index = command.index("--system-prompt")
    assert command[prompt_index + 1] == (
        "You are running a read-only MCP probe against the configured synthetic "
        "fixture. Use only the configured probe MCP server for data. Do not seek "
        "or use any other data source."
    )
    allowed_tools_index = command.index("--allowedTools")
    assert command[allowed_tools_index + 1] == (
        "mcp__probe__mail_search,mcp__probe__mail_show,"
        "mcp__probe__mail_thread,mcp__probe__mail_contact_history,"
        "mcp__probe__mail_attachments"
    )
    tools_index = command.index("--tools")
    assert command[tools_index + 1] == ""
    cwd = captured["cwd"]
    assert isinstance(cwd, Path)
    assert cwd != tmp_path
    assert captured["raw_stream"] is not None


def test_client_cwd_cleanup_retries_a_transient_windows_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_cwd = tmp_path / ".probe-client-transient"
    client_cwd.mkdir()
    real_rmtree = probe_supervisor.shutil.rmtree
    calls = 0

    def flaky_rmtree(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("simulated transient sharing violation")
        real_rmtree(path)

    monkeypatch.setattr(probe_supervisor.shutil, "rmtree", flaky_rmtree)

    probe_supervisor._remove_client_working_directory(
        client_cwd,
        expected_parent=tmp_path,
    )

    assert calls == 2
    assert not client_cwd.exists()


def test_client_cwd_cleanup_does_not_mask_a_persistent_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_cwd = tmp_path / ".probe-client-persistent"
    client_cwd.mkdir()
    calls = 0

    def locked_rmtree(path: Path) -> None:
        nonlocal calls
        calls += 1
        raise PermissionError("simulated persistent sharing violation")

    monkeypatch.setattr(probe_supervisor.shutil, "rmtree", locked_rmtree)
    monkeypatch.setattr(probe_supervisor.time, "sleep", lambda _: None)

    removed = probe_supervisor._remove_client_working_directory(
        client_cwd,
        expected_parent=tmp_path,
    )

    assert removed is False
    assert calls == 5
    assert client_cwd.exists()


def test_persistent_cwd_lock_preserves_the_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, policy, canary = _valid_cli_arguments(tmp_path)
    arguments = build_parser().parse_args(argv)

    class CompleteTransport(RecordedTransport):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(
                [
                    _clean_init(),
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "canary-tool",
                                    "name": "mcp__probe__mail_search",
                                }
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "canary-tool",
                                    "content": json.dumps({"subject": canary}),
                                }
                            ]
                        },
                    },
                    {"type": "result", "subtype": "success"},
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "answer"}]},
                    },
                    {"type": "result", "subtype": "success"},
                ]
            )

    monkeypatch.setattr(
        probe_supervisor,
        "_remove_client_working_directory",
        lambda *args, **kwargs: False,
    )

    assert (
        _run_probe(arguments, path_policy=policy, transport_factory=CompleteTransport)
        == 0
    )
    outcome_record = json.loads(
        Path(arguments.outcome_row).read_text(encoding="utf-8").splitlines()[-1]
    )
    assert outcome_record["status"] == "complete"
    assert "still locked" in capsys.readouterr().err


def test_transport_flushes_each_stdout_line_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.StringIO("not-json\n")
            self.stdin = io.StringIO()
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: int) -> int:
            assert timeout == 5
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    raw_path = tmp_path / "raw.jsonl"
    with raw_path.open("x", encoding="utf-8", newline="\n") as raw_stream:
        transport = ClaudeStreamTransport(
            ("claude.exe",),
            env={},
            cwd=tmp_path,
            raw_stream=raw_stream,
        )
        assert transport.read_record() == {"type": "unrecognized"}
        assert raw_path.read_text(encoding="utf-8") == "not-json\n"


def test_run_probe_closes_raw_stream_and_cwd_when_terminate_raises(
    tmp_path: Path,
) -> None:
    argv, policy, canary = _valid_cli_arguments(tmp_path)
    arguments = build_parser().parse_args(argv)
    captured: dict[str, object] = {}

    class RaisingTerminateTransport(RecordedTransport):
        def __init__(
            self,
            command: tuple[str, ...],
            *,
            env: dict[str, str],
            cwd: Path,
            raw_stream: object,
        ) -> None:
            captured.update(cwd=cwd, raw_stream=raw_stream)
            super().__init__(
                [
                    _clean_init(),
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "canary-tool",
                                    "name": "mcp__probe__mail_search",
                                }
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "canary-tool",
                                    "content": json.dumps({"subject": canary}),
                                }
                            ]
                        },
                    },
                    {"type": "result", "subtype": "success"},
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "answer"}]},
                    },
                    {"type": "result", "subtype": "success"},
                ]
            )

        def terminate(self) -> None:
            raise RuntimeError("terminate failed")

    with pytest.raises(RuntimeError, match="terminate failed"):
        _run_probe(
            arguments,
            path_policy=policy,
            transport_factory=RaisingTerminateTransport,
        )

    cwd = captured["cwd"]
    raw_stream = captured["raw_stream"]
    assert isinstance(cwd, Path)
    assert not cwd.exists()
    assert isinstance(raw_stream, io.TextIOBase)
    assert raw_stream.closed


def test_fixture_identity_rejects_a_renamed_database_without_manifest_binding(
    tmp_path: Path,
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    database_path = tmp_path / DATABASE_FILENAME
    renamed = tmp_path / "renamed-production.db"
    database_path.replace(renamed)
    renamed.replace(tmp_path / DATABASE_FILENAME)
    (tmp_path / MANIFEST_FILENAME).unlink()

    _assert_rejected_before_transport(launch_inputs, match="manifest")


def test_fixture_identity_rejects_a_manifest_digest_mismatch(tmp_path: Path) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    manifest_path = tmp_path / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["database"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _assert_rejected_before_transport(launch_inputs, match="digest")


def test_fixture_identity_rejects_wal_before_opening_the_database(
    tmp_path: Path,
) -> None:
    launch_inputs = _valid_launch_inputs(tmp_path)
    database_path = tmp_path / DATABASE_FILENAME
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "UPDATE messages SET subject = 'WAL only' "
            "WHERE id = (SELECT min(id) FROM messages)"
        )
        connection.commit()
        assert Path(f"{database_path}-wal").exists()

        _assert_rejected_before_transport(launch_inputs, match="sidecar")
    finally:
        connection.close()


class RecordedTransport:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = deque(records)
        self.operations: list[tuple[str, object]] = []
        self.written_turns: list[str] = []
        self.terminated = False

    def read_record(self) -> dict[str, Any] | None:
        record = self.records.popleft() if self.records else None
        self.operations.append(("read", record))
        return record

    def write_user_turn(self, text: str) -> None:
        self.written_turns.append(text)
        self.operations.append(("write", text))

    def terminate(self) -> None:
        self.terminated = True
        self.operations.append(("terminate", None))


def _clean_init() -> dict[str, object]:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": "recorded-session",
        "tools": [],
        "mcp_servers": [{"name": "probe", "status": "pending"}],
    }


def test_post_turn_init_inventory_is_metadata_not_a_gate() -> None:
    unclean_init = _clean_init()
    unclean_init["tools"] = ["Bash", "mcp__probe__mail_search"]
    transport = RecordedTransport([unclean_init])

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "unsupervisable"
    assert outcome.failure_stage == "canary"
    assert outcome.session_id == "recorded-session"
    assert transport.terminated
    assert len(transport.written_turns) == 1
    assert "test-canary" in transport.written_turns[0]


def test_missing_canary_terminates_before_target_question() -> None:
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                            "input": {"keyword": "test-canary"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "No messages found.",
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_stage == "canary"
    assert transport.terminated
    assert len(transport.written_turns) == 1
    assert "test-canary" in transport.written_turns[0]
    assert "target question" not in transport.written_turns


def test_canary_turn_finishes_before_target_turn_is_written() -> None:
    canary_result = {"type": "result", "subtype": "success", "turn": "canary"}
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                            "input": {"keyword": "test-canary"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": '{"subject":"test-canary"}',
                        }
                    ]
                },
            },
            canary_result,
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "target answer"}]},
            },
            {"type": "result", "subtype": "success", "turn": "target"},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "complete"
    record = outcome.as_record()
    assert record["launch_isolation_enforced"] is True
    assert "tool_surface_attested" not in record
    assert transport.operations[0][0] == "write"
    assert "test-canary" in str(transport.operations[0][1])
    assert transport.written_turns[1] == "target question"
    canary_result_read = transport.operations.index(("read", canary_result))
    target_question_written = transport.operations.index(("write", "target question"))
    assert canary_result_read < target_question_written


def test_each_turn_init_is_metadata_when_session_identity_is_stable() -> None:
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": '{"subject":"test-canary"}',
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
            _clean_init(),
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "target answer"}]},
            },
            {"type": "result", "subtype": "success"},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "complete"
    assert outcome.session_id == "recorded-session"


@pytest.mark.parametrize("session_id", [None, ""])
def test_canary_without_session_identity_stops_before_target(
    session_id: str | None,
) -> None:
    records: list[dict[str, object]] = []
    if session_id is not None:
        init = _clean_init()
        init["session_id"] = session_id
        records.append(init)
    records.extend(
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": '{"subject":"test-canary"}',
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
        ]
    )
    transport = RecordedTransport(records)

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_stage == "canary"
    assert outcome.failure_reason == "missing session identity"
    assert outcome.session_id is None
    assert len(transport.written_turns) == 1
    assert "target question" not in transport.written_turns


def test_target_turn_init_with_a_different_session_is_rejected() -> None:
    target_init = _clean_init()
    target_init["session_id"] = "different-session"
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": '{"subject":"test-canary"}',
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
            target_init,
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_stage == "target"
    assert outcome.failure_reason == "session identity changed"
    assert outcome.session_id == "recorded-session"


def test_subject_substring_does_not_satisfy_the_canary_stage() -> None:
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": '{"subject":"prefix-test-canary-suffix"}',
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.probe_server_answered
    assert not outcome.canary_matched
    assert "target question" not in transport.written_turns


def test_stream_ending_before_a_turn_result_is_unsupervisable() -> None:
    transport = RecordedTransport([_clean_init()])

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "unsupervisable"
    assert outcome.failure_stage == "canary"
    assert transport.terminated
    assert "target question" not in transport.written_turns


def test_tool_result_in_an_assistant_record_is_not_an_answer() -> None:
    """Only a user record carries a real tool result; roles are not interchangeable."""
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                            "input": {"keyword": "test-canary"},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": '{"subject":"test-canary"}',
                        },
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_reason == "unrecognized session record"
    assert not outcome.canary_matched
    assert "target question" not in transport.written_turns


def test_target_stage_failure_preserves_the_canary_evidence() -> None:
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                            "input": {"keyword": "test-canary"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": '{"subject":"test-canary"}',
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
            {"type": "future_unknown_record", "payload": {}},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_stage == "target"
    assert outcome.canary_matched
    assert outcome.probe_server_answered
    assert outcome.as_record()["canary_matched"] is True


def test_canary_echoed_as_a_query_field_does_not_prove_provenance() -> None:
    """`mail_contact_history` echoes the caller's query even with zero matches."""
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_contact_history",
                            "input": {"query": "test-canary"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": '{"query":"test-canary","total_messages":0,'
                            '"contacts":[]}',
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_stage == "canary"
    assert outcome.probe_server_answered
    assert not outcome.canary_matched
    assert "target question" not in transport.written_turns


def test_empty_pending_inventory_does_not_block_the_canary_turn() -> None:
    empty_init = _clean_init()
    empty_init["tools"] = []
    empty_init["mcp_servers"] = []
    transport = RecordedTransport([empty_init])

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "unsupervisable"
    assert outcome.failure_stage == "canary"
    assert outcome.session_id == "recorded-session"
    assert len(transport.written_turns) == 1


def test_named_client_telemetry_is_evidence_neutral() -> None:
    transport = RecordedTransport(
        [
            _clean_init(),
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            {"type": "system", "subtype": "thinking_tokens"},
            {
                "type": "tool_progress",
                "tool_use_id": "heartbeat-1",
                "tool_name": "mcp__probe__mail_search",
                "heartbeat": True,
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": '{"subject":"test-canary"}',
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            {"type": "system", "subtype": "thinking_tokens"},
            {
                "type": "tool_progress",
                "tool_use_id": "heartbeat-2",
                "tool_name": "mcp__probe__mail_search",
                "heartbeat": True,
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "answer"}]},
            },
            {"type": "result", "subtype": "success"},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "complete"
    assert outcome.probe_server_answered
    assert outcome.canary_matched
    assert transport.written_turns == [
        "Use only the probe MCP server. Call one probe mail tool and find a result "
        "carrying this exact generated subject: test-canary",
        "target question",
    ]


def test_tool_progress_for_an_unallowed_tool_remains_unrecognized() -> None:
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "tool_progress",
                "tool_use_id": "heartbeat-1",
                "tool_name": "Bash",
                "heartbeat": True,
            },
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_reason == "unrecognized session record"
    assert "target question" not in transport.written_turns


def test_hook_lifecycle_record_remains_unrecognized() -> None:
    transport = RecordedTransport(
        [
            {
                "type": "system",
                "subtype": "hook_started",
                "session_id": "hook-session",
            }
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_reason == "unrecognized session record"
    assert "target question" not in transport.written_turns


def test_error_tool_result_echoing_the_canary_does_not_pass_the_gate() -> None:
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                            "input": {"keyword": "test-canary"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "is_error": True,
                            "content": 'database error while searching "test-canary"',
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_stage == "canary"
    assert not outcome.probe_server_answered
    assert not outcome.canary_matched
    assert "target question" not in transport.written_turns


def test_blank_canary_is_refused_before_the_transport_is_touched() -> None:
    transport = RecordedTransport([_clean_init()])

    with pytest.raises(ValueError, match="non-empty"):
        supervise_session(
            transport,
            probe_server="probe",
            canary="   ",
            target_question="target question",
        )

    assert transport.operations == []


def test_failed_turn_result_terminates_before_target_question() -> None:
    transport = RecordedTransport(
        [
            _clean_init(),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__probe__mail_search",
                            "input": {"keyword": "test-canary"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": '{"subject":"test-canary"}',
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "error_max_turns", "is_error": True},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_stage == "canary"
    assert outcome.failure_reason == "turn did not succeed"
    assert transport.terminated
    assert "target question" not in transport.written_turns


def test_unrecognized_session_record_terminates_without_fallback() -> None:
    transport = RecordedTransport(
        [
            _clean_init(),
            {"type": "future_unknown_record", "payload": {}},
            {"type": "result", "subtype": "success"},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_reason == "unrecognized session record"
    assert transport.terminated
    assert len(transport.written_turns) == 1
    assert "target question" not in transport.written_turns
    assert len(transport.records) == 1


def test_duplicate_post_turn_init_is_rejected() -> None:
    transport = RecordedTransport([_clean_init(), _clean_init()])

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_stage == "canary"
    assert outcome.failure_reason == "unrecognized session record"
    assert outcome.session_id == "recorded-session"
    assert "target question" not in transport.written_turns


def test_other_system_record_after_canary_is_rejected() -> None:
    transport = RecordedTransport(
        [{"type": "system", "subtype": "status", "session_id": "unexpected"}]
    )

    outcome = supervise_session(
        transport,
        probe_server="probe",
        canary="test-canary",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_reason == "unrecognized session record"
    assert "target question" not in transport.written_turns

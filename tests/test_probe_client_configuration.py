"""Committed client configuration and supervisor-owned launch isolation.

The filled Client A template must pass the same immutable pre-spawn validation
used by a live run. A successful-looking canary turn without a correlated probe
server result remains invalid even when model prose happens to repeat the canary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.build_probe_fixture_db import build_probe_fixture
from scripts.probe_supervisor import (
    PathPolicy,
    _run_probe,
    build_parser,
    supervise_session,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_TEMPLATE = REPOSITORY_ROOT / ".dev" / "mcp-dev.json.template"
PROTOCOL_DOCUMENT = REPOSITORY_ROOT / "docs" / "mcp-client-probe-loop.md"
SUPERSEDED_CHECKER = REPOSITORY_ROOT / "scripts" / "check_probe_config.py"
PROBE_SERVER = "mailattic-mail-probe"


def test_supervisor_has_no_competing_standalone_configuration_checker() -> None:
    assert not SUPERSEDED_CHECKER.exists()


def test_documented_fill_assigns_the_complete_fixture_database_path() -> None:
    protocol = PROTOCOL_DOCUMENT.read_text(encoding="utf-8")

    assert "$Config = $Template | ConvertFrom-Json" in protocol
    assert (
        "$Config.mcpServers.'mailattic-mail-probe'.env.MAILATTIC_DB = "
        "(Join-Path $FixtureDir 'probe-fixture.db')"
    ) in protocol
    assert ".Replace('\"__FIXTURE_DIR__\"'" not in protocol


def test_filled_committed_template_reaches_transport_after_preflight(
    tmp_path: Path,
) -> None:
    database, canary_file = build_probe_fixture(
        tmp_path.resolve(),
        allow_repository_output_for_test=True,
    )
    uv_executable = tmp_path / "uv.exe"
    uv_executable.write_bytes(b"test uv executable")
    filled_config = tmp_path / "mcp-dev.json"
    filled_config.write_text(
        COMMITTED_TEMPLATE.read_text(encoding="utf-8")
        .replace("__UV_EXECUTABLE__", json.dumps(str(uv_executable))[1:-1])
        .replace("__WORKTREE_ROOT__", json.dumps(str(REPOSITORY_ROOT))[1:-1])
        .replace("__FIXTURE_DIR__", json.dumps(str(tmp_path))[1:-1]),
        encoding="utf-8",
    )
    raw_root = tmp_path / "probe-records"
    raw_root.mkdir()
    policy = PathPolicy.for_test(
        repository_root=REPOSITORY_ROOT,
        external_root=tmp_path,
    )
    assert policy.outcome_root is not None
    policy.outcome_root.mkdir()
    arguments = build_parser().parse_args(
        [
            "--client-executable",
            "claude.exe",
            "--probe-config",
            str(filled_config),
            "--probe-server",
            PROBE_SERVER,
            "--canary-file",
            str(canary_file),
            "--target-question",
            "target question",
            "--raw-record",
            str(raw_root / "trial.jsonl"),
            "--outcome-row",
            str(policy.outcome_root / "trial.jsonl"),
        ]
    )
    canary = canary_file.read_text(encoding="utf-8").strip()
    captured: dict[str, object] = {}

    class CapturingTransport(_RecordedTransport):
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
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": "template-plan",
                        "tools": [],
                        "mcp_servers": [{"name": PROBE_SERVER, "status": "pending"}],
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "canary-tool",
                                    "name": f"mcp__{PROBE_SERVER}__mail_search",
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
        _run_probe(
            arguments,
            path_policy=policy,
            transport_factory=CapturingTransport,
        )
        == 0
    )
    assert captured["command"]
    assert database.resolve(strict=True) == Path(
        json.loads(filled_config.read_text(encoding="utf-8"))["mcpServers"][
            PROBE_SERVER
        ]["env"]["MAILATTIC_DB"]
    ).resolve(strict=True)


class _RecordedTransport:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = list(records)
        self.written_turns: list[str] = []
        self.terminated = False

    def read_record(self) -> dict[str, Any] | None:
        if not self._records:
            return None
        return self._records.pop(0)

    def write_user_turn(self, text: str) -> None:
        self.written_turns.append(text)

    def terminate(self) -> None:
        self.terminated = True


def test_a_successful_turn_with_no_probe_tool_call_is_unreachable_not_unproven() -> (
    None
):
    # The failure this models: the MCP server never loaded, so the model answered
    # from its own prose and the process still ended successfully. Neither the
    # exit status nor the turn result distinguishes it from a good run — only the
    # absence of any probe-server tool call does.
    transport = _RecordedTransport(
        [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "session-no-server",
                "tools": [f"mcp__{PROBE_SERVER}__mail_search"],
                "mcp_servers": [{"name": PROBE_SERVER}],
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "I found a message titled PROBE-CANARY-XYZ.",
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
        ]
    )

    outcome = supervise_session(
        transport,
        probe_server=PROBE_SERVER,
        canary="PROBE-CANARY-XYZ",
        target_question="target question",
    )

    assert outcome.status == "invalid"
    assert outcome.failure_stage == "canary"
    assert outcome.launch_isolation_enforced is True
    # The distinction this test exists for: the server never answered at all, so
    # reachability failed. Recording it as a canary mismatch would suggest some
    # database answered wrongly, when in fact none answered.
    assert outcome.probe_server_answered is False
    assert outcome.canary_matched is False
    # The model's prose repeated the canary. Prose is not provenance.
    assert transport.terminated
    assert "target question" not in transport.written_turns

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from mailattic.mcp_delivery import (
    MCP_RESPONSE_BUDGET_CHARS,
    build_call_tool_result,
    measure_call_tool_result_chars,
)
from mailattic.mcp_server import create_server, shape_message_payload
from mailattic.query import get_message, resolve_thread_message_ids
from scripts.build_probe_fixture_db import (
    CANARY_FILENAME,
    DATABASE_FILENAME,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    build_probe_fixture,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _build_test_fixture(output_directory: Path) -> tuple[Path, Path]:
    return build_probe_fixture(
        output_directory.resolve(),
        allow_repository_output_for_test=True,
    )


def _message_id_for_subject(database_path: Path, subject: str) -> int:
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute(
            "SELECT id FROM messages WHERE subject = ?", [subject]
        ).fetchone()
    assert row is not None, f"fixture subject was not found: {subject}"
    return int(row[0])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_fixture_delivery_exceeds_budget(
    database_path: Path,
    *,
    record_subject: str = "Probe oversized record",
) -> None:
    server = create_server(database_path=database_path)

    record_id = _message_id_for_subject(database_path, record_subject)
    record_payload = shape_message_payload(
        get_message(database_path, record_id, read_only=True),
        include_body=True,
        recipients_limit=25,
        attachments_limit=20,
        body_max_chars=100_000,
    )
    record_payload["response_truncated"] = False
    record_tool = server._tool_manager.get_tool("mail_show")
    assert record_tool is not None
    delivered_record = build_call_tool_result(record_payload, tool=record_tool)
    assert measure_call_tool_result_chars(delivered_record) > MCP_RESPONSE_BUDGET_CHARS

    thread_start_id = _message_id_for_subject(database_path, "Probe oversized thread 1")
    thread_ids = resolve_thread_message_ids(
        database_path, thread_start_id, read_only=True
    )
    thread_messages = [
        shape_message_payload(
            get_message(database_path, message_id, read_only=True),
            include_body=True,
            recipients_limit=5,
            attachments_limit=5,
            body_max_chars=100_000,
        )
        for message_id in thread_ids
    ]
    thread_payload: dict[str, object] = {
        "messages": thread_messages,
        "total_messages": len(thread_ids),
        "returned_messages": len(thread_ids),
        "omitted_earlier_count": 0,
        "response_truncated": False,
    }
    thread_tool = server._tool_manager.get_tool("mail_thread")
    assert thread_tool is not None
    delivered_thread = build_call_tool_result(thread_payload, tool=thread_tool)
    assert measure_call_tool_result_chars(delivered_thread) > MCP_RESPONSE_BUDGET_CHARS


def test_generator_writes_every_required_shape_in_project_runtime(
    tmp_path: Path,
) -> None:
    database_path, canary_path = _build_test_fixture(tmp_path)

    assert database_path.is_absolute()
    assert database_path.is_relative_to(REPOSITORY_ROOT)
    assert canary_path == database_path.with_suffix(".canary")

    canary = canary_path.read_text(encoding="utf-8").strip()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute(
            "SELECT 1 FROM messages WHERE body_text LIKE '%探針測試內容%'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM message_attachments "
            "WHERE attachment_name = 'probe-report.pdf' "
            "AND mime_type = 'application/pdf'"
        ).fetchone()
        oversized_record = connection.execute(
            "SELECT length(body_text) AS body_length FROM messages "
            "WHERE subject = 'Probe oversized record'"
        ).fetchone()
        assert oversized_record is not None
        assert oversized_record["body_length"] >= 150_000
        oversized_thread = connection.execute(
            "SELECT count(*) AS message_count, "
            "sum(length(body_text)) AS body_length "
            "FROM messages WHERE subject LIKE 'Probe oversized thread %' "
            "GROUP BY thread_key"
        ).fetchone()
        assert oversized_thread is not None
        assert oversized_thread["message_count"] >= 3
        assert oversized_thread["body_length"] >= 150_000
        assert connection.execute(
            "SELECT 1 FROM messages WHERE subject = ?", [canary]
        ).fetchone()


def test_generator_emits_versioned_manifest_for_the_completed_snapshot(
    tmp_path: Path,
) -> None:
    database_path, canary_path = _build_test_fixture(tmp_path)
    manifest_path = tmp_path / MANIFEST_FILENAME

    assert canary_path.name == CANARY_FILENAME
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
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
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{database_path}{suffix}").exists()


def test_generator_rejects_repository_output_without_test_override(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        build_probe_fixture(tmp_path)


def test_regeneration_preserves_shapes_but_mints_a_new_canary(tmp_path: Path) -> None:
    database_path, canary_path = _build_test_fixture(tmp_path)
    first_canary = canary_path.read_text(encoding="utf-8").strip()
    first_manifest = json.loads(
        (tmp_path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )

    regenerated_database_path, regenerated_canary_path = _build_test_fixture(tmp_path)

    assert regenerated_database_path == database_path
    assert regenerated_canary_path == canary_path
    regenerated_canary = regenerated_canary_path.read_text(encoding="utf-8").strip()
    assert regenerated_canary != first_canary
    regenerated_manifest = json.loads(
        (tmp_path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert regenerated_manifest != first_manifest
    assert regenerated_manifest["artifacts"]["database"]["sha256"] == _sha256(
        regenerated_database_path
    )
    assert regenerated_manifest["artifacts"]["canary"]["sha256"] == _sha256(
        regenerated_canary_path
    )
    with closing(sqlite3.connect(regenerated_database_path)) as connection:
        message_count = connection.execute("SELECT count(*) FROM messages").fetchone()
        assert message_count == (6,)


def test_fixture_delivered_record_and_thread_exceed_response_budget(
    tmp_path: Path,
) -> None:
    database_path, _ = _build_test_fixture(tmp_path)

    _assert_fixture_delivery_exceeds_budget(database_path)


def test_fixture_validator_rejects_an_undersized_record(tmp_path: Path) -> None:
    database_path, _ = _build_test_fixture(tmp_path)

    with pytest.raises(AssertionError):
        _assert_fixture_delivery_exceeds_budget(
            database_path,
            record_subject="探針測試主旨",
        )


def test_fixture_artifacts_are_ignored_but_sources_are_not() -> None:
    for generated_artifact in (
        DATABASE_FILENAME,
        CANARY_FILENAME,
        MANIFEST_FILENAME,
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", generated_artifact],
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        assert result.returncode == 0, generated_artifact

    for tracked_source in (
        "scripts/build_probe_fixture_db.py",
        "tests/test_probe_fixture.py",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", tracked_source],
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        assert result.returncode == 1, tracked_source

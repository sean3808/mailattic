"""Build the generated mail database used by supervised MCP probes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from collections.abc import Sequence
from pathlib import Path

from mailattic.models import PstSource, build_normalized_message
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DATABASE_FILENAME = "probe-fixture.db"
CANARY_FILENAME = "probe-fixture.canary"
MANIFEST_FILENAME = "probe-fixture.manifest.json"
MANIFEST_SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_sidecars(database_path: Path) -> tuple[Path, ...]:
    return tuple(
        Path(f"{database_path}{suffix}") for suffix in ("-wal", "-shm", "-journal")
    )


def _reject_sqlite_sidecars(database_path: Path) -> None:
    present = [path.name for path in _sqlite_sidecars(database_path) if path.exists()]
    if present:
        raise ValueError(
            f"Probe fixture has SQLite sidecar files: {', '.join(present)}"
        )


def _fixture_messages(canary: str) -> list[dict[str, object]]:
    base_message: dict[str, object] = {
        "sender": {"name": "Probe Sender", "email": "probe@example.invalid"},
        "to": [{"name": "Probe Reader", "email": "reader@example.invalid"}],
        "sent_at": "2026-08-01T09:00:00+08:00",
    }
    messages: list[dict[str, object]] = [
        {
            **base_message,
            "subject": "探針測試主旨",
            "body_text": "探針測試內容：這是生成資料，不是真實郵件。",
            "message_id": "<probe-cjk@example.invalid>",
            "attachments": [
                {
                    "attachment_name": "probe-report.pdf",
                    "content_hash": "probe-fixture-content-hash",
                    "size_bytes": 4096,
                    "mime_type": "application/pdf",
                    "storage_path": "fixture/probe-report.pdf",
                }
            ],
        },
        {
            **base_message,
            "subject": "Probe oversized record",
            "body_text": "R" * 150_000,
            "message_id": "<probe-oversized-record@example.invalid>",
        },
    ]
    for index in range(3):
        thread_message: dict[str, object] = {
            **base_message,
            "subject": f"Probe oversized thread {index + 1}",
            "body_text": "T" * 60_000,
            "message_id": f"<probe-oversized-thread-{index + 1}@example.invalid>",
        }
        if index:
            thread_message["in_reply_to"] = (
                f"<probe-oversized-thread-{index}@example.invalid>"
            )
        messages.append(thread_message)
    messages.append(
        {
            **base_message,
            "subject": canary,
            "body_text": "Generated probe canary envelope.",
            "message_id": f"<probe-canary-{secrets.token_hex(12)}@example.invalid>",
        }
    )
    return messages


def _validate_output_directory(
    output_directory: Path, *, allow_repository_output_for_test: bool
) -> Path:
    if not output_directory.is_absolute():
        raise ValueError("Probe fixture output directory must be an absolute path.")
    resolved = output_directory.resolve()
    if not allow_repository_output_for_test and resolved.is_relative_to(
        REPOSITORY_ROOT
    ):
        raise ValueError(
            "Probe fixture output directory must be outside the repository."
        )
    return resolved


def build_probe_fixture(
    output_directory: Path,
    *,
    allow_repository_output_for_test: bool = False,
) -> tuple[Path, Path]:
    """Build a fresh fixture and return its database and canary paths."""
    destination = _validate_output_directory(
        output_directory,
        allow_repository_output_for_test=allow_repository_output_for_test,
    )
    destination.mkdir(parents=True, exist_ok=True)
    database_path = destination / DATABASE_FILENAME
    canary_path = destination / CANARY_FILENAME
    manifest_path = destination / MANIFEST_FILENAME
    staging_path = destination / f".{DATABASE_FILENAME}.{secrets.token_hex(8)}.tmp"
    canary = f"PROBE-CANARY-{secrets.token_urlsafe(24)}"

    _reject_sqlite_sidecars(database_path)

    initialize_database(staging_path)
    source = PstSource(
        path=destination / "generated-probe-fixture.pst",
        input_mode="file",
        requested_from=destination / "generated-probe-fixture.pst",
    )
    connection = connect_database(staging_path)
    try:
        for index, raw_message in enumerate(_fixture_messages(canary), start=1):
            message = build_normalized_message(
                raw_message,
                source=source,
                folder_path="Generated Probe Fixture",
                logical_locator=f"generated/{index}",
            )
            persist_message(connection, message)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "delete":
            raise ValueError("Probe fixture could not switch to journal_mode=DELETE.")
    finally:
        connection.close()

    _reject_sqlite_sidecars(staging_path)
    os.replace(staging_path, database_path)
    canary_path.write_text(canary + "\n", encoding="utf-8")
    _reject_sqlite_sidecars(database_path)
    manifest = {
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
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return database_path, canary_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory",
        required=True,
        type=Path,
        help="Absolute directory outside the repository for generated artifacts.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    database_path, canary_path = build_probe_fixture(arguments.output_directory)
    print(f"database={database_path}")
    print(f"canary_file={canary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Issue #28: `mail_attachment_export` copies CAS attachments to a host dir."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mailattic import attachment_store as attachment_store_module
from mailattic import mcp_server as mcp_server_module
from mailattic.attachment_store import write_attachment
from mailattic.mcp_delivery import (
    MCP_RESPONSE_BUDGET_CHARS,
    measure_call_tool_result_chars,
)
from mailattic.mcp_server import create_server
from mailattic.models import PstSource, build_normalized_message
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

PDF_BYTES = b"%PDF-1.4 fixture payload\n"


def _seed(tmp_path: Path, attachments: list[dict[str, object]]) -> Path:
    database_path = initialize_database(tmp_path / "mail.db")
    source = PstSource(
        path=Path("C:/mail/archive.pst"),
        input_mode="file",
        requested_from=Path("C:/mail/archive.pst"),
    )
    message = build_normalized_message(
        {
            "subject": "Price declaration",
            "sender": {"email": "sender@example.com"},
            "to": [{"email": "reader@example.com"}],
            "sent_at": "2026-07-24T09:00:00+08:00",
            "body_text": "please sign",
            "message_id": "<pricedecl@example.com>",
            "attachments": attachments,
        },
        source=source,
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    connection = connect_database(database_path)
    try:
        persist_message(connection, message)
        connection.commit()
    finally:
        connection.close()
    return database_path


def _stored_attachment(tmp_path: Path, name: str, payload: bytes) -> dict[str, object]:
    record = write_attachment(
        payload, name, "application/pdf", tmp_path / "attachments"
    )
    return {
        "attachment_name": name,
        "content_hash": record.content_hash,
        "size_bytes": record.size_bytes,
        "mime_type": record.mime_type,
        "storage_path": record.storage_path,
    }


def _attachment_ids(database_path: Path) -> list[int]:
    connection = connect_database(database_path)
    try:
        rows = connection.execute(
            "SELECT id FROM message_attachments ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    return [row["id"] for row in rows]


def _export(server, **arguments):
    result = asyncio.run(server.call_tool("mail_attachment_export", arguments))
    assert not result.is_error, result.content
    return result, result.structured_content


@pytest.fixture(autouse=True)
def _no_attachment_root_override(monkeypatch):
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)


def test_exported_file_hash_matches_content_hash(tmp_path: Path) -> None:
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    (attachment_id,) = _attachment_ids(database_path)
    dest = tmp_path / "out" / "nested"

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=[attachment_id],
        dest_dir=str(dest),
    )

    (item,) = payload["items"]
    assert item["status"] == "exported"
    landed = Path(item["dest_path"])
    assert landed.is_absolute() and landed.parent == dest.resolve()
    assert hashlib.sha256(landed.read_bytes()).hexdigest() == item["content_hash"]
    assert item["content_hash"] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert item["attachment_name"] == "Decl.pdf"
    assert item["size_bytes"] == len(PDF_BYTES)
    # No leftover write-then-move temp file after a successful export.
    assert list(landed.parent.iterdir()) == [landed]


def test_metadata_only_attachment_is_skipped_non_exportable(tmp_path: Path) -> None:
    database_path = _seed(tmp_path, [{"attachment_name": "meta-only.pdf"}])
    (attachment_id,) = _attachment_ids(database_path)

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=[attachment_id],
        dest_dir=str(tmp_path / "out"),
    )

    (item,) = payload["items"]
    assert item["status"] == "skipped-non-exportable"
    assert item["dest_path"] is None
    assert list((tmp_path / "out").iterdir()) == []


def test_existing_destination_is_skipped_and_left_byte_identical(
    tmp_path: Path,
) -> None:
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    (attachment_id,) = _attachment_ids(database_path)
    server = create_server(database_path=database_path)
    dest = tmp_path / "out"
    _, first = _export(server, attachment_ids=[attachment_id], dest_dir=str(dest))
    landed = Path(first["items"][0]["dest_path"])
    landed.write_bytes(b"host file the caller must not be able to replace")

    _, second = _export(server, attachment_ids=[attachment_id], dest_dir=str(dest))

    (item,) = second["items"]
    assert item["status"] == "skipped-existing"
    assert Path(item["dest_path"]) == landed
    assert landed.read_bytes() == b"host file the caller must not be able to replace"


def test_unknown_id_fails_only_that_item(tmp_path: Path) -> None:
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    (attachment_id,) = _attachment_ids(database_path)

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=[999_999, attachment_id],
        dest_dir=str(tmp_path / "out"),
    )

    missing, found = payload["items"]
    assert (missing["attachment_id"], missing["status"]) == (999_999, "failed")
    assert missing["detail"] == "attachment_id not found"
    assert found["status"] == "exported"


@pytest.mark.parametrize("inside", ["", "1c", "new-subdir"])
def test_dest_dir_inside_attachment_store_is_rejected(
    tmp_path: Path, inside: str
) -> None:
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    store = tmp_path / "attachments"
    before = sorted(store.rglob("*"))

    with pytest.raises(ToolError, match="inside the attachment store"):
        asyncio.run(
            create_server(database_path=database_path).call_tool(
                "mail_attachment_export",
                {
                    "attachment_ids": _attachment_ids(database_path),
                    "dest_dir": str(store / inside),
                },
            )
        )
    assert sorted(store.rglob("*")) == before


def test_corrupted_store_file_fails_and_writes_nothing(tmp_path: Path) -> None:
    stored = _stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)
    database_path = _seed(tmp_path, [stored])
    blob = tmp_path / str(stored["storage_path"])
    blob.write_bytes(b"X" * len(PDF_BYTES))  # same size, wrong bytes
    dest = tmp_path / "out"

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=_attachment_ids(database_path),
        dest_dir=str(dest),
    )

    (item,) = payload["items"]
    assert item["status"] == "failed"
    assert "content_hash" in item["detail"]
    assert list(dest.iterdir()) == []


def test_failed_write_leaves_no_partial_file(tmp_path: Path, monkeypatch) -> None:
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    dest = tmp_path / "out"
    real_open = Path.open

    class _FailingHandle:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

        def write(self, data):
            self._handle.write(data[:3])
            raise OSError("disk full")

    def failing_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        return _FailingHandle(handle) if mode == "xb" else handle

    monkeypatch.setattr(Path, "open", failing_open)
    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=_attachment_ids(database_path),
        dest_dir=str(dest),
    )

    (item,) = payload["items"]
    assert item["status"] == "failed"
    assert list(dest.iterdir()) == []


def test_open_failure_before_creating_the_temp_file_never_deletes_a_foreign_file(
    tmp_path: Path, monkeypatch
) -> None:
    """The temp-file `open("xb")` call itself can raise -- e.g. a foreign
    file happens to already sit at that exact (random-uuid) temp path.
    That path was never ours: cleanup must only start once open() has
    actually handed us a handle we created. A cleanup block that also
    wraps the open() call would unlink this foreign file on its way to
    re-raising FileExistsError (issue #28, 2026-09-24 decision)."""
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    dest = tmp_path / "out"
    dest.mkdir(parents=True)
    fixed_uuid = uuid.UUID(int=0)
    monkeypatch.setattr(attachment_store_module.uuid, "uuid4", lambda: fixed_uuid)
    planted_path = dest / f".mailattic-export-{fixed_uuid.hex}.tmp"
    foreign_bytes = b"host file the caller must not be able to replace"
    planted_path.write_bytes(foreign_bytes)

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=_attachment_ids(database_path),
        dest_dir=str(dest),
    )

    (item,) = payload["items"]
    assert item["status"] == "failed"
    assert planted_path.read_bytes() == foreign_bytes


def test_race_between_write_and_move_leaves_foreign_file_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """Finding 1 (Critical, issue #28 round 2): another process may create a
    file at the destination path in the window between our temp-file write
    completing and our move into place. The move must fail without ever
    deleting or overwriting that foreign file -- failure handling only
    unlinks the temp file we created ourselves, never destination_path by
    pathname."""
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    dest = tmp_path / "out"
    foreign_bytes = b"host file the caller must not be able to replace"
    real_rename = Path.rename
    planted = {"done": False}

    def racing_rename(self: Path, target):
        if not planted["done"] and self.name.startswith(".mailattic-export-"):
            planted["done"] = True
            Path(target).write_bytes(foreign_bytes)
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", racing_rename)

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=_attachment_ids(database_path),
        dest_dir=str(dest),
    )

    assert planted["done"], "race hook never fired -- test setup drifted"
    (item,) = payload["items"]
    assert item["status"] == "skipped-existing"
    landed = Path(item["dest_path"])
    assert landed.read_bytes() == foreign_bytes
    # No temp residue either: the only file in dest is the foreign one.
    assert list(landed.parent.iterdir()) == [landed]


def test_repeated_id_is_reported_once(tmp_path: Path) -> None:
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    (attachment_id,) = _attachment_ids(database_path)

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=[attachment_id, attachment_id],
        dest_dir=str(tmp_path / "out"),
    )

    assert [item["status"] for item in payload["items"]] == ["exported"]


@pytest.mark.parametrize("dest_dir", ["relative/out", "out", "C:relative", r"\rooted"])
def test_relative_dest_dir_is_rejected_without_writing(
    tmp_path: Path, dest_dir: str, monkeypatch
) -> None:
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.rglob("*"))

    with pytest.raises(ToolError, match="absolute host path"):
        asyncio.run(
            create_server(database_path=database_path).call_tool(
                "mail_attachment_export",
                {
                    "attachment_ids": _attachment_ids(database_path),
                    "dest_dir": dest_dir,
                },
            )
        )
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("count", [0, 51])
def test_id_count_outside_1_to_50_is_rejected(tmp_path: Path, count: int) -> None:
    database_path = _seed(tmp_path, [])
    with pytest.raises(ToolError, match=r"1\.\.50"):
        asyncio.run(
            create_server(database_path=database_path).call_tool(
                "mail_attachment_export",
                {
                    "attachment_ids": list(range(1, count + 1)),
                    "dest_dir": str(tmp_path / "out"),
                },
            )
        )
    assert not (tmp_path / "out").exists()


def test_fifty_long_named_exports_fit_the_delivery_budget(tmp_path: Path) -> None:
    long_name = "N" * 400 + ".pdf"
    database_path = _seed(
        tmp_path,
        [
            _stored_attachment(tmp_path, long_name, PDF_BYTES + bytes([i]))
            for i in range(50)
        ],
    )
    ids = _attachment_ids(database_path)
    # Longest accepted dest_dir, made of many separators so JSON escaping of
    # backslashes counts at its worst.
    dest = tmp_path / "d"
    while len(str(dest)) < 196:
        dest = dest / "d"

    result, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=ids,
        dest_dir=str(dest),
    )

    assert [item["status"] for item in payload["items"]] == ["exported"] * 50
    assert measure_call_tool_result_chars(result) <= MCP_RESPONSE_BUDGET_CHARS


def test_over_budget_backstop_says_files_may_be_written(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    monkeypatch.setattr(mcp_server_module, "MCP_RESPONSE_BUDGET_CHARS", 50)

    with pytest.raises(ToolError, match="may already be written"):
        asyncio.run(
            create_server(database_path=database_path).call_tool(
                "mail_attachment_export",
                {
                    "attachment_ids": _attachment_ids(database_path),
                    "dest_dir": str(tmp_path / "out"),
                },
            )
        )


def test_export_does_not_modify_database_or_store(tmp_path: Path) -> None:
    database_path = _seed(
        tmp_path, [_stored_attachment(tmp_path, "Decl.pdf", PDF_BYTES)]
    )
    store = tmp_path / "attachments"
    snapshot = {
        path: path.read_bytes()
        for path in [database_path, *store.rglob("*")]
        if path.is_file()
    }

    _export(
        create_server(database_path=database_path),
        attachment_ids=_attachment_ids(database_path),
        dest_dir=str(tmp_path / "out"),
    )

    assert {
        path: path.read_bytes()
        for path in [database_path, *store.rglob("*")]
        if path.is_file()
    } == snapshot

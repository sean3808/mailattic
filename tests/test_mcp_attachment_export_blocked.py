"""#205: MCP export never lands a file Windows would run on open.

Threat (D-20260908-1, #28 2026-09-24): CoWork, steered by mail content, picks
``dest_dir`` -- e.g. the Startup folder -- and exports a sender-controlled
``.bat``/``.lnk``/``.exe``, which then runs at the next logon. The guard is on
the file type, not the folder: auto-run locations cannot be enumerated.
Decision (A), Sean 2026-09-29: block on the MCP path only; the CLI
``bes attachments --copy-to`` is unchanged.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mailattic import attachment_store as attachment_store_module
from mailattic.attachment_store import export_attachments, write_attachment
from mailattic.mcp_server import create_server
from mailattic.mcp_writer import MailWriter, _lands_in
from tests.test_mcp_attachment_export import _attachment_ids, _export, _seed

PAYLOAD = b"@echo off\r\necho payload\r\n"


@pytest.fixture(autouse=True)
def _no_attachment_root_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)


def _stored(
    tmp_path: Path,
    name: str,
    payload: bytes = PAYLOAD,
    mime_type: str = "application/octet-stream",
) -> dict[str, object]:
    record = write_attachment(payload, name, mime_type, tmp_path / "attachments")
    return {
        "attachment_name": name,
        "content_hash": record.content_hash,
        "size_bytes": record.size_bytes,
        "mime_type": mime_type,
        "storage_path": record.storage_path,
    }


def _files_in(directory: Path) -> list[Path]:
    return sorted(directory.iterdir()) if directory.exists() else []


@pytest.mark.parametrize(
    "name",
    [
        "setup.exe",
        "run.bat",
        "run.cmd",
        "invoice.lnk",
        "macro.vbs",
        "script.ps1",
        "page.hta",
        "screensaver.scr",
        "installer.msi",
        "tool.js",
        "job.wsf",
        "link.url",
        "Setup.EXE",  # case
        "invoice.pdf.exe",  # double extension: the last one is what runs
        "invoice\u202efdp.exe",  # right-to-left override display trick
        "startup.pth",  # site-packages runs it at every Python start
        "cached.pyc",
        "update.msixbundle",
        "office.theme",
        "component.sct",
        "addin.xll",
        "deck.ppam",
        "pointer.appcontent-ms",
        "patch.msu",
        "office.vsto",
        "run.vb\u017f",  # long s: Windows upper-cases it to .VBS
        "setup.ms\u0131",  # dotless i: Windows upper-cases it to .MSI
    ],
)
def test_runnable_file_type_is_blocked_and_nothing_is_written(
    tmp_path: Path, name: str
) -> None:
    database_path = _seed(tmp_path, [_stored(tmp_path, name)])
    (attachment_id,) = _attachment_ids(database_path)
    dest = tmp_path / "Startup"

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=[attachment_id],
        dest_dir=str(dest),
    )

    (item,) = payload["items"]
    assert item["status"] == "skipped-blocked-type"
    assert item["dest_path"] is None
    assert "Outlook" in item["detail"]
    assert _files_in(dest) == []


@pytest.mark.parametrize("name", ["run.bat.", "run.bat ", "run.bat . ", "RUN.Bat"])
def test_trailing_dots_spaces_and_case_never_land_a_batch_file(
    tmp_path: Path, name: str
) -> None:
    # Windows drops trailing dots and spaces from a name. Depending on the
    # export naming rules such a name is either blocked or re-named to a
    # non-runnable .bin -- what must never happen is a .bat on disk.
    database_path = _seed(tmp_path, [_stored(tmp_path, name)])
    (attachment_id,) = _attachment_ids(database_path)

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=[attachment_id],
        dest_dir=str(tmp_path / "out"),
    )

    (item,) = payload["items"]
    assert item["status"] in {"skipped-blocked-type", "exported"}
    landed = [p.name.rstrip(" .").lower() for p in _files_in(tmp_path / "out")]
    assert not any(n.endswith(".bat") for n in landed)


def test_extensionless_attachment_is_judged_by_the_name_it_would_land_as(
    tmp_path: Path,
) -> None:
    # No usable extension: the export name comes from the MIME type, which
    # can itself be runnable (text/javascript -> .js).
    database_path = _seed(
        tmp_path, [_stored(tmp_path, "README", mime_type="text/javascript")]
    )
    (attachment_id,) = _attachment_ids(database_path)

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=[attachment_id],
        dest_dir=str(tmp_path / "out"),
    )

    assert payload["items"][0]["status"] == "skipped-blocked-type"
    assert _files_in(tmp_path / "out") == []


def test_mime_inferred_python_bytecode_is_blocked(tmp_path: Path) -> None:
    database_path = _seed(
        tmp_path,
        [_stored(tmp_path, "README", mime_type="application/x-python-code")],
    )
    (attachment_id,) = _attachment_ids(database_path)

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=[attachment_id],
        dest_dir=str(tmp_path / "out"),
    )

    assert payload["items"][0]["status"] == "skipped-blocked-type"
    assert _files_in(tmp_path / "out") == []


@pytest.mark.parametrize(
    "folder",
    [
        ("Excel", "XLSTART"),
        ("Word", "STARTUP"),
        ("Windows", "Start Menu", "Programs", "Startup"),
        ("Templates",),  # default trusted location: macros run on open
        ("Addins",),
        ("excel", "xlstart", "nested"),  # case-insensitive, and below it
    ],
)
def test_folders_that_open_their_files_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: tuple[str, ...]
) -> None:
    # Office trusts its startup folders, so a macro workbook there runs with
    # no prompt; blocking .xls/.xlsm everywhere would stop ordinary supplier
    # spreadsheets instead. Nobody exports attachments into these on purpose.
    appdata = tmp_path / "AppData" / "Roaming"
    (appdata / "Microsoft" / "Excel" / "XLSTART").mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    database_path = _seed(
        tmp_path,
        [_stored(tmp_path, "report.xlsm", b"PK fake", "application/vnd.ms-excel")],
    )
    (attachment_id,) = _attachment_ids(database_path)
    dest = appdata / "Microsoft"
    for part in folder:
        dest = dest / part

    with pytest.raises(ToolError, match="open automatically"):
        asyncio.run(
            create_server(database_path=database_path).call_tool(
                "mail_attachment_export",
                {"attachment_ids": [attachment_id], "dest_dir": str(dest)},
            )
        )

    assert not any(p.is_file() for p in (appdata / "Microsoft").rglob("*"))


def test_refusal_falls_back_to_profile_when_appdata_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A launcher that trims the environment must not switch the refusal off.
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    database_path = _seed(
        tmp_path,
        [_stored(tmp_path, "report.xlsm", b"PK fake", "application/vnd.ms-excel")],
    )
    (attachment_id,) = _attachment_ids(database_path)
    dest = tmp_path / "AppData" / "Roaming" / "Microsoft" / "Excel" / "XLSTART"

    with pytest.raises(ToolError, match="open automatically"):
        asyncio.run(
            create_server(database_path=database_path).call_tool(
                "mail_attachment_export",
                {"attachment_ids": [attachment_id], "dest_dir": str(dest)},
            )
        )


def test_folder_identity_catches_spellings_that_differ_from_the_folder(
    tmp_path: Path,
) -> None:
    # Windows reaches one folder under several spellings (\\?\ prefix,
    # trailing dots, 8.3 names): the spelling does not match, the
    # file-system identity does. Windows uses the real spellings (creating a
    # symlink there needs a privilege a normal account lacks); elsewhere a
    # symlink alias stands in for them.
    folder = tmp_path / "Roaming" / "Microsoft" / "Excel" / "XLSTART"
    folder.mkdir(parents=True)
    (folder / "nested").mkdir()
    if os.name == "nt":
        aliases = [Path("\\\\?\\" + str(folder)), Path(str(folder) + ".")]
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(folder, target_is_directory=True)
        aliases = [alias]

    for alias in aliases:
        assert str(alias) != str(folder)
        assert _lands_in(alias / "nested", folder), alias
    assert not _lands_in(tmp_path / "elsewhere", folder)


def test_ordinary_folder_under_appdata_still_exports_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    database_path = _seed(
        tmp_path,
        [_stored(tmp_path, "report.xlsm", b"PK fake", "application/vnd.ms-excel")],
    )
    (attachment_id,) = _attachment_ids(database_path)

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=[attachment_id],
        dest_dir=str(appdata / "Microsoft" / "Excel" / "exports"),
    )

    assert payload["items"][0]["status"] == "exported"


def test_mixed_batch_exports_documents_and_blocks_runnables_in_order(
    tmp_path: Path,
) -> None:
    database_path = _seed(
        tmp_path,
        [
            _stored(tmp_path, "catalog.pdf", b"%PDF-1.4 catalog\n", "application/pdf"),
            _stored(tmp_path, "update.exe", b"MZ fake"),
            _stored(tmp_path, "invoice.exe.pdf", b"%PDF-1.4 inv\n", "application/pdf"),
        ],
    )
    ids = _attachment_ids(database_path)
    dest = tmp_path / "out"

    _, payload = _export(
        create_server(database_path=database_path),
        attachment_ids=ids,
        dest_dir=str(dest),
    )

    assert [item["attachment_id"] for item in payload["items"]] == ids
    assert [item["status"] for item in payload["items"]] == [
        "exported",
        "skipped-blocked-type",
        "exported",
    ]
    landed = {
        Path(item["dest_path"]).name for item in payload["items"] if item["dest_path"]
    }
    assert {path.name for path in _files_in(dest)} == landed
    assert not any(name.endswith(".exe") for name in landed)


def test_writer_blocks_regardless_of_the_folder(tmp_path: Path) -> None:
    # Direct accessor check: the block is by type, so any folder -- not just
    # a known auto-run one -- gets the same answer.
    database_path = _seed(tmp_path, [_stored(tmp_path, "run.bat")])
    writer = MailWriter.for_database(database_path)
    row = {
        "attachment_id": _attachment_ids(database_path)[0],
        **_stored(tmp_path, "run.bat"),
        "sent_at": "2026-07-24T09:00:00+08:00",
    }

    (outcome,) = writer.export_attachments([row], tmp_path / "Documents")

    assert outcome.status == "skipped-blocked-type"
    assert outcome.destination_path is None


def test_cli_shared_export_still_copies_runnable_types(tmp_path: Path) -> None:
    # Decision (A): only the MCP path blocks. The shared function the CLI
    # calls keeps exporting any type the user asks for.
    stored = _stored(tmp_path, "setup.exe")
    row = {"attachment_id": 1, "sent_at": "2026-07-24T09:00:00+08:00", **stored}

    result = export_attachments(
        attachment_rows=[row],
        store_root=tmp_path / "attachments",
        destination_dir=tmp_path / "cli-out",
    )

    assert result.copied == 1
    (landed,) = _files_in(tmp_path / "cli-out")
    assert landed.suffix == ".exe"


def test_blocked_type_check_does_not_touch_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A blocked row is decided from metadata alone; its bytes are never read.
    database_path = _seed(tmp_path, [_stored(tmp_path, "run.bat")])
    (attachment_id,) = _attachment_ids(database_path)
    reads: list[str] = []
    original = attachment_store_module.read_attachment

    def spy(storage_path: str, store_root: Path) -> bytes:
        reads.append(storage_path)
        return original(storage_path, store_root)

    monkeypatch.setattr(attachment_store_module, "read_attachment", spy)

    _export(
        create_server(database_path=database_path),
        attachment_ids=[attachment_id],
        dest_dir=str(tmp_path / "out"),
    )

    assert reads == []

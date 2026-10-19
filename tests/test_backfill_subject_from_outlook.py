"""Tests for scripts/backfill_subject_from_outlook.py (issue #219, PST rows).

Rows are seeded through the production ``build_normalized_message`` /
``persist_message`` path with a broken subject and no raw_headers (the
readpst shape). Outlook is replaced by a fake COM surface — Stores, folders,
``Items.Find`` on a DASL Message-ID filter, ``PropertyAccessor`` — driven
through the script's real ``OutlookLocator``.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backfill_subject_from_outlook as script_module  # noqa: E402
from backfill_subject_from_outlook import (  # noqa: E402
    OutlookLocator,
    compute_plan,
    run,
)

from mailattic.models import (  # noqa: E402
    PstSource,
    build_normalized_message,
)
from mailattic.outlook_pst import (  # noqa: E402
    PR_INTERNET_MESSAGE_ID,
    PR_SENDER_SMTP_ADDRESS,
    PR_TRANSPORT_MESSAGE_HEADERS,
)
from mailattic.storage import (  # noqa: E402
    connect_database,
    initialize_database,
    persist_message,
)

_GOOD = "RE: 海灣甲嶽 PAPER-A4-80"
_BROKEN = _GOOD.encode("gbk").decode("gb2312", "replace")
_SENDER = "a@example.com"


def _headers(message_id: str, subject: str = _GOOD, charset: str = "gbk") -> str:
    encoded = base64.b64encode(subject.encode(charset)).decode("ascii")
    # Outlook hands back LF-only lines; the script must normalize them.
    return (
        f"From: {_SENDER}\nSubject: =?{charset}?B?{encoded}?=\n"
        f"Message-ID: {message_id}\n"
    )


class _Accessor:
    def __init__(self, props: dict[str, Any]) -> None:
        self._props = props

    def GetProperty(self, name: str) -> Any:  # noqa: N802 — COM surface
        if name not in self._props:
            raise OSError("property not found")
        return self._props[name]


class _Item:
    def __init__(
        self,
        mid: str,
        *,
        sender: str = _SENDER,
        smtp: str | None = None,
        headers: str | None = None,
    ) -> None:
        self.SenderEmailAddress = sender
        props: dict[str, Any] = {PR_INTERNET_MESSAGE_ID: mid}
        if smtp:
            props[PR_SENDER_SMTP_ADDRESS] = smtp
        props[PR_TRANSPORT_MESSAGE_HEADERS] = (
            _headers(mid) if headers is None else headers
        )
        self.PropertyAccessor = _Accessor(props)
        self.mid = mid


class _Items:
    def __init__(self, items: list[_Item]) -> None:
        self._by_mid = {i.mid: i for i in items}

    def Find(self, dasl: str) -> _Item | None:  # noqa: N802
        assert dasl.startswith(f'@SQL="{PR_INTERNET_MESSAGE_ID}" = \'')
        mid = dasl.split(" = '", 1)[1][:-1].replace("''", "'")
        return self._by_mid.get(mid)


class _Folders:
    def __init__(self, folders: list[_Folder]) -> None:
        self._folders = folders
        self.Count = len(folders)

    def Item(self, key: int | str) -> _Folder:  # noqa: N802
        if isinstance(key, int):
            return self._folders[key - 1]
        for f in self._folders:
            if f.Name == key:
                return f
        raise OSError(f"folder {key!r} not found")


class _Folder:
    def __init__(
        self, name: str, items: list[_Item] = (), subs: list[_Folder] = ()
    ) -> None:
        self.Name = name
        self.Items = _Items(list(items))
        self.Folders = _Folders(list(subs))


class _Store:
    def __init__(self, path: str, root: _Folder) -> None:
        self.FilePath = path
        self._root = root

    def GetRootFolder(self) -> _Folder:  # noqa: N802
        return self._root


class _Stores:
    def __init__(self, stores: list[_Store]) -> None:
        self._stores = stores
        self.Count = len(stores)

    def Item(self, index: int) -> _Store:  # noqa: N802
        return self._stores[index - 1]


class _Namespace:
    def __init__(self, stores: list[_Store]) -> None:
        self.Stores = _Stores(stores)


def _seed(
    db: Path,
    *,
    n: int,
    message_id: str | None,
    folder: str,
    pst: str = "/root/work/pst/Mail.pst",
) -> int:
    msg = build_normalized_message(
        {
            "subject": _BROKEN,
            "sender": {"name": None, "email": _SENDER},
            "to": [{"name": None, "email": "b@example.com"}],
            "cc": [],
            "bcc": [],
            "sent_at": f"2024-01-{n:02d}T12:00:00+00:00",
            "received_at": None,
            "body_text": f"body {n}",
            "attachments": [],
            "attachment_names": [],
            "message_id": message_id,
            "in_reply_to": None,
            "references": None,
            "raw_headers": None,
        },
        source=PstSource(path=Path(pst), input_mode="file", requested_from=Path(pst)),
        folder_path=folder,
        logical_locator=f"{folder}/mbox#{n}",
    )
    conn = connect_database(db)
    try:
        stored = persist_message(conn, msg).stored_message_id
        conn.commit()
    finally:
        conn.close()
    return stored


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, dict[str, int], _Namespace]:
    db = tmp_path / "fixture.db"
    initialize_database(db)
    ids = {
        "in_folder": _seed(db, n=1, message_id="<a@x>", folder="acct/Inbox/Stationery"),
        # readpst stored "乙組/丙組" as "乙組_丙組": pass 1 misses, tree walk finds it.
        "renamed": _seed(db, n=2, message_id="<b@x>", folder="acct/Inbox/乙組_丙組"),
        "smtp_fallback": _seed(db, n=3, message_id="<c'q@x>", folder="acct/Inbox"),
        "sender_mismatch": _seed(db, n=4, message_id="<d@x>", folder="acct/Inbox"),
        "source_broken": _seed(db, n=5, message_id="<e@x>", folder="acct/Inbox"),
        "missing": _seed(db, n=6, message_id="<f@x>", folder="acct/Inbox"),
        "no_mid": _seed(db, n=7, message_id=None, folder="acct/Inbox"),
        "unmounted": _seed(
            db, n=8, message_id="<g@x>", folder="acct/Inbox", pst="/root/Other.pst"
        ),
    }
    root = _Folder(
        "acct",
        subs=[
            _Folder(
                "Inbox",
                items=[
                    _Item("<c'q@x>", sender="/O=EX/CN=A", smtp=_SENDER.upper()),
                    _Item("<d@x>", sender="someone@else.com"),
                    _Item("<e@x>", headers=_headers("<e@x>", _BROKEN, "utf-8")),
                ],
                subs=[
                    _Folder("Stationery", items=[_Item("<a@x>")]),
                    _Folder("乙組/丙組", items=[_Item("<b@x>")]),
                ],
            )
        ],
    )
    ns = _Namespace([_Store(r"C:\Users\U\Outlook\mail.PST", root), _Store("", root)])
    return db, ids, ns


def _subject(db: Path, db_id: int) -> tuple[str, str]:
    conn = connect_database(db)
    try:
        row = conn.execute(
            "SELECT subject FROM messages WHERE id = ?", [db_id]
        ).fetchone()
        fts = conn.execute(
            "SELECT subject FROM message_fts WHERE rowid = ?", [db_id]
        ).fetchone()
        return row["subject"], fts["subject"]
    finally:
        conn.close()


def test_dry_run_buckets_every_row_and_writes_nothing(world, capsys) -> None:
    db, ids, ns = world
    assert run(db, confirm=False, locator=OutlookLocator(ns)) == 0
    out = capsys.readouterr().out
    for key in ("in_folder", "renamed", "smtp_fallback"):
        assert f"[repair] db_id={ids[key]}" in out
    assert f"sender_mismatch: 1 row(s): [{ids['sender_mismatch']}]" in out
    assert f"still_broken_at_source: 1 row(s): [{ids['source_broken']}]" in out
    assert f"not_found_in_outlook: 1 row(s): [{ids['missing']}]" in out
    assert f"no_message_id: 1 row(s): [{ids['no_mid']}]" in out
    assert f"pst_not_mounted:other.pst: 1 row(s): [{ids['unmounted']}]" in out
    assert f"db_id={ids['in_folder']} found_in='acct/Inbox/Stationery'" in out
    assert f"db_id={ids['renamed']} found_in='acct/Inbox/乙組/丙組'" in out
    assert "[dedup_key] changed=0" in out
    assert "[summary] repair=3 " in out
    assert all(_subject(db, i)[0] == _BROKEN for i in ids.values())


def test_confirm_repairs_only_verified_rows_then_is_idempotent(world, tmp_path) -> None:
    db, ids, ns = world
    assert (
        run(
            db,
            confirm=True,
            locator=OutlookLocator(ns),
            backup_path=tmp_path / "snap.db",
        )
        == 0
    )
    assert (tmp_path / "snap.db").exists()
    for key in ("in_folder", "renamed", "smtp_fallback"):
        subject, fts = _subject(db, ids[key])
        assert subject == _GOOD
        assert "灣" in fts and "�" not in fts
    for key in ("sender_mismatch", "source_broken", "missing", "no_mid", "unmounted"):
        assert _subject(db, ids[key])[0] == _BROKEN
    conn = connect_database(db)
    try:
        assert compute_plan(conn, OutlookLocator(ns)).repairs == []
    finally:
        conn.close()


def test_confirm_aborts_without_writing_when_backup_fails(world, monkeypatch) -> None:
    db, ids, ns = world

    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(script_module, "backup_database", _raise)
    assert run(db, confirm=True, locator=OutlookLocator(ns)) == 1
    assert _subject(db, ids["in_folder"])[0] == _BROKEN


def test_outlook_not_running_exits_1_without_touching_db(
    world, monkeypatch, capsys
) -> None:
    db, ids, _ = world

    def _not_running() -> None:
        raise RuntimeError("Outlook is not running.")

    monkeypatch.setattr(script_module, "_load_outlook_namespace", _not_running)
    assert run(db, confirm=True) == 1
    assert "Outlook is not running" in capsys.readouterr().err
    assert _subject(db, ids["in_folder"])[0] == _BROKEN

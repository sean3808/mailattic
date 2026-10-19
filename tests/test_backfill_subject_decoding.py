"""Tests for scripts/backfill_subject_decoding.py (issue #219).

Seeds a real schema through the production ``build_normalized_message`` /
``persist_message`` path with three broken-subject rows: one repairable from
raw_headers (gb2312 declared, GBK bytes), one with a
fallback dedup_key (must be skipped), one without raw_headers (unrecoverable).
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backfill_subject_decoding as script_module  # noqa: E402
from backfill_subject_decoding import run  # noqa: E402

from mailattic.models import (  # noqa: E402
    PstSource,
    build_normalized_message,
)
from mailattic.parser import _extract_raw_headers  # noqa: E402
from mailattic.storage import (  # noqa: E402
    connect_database,
    initialize_database,
    persist_message,
)

_SOURCE = PstSource(
    path=Path("dummy.pst"), input_mode="maildir", requested_from=Path("dummy")
)
_GOOD = "RE: 海灣甲嶽 PAPER-A4-80"
_BROKEN = _GOOD.encode("gbk").decode("gb2312", "replace")


def _raw_headers(message_id: str) -> str:
    encoded = base64.b64encode(_GOOD.encode("gbk")).decode("ascii")
    raw = (
        f"From: a@example.com\r\nTo: b@example.com\r\n"
        f"Subject: =?gb2312?B?{encoded}?=\r\nMessage-ID: {message_id}\r\n"
        f"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n\r\nbody\r\n"
    ).encode("ascii")
    headers = _extract_raw_headers(raw)
    assert headers is not None
    return headers


def _seed(
    db_path: Path, *, message_id: str | None, raw_headers: str | None, n: int
) -> int:
    msg = build_normalized_message(
        {
            "subject": _BROKEN,
            "sender": {"name": None, "email": "a@example.com"},
            "to": [{"name": None, "email": "b@example.com"}],
            "cc": [],
            "bcc": [],
            "sent_at": f"2024-01-0{n}T12:00:00+00:00",
            "received_at": None,
            "body_text": f"body {n}",
            "attachments": [],
            "attachment_names": [],
            "message_id": message_id,
            "in_reply_to": None,
            "references": None,
            "raw_headers": raw_headers,
        },
        source=_SOURCE,
        folder_path="INBOX",
        logical_locator=f"INBOX/{n}",
    )
    conn = connect_database(db_path)
    try:
        stored = persist_message(conn, msg).stored_message_id
        conn.commit()
    finally:
        conn.close()
    return stored


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    db = tmp_path / "fixture.db"
    initialize_database(db)
    ids = {
        "repair": _seed(db, message_id="<r@x>", raw_headers=_raw_headers("<r@x>"), n=1),
        "fallback": _seed(db, message_id=None, raw_headers=_raw_headers(""), n=2),
        "lost": _seed(db, message_id="<l@x>", raw_headers=None, n=3),
    }
    return db, ids


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


def test_dry_run_reports_all_three_buckets_and_writes_nothing(seeded, capsys) -> None:
    db, ids = seeded
    assert run(db, confirm=False) == 0
    out = capsys.readouterr().out
    assert f"[repair] db_id={ids['repair']}" in out
    assert f"[skip] db_id={ids['fallback']}" in out
    assert f"[{ids['lost']}]" in out
    assert "repair=1 skip_fallback_key=1 unrecoverable=1" in out
    assert _subject(db, ids["repair"])[0] == _BROKEN


def test_confirm_repairs_subject_and_fts_only_for_message_id_rows(
    seeded, tmp_path
) -> None:
    db, ids = seeded
    assert run(db, confirm=True, backup_path=tmp_path / "snap.db") == 0
    assert (tmp_path / "snap.db").exists()
    subject, fts = _subject(db, ids["repair"])
    assert subject == _GOOD
    assert "灣" in fts and "�" not in fts
    assert _subject(db, ids["fallback"])[0] == _BROKEN
    assert _subject(db, ids["lost"])[0] == _BROKEN
    # Idempotent: nothing left to repair.
    conn = connect_database(db)
    try:
        assert script_module.compute_plan(conn).repairs == []
    finally:
        conn.close()


def test_confirm_aborts_without_writing_when_backup_fails(seeded, monkeypatch) -> None:
    db, ids = seeded

    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(script_module, "backup_database", _raise)
    assert run(db, confirm=True) == 1
    assert _subject(db, ids["repair"])[0] == _BROKEN

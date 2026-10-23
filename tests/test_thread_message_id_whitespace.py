"""#22 -- a legal Message-ID with outer whitespace must not split threads."""

from __future__ import annotations

import itertools
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "src", REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from backfill_thread_key import run_backfill  # noqa: E402

from mailattic.outbound import _get_message_by_rfc822_message_id  # noqa: E402
from mailattic.query import resolve_thread_message_ids  # noqa: E402
from mailattic.storage import initialize_database, persist_message  # noqa: E402
from mailattic.thread_key import compute_thread_keys  # noqa: E402
from tests.test_ingestion_thread_key import make_message  # noqa: E402

ROOT_MID = "<root@example.test>  "  # parser keeps trailing whitespace
CHILD_MID = "<child@example.test>"


def _insert(db: Path, order: tuple[str, str]) -> dict[str, int]:
    ids: dict[str, int] = {}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        for name in order:
            if name == "root":
                msg = make_message(ROOT_MID)
            else:
                msg = make_message(CHILD_MID, in_reply_to="<root@example.test>")
            ids[name] = persist_message(conn, msg).stored_message_id
        conn.commit()
    finally:
        conn.close()
    return ids


@pytest.mark.parametrize("order", list(itertools.permutations(("root", "child"))))
def test_incremental_thread_survives_outer_whitespace(
    tmp_path: Path, order: tuple[str, str]
) -> None:
    db = initialize_database(tmp_path / "t.db")
    ids = _insert(db, order)
    both = sorted(ids.values())
    assert sorted(resolve_thread_message_ids(db, ids["root"])) == both
    assert sorted(resolve_thread_message_ids(db, ids["child"])) == both
    # RFC form, bare form, and the parser's verbatim (whitespace) form.
    for ident in ("<root@example.test>", "root@example.test", ROOT_MID):
        assert sorted(resolve_thread_message_ids(db, ident)) == both


def test_bulk_matches_incremental_and_recompute_repairs_stale_cache(
    tmp_path: Path,
) -> None:
    assert compute_thread_keys(
        [(1, ROOT_MID, None, None), (2, CHILD_MID, "<root@example.test>", None)]
    ) == {1: 1, 2: 1}

    db = initialize_database(tmp_path / "t.db")
    ids = _insert(db, ("root", "child"))
    conn = sqlite3.connect(db)
    try:  # simulate a cache computed by the old, buggy code
        conn.execute("UPDATE messages SET thread_key = id")
        conn.commit()
    finally:
        conn.close()
    assert resolve_thread_message_ids(db, ids["root"]) == [ids["root"]]
    run_backfill(db)
    assert sorted(resolve_thread_message_ids(db, ids["root"])) == sorted(ids.values())


def test_message_id_match_stays_case_sensitive(tmp_path: Path) -> None:
    db = initialize_database(tmp_path / "t.db")
    _insert(db, ("root", "child"))
    with pytest.raises(KeyError):
        resolve_thread_message_ids(db, "<ROOT@example.test>")


def test_draft_anchor_lookup_finds_whitespace_message_id(tmp_path: Path) -> None:
    db = initialize_database(tmp_path / "t.db")
    ids = _insert(db, ("root", "child"))
    found = _get_message_by_rfc822_message_id(db, "<root@example.test>")
    assert int(found["id"]) == ids["root"]  # type: ignore[call-overload]

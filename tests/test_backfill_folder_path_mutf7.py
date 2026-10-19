"""Tests for scripts/backfill_folder_path_mutf7.py.

Covers the acceptance criteria in
openspec/changes/add-mutf7-folder-codec/tasks.md task 4.1 and design.md
Decision 4: dry-run reports the affected IMAP-sourced rows without writing,
a confirmed run repairs `folder_path` / `pst_source` / `logical_locator`
(only the folder-name segment), a second confirmed run is a no-op, and a
PST-sourced row is never touched even when its folder text resembles a
modified UTF-7 shift sequence.

Uses a temporary SQLite database built from the real schema
(`initialize_database`); this test never opens the real
mailattic.db.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from backfill_folder_path_mutf7 import (  # noqa: E402
    compute_backfill_plan,
    run_backfill,
)

from mailattic.storage import (  # noqa: E402
    connect_database,
    initialize_database,
)

_WIRE_FOLDER = "&W8RO9lCZTv0-"
_DISPLAY_FOLDER = "寄件備份"


def _insert_message(
    db_path: Path,
    *,
    dedup_key: str,
    pst_source: str,
    folder_path: str,
    logical_locator: str,
) -> int:
    conn = connect_database(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO messages (
                subject, sender_email, body_text, pst_source, folder_path,
                logical_locator, dedup_key, recipient_search_text
            ) VALUES (?, ?, '', ?, ?, ?, ?, '')
            """,
            (
                f"fixture for {dedup_key}",
                "someone@example.com",
                pst_source,
                folder_path,
                logical_locator,
                dedup_key,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _fetch_row(db_path: Path, db_id: int) -> dict:
    conn = connect_database(db_path)
    try:
        row = conn.execute(
            "SELECT folder_path, pst_source, logical_locator FROM messages "
            "WHERE id = ?",
            [db_id],
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


@pytest.fixture
def db_with_imap_row(tmp_path: Path) -> tuple[Path, int]:
    db_path = tmp_path / "fixture.db"
    initialize_database(db_path)
    db_id = _insert_message(
        db_path,
        dedup_key="imap-row-dedup",
        pst_source=f"imap:sean3808@mailattic.test:{_WIRE_FOLDER}",
        folder_path=_WIRE_FOLDER,
        logical_locator=f"{_WIRE_FOLDER}/uid-1.eml",
    )
    return db_path, db_id


def test_dry_run_reports_but_does_not_write(
    db_with_imap_row: tuple[Path, int],
) -> None:
    db_path, db_id = db_with_imap_row

    exit_code = run_backfill(db_path, confirm=False)

    assert exit_code == 0
    row = _fetch_row(db_path, db_id)
    assert row["folder_path"] == _WIRE_FOLDER
    assert row["pst_source"] == f"imap:sean3808@mailattic.test:{_WIRE_FOLDER}"
    assert row["logical_locator"] == f"{_WIRE_FOLDER}/uid-1.eml"


def test_compute_backfill_plan_reports_the_eligible_row(
    db_with_imap_row: tuple[Path, int],
) -> None:
    db_path, db_id = db_with_imap_row
    conn = connect_database(db_path)
    try:
        rows_scanned, plans, skipped = compute_backfill_plan(conn)
    finally:
        conn.close()

    assert rows_scanned == 1
    assert len(plans) == 1
    assert plans[0].row_id == db_id
    assert plans[0].new_folder_path == _DISPLAY_FOLDER
    assert skipped == []


def test_confirm_repairs_all_three_columns(
    db_with_imap_row: tuple[Path, int],
) -> None:
    db_path, db_id = db_with_imap_row

    exit_code = run_backfill(db_path, confirm=True)

    assert exit_code == 0
    row = _fetch_row(db_path, db_id)
    assert row["folder_path"] == _DISPLAY_FOLDER
    assert row["pst_source"] == f"imap:sean3808@mailattic.test:{_DISPLAY_FOLDER}"
    assert row["logical_locator"] == f"{_DISPLAY_FOLDER}/uid-1.eml"
    # Only the folder segment changed — the account and the message-path
    # segment survive intact.
    assert row["pst_source"].startswith("imap:sean3808@mailattic.test:")
    assert row["logical_locator"].endswith("/uid-1.eml")


def test_second_confirmed_run_is_idempotent(
    db_with_imap_row: tuple[Path, int],
) -> None:
    db_path, db_id = db_with_imap_row

    first_exit = run_backfill(db_path, confirm=True)
    assert first_exit == 0
    row_after_first = _fetch_row(db_path, db_id)

    second_exit = run_backfill(db_path, confirm=True)
    assert second_exit == 0
    row_after_second = _fetch_row(db_path, db_id)

    assert row_after_second == row_after_first
    assert row_after_second["folder_path"] == _DISPLAY_FOLDER


def test_inconsistent_pst_source_shape_leaves_the_whole_row_untouched(
    tmp_path: Path,
) -> None:
    """A row whose `pst_source` does not end with the folder-name segment
    stored in `folder_path` (unexpected shape) must be left completely
    untouched -- `folder_path` included -- rather than partially repaired,
    and reported as a skipped row (Codex semantic review, all-or-nothing
    repair)."""
    db_path = tmp_path / "fixture.db"
    initialize_database(db_path)
    unexpected_pst_source = (
        f"imap:sean3808@mailattic.test:{_WIRE_FOLDER}-unexpected-suffix"
    )
    db_id = _insert_message(
        db_path,
        dedup_key="inconsistent-row-dedup",
        pst_source=unexpected_pst_source,
        folder_path=_WIRE_FOLDER,
        logical_locator=f"{_WIRE_FOLDER}/uid-2.eml",
    )

    conn = connect_database(db_path)
    try:
        rows_scanned, plans, skipped = compute_backfill_plan(conn)
    finally:
        conn.close()

    assert rows_scanned == 1
    assert plans == []
    assert len(skipped) == 1
    assert skipped[0].row_id == db_id

    confirmed_exit = run_backfill(db_path, confirm=True)
    assert confirmed_exit == 0
    row = _fetch_row(db_path, db_id)
    assert row["folder_path"] == _WIRE_FOLDER
    assert row["pst_source"] == unexpected_pst_source
    assert row["logical_locator"] == f"{_WIRE_FOLDER}/uid-2.eml"


def test_pst_source_folder_segment_must_match_folder_path_exactly(
    tmp_path: Path,
) -> None:
    """A `pst_source` whose folder segment merely ENDS WITH `folder_path`, but
    is not exactly it, must leave the whole row untouched.

    `imap:<account>:prefix&W8RO9lCZTv0-` ends with the wire folder name, but
    its folder segment is `prefix&W8RO9lCZTv0-`, not `&W8RO9lCZTv0-`. A
    trailing-substring match would rewrite it to `imap:<account>:prefix寄件備份`
    while setting `folder_path` to `寄件備份`, leaving the row permanently
    inconsistent (Codex semantic review). The suffix match is anchored on the
    ':' separator to prevent exactly this.
    """
    db_path = tmp_path / "fixture.db"
    initialize_database(db_path)
    # Two shapes whose folder segment merely ENDS WITH folder_path: one plain,
    # one where the extra text is itself colon-separated (which a ':'-anchored
    # suffix match would still have accepted).
    prefixed_pst_source = f"imap:sean3808@mailattic.test:prefix{_WIRE_FOLDER}"
    colon_prefixed_pst_source = f"imap:sean3808@mailattic.test:prefix:{_WIRE_FOLDER}"
    plain_id = _insert_message(
        db_path,
        dedup_key="prefixed-folder-segment-dedup",
        pst_source=prefixed_pst_source,
        folder_path=_WIRE_FOLDER,
        logical_locator=f"{_WIRE_FOLDER}/uid-3.eml",
    )
    colon_id = _insert_message(
        db_path,
        dedup_key="colon-prefixed-folder-segment-dedup",
        pst_source=colon_prefixed_pst_source,
        folder_path=_WIRE_FOLDER,
        logical_locator=f"{_WIRE_FOLDER}/uid-4.eml",
    )

    assert run_backfill(db_path, confirm=True) == 0

    plain_row = _fetch_row(db_path, plain_id)
    assert plain_row["folder_path"] == _WIRE_FOLDER
    assert plain_row["pst_source"] == prefixed_pst_source
    assert plain_row["logical_locator"] == f"{_WIRE_FOLDER}/uid-3.eml"

    colon_row = _fetch_row(db_path, colon_id)
    assert colon_row["folder_path"] == _WIRE_FOLDER
    assert colon_row["pst_source"] == colon_prefixed_pst_source
    assert colon_row["logical_locator"] == f"{_WIRE_FOLDER}/uid-4.eml"


def test_pst_sourced_row_is_never_touched(tmp_path: Path) -> None:
    """A PST-sourced row whose folder name merely resembles a shift sequence
    must never be rewritten, in dry-run or confirmed mode."""
    db_path = tmp_path / "fixture.db"
    initialize_database(db_path)
    # `&W8RO9lCZTv0-` decodes to a real value, so it is a strong stand-in for
    # "text that resembles a modified UTF-7 shift sequence" while still being
    # something the codec would happily decode if given the chance.
    pst_folder = f"Inbox/{_WIRE_FOLDER}"
    db_id = _insert_message(
        db_path,
        dedup_key="pst-row-dedup",
        pst_source=r"D:\mail\archive_2020-2023.pst",
        folder_path=pst_folder,
        logical_locator=f"{pst_folder}/msg-1",
    )

    dry_run_exit = run_backfill(db_path, confirm=False)
    assert dry_run_exit == 0
    row = _fetch_row(db_path, db_id)
    assert row["folder_path"] == pst_folder

    confirmed_exit = run_backfill(db_path, confirm=True)
    assert confirmed_exit == 0
    row = _fetch_row(db_path, db_id)
    assert row["folder_path"] == pst_folder
    assert row["pst_source"] == r"D:\mail\archive_2020-2023.pst"
    assert row["logical_locator"] == f"{pst_folder}/msg-1"

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from mailattic.storage import initialize_database

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from backfill_thread_key import run_backfill as run_thread_key_backfill  # noqa: E402
from bridge_orphan_outbound import run_bridge_backfill  # noqa: E402

SELF = "sean3808@mailattic.test"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_message(
    conn: sqlite3.Connection,
    *,
    subject: str,
    sender_email: str,
    recipients: tuple[str, ...],
    sent_at: str,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references_header: str | None = None,
    thread_key: int | None = None,
    dedup_key: str,
) -> int:
    conn.execute("INSERT OR IGNORE INTO self_addresses (address) VALUES (?)", [SELF])
    cursor = conn.execute(
        """
        INSERT INTO messages (
            subject,
            sender_email,
            sent_at,
            body_text,
            message_id,
            in_reply_to,
            references_header,
            pst_source,
            folder_path,
            logical_locator,
            dedup_key,
            thread_key
        ) VALUES (?, ?, ?, '', ?, ?, ?, 'test', '/Inbox', ?, ?, ?)
        """,
        [
            subject,
            sender_email,
            sent_at,
            message_id,
            in_reply_to,
            references_header,
            f"loc-{dedup_key}",
            dedup_key,
            thread_key,
        ],
    )
    db_id = int(cursor.lastrowid)
    if thread_key is None:
        conn.execute("UPDATE messages SET thread_key = ? WHERE id = ?", [db_id, db_id])
    for recipient in recipients:
        conn.execute(
            """
            INSERT INTO message_recipients (
                message_id,
                recipient_type,
                display_name,
                email
            ) VALUES (?, 'to', '', ?)
            """,
            [db_id, recipient],
        )
    return db_id


def _seed_candidate_db(db_path: Path) -> int:
    conn = _connect(db_path)
    try:
        anchor_id = _insert_message(
            conn,
            subject="Project Alpha",
            sender_email="customer@example.com",
            recipients=(SELF,),
            sent_at="2026-01-01T09:00:00",
            message_id="<anchor@example.com>",
            dedup_key="anchor-dedup",
        )
        _insert_message(
            conn,
            subject="Re: Project Alpha",
            sender_email=SELF,
            recipients=("customer@example.com",),
            sent_at="2026-01-01T10:00:00",
            message_id="<reply@example.com>",
            in_reply_to="<anchor@example.com>",
            references_header="<anchor@example.com>",
            thread_key=anchor_id,
            dedup_key="reply-dedup",
        )
        orphan_id = _insert_message(
            conn,
            subject="Re: Project Alpha",
            sender_email=SELF,
            recipients=("customer@example.com",),
            sent_at="2026-01-01T12:00:00",
            dedup_key="orphan-dedup",
        )
        conn.commit()
        return orphan_id
    finally:
        conn.close()


def _bridge_state(db_path: Path) -> list[tuple[int, str | None, str | None, int]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, thread_bridge_target, thread_bridge_source, thread_key
            FROM messages
            ORDER BY id
            """
        ).fetchall()
        return [
            (
                int(row["id"]),
                row["thread_bridge_target"],
                row["thread_bridge_source"],
                int(row["thread_key"]),
            )
            for row in rows
        ]
    finally:
        conn.close()


def _row_by_dedup(db_path: Path, dedup_key: str) -> sqlite3.Row:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT id, thread_bridge_target, thread_bridge_source, thread_key
            FROM messages
            WHERE dedup_key = ?
            """,
            [dedup_key],
        ).fetchone()
        assert row is not None
        return row
    finally:
        conn.close()


def _thread_keys_by_dedup(db_path: Path) -> dict[str, int]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT dedup_key, thread_key FROM messages ORDER BY dedup_key"
        ).fetchall()
        return {str(row["dedup_key"]): int(row["thread_key"]) for row in rows}
    finally:
        conn.close()


def test_dry_run_reports_candidates_without_writing(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = initialize_database(tmp_path / "bridge.db")
    _seed_candidate_db(db_path)
    before = _bridge_state(db_path)

    exit_code = run_bridge_backfill(db_path, confirm=False)

    output = capsys.readouterr().out
    after = _bridge_state(db_path)

    assert exit_code == 0
    assert "orphan=orphan-dedup" in output
    assert "target=<anchor@example.com>" in output
    assert "subject='project alpha'" in output
    assert "external=customer@example.com" in output
    assert "delta_seconds=" in output
    assert "candidate_count=1" in output
    assert "[dry-run] 1 bridge candidate(s); no rows changed." in output
    assert after == before


def test_confirmed_run_is_recompute_idempotent(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = initialize_database(tmp_path / "confirm.db")
    _seed_candidate_db(db_path)

    assert run_bridge_backfill(db_path, confirm=True) == 0
    first_output = capsys.readouterr().out
    first_state = _bridge_state(db_path)

    assert "orphan=orphan-dedup" in first_output
    orphan = _row_by_dedup(db_path, "orphan-dedup")
    anchor = _row_by_dedup(db_path, "anchor-dedup")
    assert orphan["thread_bridge_target"] == "<anchor@example.com>"
    assert orphan["thread_bridge_source"] == "subject_heuristic"
    assert orphan["thread_key"] == anchor["thread_key"]

    assert run_bridge_backfill(db_path, confirm=True) == 0
    second_output = capsys.readouterr().out
    second_state = _bridge_state(db_path)

    assert "[confirm] 0 bridge row(s) changed; 0 thread_key row(s) changed." in (
        second_output
    )
    assert second_state == first_state


def test_stale_bridge_is_overwritten_by_recomputed_target(
    tmp_path: Path,
) -> None:
    db_path = initialize_database(tmp_path / "stale.db")
    _seed_candidate_db(db_path)
    assert run_bridge_backfill(db_path, confirm=True) == 0

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE messages
            SET thread_bridge_target = ?
            WHERE dedup_key = ?
            """,
            ["<stale@example.com>", "orphan-dedup"],
        )
        conn.commit()
    finally:
        conn.close()

    assert run_bridge_backfill(db_path, confirm=True) == 0

    orphan = _row_by_dedup(db_path, "orphan-dedup")
    assert orphan["thread_bridge_target"] == "<anchor@example.com>"
    assert orphan["thread_bridge_source"] == "subject_heuristic"


def test_bridge_survives_messages_id_reshuffle_by_anchor_message_id(
    tmp_path: Path,
) -> None:
    db_path = initialize_database(tmp_path / "reshuffle.db")
    conn = _connect(db_path)
    try:
        orphan_id = _insert_message(
            conn,
            subject="Re: Project Alpha",
            sender_email=SELF,
            recipients=("customer@example.com",),
            sent_at="2026-01-01T12:00:00",
            dedup_key="orphan-dedup",
        )
        conn.execute(
            """
            UPDATE messages
            SET thread_bridge_target = ?, thread_bridge_source = ?
            WHERE id = ?
            """,
            ["<anchor@example.com>", "subject_heuristic", orphan_id],
        )
        anchor_id = _insert_message(
            conn,
            subject="Project Alpha",
            sender_email="customer@example.com",
            recipients=(SELF,),
            sent_at="2026-01-01T09:00:00",
            message_id="<anchor@example.com>",
            dedup_key="anchor-dedup",
        )
        _insert_message(
            conn,
            subject="Re: Project Alpha",
            sender_email=SELF,
            recipients=("customer@example.com",),
            sent_at="2026-01-01T10:00:00",
            message_id="<reply@example.com>",
            in_reply_to="<anchor@example.com>",
            references_header="<anchor@example.com>",
            thread_key=anchor_id,
            dedup_key="reply-dedup",
        )
        conn.commit()
    finally:
        conn.close()

    assert run_bridge_backfill(db_path, confirm=True) == 0

    orphan = _row_by_dedup(db_path, "orphan-dedup")
    anchor = _row_by_dedup(db_path, "anchor-dedup")
    assert int(orphan["id"]) != int(anchor["id"])
    assert orphan["thread_bridge_target"] == "<anchor@example.com>"
    assert orphan["thread_key"] == anchor["thread_key"]


def test_stale_bridge_cleared_when_no_longer_qualifies(
    tmp_path: Path,
) -> None:
    """Finding 2: a stale bridge whose orphan no longer qualifies must be cleared."""
    db_path = initialize_database(tmp_path / "stale_cleared.db")
    _seed_candidate_db(db_path)

    # First confirmed run: orphan gets bridged to anchor.
    assert run_bridge_backfill(db_path, confirm=True) == 0
    orphan = _row_by_dedup(db_path, "orphan-dedup")
    assert orphan["thread_bridge_target"] is not None

    # Change orphan subject so the bridge no longer qualifies.
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE messages SET subject = 'Unrelated Topic' WHERE dedup_key = ?",
            ["orphan-dedup"],
        )
        conn.commit()
    finally:
        conn.close()

    # Second confirmed run: stale bridge must be cleared to NULL.
    assert run_bridge_backfill(db_path, confirm=True) == 0

    orphan = _row_by_dedup(db_path, "orphan-dedup")
    assert orphan["thread_bridge_target"] is None
    assert orphan["thread_bridge_source"] is None


def test_stale_bridge_polluted_thread_key_does_not_affect_inference(
    tmp_path: Path,
) -> None:
    """Finding 1+2: a stale bridge that polluted thread_key must not mislead
    re-inference; header-only component grouping must produce the correct bridge.

    Setup: two inbound threads with the same subject but different participants.
    Component A has customer1; component X has customer2.  The orphan sends to
    customer1, so it belongs to A.  We inject a wrong bridge O→X and update
    thread_key to put O inside X's stored component.  With the old code
    (rows_by_thread_key), O's presence boosts X's participant set with customer1,
    making both A and X qualify → unique-candidate gate fires → no bridge.
    With the fix (rows_by_header_key), X's header-only component has only X1
    (customer2 only); intersection with O's {customer1} is empty → X doesn't
    qualify; only A qualifies → correct bridge O→A1.
    """
    db_path = initialize_database(tmp_path / "polluted.db")
    conn = _connect(db_path)
    try:
        # Component A: correct thread for orphan (participants: customer1).
        _insert_message(
            conn,
            subject="Project Alpha",
            sender_email="customer1@example.com",
            recipients=(SELF,),
            sent_at="2026-01-01T09:00:00",
            message_id="<anchorA@example.com>",
            dedup_key="anchorA-dedup",
        )
        # Component X: wrong thread (participants: customer2 only — no customer1).
        anchor_x_id = _insert_message(
            conn,
            subject="Project Alpha",
            sender_email="customer2@example.com",
            recipients=(SELF,),
            sent_at="2026-01-01T08:00:00",
            message_id="<anchorX@example.com>",
            dedup_key="anchorX-dedup",
        )
        # Orphan O: sent to customer1 only.
        orphan_id = _insert_message(
            conn,
            subject="Re: Project Alpha",
            sender_email=SELF,
            recipients=("customer1@example.com",),
            sent_at="2026-01-01T12:00:00",
            dedup_key="orphan-dedup",
        )
        # Inject wrong stale bridge O→X and pollute thread_key to put O inside X.
        conn.execute(
            """
            UPDATE messages
            SET thread_bridge_target = ?,
                thread_bridge_source = ?,
                thread_key = ?
            WHERE id = ?
            """,
            ["<anchorX@example.com>", "subject_heuristic", anchor_x_id, orphan_id],
        )
        conn.commit()
    finally:
        conn.close()

    # Confirmed run: header-only inference must ignore the polluted thread_key.
    assert run_bridge_backfill(db_path, confirm=True) == 0

    orphan = _row_by_dedup(db_path, "orphan-dedup")
    # Must bridge to the correct anchor A (customer1's thread), not the stale X.
    assert orphan["thread_bridge_target"] == "<anchorA@example.com>"
    assert orphan["thread_bridge_source"] == "subject_heuristic"


def test_clearing_bridge_then_header_backfill_restores_prebridge_thread_keys(
    tmp_path: Path,
) -> None:
    db_path = initialize_database(tmp_path / "rollback.db")
    _seed_candidate_db(db_path)
    pre_bridge = _thread_keys_by_dedup(db_path)

    assert run_bridge_backfill(db_path, confirm=True) == 0
    bridged = _thread_keys_by_dedup(db_path)
    assert bridged["orphan-dedup"] == bridged["anchor-dedup"]
    assert bridged != pre_bridge

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE messages
            SET thread_bridge_target = NULL,
                thread_bridge_source = NULL
            """
        )
        conn.commit()
    finally:
        conn.close()

    run_thread_key_backfill(db_path)

    assert _thread_keys_by_dedup(db_path) == pre_bridge

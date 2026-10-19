from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from mailattic.storage import initialize_database

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bridge_orphan_outbound import (  # noqa: E402
    infer_bridge_candidates,
    normalize_base_subject,
    select_orphan_outbound_candidates,
)

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
    folder_path: str = "/Inbox",
    dedup_key: str | None = None,
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
        ) VALUES (?, ?, ?, '', ?, ?, ?, 'test', ?, ?, ?, ?)
        """,
        [
            subject,
            sender_email,
            sent_at,
            message_id,
            in_reply_to,
            references_header,
            folder_path,
            f"loc-{dedup_key or message_id or subject}",
            dedup_key or f"dedup-{subject}-{sent_at}-{sender_email}",
            thread_key,
        ],
    )
    message_db_id = int(cursor.lastrowid)
    if thread_key is None:
        conn.execute(
            "UPDATE messages SET thread_key = ? WHERE id = ?",
            [message_db_id, message_db_id],
        )
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
            [message_db_id, recipient],
        )
    return message_db_id


def _seed_target_thread(
    conn: sqlite3.Connection,
    *,
    subject: str = "Project Alpha",
    external: str = "customer@example.com",
    sent_at: str = "2026-01-01T09:00:00",
) -> int:
    anchor_id = _insert_message(
        conn,
        subject=subject,
        sender_email=external,
        recipients=(SELF,),
        sent_at=sent_at,
        message_id=f"<anchor-{external}>",
        dedup_key=f"anchor-{external}",
    )
    _insert_message(
        conn,
        subject=f"Re: {subject}",
        sender_email=SELF,
        recipients=(external,),
        sent_at="2026-01-01T10:00:00",
        message_id=f"<reply-{external}>",
        in_reply_to=f"<anchor-{external}>",
        references_header=f"<anchor-{external}>",
        thread_key=anchor_id,
        dedup_key=f"reply-{external}",
    )
    return anchor_id


def test_normalize_base_subject_strips_reply_and_forward_markers() -> None:
    assert normalize_base_subject("[List] Re: Fwd: Project Alpha (fwd)") == (
        "project alpha"
    )


def test_candidate_selection_uses_sender_direction_not_folder_path(
    tmp_path: Path,
) -> None:
    db_path = initialize_database(tmp_path / "gate.db")
    conn = _connect(db_path)
    try:
        outbound_in_inbox = _insert_message(
            conn,
            subject="Re: Folder lies",
            sender_email=SELF,
            recipients=("customer@example.com",),
            sent_at="2026-01-02T09:00:00",
            folder_path="/Inbox",
            dedup_key="outbound-in-inbox",
        )
        _insert_message(
            conn,
            subject="Re: Inbound in sent",
            sender_email="customer@example.com",
            recipients=(SELF,),
            sent_at="2026-01-02T10:00:00",
            folder_path="/Sent",
            dedup_key="inbound-in-sent",
        )
        _insert_message(
            conn,
            subject="Re: Header linked",
            sender_email=SELF,
            recipients=("customer@example.com",),
            sent_at="2026-01-02T11:00:00",
            in_reply_to="<parent@example.com>",
            dedup_key="header-linked",
        )

        candidates = select_orphan_outbound_candidates(conn)
    finally:
        conn.close()

    assert [int(row["id"]) for row in candidates] == [outbound_in_inbox]


def test_qualifying_outbound_orphan_bridges_to_header_linked_thread(
    tmp_path: Path,
) -> None:
    db_path = initialize_database(tmp_path / "qualify.db")
    conn = _connect(db_path)
    try:
        _seed_target_thread(conn)
        orphan_id = _insert_message(
            conn,
            subject="Re: Project Alpha",
            sender_email=SELF,
            recipients=("customer@example.com",),
            sent_at="2026-01-01T12:00:00",
            dedup_key="orphan-good",
        )

        candidates = infer_bridge_candidates(conn)
    finally:
        conn.close()

    assert [candidate.orphan_id for candidate in candidates] == [orphan_id]
    assert candidates[0].target_anchor == "<anchor-customer@example.com>"
    assert candidates[0].external_participants == ("customer@example.com",)


def test_gate_truth_table_negative_cases(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "negative.db")
    conn = _connect(db_path)
    try:
        _seed_target_thread(conn, subject="Project Alpha")
        _insert_message(
            conn,
            subject="Re:",
            sender_email=SELF,
            recipients=("customer@example.com",),
            sent_at="2026-01-01T12:00:00",
            dedup_key="empty-subject",
        )
        _insert_message(
            conn,
            subject="Re: Project Alpha",
            sender_email=SELF,
            recipients=(SELF,),
            sent_at="2026-01-01T12:01:00",
            dedup_key="self-only",
        )
        _insert_message(
            conn,
            subject="Re: Project Alpha",
            sender_email="customer@example.com",
            recipients=(SELF,),
            sent_at="2026-01-01T12:02:00",
            dedup_key="inbound-row",
        )
        _insert_message(
            conn,
            subject="Re: Project Alpha",
            sender_email=SELF,
            recipients=("customer@example.com",),
            sent_at="2026-01-01T12:03:00",
            in_reply_to="<anchor-customer@example.com>",
            dedup_key="header-linked",
        )
        _insert_message(
            conn,
            subject="Re: Orphan Only",
            sender_email=SELF,
            recipients=("other@example.com",),
            sent_at="2026-01-01T12:04:00",
            dedup_key="orphan-to-orphan-target",
        )
        _insert_message(
            conn,
            subject="Re: Orphan Only",
            sender_email=SELF,
            recipients=("other@example.com",),
            sent_at="2026-01-01T12:05:00",
            dedup_key="orphan-to-orphan-candidate",
        )

        candidates = infer_bridge_candidates(conn)
    finally:
        conn.close()

    assert candidates == []


def test_multi_candidate_target_skips_bridge(tmp_path: Path) -> None:
    db_path = initialize_database(tmp_path / "multi.db")
    conn = _connect(db_path)
    try:
        _seed_target_thread(conn, subject="Project Alpha", external="a@example.com")
        _seed_target_thread(conn, subject="Project Alpha", external="b@example.com")
        _insert_message(
            conn,
            subject="Re: Project Alpha",
            sender_email=SELF,
            recipients=("a@example.com", "b@example.com"),
            sent_at="2026-01-01T12:00:00",
            dedup_key="multi-candidate",
        )

        candidates = infer_bridge_candidates(conn)
    finally:
        conn.close()

    assert candidates == []

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from mailattic import storage
from mailattic.models import MailboxParticipant, NormalizedMessage
from mailattic.query import resolve_thread_message_ids
from mailattic.storage import (
    _assign_thread_key_for_new_message,
    initialize_database,
    persist_message,
)
from mailattic.thread_key import compute_thread_keys


def make_message(
    message_id: str | None,
    in_reply_to: str | None = None,
    references: str | None = None,
    subject: str = "Test",
) -> NormalizedMessage:
    return NormalizedMessage(
        subject=subject,
        sender=MailboxParticipant(display_name="Sender", email="sender@example.com"),
        to_recipients=(
            MailboxParticipant(display_name="Rec", email="rec@example.com"),
        ),
        cc_recipients=(),
        bcc_recipients=(),
        sent_at=datetime(2024, 1, 1),
        received_at=None,
        body_text="body",
        attachments=(),
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        pst_source="test",
        folder_path="/Inbox",
        logical_locator=message_id or "no-mid",
        source_input_mode="test",
        source_requested_from="test",
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return initialize_database(tmp_path / "test.db")


def test_reply_gets_parents_thread_key(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        parent_result = persist_message(conn, make_message(message_id="parent@test"))
        reply_result = persist_message(
            conn,
            make_message(message_id="reply@test", in_reply_to="parent@test"),
        )

        parent_row = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [parent_result.stored_message_id],
        ).fetchone()
        reply_row = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [reply_result.stored_message_id],
        ).fetchone()
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]

        assert parent_row is not None
        assert reply_row is not None
        assert parent_row["thread_key"] == reply_row["thread_key"]
        assert parent_row["thread_key"] == parent_result.stored_message_id
        assert busy_timeout == 5000
    finally:
        conn.close()


def test_bridging_message_merges_two_components(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        r_a1 = persist_message(conn, make_message(message_id="a1@test"))
        r_a2 = persist_message(
            conn,
            make_message(message_id="a2@test", in_reply_to="a1@test"),
        )
        r_b1 = persist_message(conn, make_message(message_id="b1@test"))
        r_b2 = persist_message(
            conn,
            make_message(message_id="b2@test", in_reply_to="b1@test"),
        )
        r_b3 = persist_message(
            conn,
            make_message(message_id="b3@test", in_reply_to="b1@test"),
        )

        def read_thread_key(message_db_id: int) -> int:
            row = conn.execute(
                "SELECT thread_key FROM messages WHERE id = ?",
                [message_db_id],
            ).fetchone()
            assert row is not None
            return int(row["thread_key"])

        a_key_before = read_thread_key(r_a1.stored_message_id)
        b_key_before = read_thread_key(r_b1.stored_message_id)
        assert a_key_before != b_key_before

        r_bridge = persist_message(
            conn,
            make_message(
                message_id="bridge@test",
                in_reply_to="a2@test",
                references="b1@test",
            ),
        )

        expected_canonical = r_a1.stored_message_id
        assert a_key_before == expected_canonical
        assert read_thread_key(r_a2.stored_message_id) == expected_canonical
        assert b_key_before == r_b1.stored_message_id

        all_ids = (
            r_a1.stored_message_id,
            r_a2.stored_message_id,
            r_b1.stored_message_id,
            r_b2.stored_message_id,
            r_b3.stored_message_id,
            r_bridge.stored_message_id,
        )
        assert all(
            read_thread_key(message_db_id) == expected_canonical
            for message_db_id in all_ids
        )
    finally:
        conn.close()


def test_bridged_orphan_component_uses_target_anchor_thread_key() -> None:
    messages = [
        (10, "<anchor@example.com>", None, None, "anchor-dedup", None),
        (
            11,
            "<reply@example.com>",
            "<anchor@example.com>",
            "<anchor@example.com>",
            "reply-dedup",
            None,
        ),
        (20, None, None, None, "orphan-dedup", "<anchor@example.com>"),
    ]

    result = compute_thread_keys(messages)

    assert result[10] == 10
    assert result[11] == 10
    assert result[20] == 10


def test_prebridged_new_row_merges_into_target_component(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        anchor_result = persist_message(conn, make_message(message_id="<anchor@test>"))
        reply_result = persist_message(
            conn,
            make_message(message_id="<reply@test>", in_reply_to="<anchor@test>"),
        )
        orphan_result = persist_message(
            conn,
            make_message(message_id=None, subject="Re: Test"),
        )

        orphan_before = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [orphan_result.stored_message_id],
        ).fetchone()
        assert orphan_before is not None
        assert orphan_before["thread_key"] == orphan_result.stored_message_id

        conn.execute(
            """
            UPDATE messages
            SET thread_bridge_target = ?, thread_bridge_source = ?
            WHERE id = ?
            """,
            [
                "<anchor@test>",
                "subject_heuristic",
                orphan_result.stored_message_id,
            ],
        )
        _assign_thread_key_for_new_message(
            conn,
            new_db_id=orphan_result.stored_message_id,
            message_id=None,
            in_reply_to=None,
            references_header=None,
            thread_bridge_target="<anchor@test>",
        )

        rows = conn.execute(
            "SELECT id, thread_key FROM messages ORDER BY id"
        ).fetchall()
        keys_by_id = {int(row["id"]): int(row["thread_key"]) for row in rows}

        assert keys_by_id[anchor_result.stored_message_id] == (
            anchor_result.stored_message_id
        )
        assert keys_by_id[reply_result.stored_message_id] == (
            anchor_result.stored_message_id
        )
        assert keys_by_id[orphan_result.stored_message_id] == (
            anchor_result.stored_message_id
        )
    finally:
        conn.close()


def test_reply_before_parent_gets_nonnull_thread_key(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        reply_result = persist_message(
            conn,
            make_message(message_id="reply@test", in_reply_to="parent@test"),
        )
        reply_row = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [reply_result.stored_message_id],
        ).fetchone()

        assert reply_row is not None
        assert reply_row["thread_key"] == reply_result.stored_message_id

        parent_result = persist_message(conn, make_message(message_id="parent@test"))
        expected_canonical = min(
            reply_result.stored_message_id,
            parent_result.stored_message_id,
        )
        parent_row = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [parent_result.stored_message_id],
        ).fetchone()
        reply_row_after = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [reply_result.stored_message_id],
        ).fetchone()

        assert parent_row is not None
        assert reply_row_after is not None
        assert expected_canonical == reply_result.stored_message_id
        assert parent_row["thread_key"] == expected_canonical
        assert reply_row_after["thread_key"] == expected_canonical
    finally:
        conn.close()


def test_parent_arriving_late_merges_via_references_header(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        reply_result = persist_message(
            conn,
            make_message(message_id="reply-ref@test", references="<pid@test>"),
        )
        reply_row = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [reply_result.stored_message_id],
        ).fetchone()

        assert reply_row is not None
        assert reply_row["thread_key"] == reply_result.stored_message_id

        parent_result = persist_message(conn, make_message(message_id="pid@test"))
        expected_canonical = min(
            reply_result.stored_message_id,
            parent_result.stored_message_id,
        )
        parent_row = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [parent_result.stored_message_id],
        ).fetchone()
        reply_row_after = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [reply_result.stored_message_id],
        ).fetchone()

        assert parent_row is not None
        assert reply_row_after is not None
        assert expected_canonical == reply_result.stored_message_id
        assert parent_row["thread_key"] == expected_canonical
        assert reply_row_after["thread_key"] == expected_canonical
    finally:
        conn.close()


def test_reply_joins_parent_with_bracketed_message_id(db_path: Path) -> None:
    """Parent stored with angle-bracket message_id; reply must join its thread.

    Regression: ancestor lookup used bare ref_id only, so a parent stored as
    '<parent@test>' was not found when the reply's in_reply_to was stripped to
    'parent@test'.  The fix queries both bare and bracketed forms.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Insert parent with bracketed message_id stored verbatim.
        parent_result = persist_message(conn, make_message(message_id="<parent@test>"))
        # Reply references the parent using bracketed in_reply_to (real-mail form).
        reply_result = persist_message(
            conn,
            make_message(message_id="<reply@test>", in_reply_to="<parent@test>"),
        )

        parent_row = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [parent_result.stored_message_id],
        ).fetchone()
        reply_row = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [reply_result.stored_message_id],
        ).fetchone()

        assert parent_row is not None
        assert reply_row is not None
        # Both must share the same thread_key (= parent's db_id, which is the min).
        assert parent_row["thread_key"] == parent_result.stored_message_id
        assert reply_row["thread_key"] == parent_result.stored_message_id
    finally:
        conn.close()


@pytest.mark.parametrize("scenario", ["missing_parent", "late_bare_reference"])
def test_incremental_thread_keys_equal_rebuild(db_path: Path, scenario: str) -> None:
    """Incremental persist_message() must agree with compute_thread_keys() (#191).

    Two cases where the incremental ancestor/descendant lookups previously
    diverged from the batch union-find: (1) two replies whose common
    ancestor is never itself persisted, (2) a child using the bracket-free
    References form arriving before its parent.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if scenario == "missing_parent":
            messages = [
                make_message(
                    message_id="a@example.com", in_reply_to="missing@example.com"
                ),
                make_message(
                    message_id="b@example.com", in_reply_to="missing@example.com"
                ),
            ]
        else:
            messages = [
                make_message(
                    message_id="child@example.com", references="parent@example.com"
                ),
                make_message(message_id="parent@example.com"),
            ]
        for message in messages:
            persist_message(conn, message)
        conn.commit()  # resolve_thread_message_ids() opens its own connection

        rows = conn.execute(
            "SELECT id, message_id, in_reply_to, references_header, thread_key"
            " FROM messages ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    actual = {row["id"]: row["thread_key"] for row in rows}
    rebuilt = compute_thread_keys([tuple(row)[:4] for row in rows])
    assert actual == rebuilt

    thread_ids = resolve_thread_message_ids(db_path, 1, read_only=True)
    assert set(thread_ids) == {1, 2}


def test_insert_with_many_references_scans_messages_at_most_once(
    db_path: Path,
) -> None:
    """A long References header must not turn one insert into N table scans (#191).

    Guards the per-insert query shape: SELECT statements that touch the
    unindexed in_reply_to column or LIKE-scan references_header must be
    folded into a single statement regardless of how many distinct ids the
    new message's References header carries. message_id lookups (index-only,
    unique index) are exempt -- they stay one-per-id by design and are cheap.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for i in range(10):
            persist_message(conn, make_message(message_id=f"ref{i}@example.com"))

        references_header = " ".join(f"ref{i}@example.com" for i in range(10))

        scanning_statements: list[str] = []

        def _trace(sql: str) -> None:
            normalized = " ".join(sql.split())
            has_like = "references_header LIKE" in normalized
            has_in_reply_to_eq = "in_reply_to = ?" in normalized
            if has_like or has_in_reply_to_eq:
                scanning_statements.append(normalized)

        conn.set_trace_callback(_trace)
        try:
            persist_message(
                conn,
                make_message(
                    message_id="many-refs@example.com",
                    references=references_header,
                ),
            )
        finally:
            conn.set_trace_callback(None)

        assert len(scanning_statements) <= 1, scanning_statements
    finally:
        conn.close()


def test_incremental_link_in_later_scan_batch_still_merges(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunked candidate batches must never drop a later-batch link (#191).

    Batch size is monkeypatched down to 2 so the test stays small while
    forcing the actual sibling link -- the shared absent ancestor
    'missing@test', a bare References token -- into the SECOND scanning
    batch, behind two decoy ids that fill the first batch. A cap-based
    implementation that truncates candidates instead of chunking them would
    drop 'missing@test' entirely and leave the two messages ungrouped.
    """
    monkeypatch.setattr(storage, "_INCREMENTAL_REF_SCAN_BATCH_SIZE", 2)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        a_result = persist_message(
            conn, make_message(message_id="a@test", in_reply_to="missing@test")
        )
        b_result = persist_message(
            conn,
            make_message(
                message_id="b@test",
                references="decoy0@test decoy1@test missing@test",
            ),
        )

        rows = conn.execute(
            "SELECT id, message_id, in_reply_to, references_header, thread_key"
            " FROM messages ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    actual = {row["id"]: row["thread_key"] for row in rows}
    rebuilt = compute_thread_keys([tuple(row)[:4] for row in rows])
    assert actual == rebuilt
    assert actual[a_result.stored_message_id] == actual[b_result.stored_message_id]


def test_full_size_scan_batch_stays_within_sqlite_expression_limits(
    db_path: Path,
) -> None:
    """The real batch size must fit SQLite's expression-depth limit (#191).

    No monkeypatch: a References header long enough to fill more than one
    batch at the production batch size must still ingest, and the link held
    in the last id must still merge the thread.
    """
    decoys = " ".join(
        f"decoy{i}@test" for i in range(storage._INCREMENTAL_REF_SCAN_BATCH_SIZE * 2)
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        a_result = persist_message(
            conn, make_message(message_id="a@test", in_reply_to="missing@test")
        )
        b_result = persist_message(
            conn,
            make_message(message_id="b@test", references=f"{decoys} missing@test"),
        )
        keys = {
            row["id"]: row["thread_key"]
            for row in conn.execute("SELECT id, thread_key FROM messages")
        }
    finally:
        conn.close()

    assert keys[a_result.stored_message_id] == keys[b_result.stored_message_id]


class FaultInjectingConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_on_update_call: int | None = None
        self.update_call_count = 0

    def execute(self, sql: str, parameters=(), /):  # type: ignore[override]
        if "UPDATE messages SET thread_key" in " ".join(sql.split()):
            self.update_call_count += 1
            if self.fail_on_update_call == self.update_call_count:
                raise sqlite3.OperationalError("Simulated mid-merge failure")
        return super().execute(sql, parameters)


def test_merge_is_atomic_no_half_updated_state(db_path: Path) -> None:
    # Atomicity for the thread_key merge is provided by the caller's SAVEPOINT
    # (ingest.py._persist_extraction_results).  This test mirrors that pattern:
    # SAVEPOINT wraps persist_message; on failure the SAVEPOINT is rolled back,
    # so no partial rewrite (key_b changed but key_a / bridge row not) survives.
    conn = sqlite3.connect(db_path, factory=FaultInjectingConnection)
    conn.row_factory = sqlite3.Row
    try:
        r_a = persist_message(conn, make_message(message_id="atomic-a@test"))
        r_b = persist_message(conn, make_message(message_id="atomic-b@test"))
        conn.execute(
            "COMMIT"
        )  # commit the setup rows so they are visible after rollback

        key_a_before = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [r_a.stored_message_id],
        ).fetchone()["thread_key"]
        key_b_before = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [r_b.stored_message_id],
        ).fetchone()["thread_key"]

        conn.update_call_count = 0
        conn.fail_on_update_call = (
            2  # fail on the second UPDATE (setting new row's key)
        )
        with pytest.raises(
            sqlite3.OperationalError, match="Simulated mid-merge failure"
        ):
            conn.execute("SAVEPOINT persist_message")
            try:
                persist_message(
                    conn,
                    make_message(
                        message_id="atomic-bridge@test",
                        in_reply_to="atomic-a@test",
                        references="atomic-b@test",
                    ),
                )
                conn.execute("RELEASE SAVEPOINT persist_message")
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT persist_message")
                conn.execute("RELEASE SAVEPOINT persist_message")
                raise

        key_a_after = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [r_a.stored_message_id],
        ).fetchone()["thread_key"]
        key_b_after = conn.execute(
            "SELECT thread_key FROM messages WHERE id = ?",
            [r_b.stored_message_id],
        ).fetchone()["thread_key"]
        bridge_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id = ?",
            ["atomic-bridge@test"],
        ).fetchone()[0]

        # SAVEPOINT rollback restored all rows to their pre-bridge state.
        assert key_a_after == key_a_before
        assert key_b_after == key_b_before
        assert bridge_count == 0
    finally:
        conn.close()

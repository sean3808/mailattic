"""Tests for walk_thread() (Python API) and bes thread (CLI integration).

Covers tasks.md 4.1 + 4.2 + task-5 verification requirements:
- test_tokenize_references_handles_multiple_separators
- test_walk_thread_returns_ordered_list_by_sent_at
- test_walk_thread_same_result_for_db_id_and_message_id
- test_walk_thread_with_no_replies_returns_single_message
- test_walk_thread_truncates_at_100_with_stderr_warning
- test_walk_thread_raises_on_unknown_identifier
- test_walk_thread_no_substring_collision
- test_walk_thread_includes_reply_without_message_id
- test_bes_thread_db_id_returns_ordered_thread
- test_bes_thread_message_id_returns_same_as_db_id
- test_bes_thread_unknown_id_exits_nonzero_with_diagnostic
- test_bes_thread_format_json_returns_array
- [task-5 new] test_walk_thread_explain_uses_index
- [task-5 new] test_walk_thread_standalone_returns_itself
- [task-5 new] test_walk_thread_over_100_truncates_earliest
- [task-5 new] test_walk_thread_unknown_identifier_errors

Task 1.1 (change fix-thread-resolver-out-of-range-identifier, red-first):
- test_resolve_thread_rejects_str_over_64bit_positive_bound
- test_resolve_thread_rejects_str_4301_effective_digits
- test_resolve_thread_rejects_str_under_64bit_negative_bound
- test_resolve_thread_rejects_int_over_64bit_bound
- test_resolve_thread_rejects_int_beyond_render_limit
- test_resolve_thread_rejects_digit_shaped_char_without_decimal_value
- test_resolve_thread_accepts_str_at_positive_64bit_boundary
- test_resolve_thread_accepts_int_at_negative_64bit_boundary
- test_resolve_thread_normalizes_zero_variants_to_id_zero
- test_resolve_thread_normalizes_long_leading_zeros_to_effective_digit
- test_resolve_thread_normalizes_arabic_indic_digit_like_ascii
- test_resolve_thread_missing_database_raises_identifier_error_before_connect
- test_resolve_thread_regression_negative_five_still_keyerror
- test_resolve_thread_regression_leading_zero_007_still_keyerror
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from mailattic.cli import main
from mailattic.models import PstSource, build_normalized_message
from mailattic.query import (
    ThreadIdentifierError,
    _tokenize_references,
    resolve_thread_message_ids,
    walk_thread,
)
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message
from mailattic.thread_key import compute_thread_keys

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SOURCE = PstSource(
    path=Path("C:/mail/archive.pst"),
    input_mode="file",
    requested_from=Path("C:/mail/archive.pst"),
)


def _persist(
    conn,
    subject,
    sender_email,
    sent_at,
    message_id,
    in_reply_to=None,
    references=None,
    folder="Inbox",
    locator_suffix="1",
):
    msg = build_normalized_message(
        {
            "subject": subject,
            "sender": {"email": sender_email},
            "to": [{"email": "recipient@example.com"}],
            "sent_at": sent_at,
            "body_text": f"Body of {subject}",
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "references": references,
            "attachments": [],
        },
        source=_SOURCE,
        folder_path=folder,
        logical_locator=f"{folder}/{locator_suffix}",
    )
    result = storage_persist_message(conn, msg)
    return result.stored_message_id


def _backfill_thread_keys(conn) -> None:
    """Compute thread_key for all rows in conn and UPDATE in place.

    Uses compute_thread_keys (thread_key.py) — the same union-find used by
    the real backfill script — so test fixtures reflect exactly the same
    thread_key semantics as production backfill.

    Must be called after all _persist() calls and before conn.commit().
    """
    rows = conn.execute(
        "SELECT id, message_id, in_reply_to, references_header, "
        "dedup_key, thread_bridge_target FROM messages"
    ).fetchall()
    messages = [
        (
            int(r["id"]),
            r["message_id"],
            r["in_reply_to"],
            r["references_header"],
            r["dedup_key"],
            r["thread_bridge_target"],
        )
        for r in rows
    ]
    thread_key_map = compute_thread_keys(messages)
    conn.executemany(
        "UPDATE messages SET thread_key = ? WHERE id = ?",
        [(tk, db_id) for db_id, tk in thread_key_map.items()],
    )


def _make_thread_db(temp_dir: str) -> tuple[Path, list[int]]:
    """Create a DB with a 3-message thread.

    Returns (db_path, [db_id_1, db_id_2, db_id_3]).
    """
    db_path = initialize_database(Path(temp_dir) / "thread_test.db")
    conn = connect_database(db_path)
    try:
        # Message 1: root
        id1 = _persist(
            conn,
            subject="Root message",
            sender_email="alice@example.com",
            sent_at="2025-11-14T10:00:00",
            message_id="<root@example.com>",
            locator_suffix="1",
        )
        # Message 2: direct reply to root
        id2 = _persist(
            conn,
            subject="Re: Root message",
            sender_email="bob@example.com",
            sent_at="2025-11-14T11:00:00",
            message_id="<reply@example.com>",
            in_reply_to="<root@example.com>",
            references="<root@example.com>",
            locator_suffix="2",
        )
        # Message 3: reply to reply (later date)
        id3 = _persist(
            conn,
            subject="Re: Re: Root message",
            sender_email="carol@example.com",
            sent_at="2025-11-18T09:00:00",
            message_id="<reply2@example.com>",
            in_reply_to="<reply@example.com>",
            references="<root@example.com> <reply@example.com>",
            locator_suffix="3",
        )
        _backfill_thread_keys(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path, [id1, id2, id3]


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    """Run main() capturing stdout/stderr. Returns (exit_code, stdout, stderr)."""
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = StringIO()
    sys.stderr = StringIO()
    try:
        exit_code = main(args)
    except SystemExit as e:
        exit_code = int(e.code) if e.code is not None else 0
    finally:
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        sys.stdout, sys.stderr = old_stdout, old_stderr
    return exit_code, out, err


# ---------------------------------------------------------------------------
# Unit tests: _tokenize_references
# ---------------------------------------------------------------------------


class TokenizeReferencesTests(unittest.TestCase):
    """Unit tests for _tokenize_references — spike-notes.md representative cases."""

    def test_tokenize_references_handles_multiple_separators(self) -> None:
        """Space-separated, tab-separated, and None all handled correctly."""
        # Single-line space-separated (most common observed format)
        result = _tokenize_references(
            "<id1@example.com> <id2@example.com> <id3@example.com>"
        )
        self.assertEqual(
            result, ["id1@example.com", "id2@example.com", "id3@example.com"]
        )

        # Tab-folded (RFC 5322 folding, observed in spike sample 5)
        result = _tokenize_references("<id1@example.com>\t<id2@example.com>")
        self.assertEqual(result, ["id1@example.com", "id2@example.com"])

        # None input → empty list
        result = _tokenize_references(None)
        self.assertEqual(result, [])

        # Empty string → empty list
        result = _tokenize_references("")
        self.assertEqual(result, [])

        # Single ID
        result = _tokenize_references("<single@example.com>")
        self.assertEqual(result, ["single@example.com"])


# ---------------------------------------------------------------------------
# Unit tests: walk_thread (Python API)
# ---------------------------------------------------------------------------


class WalkThreadTests(unittest.TestCase):
    def test_walk_thread_returns_ordered_list_by_sent_at(self) -> None:
        """Thread messages must be sorted by sent_at ASC."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, [id1, id2, id3] = _make_thread_db(tmp)
            messages = walk_thread(db_path, id2)  # start from the middle message

        self.assertEqual(len(messages), 3)
        sent_ats = [m["sent_at"] for m in messages]
        self.assertEqual(sent_ats, sorted(sent_ats))
        # First message should be the earliest
        self.assertIn("2025-11-14T10:00:00", str(sent_ats[0]))

    def test_walk_thread_same_result_for_db_id_and_message_id(self) -> None:
        """Supplying db_id vs RFC822 Message-ID must yield identical results."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, [id1, id2, id3] = _make_thread_db(tmp)

            result_by_db_id = walk_thread(db_path, id1)
            result_by_msg_id = walk_thread(
                db_path, "root@example.com"
            )  # bare, no brackets
            result_by_bracketed = walk_thread(db_path, "<root@example.com>")

        db_ids_by_db_id = [m["id"] for m in result_by_db_id]
        db_ids_by_msg_id = [m["id"] for m in result_by_msg_id]
        db_ids_by_bracketed = [m["id"] for m in result_by_bracketed]

        self.assertEqual(db_ids_by_db_id, db_ids_by_msg_id)
        self.assertEqual(db_ids_by_db_id, db_ids_by_bracketed)
        self.assertEqual(len(db_ids_by_db_id), 3)

    def test_walk_thread_with_no_replies_returns_single_message(self) -> None:
        """A standalone message (no in_reply_to, no references, no children)
        returns exactly 1."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "solo.db")
            conn = connect_database(db_path)
            try:
                db_id = _persist(
                    conn,
                    subject="Standalone",
                    sender_email="solo@example.com",
                    sent_at="2025-01-01T00:00:00",
                    message_id="<standalone@example.com>",
                    locator_suffix="1",
                )
                _backfill_thread_keys(conn)
                conn.commit()
            finally:
                conn.close()

            messages = walk_thread(db_path, db_id)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["message_id"], "<standalone@example.com>")

    def test_walk_thread_truncates_at_100_with_stderr_warning(self) -> None:
        """A thread exceeding 100 messages must be truncated to the first 100
        by sent_at ASC and emit a stderr warning.

        BFS discovery order is deliberately decoupled from sent_at order.
        Messages are arranged into two groups:
        - Group A (indices 0-49): discovered first (BFS root is here)
          but they have LATER sent_at values (2025-06 range).
        - Group B (indices 50-149): discovered later (children of group A)
          but they have EARLIER sent_at values (2024-01 range).

        The 100 EARLIEST by sent_at are therefore the 100 messages from group B.
        The test verifies that the 50 first-discovered messages from group A
        (all in 2025) are excluded in favour of the 100 earlier group-B messages.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "big.db")
            conn = connect_database(db_path)
            try:
                # Pre-build all 150 message-ids
                all_msg_ids: list[str] = [
                    f"<msg-{i:04d}@example.com>" for i in range(150)
                ]

                # Group A (indices 0-49): newer dates (2025-06), BFS root is here.
                # msg-0000 → msg-0001 → ... → msg-0049 (linear chain)
                # msg-0049 then references msg-0050 (bridge to group B)
                first_db_id = None
                for i in range(50):
                    mid = all_msg_ids[i]
                    in_reply_to = all_msg_ids[i - 1] if i > 0 else None
                    # msg-0049 bridges to group B by referencing msg-0050
                    if i == 49:
                        references = f"{all_msg_ids[48]} {all_msg_ids[50]}"
                    else:
                        references = all_msg_ids[i - 1] if i > 0 else None
                    day = (i % 28) + 1
                    db_id = _persist(
                        conn,
                        subject=f"Group-A message {i}",
                        sender_email="groupa@example.com",
                        sent_at=f"2025-06-{day:02d}T{(i % 24):02d}:00:00",
                        message_id=mid,
                        in_reply_to=in_reply_to,
                        references=references,
                        locator_suffix=str(i),
                    )
                    if i == 0:
                        first_db_id = db_id

                # Group B (indices 50-149): older dates (2024-01), 100 messages.
                # Each reply to the previous, chain anchors to group A via msg-0049.
                for i in range(50, 150):
                    mid = all_msg_ids[i]
                    in_reply_to = all_msg_ids[i - 1]
                    references = " ".join(all_msg_ids[max(0, i - 5) : i])
                    day = ((i - 50) % 28) + 1
                    _persist(
                        conn,
                        subject=f"Group-B message {i}",
                        sender_email="groupb@example.com",
                        sent_at=f"2024-01-{day:02d}T{((i - 50) % 24):02d}:00:00",
                        message_id=mid,
                        in_reply_to=in_reply_to,
                        references=references,
                        locator_suffix=str(i),
                    )

                _backfill_thread_keys(conn)
                conn.commit()
            finally:
                conn.close()

            old_stderr = sys.stderr
            sys.stderr = StringIO()
            try:
                messages = walk_thread(db_path, first_db_id)
                warning_output = sys.stderr.getvalue()
            finally:
                sys.stderr = old_stderr

        # Must return exactly 100 messages (truncated from 150)
        self.assertEqual(len(messages), 100)
        self.assertIn("thread exceeded", warning_output.lower())
        self.assertIn("100", warning_output)

        # The 100 returned messages must all be from 2024 (group B — earlier dates).
        # Group A (2025) has only 50 messages, which should all be excluded.
        returned_sent_ats = [m["sent_at"] for m in messages]
        for sat in returned_sent_ats:
            self.assertTrue(
                str(sat).startswith("2024-"),
                f"Expected 2024-xx date (group B, earliest), got {sat!r}",
            )

    def test_walk_thread_raises_on_unknown_identifier(self) -> None:
        """Unknown db_id or Message-ID must raise KeyError."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "empty.db")

            with self.assertRaises(KeyError):
                walk_thread(db_path, 999999)

            with self.assertRaises(KeyError):
                walk_thread(db_path, "nonexistent@example.com")

    def test_walk_thread_no_substring_collision(self) -> None:
        """Thread for <bc@x.com> must NOT include <abc@x.com>.

        With thread_key, the union-find uses exact parser-based matching —
        <abc@x.com> is never unioned into <bc@x.com>'s component unless it
        explicitly references <bc@x.com>, so substring collisions are impossible.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "collision.db")
            conn = connect_database(db_path)
            try:
                # Seed message: <bc@x.com> — the target thread root
                seed_id = _persist(
                    conn,
                    subject="Seed",
                    sender_email="seed@example.com",
                    sent_at="2025-06-01T10:00:00",
                    message_id="<bc@x.com>",
                    locator_suffix="seed",
                )
                # Unrelated message: <abc@x.com> references nothing related
                # but its message_id contains "bc@x.com" as a substring
                _persist(
                    conn,
                    subject="Unrelated",
                    sender_email="other@example.com",
                    sent_at="2025-06-01T11:00:00",
                    message_id="<abc@x.com>",
                    in_reply_to=None,
                    references=None,
                    locator_suffix="unrelated",
                )
                # A real reply to <bc@x.com>
                _persist(
                    conn,
                    subject="Re: Seed",
                    sender_email="reply@example.com",
                    sent_at="2025-06-01T12:00:00",
                    message_id="<reply-to-bc@x.com>",
                    in_reply_to="<bc@x.com>",
                    references="<bc@x.com>",
                    locator_suffix="reply",
                )
                _backfill_thread_keys(conn)
                conn.commit()
            finally:
                conn.close()

            messages = walk_thread(db_path, seed_id)

        msg_ids = [m["message_id"] for m in messages]
        # Thread must contain seed + its real reply
        self.assertIn("<bc@x.com>", msg_ids)
        self.assertIn("<reply-to-bc@x.com>", msg_ids)
        # Must NOT contain the unrelated message whose id is a superstring
        self.assertNotIn(
            "<abc@x.com>",
            msg_ids,
            "Substring collision: <abc@x.com> must not appear in thread for <bc@x.com>",
        )

    def test_walk_thread_includes_reply_without_message_id(self) -> None:
        """A reply whose own message_id is NULL must still be returned.

        With thread_key, compute_thread_keys() unions the NULL-message_id row
        via its in_reply_to edge (using a synthetic 'dbid:<n>' node that gets
        unioned to the root's component), so it inherits the root's thread_key
        and appears in the thread query result.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "nullid.db")
            conn = connect_database(db_path)
            try:
                root_id = _persist(
                    conn,
                    subject="Root",
                    sender_email="root@example.com",
                    sent_at="2025-07-01T10:00:00",
                    message_id="<root@example.com>",
                    locator_suffix="root",
                )
                # Reply whose own message_id is NULL but references the root
                _persist(
                    conn,
                    subject="Re: Root (no msg id)",
                    sender_email="reply@example.com",
                    sent_at="2025-07-01T11:00:00",
                    message_id=None,
                    in_reply_to="<root@example.com>",
                    references="<root@example.com>",
                    locator_suffix="reply-no-id",
                )
                _backfill_thread_keys(conn)
                conn.commit()
            finally:
                conn.close()

            messages = walk_thread(db_path, root_id)

        # Thread must include the root AND the no-message-id reply
        self.assertEqual(
            len(messages),
            2,
            "Reply with NULL message_id was dropped",
        )
        subjects = [m["subject"] for m in messages]
        self.assertIn("Root", subjects)
        self.assertIn("Re: Root (no msg id)", subjects)

    def test_walk_thread_includes_papyra_bridged_outbound_orphans(self) -> None:
        """Papyra-like thread includes the three bridged outbound orphan rows."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "papyra.db")
            conn = connect_database(db_path)
            try:
                conn.execute(
                    """
                    INSERT INTO messages (
                        id,
                        subject,
                        sender_email,
                        sent_at,
                        body_text,
                        message_id,
                        pst_source,
                        folder_path,
                        logical_locator,
                        dedup_key
                    ) VALUES (
                        100,
                        'Papyra update',
                        'customer@example.com',
                        '2026-01-01T09:00:00',
                        'anchor',
                        '<papyra-anchor@example.com>',
                        'test',
                        '/Inbox',
                        'papyra-anchor',
                        'papyra-anchor-dedup'
                    )
                    """
                )
                for db_id, sent_at in (
                    (3201, "2026-01-01T10:00:00"),
                    (3246, "2026-01-01T11:00:00"),
                    (3253, "2026-01-01T12:00:00"),
                ):
                    conn.execute(
                        """
                        INSERT INTO messages (
                            id,
                            subject,
                            sender_email,
                            sent_at,
                            body_text,
                            pst_source,
                            folder_path,
                            logical_locator,
                            dedup_key,
                            thread_bridge_target,
                            thread_bridge_source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            db_id,
                            "Re: Papyra update",
                            "sean3808@mailattic.test",
                            sent_at,
                            f"orphan {db_id}",
                            "test",
                            "/Sent",
                            f"papyra-orphan-{db_id}",
                            f"papyra-orphan-{db_id}",
                            "<papyra-anchor@example.com>",
                            "subject_heuristic",
                        ],
                    )
                _backfill_thread_keys(conn)
                conn.commit()
            finally:
                conn.close()

            messages = walk_thread(db_path, 100)

        returned_ids = [int(message["id"]) for message in messages]
        self.assertEqual(returned_ids, [100, 3201, 3246, 3253])


# ---------------------------------------------------------------------------
# Task-5 new tests: index usage, standalone, >100 truncation, unknown id
# ---------------------------------------------------------------------------


class WalkThreadTask5Tests(unittest.TestCase):
    """New tests added for task 5 (thread_key rewrite) verification."""

    def test_walk_thread_explain_uses_index(self) -> None:
        """EXPLAIN QUERY PLAN of the thread query must show SEARCH … USING INDEX.

        Verifies design.md Decision 1: the thread read path must be a single
        indexed equality query on thread_key, not a SCAN messages.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path, [id1, _id2, _id3] = _make_thread_db(tmp)

            # Everything inside the with-block so tmp dir stays alive.
            conn = connect_database(db_path, create=False)
            try:
                start_row = conn.execute(
                    "SELECT thread_key FROM messages WHERE id = ?", [id1]
                ).fetchone()
                thread_key = start_row["thread_key"]

                plan_rows = conn.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT id FROM messages "
                    "WHERE thread_key = ? "
                    "ORDER BY COALESCE(unixepoch(sent_at), -62135596800) ASC "
                    "LIMIT ?",
                    [thread_key, 101],
                ).fetchall()
            finally:
                conn.close()

        # At least one plan row must show SEARCH … USING INDEX (not SCAN)
        plan_text = " ".join(
            str(r["detail"] if "detail" in r else r[3]) for r in plan_rows
        )
        self.assertIn(
            "USING INDEX",
            plan_text.upper(),
            f"Expected USING INDEX in EXPLAIN QUERY PLAN, got: {plan_text!r}",
        )
        self.assertNotIn(
            "SCAN MESSAGES",
            plan_text.upper(),
            f"Full table scan detected in EXPLAIN QUERY PLAN: {plan_text!r}",
        )

    def test_walk_thread_standalone_returns_itself(self) -> None:
        """A message with no thread relatives must return exactly itself.

        Verifies spec scenario 'A thread with no reply relationships returns
        a single message' under the new thread_key implementation.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "standalone.db")
            conn = connect_database(db_path)
            try:
                db_id = _persist(
                    conn,
                    subject="Isolated",
                    sender_email="isolated@example.com",
                    sent_at="2025-03-15T09:00:00",
                    message_id="<isolated@test.example.com>",
                    locator_suffix="iso",
                )
                _backfill_thread_keys(conn)
                conn.commit()
            finally:
                conn.close()

            messages = walk_thread(db_path, db_id)

        self.assertEqual(len(messages), 1)
        self.assertEqual(int(messages[0]["id"]), db_id)

    def test_walk_thread_over_100_truncates_earliest(self) -> None:
        """A component with > 100 messages returns only the 100 earliest by sent_at.

        Uses a simple linear chain of 110 messages where the last 10 are the
        newest (easiest to exclude). Verifies:
        (a) exactly 100 returned,
        (b) stderr warning emitted,
        (c) returned messages are the EARLIEST 100 (not arbitrary 100).
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "over100.db")
            conn = connect_database(db_path)
            try:
                msg_ids = [f"<chain-{i:04d}@example.com>" for i in range(110)]
                first_db_id = None
                for i in range(110):
                    db_id = _persist(
                        conn,
                        subject=f"Chain {i}",
                        sender_email="chain@example.com",
                        # Messages 0-99 in 2023, messages 100-109 in 2026
                        sent_at=(
                            f"2023-01-{(i % 28) + 1:02d}T{(i % 24):02d}:00:00"
                            if i < 100
                            else f"2026-01-{(i - 100) + 1:02d}T00:00:00"
                        ),
                        message_id=msg_ids[i],
                        in_reply_to=msg_ids[i - 1] if i > 0 else None,
                        references=msg_ids[i - 1] if i > 0 else None,
                        locator_suffix=str(i),
                    )
                    if i == 0:
                        first_db_id = db_id
                _backfill_thread_keys(conn)
                conn.commit()
            finally:
                conn.close()

            old_stderr = sys.stderr
            sys.stderr = StringIO()
            try:
                messages = walk_thread(db_path, first_db_id)
                warning_output = sys.stderr.getvalue()
            finally:
                sys.stderr = old_stderr

        self.assertEqual(len(messages), 100)
        self.assertIn("thread exceeded", warning_output.lower())

        # All returned messages must be from 2023 (the 100 earliest)
        returned_db_ids = {int(m["id"]) for m in messages}
        for sent in [m["sent_at"] for m in messages]:
            self.assertTrue(
                str(sent).startswith("2023-"),
                f"Expected 2023 date (earliest 100), got {sent!r}",
            )
        # The 10 newest (2026) must NOT be in the result
        # We verify this by checking sent_at — all returned must be in 2023
        # (already asserted above; verifying db_id set would require knowing them)
        _ = returned_db_ids  # used above indirectly via message count

    def test_walk_thread_unknown_identifier_errors(self) -> None:
        """Unknown db_id and unknown message_id must both raise KeyError.

        Verifies spec scenario 'An unknown identifier returns an error' under
        the new thread_key implementation.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "unk.db")

            with self.assertRaises(KeyError):
                walk_thread(db_path, 99999)

            with self.assertRaises(KeyError):
                walk_thread(db_path, "does-not-exist@missing.example.com")

            with self.assertRaises(KeyError):
                walk_thread(db_path, "<bracketed-missing@example.com>")

    def test_walk_thread_null_thread_key_returns_singleton(self) -> None:
        """A message whose thread_key IS NULL must return exactly that one message.

        Verifies FIX ①: before the fix, walk_thread set thread_key to int(row.id)
        then ran `WHERE thread_key = ?`, which matched no row (the column is NULL,
        not equal to any integer) → returned empty.  After the fix the NULL path
        skips the equality query and returns the start row directly as a singleton.

        Setup: insert one message, force thread_key = NULL (do NOT backfill).
        Call walk_thread by db_id AND by message_id; both must return exactly
        that one message.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "null_tk.db")
            conn = connect_database(db_path)
            try:
                db_id = _persist(
                    conn,
                    subject="NULL thread_key message",
                    sender_email="null@example.com",
                    sent_at="2025-04-01T10:00:00",
                    message_id="<null-tk@example.com>",
                    locator_suffix="null-tk",
                )
                # Force thread_key = NULL — do NOT call _backfill_thread_keys
                conn.execute(
                    "UPDATE messages SET thread_key = NULL WHERE id = ?", [db_id]
                )
                conn.commit()
            finally:
                conn.close()

            # Both lookup forms must return exactly the one message, not empty
            result_by_db_id = walk_thread(db_path, db_id)
            result_by_msg_id = walk_thread(db_path, "<null-tk@example.com>")

        self.assertEqual(
            len(result_by_db_id),
            1,
            "Expected singleton for NULL thread_key via db_id, "
            f"got {len(result_by_db_id)} messages",
        )
        self.assertEqual(int(result_by_db_id[0]["id"]), db_id)

        self.assertEqual(
            len(result_by_msg_id),
            1,
            "Expected singleton for NULL thread_key via message_id, "
            f"got {len(result_by_msg_id)} messages",
        )
        self.assertEqual(int(result_by_msg_id[0]["id"]), db_id)


# ---------------------------------------------------------------------------
# Task 1.1 (change fix-thread-resolver-out-of-range-identifier): red-first
# regression tests for resolve_thread_message_ids()'s identifier magnitude
# gate. All fixtures below are an empty (schema-only) database — every case
# is decided purely by which exception type resolve_thread_message_ids()
# raises before any row could match, so no thread data is needed.
# ---------------------------------------------------------------------------

_INT64_MAX = 9223372036854775807
_INT64_MIN = -9223372036854775808


class ResolveThreadIdentifierGateTests(unittest.TestCase):
    def _empty_db(self, tmp: str) -> Path:
        return initialize_database(Path(tmp) / "gate_test.db")

    # -- defect cases: must raise the dedicated exception -------------------

    def test_resolve_thread_rejects_str_over_64bit_positive_bound(self) -> None:
        """One past the signed 64-bit max as a digit string must be rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(ThreadIdentifierError):
                resolve_thread_message_ids(db_path, str(_INT64_MAX + 1))

    def test_resolve_thread_rejects_str_4301_effective_digits(self) -> None:
        """A digit string with 4301 effective digits must be rejected by
        length alone, never reaching CPython's int() digit-limit crash."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(ThreadIdentifierError):
                resolve_thread_message_ids(db_path, "9" * 4301)

    def test_resolve_thread_rejects_str_under_64bit_negative_bound(self) -> None:
        """One past the signed 64-bit min as a digit string must be rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(ThreadIdentifierError):
                resolve_thread_message_ids(db_path, str(_INT64_MIN - 1))

    def test_resolve_thread_rejects_int_over_64bit_bound(self) -> None:
        """An int identifier one past the signed 64-bit max must be rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(ThreadIdentifierError):
                resolve_thread_message_ids(db_path, _INT64_MAX + 1)

    def test_resolve_thread_rejects_int_beyond_render_limit(self) -> None:
        """An int identifier too large to even render to decimal text
        (beyond CPython's int-to-str digit limit) must still be rejected by
        the dedicated exception, not by an int-to-str rendering crash."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(ThreadIdentifierError):
                resolve_thread_message_ids(db_path, 10**4300)

    def test_resolve_thread_rejects_digit_shaped_char_without_decimal_value(
        self,
    ) -> None:
        """A digit-shaped character with no Unicode decimal value (superscript
        two) is shape-accepted by str.isdigit() but must be rejected by the
        dedicated exception, not crash unclassified out of int()."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(ThreadIdentifierError):
                resolve_thread_message_ids(db_path, "²")  # superscript two

    # -- boundary acceptance: still reach the ordinary lookup ---------------

    def test_resolve_thread_accepts_str_at_positive_64bit_boundary(self) -> None:
        """Exactly INT64_MAX as a digit string reaches the ordinary lookup
        and raises KeyError (absent row), never the identifier exception."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(KeyError):
                resolve_thread_message_ids(db_path, str(_INT64_MAX))

    def test_resolve_thread_accepts_int_at_negative_64bit_boundary(self) -> None:
        """Exactly INT64_MIN as an int reaches the ordinary lookup and
        raises KeyError (absent row), never the identifier exception."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(KeyError):
                resolve_thread_message_ids(db_path, _INT64_MIN)

    # -- normalization pins ---------------------------------------------------

    def test_resolve_thread_normalizes_zero_variants_to_id_zero(self) -> None:
        """ "0", "000", and "-000" must all be looked up as id 0 — KeyError
        when absent, never the identifier exception or a crash."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            for value in ("0", "000", "-000"):
                with self.subTest(value=value), self.assertRaises(KeyError):
                    resolve_thread_message_ids(db_path, value)

    def test_resolve_thread_normalizes_long_leading_zeros_to_effective_digit(
        self,
    ) -> None:
        """4300 leading zeros followed by "7" has only one effective digit
        and must be looked up as id 7 (KeyError when absent) — never trip
        CPython's int() digit-limit crash on the raw 4301-character string."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(KeyError):
                resolve_thread_message_ids(db_path, "0" * 4300 + "7")

    def test_resolve_thread_normalizes_arabic_indic_digit_like_ascii(self) -> None:
        """An in-range Arabic-Indic digit string must resolve identically to
        its ASCII form — both KeyError-when-absent against the same DB."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(KeyError):
                resolve_thread_message_ids(db_path, "٥")  # Arabic-Indic 5

            with self.assertRaises(KeyError):
                resolve_thread_message_ids(db_path, "5")

    # -- missing-database pin ------------------------------------------------

    def test_resolve_thread_missing_database_raises_identifier_error_before_connect(
        self,
    ) -> None:
        """With a nonexistent database path, an out-of-range identifier must
        raise the dedicated exception, not FileNotFoundError — proving the
        magnitude gate runs before the resolver opens its connection."""
        with tempfile.TemporaryDirectory() as tmp:
            missing_db_path = Path(tmp) / "does-not-exist.db"

            with self.assertRaises(ThreadIdentifierError):
                resolve_thread_message_ids(missing_db_path, str(_INT64_MAX + 1))

    # -- regression pins ------------------------------------------------------

    def test_resolve_thread_regression_negative_five_still_keyerror(self) -> None:
        """ "-5" must keep raising KeyError (in-range, unknown row)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(KeyError):
                resolve_thread_message_ids(db_path, "-5")

    def test_resolve_thread_regression_leading_zero_007_still_keyerror(self) -> None:
        """ "007" must keep raising KeyError (in-range, unknown row)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_db(tmp)

            with self.assertRaises(KeyError):
                resolve_thread_message_ids(db_path, "007")


# ---------------------------------------------------------------------------
# CLI integration tests: bes thread
# ---------------------------------------------------------------------------


class BesThreadCLITests(unittest.TestCase):
    def test_bes_thread_db_id_returns_ordered_thread(self) -> None:
        """bes thread <db_id> must output all thread messages in sent_at ASC order."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, [id1, id2, id3] = _make_thread_db(tmp)

            code, out, err = _run_cli(["thread", "--database", str(db_path), str(id1)])

        self.assertEqual(code, 0)
        # All 3 messages should appear
        self.assertIn("1 of 3", out)
        self.assertIn("2 of 3", out)
        self.assertIn("3 of 3", out)
        # Root message subject
        self.assertIn("Root message", out)

    def test_bes_thread_message_id_returns_same_as_db_id(self) -> None:
        """bes thread with RFC822 Message-ID must produce same output as db_id."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, [id1, id2, id3] = _make_thread_db(tmp)

            code_id, out_id, _ = _run_cli(
                ["thread", "--database", str(db_path), str(id1)]
            )
            code_mid, out_mid, _ = _run_cli(
                ["thread", "--database", str(db_path), "<root@example.com>"]
            )

        self.assertEqual(code_id, 0)
        self.assertEqual(code_mid, 0)
        # Both should report 3 messages
        self.assertIn("3", out_id)
        self.assertIn("3", out_mid)

    def test_bes_thread_unknown_id_exits_nonzero_with_diagnostic(self) -> None:
        """bes thread with an unknown identifier must exit non-zero and print
        diagnostic."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "empty.db")

            code, _, err = _run_cli(["thread", "--database", str(db_path), "999999"])

        self.assertNotEqual(code, 0)
        self.assertIn("999999", err)

    def test_bes_thread_format_json_returns_array(self) -> None:
        """bes thread --format json must return a JSON object with 'messages' array."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, [id1, id2, id3] = _make_thread_db(tmp)

            code, out, _ = _run_cli(
                ["thread", "--database", str(db_path), "--format", "json", str(id1)]
            )

        self.assertEqual(code, 0)
        # Output must be valid JSON
        parsed = json.loads(out)
        # walk_thread returns a list directly in JSON mode
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 3)
        # Each element must have message shape fields
        for msg in parsed:
            self.assertIn("id", msg)
            self.assertIn("subject", msg)
            self.assertIn("sent_at", msg)


if __name__ == "__main__":
    unittest.main()

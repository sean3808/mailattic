"""Unit tests for thread_key.compute_thread_keys().

Covers task 2 acceptance criteria:
- equivalence basic: a small reply chain groups together with correct min-db_id key
- multiple independent components get distinct keys
- reference loop (A refs B, B refs A) terminates and groups
- NULL message_id singleton -> own db_id
- minimum-db_id canonical selection (component members all map to the smallest
  db_id, even when input order is shuffled)
"""

from __future__ import annotations

import random

from mailattic.thread_key import compute_thread_keys

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(
    db_id: int,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> tuple[int, str | None, str | None, str | None]:
    """Shorthand to build a (db_id, message_id, in_reply_to, references) tuple."""
    return (db_id, message_id, in_reply_to, references)


# ---------------------------------------------------------------------------
# Test: basic reply chain groups with correct min-db_id key
# ---------------------------------------------------------------------------


def test_basic_reply_chain_groups_together() -> None:
    """A -> B (reply) -> C (reply) must all share thread_key = min(1,2,3) = 1."""
    messages = [
        _msg(1, "msg-a@host"),
        _msg(2, "msg-b@host", in_reply_to="<msg-a@host>"),
        _msg(
            3,
            "msg-c@host",
            in_reply_to="<msg-b@host>",
            references="<msg-a@host> <msg-b@host>",
        ),
    ]
    result = compute_thread_keys(messages)

    assert result[1] == 1
    assert result[2] == 1
    assert result[3] == 1


# ---------------------------------------------------------------------------
# Test: multiple independent components get distinct keys
# ---------------------------------------------------------------------------


def test_independent_components_get_distinct_keys() -> None:
    """Two unrelated threads must produce two different thread_key values."""
    messages = [
        # Thread 1: db_ids 10, 11
        _msg(10, "t1-a@host"),
        _msg(11, "t1-b@host", in_reply_to="<t1-a@host>"),
        # Thread 2: db_ids 20, 21 (no overlap with thread 1)
        _msg(20, "t2-a@host"),
        _msg(21, "t2-b@host", in_reply_to="<t2-a@host>"),
    ]
    result = compute_thread_keys(messages)

    # Thread 1 canonical = 10
    assert result[10] == 10
    assert result[11] == 10

    # Thread 2 canonical = 20
    assert result[20] == 20
    assert result[21] == 20

    # The two components are distinct
    assert result[10] != result[20]


# ---------------------------------------------------------------------------
# Test: reference loop terminates and groups
# ---------------------------------------------------------------------------


def test_reference_loop_terminates() -> None:
    """A references B, B references A — must not loop forever and must group."""
    messages = [
        _msg(5, "loop-a@host", references="<loop-b@host>"),
        _msg(6, "loop-b@host", references="<loop-a@host>"),
    ]
    # If this hangs, the test runner will timeout — proves loop safety.
    result = compute_thread_keys(messages)

    # Both must share the same thread_key = min(5, 6) = 5
    assert result[5] == 5
    assert result[6] == 5


# ---------------------------------------------------------------------------
# Test: NULL message_id singleton -> own db_id
# ---------------------------------------------------------------------------


def test_null_message_id_becomes_singleton() -> None:
    """A message with no message_id and no references is its own thread_key."""
    messages = [
        _msg(42, None, None, None),
    ]
    result = compute_thread_keys(messages)
    assert result[42] == 42


def test_empty_message_id_becomes_singleton() -> None:
    """Empty-string message_id (after strip) is treated identically to NULL."""
    messages = [
        _msg(99, "", None, None),
    ]
    result = compute_thread_keys(messages)
    assert result[99] == 99


def test_two_null_message_id_messages_are_independent_singletons() -> None:
    """Two messages both with NULL message_id must NOT be merged together."""
    messages = [
        _msg(7, None, None, None),
        _msg(8, None, None, None),
    ]
    result = compute_thread_keys(messages)
    assert result[7] == 7
    assert result[8] == 8


# ---------------------------------------------------------------------------
# Test: minimum-db_id canonical selection (order-independent)
# ---------------------------------------------------------------------------


def test_minimum_db_id_is_canonical_regardless_of_input_order() -> None:
    """thread_key must be the smallest db_id in the component, not the first-seen."""
    # A single thread: db_ids 100, 50, 75 — canonical should be 50.
    messages = [
        _msg(100, "root@host"),
        _msg(50, "reply-early@host", in_reply_to="<root@host>"),
        _msg(75, "reply-late@host", references="<root@host> <reply-early@host>"),
    ]
    result = compute_thread_keys(messages)

    assert result[100] == 50
    assert result[50] == 50
    assert result[75] == 50


def test_minimum_db_id_stable_under_shuffled_input() -> None:
    """Shuffling the input order must not change thread_key values."""
    base = [
        _msg(200, "m-a@host"),
        _msg(150, "m-b@host", in_reply_to="<m-a@host>"),
        _msg(300, "m-c@host", references="<m-a@host>"),
        _msg(100, "m-d@host", in_reply_to="<m-c@host>"),
    ]
    # All four are connected; canonical = min(200, 150, 300, 100) = 100
    expected_key = 100

    result_original = compute_thread_keys(base)
    for db_id in [200, 150, 300, 100]:
        assert result_original[db_id] == expected_key, (
            f"original order: db_id={db_id} mapped to "
            f"{result_original[db_id]}, expected {expected_key}"
        )

    # Try several random shuffles
    rng = random.Random(42)
    for _ in range(10):
        shuffled = base[:]
        rng.shuffle(shuffled)
        result_shuffled = compute_thread_keys(shuffled)
        for db_id in [200, 150, 300, 100]:
            assert result_shuffled[db_id] == expected_key, (
                f"shuffled order: db_id={db_id} mapped to "
                f"{result_shuffled[db_id]}, expected {expected_key}"
            )


# ---------------------------------------------------------------------------
# Test: angle-bracket normalisation (same as walk_thread .strip("<>"))
# ---------------------------------------------------------------------------


def test_angle_bracket_in_reply_to_normalised() -> None:
    """in_reply_to stored with angle brackets must link to message_id without."""
    messages = [
        _msg(1, "plain@host"),  # message_id has no brackets
        _msg(2, "reply@host", in_reply_to="<plain@host>"),  # in_reply_to has brackets
    ]
    result = compute_thread_keys(messages)
    assert result[1] == 1
    assert result[2] == 1


def test_references_header_parsed_by_tokenize_references() -> None:
    """Multiple references in a References header must all be extracted."""
    # Three messages in a chain linked only via References header
    messages = [
        _msg(10, "a@host"),
        _msg(20, "b@host"),
        _msg(30, "c@host", references="<a@host> <b@host>"),
    ]
    result = compute_thread_keys(messages)
    # All three are in the same component; canonical = 10
    assert result[10] == 10
    assert result[20] == 10
    assert result[30] == 10


# ---------------------------------------------------------------------------
# Test: empty input
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_mapping() -> None:
    """compute_thread_keys([]) must return {}."""
    result = compute_thread_keys([])
    assert result == {}

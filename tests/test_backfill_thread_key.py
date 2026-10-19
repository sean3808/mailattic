"""Tests for scripts/backfill_thread_key.py — Task 3 + 4 acceptance criteria.

Covers:
- Backfill writes correct thread_key values to a temp DB.
- Idempotency: second run yields identical values, exits 0.
- PRAGMA/SELECT spot-check: component rows share the min-db_id thread_key.
- Equivalence verify: all diffs attributed (IDENTICAL or EXPLAINED), no UNEXPLAINED.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from backfill_thread_key import run_backfill, run_verify  # noqa: E402

from mailattic.storage import initialize_database  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal schema — used for backfill-only tests (tests 1-4, 6).
# Does NOT include received_at / source_input_mode etc. — sufficient for
# backfill which only reads id/message_id/in_reply_to/references_header.
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY,
    message_id       TEXT,
    in_reply_to      TEXT,
    references_header TEXT,
    sent_at          TEXT,
    subject          TEXT DEFAULT '',
    sender_email     TEXT DEFAULT '',
    sender_name      TEXT DEFAULT '',
    body_text        TEXT DEFAULT '',
    folder_path      TEXT DEFAULT '',
    pst_source       TEXT DEFAULT 'test',
    logical_locator  TEXT DEFAULT '',
    dedup_key        TEXT DEFAULT '',
    recipient_search_text TEXT DEFAULT '',
    attachment_search_text TEXT DEFAULT '',
    thread_key       INTEGER
);

CREATE TABLE IF NOT EXISTS message_recipients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   INTEGER,
    recipient_type TEXT DEFAULT 'to',
    email        TEXT DEFAULT '',
    display_name TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS message_attachments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id       INTEGER,
    attachment_name  TEXT DEFAULT '',
    content_hash     TEXT,
    size_bytes       INTEGER DEFAULT 0,
    mime_type        TEXT DEFAULT '',
    storage_path     TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    message_rowid UNINDEXED,
    subject,
    sender,
    recipients,
    body_text,
    attachment_names
);
"""


def _make_db(tmp_path: Path, rows: list[tuple], *, suffix: str = "test.db") -> Path:
    """Create a minimal temp DB with given message rows.

    Each row is (id, message_id, in_reply_to, references_header, sent_at).
    Uses a hand-rolled schema sufficient for backfill (no walk_thread calls).
    """
    db_path = tmp_path / suffix
    con = sqlite3.connect(db_path)
    con.executescript(_SCHEMA)
    con.executemany(
        "INSERT INTO messages(id, message_id, in_reply_to, references_header, sent_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()
    return db_path


def _make_full_db(
    tmp_path: Path, rows: list[tuple], *, suffix: str = "full.db"
) -> Path:
    """Create a temp DB using the real production schema (needed for walk_thread).

    Each row is (message_id, in_reply_to, references_header, sent_at).
    Uses initialize_database so get_message() doesn't hit missing-column errors.
    Returns db_path; inserted rows get auto-assigned ids by AUTOINCREMENT.
    """
    db_path = tmp_path / suffix
    initialize_database(db_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    for message_id, in_reply_to, references_header, sent_at in rows:
        dedup_key = f"dedup-{message_id or 'none'}"
        con.execute(
            "INSERT INTO messages("
            "  message_id, in_reply_to, references_header, sent_at,"
            "  pst_source, folder_path, logical_locator, dedup_key"
            ") VALUES (?, ?, ?, ?, 'test', 'INBOX', '', ?)",
            [message_id, in_reply_to, references_header, sent_at, dedup_key],
        )
    con.commit()
    con.close()
    return db_path


# ---------------------------------------------------------------------------
# Helper: read thread_key column for a set of db_ids
# ---------------------------------------------------------------------------


def _read_thread_keys(db_path: Path) -> dict[int, int | None]:
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT id, thread_key FROM messages").fetchall()
    con.close()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# Test 1: Basic backfill assigns correct thread_key values
# ---------------------------------------------------------------------------


def test_backfill_assigns_correct_thread_keys(tmp_path: Path) -> None:
    """Three messages in one thread; two unrelated singletons.

    Thread A: db_ids 10, 20, 30 — 20 replies to 10, 30 replies to 20.
    Singletons: 99, 100 — no references.
    Expected: 10→10, 20→10, 30→10, 99→99, 100→100.
    """
    rows = [
        (10, "root@test", None, None, "2024-01-01"),
        (20, "reply1@test", "<root@test>", None, "2024-01-02"),
        (30, "reply2@test", "<reply1@test>", "<root@test> <reply1@test>", "2024-01-03"),
        (99, "lone1@test", None, None, "2024-01-04"),
        (100, "lone2@test", None, None, "2024-01-05"),
    ]
    db_path = _make_db(tmp_path, rows)

    run_backfill(db_path)

    tks = _read_thread_keys(db_path)
    assert tks[10] == 10
    assert tks[20] == 10
    assert tks[30] == 10
    assert tks[99] == 99
    assert tks[100] == 100


# ---------------------------------------------------------------------------
# Test 2: Idempotency — second run produces identical values, no error
# ---------------------------------------------------------------------------


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    """Running backfill twice must yield identical thread_key values."""
    rows = [
        (1, "a@test", None, None, "2024-01-01"),
        (2, "b@test", "<a@test>", None, "2024-01-02"),
        (3, "c@test", None, "<a@test>", "2024-01-03"),
    ]
    db_path = _make_db(tmp_path, rows)

    run_backfill(db_path)
    first_run = _read_thread_keys(db_path)

    run_backfill(db_path)
    second_run = _read_thread_keys(db_path)

    assert first_run == second_run, (
        f"Second run changed thread_keys: first={first_run}, second={second_run}"
    )


# ---------------------------------------------------------------------------
# Test 3: PRAGMA/SELECT spot-check — component rows share min-db_id thread_key
# ---------------------------------------------------------------------------


def test_component_rows_share_min_db_id_thread_key(tmp_path: Path) -> None:
    """Spec example: ids 51474, 51475, 51480 all linked → thread_key = 51474."""
    rows = [
        (51474, "root@example", None, None, "2024-01-01"),
        (51475, "msg1@example", "<root@example>", None, "2024-01-02"),
        (51480, "msg2@example", "<root@example>", None, "2024-01-03"),
    ]
    db_path = _make_db(tmp_path, rows)

    run_backfill(db_path)

    con = sqlite3.connect(db_path)
    rows_out = con.execute("SELECT id, thread_key FROM messages ORDER BY id").fetchall()
    con.close()

    for row in rows_out:
        assert row[1] == 51474, f"id={row[0]} has thread_key={row[1]}, expected 51474"


# ---------------------------------------------------------------------------
# Test 4: NULL message_id messages each become singletons
# ---------------------------------------------------------------------------


def test_null_message_id_singletons(tmp_path: Path) -> None:
    """Messages with NULL message_id and no references → each is own singleton."""
    rows = [
        (5, None, None, None, "2024-01-01"),
        (6, None, None, None, "2024-01-02"),
    ]
    db_path = _make_db(tmp_path, rows)

    run_backfill(db_path)

    tks = _read_thread_keys(db_path)
    assert tks[5] == 5
    assert tks[6] == 6
    assert tks[5] != tks[6]


# ---------------------------------------------------------------------------
# Test 5: Equivalence verify — no unexplained divergences on the temp DB
# ---------------------------------------------------------------------------


def test_verify_no_unexplained_divergences(tmp_path: Path) -> None:
    """After backfill, run_verify must return True (gate passes, UNEXPLAINED=0).

    Uses _make_full_db (real production schema) so walk_thread / get_message
    do not fail on missing columns (received_at, source_input_mode, etc.).
    Two threads: alpha-beta-gamma linked; delta-epsilon linked.
    walk_thread and thread_key queries should agree exactly on this tiny DB.
    """
    # rows: (message_id, in_reply_to, references_header, sent_at)
    rows = [
        ("alpha@test", None, None, "2024-01-01T00:00:00"),
        ("beta@test", "<alpha@test>", None, "2024-01-02T00:00:00"),
        (
            "gamma@test",
            "<beta@test>",
            "<alpha@test> <beta@test>",
            "2024-01-03T00:00:00",
        ),
        ("delta@test", None, None, "2024-01-04T00:00:00"),
        ("epsilon@test", "<delta@test>", None, "2024-01-05T00:00:00"),
    ]
    db_path = _make_full_db(tmp_path, rows)

    run_backfill(db_path)

    # Verify with sample covering all distinct thread_key values (2 components)
    ok = run_verify(db_path, sample_n=10, seed=0)
    assert ok, "run_verify returned False — unexplained divergences found"


# ---------------------------------------------------------------------------
# Test 6: Cross-component references header (all in one component)
# ---------------------------------------------------------------------------


def test_references_header_links_across_component(tmp_path: Path) -> None:
    """A message referencing two unrelated message_ids merges their components."""
    rows = [
        (10, "x@test", None, None, "2024-01-01"),
        (20, "y@test", None, None, "2024-01-02"),
        # This message references both x and y → merges all three into one component
        (30, "z@test", None, "<x@test> <y@test>", "2024-01-03"),
    ]
    db_path = _make_db(tmp_path, rows)

    run_backfill(db_path)

    tks = _read_thread_keys(db_path)
    # All three should share the smallest db_id = 10
    assert tks[10] == 10
    assert tks[20] == 10
    assert tks[30] == 10


# ---------------------------------------------------------------------------
# Test 7: Divergence detection — run_verify returns False on UNEXPLAINED diff
# ---------------------------------------------------------------------------


def test_verify_detects_unexplained_divergence(tmp_path: Path) -> None:
    r"""Prove run_verify is NON-vacuous: it returns False on a real divergence.

    Construction:
      - root (id=1, message_id="root@test") and child (id=2, message_id="child@test",
        in_reply_to="<root@test>") form a single thread component.
      - run_backfill correctly assigns both thread_key=1.
      - We then manually corrupt child's thread_key to 2, simulating a backfill
        failure where the child is separated into its own component.
      - Now old _old_bfs_component starting from db_id=1 will follow in_reply_to
        and find db_id=2 (old_set = {1, 2}).
      - But new_set query (WHERE thread_key = 1 LIMIT 100) returns only {1}.
      - The diff {2} in old_set \ new_set is not explained by LIKE-false-positive
        (child's references_header is NULL) and not by truncation (component size=1).
      - run_verify must detect this UNEXPLAINED diff and return False.

    This test is the non-vacuousness proof: if run_verify always returned True
    (as it did when walk_thread raised NameError and was silently swallowed),
    this test would fail.
    """
    rows = [
        (1, "root@test", None, None, "2024-01-01"),
        (2, "child@test", "<root@test>", None, "2024-01-02"),
        # Unrelated singleton — ensures at least 2 thread_key values exist
        # so run_verify has something to sample beyond the corrupted component.
        (3, "lone@test", None, None, "2024-01-03"),
    ]
    db_path = _make_db(tmp_path, rows)

    # Backfill: all correct — root=1→tk=1, child=2→tk=1, lone=3→tk=3
    run_backfill(db_path)

    # Corrupt: detach child from root's component (simulate backfill bug)
    con = sqlite3.connect(db_path)
    con.execute("UPDATE messages SET thread_key = 2 WHERE id = 2")
    con.commit()
    con.close()

    # Now thread_key=1 component = {1}, thread_key=2 component = {2}, tk=3 → {3}
    # _old_bfs_component from id=1 follows in_reply_to of id=2 → visits {1, 2}
    # new_set for thread_key=1 → {1}  (id=2 has thread_key=2 now)
    # diff: {2} in old_set \ new_set — not LIKE-FP (refs_header is NULL),
    #        not truncation (tk=1 has only 1 message) → UNEXPLAINED

    # run_verify samples from existing thread_key values {1, 2, 3}; with seed=0
    # it will hit thread_key=1 which produces the unexplained divergence.
    # Sample all 3 to guarantee we hit it.
    result = run_verify(db_path, sample_n=3, seed=0)
    assert result is False, (
        "run_verify returned True on a known UNEXPLAINED divergence — "
        "gate is VACUOUS (not detecting divergences)"
    )


# ---------------------------------------------------------------------------
# Test 8: FIX ④ — _classify_diff LIKE-FP uses bracketed match
# ---------------------------------------------------------------------------


def test_classify_diff_bare_id_in_references_not_classified_as_fp(
    tmp_path: Path,
) -> None:
    r"""Candidate whose references_header has bare `id` but NOT `<id>` is UNEXPLAINED.

    Construction:
      - root (id=1, message_id="root@test") is the sampled component root.
      - candidate (id=2): references_header contains "root@test" (bare, no angle
        brackets) but NOT "<root@test>".  Also, thread_key=2 (detached).
      - old BFS from id=1 will NOT pick up id=2 via the bracketed LIKE
        `references_header LIKE '%<root@test>%'` because `<root@test>` is not
        present as a substring.  So old_set = {1}, new_set = {1}.
      - After manual corrupt: old BFS gives {1}, new gives {1} → IDENTICAL.
        That's not what we want to test.

    What we actually need to test: when _classify_diff receives a diff where
    a candidate row has bare `id` in references_header but NOT `<id>`,
    _classify_diff must NOT classify it as a LIKE-false-positive.

    We test _classify_diff directly:
      - old_set = {1, 2}, new_set = {1}
      - id=2 has references_header = "root@test" (bare, no brackets)
      - After FIX ④: bracketed LIKE `%<root@test>%` does NOT match → id=2 not
        classified as FP → verdict = UNEXPLAINED (gate returns False).
      - Before FIX ④: bare LIKE `%root@test%` WOULD match → id=2 wrongly
        classified as FP → verdict = EXPLAINED (gate falsely passes).
    """
    import sqlite3 as _sqlite3

    from backfill_thread_key import _classify_diff

    # Build a minimal DB with 2 messages
    db_path = tmp_path / "fp_test.db"
    con = _sqlite3.connect(db_path)
    con.executescript(_SCHEMA)
    _ins = (
        "INSERT INTO messages"
        "(id, message_id, in_reply_to, references_header, sent_at, thread_key)"
        " VALUES (?, ?, ?, ?, ?, ?)"
    )
    # id=1: root
    con.execute(_ins, [1, "root@test", None, None, "2024-01-01", 1])
    # id=2: bare "root@test" in references (no angle brackets)
    con.execute(_ins, [2, "other@test", None, "root@test", "2024-01-02", 1])
    con.commit()
    con.row_factory = _sqlite3.Row

    result = _classify_diff(
        con,
        sampled_db_id=1,
        root_message_id="<root@test>",
        thread_key=1,
        old_set={1, 2},
        new_set={1},
    )
    con.close()

    # id=2 has bare "root@test" but NOT "<root@test>" → not a LIKE-FP → UNEXPLAINED
    assert result["verdict"] == "UNEXPLAINED", (
        f"Expected UNEXPLAINED (bare id not bracketed → not a real LIKE-FP), "
        f"got {result['verdict']!r}. like_fp_ids={result['like_false_positive_ids']}"
    )
    assert 2 in result["unexplained_ids"], (
        f"id=2 should be in unexplained_ids, got {result['unexplained_ids']}"
    )


# ---------------------------------------------------------------------------
# Test 9: FIX ③ — >100 component: divergence INSIDE earliest-100 is UNEXPLAINED
# ---------------------------------------------------------------------------


def test_classify_diff_large_component_inside_earliest_100_is_unexplained(
    tmp_path: Path,
) -> None:
    r"""A >100 component divergence INSIDE the earliest-100 region is UNEXPLAINED.

    Construction:
      - 105 messages in thread_key=1: ids 1..105
        - ids 1..100: sent_at 2024-01-01 .. 2024-04-10 (earliest 100)
        - ids 101..105: sent_at 2025-01-01 .. 2025-01-05 (outside earliest 100)
      - old_set = {1..100, 102}  (id=102 is inside earliest-100? No — id=102 is
        outside earliest-100 range by sent_at, but id=50 is inside and is
        mismatched)
      - We construct: old_set = {1..50, 52..101}, new_set = {1..100}
        i.e. id=51 is in new but not old; id=101 is in old but not new.
        id=51 sent_at is 2024-02-20 (inside earliest-100).
        id=101 sent_at is 2025-01-01 (outside earliest-100) → attributed to truncation.
        id=51 must be UNEXPLAINED.
      - run_verify should classify this as UNEXPLAINED (gate FAIL).

    This proves FIX ③: the old code blanket-attributed ALL diffs to truncation
    when total_component > 100 — after the fix, only ids OUTSIDE earliest-100
    are truncation-attributed.
    """
    import sqlite3 as _sqlite3

    from backfill_thread_key import _classify_diff

    db_path = tmp_path / "large_comp.db"
    con = _sqlite3.connect(db_path)
    con.executescript(_SCHEMA)

    # Insert 105 messages: ids 1..100 in 2024, ids 101..105 in 2025
    for i in range(1, 106):
        if i <= 100:
            # 100 messages: 2024-01-01 .. 2024-04-10 (roughly 3 days apart)
            month = ((i - 1) // 30) + 1  # months 1..4
            day = ((i - 1) % 30) + 1
            sent_at = f"2024-{month:02d}-{day:02d}T00:00:00"
        else:
            sent_at = f"2025-01-{i - 100:02d}T00:00:00"
        con.execute(
            "INSERT INTO messages"
            "(id, message_id, in_reply_to, references_header, sent_at, thread_key)"
            " VALUES (?, ?, NULL, NULL, ?, 1)",
            [i, f"msg{i}@test", sent_at],
        )
    con.commit()
    con.row_factory = _sqlite3.Row

    # Construct old_set and new_set such that:
    #   id=51 is in new_set (earliest-100) but NOT in old_set → diff inside earliest-100
    #   id=101 is in old_set (outside earliest-100) but NOT in new_set → truncation diff
    old_set = set(range(1, 51)) | set(range(52, 102))  # ids 1..50, 52..101
    new_set = set(range(1, 101))  # ids 1..100 (earliest by sent_at)

    result = _classify_diff(
        con,
        sampled_db_id=1,
        root_message_id="<msg1@test>",
        thread_key=1,
        old_set=old_set,
        new_set=new_set,
    )
    con.close()

    # id=101 is outside earliest-100 → truncation-attributed (explained)
    # id=51 is inside earliest-100 and not a LIKE-FP → must be UNEXPLAINED
    assert result["verdict"] == "UNEXPLAINED", (
        f"Expected UNEXPLAINED (id=51 inside earliest-100 is not truncation), "
        f"got {result['verdict']!r}. unexplained_ids={result['unexplained_ids']}"
    )
    assert 51 in result["unexplained_ids"], (
        f"id=51 should be unexplained (inside earliest-100), "
        f"got unexplained_ids={result['unexplained_ids']}"
    )
    # id=101 should NOT be in unexplained (it is outside earliest-100 → truncation)
    assert 101 not in result["unexplained_ids"], (
        f"id=101 is outside earliest-100, should be truncation-attributed, "
        f"but appears in unexplained_ids={result['unexplained_ids']}"
    )

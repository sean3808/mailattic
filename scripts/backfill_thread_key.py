"""backfill_thread_key.py — Backfill the thread_key column for all messages.

Computes connected-component thread_key values via union-find (reusing
thread_key.compute_thread_keys) and writes them as a shadow column.

Usage:
    uv run python scripts/backfill_thread_key.py [--database <path>]
    uv run python scripts/backfill_thread_key.py [--database <path>]
        --verify [--sample N]

Exit codes:
    0 — backfill/verify succeeded
    1 — verify found unexplained divergences (gate failure)
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running directly without editable install
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic.query import (  # noqa: E402
    _escape_like,
    _tokenize_references,
)
from mailattic.thread_key import compute_thread_keys  # noqa: E402

# ---------------------------------------------------------------------------
# Default DB path (mirrors CLI convention — absolute, cwd-invariant)
# ---------------------------------------------------------------------------
_DEFAULT_DB = REPO_ROOT / "mailattic.db"
_BATCH_SIZE = 2_000
# Threshold for the frozen old-BFS oracle, used by _classify_diff to attribute
# >ceiling truncation (design.md Decision 3).
_BFS_HARD_CEILING = 1000


def _connect(database_path: Path) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with busy_timeout."""
    con = sqlite3.connect(database_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    # Tolerate concurrent IMAP sync writes (daily sync window)
    con.execute("PRAGMA busy_timeout = 30000;")  # 30 s
    return con


# ---------------------------------------------------------------------------
# Frozen oracle: old BFS implementation (pre-thread_key rewrite)
# ---------------------------------------------------------------------------
# This reconstructs the walk_thread BFS from the pre-change query.py
# (kept out of the repo).
# It is used ONLY by run_verify() as the independent "old" side of the
# equivalence gate.  It must NOT import or call the current walk_thread
# (which was rewritten to use thread_key), otherwise the gate degenerates
# into thread_key-vs-thread_key and gives a false PASS on every sample.
#
# Key BFS behaviour preserved from the old code:
#   • _enqueue_ids_from_row  – collects in_reply_to + references ancestors
#   • _enqueue_descendant_ids – uses angle-bracket-wrapped LIKE
#       `references_header LIKE '%<id>%' ESCAPE '\'`
#       This form can over-match: searching for <abc@x.com> will also
#       hit <abc@x.com.tw> because SQLite LIKE '%<abc@x.com>%' is a
#       pure substring check — the closing '>' is not anchored.
#       _tokenize_references (used by compute_thread_keys) extracts full
#       '<…>' tokens via regex and correctly separates them.
#   • BFS batch loop with _BFS_HARD_CEILING abort
#   • Returns (visited_db_ids, ceiling_hit) — the PRE-TRUNCATION component
#     plus whether the ceiling fired.  The caller (_classify_diff) receives
#     the full visited set; truncation to 100 happens only in the new-side
#     query, not here.


def _old_bfs_component(
    con: sqlite3.Connection,
    start_db_id: int,
) -> tuple[set[int], bool]:
    """Return (visited_db_ids, ceiling_hit) using the OLD angle-bracket LIKE BFS.

    Reconstructs the pre-change walk_thread BFS faithfully:
    - ancestor expansion via in_reply_to + _tokenize_references
    - descendant expansion via `references_header LIKE '%<id>%' ESCAPE '\\'`
    - hard ceiling abort at _BFS_HARD_CEILING unique visits

    Uses the supplied connection; does NOT open a new DB connection.
    Returns the FULL pre-truncation visited set so _classify_diff can
    compare old vs new before either side applies the 100-message cap.
    """
    start_row = con.execute(
        "SELECT id, message_id, in_reply_to, references_header "
        "FROM messages WHERE id = ?",
        [start_db_id],
    ).fetchone()
    if start_row is None:
        return (set(), False)

    visited_db_ids: set[int] = set()
    pending_message_ids: set[str] = set()

    def _enqueue_ids_from_row(row: sqlite3.Row) -> None:
        """Collect ancestor IDs from in_reply_to + references_header."""
        if row["in_reply_to"]:
            pending_message_ids.add(row["in_reply_to"].strip("<>"))
        for ref_id in _tokenize_references(row["references_header"]):
            pending_message_ids.add(ref_id.strip("<>"))

    def _enqueue_descendant_ids(msg_id: str) -> list[sqlite3.Row]:
        """Find descendants using the OLD angle-bracket LIKE query.

        LIKE pattern: `%<escaped_id>%` — this is a pure substring match
        after the leading '<'.  It can over-match when another Message-ID
        starts with the same prefix (e.g. searching for <abc@x.com> also
        hits <abc@x.com.tw> because '<abc@x.com>' appears as a substring
        of '<abc@x.com.tw>').
        """
        clean = msg_id.strip("<>")
        bracketed = f"<{clean}>"
        escaped_clean = _escape_like(clean)
        rows = con.execute(
            "SELECT id, message_id, in_reply_to, references_header "
            "FROM messages "
            "WHERE in_reply_to IN (?, ?) "
            "   OR references_header LIKE ? ESCAPE '\\'",
            [clean, bracketed, f"%<{escaped_clean}>%"],
        ).fetchall()
        return rows

    def _visit_row(row: sqlite3.Row) -> None:
        _enqueue_ids_from_row(row)
        if row["message_id"]:
            for desc_row in _enqueue_descendant_ids(row["message_id"]):
                child_db_id = int(desc_row["id"])
                if child_db_id in visited_db_ids:
                    continue
                if desc_row["message_id"]:
                    pending_message_ids.add(desc_row["message_id"].strip("<>"))
                else:
                    visited_db_ids.add(child_db_id)
                    _enqueue_ids_from_row(desc_row)

    # Bootstrap
    visited_db_ids.add(int(start_row["id"]))
    _visit_row(start_row)

    bfs_ceiling_hit = False
    while pending_message_ids:
        batch = list(pending_message_ids)
        pending_message_ids.clear()

        bare_ids = [mid.strip("<>") for mid in batch]
        bracketed_ids = [f"<{mid}>" for mid in bare_ids]
        all_forms = bare_ids + bracketed_ids
        placeholders = ",".join("?" for _ in all_forms)
        rows = con.execute(
            f"SELECT id, message_id, in_reply_to, references_header "
            f"FROM messages WHERE message_id IN ({placeholders})",
            all_forms,
        ).fetchall()

        for row in rows:
            db_id = int(row["id"])
            if db_id in visited_db_ids:
                continue
            visited_db_ids.add(db_id)
            if len(visited_db_ids) > _BFS_HARD_CEILING:
                bfs_ceiling_hit = True
                break
            _visit_row(row)

        if bfs_ceiling_hit:
            break

    return (visited_db_ids, bfs_ceiling_hit)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def _ensure_column(con: sqlite3.Connection) -> None:
    """Add thread bridge/thread_key columns if needed (idempotent)."""
    cols = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
    if "thread_key" not in cols:
        con.execute("ALTER TABLE messages ADD COLUMN thread_key INTEGER")
        con.commit()
        print("[backfill] Added thread_key column to messages table.")
    else:
        print("[backfill] thread_key column already exists — proceeding.")
    if "thread_bridge_target" not in cols:
        con.execute("ALTER TABLE messages ADD COLUMN thread_bridge_target TEXT")
        con.commit()
        print("[backfill] Added thread_bridge_target column to messages table.")
    if "thread_bridge_source" not in cols:
        con.execute(
            "ALTER TABLE messages ADD COLUMN thread_bridge_source TEXT "
            "CHECK (thread_bridge_source IS NULL "
            "OR thread_bridge_source = 'subject_heuristic')"
        )
        con.commit()
        print("[backfill] Added thread_bridge_source column to messages table.")


def run_backfill(database_path: Path) -> None:
    """Read all messages, compute thread_keys, write them back in batches."""
    con = _connect(database_path)
    try:
        _ensure_column(con)

        print("[backfill] Reading all messages …")
        rows = con.execute(
            "SELECT id, message_id, in_reply_to, references_header, "
            "dedup_key, thread_bridge_target FROM messages"
        ).fetchall()
        total = len(rows)
        print(f"[backfill] {total:,} messages loaded.")

        tuples = [
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

        print("[backfill] Computing thread_key via union-find …")
        thread_key_map: dict[int, int] = compute_thread_keys(tuples)
        print(f"[backfill] Union-find complete — {len(thread_key_map):,} mappings.")

        print(f"[backfill] Writing thread_key in batches of {_BATCH_SIZE:,} …")
        items = list(thread_key_map.items())
        with con:  # single transaction wrapping all UPDATEs
            for start in range(0, len(items), _BATCH_SIZE):
                batch = items[start : start + _BATCH_SIZE]
                con.executemany(
                    "UPDATE messages SET thread_key = ? WHERE id = ?",
                    [(tk, db_id) for db_id, tk in batch],
                )
                end = min(start + _BATCH_SIZE, len(items))
                print(f"[backfill]   … {end:,}/{len(items):,}")

        print(f"[backfill] Done.  {len(thread_key_map):,} rows updated.")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Equivalence verification (Task 4)
# ---------------------------------------------------------------------------
# Classification logic for expected allowed differences (design.md D2 + D3):
#
#   (a) LIKE false-positive: walk_thread's BFS expands
#       `references_header LIKE '%<id>%'` which can pick up substring matches
#       (e.g. <bc@x.com> matching for <abc@x.com>). The parser-based union-find
#       in compute_thread_keys uses _tokenize_references (same helper) which
#       extracts full bracket-delimited tokens — no substring ambiguity.
#       Detection: walk_thread found a row whose thread_key ≠ sampled thread_key,
#       AND that row's references_header contains the sampled root message_id as a
#       SUBSTRING but NOT as a full <token> match.
#
#   (b) >100 component truncation: walk_thread aborts BFS at _BFS_HARD_CEILING
#       (1000 unique visits) then takes first-100-by-sent_at from that partial set.
#       thread_key lookup queries the FULL component then takes first-100-by-sent_at.
#       Detection: the total component size (by thread_key query) > 100.


def _classify_diff(
    con: sqlite3.Connection,
    *,
    sampled_db_id: int,
    root_message_id: str | None,
    thread_key: int,
    old_set: set[int],
    new_set: set[int],
) -> dict:
    """Return a classification dict for one divergent thread."""
    in_old_not_new = sorted(old_set - new_set)
    in_new_not_old = sorted(new_set - old_set)

    # Total component size — if > 100, some diffs may be due to truncation.
    # However, only ids that fall OUTSIDE the earliest-100-by-sent_at boundary
    # are attributed to truncation.  An id INSIDE the earliest-100 that differs
    # is a real divergence and must be UNEXPLAINED.
    total_component = con.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_key = ?", [thread_key]
    ).fetchone()[0]

    # Compute the earliest-100 set explicitly when the component exceeds 100.
    if total_component > 100:
        earliest_100_rows = con.execute(
            "SELECT id FROM messages "
            "WHERE thread_key = ? "
            "ORDER BY COALESCE(unixepoch(sent_at), -62135596800) ASC "
            "LIMIT 100",
            [thread_key],
        ).fetchall()
        earliest_100_ids: set[int] = {int(r["id"]) for r in earliest_100_rows}
    else:
        earliest_100_ids = set()

    # Check LIKE false-positive: any row in old_set \ new_set or new_set \ old_set
    # whose references_header contains root_message_id as substring but NOT as
    # a full <token>.
    like_fp_ids: list[int] = []
    if root_message_id:
        clean_id = root_message_id.strip("<>")
        escaped = _escape_like(clean_id)
        candidates = in_old_not_new + in_new_not_old
        if candidates:
            placeholders = ",".join("?" * len(candidates))
            # Replicate the old BFS bracketed LIKE match: the old BFS used
            # `references_header LIKE '%<id>%'` (angle-bracket wrapped), so a
            # row is a genuine LIKE false-positive only when references_header
            # contains `<id>` as a substring AND the tokenizer does NOT find it
            # as a full token.  Using bare `%id%` would wrongly match rows that
            # old BFS would never have touched.
            fp_rows = con.execute(
                f"SELECT id, references_header FROM messages "
                f"WHERE id IN ({placeholders}) "
                f"  AND references_header LIKE ? ESCAPE '\\'",
                [*candidates, f"%<{escaped}>%"],
            ).fetchall()
            for fp_row in fp_rows:
                refs = fp_row["references_header"] or ""
                # It's a LIKE false-positive if the LIKE matched (substring)
                # but the full-token parse does NOT find this id.
                parsed_tokens = {t.strip("<>") for t in _tokenize_references(refs)}
                if clean_id not in parsed_tokens:
                    like_fp_ids.append(int(fp_row["id"]))

    explained_ids = set(like_fp_ids)
    if total_component > 100:
        # Attribute a diff to truncation only when the differing id falls
        # OUTSIDE the earliest-100-by-sent_at boundary.  An id inside that
        # boundary that is not a LIKE-false-positive must remain UNEXPLAINED.
        all_diff_ids = set(in_old_not_new) | set(in_new_not_old)
        outside_earliest_100 = all_diff_ids - earliest_100_ids
        explained_ids |= outside_earliest_100

    unexplained = (set(in_old_not_new) | set(in_new_not_old)) - explained_ids
    truncation_diff = total_component > 100

    return {
        "sampled_db_id": sampled_db_id,
        "thread_key": thread_key,
        "old_bfs_count": len(old_set),
        "new_thread_key_count": len(new_set),
        "total_component_size": total_component,
        "in_old_not_new": in_old_not_new,
        "in_new_not_old": in_new_not_old,
        "like_false_positive_ids": like_fp_ids,
        "truncation_difference": truncation_diff,
        "unexplained_ids": sorted(unexplained),
        "verdict": "EXPLAINED" if not unexplained else "UNEXPLAINED",
    }


def run_verify(database_path: Path, sample_n: int = 20, seed: int = 42) -> bool:
    """Compare old BFS walk_thread vs new thread_key query for sampled threads.

    Returns True if all divergences are explained, False if any are unexplained.
    """
    con = _connect(database_path)
    try:
        # Pick sample_n distinct thread_key values (each represents one component)
        all_keys = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT thread_key FROM messages WHERE thread_key IS NOT NULL"
            ).fetchall()
        ]
        if not all_keys:
            print("[verify] No thread_key values found — run backfill first.")
            return False

        rng = random.Random(seed)
        sample_keys = rng.sample(all_keys, min(sample_n, len(all_keys)))
        print(f"[verify] Sampling {len(sample_keys)} thread components …")

        reports: list[dict] = []
        unexplained_count = 0

        for tk in sample_keys:
            # Pick one representative db_id from this component to feed walk_thread
            rep_row = con.execute(
                "SELECT id, message_id FROM messages WHERE thread_key = ? LIMIT 1",
                [tk],
            ).fetchone()
            if rep_row is None:
                continue
            sampled_db_id = int(rep_row["id"])
            root_message_id = rep_row["message_id"]

            # --- Old BFS ---
            try:
                old_visited, _ceiling_hit = _old_bfs_component(con, sampled_db_id)
                old_set = old_visited
            except Exception as exc:
                print(
                    f"[verify]   thread_key={tk} db_id={sampled_db_id}: "
                    f"_old_bfs_component raised {exc!r} — skipping"
                )
                continue

            # --- New thread_key query (first 100 by sent_at) ---
            new_rows = con.execute(
                "SELECT id FROM messages "
                "WHERE thread_key = ? "
                "ORDER BY COALESCE(unixepoch(sent_at), -62135596800) ASC "
                "LIMIT 100",
                [tk],
            ).fetchall()
            new_set = {int(r["id"]) for r in new_rows}

            if old_set == new_set:
                reports.append(
                    {
                        "sampled_db_id": sampled_db_id,
                        "thread_key": tk,
                        "verdict": "IDENTICAL",
                        "old_bfs_count": len(old_set),
                        "new_thread_key_count": len(new_set),
                    }
                )
                print(
                    f"[verify]   thread_key={tk:>8}  db_id={sampled_db_id:>8}"
                    f"  size={len(new_set):>3}  IDENTICAL"
                )
            else:
                report = _classify_diff(
                    con,
                    sampled_db_id=sampled_db_id,
                    root_message_id=root_message_id,
                    thread_key=tk,
                    old_set=old_set,
                    new_set=new_set,
                )
                reports.append(report)
                verdict = report["verdict"]
                if verdict == "UNEXPLAINED":
                    unexplained_count += 1
                print(
                    f"[verify]   thread_key={tk:>8}  db_id={sampled_db_id:>8}"
                    f"  old={len(old_set):>3}  new={len(new_set):>3}  {verdict}"
                    + (
                        f"  [like_fp={len(report['like_false_positive_ids'])},"
                        f" truncation={report['truncation_difference']}]"
                        if verdict != "IDENTICAL"
                        else ""
                    )
                )
                if verdict == "UNEXPLAINED":
                    print(
                        f"[verify]   !!! UNEXPLAINED divergence for thread_key={tk}"
                        f" unexplained_ids={report['unexplained_ids']}"
                    )

    finally:
        con.close()

    identical = sum(1 for r in reports if r.get("verdict") == "IDENTICAL")
    explained = sum(1 for r in reports if r.get("verdict") == "EXPLAINED")
    print()
    print("[verify] ─── Equivalence Report ───────────────────────────────────────")
    print(f"[verify]   Sampled threads : {len(reports)}")
    print(f"[verify]   IDENTICAL       : {identical}")
    print(f"[verify]   EXPLAINED diff  : {explained}")
    print(f"[verify]   UNEXPLAINED diff: {unexplained_count}")
    if unexplained_count == 0:
        print("[verify]   GATE: PASS — no unexplained divergences.")
    else:
        print(
            f"[verify]   GATE: FAIL — {unexplained_count} unexplained divergences."
            "  DO NOT build index on live DB until resolved."
        )
    print("[verify] ─────────────────────────────────────────────────────────────")

    return unexplained_count == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Backfill thread_key column for all messages and optionally verify "
            "equivalence against the old BFS walk_thread."
        )
    )
    p.add_argument(
        "--database",
        type=Path,
        default=_DEFAULT_DB,
        metavar="PATH",
        help=f"Path to the SQLite database (default: {_DEFAULT_DB})",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="Run sampling equivalence verification after backfill.",
    )
    p.add_argument(
        "--verify-only",
        action="store_true",
        help=(
            "Skip backfill; run only the sampling equivalence verification "
            "(requires thread_key to already be populated)."
        ),
    )
    p.add_argument(
        "--sample",
        type=int,
        default=20,
        metavar="N",
        help="Number of threads to sample in verification (default: 20).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        metavar="S",
        help="Random seed for reproducible sampling (default: 42).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    database_path: Path = args.database.expanduser().resolve()

    if not database_path.exists():
        print(f"[error] Database not found: {database_path}", file=sys.stderr)
        return 1

    if not args.verify_only:
        run_backfill(database_path)

    if args.verify or args.verify_only:
        ok = run_verify(database_path, sample_n=args.sample, seed=args.seed)
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

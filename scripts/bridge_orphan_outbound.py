"""Infer low-confidence bridges for header-less outbound thread orphans."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic.identity import resolve_self_addresses  # noqa: E402
from mailattic.query import _tokenize_references  # noqa: E402
from mailattic.thread_key import compute_thread_keys  # noqa: E402

DEFAULT_TIME_WINDOW = timedelta(days=30)
BRIDGE_SOURCE = "subject_heuristic"


@dataclass(frozen=True)
class BridgeCandidate:
    orphan_id: int
    orphan_dedup_key: str
    target_anchor_id: int
    target_anchor: str
    normalized_subject: str
    external_participants: tuple[str, ...]
    time_delta_seconds: int
    candidate_count: int


def connect_database(database_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(database_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    con.execute("PRAGMA busy_timeout = 30000;")
    return con


def normalize_base_subject(subject: str | None) -> str:
    value = (subject or "").strip()
    value = re.sub(r"^\s*(\[[^\]]+\]\s*)+", "", value)
    changed = True
    while changed:
        before = value
        value = re.sub(r"^\s*(re|fw|fwd)\s*(\[\d+\])?\s*:\s*", "", value, flags=re.I)
        value = re.sub(r"^\s*\[fwd:\s*(.*?)\s*\]\s*$", r"\1", value, flags=re.I)
        value = re.sub(r"\s*\(fwd\)\s*$", "", value, flags=re.I)
        value = value.strip()
        changed = value != before
    return " ".join(value.split()).lower()


def _is_self(email: str | None, self_addresses: frozenset[str]) -> bool:
    return (email or "").strip().lower() in self_addresses


def _has_header_linkage(row: sqlite3.Row) -> bool:
    if (row["in_reply_to"] or "").strip():
        return True
    return bool(_tokenize_references(row["references_header"]))


def _parse_sent_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _anchor_identifier(row: sqlite3.Row) -> str:
    return str(row["message_id"] or row["dedup_key"])


def _message_participants(
    con: sqlite3.Connection,
    message_id: int,
    sender_email: str | None,
) -> set[str]:
    participants = {
        email.strip().lower() for email in [sender_email] if email and email.strip()
    }
    rows = con.execute(
        "SELECT email FROM message_recipients WHERE message_id = ?",
        [message_id],
    ).fetchall()
    for row in rows:
        email = row["email"]
        if email and str(email).strip():
            participants.add(str(email).strip().lower())
    return participants


def _external_participants(
    participants: set[str],
    self_addresses: frozenset[str],
) -> set[str]:
    return {email for email in participants if email not in self_addresses}


def select_orphan_outbound_candidates(
    con: sqlite3.Connection,
    *,
    self_addresses: frozenset[str] | None = None,
    include_existing_bridges: bool = False,
) -> list[sqlite3.Row]:
    if self_addresses is None:
        self_addresses = resolve_self_addresses(con)
    rows = con.execute(
        """
        SELECT
            id,
            subject,
            sender_email,
            sent_at,
            message_id,
            in_reply_to,
            references_header,
            dedup_key,
            thread_key,
            thread_bridge_target,
            thread_bridge_source
        FROM messages
        ORDER BY id
        """
    ).fetchall()
    candidates: list[sqlite3.Row] = []
    for row in rows:
        if not _is_self(row["sender_email"], self_addresses):
            continue
        if _has_header_linkage(row):
            continue
        has_existing_bridge = bool(row["thread_bridge_target"])
        is_isolated = row["thread_key"] is not None and int(row["thread_key"]) == int(
            row["id"]
        )
        if not is_isolated and not (include_existing_bridges and has_existing_bridge):
            continue
        candidates.append(row)
    return candidates


def infer_bridge_candidates(
    con: sqlite3.Connection,
    *,
    self_addresses: frozenset[str] | None = None,
    window: timedelta = DEFAULT_TIME_WINDOW,
    include_existing_bridges: bool = False,
) -> list[BridgeCandidate]:
    if self_addresses is None:
        self_addresses = resolve_self_addresses(con)
    rows = con.execute(
        """
        SELECT
            id,
            subject,
            sender_email,
            sent_at,
            message_id,
            in_reply_to,
            references_header,
            dedup_key,
            thread_key,
            thread_bridge_target,
            thread_bridge_source
        FROM messages
        ORDER BY id
        """
    ).fetchall()
    # Build header-only connected components (thread_bridge_target=None for all rows)
    # to avoid feedback-loop pollution on re-runs. Decision 5 requires grouping by
    # "current header-linked components", not by the bridge-widened stored thread_key.
    header_only_keys = compute_thread_keys(
        [
            (
                int(row["id"]),
                row["message_id"],
                row["in_reply_to"],
                row["references_header"],
                row["dedup_key"],
                None,  # suppress bridge edges → header edges only
            )
            for row in rows
        ]
    )
    rows_by_id = {int(row["id"]): row for row in rows}
    rows_by_header_key: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        header_key = header_only_keys.get(int(row["id"]))
        if header_key is None:
            continue
        rows_by_header_key.setdefault(header_key, []).append(row)

    results: list[BridgeCandidate] = []
    for candidate in select_orphan_outbound_candidates(
        con,
        self_addresses=self_addresses,
        include_existing_bridges=include_existing_bridges,
    ):
        candidate_subject = normalize_base_subject(candidate["subject"])
        if not candidate_subject:
            continue
        candidate_sent_at = _parse_sent_at(candidate["sent_at"])
        if candidate_sent_at is None:
            continue

        candidate_participants = _external_participants(
            _message_participants(
                con,
                int(candidate["id"]),
                candidate["sender_email"],
            ),
            self_addresses,
        )
        if not candidate_participants:
            continue

        candidate_header_key = header_only_keys.get(int(candidate["id"]))
        qualifying: list[tuple[sqlite3.Row, set[str], timedelta]] = []
        for header_key, component_rows in rows_by_header_key.items():
            if not include_existing_bridges and header_key == candidate_header_key:
                continue

            component_participants: set[str] = set()
            for row in component_rows:
                component_participants |= _message_participants(
                    con,
                    int(row["id"]),
                    row["sender_email"],
                )
            intersection = candidate_participants & _external_participants(
                component_participants,
                self_addresses,
            )
            if not intersection:
                continue

            inbound_anchors: list[tuple[sqlite3.Row, timedelta]] = []
            header_anchors: list[tuple[sqlite3.Row, timedelta]] = []
            for row in component_rows:
                if int(row["id"]) == int(candidate["id"]):
                    continue
                if normalize_base_subject(row["subject"]) != candidate_subject:
                    continue
                row_is_self = _is_self(row["sender_email"], self_addresses)
                if row_is_self and not _has_header_linkage(row):
                    continue
                sent_at = _parse_sent_at(row["sent_at"])
                if sent_at is None or sent_at > candidate_sent_at:
                    continue
                delta = candidate_sent_at - sent_at
                if delta <= window:
                    if row_is_self:
                        header_anchors.append((row, delta))
                    else:
                        inbound_anchors.append((row, delta))

            anchors = inbound_anchors or header_anchors
            if not anchors:
                continue
            anchor, delta = min(anchors, key=lambda pair: pair[1])
            qualifying.append((anchor, intersection, delta))

        if len(qualifying) != 1:
            continue

        anchor, intersection, delta = qualifying[0]
        target_id = int(anchor["id"])
        if target_id not in rows_by_id:
            continue
        results.append(
            BridgeCandidate(
                orphan_id=int(candidate["id"]),
                orphan_dedup_key=str(candidate["dedup_key"]),
                target_anchor_id=target_id,
                target_anchor=_anchor_identifier(anchor),
                normalized_subject=candidate_subject,
                external_participants=tuple(sorted(intersection)),
                time_delta_seconds=int(delta.total_seconds()),
                candidate_count=len(qualifying),
            )
        )
    return results


def _recompute_thread_keys(con: sqlite3.Connection) -> int:
    rows = con.execute(
        """
        SELECT
            id,
            message_id,
            in_reply_to,
            references_header,
            dedup_key,
            thread_bridge_target,
            thread_key
        FROM messages
        ORDER BY id
        """
    ).fetchall()
    inputs = [
        (
            int(row["id"]),
            row["message_id"],
            row["in_reply_to"],
            row["references_header"],
            row["dedup_key"],
            row["thread_bridge_target"],
        )
        for row in rows
    ]
    computed = compute_thread_keys(inputs)
    updates = [
        (thread_key, int(row["id"]))
        for row in rows
        if (thread_key := computed[int(row["id"])]) != row["thread_key"]
    ]
    if updates:
        con.executemany(
            "UPDATE messages SET thread_key = ? WHERE id = ?",
            updates,
        )
    return len(updates)


def format_report_line(candidate: BridgeCandidate) -> str:
    participants = ",".join(candidate.external_participants)
    return (
        f"orphan={candidate.orphan_dedup_key} "
        f"target={candidate.target_anchor} "
        f"subject={candidate.normalized_subject!r} "
        f"external={participants} "
        f"delta_seconds={candidate.time_delta_seconds} "
        f"candidate_count={candidate.candidate_count}"
    )


def run_bridge_backfill(database_path: Path, *, confirm: bool = False) -> int:
    con = connect_database(database_path)
    try:
        candidates = infer_bridge_candidates(
            con,
            include_existing_bridges=confirm,
        )
        for candidate in candidates:
            print(format_report_line(candidate))
        if not confirm:
            print(f"[dry-run] {len(candidates)} bridge candidate(s); no rows changed.")
            return 0
        changed_bridges = 0
        with con:
            for candidate in candidates:
                row = con.execute(
                    """
                    SELECT thread_bridge_target, thread_bridge_source
                    FROM messages
                    WHERE id = ?
                    """,
                    [candidate.orphan_id],
                ).fetchone()
                if row is None:
                    continue
                if (
                    row["thread_bridge_target"] == candidate.target_anchor
                    and row["thread_bridge_source"] == BRIDGE_SOURCE
                ):
                    continue
                con.execute(
                    """
                    UPDATE messages
                    SET thread_bridge_target = ?,
                        thread_bridge_source = ?
                    WHERE id = ?
                    """,
                    [candidate.target_anchor, BRIDGE_SOURCE, candidate.orphan_id],
                )
                changed_bridges += 1
            # Clear stale bridges: rows managed by this script in a prior run
            # that are no longer in the freshly-recomputed candidate set.
            # Decision 5: "recompute wins", including overwrite-to-NULL.
            bridged_orphan_ids = {c.orphan_id for c in candidates}
            stale_rows = con.execute(
                """
                SELECT id FROM messages
                WHERE thread_bridge_source = ?
                  AND thread_bridge_target IS NOT NULL
                """,
                [BRIDGE_SOURCE],
            ).fetchall()
            for stale_row in stale_rows:
                stale_id = int(stale_row["id"])
                if stale_id not in bridged_orphan_ids:
                    con.execute(
                        """
                        UPDATE messages
                        SET thread_bridge_target = NULL,
                            thread_bridge_source = NULL
                        WHERE id = ?
                        """,
                        [stale_id],
                    )
                    changed_bridges += 1
            changed_thread_keys = _recompute_thread_keys(con)
        print(
            f"[confirm] {changed_bridges} bridge row(s) changed; "
            f"{changed_thread_keys} thread_key row(s) changed."
        )
        return 0
    finally:
        con.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Infer header-less outbound thread bridges."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=REPO_ROOT / "mailattic.db",
        help="SQLite database path.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Write bridge columns and recompute thread_key. Omit for dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return run_bridge_backfill(
        args.database.expanduser().resolve(),
        confirm=args.confirm,
    )


if __name__ == "__main__":
    raise SystemExit(main())

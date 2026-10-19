"""Regression for #231: thread / contact-history order by absolute instant.

Stored ``sent_at`` keeps its original offset, so comparing the strings orders
cross-timezone messages wrongly. Fixture: id1 = 08:00+08:00 (UTC 00:00, older),
id2 = 01:00+00:00 (UTC 01:00, newer) -- string order is the reverse.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mailattic.contact_history import aggregate_contact_history
from mailattic.models import PstSource, build_normalized_message
from mailattic.query import resolve_thread_message_ids, walk_thread
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

_SOURCE = PstSource(
    path=Path("C:/mail/archive.pst"),
    input_mode="file",
    requested_from=Path("C:/mail/archive.pst"),
)
_OLDER = "2026-09-01T08:00:00+08:00"
_NEWER = "2026-09-01T01:00:00+00:00"


def _build(temp_dir: str, rows: list[tuple[str, str | None]]) -> Path:
    """rows: (label, sent_at). Each replies to the first, with its own attachment."""
    db = initialize_database(Path(temp_dir) / "t.db")
    conn = connect_database(db)
    for i, (label, stamp) in enumerate(rows):
        raw = {
            "subject": label,
            "sender": {"email": "buyer@client.example"},
            "to": [{"email": "buyer@client.example"}],
            "sent_at": stamp,
            "body_text": "x " + label,
            "message_id": f"<{label}@repro.example>",
            "in_reply_to": f"<{rows[0][0]}@repro.example>" if i else None,
            "attachments": [{"attachment_name": label + ".pdf"}],
        }
        persist_message(
            conn,
            build_normalized_message(
                raw, source=_SOURCE, folder_path="Inbox", logical_locator=label
            ),
        )
    conn.commit()
    conn.close()
    return db


class EpochOrderingTests(unittest.TestCase):
    def test_thread_orders_cross_offset_by_instant(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = _build(d, [("older", _OLDER), ("newer", _NEWER)])
            self.assertEqual(resolve_thread_message_ids(db, 1, read_only=True), [1, 2])
            self.assertEqual(
                [m["id"] for m in walk_thread(db, 1, read_only=True)], [1, 2]
            )

    def test_thread_null_first_and_same_instant_tiebreak_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = _build(
                d,
                [
                    ("a", "2026-09-01T09:00:00+08:00"),
                    ("b", None),
                    ("c", "2026-09-01T01:00:00+00:00"),
                ],
            )
            # b NULL first; a and c are the same instant -> id ascending.
            expected = [2, 1, 3]
            self.assertEqual(
                resolve_thread_message_ids(db, 1, read_only=True), expected
            )
            self.assertEqual(
                [m["id"] for m in walk_thread(db, 1, read_only=True)], expected
            )

    def test_contact_history_first_last_seen_and_recent_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = _build(d, [("older", _OLDER), ("newer", _NEWER)])
            s = aggregate_contact_history(
                database_path=db, domain="client.example", read_only=True
            )
            self.assertEqual(s["first_message_at"], _OLDER)
            self.assertEqual(s["last_message_at"], _NEWER)
            self.assertEqual(s["contacts"][0]["last_seen"], _NEWER)
            self.assertEqual(s["attachments"]["recent"], ["newer.pdf", "older.pdf"])

    def test_contact_history_ignores_null_dates_for_first_last(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = _build(d, [("n", None), ("older", _OLDER), ("newer", _NEWER)])
            s = aggregate_contact_history(
                database_path=db, domain="client.example", read_only=True
            )
            self.assertEqual(s["first_message_at"], _OLDER)
            self.assertEqual(s["last_message_at"], _NEWER)

    def test_thread_cap_keeps_earliest_100_by_instant(self) -> None:
        # 101 messages. "late" has the lexically second-smallest string but the
        # latest instant (-12:00), so the earliest-100 cap must drop it.
        rows: list[tuple[str, str | None]] = [("root", "2026-09-01T00:00:00+08:00")]
        rows.append(("late", "2026-09-01T23:59:00-12:00"))
        rows += [(f"m{i:02d}", f"2026-09-02T10:{i:02d}:00+08:00") for i in range(60)]
        rows += [(f"n{i:02d}", f"2026-09-02T11:{i:02d}:00+08:00") for i in range(39)]
        with tempfile.TemporaryDirectory() as d:
            db = _build(d, rows)
            ids = [m["id"] for m in walk_thread(db, 1, read_only=True)]
            self.assertEqual(len(ids), 100)
            self.assertNotIn(2, ids)  # "late"
            self.assertEqual(ids, [1, *range(3, 102)])


if __name__ == "__main__":
    unittest.main()

"""backfill_raw_headers.py — One-shot backfill of raw_headers for historical PST
messages.

Re-parses a PST file using the same readpst-based pipeline used at ingest time.
For each parsed message the script reconstructs the dedup identity exactly as
ingestion does (has message_id → exact match; no message_id → fallback sha256
fingerprint) and updates ONLY the ``raw_headers`` column when:

  - Exactly one DB row matches the dedup identity, AND
  - That row currently has ``raw_headers IS NULL``.

Rows with 0 or >1 matches are skipped and counted.  Rows with a non-NULL
``raw_headers`` are also skipped (idempotent — safe to re-run).

Usage:
    uv run python scripts/backfill_raw_headers.py --pst <path> [--pst <path> ...]
        [--database <path>] [--readpst-path <path>] [--work-root <dir>]

Exit codes:
    0 — run completed (check printed summary for counts)
    1 — fatal error (DB not found, PST parse error, etc.)
"""

from __future__ import annotations

import argparse
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

from mailattic.models import (  # noqa: E402
    PstSource,
    _coerce_optional_raw_headers,
    build_normalized_message,
)
from mailattic.parser import ReadpstParser  # noqa: E402
from mailattic.storage import connect_database  # noqa: E402

# ---------------------------------------------------------------------------
# Default DB path (mirrors CLI convention — absolute, cwd-invariant)
# ---------------------------------------------------------------------------
_DEFAULT_DB = REPO_ROOT / "mailattic.db"


def _connect(database_path: Path) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with busy_timeout."""
    con = connect_database(database_path, create=False)
    con.execute("PRAGMA busy_timeout = 30000;")  # 30 s, tolerate concurrent sync
    return con


def _lookup_row_id(
    con: sqlite3.Connection,
    message_id: str | None,
    dedup_key: str,
) -> list[int]:
    """Return list of DB row ids matching the dedup identity.

    Mirrors the storage.py dedup gate logic exactly:
    - When message_id is not None: match by message_id column OR dedup_key column.
    - When message_id is None: match by dedup_key column only.

    Returns all matching row ids (0, 1, or >1).
    """
    if message_id is not None:
        rows = con.execute(
            """
            SELECT id
            FROM messages
            WHERE message_id = ?
               OR dedup_key = ?
            """,
            [message_id, dedup_key],
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT id
            FROM messages
            WHERE dedup_key = ?
            """,
            [dedup_key],
        ).fetchall()
    return [int(r["id"]) for r in rows]


def run_backfill(
    database_path: Path,
    pst_paths: list[Path],
    *,
    readpst_path: Path | None = None,
    work_root: Path | None = None,
) -> tuple[int, int, int]:
    """Backfill raw_headers for messages from the given PST files.

    Returns (updated, unmatched, skipped) counts where:
    - updated: rows that were NULL and are now filled
    - unmatched: PST messages with 0 DB matches (message not in DB)
    - skipped: PST messages skipped due to already-populated raw_headers,
               ambiguous multi-match (>1 DB rows), or no header data from parser
    """
    con = _connect(database_path)

    updated = 0
    unmatched = 0
    skipped = 0
    processed = 0

    try:
        parser = ReadpstParser(
            readpst_path=readpst_path,
            work_root=work_root,
        )

        for pst_path in pst_paths:
            print(f"[backfill] Processing PST: {pst_path}")
            source = PstSource(
                path=pst_path,
                input_mode="file",
                requested_from=pst_path,
            )

            for result in parser.iter_extraction_results(source):
                if result.failure is not None:
                    # Parse failure — cannot extract raw_headers anyway; count as
                    # unmatched since we have no identity to look up.
                    unmatched += 1
                    if unmatched <= 20:
                        print(
                            f"[backfill]   parse failure: "
                            f"{result.failure.logical_locator} — "
                            f"{result.failure.error_type}: "
                            f"{result.failure.error_message}"
                        )
                    continue

                assert result.message is not None  # guaranteed when failure is None

                raw_message = result.message.raw_message

                # Extract raw_headers NOW before building NormalizedMessage so we
                # have it even when message_id is absent (raw_headers is in the dict
                # from the parser already — _parse_email_message puts it there).
                raw_headers_value = _coerce_optional_raw_headers(
                    raw_message.get("raw_headers")
                )

                # Build NormalizedMessage to get the dedup identity.
                # This replicates exactly what ingest.py does:
                # build_normalized_message → NormalizedMessage.dedup_key
                try:
                    normalized = build_normalized_message(
                        raw_message,
                        source=source,
                        folder_path=result.message.folder_path,
                        logical_locator=result.message.logical_locator,
                    )
                except Exception as exc:
                    unmatched += 1
                    if unmatched <= 20:
                        print(
                            f"[backfill]   normalize failure: "
                            f"{result.message.logical_locator} — {exc!r}"
                        )
                    continue

                dedup_key = normalized.dedup_key
                message_id = normalized.message_id

                # Find matching DB row(s)
                row_ids = _lookup_row_id(con, message_id, dedup_key)

                if len(row_ids) == 0:
                    # 0 matches: message not in DB (maybe ingest failure or not yet
                    # imported) — counted as unmatched per spec.
                    unmatched += 1
                    if unmatched <= 20:
                        print(
                            "[backfill]   skip"
                            f" (match=0): "
                            f"{result.message.logical_locator}"
                        )
                    processed += 1
                    continue

                if len(row_ids) > 1:
                    # >1 matches: ambiguous (dedup collision) — counted as skipped
                    # per spec ("already-populated or ambiguous (multi-match) target").
                    skipped += 1
                    if skipped <= 20:
                        print(
                            "[backfill]   skip"
                            f" (match={len(row_ids)}, ambiguous): "
                            f"{result.message.logical_locator}"
                        )
                    processed += 1
                    continue

                row_id = row_ids[0]

                # Check whether raw_headers is already set
                existing = con.execute(
                    "SELECT raw_headers FROM messages WHERE id = ?",
                    [row_id],
                ).fetchone()

                if existing is None:
                    # Row disappeared between lookup and check — treat as unmatched
                    unmatched += 1
                    processed += 1
                    continue

                if existing["raw_headers"] is not None:
                    # Already populated — idempotent skip
                    skipped += 1
                    processed += 1
                    continue

                # Write raw_headers (may be None if parser could not extract it;
                # in that case we leave it NULL — do not write NULL over NULL,
                # skip to preserve idempotency for future re-runs once PST is
                # re-processed with a fixed parser).
                if raw_headers_value is None:
                    # Parser gave us no header data for this message; skip.
                    skipped += 1
                    processed += 1
                    continue

                con.execute(
                    "UPDATE messages SET raw_headers = ? WHERE id = ?",
                    [raw_headers_value, row_id],
                )
                con.commit()
                updated += 1
                processed += 1

                if processed % 1000 == 0:
                    print(
                        f"[backfill]   processed {processed:,} "
                        f"(updated={updated}, unmatched={unmatched}, skipped={skipped})"
                    )

        print("[backfill] Done.")
        print(f"[backfill]   processed : {processed:,}")
        print(f"[backfill]   updated   : {updated:,}")
        print(f"[backfill]   unmatched : {unmatched:,}")
        print(
            f"[backfill]   skipped   : {skipped:,}  (already filled or no header data)"
        )

    finally:
        con.close()

    return updated, unmatched, skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Backfill the raw_headers column for historical PST messages. "
            "Re-parses PST file(s) and updates only rows where raw_headers IS NULL "
            "and exactly one DB row matches the dedup identity."
        )
    )
    p.add_argument(
        "--pst",
        action="append",
        required=True,
        type=Path,
        metavar="PATH",
        help="Path to a PST file to process. Repeat for multiple PST files.",
    )
    p.add_argument(
        "--database",
        type=Path,
        default=_DEFAULT_DB,
        metavar="PATH",
        help=f"Path to the SQLite database (default: {_DEFAULT_DB})",
    )
    p.add_argument(
        "--readpst-path",
        type=Path,
        default=None,
        dest="readpst_path",
        metavar="PATH",
        help="Path to readpst executable (optional; defaults to auto-detection).",
    )
    p.add_argument(
        "--work-root",
        type=Path,
        default=None,
        dest="work_root",
        metavar="DIR",
        help="Writable directory for temporary readpst extraction files.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    database_path: Path = args.database.expanduser().resolve()

    if not database_path.exists():
        print(f"[error] Database not found: {database_path}", file=sys.stderr)
        return 1

    pst_paths: list[Path] = [p.expanduser().resolve() for p in args.pst]
    for pst_path in pst_paths:
        if not pst_path.exists():
            print(f"[error] PST file not found: {pst_path}", file=sys.stderr)
            return 1

    try:
        run_backfill(
            database_path,
            pst_paths,
            readpst_path=args.readpst_path,
            work_root=args.work_root,
        )
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""verify-rebuild.py — Pre-swap integrity verification for mailattic.

Run five checks against a rebuilt staging SQLite DB.  All five must pass
before the operator is allowed to swap the stage DB into production.  The
source-count check needs an operator-approved baseline file; without one
(or with an invalid one) the verifier cannot PASS.  PASS means the baseline
counts and the mechanical checks hold -- not per-message / byte preservation,
and not the manual recipient sampling.

Usage:
    uv run python scripts/verify-rebuild.py --db <path> --baseline <path>
        [--attachment-root <path>]

Exit codes:
    0 — all five checks passed (overall == PASS)
    1 — one or more checks failed  (overall == FAIL)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def check_integrity(conn: sqlite3.Connection) -> dict:
    """PRAGMA integrity_check — must return 'ok'."""
    row = conn.execute("PRAGMA integrity_check").fetchone()
    result = row[0] if row else ""
    passed = result == "ok"
    return {"result": result, "passed": passed}


_BASELINE_VERSION = 1
_ENTRY_KEYS = {
    "name",
    "min_count",
    "optional",
    "approved_gap",
    "gap_reason",
    "pst_source_paths",
}


def _norm_path(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _source_family(pst_source: str) -> str:
    """'pst' for real PST sources; otherwise the non-PST family name."""
    low = pst_source.casefold()
    if low.startswith("archive:"):
        return "archive"
    if low.startswith("imap:"):
        return "imap"
    if low == "outbound-bes":
        return "outbound-bes"
    return "pst"


def load_baseline(path: Path) -> list[dict]:
    """Parse and validate the operator-approved baseline file (read-only).

    Raises ValueError with a locatable reason on any problem.  Format:
    {"version": 1, "sources": [{"name": "x.pst", "min_count": 10,
     "optional": false, "approved_gap": 0, "gap_reason": "...",
     "pst_source_paths": ["D:/old/x.pst"]}]}
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ValueError(f"baseline unreadable: {path}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"baseline is not valid JSON: {path}: {exc}") from exc
    version = raw.get("version") if isinstance(raw, dict) else None
    # type() not ==: True and 1.0 both compare equal to 1.
    if type(version) is not int or version != _BASELINE_VERSION:
        raise ValueError(
            f"baseline must be an object with version == {_BASELINE_VERSION}"
        )
    extra = set(raw) - {"version", "sources", "note"}
    if extra:
        raise ValueError(f"baseline has unknown top-level keys: {sorted(extra)}")
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("baseline.sources must be a non-empty list")

    entries: list[dict] = []
    seen: set[str] = set()
    for i, e in enumerate(sources):
        where = f"baseline.sources[{i}]"
        if not isinstance(e, dict):
            raise ValueError(f"{where} must be an object")
        if set(e) - _ENTRY_KEYS:
            raise ValueError(
                f"{where} has unknown keys: {sorted(set(e) - _ENTRY_KEYS)}"
            )
        name = e.get("name")
        if (
            not isinstance(name, str)
            or not name.casefold().endswith(".pst")
            or "/" in name
            or "\\" in name
        ):
            raise ValueError(f"{where}.name must be a bare '*.pst' file name")
        if name.casefold() in seen:
            raise ValueError(f"{where}.name duplicates an earlier entry: {name}")
        seen.add(name.casefold())
        min_count = e.get("min_count")
        if (
            not isinstance(min_count, int)
            or isinstance(min_count, bool)
            or min_count < 1
        ):
            raise ValueError(f"{where}.min_count must be an integer >= 1")
        gap = e.get("approved_gap", 0)
        if (
            not isinstance(gap, int)
            or isinstance(gap, bool)
            or not 0 <= gap < min_count
        ):
            raise ValueError(
                f"{where}.approved_gap must be an integer in [0, min_count)"
            )
        reason = e.get("gap_reason", "")
        if gap > 0 and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError(f"{where}.gap_reason is required when approved_gap > 0")
        optional = e.get("optional", False)
        if not isinstance(optional, bool):
            raise ValueError(f"{where}.optional must be a boolean")
        paths = e.get("pst_source_paths")
        if paths is not None and (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(p, str) and p.strip() for p in paths)
        ):
            raise ValueError(
                f"{where}.pst_source_paths must be a non-empty list of strings"
            )
        if paths and any(_source_family(p) != "pst" for p in paths):
            raise ValueError(f"{where}.pst_source_paths contains a non-PST source")
        entries.append(
            {
                "name": name,
                "min_count": min_count,
                "optional": optional,
                "approved_gap": gap,
                "gap_reason": reason if gap else "",
                "threshold": min_count - gap,
                "pst_source_paths": paths,
            }
        )
    if all(e["optional"] for e in entries):
        raise ValueError("baseline needs at least one non-optional source")
    return entries


def check_per_source_counts(
    conn: sqlite3.Connection, baseline_path: Path | None = None
) -> dict:
    """Compare persisted per-PST-source counts with an operator-approved baseline.

    No baseline, or an invalid one, is a FAIL with ``refusal_reason`` -- never
    a PASS.  Each baseline entry is checked on its own: other sources (unlisted
    PSTs, imap:, archive:, outbound-bes) can never fill a gap.  Entries match a
    DB ``pst_source`` by file name (separators/case-insensitive) or, when
    ``pst_source_paths`` is given, by those exact paths.  Two distinct DB
    sources sharing one file name are refused as ambiguous, never merged.
    """
    result: dict = {
        "baseline_path": str(baseline_path) if baseline_path else None,
        "passed": False,
        "refusal_reason": None,
        "sources": [],
        "unlisted_pst_sources": {},
        "excluded_non_pst": {},
    }
    if baseline_path is None:
        result["refusal_reason"] = (
            "no baseline supplied (--baseline); source counts cannot be verified"
        )
        return result
    try:
        entries = load_baseline(baseline_path)
    except ValueError as exc:
        result["refusal_reason"] = str(exc)
        return result

    db_counts: dict[str, int] = {}
    for src, cnt in conn.execute(
        "SELECT pst_source, COUNT(*) FROM messages WHERE pst_source IS NOT NULL "
        "GROUP BY pst_source"
    ):
        family = _source_family(src)
        if family == "pst":
            db_counts[src] = cnt
        else:
            result["excluded_non_pst"][family] = (
                result["excluded_non_pst"].get(family, 0) + cnt
            )

    claimed: dict[str, str] = {}
    refusal: str | None = None
    for e in entries:
        if e["pst_source_paths"]:
            wanted = {_norm_path(p) for p in e["pst_source_paths"]}
            matched = [s for s in db_counts if _norm_path(s) in wanted]
        else:
            matched = [
                s
                for s in db_counts
                if _norm_path(s).rsplit("/", 1)[-1] == e["name"].casefold()
            ]
        actual = sum(db_counts[s] for s in matched)
        if len(matched) > 1 and not e["pst_source_paths"]:
            status = "ambiguous"
            refusal = refusal or (
                f"{e['name']}: {len(matched)} different DB sources share this file "
                f"name {sorted(matched)}; list the intended ones in pst_source_paths"
            )
        elif not matched and e["optional"]:
            status = "absent_approved"
        elif not matched:
            status = "missing"
        elif actual < e["threshold"]:
            status = "short"
        else:
            status = "ok"
        for s in matched:
            if s in claimed:
                refusal = refusal or (
                    f"DB source {s} is claimed by both {claimed[s]} and {e['name']}"
                )
            claimed[s] = e["name"]
        result["sources"].append(
            {
                "name": e["name"],
                "optional": e["optional"],
                "expected_min_count": e["min_count"],
                "approved_gap": e["approved_gap"],
                "gap_reason": e["gap_reason"],
                "threshold": e["threshold"],
                "matched_pst_sources": sorted(matched),
                "actual": actual,
                "status": status,
            }
        )
    result["unlisted_pst_sources"] = {
        s: c for s, c in db_counts.items() if s not in claimed
    }
    result["refusal_reason"] = refusal
    result["passed"] = refusal is None and all(
        s["status"] in ("ok", "absent_approved") for s in result["sources"]
    )
    return result


def check_null_hash_by_mode(conn: sqlite3.Connection) -> dict:
    """Count NULL content_hash grouped by pst_source family.

    Family classification via pst_source prefix:
    - 'archive:%' prefix  → 'archive' family (placeholder pattern allowed)
    - 'imap:%' prefix     → 'imap' family    (must be 0)
    - anything else       → 'pst' family     (TNEF-pattern NULLs allowed)

    PASS iff:
    - imap    family null count == 0
    - pst     family null count is composed entirely of completely-undecodable
      attachments (mime_type IS NULL AND size_bytes IS NULL AND storage_path IS NULL).
      These represent attachments the readpst pipeline could not extract metadata
      for at all — known categories include Outlook TNEF-encapsulated 'details.txt'
      shells and Google DMARC aggregate-report .zip submissions. A partial-success
      NULL (where mime/size populated but content_hash missing) signals a real
      regression and fails PASS.
    - archive family is allowed any value (placeholder sentinel pattern)
    """
    rows = conn.execute(
        """
        SELECT
          CASE
            WHEN m.pst_source LIKE 'archive:%' THEN 'archive'
            WHEN m.pst_source LIKE 'imap:%' THEN 'imap'
            ELSE 'pst'
          END AS source_family,
          COUNT(*) AS null_cnt
        FROM messages m
        JOIN message_attachments a ON a.message_id = m.id
        WHERE a.content_hash IS NULL
        GROUP BY source_family
        """
    ).fetchall()
    counts: dict[str, int] = {r[0]: r[1] for r in rows}

    # imap must be strict zero (server retention loss is operator-accepted but
    # any remaining IMAP-family null after rebuild signals an attachment_root
    # wiring regression).
    imap_pass = counts.get("imap", 0) == 0

    # pst family: count must equal completely-undecodable subset (all metadata
    # fields NULL — readpst couldn't extract anything). Partial-NULL (e.g. mime
    # present but hash missing) would indicate a real regression.
    pst_null_total = counts.get("pst", 0)
    pst_complete_decode_failures = conn.execute(
        """
        SELECT COUNT(*) FROM messages m
        JOIN message_attachments a ON a.message_id = m.id
        WHERE a.content_hash IS NULL
          AND m.pst_source NOT LIKE 'archive:%'
          AND m.pst_source NOT LIKE 'imap:%'
          AND a.mime_type IS NULL
          AND a.size_bytes IS NULL
          AND a.storage_path IS NULL
        """
    ).fetchone()[0]
    pst_pass = pst_null_total == pst_complete_decode_failures

    passed = imap_pass and pst_pass

    return {
        "counts": counts,
        "pst_complete_decode_failures": pst_complete_decode_failures,
        "pst_partial_null_count": pst_null_total - pst_complete_decode_failures,
        "passed": passed,
    }


def check_cas_spot_check(
    conn: sqlite3.Connection,
    attachment_root: Path,
) -> dict:
    """Random spot-check: up to 3 CAS files must exist, sha256 and size must match.

    Checks whatever exists (1 or 2 rows are checked); with no non-NULL rows,
    PASS with skipped_reason.
    """
    rows = conn.execute(
        """
        SELECT storage_path, content_hash, size_bytes
        FROM message_attachments
        WHERE content_hash IS NOT NULL
        ORDER BY RANDOM()
        LIMIT 3
        """
    ).fetchall()

    if not rows:
        return {"samples": [], "passed": True, "skipped_reason": "no-attachments"}

    samples: list[dict] = []
    all_passed = True

    for storage_path, expected_hash, expected_size in rows:
        # Resolve: storage_path starts with "attachments/"; strip prefix and
        # join with attachment_root so the actual file is at
        # <attachment_root>/<bucket_l1>/<bucket_l2>/<hash>.<ext>
        prefix = "attachments/"
        if storage_path.startswith(prefix):
            rel = storage_path[len(prefix) :]
        else:
            rel = storage_path
        file_path = attachment_root / rel

        sample: dict = {
            "storage_path": storage_path,
            "expected_hash": expected_hash,
            "actual_hash": None,
            "expected_size": expected_size,
            "actual_size": None,
            "size_match": False,
            "hash_match": False,
            "passed": False,
        }

        if file_path.exists():
            data = file_path.read_bytes()
            actual_hash = hashlib.sha256(data).hexdigest()
            actual_size = file_path.stat().st_size
            hash_match = actual_hash == expected_hash
            size_match = (expected_size is None) or (actual_size == expected_size)
            row_passed = hash_match and size_match
            sample.update(
                {
                    "actual_hash": actual_hash,
                    "actual_size": actual_size,
                    "size_match": size_match,
                    "hash_match": hash_match,
                    "passed": row_passed,
                }
            )
            if not row_passed:
                all_passed = False
        else:
            sample["error"] = f"file not found: {file_path}"
            all_passed = False

        samples.append(sample)

    return {"samples": samples, "passed": all_passed}


def check_fts_rowid_alignment(conn: sqlite3.Connection) -> dict:
    """Verify every messages.id has a matching message_fts rowid.

    PASS iff orphan count == 0.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) FROM messages m
        WHERE NOT EXISTS (
            SELECT 1 FROM message_fts f WHERE f.rowid = m.id
        )
        """
    ).fetchone()
    orphans = row[0] if row else 0
    passed = orphans == 0
    return {"orphans": orphans, "passed": passed}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def verify(
    db_path: Path, attachment_root: Path, baseline_path: Path | None = None
) -> tuple[dict, bool]:
    """Run all five checks and return (result_dict, overall_passed).

    The DB is opened read-only; without a valid baseline the result is FAIL.
    """
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row

        integrity = check_integrity(conn)
        per_source = check_per_source_counts(conn, baseline_path)
        null_hash = check_null_hash_by_mode(conn)
        cas = check_cas_spot_check(conn, attachment_root)
        fts = check_fts_rowid_alignment(conn)
    finally:
        conn.close()

    all_passed = all(
        [
            integrity["passed"],
            per_source["passed"],
            null_hash["passed"],
            cas["passed"],
            fts["passed"],
        ]
    )
    overall = "PASS" if all_passed else "FAIL"

    result: dict = {
        "integrity_check": integrity,
        "per_source_counts": per_source,
        "null_hash_by_family": null_hash,
        "cas_spot_check": cas,
        "fts_rowid_alignment": fts,
        "overall": overall,
    }
    return result, all_passed


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Parse args, run verify, print JSON, return exit code."""
    parser = argparse.ArgumentParser(
        description=(
            "Verify a rebuilt mailattic SQLite DB before swapping "
            "it into production.  Prints a JSON report to stdout.  "
            "Exits 0 on PASS, 1 on FAIL."
        )
    )
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="Path to the stage SQLite DB to verify.",
    )
    parser.add_argument(
        "--attachment-root",
        type=Path,
        default=None,
        help=(
            "Root directory for CAS attachment files.  "
            "Defaults to <cwd>/attachments/ if not supplied.  "
            "Overrides MAILATTIC_ATTACHMENT_ROOT env var."
        ),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help=(
            "Operator-approved source-count baseline (JSON, see "
            "docs/rebuild-db-from-pst.md).  Without it the verifier cannot PASS."
        ),
    )
    args = parser.parse_args()

    db_path: Path = args.db.expanduser().resolve()
    if not db_path.exists():
        print(
            json.dumps(
                {"error": f"DB not found: {db_path}", "overall": "FAIL"}, indent=2
            )
        )
        return 1

    # Resolve attachment root: CLI > env var > cwd/attachments/
    # (mirrors resolve_store_root logic from attachment_store.py)
    if args.attachment_root is not None:
        attachment_root = args.attachment_root.expanduser().resolve()
    else:
        env_val = os.environ.get("MAILATTIC_ATTACHMENT_ROOT", "").strip()
        if env_val:
            attachment_root = Path(env_val).expanduser().resolve()
        else:
            attachment_root = Path.cwd() / "attachments"

    baseline_path = args.baseline.expanduser().resolve() if args.baseline else None
    result, passed = verify(db_path, attachment_root, baseline_path)
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

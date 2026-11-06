"""body_rederive_apply.py -- apply a re-derived-body stage to the canonical
database (issue #14 phase 2, second half; the stage is built by
``scripts/body_rederive_stage.py``).

This is a bulk rewrite of ``messages.body_text`` and its paired
``message_fts.body_text`` for tens of thousands of rows of the production mail
database. It is run only by the human owner, so every step is guarded.

What changes: exactly two columns, as a pair, per planned message --
``messages.body_text`` (the stage value passed through
``models._normalize_body_text``, which is what ingest stores) and
``message_fts.body_text`` (``segment_cjk`` of that stored value, which is what
ingest indexes). ``message_fts`` is a plain FTS5 table keyed by
``rowid == messages.id`` with no trigger, so both are written explicitly.

Plan: every stage row with ``status='rewrite'`` whose canonical row exists,
has a ``message_fts`` row at the same rowid, belongs to the ``source_input_mode``
its ``source_kind`` implies (``pst`` -> ``file``, ``imap`` -> ``maildir``), and
whose current normalized ``message_id`` equals the stage's
``expected_message_id`` (the pairing the stage verified; a rebuilt canonical DB
may have reassigned ids), and whose current ``sha256(body_text)`` equals the
stage's ``pre_image_sha256``. Rows failing a check are excluded and counted
(``missing_row``, ``missing_fts``, ``mode_mismatch``, ``message_id_changed``,
``drifted``, ``not_rewrite``) and are never written. A stage built before the
``expected_message_id`` column existed is refused: rebuild it.

Completeness verdict: the stage builder persists ``verdict`` in the stage's
report (``stage_meta.report_json``). Dry-run and ``--confirm`` both refuse a
stage whose report lacks it or whose ``verdict.complete`` is not exactly
``True`` (missing / malformed / non-bool fail closed; the message lists the
reasons) -- rebuild the stage. There is no override. ``--restore`` reads a
sidecar, not a stage, and is unaffected.

Usage:
    uv run python scripts/body_rederive_apply.py --stage <stage.db>
        [--canonical <path>]                       # dry-run (default)
    uv run python scripts/body_rederive_apply.py --stage <stage.db>
        --confirm [--canonical <path>]             # writes the canonical DB
    uv run python scripts/body_rederive_apply.py --restore <sidecar.db>
        [--canonical <path>] [--confirm]           # put pre-images back

Dry-run opens the canonical database read-only and writes only
``<stage stem>-apply-report-dryrun.json`` beside the stage. The report carries
``plan_digest`` (SHA-256 over every planned (id, pre-image sha, post-image sha)),
the resolved canonical path, ``read_only``, counts, the stage file hash and a
sample of ids with before/after lengths. No body text appears in the report or
on stdout.

``--confirm`` refuses unless that dry-run report matches the plan it just
recomputed (plan_digest, canonical path, stage path, ``read_only`` exactly
True). It then writes a pre-image sidecar ``<stage stem>-apply-preimage.db``
beside the stage (refused if that path already exists; committed and verified
before the canonical database is opened for writing), and applies everything in
one ``BEGIN IMMEDIATE`` transaction: each row's pre-image sha (and old FTS
value) is re-checked inside the transaction, so any drift rolls the whole run
back; before commit every planned row is re-read and both columns compared to
the expected values, table row counts must be unchanged, and a digest of all
NON-plan rows' ``body_text`` (both tables) must be identical to its value at the
start of the transaction. Any mismatch or exception rolls back.

``--restore`` plans from the sidecar: rows whose current body sha equals the
sidecar's post sha are restored (old body and old FTS value written back
verbatim); others are counted ``drifted_since_apply`` and skipped. Dry-run
unless ``--confirm``; the sidecar must have been written for this canonical
path. Same transaction and reconciliation discipline.

Threat model (as recipient_rebuild_apply.py): the gates defend against a wrong
path, a stale or edited stage, and drift between review and run -- not against
an operator deliberately constructing colliding inputs.

Exit codes:
    0 -- dry-run report written (canonical untouched), or a confirmed apply /
         restore completed and reconciled (a confirm / restore report that
         cannot be written only warns: the canonical write is already done and
         the sidecar is the record, so exit 1 would falsely claim a rollback)
    1 -- refused (preflight incl. a stage without a complete verdict, dry-run
         binding, dry-run report not writable, sidecar, bad arguments) or rolled
         back (drift, reconciliation mismatch, any exception), or the dry-run
         saw the canonical file change while reading. Exit 1 from a confirmed
         run always means the canonical database was NOT left modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic.cjk_segmentation import segment_cjk  # noqa: E402
from mailattic.models import _normalize_body_text  # noqa: E402
from mailattic.query import normalize_message_id  # noqa: E402
from mailattic.storage import connect_database, resolve_database_default  # noqa: E402

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from body_rederive_stage import _sha256_text as _stage_sha256_text  # noqa: E402

# source_kind written by the stage builder -> messages.source_input_mode.
_MODE_FOR_KIND = {"pst": "file", "imap": "maildir"}

_DRYRUN_REPORT = "{stem}-apply-report-dryrun.json"
_CONFIRM_REPORT = "{stem}-apply-report-confirm.json"
_SIDECAR = "{stem}-apply-preimage.db"
_RESTORE_REPORT = "{stem}-restore-report.json"

_SAMPLE_SIZE = 10
_BUSY_TIMEOUT_MS = 30000


class ApplyRefused(RuntimeError):
    """Raised before any canonical write when the inputs are not fit."""


class ReconciliationError(RuntimeError):
    """Raised inside the write transaction to force a rollback."""


class Entry(NamedTuple):
    """One row to write. ``expected_*`` are re-checked inside the transaction;
    ``expected_fts`` None skips the FTS pre-check (restore)."""

    id: int
    expected_sha: str
    expected_fts: str | None
    new_body: str
    new_fts: str
    # Normalized Message-ID the row must still carry; None skips the check
    # (restore: its guard is the post-apply body sha).
    expected_message_id: str | None = None


# The pre-image fingerprint must be computed exactly as the stage builder
# computed it, so reuse its helper rather than keeping a second copy.
_sha = _stage_sha256_text


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _stat_snapshot(path: Path) -> dict[str, int]:
    """Size + mtime of the database file and its non-empty WAL sidecar.

    An EMPTY ``-wal`` is ignored: merely opening a WAL database read-only
    creates a 0-byte sidecar, which is not a write.
    """
    st = os.stat(path)
    snapshot = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and (wal_st := os.stat(wal)).st_size > 0:
        snapshot["wal_size"] = wal_st.st_size
        snapshot["wal_mtime_ns"] = wal_st.st_mtime_ns
    return snapshot


def _same_file(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    return left.exists() and right.exists() and left.samefile(right)


# ---------------------------------------------------------------------------
# Plan (apply)
# ---------------------------------------------------------------------------
def _check_stage(stage: Path) -> None:
    if not stage.exists():
        raise ApplyRefused(f"stage not found: {stage}")
    con = connect_database(stage, read_only=True)
    try:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"body_rederive", "stage_meta"} <= tables:
            raise ApplyRefused(
                f"{stage} is not a body re-derivation stage "
                "(body_rederive / stage_meta table missing)"
            )
        columns = {row[1] for row in con.execute("PRAGMA table_info(body_rederive)")}
        if "expected_message_id" not in columns:
            raise ApplyRefused(
                f"stage {stage} predates the expected_message_id column (it "
                "cannot prove which message each row belongs to); rebuild the stage"
            )
        kind = con.execute(
            "SELECT value FROM stage_meta WHERE key = 'source_kind'"
        ).fetchone()
        if kind is None:
            raise ApplyRefused(
                f"stage {stage} has no stage_meta.source_kind; the stage build "
                "did not finish"
            )
        stored = con.execute(
            "SELECT value FROM stage_meta WHERE key = 'report_json'"
        ).fetchone()
        if stored is None:
            raise ApplyRefused(
                f"stage {stage} has no stage_meta.report_json; stage incomplete "
                "-- rebuild it (its excluded/failed counts would be unreviewable)"
            )
        try:
            stage_report = json.loads(stored[0])
        except ValueError as exc:
            raise ApplyRefused(
                f"stage {stage} has an unreadable stage_meta.report_json "
                f"({exc!r}); stage incomplete -- rebuild it"
            ) from exc
        _require_complete_verdict(stage, stage_report)
    finally:
        con.close()


def _require_complete_verdict(stage: Path, report: object) -> None:
    """Fail closed: the stage's persisted verdict must be exactly
    ``complete: True``; missing / malformed / non-bool all refuse."""
    verdict = report.get("verdict") if isinstance(report, dict) else None
    if not isinstance(verdict, dict) or verdict.get("complete") is not True:
        reasons = verdict.get("reasons") if isinstance(verdict, dict) else None
        if isinstance(reasons, list) and reasons:
            detail = "; ".join(str(r) for r in reasons)
        elif verdict is None:
            detail = "the stage report carries no completeness verdict"
        else:
            detail = f"the completeness verdict is not a valid one: {verdict!r}"
        raise ApplyRefused(
            f"stage {stage} is not proven complete ({detail}); rebuild the stage"
        )


def _stage_report(stage: Path) -> dict:
    """The stage builder's own report (counts of excluded / failed rows)."""
    con = connect_database(stage, read_only=True)
    try:
        row = con.execute(
            "SELECT value FROM stage_meta WHERE key = 'report_json'"
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0])


def _preflight(canonical: Path, stage: Path) -> None:
    if not canonical.exists():
        raise ApplyRefused(f"canonical database not found: {canonical}")
    if _same_file(canonical, stage):
        raise ApplyRefused(
            f"--canonical and --stage resolve to the same file ({canonical}); "
            "the stage must be a separate database"
        )
    _check_stage(stage)


def _digest_plan(
    rows: list[tuple[int, str, str]], expected_message_ids: dict[int, str]
) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"BODY-REDERIVE-PLAN\x00")
    for mid, pre_sha, post_sha in sorted(rows):
        hasher.update(
            f"{mid}\x00{pre_sha}\x00{post_sha}\x00{expected_message_ids[mid]}\x00".encode()
        )
    return hasher.hexdigest()


def _current_message_id(raw: str | None) -> str:
    return normalize_message_id(raw)


def build_plan(canonical: Path, stage: Path) -> tuple[dict, dict]:
    """Read-only on both databases. Returns (plan, report)."""
    con_c = connect_database(canonical, read_only=True)
    con_s = connect_database(stage, read_only=True)
    counts = {
        "stage_rows": 0,
        "not_rewrite": 0,
        "missing_row": 0,
        "missing_fts": 0,
        "mode_mismatch": 0,
        "message_id_changed": 0,
        "drifted": 0,
        "planned": 0,
    }
    planned: list[tuple[int, str, str]] = []
    expected_ids: dict[int, str] = {}
    lengths: dict[int, tuple[int, int]] = {}
    try:
        cursor = con_s.execute(
            "SELECT message_id, source_kind, pre_image_sha256, new_body_text, status, "
            "expected_message_id FROM body_rederive ORDER BY message_id"
        )
        for row in cursor:
            counts["stage_rows"] += 1
            if row["status"] != "rewrite":
                counts["not_rewrite"] += 1
                continue
            mid = row["message_id"]
            current = con_c.execute(
                "SELECT source_input_mode, body_text, message_id "
                "FROM messages WHERE id = ?",
                (mid,),
            ).fetchone()
            if current is None:
                counts["missing_row"] += 1
                continue
            if _MODE_FOR_KIND.get(row["source_kind"]) != current["source_input_mode"]:
                counts["mode_mismatch"] += 1
                continue
            # The stage verified this id -> Message-ID pairing; ids can be
            # reassigned if the canonical DB was rebuilt since, and bodies
            # alone cannot tell (all empty bodies share one sha).
            expected_message_id = row["expected_message_id"]
            if (
                not expected_message_id
                or _current_message_id(current["message_id"]) != expected_message_id
            ):
                counts["message_id_changed"] += 1
                continue
            if _sha(current["body_text"]) != row["pre_image_sha256"]:
                counts["drifted"] += 1
                continue
            fts = con_c.execute(
                "SELECT 1 FROM message_fts WHERE rowid = ?", (mid,)
            ).fetchone()
            if fts is None:
                counts["missing_fts"] += 1
                continue
            stored = _normalize_body_text(row["new_body_text"])
            planned.append((mid, row["pre_image_sha256"], _sha(stored)))
            expected_ids[mid] = expected_message_id
            lengths[mid] = (len(current["body_text"]), len(stored))
    finally:
        con_c.close()
        con_s.close()
    counts["planned"] = len(planned)
    plan_digest = _digest_plan(planned, expected_ids)
    sample_ids = sorted(lengths)[:_SAMPLE_SIZE]
    plan = {"rows": sorted(planned), "expected_message_ids": expected_ids}
    report = {
        "plan_digest": plan_digest,
        "canonical": str(canonical),
        "stage": str(stage),
        "stage_sha256": _file_sha256(stage),
        "counts": counts,
        "stage_report": _stage_report(stage),
        "sample": [
            {"id": mid, "before_len": lengths[mid][0], "after_len": lengths[mid][1]}
            for mid in sample_ids
        ],
    }
    return plan, report


def _write_json(path: Path, payload: dict, label: str) -> bool:
    """True when the report landed. A failure is reported, never raised: the
    caller decides whether it may be survived."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"[{label}] WARNING: could not write {path} ({exc!r})", file=sys.stderr)
        return False
    print(f"[{label}] wrote {path}")
    return True


def _print_counts(label: str, counts: dict[str, int]) -> None:
    for key, value in counts.items():
        print(f"[{label}] {key}: {value}")


def run_dry_run(canonical: Path, stage: Path) -> int:
    try:
        _preflight(canonical, stage)
    except ApplyRefused as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1
    before = _stat_snapshot(canonical)
    _plan, report = build_plan(canonical, stage)
    after = _stat_snapshot(canonical)
    read_only = before == after
    report.update(mode="dry-run", read_only=read_only)
    print(f"[canonical] read_only={read_only}")
    if not read_only:
        print(
            "[refuse] canonical database changed while building the plan; "
            "this report is not evidence.",
            file=sys.stderr,
        )
        return 1
    # --confirm binds to this report: without it the dry-run produced no
    # reviewable artifact, so a "successful" exit would be false.
    if not _write_json(
        stage.parent / _DRYRUN_REPORT.format(stem=stage.stem), report, "dry-run"
    ):
        print(
            "[abort] the dry-run report could not be written; --confirm cannot "
            "bind to it. Fix the path and re-run the dry-run.",
            file=sys.stderr,
        )
        return 1
    print(f"[dry-run] plan_digest: {report['plan_digest']}")
    # _check_stage already refused anything but a complete verdict.
    print("[dry-run] stage verdict: complete")
    for source in report["stage_report"]["verdict"].get("missing_sources", []):
        print(f"[dry-run] stage missing source (file not found): {source}")
    _print_counts("dry-run", report["counts"])
    stage_counts = report["stage_report"].get("counts", {})
    print(
        f"[dry-run] stage build counts ({report['stage_report'].get('mode')}): "
        "review the exclusions before --confirm"
    )
    _print_counts("stage", stage_counts)
    return 0


_BINDING_KEYS = ("plan_digest", "canonical", "stage", "read_only")


def require_matching_dry_run(stage: Path, report: dict) -> None:
    """Refuse unless the dry-run report beside the stage describes exactly
    this plan (and itself proved the canonical read-only)."""
    report_path = stage.parent / _DRYRUN_REPORT.format(stem=stage.stem)
    if not report_path.exists():
        raise ApplyRefused(
            f"no dry-run report at {report_path}; run without --confirm first "
            "and review it before applying"
        )
    try:
        reviewed = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplyRefused(
            f"dry-run report {report_path} is unreadable: {exc!r}"
        ) from exc
    mismatches: list[str] = []
    for key in _BINDING_KEYS:
        value = reviewed.get(key)
        if key == "read_only":
            if value is not True:
                mismatches.append(f"read_only: dry-run={value!r} (must be True)")
            continue
        if key not in reviewed or value != report.get(key):
            mismatches.append(f"{key}: dry-run={value!r} now={report.get(key)!r}")
    if mismatches:
        raise ApplyRefused(
            f"this plan does not match the reviewed dry-run at {report_path}; "
            "re-run the dry-run and review it again. Differences: "
            + "; ".join(mismatches)
        )


# ---------------------------------------------------------------------------
# Pre-image sidecar
# ---------------------------------------------------------------------------
def _write_pre_image_sidecar(
    canonical: Path,
    stage: Path,
    sidecar: Path,
    plan: dict,
    plan_digest: str,
) -> int:
    """Save old body, old FTS body and the pre/post shas of every planned row
    to a fresh SQLite file. Refuses if the path exists. Committed, closed and
    re-read (row count) before returning; any failure is fatal to the run."""
    if sidecar.exists():
        raise ApplyRefused(
            f"pre-image sidecar already exists: {sidecar}; it belongs to an "
            "earlier run -- move or rename it deliberately, then retry"
        )
    with sidecar.open("xb"):
        pass  # claim the path atomically
    try:
        return _fill_pre_image_sidecar(canonical, stage, sidecar, plan, plan_digest)
    except BaseException:
        # Our own half-written file: remove it so a retry is not blocked by it.
        sidecar.unlink(missing_ok=True)
        raise


def _fill_pre_image_sidecar(
    canonical: Path,
    stage: Path,
    sidecar: Path,
    plan: dict,
    plan_digest: str,
) -> int:
    con_c = connect_database(canonical, read_only=True)
    try:
        out = sqlite3.connect(str(sidecar))
        try:
            out.execute("PRAGMA synchronous = FULL")
            out.execute(
                "CREATE TABLE pre_image (id INTEGER PRIMARY KEY, "
                "old_body_text TEXT NOT NULL, old_fts_body_text TEXT NOT NULL, "
                "pre_sha256 TEXT NOT NULL, post_sha256 TEXT NOT NULL)"
            )
            out.execute(
                "CREATE TABLE sidecar_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            for mid, pre_sha, post_sha in plan["rows"]:
                body = con_c.execute(
                    "SELECT body_text FROM messages WHERE id = ?", (mid,)
                ).fetchone()
                fts = con_c.execute(
                    "SELECT body_text FROM message_fts WHERE rowid = ?", (mid,)
                ).fetchone()
                if body is None or fts is None or _sha(body[0]) != pre_sha:
                    raise ApplyRefused(
                        f"row {mid} changed since the plan was built; "
                        "re-run the dry-run"
                    )
                out.execute(
                    "INSERT INTO pre_image VALUES (?, ?, ?, ?, ?)",
                    (mid, body[0], fts[0], pre_sha, post_sha),
                )
            out.executemany(
                "INSERT INTO sidecar_meta VALUES (?, ?)",
                [
                    ("canonical", str(canonical)),
                    ("stage", str(stage)),
                    ("plan_digest", plan_digest),
                    ("created_at", datetime.now(UTC).isoformat()),
                ],
            )
            out.commit()
        finally:
            out.close()
        check = sqlite3.connect(str(sidecar))
        try:
            stored = check.execute("SELECT COUNT(*) FROM pre_image").fetchone()[0]
        finally:
            check.close()
        if stored != len(plan["rows"]):
            raise ApplyRefused(
                f"sidecar holds {stored} rows, expected {len(plan['rows'])}"
            )
        with sidecar.open("r+b") as handle:  # Windows fsync needs a writable handle
            os.fsync(handle.fileno())
    finally:
        con_c.close()
    return len(plan["rows"])


# ---------------------------------------------------------------------------
# Transaction core (shared by apply and restore)
# ---------------------------------------------------------------------------
def _write_row(con: sqlite3.Connection, entry: Entry) -> None:
    """The only place canonical rows are written: both columns, as a pair."""
    cur = con.execute(
        "UPDATE messages SET body_text = ? WHERE id = ?", (entry.new_body, entry.id)
    )
    if cur.rowcount != 1:
        raise ReconciliationError(
            f"messages update hit {cur.rowcount} rows: {entry.id}"
        )
    cur = con.execute(
        "UPDATE message_fts SET body_text = ? WHERE rowid = ?",
        (entry.new_fts, entry.id),
    )
    if cur.rowcount != 1:
        raise ReconciliationError(
            f"message_fts update hit {cur.rowcount} rows: {entry.id}"
        )


def _nonplan_digest(con: sqlite3.Connection, plan_ids: set[int]) -> str:
    """Digest of every body_text (both tables) outside the plan."""
    hasher = hashlib.sha256()
    for table, key in (("messages", "id"), ("message_fts", "rowid")):
        hasher.update(table.encode() + b"\x00")
        for row in con.execute(
            f"SELECT {key}, body_text FROM {table} ORDER BY {key}"  # noqa: S608
        ):
            if row[0] in plan_ids:
                continue
            hasher.update(f"{row[0]}\x00".encode())
            hasher.update(row[1].encode("utf-8", "surrogatepass") + b"\x00")
    return hasher.hexdigest()


def _table_counts(con: sqlite3.Connection) -> tuple[int, int]:
    return (
        con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0],
    )


def _run_transaction(
    canonical: Path,
    plan_ids: set[int],
    entries: Callable[[], Iterator[Entry]],
) -> int:
    """Write all entries in one BEGIN IMMEDIATE transaction; roll back on any
    drift, exception or reconciliation mismatch (re-raised). Returns the number
    of rows written."""
    con = connect_database(canonical, create=False)
    con.isolation_level = None  # explicit transaction control
    con.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            counts_before = _table_counts(con)
            digest_before = _nonplan_digest(con, plan_ids)
            written = 0
            for entry in entries():
                row = con.execute(
                    "SELECT body_text, message_id FROM messages WHERE id = ?",
                    (entry.id,),
                ).fetchone()
                if row is None or _sha(row[0]) != entry.expected_sha:
                    raise ReconciliationError(
                        f"row {entry.id} changed since the plan was built"
                    )
                if (
                    entry.expected_message_id is not None
                    and _current_message_id(row[1]) != entry.expected_message_id
                ):
                    raise ReconciliationError(
                        f"row {entry.id} Message-ID changed since the plan was built"
                    )
                fts = con.execute(
                    "SELECT body_text FROM message_fts WHERE rowid = ?", (entry.id,)
                ).fetchone()
                if fts is None or (
                    entry.expected_fts is not None and fts[0] != entry.expected_fts
                ):
                    raise ReconciliationError(
                        f"message_fts row {entry.id} changed since the plan was built"
                    )
                _write_row(con, entry)
                written += 1
            if written != len(plan_ids):
                raise ReconciliationError(
                    f"rows written ({written}) != plan size ({len(plan_ids)})"
                )
            for entry in entries():
                body = con.execute(
                    "SELECT body_text FROM messages WHERE id = ?", (entry.id,)
                ).fetchone()
                fts = con.execute(
                    "SELECT body_text FROM message_fts WHERE rowid = ?", (entry.id,)
                ).fetchone()
                if body is None or body[0] != entry.new_body:
                    raise ReconciliationError(
                        f"messages.body_text mismatch: {entry.id}"
                    )
                if fts is None or fts[0] != entry.new_fts:
                    raise ReconciliationError(
                        f"message_fts.body_text mismatch: {entry.id}"
                    )
            if _table_counts(con) != counts_before:
                raise ReconciliationError("table row counts changed")
            if _nonplan_digest(con, plan_ids) != digest_before:
                raise ReconciliationError("rows outside the plan changed")
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return written


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def _apply_entries(
    sidecar: Path, stage: Path, expected_message_ids: dict[int, str]
) -> Callable[[], Iterator[Entry]]:
    def generate() -> Iterator[Entry]:
        side = connect_database(sidecar, read_only=True)
        stg = connect_database(stage, read_only=True)
        try:
            for mid, old_fts, pre_sha, post_sha in side.execute(
                "SELECT id, old_fts_body_text, pre_sha256, post_sha256 "
                "FROM pre_image ORDER BY id"
            ):
                staged = stg.execute(
                    "SELECT new_body_text FROM body_rederive WHERE message_id = ?",
                    (mid,),
                ).fetchone()
                if staged is None:
                    raise ReconciliationError(f"stage lost row {mid}")
                stored = _normalize_body_text(staged[0])
                if _sha(stored) != post_sha:
                    raise ReconciliationError(
                        f"stage body for {mid} changed since the plan was built"
                    )
                yield Entry(
                    mid,
                    pre_sha,
                    old_fts,
                    stored,
                    segment_cjk(stored),
                    expected_message_ids[mid],
                )
        finally:
            side.close()
            stg.close()

    return generate


def run_confirm(canonical: Path, stage: Path) -> int:
    try:
        _preflight(canonical, stage)
        plan, report = build_plan(canonical, stage)
        require_matching_dry_run(stage, report)
        if not plan["rows"]:
            raise ApplyRefused("the plan is empty; nothing to apply")
        sidecar = stage.parent / _SIDECAR.format(stem=stage.stem)
        saved = _write_pre_image_sidecar(
            canonical, stage, sidecar, plan, report["plan_digest"]
        )
    except ApplyRefused as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(
            f"[abort] {exc!r}; the canonical database was not modified.",
            file=sys.stderr,
        )
        return 1
    print(f"[confirm] pre-image sidecar: {sidecar} ({saved} rows)")

    plan_ids = {mid for mid, _pre, _post in plan["rows"]}
    try:
        written = _run_transaction(
            canonical,
            plan_ids,
            _apply_entries(sidecar, stage, plan["expected_message_ids"]),
        )
    except Exception as exc:
        print(
            f"[abort] rolled back, canonical database not modified: {exc}",
            file=sys.stderr,
        )
        return 1
    report.update(
        mode="confirm",
        rows_written=written,
        pre_image_sidecar=str(sidecar),
        finished_at=datetime.now(UTC).isoformat(),
    )
    _write_json(
        stage.parent / _CONFIRM_REPORT.format(stem=stage.stem), report, "confirm"
    )
    print(f"[confirm] rows written: {written}")
    return 0


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
def _restore_entries(sidecar: Path, ids: list[int]) -> Callable[[], Iterator[Entry]]:
    def generate() -> Iterator[Entry]:
        side = connect_database(sidecar, read_only=True)
        try:
            for mid in ids:
                old_body, old_fts, post_sha = side.execute(
                    "SELECT old_body_text, old_fts_body_text, post_sha256 "
                    "FROM pre_image WHERE id = ?",
                    (mid,),
                ).fetchone()
                yield Entry(mid, post_sha, None, old_body, old_fts)
        finally:
            side.close()

    return generate


def _check_sidecar(sidecar: Path, canonical: Path) -> None:
    if not sidecar.exists():
        raise ApplyRefused(f"sidecar not found: {sidecar}")
    if not canonical.exists():
        raise ApplyRefused(f"canonical database not found: {canonical}")
    con = connect_database(sidecar, read_only=True)
    try:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"pre_image", "sidecar_meta"} <= tables:
            raise ApplyRefused(f"{sidecar} is not a body re-derivation sidecar")
        meta = dict(con.execute("SELECT key, value FROM sidecar_meta"))
    finally:
        con.close()
    recorded = meta.get("canonical")
    if recorded is None or os.path.normcase(recorded) != os.path.normcase(
        str(canonical)
    ):
        raise ApplyRefused(
            f"sidecar was written for canonical {recorded!r}, not {str(canonical)!r}"
        )


def run_restore(canonical: Path, sidecar: Path, confirm: bool) -> int:
    try:
        _check_sidecar(sidecar, canonical)
    except (ApplyRefused, sqlite3.Error) as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1
    counts = {"sidecar_rows": 0, "missing_row": 0, "drifted_since_apply": 0}
    ids: list[int] = []
    side = connect_database(sidecar, read_only=True)
    con = connect_database(canonical, read_only=True)
    try:
        for mid, post_sha in side.execute(
            "SELECT id, post_sha256 FROM pre_image ORDER BY id"
        ):
            counts["sidecar_rows"] += 1
            row = con.execute(
                "SELECT body_text FROM messages WHERE id = ?", (mid,)
            ).fetchone()
            fts = con.execute(
                "SELECT 1 FROM message_fts WHERE rowid = ?", (mid,)
            ).fetchone()
            if row is None or fts is None:
                counts["missing_row"] += 1
            elif _sha(row[0]) != post_sha:
                counts["drifted_since_apply"] += 1
            else:
                ids.append(mid)
    finally:
        side.close()
        con.close()
    counts["restorable"] = len(ids)
    label = "restore" if confirm else "restore-dry-run"
    _print_counts(label, counts)
    if not confirm:
        print("[restore-dry-run] nothing written; add --confirm to restore")
        return 0
    if not ids:
        print("[abort] nothing to restore", file=sys.stderr)
        return 1
    try:
        written = _run_transaction(canonical, set(ids), _restore_entries(sidecar, ids))
    except Exception as exc:
        print(
            f"[abort] rolled back, canonical database not modified: {exc}",
            file=sys.stderr,
        )
        return 1
    _write_json(
        sidecar.parent / _RESTORE_REPORT.format(stem=sidecar.stem),
        {
            "mode": "restore",
            "canonical": str(canonical),
            "sidecar": str(sidecar),
            "counts": counts,
            "rows_written": written,
            "finished_at": datetime.now(UTC).isoformat(),
        },
        "restore",
    )
    print(f"[restore] rows restored: {written}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply a body re-derivation stage to the canonical database "
            "(issue #14 phase 2). Default is dry-run; --confirm writes."
        )
    )
    parser.add_argument("--canonical", type=Path, default=None)
    parser.add_argument("--stage", type=Path, default=None)
    parser.add_argument(
        "--restore",
        type=Path,
        default=None,
        metavar="SIDECAR",
        help="Put pre-images from a sidecar back (dry-run unless --confirm).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Write the canonical database. Absent = dry-run (default).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = _build_parser()
    args = parser.parse_args(argv)
    if (args.stage is None) == (args.restore is None):
        parser.error("give exactly one of --stage or --restore")
    canonical = resolve_database_default(args.canonical).expanduser().resolve()
    if args.restore is not None:
        sidecar = Path(args.restore).expanduser().resolve()
        return run_restore(canonical, sidecar, args.confirm)
    stage = Path(args.stage).expanduser().resolve()
    if args.confirm:
        return run_confirm(canonical, stage)
    return run_dry_run(canonical, stage)


if __name__ == "__main__":
    sys.exit(main())

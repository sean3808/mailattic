"""body_rederive_stage.py — stage builder for re-deriving stored message bodies
(issue #14 phase 2, first half of a two-step migration).

Phase 1 fixed the HTML-to-text conversion for newly ingested mail only. Rows
already in the canonical database still carry the pre-table-fidelity
``messages.body_text``. This script re-derives the body of PST-imported rows
(``source_input_mode='file'``) into a separate *stage* SQLite database. It
never writes the canonical database; a later step applies the stage.

How a row is re-derived (``pst`` subcommand):
  1. every distinct ``pst_source`` of the PST rows is re-unpacked with
     ``parser.ReadpstParser`` -- the same reader the original import used --
     so the body comes from ``parser._extract_body_text`` (via
     ``_parse_email_message``) passed through ``models._normalize_body_text``,
     exactly what ingest stores today. Nothing here reimplements extraction;
  2. a canonical row is paired with the re-unpacked message by
     (``pst_source``, ``logical_locator`` = ``<folder>/mbox#N``) and the pair
     is then verified by Message-ID (``query.normalize_message_id``);
  3. rows with no Message-ID are NEVER staged (Sean ruling, issue #14: their
     ``dedup_key`` embeds a body hash, so rewriting the body would make a
     later re-import disagree with the stored key). They are counted, and so
     is how many of them would contain a data table after re-derivation.

"Contains a table" is phase 1's own decision: ``_HTMLTextExtractor.
has_data_table`` as returned by ``parser._html_to_text_and_table_flag`` for the
message's HTML part(s). ``_extract_body_text`` consumes that flag but does not
expose it, so this script wraps the function for the duration of a run to
observe what the unchanged phase-1 code decided (no second heuristic).

Stage DB (``body_rederive`` table): canonical ``messages.id``, ``source_kind``
(``pst``), ``pre_image_sha256`` (SHA-256 of the canonical ``body_text`` encoded
as UTF-8, at extraction time -- the pre-image fingerprint the apply step must
re-check), ``new_body_text`` and ``status`` (``rewrite``). Rows whose
re-derived body equals the canonical one are counted as ``unchanged`` and not
staged.

Usage:
    uv run python scripts/body_rederive_stage.py pst --database <canonical.db> \\
        --stage <stage.db> [--readpst-path P] [--pst <path> ...] [--overwrite]

The canonical database is opened read-only (``mode=ro``). The only writes are
the stage database, a scratch work directory beside it (readpst output,
removed afterwards) and a JSON report ``<stage stem>-report.json`` next to the
stage. A stage that already exists is refused unless ``--overwrite`` is given.

Exit codes:
    0 — stage built, report written
    1 — refused (stage exists without --overwrite, stage is the canonical DB,
        canonical DB missing) or readpst could not be located
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic import parser as mail_parser  # noqa: E402
from mailattic.models import PstSource, _normalize_body_text  # noqa: E402
from mailattic.parser import ReadpstParser  # noqa: E402
from mailattic.query import normalize_message_id  # noqa: E402
from mailattic.storage import connect_database, resolve_database_default  # noqa: E402

SOURCE_KIND_PST = "pst"
STATUS_REWRITE = "rewrite"

_STAGE_SCHEMA = """
CREATE TABLE body_rederive (
    message_id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    pre_image_sha256 TEXT NOT NULL,
    new_body_text TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE stage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_OUTCOMES = (
    "rewrite",
    "unchanged",
    "no_message_id",
    "no_message_id_with_table",
    "no_message_id_unpaired",
    "message_id_mismatch",
    "locator_not_found",
    "pst_missing",
    "parse_failure",
)


class StageRefused(RuntimeError):
    """The run was refused before any write."""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _TableFlagRecorder:
    """Observes phase 1's data-table decision while messages are parsed."""

    def __init__(self) -> None:
        self.seen = False


@contextmanager
def _record_table_flag() -> Iterator[_TableFlagRecorder]:
    recorder = _TableFlagRecorder()
    original = mail_parser._html_to_text_and_table_flag

    def wrapper(html: str):
        result = original(html)
        if result[1]:
            recorder.seen = True
        return result

    mail_parser._html_to_text_and_table_flag = wrapper
    try:
        yield recorder
    finally:
        mail_parser._html_to_text_and_table_flag = original


def _same_path(left: str, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _report_path(stage: Path) -> Path:
    return stage.parent / f"{stage.stem}-report.json"


def _remove_stage_files(stage: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(stage) + suffix)
        if candidate.exists():
            candidate.unlink()


def _canonical_pst_sources(con: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in con.execute(
            "SELECT DISTINCT pst_source FROM messages "
            "WHERE source_input_mode = 'file' ORDER BY pst_source"
        )
    ]


def _canonical_rows_by_locator(
    con: sqlite3.Connection, pst_source: str
) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    cursor = con.execute(
        "SELECT id, message_id, logical_locator, body_text FROM messages "
        "WHERE source_input_mode = 'file' AND pst_source = ?",
        (pst_source,),
    )
    for row in cursor:
        rows.setdefault(row["logical_locator"], []).append(
            {
                "id": row["id"],
                "message_id": normalize_message_id(row["message_id"]),
                "body_sha256": _sha256_text(row["body_text"]),
            }
        )
    return rows


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(_OUTCOMES, 0)


def _classify_pair(
    row: dict,
    derived_message_id: str,
    new_body: str,
    has_table: bool,
    counts: dict[str, int],
    stage_con: sqlite3.Connection,
) -> None:
    if not row["message_id"]:
        # Never staged. Only count a table when the re-unpacked message is
        # also id-less, i.e. it is plausibly the same message.
        counts["no_message_id"] += 1
        if derived_message_id:
            counts["no_message_id_unpaired"] += 1
        elif has_table:
            counts["no_message_id_with_table"] += 1
        return
    if derived_message_id != row["message_id"]:
        counts["message_id_mismatch"] += 1
        return
    if _sha256_text(new_body) == row["body_sha256"]:
        counts["unchanged"] += 1
        return
    stage_con.execute(
        "INSERT INTO body_rederive "
        "(message_id, source_kind, pre_image_sha256, new_body_text, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (row["id"], SOURCE_KIND_PST, row["body_sha256"], new_body, STATUS_REWRITE),
    )
    counts["rewrite"] += 1


def _process_pst(
    pst_source: str,
    rows: dict[str, list[dict]],
    parser_factory,
    stage_con: sqlite3.Connection,
) -> dict[str, int]:
    counts = _empty_counts()
    if not Path(pst_source).exists():
        counts["pst_missing"] = sum(len(v) for v in rows.values())
        return counts

    source = PstSource(
        path=Path(pst_source), input_mode="file", requested_from=Path(pst_source)
    )
    failed_locators: set[str] = set()
    pst_level_failure = False
    parser = parser_factory()
    with _record_table_flag() as recorder:
        iterator = iter(parser.iter_extraction_results(source))
        while True:
            recorder.seen = False
            try:
                result = next(iterator)
            except StopIteration:
                break
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"{pst_source}: {exc}", file=sys.stderr)
                pst_level_failure = True
                break
            if result.failure is not None:
                if result.failure.logical_locator:
                    failed_locators.add(result.failure.logical_locator)
                continue
            extracted = result.message
            if extracted is None:
                continue
            matched = rows.pop(extracted.logical_locator, None)
            if matched is None:
                continue
            raw = extracted.raw_message
            derived_message_id = normalize_message_id(raw.get("message_id"))
            new_body = _normalize_body_text(raw.get("body_text"))
            for row in matched:
                _classify_pair(
                    row, derived_message_id, new_body, recorder.seen, counts, stage_con
                )

    for locator, leftover in rows.items():
        key = (
            "parse_failure"
            if pst_level_failure or locator in failed_locators
            else "locator_not_found"
        )
        counts[key] += len(leftover)
    return counts


def build_pst_stage(
    *,
    database: Path,
    stage: Path,
    readpst_path: Path | None,
    pst_filter: list[Path],
    overwrite: bool,
) -> dict:
    if database.resolve() == stage.resolve():
        raise StageRefused(f"--stage resolves to the canonical database ({database})")
    if not database.exists():
        raise StageRefused(f"canonical database not found: {database}")
    if stage.exists():
        if not overwrite:
            raise StageRefused(
                f"stage already exists: {stage} (use --overwrite to replace it)"
            )
        _remove_stage_files(stage)

    stage.parent.mkdir(parents=True, exist_ok=True)
    work_root = stage.parent / f"{stage.stem}-work"
    started = datetime.now(UTC)

    canonical = connect_database(database, read_only=True)
    try:
        pst_sources = _canonical_pst_sources(canonical)
        not_in_canonical: list[str] = []
        if pst_filter:
            wanted = [str(p) for p in pst_filter]
            selected = [
                s for s in pst_sources if any(_same_path(s, Path(w)) for w in wanted)
            ]
            not_in_canonical = [
                w
                for w in wanted
                if not any(_same_path(s, Path(w)) for s in pst_sources)
            ]
            pst_sources = selected

        parser_cache: list[ReadpstParser] = []

        def parser_factory() -> ReadpstParser:
            if not parser_cache:
                work_root.mkdir(parents=True, exist_ok=True)
                parser_cache.append(
                    ReadpstParser(readpst_path=readpst_path, work_root=work_root)
                )
            return parser_cache[0]

        stage_con = sqlite3.connect(stage)
        totals = _empty_counts()
        per_pst: list[dict] = []
        try:
            stage_con.executescript(_STAGE_SCHEMA)
            for pst_source in pst_sources:
                began = time.perf_counter()
                rows = _canonical_rows_by_locator(canonical, pst_source)
                row_count = sum(len(v) for v in rows.values())
                counts = _process_pst(pst_source, rows, parser_factory, stage_con)
                stage_con.commit()
                for key, value in counts.items():
                    totals[key] += value
                per_pst.append(
                    {
                        "pst_source": pst_source,
                        "rows": row_count,
                        "elapsed_seconds": round(time.perf_counter() - began, 3),
                        "counts": counts,
                    }
                )
            stage_con.executemany(
                "INSERT INTO stage_meta (key, value) VALUES (?, ?)",
                [
                    ("source_kind", SOURCE_KIND_PST),
                    ("canonical", str(database)),
                    ("created_at", started.isoformat()),
                ],
            )
            stage_con.commit()
        finally:
            stage_con.close()
    finally:
        canonical.close()
        with suppress(OSError):
            work_root.rmdir()

    return {
        "mode": "pst",
        "canonical": str(database),
        "stage": str(stage),
        "started_at": started.isoformat(),
        "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
        "rows_in_scope": sum(p["rows"] for p in per_pst),
        "counts": totals,
        "requested_psts_not_in_canonical": not_in_canonical,
        "per_pst": per_pst,
    }


def _print_summary(report: dict, report_path: Path) -> None:
    counts = report["counts"]
    print(
        f"[stage] {report['rows_in_scope']} PST rows in scope "
        f"across {len(report['per_pst'])} PST(s), "
        f"{report['elapsed_seconds']}s"
    )
    for key in _OUTCOMES:
        print(f"  {key}: {counts[key]}")
    print(f"[stage] stage database: {report['stage']}")
    print(f"[stage] report: {report_path}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage re-derived message bodies (issue #14 phase 2)."
    )
    sub = parser.add_subparsers(dest="source", required=True)
    pst = sub.add_parser("pst", help="Re-derive PST-imported rows via readpst.")
    pst.add_argument("--database", type=Path, default=None)
    pst.add_argument("--stage", type=Path, required=True)
    pst.add_argument("--readpst-path", type=Path, default=None)
    pst.add_argument("--pst", type=Path, action="append", default=[])
    pst.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing stage database.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args(argv)
    database = resolve_database_default(args.database).expanduser().resolve()
    stage = Path(args.stage).expanduser().resolve()
    try:
        report = build_pst_stage(
            database=database,
            stage=stage,
            readpst_path=args.readpst_path,
            pst_filter=list(args.pst),
            overwrite=args.overwrite,
        )
    except StageRefused as exc:
        print(f"[refuse] {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, mail_parser.ParserUnavailableError) as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1
    report_path = _report_path(stage)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_summary(report, report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

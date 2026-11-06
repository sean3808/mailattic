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
  2b. Message-ID fallback: a row WITH a Message-ID whose locator pairing failed
     (``locator_not_found`` or ``message_id_mismatch`` at the locator -- the
     PST changed after import) joins a pool. ``parse_failure`` /
     ``pst_unreadable`` / ``pst_missing`` rows do not. While every selected PST
     is re-unpacked, each extracted message that has a Message-ID is recorded
     (normalized Message-ID -> normalized body) in a temporary SQLite file in
     the ``-work`` directory (removed at the end, also when the run raises).
     After all PSTs are done the pool is resolved across ALL PSTs of the run:
     the Message-ID is found and every candidate carries the same body ->
     paired (``unchanged`` or staged ``rewrite``, counted again in the
     informational ``paired_by_message_id``); candidates disagree ->
     ``message_id_ambiguous`` (not staged); not found anywhere -> the row keeps
     its original locator reason. Counts are attributed to the row's own
     ``pst_source``. Rows paired by locator are never re-paired;
  3. rows with no Message-ID are NEVER staged (Sean ruling, issue #14: their
     ``dedup_key`` embeds a body hash, so rewriting the body would make a
     later re-import disagree with the stored key). They are counted, and so
     is how many of them would contain a data table after re-derivation. They
     never use the fallback.

The report's per-PST entries also carry ``extracted_messages`` (how many
messages readpst yielded), for cross-checking extraction completeness.

Extraction completeness ledger: readpst has been seen to stop early on a big
PST with return code 0 and empty stderr, which would make the missing rows look
like ``locator_not_found`` in a "successful" run. Before a PST is unpacked,
``lspst`` (same pst-utils package; ``--lspst-path``, default: next to the
resolved readpst, then PATH) lists it and the lines starting with ``Email`` are
counted. Ledger state per PST: ``ok`` (``lspst`` exit 0 and no
``A second message_store has been found`` abort line; ``ledger_emails`` = the
count) or ``unavailable`` (non-zero exit or that abort line; ``ledger_note``
says why -- e.g. the live Outlook PST aborts part-way). With ``ok``, a PST whose
``extracted_messages`` + failed messages is below ``ledger_emails`` is
INCOMPLETE (``extraction_complete`` false): its leftover canonical rows (those
that would be ``locator_not_found`` or pooled for the fallback) are counted as
``extraction_incomplete`` and are NOT pooled. Rows already paired stay staged
(each was Message-ID verified) and the PST's extracted messages still feed the
fallback store. More extracted than listed is fine. Per-PST entries carry
``ledger``, ``ledger_emails``, ``ledger_note`` and ``extraction_complete``
(null when the ledger is unavailable).

Recount ledger: a PST whose lspst ledger is ``unavailable`` (and that exists and
was read) is extracted once more by the same ``ReadpstParser`` command into a
fresh temporary directory under ``-work`` (removed afterwards, also on error)
and its messages are counted without parsing bodies (every ``mbox`` key plus
``*.eml`` files). State ``recount``: ``ledger_emails`` = that count,
``ledger_note`` = the lspst reason, and the completeness rule above applies to
it. Main extracted + failures above the recount means the two passes disagree
(at least one stopped early): the ledger becomes ``unavailable`` with a WARNING,
so the run exits 2. A recount that cannot run leaves the ledger
``unavailable`` (note extended). PSTs with an ``ok`` ledger are not
recounted. Per-PST entries carry ``recount_seconds`` (null when not run).

"Contains a table" is phase 1's own decision: ``_HTMLTextExtractor.
has_data_table`` as returned by ``parser._html_to_text_and_table_flag`` for the
message's HTML part(s). ``_extract_body_text`` consumes that flag but does not
expose it, so this script wraps the function for the duration of a run to
observe what the unchanged phase-1 code decided (no second heuristic).

Stage DB (``body_rederive`` table): canonical ``messages.id``, ``source_kind``
(``pst`` / ``imap`` / ``imap_pst``), ``expected_message_id`` (the normalized
Message-ID the pairing was verified against; apply re-checks it),
``pre_image_sha256`` (SHA-256 of the
canonical ``body_text`` encoded as UTF-8, at extraction time -- the pre-image
fingerprint the apply step must re-check), ``new_body_text`` and ``status``
(``rewrite``). Rows whose re-derived body equals the canonical one are counted
as ``unchanged`` and not staged.

Degradation guard (both subcommands): before a rewrite is staged the stored
body is compared with the re-derived one (``_degradation_reason``). A rewrite
that turns CJK text into Latin-1 mojibake (stored has >= 10 CJK chars, new
keeps < half of them and has >= 20 chars in U+00A1-U+00FF), or turns readable
text into mostly raw base64 lines (> 50% of new's non-empty lines vs < 20% of
old's), is NOT staged: it is counted ``degraded_kept`` (pst: in the row's own
per-PST counts, also for Message-ID-fallback rows; imap: per folder) and the
first 20 such canonical ids with their reason are listed in the report's
``degraded_sample_ids``. The stored body is read by id only when a rewrite is
about to be staged. The parser defect behind these bodies is a separate issue.

How IMAP rows are re-derived (``imap`` subcommand):
  * scope is ``source_input_mode='maildir'``; ``archive`` / ``outbound`` rows
    are out of scope and not counted;
  * ``logical_locator`` ``<display folder>/new/uid-N[.v<uidvalidity>[.<hash>]].eml``
    gives (display folder, UID). Old mbsync file names are counted as
    ``locator_unparsable`` and never fetched;
  * the display folder is mapped to the server's own wire name through
    ``NativeImapClient.list_folders()``; a folder no longer listed counts all
    its rows as ``folder_missing``;
  * per folder: SELECT readonly, then ``fetch_rfc822(uid)`` (BODY.PEEK). The
    body is what ingest derives for a maildir message (``_parse_email_message``
    + ``_normalize_body_text``). A UID the server no longer holds is
    ``not_on_server``. The derived Message-ID must equal the row's
    (UIDVALIDITY-change guard) or the row is ``message_id_mismatch``;
  * only LIST / STATUS / SELECT readonly / UID FETCH BODY.PEEK[] are issued;
  * a lost connection stops the run: what is staged is committed, the
    remaining rows are counted as ``fetch_error`` and the exit code is 2.

IMAP rows the server no longer holds (``imap-from-pst`` subcommand, #59):
  * the ``imap`` stage records the sorted canonical ``messages.id`` of every
    row counted ``not_on_server``, ``folder_missing`` or ``locator_unparsable``
    as ``unreachable`` in its report (``stage_meta.report_json`` and the JSON
    report): a list of ``{id, message_id}`` sorted by id, ``message_id`` being
    the normalized Message-ID; ``unreachable_ids_count`` is its length;
  * ``imap-from-pst`` takes that stage (``--imap-stage``; refused unless it is
    an ``imap`` stage with a complete verdict, a ``canonical`` and the list) and
    re-derives exactly
    those rows from PST content found by Message-ID. It unpacks the canonical
    PSTs like ``pst`` mode (same ledgers, completeness verdict and
    ``--pst-map`` option). There is no ``--pst`` filter: every canonical
    file-mode ``pst_source`` is searched, so "all candidates agree" always
    covers all of them. A PST whose file is gone is named in the verdict's
    ``missing_sources`` and does not block (the #14 ``pst_missing`` rule); a PST
    with per-message extraction failures makes the verdict NOT complete. Every
    extracted Message-ID's body is recorded; each listed id must be an existing
    ``source_input_mode='maildir'`` row whose normalized Message-ID equals the
    one the imap stage recorded (otherwise ``identity_mismatch``: another
    snapshot, never staged, verdict NOT complete) with a Message-ID (otherwise
    ``no_message_id``, never staged). Then, as in the pst fallback: found and
    all candidates agree -> ``unchanged`` or a staged ``rewrite`` (or
    ``degraded_kept``); candidates differ -> ``message_id_ambiguous``; not found
    -> ``not_in_pst``, or ``extraction_incomplete`` unless every existing PST
    is proven complete with zero extraction failures. The stage's ``source_kind``
    is ``imap_pst``. Rows the
    server still serves are never listed, so they are never overwritten with a
    PST-derived body. Refusals (bad ``--pst-map`` key, no canonical PST, unusable
    imap stage) happen before an old stage is cleared.

Usage:
    uv run python scripts/body_rederive_stage.py pst --database <canonical.db> \\
        --stage <stage.db> [--readpst-path P] [--lspst-path P] \\
        [--pst <path> ...] [--overwrite]
    uv run python scripts/body_rederive_stage.py imap --database <canonical.db> \\
        --stage <stage.db> --env-file <mailbox settings> [--folder <display> ...] \\
        [--limit N] [--overwrite]
    uv run python scripts/body_rederive_stage.py imap-from-pst \\
        --database <canonical.db> \\
        --stage <stage.db> --imap-stage <stage-imap.db> \\
        [--pst-map CANONICAL=LOCAL ...] [--readpst-path P] \\
        [--lspst-path P] [--overwrite]

The canonical database is opened read-only (``mode=ro``). The only writes are
the stage database, a scratch work directory beside it (readpst output,
removed afterwards) and a JSON report ``<stage stem>-report.json`` next to the
stage. A stage that already exists is refused unless ``--overwrite`` is given.
A stage holds one source kind (``stage_meta.source_kind``).

Completeness verdict: ``_verdict(report)`` computes ONE whitelist-shaped verdict
(``{"complete": bool, "reasons": [...], "missing_sources": [...]}``), stored as
``report["verdict"]`` (in ``stage_meta.report_json`` and the sidecar report).
``complete`` is True only when: pst / imap-from-pst -- (pst only: no
``pst_unreadable`` and) every PST has a
proven ledger (``ok`` / ``recount``) with ``extraction_complete`` True (a
missing PST file is allowed and listed in ``missing_sources``); imap -- not
aborted, no ``fetch_error``, no ``select_failed``. Unknown mode / missing keys
-> not complete. The exit code derives from it alone, and apply refuses any
stage whose verdict is not exactly ``complete: True``.

Exit codes:
    0 — stage built, report written, verdict complete
    1 — refused (stage exists without --overwrite, stage is the canonical DB,
        canonical DB missing), readpst could not be located, lspst could not
        be located (pst; refused before the previous stage is cleared), or the
        IMAP settings / connection could not be established
    2 — stage built but the verdict is NOT complete (connection lost, fetch /
        select errors, unreadable PST, incomplete extraction, or no
        completeness ledger): rerun the stage; apply will refuse it
"""

from __future__ import annotations

import argparse
import hashlib
import imaplib
import json
import mailbox
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mailattic import parser as mail_parser  # noqa: E402
from mailattic.imap_native import ImapFolder, NativeImapClient  # noqa: E402
from mailattic.imap_runtime import (  # noqa: E402
    load_env_file,
    validate_sync_environment,
)
from mailattic.models import PstSource, _normalize_body_text  # noqa: E402
from mailattic.parser import ReadpstParser  # noqa: E402
from mailattic.query import normalize_message_id  # noqa: E402
from mailattic.storage import connect_database, resolve_database_default  # noqa: E402

SOURCE_KIND_PST = "pst"
SOURCE_KIND_IMAP = "imap"
SOURCE_KIND_IMAP_PST = "imap_pst"
STATUS_REWRITE = "rewrite"

# Degradation guard thresholds (see _degradation_reason).
_MOJIBAKE_OLD_CJK_MIN = 10
_MOJIBAKE_CJK_KEEP_RATIO = 0.5
_MOJIBAKE_LATIN1_MIN = 20
_BASE64_LINE_RE = re.compile(r"^[A-Za-z0-9+/=]{40,}$")
_BASE64_NEW_MIN_SHARE = 0.5
_BASE64_OLD_MAX_SHARE = 0.2
_DEGRADED_SAMPLE_CAP = 20

_STAGE_SCHEMA = """
CREATE TABLE body_rederive (
    message_id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    pre_image_sha256 TEXT NOT NULL,
    new_body_text TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_message_id TEXT NOT NULL
);
CREATE TABLE stage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_OUTCOMES = (
    "rewrite",
    "unchanged",
    # degraded_kept: the re-derived body is visibly worse (mojibake / base64);
    # the stored body is kept and nothing is staged.
    "degraded_kept",
    "no_message_id",
    "no_message_id_with_table",
    "no_message_id_unpaired",
    "message_id_mismatch",
    "locator_not_found",
    "pst_missing",
    "parse_failure",
    "pst_unreadable",
    # extraction_incomplete: lspst's ledger says readpst yielded fewer messages
    # than the PST holds; the PST's leftover rows are not trusted as "gone".
    "extraction_incomplete",
    # message_id_ambiguous: fallback found the Message-ID with differing bodies.
    # paired_by_message_id: informational; how many of rewrite + unchanged came
    # via the fallback (those two stay the true totals).
    "message_id_ambiguous",
    "paired_by_message_id",
)

_IMAP_OUTCOMES = (
    "rewrite",
    "unchanged",
    "degraded_kept",
    "no_message_id",
    "message_id_mismatch",
    "locator_unparsable",
    "folder_missing",
    "not_on_server",
    "select_failed",
    "fetch_error",
    "parse_failure",
    # Informational: not an outcome of its own (the row also lands in one of
    # the outcomes above); the locator's UIDVALIDITY differs from the server's.
    "uidvalidity_changed",
)

_LOCATOR_RE = re.compile(
    r"^(?P<folder>.+)/new/uid-(?P<uid>\d+)"
    r"(?:\.v(?P<uidvalidity>\d+)(?:\.[0-9a-f]+)?)?\.eml$"
)

# imap-from-pst has no per-PST row ownership; the shared fallback store still
# wants a pst_source key, so every listed id is pooled under this one.
_IMAP_FROM_PST_POOL = ""

# imap-from-pst (#59): outcomes per listed canonical id. paired_by_message_id
# is informational (how many of rewrite + unchanged were resolved), exactly as
# in pst mode.
_IMAP_PST_OUTCOMES = (
    "rewrite",
    "unchanged",
    "degraded_kept",
    "no_message_id",
    "message_id_ambiguous",
    # not_in_pst: every PST of the run is proven complete and none holds it.
    "not_in_pst",
    # extraction_incomplete: not found, but some PST of the run is not proven
    # complete, so "not in PST" would be a claim the evidence cannot back.
    "extraction_incomplete",
    # identity_mismatch: the listed id is missing, not a maildir row, or its
    # Message-ID differs from the one the imap stage recorded (the stage
    # belongs to another snapshot).
    "identity_mismatch",
    "paired_by_message_id",
)

# A dropped / unreachable connection: stop the run (anything else is per row).
_CONNECTION_ERRORS = (imaplib.IMAP4.abort, OSError, EOFError)


class StageRefused(RuntimeError):
    """The run was refused before any write."""


_LSPST_ABORT_PREFIX = "A second message_store has been found"
_LEDGER_NOTE_PST_MISSING = "pst file missing"


def _resolve_lspst(lspst_path: Path | None, readpst_path: Path | None) -> Path:
    """``--lspst-path``, else next to the resolved readpst, else PATH."""
    if lspst_path is not None:
        candidate = lspst_path.expanduser().resolve()
        if candidate.exists():
            return candidate
        raise StageRefused(f"lspst not found at --lspst-path: {lspst_path}")
    candidates: list[Path] = []
    try:
        resolved_readpst = mail_parser.resolve_readpst_path(readpst_path)
    except mail_parser.ParserUnavailableError:
        resolved_readpst = None
    if resolved_readpst is not None:
        candidates += [
            resolved_readpst.parent / "lspst.exe",
            resolved_readpst.parent / "lspst",
        ]
    which_result = shutil.which("lspst.exe") or shutil.which("lspst")
    if which_result is not None:
        candidates.append(Path(which_result))
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise StageRefused(
        "Unable to locate lspst (needed for the extraction completeness "
        "ledger). Provide --lspst-path or put lspst next to readpst / on PATH."
    )


def _run_lspst(lspst: Path, pst: Path) -> tuple[int, str, str]:
    """(return code, stdout, stderr) of ``lspst <pst>``; no shell."""
    completed = subprocess.run(
        [str(lspst), str(pst)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _ledger(state: str, emails: int | None, note: str | None) -> dict:
    return {"state": state, "emails": emails, "note": note}


def _lspst_ledger(lspst: Path, pst: Path) -> dict:
    """Ledger of one existing PST: count of ``Email`` lines lspst lists."""
    try:
        returncode, stdout, stderr = _run_lspst(lspst, pst)
    except OSError as exc:
        return _ledger("unavailable", None, f"lspst could not run: {exc}")
    if returncode != 0:
        return _ledger("unavailable", None, f"lspst exited with {returncode}")
    lines = (stdout + "\n" + stderr).splitlines()
    if any(line.startswith(_LSPST_ABORT_PREFIX) for line in lines):
        return _ledger("unavailable", None, "lspst aborted: second message_store")
    emails = sum(1 for line in stdout.splitlines() if line.startswith("Email\t"))
    if emails == 0:
        # Nothing to compare against: an empty listing proves no completeness.
        return _ledger("unavailable", None, "lspst listed no Email items")
    return _ledger("ok", emails, None)


def _count_extraction(output_root: Path) -> int:
    """Messages in a readpst output tree, enumerated the way
    ``ReadpstParser.iter_extraction_results`` walks it (every ``*.eml`` plus
    every key of every ``mbox``), without parsing any body."""
    total = sum(1 for _ in output_root.rglob("*.eml"))
    for mbox_path in sorted(output_root.rglob("mbox")):
        mbox = mailbox.mbox(mbox_path, create=False)
        try:
            total += sum(1 for _ in mbox.iterkeys())
        finally:
            mbox.close()
    return total


def _recount_ledger(
    parser: ReadpstParser, pst: Path, work_root: Path, lspst_note: str | None
) -> dict:
    """Second ledger source for a PST lspst cannot list: run readpst once more
    (the parser's own command) into a fresh temp dir and count what it yields.
    The temp dir is removed afterwards, also on error."""
    work_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(dir=work_root, prefix="recount-"))
    try:
        output_root = temp_dir / "out"
        output_root.mkdir()
        parser._extract_pst(pst, output_root)
        return _ledger("recount", _count_extraction(output_root), lspst_note)
    except (OSError, RuntimeError, ValueError, mailbox.Error) as exc:
        return _ledger("unavailable", None, f"{lspst_note}; recount failed: {exc}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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


def _empty_counts(outcomes: tuple[str, ...] = _OUTCOMES) -> dict[str, int]:
    return dict.fromkeys(outcomes, 0)


def _count_range(text: str, low: int, high: int) -> int:
    return sum(1 for ch in text if low <= ord(ch) <= high)


def _base64_line_share(text: str) -> float:
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return 0.0
    return sum(1 for line in lines if _BASE64_LINE_RE.match(line)) / len(lines)


def _degradation_reason(old: str, new: str) -> str | None:
    """Why *new* is visibly worse than *old*: ``"mojibake"`` (CJK text turned
    into Latin-1 high characters), ``"base64"`` (readable text turned into
    mostly raw base64 lines), or None. When it cannot be told, None (keep the
    rewrite decision to the caller; PC-230)."""
    old_cjk = _count_range(old, 0x4E00, 0x9FFF)
    if (
        old_cjk >= _MOJIBAKE_OLD_CJK_MIN
        and _count_range(new, 0x4E00, 0x9FFF) < _MOJIBAKE_CJK_KEEP_RATIO * old_cjk
        and _count_range(new, 0x00A1, 0x00FF) >= _MOJIBAKE_LATIN1_MIN
    ):
        return "mojibake"
    if (
        _base64_line_share(new) > _BASE64_NEW_MIN_SHARE
        and _base64_line_share(old) < _BASE64_OLD_MAX_SHARE
    ):
        return "base64"
    return None


class _DegradedSamples:
    """First ``_DEGRADED_SAMPLE_CAP`` degraded canonical ids, with reason."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, row_id: int, reason: str) -> None:
        if len(self.items) < _DEGRADED_SAMPLE_CAP:
            self.items.append({"id": row_id, "reason": reason})


def _stored_body(canonical: sqlite3.Connection, row_id: int) -> str:
    """The stored body, fetched only when a rewrite is about to be staged."""
    found = canonical.execute(
        "SELECT body_text FROM messages WHERE id = ?", (row_id,)
    ).fetchone()
    return found[0] if found and found[0] is not None else ""


def _stage_or_keep(
    stage_con: sqlite3.Connection,
    canonical: sqlite3.Connection,
    row: dict,
    new_body: str,
    source_kind: str,
    counts: dict[str, int],
    samples: _DegradedSamples,
) -> None:
    """Stage the rewrite unless the re-derived body is visibly worse than the
    stored one (then keep the old body and count ``degraded_kept``)."""
    reason = _degradation_reason(_stored_body(canonical, row["id"]), new_body)
    if reason is not None:
        counts["degraded_kept"] += 1
        samples.add(row["id"], reason)
        return
    _stage_rewrite(stage_con, row, new_body, source_kind)
    counts["rewrite"] += 1


def _stage_rewrite(
    stage_con: sqlite3.Connection,
    row: dict,
    new_body: str,
    source_kind: str,
) -> None:
    stage_con.execute(
        "INSERT INTO body_rederive "
        "(message_id, source_kind, pre_image_sha256, new_body_text, status, "
        "expected_message_id) VALUES (?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            source_kind,
            row["body_sha256"],
            new_body,
            STATUS_REWRITE,
            # The normalized Message-ID the pairing was verified against; the
            # apply step re-checks it against the canonical row.
            row["message_id"],
        ),
    )


def _check_stage_target(database: Path, stage: Path, overwrite: bool) -> None:
    """Every refusal that can be decided without touching the stage."""
    if database.resolve() == stage.resolve():
        raise StageRefused(f"--stage resolves to the canonical database ({database})")
    if not database.exists():
        raise StageRefused(f"canonical database not found: {database}")
    if stage.exists() and not overwrite:
        raise StageRefused(
            f"stage already exists: {stage} (use --overwrite to replace it)"
        )


def _clear_stage_target(stage: Path) -> None:
    """The destructive half: call only after every refusal has been decided."""
    _remove_stage_files(stage)
    stage.parent.mkdir(parents=True, exist_ok=True)


class _FallbackStore:
    """On-disk (SQLite, inside the work directory) Message-ID fallback state:
    every extracted message that has a Message-ID, and the pool of canonical
    rows whose locator pairing failed. Kept off the Python heap: a real run
    holds tens of thousands of bodies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # A hard-killed earlier run can leave the file behind; it is scratch.
        _remove_stage_files(path)
        self._con = sqlite3.connect(path)
        self._con.executescript(
            "CREATE TABLE extracted (message_id TEXT NOT NULL, "
            "body_sha256 TEXT NOT NULL, body_text TEXT NOT NULL);"
            "CREATE TABLE pool (row_id INTEGER NOT NULL, pst_source TEXT NOT NULL, "
            "message_id TEXT NOT NULL, body_sha256 TEXT NOT NULL, "
            "reason TEXT NOT NULL);"
        )

    def add_extracted(self, message_id: str, body: str) -> None:
        self._con.execute(
            "INSERT INTO extracted VALUES (?, ?, ?)",
            (message_id, _sha256_text(body), body),
        )

    def add_pool(self, pst_source: str, row: dict, reason: str) -> None:
        self._con.execute(
            "INSERT INTO pool VALUES (?, ?, ?, ?, ?)",
            (row["id"], pst_source, row["message_id"], row["body_sha256"], reason),
        )

    def commit(self) -> None:
        self._con.commit()

    def resolve(self) -> Iterator[tuple[str, dict, str, list[str]]]:
        """Yield (pst_source, row, reason, candidate bodies): one distinct body
        per entry of the list, [] when the Message-ID was never extracted."""
        self._con.commit()
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS extracted_mid ON extracted (message_id)"
        )
        pool = self._con.execute(
            "SELECT row_id, pst_source, message_id, body_sha256, reason FROM pool "
            "ORDER BY rowid"
        )
        for row_id, pst_source, message_id, body_sha256, reason in pool:
            bodies = [
                r[0]
                for r in self._con.execute(
                    "SELECT MIN(body_text) FROM extracted WHERE message_id = ? "
                    "GROUP BY body_sha256",
                    (message_id,),
                )
            ]
            row = {"id": row_id, "message_id": message_id, "body_sha256": body_sha256}
            yield pst_source, row, reason, bodies

    def discard(self) -> None:
        with suppress(sqlite3.Error):
            self._con.close()
        with suppress(OSError):
            _remove_stage_files(self.path)


def _resolve_fallback(
    store: _FallbackStore,
    stage_con: sqlite3.Connection,
    counts_by_pst: dict[str, dict[str, int]],
    canonical: sqlite3.Connection,
    samples: _DegradedSamples,
    source_kind: str = SOURCE_KIND_PST,
) -> None:
    for pst_source, row, reason, bodies in store.resolve():
        counts = counts_by_pst[pst_source]
        if not bodies:
            counts[reason] += 1
        elif len(bodies) > 1:
            counts["message_id_ambiguous"] += 1
        elif _sha256_text(bodies[0]) == row["body_sha256"]:
            counts["unchanged"] += 1
            counts["paired_by_message_id"] += 1
        else:
            _stage_or_keep(
                stage_con, canonical, row, bodies[0], source_kind, counts, samples
            )
            counts["paired_by_message_id"] += 1


def _classify_pair(
    row: dict,
    derived_message_id: str,
    new_body: str,
    has_table: bool,
    counts: dict[str, int],
    stage_con: sqlite3.Connection,
    pst_source: str,
    store: _FallbackStore,
    canonical: sqlite3.Connection,
    samples: _DegradedSamples,
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
        # The PST changed since import: try the Message-ID fallback later.
        store.add_pool(pst_source, row, "message_id_mismatch")
        return
    if _sha256_text(new_body) == row["body_sha256"]:
        counts["unchanged"] += 1
        return
    _stage_or_keep(
        stage_con, canonical, row, new_body, SOURCE_KIND_PST, counts, samples
    )


def _process_pst(
    pst_source: str,
    rows: dict[str, list[dict]],
    parser_factory,
    stage_con: sqlite3.Connection,
    read_from: Path,
    store: _FallbackStore,
    canonical: sqlite3.Connection,
    samples: _DegradedSamples,
    ledger: dict | None = None,
    work_root: Path | None = None,
) -> tuple[dict[str, int], int, bool | None, dict | None, float | None, int]:
    """``pst_source`` keys the canonical rows; ``read_from`` is the file read.

    They differ when the PST moved since import (``--pst-map``). The locator
    is relative to readpst's output root, so pairing does not depend on it.

    Returns (counts, extracted, extraction_complete, the ledger actually used
    -- an ``unavailable`` lspst ledger is replaced by a ``recount`` one when
    *work_root* is given and the recount ran --, the recount seconds, the
    number of messages readpst yielded as per-message failures, and whether
    readpst itself failed part-way through the PST).
    """
    counts = _empty_counts()
    if not read_from.exists():
        counts["pst_missing"] = sum(len(v) for v in rows.values())
        return counts, 0, None, ledger, None, 0, False

    source = PstSource(path=read_from, input_mode="file", requested_from=read_from)
    failed_locators: set[str] = set()
    pst_level_failure = False
    extracted_count = 0
    failure_count = 0
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
                failure_count += 1
                if result.failure.logical_locator:
                    failed_locators.add(result.failure.logical_locator)
                continue
            extracted = result.message
            if extracted is None:
                continue
            extracted_count += 1
            raw = extracted.raw_message
            derived_message_id = normalize_message_id(raw.get("message_id"))
            new_body = _normalize_body_text(raw.get("body_text"))
            if derived_message_id:
                store.add_extracted(derived_message_id, new_body)
            matched = rows.pop(extracted.logical_locator, None)
            if matched is None:
                continue
            for row in matched:
                _classify_pair(
                    row,
                    derived_message_id,
                    new_body,
                    recorder.seen,
                    counts,
                    stage_con,
                    pst_source,
                    store,
                    canonical,
                    samples,
                )

    recount_seconds: float | None = None
    if (
        ledger is not None
        and ledger["state"] == "unavailable"
        and not pst_level_failure
        and work_root is not None
    ):
        began = time.perf_counter()
        ledger = _recount_ledger(parser, read_from, work_root, ledger["note"])
        recount_seconds = round(time.perf_counter() - began, 3)
        main_total = extracted_count + failure_count
        if ledger["state"] == "recount" and main_total > ledger["emails"]:
            # Two passes disagree: at least one stopped early, and nothing says
            # the main pass did not. No ledger -> completeness unproven (exit 2).
            print(
                f"[stage] WARNING: recount of {pst_source} found "
                f"{ledger['emails']} messages but the main pass yielded "
                f"{main_total}; the two passes disagree, completeness cannot be "
                "proven"
            )
            ledger = _ledger(
                "unavailable",
                None,
                f"{ledger['note']}; recount {ledger['emails']} < main {main_total}",
            )
    extraction_complete: bool | None = None
    if ledger is not None and ledger["state"] in ("ok", "recount"):
        extraction_complete = extracted_count + failure_count >= ledger["emails"]
    for locator, leftover in rows.items():
        if pst_level_failure:
            # readpst itself failed (e.g. the file is locked by Outlook): the
            # rows were never examined, which is not a per-message parse error.
            counts["pst_unreadable"] += len(leftover)
        elif locator in failed_locators:
            counts["parse_failure"] += len(leftover)
        elif extraction_complete is False:
            # The ledger says readpst stopped early: "not found" proves nothing.
            counts["extraction_incomplete"] += len(leftover)
        else:
            for row in leftover:
                if row["message_id"]:
                    store.add_pool(pst_source, row, "locator_not_found")
                else:
                    counts["locator_not_found"] += 1
    return (
        counts,
        extracted_count,
        extraction_complete,
        ledger,
        recount_seconds,
        failure_count,
        pst_level_failure,
    )


def build_pst_stage(
    *,
    database: Path,
    stage: Path,
    readpst_path: Path | None,
    lspst_path: Path | None = None,
    pst_filter: list[Path],
    overwrite: bool,
    pst_map: dict[str, Path] | None = None,
) -> dict:
    pst_map = pst_map or {}
    _check_stage_target(database, stage, overwrite)
    lspst = _resolve_lspst(lspst_path, readpst_path)
    work_root = stage.parent / f"{stage.stem}-work"
    started = datetime.now(UTC)

    canonical = connect_database(database, read_only=True)
    try:
        pst_sources = _canonical_pst_sources(canonical)
        unknown_keys = sorted(set(pst_map) - set(pst_sources))
        if unknown_keys:
            raise StageRefused(
                f"--pst-map names a pst_source not in the canonical DB: {unknown_keys}"
            )
        # Every refusal is decided; only now may a previous stage be removed.
        _clear_stage_target(stage)
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

        work_root.mkdir(parents=True, exist_ok=True)
        store = _FallbackStore(work_root / "message-id-fallback.db")
        try:
            stage_con = sqlite3.connect(stage)
        except BaseException:
            store.discard()
            raise
        per_pst: list[dict] = []
        samples = _DegradedSamples()
        try:
            stage_con.executescript(_STAGE_SCHEMA)
            for pst_source in pst_sources:
                began = time.perf_counter()
                rows = _canonical_rows_by_locator(canonical, pst_source)
                row_count = sum(len(v) for v in rows.values())
                read_from = pst_map.get(pst_source, Path(pst_source))
                if read_from.exists():
                    ledger = _lspst_ledger(lspst, read_from)
                else:
                    ledger = _ledger("unavailable", None, _LEDGER_NOTE_PST_MISSING)
                counts, extracted_count, complete, ledger, recount_seconds, _, _ = (
                    _process_pst(
                        pst_source,
                        rows,
                        parser_factory,
                        stage_con,
                        read_from,
                        store,
                        canonical,
                        samples,
                        ledger,
                        work_root,
                    )
                )
                if ledger is None:  # _process_pst echoes the ledger it was given
                    raise RuntimeError(f"no ledger for {pst_source}")
                stage_con.commit()
                store.commit()
                per_pst.append(
                    {
                        "pst_source": pst_source,
                        "read_from": str(read_from),
                        "rows": row_count,
                        "extracted_messages": extracted_count,
                        "ledger": ledger["state"],
                        "ledger_emails": ledger["emails"],
                        "ledger_note": ledger["note"],
                        "extraction_complete": complete,
                        "recount_seconds": recount_seconds,
                        "elapsed_seconds": round(time.perf_counter() - began, 3),
                        "counts": counts,
                    }
                )
            # Every PST is unpacked: resolve the fallback pool across all of
            # them (a row of PST A may live in PST B now).
            _resolve_fallback(
                store,
                stage_con,
                {p["pst_source"]: p["counts"] for p in per_pst},
                canonical,
                samples,
            )
            stage_con.commit()
            totals = _empty_counts()
            for entry in per_pst:
                for key, value in entry["counts"].items():
                    totals[key] += value
            report = {
                "mode": "pst",
                "canonical": str(database),
                "stage": str(stage),
                "started_at": started.isoformat(),
                "elapsed_seconds": round(
                    (datetime.now(UTC) - started).total_seconds(), 3
                ),
                "rows_in_scope": sum(p["rows"] for p in per_pst),
                "counts": totals,
                "requested_psts_not_in_canonical": not_in_canonical,
                "degraded_sample_ids": samples.items,
                "per_pst": per_pst,
            }
            report["verdict"] = _verdict(report)
            # The report travels with the stage (same commit as the other
            # stage_meta keys) so apply can show the exclusion counts even if
            # the sidecar report file is lost.
            stage_con.executemany(
                "INSERT INTO stage_meta (key, value) VALUES (?, ?)",
                [
                    ("source_kind", SOURCE_KIND_PST),
                    ("canonical", str(database)),
                    ("created_at", started.isoformat()),
                    ("report_json", json.dumps(report, ensure_ascii=False)),
                ],
            )
            stage_con.commit()
        finally:
            stage_con.close()
            store.discard()
    finally:
        canonical.close()
        with suppress(OSError):
            work_root.rmdir()

    return report


def _unreachable_entry(row: dict) -> dict:
    """What the imap stage records per unreachable row: the id plus the
    normalized Message-ID that identifies the row's content across copies of
    the canonical database."""
    return {"id": row["id"], "message_id": row["message_id"]}


def _read_imap_stage_ids(imap_stage: Path) -> list[dict]:
    """The unreachable ``{id, message_id}`` entries recorded by a finished,
    complete ``imap``
    stage (its ``stage_meta.report_json``). Refuses anything else: a missing or
    non-SQLite file, another source kind, a verdict that is not complete, no
    ``canonical``, or no ``unreachable`` list."""
    if not imap_stage.is_file():
        raise StageRefused(f"--imap-stage not found: {imap_stage}")
    try:
        con = sqlite3.connect(imap_stage.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            meta = dict(con.execute("SELECT key, value FROM stage_meta"))
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise StageRefused(f"--imap-stage is not a stage database: {exc}") from exc
    if meta.get("source_kind") != SOURCE_KIND_IMAP:
        raise StageRefused(
            f"--imap-stage source_kind is {meta.get('source_kind')!r}, "
            f"expected {SOURCE_KIND_IMAP!r}"
        )
    if not meta.get("canonical"):
        raise StageRefused("--imap-stage records no canonical database")
    try:
        report = json.loads(meta.get("report_json", ""))
    except ValueError:
        raise StageRefused("--imap-stage has no readable report_json") from None
    verdict = report.get("verdict") if isinstance(report, dict) else None
    if not isinstance(verdict, dict) or verdict.get("complete") is not True:
        raise StageRefused(
            "--imap-stage verdict is not complete; rerun the imap stage first"
        )
    entries = report.get("unreachable")
    if not isinstance(entries, list) or not all(
        isinstance(e, dict)
        and isinstance(e.get("id"), int)
        and not isinstance(e.get("id"), bool)
        and isinstance(e.get("message_id"), str)
        for e in entries
    ):
        raise StageRefused(
            "--imap-stage has no usable unreachable list (built before the "
            "Message-ID binding?); rerun the imap stage"
        )
    return sorted(
        {
            e["id"]: {"id": e["id"], "message_id": e["message_id"]} for e in entries
        }.values(),
        key=lambda e: e["id"],
    )


def build_imap_from_pst_stage(
    *,
    database: Path,
    stage: Path,
    imap_stage: Path,
    readpst_path: Path | None,
    lspst_path: Path | None = None,
    overwrite: bool,
    pst_map: dict[str, Path] | None = None,
) -> dict:
    """Re-derive the IMAP (maildir) rows the *imap_stage* could not reach, from
    PST content found by Message-ID (#59). Same unpack / ledger / fallback
    machinery as ``build_pst_stage``; only the listed rows are examined. Every
    canonical file-mode ``pst_source`` takes part (there is no PST filter); one
    whose file is gone is named in the verdict's ``missing_sources`` and
    does not block."""
    pst_map = pst_map or {}
    _check_stage_target(database, stage, overwrite)
    if imap_stage.resolve() == stage.resolve():
        raise StageRefused("--imap-stage and --stage are the same file")
    wanted_ids = _read_imap_stage_ids(imap_stage)
    lspst = _resolve_lspst(lspst_path, readpst_path)
    work_root = stage.parent / f"{stage.stem}-work"
    started = datetime.now(UTC)

    canonical = connect_database(database, read_only=True)
    try:
        pst_sources = _canonical_pst_sources(canonical)
        unknown_keys = sorted(set(pst_map) - set(pst_sources))
        if unknown_keys:
            raise StageRefused(
                f"--pst-map names a pst_source not in the canonical DB: {unknown_keys}"
            )
        if not pst_sources:
            raise StageRefused("no PST to search: the canonical DB has no file rows")
        # Every refusal is decided; only now may a previous stage be removed.
        _clear_stage_target(stage)

        parser_cache: list[ReadpstParser] = []

        def parser_factory() -> ReadpstParser:
            if not parser_cache:
                work_root.mkdir(parents=True, exist_ok=True)
                parser_cache.append(
                    ReadpstParser(readpst_path=readpst_path, work_root=work_root)
                )
            return parser_cache[0]

        work_root.mkdir(parents=True, exist_ok=True)
        store = _FallbackStore(work_root / "message-id-fallback.db")
        try:
            stage_con = sqlite3.connect(stage)
        except BaseException:
            store.discard()
            raise
        per_pst: list[dict] = []
        samples = _DegradedSamples()
        counts = _empty_counts(_IMAP_PST_OUTCOMES)
        try:
            stage_con.executescript(_STAGE_SCHEMA)
            for pst_source in pst_sources:
                began = time.perf_counter()
                read_from = pst_map.get(pst_source, Path(pst_source))
                if read_from.exists():
                    ledger = _lspst_ledger(lspst, read_from)
                else:
                    ledger = _ledger("unavailable", None, _LEDGER_NOTE_PST_MISSING)
                # No canonical PST rows are paired here: the unpack only
                # feeds the Message-ID store (and settles the ledger).
                (
                    _,
                    extracted_count,
                    complete,
                    ledger,
                    recount_seconds,
                    failures,
                    unreadable,
                ) = _process_pst(
                    pst_source,
                    {},
                    parser_factory,
                    stage_con,
                    read_from,
                    store,
                    canonical,
                    samples,
                    ledger,
                    work_root,
                )
                if ledger is None:  # _process_pst echoes the ledger it was given
                    raise RuntimeError(f"no ledger for {pst_source}")
                store.commit()
                per_pst.append(
                    {
                        "pst_source": pst_source,
                        "read_from": str(read_from),
                        "rows": 0,
                        "extracted_messages": extracted_count,
                        "extraction_failures": failures,
                        # readpst itself failed part-way: whatever was yielded
                        # before the failure cannot prove the PST complete.
                        "pst_unreadable": unreadable,
                        "ledger": ledger["state"],
                        "ledger_emails": ledger["emails"],
                        "ledger_note": ledger["note"],
                        "extraction_complete": complete,
                        "recount_seconds": recount_seconds,
                        "elapsed_seconds": round(time.perf_counter() - began, 3),
                    }
                )
            # "Not in PST" is claimed only when at least one PST file exists and
            # every existing one is proven complete with no per-message failure.
            # A PST whose file is gone is named by the verdict (missing_sources)
            # and, as in pst mode, does not by itself weaken that claim.
            existing = [
                p for p in per_pst if p["ledger_note"] != _LEDGER_NOTE_PST_MISSING
            ]
            all_proven = bool(existing) and all(
                p["extraction_complete"] is True
                and p["extraction_failures"] == 0
                and p["pst_unreadable"] is False
                for p in existing
            )
            not_found_reason = "not_in_pst" if all_proven else "extraction_incomplete"
            for entry in wanted_ids:
                found = canonical.execute(
                    "SELECT id, message_id, source_input_mode, body_text "
                    "FROM messages WHERE id = ?",
                    (entry["id"],),
                ).fetchone()
                # The imap stage lists only maildir rows of its own snapshot, so
                # a missing row, a non-maildir row or a different Message-ID all
                # mean this canonical DB is not that snapshot. Never staged.
                if (
                    found is None
                    or found["source_input_mode"] != "maildir"
                    or normalize_message_id(found["message_id"]) != entry["message_id"]
                ):
                    counts["identity_mismatch"] += 1
                    continue
                canonical_mid = normalize_message_id(found["message_id"])
                row = {
                    "id": found["id"],
                    "message_id": canonical_mid,
                    "body_sha256": _sha256_text(found["body_text"] or ""),
                }
                if not row["message_id"]:
                    counts["no_message_id"] += 1
                    continue
                store.add_pool(_IMAP_FROM_PST_POOL, row, not_found_reason)
            _resolve_fallback(
                store,
                stage_con,
                {_IMAP_FROM_PST_POOL: counts},
                canonical,
                samples,
                SOURCE_KIND_IMAP_PST,
            )
            stage_con.commit()
            report = {
                "mode": "imap-from-pst",
                "canonical": str(database),
                "stage": str(stage),
                "imap_stage": str(imap_stage),
                "started_at": started.isoformat(),
                "elapsed_seconds": round(
                    (datetime.now(UTC) - started).total_seconds(), 3
                ),
                "rows_in_scope": len(wanted_ids),
                "counts": counts,
                "degraded_sample_ids": samples.items,
                "per_pst": per_pst,
            }
            report["verdict"] = _verdict(report)
            stage_con.executemany(
                "INSERT INTO stage_meta (key, value) VALUES (?, ?)",
                [
                    ("source_kind", SOURCE_KIND_IMAP_PST),
                    ("canonical", str(database)),
                    ("created_at", started.isoformat()),
                    ("report_json", json.dumps(report, ensure_ascii=False)),
                ],
            )
            stage_con.commit()
        finally:
            stage_con.close()
            store.discard()
    finally:
        canonical.close()
        with suppress(OSError):
            work_root.rmdir()

    return report


def _canonical_maildir_rows(con: sqlite3.Connection) -> list[dict]:
    rows: list[dict] = []
    cursor = con.execute(
        "SELECT id, message_id, logical_locator, body_text FROM messages "
        "WHERE source_input_mode = 'maildir' ORDER BY id"
    )
    for row in cursor:
        rows.append(
            {
                "id": row["id"],
                "message_id": normalize_message_id(row["message_id"]),
                "locator": row["logical_locator"] or "",
                "body_sha256": _sha256_text(row["body_text"]),
            }
        )
    return rows


def _derive_imap_message(raw_bytes: bytes) -> tuple[str, str]:
    """(normalised Message-ID, normalised body) the way ingest derives a
    maildir message: ``_parse_eml_file`` is exactly these two calls on the
    file's bytes (no attachment_root, so nothing is written)."""
    message = BytesParser(policy=mail_parser._MAIL_POLICY).parsebytes(raw_bytes)
    raw = mail_parser._parse_email_message(message, raw_bytes=raw_bytes)
    return (
        normalize_message_id(raw.get("message_id")),
        _normalize_body_text(raw.get("body_text")),
    )


def _process_imap_folder(
    client,
    folder: ImapFolder,
    items: list[tuple[int, int | None, dict]],
    stage_con: sqlite3.Connection,
    counts: dict[str, int],
    canonical: sqlite3.Connection,
    samples: _DegradedSamples,
    unreachable: list[dict] | None = None,
) -> bool:
    """Re-fetch *items* (uid, locator uidvalidity, row) of one folder.
    Rows counted ``not_on_server`` are appended to *unreachable* as
    ``{id, message_id}``.

    Returns True when the connection was lost (the unprocessed rows have
    already been counted as ``fetch_error``)."""
    current_uidvalidity: int | None = None
    if any(uv is not None for _, uv, _ in items):
        try:
            current_uidvalidity = client.get_uidvalidity(folder.wire_name)
        except _CONNECTION_ERRORS:
            counts["fetch_error"] += len(items)
            return True
        except (RuntimeError, imaplib.IMAP4.error):
            current_uidvalidity = None  # informational counter only
    try:
        client.select_folder(folder.wire_name)
    except _CONNECTION_ERRORS:
        counts["fetch_error"] += len(items)
        return True
    except (RuntimeError, imaplib.IMAP4.error):
        counts["select_failed"] += len(items)
        return False

    for index, (uid, locator_uidvalidity, row) in enumerate(items):
        if not row["message_id"]:
            counts["no_message_id"] += 1
            continue
        if (
            locator_uidvalidity is not None
            and current_uidvalidity is not None
            and locator_uidvalidity != current_uidvalidity
        ):
            counts["uidvalidity_changed"] += 1
        try:
            raw_bytes = client.fetch_rfc822(uid)
        except _CONNECTION_ERRORS:
            counts["fetch_error"] += len(items) - index
            return True
        except RuntimeError as exc:
            # OK reply without a body = the UID is no longer on the server.
            if str(exc).startswith("Could not extract RFC822 bytes"):
                counts["not_on_server"] += 1
                if unreachable is not None:
                    unreachable.append(_unreachable_entry(row))
            else:
                counts["fetch_error"] += 1
            continue
        except imaplib.IMAP4.error:
            counts["fetch_error"] += 1
            continue
        try:
            derived_message_id, new_body = _derive_imap_message(raw_bytes)
        except Exception as exc:
            print(f"{folder.display_name} uid {uid}: {exc}", file=sys.stderr)
            counts["parse_failure"] += 1
            continue
        if derived_message_id != row["message_id"]:
            counts["message_id_mismatch"] += 1
            continue
        if _sha256_text(new_body) == row["body_sha256"]:
            counts["unchanged"] += 1
            continue
        _stage_or_keep(
            stage_con, canonical, row, new_body, SOURCE_KIND_IMAP, counts, samples
        )
    return False


def build_imap_stage(
    *,
    database: Path,
    stage: Path,
    env: dict[str, str],
    folder_filter: list[str],
    overwrite: bool,
    limit: int | None,
    client_factory: Callable[[dict[str, str]], NativeImapClient] | None = None,
) -> dict:
    _check_stage_target(database, stage, overwrite)
    factory = client_factory or (lambda e: NativeImapClient(e))
    started = datetime.now(UTC)

    # Stays open for the run: stored bodies are looked up by id, only for the
    # rows about to be staged (the row list carries hashes, not bodies).
    canonical = connect_database(database, read_only=True)
    try:
        return _build_imap_stage(
            canonical,
            database=database,
            stage=stage,
            env=env,
            folder_filter=folder_filter,
            overwrite=overwrite,
            limit=limit,
            factory=factory,
            started=started,
        )
    finally:
        canonical.close()


def _build_imap_stage(
    canonical: sqlite3.Connection,
    *,
    database: Path,
    stage: Path,
    env: dict[str, str],
    folder_filter: list[str],
    overwrite: bool,
    limit: int | None,
    factory: Callable[[dict[str, str]], NativeImapClient],
    started: datetime,
) -> dict:
    rows = _canonical_maildir_rows(canonical)
    if folder_filter:
        rows = [
            r
            for r in rows
            if any(r["locator"].startswith(f + "/") for f in folder_filter)
        ]
    if limit is not None:
        rows = rows[:limit]

    totals = _empty_counts(_IMAP_OUTCOMES)
    # Rows the server cannot serve (not_on_server / folder_missing /
    # locator_unparsable): the input list of the imap-from-pst subcommand.
    unreachable: list[dict] = []
    by_folder: dict[str, list[tuple[int, int | None, dict]]] = {}
    for row in rows:
        match = _LOCATOR_RE.match(row["locator"])
        if match is None:
            totals["locator_unparsable"] += 1
            unreachable.append(_unreachable_entry(row))
            continue
        uv = match["uidvalidity"]
        by_folder.setdefault(match["folder"], []).append(
            (int(match["uid"]), int(uv) if uv is not None else None, row)
        )

    per_folder: list[dict] = []
    samples = _DegradedSamples()
    aborted = False
    client = factory(env)
    try:
        listed: dict[str, ImapFolder] = {}
        for folder in client.list_folders():
            listed.setdefault(folder.display_name, folder)

        # Connection and LIST succeeded: only now may a previous stage go.
        _clear_stage_target(stage)
        stage_con = sqlite3.connect(stage)
        try:
            stage_con.executescript(_STAGE_SCHEMA)
            for display, items in by_folder.items():
                began = time.perf_counter()
                counts = _empty_counts(_IMAP_OUTCOMES)
                folder = listed.get(display)
                if folder is None:
                    counts["folder_missing"] += len(items)
                    unreachable.extend(_unreachable_entry(item[2]) for item in items)
                elif aborted:
                    counts["fetch_error"] += len(items)
                else:
                    items.sort(key=lambda item: item[0])
                    aborted = _process_imap_folder(
                        client,
                        folder,
                        items,
                        stage_con,
                        counts,
                        canonical,
                        samples,
                        unreachable,
                    )
                stage_con.commit()
                for key, value in counts.items():
                    totals[key] += value
                per_folder.append(
                    {
                        "folder": display,
                        "wire_name": folder.wire_name if folder else None,
                        "rows": len(items),
                        "elapsed_seconds": round(time.perf_counter() - began, 3),
                        "counts": counts,
                    }
                )
            unreachable_entries = sorted(
                {e["id"]: e for e in unreachable}.values(), key=lambda e: e["id"]
            )
            report = {
                "mode": "imap",
                "canonical": str(database),
                "stage": str(stage),
                "started_at": started.isoformat(),
                "elapsed_seconds": round(
                    (datetime.now(UTC) - started).total_seconds(), 3
                ),
                "rows_in_scope": len(rows),
                "aborted": aborted,
                "gone_from_server": totals["not_on_server"] + totals["folder_missing"],
                "unreachable": unreachable_entries,
                "unreachable_ids_count": len(unreachable_entries),
                "counts": totals,
                "degraded_sample_ids": samples.items,
                "per_folder": per_folder,
            }
            report["verdict"] = _verdict(report)
            stage_con.executemany(
                "INSERT INTO stage_meta (key, value) VALUES (?, ?)",
                [
                    ("source_kind", SOURCE_KIND_IMAP),
                    ("canonical", str(database)),
                    ("created_at", started.isoformat()),
                    ("report_json", json.dumps(report, ensure_ascii=False)),
                ],
            )
            stage_con.commit()
        finally:
            stage_con.close()
    finally:
        client.close()

    return report


def _verdict(report: dict) -> dict:
    """The ONE completeness verdict of a stage (whitelist: ``complete`` is True
    only when every condition holds; anything unexpected is NOT complete).

    pst: no unreadable PST, and every per-PST entry has a proven ledger
    (``ok`` / ``recount``) with ``extraction_complete`` exactly True -- or is a
    missing PST file (informational, listed in ``missing_sources``).
    imap-from-pst: the per-PST rule of pst (an unreadable PST never reaches
    a proven ledger, so it is caught there).
    imap: not aborted, no ``fetch_error``, no ``select_failed``.

    Persisted in the report, drives the exit code, and apply refuses a stage
    whose verdict is not exactly ``complete: True``."""
    reasons: list[str] = []
    missing_sources: list[str] = []
    mode = report.get("mode")
    counts = report.get("counts")
    if not isinstance(counts, dict):
        reasons.append("report has no counts")
        counts = {}
    if mode == "imap-from-pst" and counts.get("identity_mismatch") != 0:
        reasons.append(
            f"identity_mismatch is {counts.get('identity_mismatch')!r}, expected 0: "
            "the imap stage and this canonical snapshot do not belong together; "
            "rebuild the imap stage from this snapshot"
        )
    if mode == "imap-from-pst" and isinstance(report.get("per_pst"), list):
        readable = [
            e
            for e in report["per_pst"]
            if isinstance(e, dict) and e.get("ledger_note") != _LEDGER_NOTE_PST_MISSING
        ]
        if not readable:
            reasons.append("no PST file exists, so nothing was searched")
    if mode in ("pst", "imap-from-pst"):
        if mode == "pst" and counts.get("pst_unreadable") != 0:
            reasons.append(
                f"pst_unreadable is {counts.get('pst_unreadable')!r}, expected 0: "
                "a PST could not be read and its rows were not examined"
            )
        entries = report.get("per_pst")
        if not isinstance(entries, list):
            reasons.append("report has no per_pst list")
            entries = []
        for entry in entries:
            name = entry.get("pst_source") if isinstance(entry, dict) else None
            if (
                mode == "imap-from-pst"
                and isinstance(entry, dict)
                and entry.get("extraction_failures") != 0
            ):
                reasons.append(
                    f"{name}: {entry.get('extraction_failures')!r} message(s) "
                    "failed extraction; their Message-IDs never reached the "
                    "lookup, so candidate agreement cannot be proven"
                )
            if (
                mode == "imap-from-pst"
                and isinstance(entry, dict)
                and entry.get("pst_unreadable") is not False
            ):
                reasons.append(
                    f"{name}: readpst failed part-way (or the flag is missing), "
                    "so the PST is not proven completely extracted"
                )
            if not isinstance(entry, dict):
                reasons.append(f"malformed per_pst entry: {entry!r}")
            elif entry.get("ledger_note") == _LEDGER_NOTE_PST_MISSING:
                missing_sources.append(str(name))
            elif entry.get("ledger") not in ("ok", "recount"):
                reasons.append(
                    f"{name}: no completeness ledger (ledger={entry.get('ledger')!r}, "
                    f"note={entry.get('ledger_note')!r}); completeness cannot be proven"
                )
            elif entry.get("extraction_complete") is not True:
                reasons.append(
                    f"{name}: extraction incomplete (extracted "
                    f"{entry.get('extracted_messages')!r}, ledger lists "
                    f"{entry.get('ledger_emails')!r}, "
                    f"extraction_complete={entry.get('extraction_complete')!r})"
                )
    elif mode == "imap":
        if report.get("aborted") is not False:
            reasons.append("imap run aborted (connection lost)")
        for key in ("fetch_error", "select_failed"):
            if counts.get(key) != 0:
                reasons.append(f"{key} is {counts.get(key)!r}, expected 0")
    else:
        reasons.append(f"unknown report mode {mode!r}")
    return {
        "complete": not reasons,
        "reasons": reasons,
        "missing_sources": missing_sources,
    }


def _print_summary(report: dict, report_path: Path) -> None:
    counts = report["counts"]
    if report["mode"] == "imap-from-pst":
        print(
            f"[stage] {report['rows_in_scope']} unreachable IMAP id(s) "
            "from the imap stage, "
            f"{len(report['per_pst'])} PST(s) unpacked, {report['elapsed_seconds']}s"
        )
        outcomes = _IMAP_PST_OUTCOMES
    elif report["mode"] == "imap":
        print(
            f"[stage] {report['rows_in_scope']} IMAP rows in scope "
            f"across {len(report['per_folder'])} folder(s), "
            f"{report['elapsed_seconds']}s"
        )
        outcomes = _IMAP_OUTCOMES
    else:
        print(
            f"[stage] {report['rows_in_scope']} PST rows in scope "
            f"across {len(report['per_pst'])} PST(s), "
            f"{report['elapsed_seconds']}s"
        )
        outcomes = _OUTCOMES
    for key in outcomes:
        print(f"  {key}: {counts[key]}")
    if report["mode"] == "imap":
        print(
            f"  unreachable ids (input of imap-from-pst): "
            f"{report['unreachable_ids_count']}"
        )
        print(
            f"  gone_from_server (not_on_server + folder_missing): "
            f"{report['gone_from_server']}"
        )
    verdict = report["verdict"]
    if verdict["complete"]:
        print("[stage] VERDICT: complete")
    else:
        print("[stage] VERDICT: NOT complete — rerun the stage")
        for reason in verdict["reasons"]:
            print(f"[stage]   reason: {reason}")
    for source in verdict["missing_sources"]:
        print(f"[stage]   missing source (file not found): {source}")
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
    pst.add_argument(
        "--lspst-path",
        type=Path,
        default=None,
        help="lspst binary for the completeness ledger (default: next to "
        "readpst, then PATH).",
    )
    pst.add_argument("--pst", type=Path, action="append", default=[])
    pst.add_argument(
        "--pst-map",
        action="append",
        default=[],
        metavar="CANONICAL=LOCAL",
        help="Read the PST recorded as CANONICAL pst_source from LOCAL instead "
        "(the file moved since import). Repeatable.",
    )
    pst.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing stage database.",
    )
    imap = sub.add_parser(
        "imap", help="Re-derive IMAP-ingested (maildir) rows by re-fetching."
    )
    imap.add_argument("--database", type=Path, default=None)
    imap.add_argument("--stage", type=Path, required=True)
    imap.add_argument(
        "--env-file",
        type=Path,
        required=True,
        help="Mailbox settings file (same format as `bes sync-imap --env-file`).",
    )
    imap.add_argument(
        "--folder",
        action="append",
        default=[],
        help="Display folder name to limit the run to (repeatable).",
    )
    imap.add_argument("--limit", type=int, default=None, help="Max rows (trial run).")
    imap.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing stage database.",
    )
    from_pst = sub.add_parser(
        "imap-from-pst",
        help="Re-derive the IMAP rows the server no longer has from PST "
        "content, found by Message-ID (#59).",
    )
    from_pst.add_argument("--database", type=Path, default=None)
    from_pst.add_argument("--stage", type=Path, required=True)
    from_pst.add_argument(
        "--imap-stage",
        type=Path,
        required=True,
        help="A finished, complete `imap` stage database; its unreachable list "
        "are the rows to re-derive.",
    )
    from_pst.add_argument("--readpst-path", type=Path, default=None)
    from_pst.add_argument("--lspst-path", type=Path, default=None)
    from_pst.add_argument(
        "--pst-map",
        action="append",
        default=[],
        metavar="CANONICAL=LOCAL",
        help="Read the PST recorded as CANONICAL pst_source from LOCAL instead. "
        "Repeatable.",
    )
    from_pst.add_argument("--overwrite", action="store_true")
    return parser


def _parse_pst_map(entries: list[str]) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for entry in entries:
        canonical, sep, local = entry.partition("=")
        if not sep or not canonical or not local:
            raise StageRefused(f"--pst-map expects CANONICAL=LOCAL, got {entry!r}")
        mapping[canonical] = Path(local)
    return mapping


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args(argv)
    database = resolve_database_default(args.database).expanduser().resolve()
    stage = Path(args.stage).expanduser().resolve()
    try:
        if args.source == "imap":
            env = load_env_file(args.env_file)
            validate_sync_environment(env)
            report = build_imap_stage(
                database=database,
                stage=stage,
                env=env,
                folder_filter=list(args.folder),
                overwrite=args.overwrite,
                limit=args.limit,
            )
        elif args.source == "imap-from-pst":
            report = build_imap_from_pst_stage(
                database=database,
                stage=stage,
                imap_stage=Path(args.imap_stage).expanduser().resolve(),
                readpst_path=args.readpst_path,
                lspst_path=args.lspst_path,
                overwrite=args.overwrite,
                pst_map=_parse_pst_map(args.pst_map),
            )
        else:
            report = build_pst_stage(
                database=database,
                stage=stage,
                readpst_path=args.readpst_path,
                lspst_path=args.lspst_path,
                pst_filter=list(args.pst),
                overwrite=args.overwrite,
                pst_map=_parse_pst_map(args.pst_map),
            )
    except StageRefused as exc:
        print(f"[refuse] {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, mail_parser.ParserUnavailableError) as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, imaplib.IMAP4.error, OSError) as exc:
        # imap only: settings missing/invalid, or the connection / LIST failed
        # before anything was staged. (A pst run keeps its own behaviour.)
        if args.source != "imap":
            raise
        print(f"[abort] {exc}", file=sys.stderr)
        return 1
    report_path = _report_path(stage)
    try:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(
            f"[abort] the stage was built but the report file {report_path} could "
            f"not be written ({exc!r}); the counts are kept in the stage's "
            "stage_meta.report_json. Fix the path and rebuild with --overwrite.",
            file=sys.stderr,
        )
        return 1
    _print_summary(report, report_path)
    # The persisted verdict is the only source of the exit code.
    return 0 if report["verdict"]["complete"] else 2


if __name__ == "__main__":
    sys.exit(main())

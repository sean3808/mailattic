"""Database maintenance utilities: WAL backup, health checks, and FTS reindex.

Design decisions:
- backup() uses sqlite3.Connection.backup() (online backup API) — never copies .db
  directly, because uncheckpointed WAL changes would be lost.
- Default output anchors to resolved DB parent directory, not caller cwd — because
  the global `bes` runs from arbitrary project directories.
- doctor() collects ALL pragma results before returning; never aborts on first failure.
- reindex_fts() executes the full DELETE + repopulate in a single transaction with no
  intermediate commits. Any exception or interruption rolls back the entire operation,
  preserving the pre-existing complete index (design.md: "中斷安全硬契約").
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .cjk_segmentation import segment_cjk
from .models import build_participant_search_text

_BUSY_TIMEOUT_MS = 30_000  # 30 seconds — absorbs transient IMAP sync competition


def backup_database(
    source_path: str | Path,
    output_path: str | Path | None = None,
) -> Path:
    """Produce a consistent single-file WAL snapshot using sqlite3.Connection.backup().

    Args:
        source_path: Resolved path to the source SQLite database.
        output_path: Optional explicit output path.  When omitted, defaults to
            ``<source_db_dir>/backups/<YYYYMMDD-HHMMSS>.db`` — anchored to the
            source DB's parent directory, never the caller's cwd.

    Returns:
        Absolute Path of the produced backup file.

    Raises:
        FileNotFoundError: source_path does not exist or cannot be opened.
        FileExistsError: resolved output path already exists.
        ValueError: resolved output path equals source_path.
        OSError: any I/O failure during the backup.
    """
    src = Path(source_path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source database not found: {src}")

    # Resolve output path
    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = src.parent / "backups" / f"{timestamp}.db"
    else:
        dest = Path(output_path).expanduser().resolve()

    # Safety guards
    if dest == src:
        raise ValueError(f"Output path is the same as the source database: {dest}")
    if dest.exists():
        raise FileExistsError(
            f"Output path already exists (will not overwrite): {dest}"
        )

    # Ensure output directory exists
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Open source connection; the sqlite3 `timeout` arg sets the busy timeout,
    # letting the backup tolerate a concurrent writer (e.g. in-progress IMAP sync).
    src_conn = sqlite3.connect(str(src), timeout=_BUSY_TIMEOUT_MS / 1000)
    try:
        # Write to a temp file in the same directory, then atomic rename
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=dest.parent, suffix=".db.tmp")
        os.close(tmp_fd)
        tmp_path = Path(tmp_path_str)

        try:
            dst_conn = sqlite3.connect(str(tmp_path))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()

            # Atomic hard-link into final position; os.link raises FileExistsError
            # atomically if dest was created between our earlier exists() check and
            # now, preventing silent overwrites (TOCTOU fix).
            os.link(tmp_path, dest)
            os.unlink(tmp_path)
        except Exception:
            # Clean up temp file on failure — do NOT leave partial backup
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise
    finally:
        src_conn.close()

    return dest.resolve()


def check_database(
    source_path: str | Path,
) -> dict[str, object]:
    """Run four PRAGMA health checks against the database.

    Collects ALL results before returning — never aborts on the first failing
    check — so the caller can see the full picture at once.

    Args:
        source_path: Resolved path to the SQLite database to check.

    Returns:
        A dict with keys:
            - ``db_path``: absolute path string
            - ``journal_mode``: str
            - ``integrity_check``: list[str]   — ["ok"] on success
            - ``quick_check``: list[str]        — ["ok"] on success
            - ``foreign_key_check``: list[str]  — [] on success
            - ``passed``: bool  — True iff all three checks indicate no problems

    Raises:
        FileNotFoundError: source_path does not exist.
    """
    src = Path(source_path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Database not found: {src}")

    conn = sqlite3.connect(str(src))
    try:
        # journal_mode
        journal_mode: str = conn.execute("PRAGMA journal_mode;").fetchone()[0]

        # integrity_check — "ok" when healthy; problem rows otherwise
        integrity_results = [
            row[0] for row in conn.execute("PRAGMA integrity_check;").fetchall()
        ]

        # quick_check — subset of integrity_check
        quick_results = [
            row[0] for row in conn.execute("PRAGMA quick_check;").fetchall()
        ]

        # foreign_key_check — empty set = no violations
        foreign_key_results = [
            str(row[0]) for row in conn.execute("PRAGMA foreign_key_check;").fetchall()
        ]
    finally:
        conn.close()

    integrity_ok = integrity_results == ["ok"]
    quick_ok = quick_results == ["ok"]
    fk_ok = len(foreign_key_results) == 0

    return {
        "db_path": str(src),
        "journal_mode": journal_mode,
        "integrity_check": integrity_results,
        "quick_check": quick_results,
        "foreign_key_check": foreign_key_results,
        "passed": integrity_ok and quick_ok and fk_ok,
    }


@dataclass(frozen=True, slots=True)
class ReindexResult:
    """Result of a full FTS reindex operation."""

    messages_count: int
    fts_count: int


def reindex_fts(source_path: str | Path) -> ReindexResult:
    """Rebuild ``message_fts`` from ``messages`` with CJK segmentation applied.

    The entire DELETE + repopulate sequence executes inside a **single transaction
    with no intermediate commits**.  Any exception or interruption causes a full
    rollback, preserving the pre-existing complete index intact.

    Design contract (design.md "全量 reindex 機制"):
    - idempotent: running twice produces the same index state.
    - After success: ``SELECT COUNT(*) FROM message_fts``
      == ``SELECT COUNT(*) FROM messages``.
    - On failure/interruption: ``message_fts`` retains its pre-reindex content; keyword
      recall is NOT degraded.

    Segmentation rules mirror storage.py ``persist_message``:
    - subject, recipients, body_text, attachment_names: segmented via ``segment_cjk``.
    - sender: NOT segmented (task 2.1 contract).

    Args:
        source_path: Path to the SQLite database file.

    Returns:
        ReindexResult with ``messages_count`` and ``fts_count``
        (always equal on success).

    Raises:
        FileNotFoundError: source_path does not exist.
        sqlite3.Error: any database error (transaction is rolled back automatically).
    """
    src = Path(source_path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Database not found: {src}")

    # Use a generous busy timeout to tolerate concurrent IMAP sync on the live WAL DB.
    conn = sqlite3.connect(str(src), timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    try:
        # A single explicit transaction wraps the entire DELETE + INSERT cycle.
        # Python's sqlite3 module uses autocommit by default for DDL; for DML we
        # use an explicit BEGIN to guarantee single-transaction atomicity.
        conn.execute("BEGIN")
        try:
            # Step 1: clear the existing FTS content.
            conn.execute("DELETE FROM message_fts")

            # Step 2: streaming cursor over messages — avoids loading 56k rows
            # into memory at once.
            read_cur = conn.execute(
                """
                SELECT
                    id,
                    subject,
                    sender_name,
                    sender_email,
                    recipient_search_text,
                    body_text,
                    attachment_search_text
                FROM messages
                ORDER BY id
                """
            )

            write_cur = conn.cursor()
            for row in read_cur:
                row_id = row["id"]
                # Sender FTS text via the same canonical helper as the index
                # write path (storage.py), so reindex never drifts from it.
                sender_search = build_participant_search_text(
                    row["sender_name"], row["sender_email"]
                )
                write_cur.execute(
                    """
                    INSERT INTO message_fts (
                        rowid,
                        message_rowid,
                        subject,
                        sender,
                        recipients,
                        body_text,
                        attachment_names
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        row_id,
                        row_id,
                        segment_cjk(row["subject"]),
                        sender_search,  # sender: NOT segmented
                        segment_cjk(row["recipient_search_text"]),
                        segment_cjk(row["body_text"]),
                        segment_cjk(row["attachment_search_text"]),
                    ],
                )

            # Step 3: verify row count parity before committing.
            messages_count: int = conn.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
            fts_count: int = conn.execute(
                "SELECT COUNT(*) FROM message_fts"
            ).fetchone()[0]

            if messages_count != fts_count:
                raise RuntimeError(
                    f"FTS row count mismatch after reindex: "
                    f"messages={messages_count}, fts={fts_count}"
                )

            conn.execute("COMMIT")
        except Exception:
            # Rollback on ANY failure — preserves the pre-existing complete index.
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return ReindexResult(messages_count=messages_count, fts_count=fts_count)

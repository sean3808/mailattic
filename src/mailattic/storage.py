from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .cjk_segmentation import segment_cjk
from .models import IngestFailure, NormalizedMessage
from .schema import SCHEMA_SQL

DEFAULT_DB_NAME = r"C:\IT_Projects\mailattic\mailattic.db"
ENV_VAR_DB = "MAILATTIC_DB"

# Fixed key for the single-row sync_state table (design.md: "Persist one row
# of sync state, not a run history table" -- the row is keyed by a fixed
# constant rather than by account or folder).
SYNC_STATE_KEY = "native_sync"


def _recipient_domain_match(recipient_text: str | None, domain: str | None) -> int:
    """Boundary-aware recipient-domain match, registered as a SQL function.

    Anchors at the domain boundary ("not followed by another domain-label
    character") rather than an unanchored substring, so ``acme.com`` matches
    ``bob@acme.com`` (including embedded mid-blob in a legacy packed
    recipient field) but not ``alice@acme.com.evil`` or
    ``alice@acme.com海灣``. See design.md > "Register a boundary-aware
    recipient_domain_match SQL function in the connection factory".
    """
    if not recipient_text or not domain:
        return 0
    normalized_domain = domain.strip().lstrip("@").strip()
    if not normalized_domain:
        return 0
    pattern = "@" + re.escape(normalized_domain) + r"(?![\w.-])"
    return 1 if re.search(pattern, recipient_text, re.IGNORECASE) else 0


@dataclass(frozen=True, slots=True)
class PersistResult:
    inserted: bool
    stored_message_id: int
    recovered_attachments: int = 0


def resolve_database_default(explicit_path: str | Path | None = None) -> Path:
    """Return the canonical database path using explicit/env/default precedence."""
    if explicit_path is not None:
        return Path(explicit_path)
    env_val = os.environ.get(ENV_VAR_DB, "")
    if env_val.strip():
        return Path(env_val)
    return Path(DEFAULT_DB_NAME)


def connect_database(
    database_path: str | Path,
    *,
    create: bool = True,
    read_only: bool = False,
) -> sqlite3.Connection:
    path = Path(database_path).expanduser().resolve()
    if not read_only and create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise FileNotFoundError(f"Database file was not found: {path}")
    if read_only:
        # Percent-encode the path so URI-special chars legal in filenames
        # (e.g. '#', '?', '%') cannot break the '?mode=ro' query segment and
        # silently drop the read-only guarantee. Keep '/' and ':' (drive letter).
        uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only = ON;")
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    # Every recipient-domain query path (query.py, contact_history.py) routes
    # through this single factory and relies on this function existing on
    # the connection. A caller opening a raw sqlite3.connect() bypassing
    # connect_database would fail loud with "no such function:
    # recipient_domain_match" rather than silently falling back to a wrong
    # match -- see design.md > "Failure modes".
    connection.create_function(
        "recipient_domain_match", 2, _recipient_domain_match, deterministic=True
    )
    return connection


def initialize_database(database_path: str | Path) -> Path:
    path = Path(database_path).expanduser().resolve()
    connection = connect_database(path)
    try:
        # SQLite silently falls back to the existing journal mode if WAL is
        # refused (read-only FS, incompatible storage). Verify the returned
        # mode is `wal`; surface as RuntimeError per spec contract.
        result = connection.execute("PRAGMA journal_mode=WAL;").fetchone()
        actual_mode = result[0] if result else None
        if actual_mode is None or str(actual_mode).lower() != "wal":
            raise RuntimeError(
                f"SQLite refused journal_mode=WAL on {path}; "
                f"actual mode = {actual_mode!r}"
            )
        connection.executescript(SCHEMA_SQL)
        _ensure_message_schema(connection)
        connection.commit()
    finally:
        connection.close()
    return path


def persist_message(
    connection: sqlite3.Connection,
    message: NormalizedMessage,
) -> PersistResult:
    # Design: atomicity for the thread_key merge + insert is provided by the
    # caller's SAVEPOINT (ingest.py._persist_extraction_results).  We only set
    # busy_timeout here; no nested transaction management in this function.
    connection.execute("PRAGMA busy_timeout = 5000;")

    existing_row = connection.execute(
        """
        SELECT id
        FROM messages
        WHERE (? IS NOT NULL AND message_id = ?)
           OR dedup_key = ?
        LIMIT 1
        """,
        [message.message_id, message.message_id, message.dedup_key],
    ).fetchone()
    if existing_row is not None:
        stored_id = int(existing_row["id"])
        return PersistResult(
            inserted=False,
            stored_message_id=stored_id,
            recovered_attachments=_backfill_empty_attachments(
                connection, stored_id, message
            ),
        )

    cursor = connection.execute(
        """
        INSERT INTO messages (
            subject,
            sender_name,
            sender_email,
            sent_at,
            received_at,
            body_text,
            message_id,
            in_reply_to,
            references_header,
            pst_source,
            folder_path,
            logical_locator,
            source_input_mode,
            source_requested_from,
            dedup_key,
            dedup_basis,
            recipient_search_text,
            attachment_search_text,
            raw_headers
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            message.subject,
            message.sender.display_name,
            message.sender.email,
            message.sent_at.isoformat() if message.sent_at else None,
            message.received_at.isoformat() if message.received_at else None,
            message.body_text,
            message.message_id,
            message.in_reply_to,
            message.references,
            message.pst_source,
            message.folder_path,
            message.logical_locator,
            message.source_input_mode,
            message.source_requested_from,
            message.dedup_key,
            message.dedup_basis,
            message.recipient_search_text,
            message.attachment_search_text,
            message.raw_headers,  # None → NULL; never write empty string
        ],
    )
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return a message row id after insert.")
    stored_message_id = int(cursor.lastrowid)

    for recipient_type, recipients in (
        ("to", message.to_recipients),
        ("cc", message.cc_recipients),
        ("bcc", message.bcc_recipients),
    ):
        for recipient in recipients:
            connection.execute(
                """
                INSERT INTO message_recipients (
                    message_id,
                    recipient_type,
                    display_name,
                    email
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    stored_message_id,
                    recipient_type,
                    recipient.display_name,
                    recipient.email,
                ],
            )

    for attachment in message.attachments:
        connection.execute(
            """
            INSERT INTO message_attachments (
                message_id,
                attachment_name,
                content_hash,
                size_bytes,
                mime_type,
                storage_path
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                stored_message_id,
                attachment.attachment_name,
                attachment.content_hash,
                attachment.size_bytes,
                attachment.mime_type,
                attachment.storage_path,
            ],
        )

    # Apply CJK segmentation to FTS fields so that CJK subword queries work
    # with unicode61 tokenizer (design.md: "保持 unicode61、只切分索引內容").
    # Only subject / recipients / body_text / attachment_names are segmented;
    # sender is intentionally NOT segmented (task 2.1 contract).
    # The messages table original text is NOT modified.
    connection.execute(
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
            stored_message_id,
            stored_message_id,
            segment_cjk(message.subject),
            message.sender.search_text,
            segment_cjk(message.recipient_search_text),
            segment_cjk(message.body_text),
            segment_cjk(message.attachment_search_text),
        ],
    )

    # Assign thread_key for the new message (design.md Decision 4).
    # Late-bridging: if refs span >=2 existing components, rewrites smaller
    # component(s) to canonical (min db_id). Atomicity guaranteed by caller's
    # SAVEPOINT in ingest.py._persist_extraction_results.
    _assign_thread_key_for_new_message(
        connection,
        new_db_id=stored_message_id,
        message_id=message.message_id,
        in_reply_to=message.in_reply_to,
        references_header=message.references,
        thread_bridge_target=None,
    )

    return PersistResult(inserted=True, stored_message_id=stored_message_id)


def record_ingest_failure(
    connection: sqlite3.Connection,
    failure: IngestFailure,
) -> None:
    connection.execute(
        """
        INSERT INTO ingest_failures (
            pst_source,
            folder_path,
            logical_locator,
            source_input_mode,
            source_requested_from,
            error_stage,
            error_type,
            error_message,
            attachment_name,
            attachment_size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            failure.pst_source,
            failure.folder_path,
            failure.logical_locator,
            failure.source_input_mode,
            failure.source_requested_from,
            failure.error_stage,
            failure.error_type,
            failure.error_message,
            failure.attachment_name,
            failure.attachment_size,
        ],
    )


def _backfill_empty_attachments(
    connection: sqlite3.Connection,
    stored_message_id: int,
    message: NormalizedMessage,
) -> int:
    """#255: fill an existing message's empty attachment rows from a fresh extraction.

    Same-name attachments are matched POSITIONALLY: the i-th fresh attachment
    named N belongs to the i-th existing row named N (by id); if the counts
    differ nothing is filled for N. Row i is filled only when ALL hold: it is
    empty (no hash, no path), fresh attachment i was stored, and an open
    ``attachment-*`` ledger entry of the message's own source records the same
    name AND size. Ledger entries (kept, ``resolved_at`` set) resolve per
    (name, size), only once every fresh attachment of that name+size sits in a
    complete row. Complete rows, legacy placeholders without a ledger entry and
    mismatches stay byte-for-byte unchanged (#22 first-arrival).
    """
    existing = connection.execute(
        "SELECT pst_source, logical_locator FROM messages WHERE id = ?",
        [stored_message_id],
    ).fetchone()
    fresh_by_name: dict[str, list] = {}
    for attachment in message.attachments:
        fresh_by_name.setdefault(attachment.attachment_name, []).append(attachment)
    filled = 0
    for name, fresh in fresh_by_name.items():
        rows = connection.execute(
            "SELECT id, content_hash, storage_path FROM message_attachments"
            " WHERE message_id = ? AND attachment_name = ? ORDER BY id",
            [stored_message_id, name],
        ).fetchall()
        if len(rows) != len(fresh):
            continue
        filled_sizes: set[int] = set()
        for attachment, row in zip(fresh, rows, strict=True):
            if (
                not attachment.content_hash
                or not attachment.storage_path
                or attachment.size_bytes is None
                or row["content_hash"] is not None
                or row["storage_path"] is not None
            ):
                continue
            has_open_entry = connection.execute(
                """
                SELECT 1 FROM ingest_failures
                WHERE resolved_at IS NULL
                  AND error_stage IN ('attachment-store', 'attachment-decode')
                  AND pst_source = ? AND logical_locator IS ?
                  AND attachment_name = ? AND attachment_size = ?
                LIMIT 1
                """,
                [
                    existing["pst_source"],
                    existing["logical_locator"],
                    name,
                    attachment.size_bytes,
                ],
            ).fetchone()
            if has_open_entry is None:
                continue
            connection.execute(
                """
                UPDATE message_attachments
                SET content_hash = ?, size_bytes = ?, mime_type = ?, storage_path = ?
                WHERE id = ?
                """,
                [
                    attachment.content_hash,
                    attachment.size_bytes,
                    attachment.mime_type,
                    attachment.storage_path,
                    row["id"],
                ],
            )
            filled += 1
            filled_sizes.add(attachment.size_bytes)
        if not filled_sizes:
            continue
        complete = {
            r["id"]
            for r in connection.execute(
                "SELECT id FROM message_attachments WHERE message_id = ?"
                " AND attachment_name = ? AND content_hash IS NOT NULL",
                [stored_message_id, name],
            )
        }
        for size in filled_sizes:
            if all(
                row["id"] in complete
                for attachment, row in zip(fresh, rows, strict=True)
                if attachment.size_bytes == size
            ):
                connection.execute(
                    """
                    UPDATE ingest_failures SET resolved_at = CURRENT_TIMESTAMP
                    WHERE resolved_at IS NULL
                      AND error_stage IN ('attachment-store', 'attachment-decode')
                      AND pst_source = ? AND logical_locator IS ?
                      AND attachment_name = ? AND attachment_size = ?
                    """,
                    [existing["pst_source"], existing["logical_locator"], name, size],
                )
    return filled


def record_sync_run_start(
    connection: sqlite3.Connection, *, started_at: datetime
) -> None:
    """Open (or replace) the single sync_state row with the running outcome.

    A second call overwrites the previous row rather than accumulating a
    history (design.md: "Persist one row of sync state, not a run history
    table"). ``ended_at`` is reset to NULL so a fresh run is never seen
    carrying a stale end timestamp from a prior run.
    """
    connection.execute(
        """
        INSERT INTO sync_state (sync_key, started_at, ended_at, outcome)
        VALUES (?, ?, NULL, 'running')
        ON CONFLICT(sync_key) DO UPDATE SET
            started_at = excluded.started_at,
            ended_at = NULL,
            outcome = 'running'
        """,
        [SYNC_STATE_KEY, started_at.isoformat()],
    )
    connection.commit()


def record_sync_run_end(
    connection: sqlite3.Connection, *, ended_at: datetime, outcome: str
) -> None:
    """Stamp the sync_state row's end timestamp and terminal outcome.

    ``outcome`` must be ``completed`` or ``failed`` -- the terminal values
    per the imap-sync spec; ``running`` is only ever written by
    ``record_sync_run_start``.
    """
    if outcome not in ("completed", "failed"):
        raise ValueError(
            "record_sync_run_end outcome must be 'completed' or 'failed', "
            f"got {outcome!r}"
        )
    connection.execute(
        "UPDATE sync_state SET ended_at = ?, outcome = ? WHERE sync_key = ?",
        [ended_at.isoformat(), outcome, SYNC_STATE_KEY],
    )
    connection.commit()


def get_imap_progress(
    connection: sqlite3.Connection,
    account_name: str,
    folder: str,
    delimiter: str,
    uidvalidity: int,
) -> int | None:
    row = connection.execute(
        "SELECT last_uid FROM imap_sync_progress "
        "WHERE account_name = ? AND folder = ? AND delimiter = ? AND uidvalidity = ?",
        [account_name, folder, delimiter, uidvalidity],
    ).fetchone()
    return None if row is None else int(row[0])


def set_imap_progress(
    connection: sqlite3.Connection,
    account_name: str,
    folder: str,
    delimiter: str,
    uidvalidity: int,
    last_uid: int,
) -> None:
    """Upsert progress; the caller's commit makes it durable with the message."""
    connection.execute(
        "INSERT OR REPLACE INTO imap_sync_progress "
        "(account_name, folder, delimiter, uidvalidity, last_uid) "
        "VALUES (?, ?, ?, ?, ?)",
        [account_name, folder, delimiter, uidvalidity, last_uid],
    )


def _ensure_message_schema(connection: sqlite3.Connection) -> None:
    message_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(messages)")
    }
    if "dedup_basis" not in message_columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN dedup_basis TEXT NOT NULL DEFAULT ''"
        )
    if "source_input_mode" not in message_columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN source_input_mode TEXT NOT NULL DEFAULT ''"
        )
    if "source_requested_from" not in message_columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN "
            "source_requested_from TEXT NOT NULL DEFAULT ''"
        )

    failure_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(ingest_failures)")
    }
    if "source_input_mode" not in failure_columns:
        connection.execute(
            "ALTER TABLE ingest_failures ADD COLUMN "
            "source_input_mode TEXT NOT NULL DEFAULT ''"
        )
    if "source_requested_from" not in failure_columns:
        connection.execute(
            "ALTER TABLE ingest_failures ADD COLUMN "
            "source_requested_from TEXT NOT NULL DEFAULT ''"
        )
    # #255: attachment sub-failure ledger. resolved_at NULL = still open.
    for column, ddl in (
        ("attachment_name", "TEXT"),
        ("attachment_size", "INTEGER"),
        ("resolved_at", "TEXT"),
        # #256: set together with resolved_at when a human abandons the entry.
        ("abandon_reason", "TEXT"),
    ):
        if column not in failure_columns:
            connection.execute(f"ALTER TABLE ingest_failures ADD COLUMN {column} {ddl}")

    attachment_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(message_attachments)")
    }
    if "content_hash" not in attachment_columns:
        connection.execute(
            "ALTER TABLE message_attachments ADD COLUMN content_hash TEXT"
        )
    if "size_bytes" not in attachment_columns:
        connection.execute(
            "ALTER TABLE message_attachments ADD COLUMN size_bytes INTEGER"
        )
    if "mime_type" not in attachment_columns:
        connection.execute("ALTER TABLE message_attachments ADD COLUMN mime_type TEXT")
    if "storage_path" not in attachment_columns:
        connection.execute(
            "ALTER TABLE message_attachments ADD COLUMN storage_path TEXT"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_attachments_hash "
        "ON message_attachments(content_hash)"
    )
    if "thread_key" not in message_columns:
        connection.execute("ALTER TABLE messages ADD COLUMN thread_key INTEGER")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_thread_key ON messages(thread_key)"
    )
    if "thread_bridge_target" not in message_columns:
        connection.execute("ALTER TABLE messages ADD COLUMN thread_bridge_target TEXT")
    if "thread_bridge_source" not in message_columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN thread_bridge_source TEXT "
            "CHECK (thread_bridge_source IS NULL "
            "OR thread_bridge_source = 'subject_heuristic')"
        )
    if "raw_headers" not in message_columns:
        connection.execute("ALTER TABLE messages ADD COLUMN raw_headers TEXT")


# Batch size for the per-insert scanning query in
# _assign_thread_key_for_new_message() (#191 perf rework). Each candidate
# consumes 3 bound parameters and one OR group; at this batch size a single
# scanning statement uses 1501 bound parameters (under the 32,766-variable
# limit) and 500 OR groups (SQLite's expression-depth limit of 1000 rejects
# more than 996 groups, measured on the bundled SQLite 3.53.1). Typical and all
# observed real References headers (even the long ones) fit in one batch and therefore
# issue exactly one scanning statement per insert; no candidate id is ever
# dropped -- a header with more than 500 distinct ids just issues one
# additional scanning statement per extra batch. Do not raise this past the
# expression-depth limit above; test_full_size_scan_batch_stays_within_sqlite_
# expression_limits guards it.
_INCREMENTAL_REF_SCAN_BATCH_SIZE = 500


def _assign_thread_key_for_new_message(
    connection: sqlite3.Connection,
    new_db_id: int,
    message_id: str | None,
    in_reply_to: str | None,
    references_header: str | None,
    thread_bridge_target: str | None = None,
) -> int:
    """Assign thread_key to a newly inserted message inside the active transaction.

    Called within the caller's transaction (SAVEPOINT in ingest.py).
    Returns the thread_key assigned to the new message.

    Algorithm (design.md Decision 4):
    1. Parse in_reply_to + references_header to collect referenced message-ids.
    2. Look up which existing rows have those message-ids (index-only, one
       query per id -- unique index on message_id) OR themselves reference the
       same id (sibling rows sharing an ancestor absent from the DB) OR
       reference the new message's own id (descendants that arrived first).
       This mirrors compute_thread_keys(), which always creates a union-find
       node for a referenced id even when no row for it exists yet, so
       siblings of an absent ancestor still merge regardless of insert order,
       and reads their thread_keys (#191). If a bridge target is already
       recorded, resolve it by message_id/dedup_key and include that target
       component.
    3. If no existing component: singleton, thread_key = new_db_id.
    4. If one existing component: join it, thread_key = that component's canonical key.
    5. If >=2 existing components (late-bridging): pick canonical = min db_id across
       all, rewrite non-canonical component rows to canonical, then set new row.

    Query cost (#191): message_id lookups stay index-only, one per referenced
    id; every in_reply_to / references_header match is folded into one
    scanning statement per batch -- see _INCREMENTAL_REF_SCAN_BATCH_SIZE.

    references_header lookups use a bracket-free LIKE prefilter (superset of
    both bracketed and bare token forms) verified against the canonical
    _tokenize_references() parser, so bracket-free References values (#191)
    are found without the LIKE substring match producing false positives.
    """
    from .query import _escape_like, _tokenize_references  # noqa: PLC0415

    def _row_actually_references(row: sqlite3.Row, target_id: str) -> bool:
        """Confirm a LIKE-prefiltered candidate genuinely references target_id."""
        row_mid = (row["message_id"] or "").strip("<>").strip()
        if row_mid == target_id:
            return True
        row_irt = (row["in_reply_to"] or "").strip("<>").strip()
        if row_irt == target_id:
            return True
        return any(
            tok.strip("<>").strip() == target_id
            for tok in _tokenize_references(row["references_header"])
        )

    new_mid = message_id.strip("<>").strip() if message_id else None

    ref_ids: list[str] = []
    if in_reply_to:
        stripped = in_reply_to.strip("<>").strip()
        if stripped:
            ref_ids.append(stripped)
    for token in _tokenize_references(references_header):
        stripped = token.strip("<>").strip()
        if stripped:
            ref_ids.append(stripped)
    unique_ref_ids = list(dict.fromkeys(ref_ids))  # dedupe, preserve order

    existing_keys: set[int] = set()

    def _add_existing_key(row: sqlite3.Row) -> None:
        thread_key = row["thread_key"]
        existing_keys.add(int(thread_key) if thread_key is not None else int(row["id"]))

    # Step 1: ancestor-row lookup, index-only (unique index on message_id).
    # One query per referenced id, but each is a direct index seek, not a scan.
    for ref_id in unique_ref_ids:
        rows = connection.execute(
            "SELECT id, thread_key FROM messages"
            " WHERE id != ? AND (message_id = ? OR message_id = ?)",
            [new_db_id, ref_id, f"<{ref_id}>"],
        ).fetchall()
        for row in rows:
            _add_existing_key(row)

    if thread_bridge_target:
        target = thread_bridge_target.strip()
        stripped_target = target.strip("<>").strip()
        bridge_rows = connection.execute(
            """
            SELECT id, thread_key
            FROM messages
            WHERE id != ?
              AND (
                    message_id = ?
                 OR message_id = ?
                 OR dedup_key = ?
              )
            """,
            [new_db_id, stripped_target, f"<{stripped_target}>", target],
        ).fetchall()
        for row in bridge_rows:
            _add_existing_key(row)

    # Step 2: the scanning query per insert (#191 perf rework). Folds two
    # roles into one query, classified in Python:
    #   - sibling: an existing row references the same (possibly absent)
    #     ancestor as this new message (candidate in unique_ref_ids)
    #   - descendant: an existing row references this new message's own id
    #     (candidate == new_mid)
    # Batching rationale: see _INCREMENTAL_REF_SCAN_BATCH_SIZE.
    scan_candidates = list(
        dict.fromkeys(([new_mid] if new_mid else []) + unique_ref_ids)
    )
    ref_id_set = set(unique_ref_ids)

    for batch_start in range(0, len(scan_candidates), _INCREMENTAL_REF_SCAN_BATCH_SIZE):
        batch = scan_candidates[
            batch_start : batch_start + _INCREMENTAL_REF_SCAN_BATCH_SIZE
        ]
        conditions: list[str] = []
        params: list[object] = [new_db_id]
        for candidate in batch:
            conditions.append(
                "(in_reply_to = ? OR in_reply_to = ?"
                " OR references_header LIKE ? ESCAPE '\\')"
            )
            params.extend([candidate, f"<{candidate}>", f"%{_escape_like(candidate)}%"])
        scan_rows = connection.execute(
            "SELECT id, thread_key, message_id, in_reply_to, references_header"
            " FROM messages WHERE id != ? AND (" + " OR ".join(conditions) + ")",
            params,
        ).fetchall()

        for row in scan_rows:
            for candidate in batch:
                if not _row_actually_references(row, candidate):
                    continue
                if candidate in ref_id_set:
                    _add_existing_key(row)
                if new_mid is not None and candidate == new_mid:
                    thread_key = row["thread_key"]
                    if thread_key is not None:
                        existing_keys.add(int(thread_key))

    if not existing_keys:
        # Singleton: no known refs in DB yet.
        thread_key = new_db_id
    else:
        thread_key = min(existing_keys)
        if len(existing_keys) > 1:
            # Late-bridging: refs span >=2 existing components.
            # Rewrite non-canonical component(s) to canonical (min db_id).
            for old_key in sorted(existing_keys - {thread_key}):
                connection.execute(
                    "UPDATE messages SET thread_key = ? WHERE thread_key = ?",
                    [thread_key, old_key],
                )

    connection.execute(
        "UPDATE messages SET thread_key = ? WHERE id = ?",
        [thread_key, new_db_id],
    )
    return thread_key

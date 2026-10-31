from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .discovery import discover_pst_sources
from .models import (
    ImportSummary,
    IngestFailure,
    MaildirSource,
    PstSource,
    build_normalized_message,
)
from .mutf7 import decode_mutf7
from .outlook_pst import (
    OutlookComReader,
    OutlookPstParser,
    OutlookReader,
    PstExtractionAccount,
    write_pst_extraction_summary,
)
from .parser import MaildirParser, ReadpstParser, _parse_maildir_message
from .quarantine import release_quarantined_mail
from .storage import (
    close_self_healed_failures,
    connect_database,
    persist_message,
    record_ingest_failure,
    stored_dedup_keys_at_locator,
)


def resolve_ingestion_sources(
    directory_inputs: Sequence[str | Path] | None = None,
    file_inputs: Sequence[str | Path] | None = None,
) -> tuple[PstSource, ...]:
    return tuple(
        discover_pst_sources(
            directory_inputs=directory_inputs,
            file_inputs=file_inputs,
        )
    )


def import_pst_sources(
    *,
    database_path: str | Path,
    sources: Sequence[PstSource],
    backend: Literal["outlook", "readpst"] = "outlook",
    readpst_path: str | Path | None = None,
    work_root: str | Path | None = None,
    parallel_jobs: int | None = None,
    attachment_root: Path | None = None,
    reader: OutlookReader | None = None,
    summary_root: Path | None = None,
    run_id: str | None = None,
) -> ImportSummary:
    if backend == "readpst":
        return _import_pst_sources_with_readpst(
            database_path=database_path,
            sources=sources,
            readpst_path=readpst_path,
            work_root=work_root,
            parallel_jobs=parallel_jobs,
            attachment_root=attachment_root,
        )
    summary = ImportSummary(discovered_psts=len(sources))
    parser = OutlookPstParser(
        reader=reader or OutlookComReader(attachment_root=attachment_root)
    )
    extraction_accounts: list[PstExtractionAccount] = []
    connection = connect_database(database_path)

    try:
        for source in sources:
            account, extraction_results = parser.extract(source)
            try:
                _persist_extraction_results(
                    connection=connection,
                    summary=summary,
                    source=source,
                    extraction_results=extraction_results,
                )
                connection.commit()
            except (OSError, RuntimeError, ValueError) as exc:
                _record_pst_extraction_failure(
                    connection=connection,
                    summary=summary,
                    source=source,
                    exc=exc,
                )
            finally:
                extraction_accounts.append(account)
    finally:
        connection.close()

    summary_path, extraction_complete = write_pst_extraction_summary(
        extraction_accounts,
        database_path=database_path,
        summary_root=summary_root,
        run_id=run_id,
    )
    summary.recipient_extraction_summary_path = str(summary_path)
    summary.recipient_extraction_complete = extraction_complete
    return summary


def _import_pst_sources_with_readpst(
    *,
    database_path: str | Path,
    sources: Sequence[PstSource],
    readpst_path: str | Path | None,
    work_root: str | Path | None,
    parallel_jobs: int | None,
    attachment_root: Path | None,
) -> ImportSummary:
    """Readpst extraction path: emergency-only, produces known-bad recipients.

    Three things a future reader needs before selecting this backend (issue
    #184, kept deliberately):

    1. No production caller. `bes rebuild` was the last one and moved to the
       Outlook backend in #180; nothing in `src/` or `scripts/` selects
       `backend="readpst"` any more -- only tests do.
    2. **Its recipients are known broken.** readpst reads `PR_DISPLAY_TO`,
       which yields packed blobs, display names without addresses, or nothing
       at all. Repairing exactly that damage is what #175 spent a full rebuild
       on; running this path over a repaired database re-introduces it.
    3. It exists for one situation only: a PST in hand and Outlook
       unavailable. Treat its output as a stopgap and re-run the Outlook
       extraction once Outlook is usable again.

    For a mail source that is not a PST, `import_maildir_sources` needs
    neither Outlook nor readpst and does not carry this defect.
    """
    summary = ImportSummary(discovered_psts=len(sources))
    work_root_path = _resolve_work_root(database_path, work_root=work_root)
    work_root_path.mkdir(parents=True, exist_ok=True)
    parser = ReadpstParser(
        readpst_path=readpst_path,
        work_root=work_root_path,
        parallel_jobs=parallel_jobs,
        attachment_root=attachment_root,
    )
    connection = connect_database(database_path)
    try:
        for source in sources:
            try:
                _persist_extraction_results(
                    connection=connection,
                    summary=summary,
                    source=source,
                    extraction_results=parser.iter_extraction_results(source),
                )
                connection.commit()
            except (OSError, RuntimeError, ValueError) as exc:
                _record_pst_extraction_failure(
                    connection=connection,
                    summary=summary,
                    source=source,
                    exc=exc,
                )
    finally:
        connection.close()
    return summary


def _record_pst_extraction_failure(
    *,
    connection: sqlite3.Connection,
    summary: ImportSummary,
    source: PstSource,
    exc: Exception,
) -> None:
    print(f"{source.source_path}: {exc}", file=sys.stderr)
    record_ingest_failure(
        connection,
        IngestFailure.from_source(
            source,
            error_stage="extract-pst",
            error_type=type(exc).__name__,
            error_message=str(exc),
        ),
    )
    connection.commit()
    summary.failure_count += 1


def resolve_maildir_sources(
    *,
    maildir_root: str | Path,
    account_email: str,
    account_name: str,
    folder: str | None = None,
) -> tuple[MaildirSource, ...]:
    account_root = Path(maildir_root).expanduser().resolve() / account_name
    if folder is not None:
        folder_names = [folder]
    elif account_root.exists():
        folder_names = sorted(
            path.name
            for path in account_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
    else:
        folder_names = []

    return tuple(
        MaildirSource(
            path=(account_root / folder_name).resolve(),
            account_email=account_email,
            account_name=account_name,
            folder_name=decode_mutf7(folder_name),
            requested_from=account_root,
        )
        for folder_name in folder_names
    )


def import_maildir_sources(
    *,
    database_path: str | Path,
    sources: Sequence[MaildirSource],
    attachment_root: Path | None = None,
) -> ImportSummary:
    summary = ImportSummary()
    connection = connect_database(database_path)
    parser = MaildirParser()

    try:
        for source in sources:
            try:
                _persist_extraction_results(
                    connection=connection,
                    summary=summary,
                    source=source,
                    extraction_results=parser.iter_extraction_results(
                        source, attachment_root=attachment_root
                    ),
                )
                connection.commit()
            except (OSError, RuntimeError, ValueError) as exc:
                record_ingest_failure(
                    connection,
                    IngestFailure.from_source(
                        source,
                        folder_path=source.folder_name,
                        error_stage="read-maildir",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    ),
                )
                connection.commit()
                summary.failure_count += 1
        try:
            close_self_healed_failures(connection)
        except (sqlite3.Error, OSError) as exc:
            print(
                "self-healed failures sweep failed (swallowed): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    finally:
        connection.close()

    return summary


def import_maildir_message(
    *,
    connection: sqlite3.Connection,
    source: MaildirSource,
    message_path: str | Path,
    attachment_root: Path | None = None,
    record_attachment_failures: bool = True,
) -> ImportSummary:
    summary = ImportSummary()
    _persist_extraction_results(
        connection=connection,
        summary=summary,
        source=source,
        record_attachment_failures=record_attachment_failures,
        extraction_results=[
            _parse_maildir_message(
                source=source,
                message_path=message_path,
                attachment_root=attachment_root,
            )
        ],
    )
    return summary


def _resolve_work_root(
    database_path: str | Path,
    *,
    work_root: str | Path | None = None,
) -> Path:
    if work_root is not None:
        return Path(work_root).expanduser().resolve()
    return Path(database_path).expanduser().resolve().parent / ".mailattic-work"


def _persist_extraction_results(
    *,
    connection,
    summary: ImportSummary,
    source,
    extraction_results,
    record_attachment_failures: bool = True,
) -> None:
    for extraction_result in extraction_results:
        if extraction_result.failure is not None:
            record_ingest_failure(connection, extraction_result.failure)
            summary.failure_count += 1
            continue

        if extraction_result.message is None:
            continue

        try:
            normalized_message = build_normalized_message(
                extraction_result.message.raw_message,
                source=source,
                folder_path=extraction_result.message.folder_path,
                logical_locator=extraction_result.message.logical_locator,
            )
        except Exception as exc:
            record_ingest_failure(
                connection,
                IngestFailure.from_source(
                    source,
                    folder_path=extraction_result.message.folder_path,
                    logical_locator=extraction_result.message.logical_locator,
                    error_stage="normalize-message",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                ),
            )
            summary.failure_count += 1
            continue

        if record_attachment_failures:
            _record_attachment_failures(
                connection=connection,
                summary=summary,
                source=source,
                extraction_result=extraction_result,
            )

        connection.execute("SAVEPOINT persist_message")
        try:
            persist_result = persist_message(connection, normalized_message)
        except Exception as exc:
            connection.execute("ROLLBACK TO SAVEPOINT persist_message")
            connection.execute("RELEASE SAVEPOINT persist_message")
            record_ingest_failure(
                connection,
                IngestFailure.from_source(
                    source,
                    folder_path=extraction_result.message.folder_path,
                    logical_locator=extraction_result.message.logical_locator,
                    error_stage="persist-message",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                ),
            )
            summary.failure_count += 1
            continue
        connection.execute("RELEASE SAVEPOINT persist_message")
        if persist_result.inserted:
            summary.imported_messages += 1
        else:
            summary.duplicate_messages += 1
            summary.recovered_attachments += persist_result.recovered_attachments


def _record_attachment_failures(
    *,
    connection,
    summary: ImportSummary,
    source,
    extraction_result,
) -> None:
    collection_error = extraction_result.message.raw_message.get(
        "attachment_collection_error"
    )
    if collection_error:
        record_ingest_failure(
            connection,
            IngestFailure.from_source(
                source,
                folder_path=extraction_result.message.folder_path,
                logical_locator=extraction_result.message.logical_locator,
                error_stage="attachment-collection-read",
                error_type="AttachmentCollectionError",
                error_message=f"Attachment collection unreadable: {collection_error}",
            ),
        )
        summary.failure_count += 1
    raw_attachments = extraction_result.message.raw_message.get("attachments") or []
    for attachment in raw_attachments:
        if not isinstance(attachment, dict):
            continue
        error_stage = None
        if attachment.get("_decode_error"):
            error_stage = "attachment-decode"
        elif attachment.get("_store_error"):
            error_stage = "attachment-store"
        if error_stage is None:
            continue
        record_ingest_failure(
            connection,
            IngestFailure.from_source(
                source,
                folder_path=extraction_result.message.folder_path,
                logical_locator=extraction_result.message.logical_locator,
                error_stage=error_stage,
                error_type="AttachmentError",
                error_message=(
                    "Attachment binary failure for: "
                    f"{attachment.get('attachment_name', 'unknown')}"
                ),
                attachment_name=attachment.get("attachment_name"),
                attachment_size=attachment.get("_expected_size"),
            ),
        )
        summary.failure_count += 1


@dataclass(frozen=True, slots=True)
class AttachmentRetryResult:
    recovered: int = 0
    still_failing: int = 0
    source_missing: int = 0


def retry_attachment_failures(
    *,
    connection: sqlite3.Connection,
    maildir_root: Path,
    account_name: str,
    account_email: str,
    attachment_root: Path | None,
    quarantine_root: Path | None = None,
) -> AttachmentRetryResult:
    """#255: re-extract every open attachment-failure ledger entry from the local mail.

    Local files only, no network. #256: a mail missing from staging is looked up
    in ``quarantine_root`` too, and a quarantined file at that locator is handed to
    ``release_quarantined_mail`` after each retried mail. Each distinct
    (source, locator, name, size) with an open entry counts once as recovered /
    still failing / source missing.
    A retry that fails again records nothing new and never raises, so the
    caller's watermark and exit code are untouched (contract #5).
    """
    if attachment_root is None:
        return AttachmentRetryResult()
    account_root = Path(maildir_root).expanduser().resolve() / account_name
    folder_dirs = _local_folder_dirs(account_root)
    quarantine_dirs = (
        _local_folder_dirs(Path(quarantine_root).expanduser().resolve() / account_name)
        if quarantine_root is not None
        else {}
    )
    prefix = f"imap:{account_email}:"
    entries = connection.execute(
        """
        SELECT DISTINCT pst_source, folder_path, logical_locator,
               attachment_name, attachment_size
        FROM ingest_failures
        WHERE resolved_at IS NULL
          AND error_stage IN ('attachment-store', 'attachment-decode')
          AND source_input_mode = 'maildir'
          AND attachment_name IS NOT NULL
          AND substr(pst_source, 1, ?) = ?
        """,
        [len(prefix), prefix],
    ).fetchall()
    by_mail: dict[tuple, int] = {}
    for entry in entries:
        key = (entry["pst_source"], entry["folder_path"], entry["logical_locator"])
        by_mail[key] = by_mail.get(key, 0) + 1

    recovered = still_failing = missing = 0
    for (pst_source, folder_path, locator), count in by_mail.items():
        relative = (locator or "")[len(folder_path or "") + 1 :]
        message_path = folder_dir = source = None
        unproven = False
        # staging first; #256 quarantine second. #17: a file at the locator only
        # counts when it is the stored mail of that locator (a reused UID or a
        # pruned name can leave another mail there); otherwise it is "missing".
        for root_dirs in (folder_dirs, quarantine_dirs):
            candidate_dir = root_dirs.get(folder_path or "")
            if not (
                candidate_dir and relative and (candidate_dir / relative).is_file()
            ):
                continue
            candidate_source = MaildirSource(
                path=candidate_dir,
                account_email=account_email,
                account_name=account_name,
                folder_name=folder_path or "",
                requested_from=account_root,
            )
            if _matches_stored_dedup_key(
                connection,
                candidate_source,
                candidate_dir / relative,
                pst_source,
                locator,
            ):
                folder_dir, message_path = candidate_dir, candidate_dir / relative
                source = candidate_source
                break
            unproven = True
        if message_path is None or folder_dir is None or source is None:
            # a file exists but is not provably this mail: still owed, not missing
            if unproven:
                still_failing += count
            else:
                missing += count
            continue
        try:
            summary = import_maildir_message(
                connection=connection,
                source=source,
                message_path=message_path,
                attachment_root=attachment_root,
                record_attachment_failures=False,
            )
            if summary.failure_count:
                # The mail itself failed this time (parse / normalize / persist).
                # It is already imported, so drop the failure row just written
                # rather than growing the ledger by one on every sync.
                connection.rollback()
                print(
                    f"attachment retry failed for {locator}: message-stage failure",
                    file=sys.stderr,
                )
            else:
                connection.commit()
        except Exception as exc:
            connection.rollback()
            print(
                f"attachment retry failed for {locator}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        remaining = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT attachment_name, attachment_size
                FROM ingest_failures
                WHERE resolved_at IS NULL
                  AND error_stage IN ('attachment-store', 'attachment-decode')
                  AND attachment_name IS NOT NULL
                  AND pst_source = ? AND folder_path IS ? AND logical_locator IS ?
            )
            """,
            [pst_source, folder_path, locator],
        ).fetchone()[0]
        recovered += count - remaining
        still_failing += remaining
        # #256: whichever copy was read, the quarantined one is released only if
        # the hold rule lets go (release_quarantined_mail decides).
        quarantine_dir = quarantine_dirs.get(folder_path or "")
        if quarantine_dir and relative and (quarantine_dir / relative).is_file():
            try:
                release_quarantined_mail(
                    connection, quarantine_dir / relative, locator or ""
                )
            except (OSError, sqlite3.Error) as exc:
                print(
                    f"quarantine cleanup failed for {locator}: {exc}",
                    file=sys.stderr,
                )
    d_recovered, d_failing, d_missing = _retry_outbound_draft_failures(
        connection, attachment_root
    )
    return AttachmentRetryResult(
        recovered + d_recovered, still_failing + d_failing, missing + d_missing
    )


def _retry_outbound_draft_failures(
    connection: sqlite3.Connection, attachment_root: Path
) -> tuple[int, int, int]:
    """#18: repair ``outbound:<draft_id>`` ledger entries from the retained draft.

    Reads the draft .eml only (never SMTP, never the send-once marker). The draft
    must carry the Message-ID of the stored mail; otherwise it is not a provable
    source and the entry stays open. Filling goes through ``persist_message``'s
    existing-message path, i.e. the same name+size+ledger evidence as a Sent copy.
    """
    from email import message_from_bytes, policy

    from .outbound import _draft_dir, _normalized_from_draft

    db_file = connection.execute("PRAGMA database_list").fetchone()[2]
    if not db_file:
        return 0, 0, 0
    entries = connection.execute(
        """
        SELECT DISTINCT logical_locator FROM ingest_failures
        WHERE resolved_at IS NULL
          AND error_stage IN ('attachment-store', 'attachment-decode')
          AND pst_source = 'outbound-bes' AND source_input_mode = 'outbound'
          AND attachment_name IS NOT NULL
          AND logical_locator LIKE 'outbound:%'
        """
    ).fetchall()
    recovered = still_failing = missing = 0
    for (locator,) in entries:
        draft_id = locator[len("outbound:") :]
        open_sql = (
            "SELECT COUNT(*) FROM (SELECT DISTINCT attachment_name, attachment_size"
            " FROM ingest_failures WHERE resolved_at IS NULL"
            " AND error_stage IN ('attachment-store', 'attachment-decode')"
            " AND attachment_name IS NOT NULL AND pst_source = 'outbound-bes'"
            " AND logical_locator = ?)"
        )
        count = connection.execute(open_sql, [locator]).fetchone()[0]
        draft_path = _draft_dir(db_file) / f"{draft_id}.eml"
        if (
            not draft_id
            or "/" in draft_id
            or "\\" in draft_id
            or not draft_path.is_file()
        ):
            missing += count
            continue
        stored = connection.execute(
            "SELECT message_id FROM messages WHERE pst_source = 'outbound-bes'"
            " AND logical_locator = ?",
            [locator],
        ).fetchall()
        try:
            msg = message_from_bytes(draft_path.read_bytes(), policy=policy.default)
            header_id = str(msg.get("Message-ID", "")).strip()
            if len(stored) != 1 or not header_id or header_id != stored[0][0]:
                still_failing += count
                continue
            normalized, _ = _normalized_from_draft(
                msg, draft_id, header_id, db_file, attachment_root=attachment_root
            )
            persist_message(connection, normalized)
            connection.commit()
        except Exception as exc:
            connection.rollback()
            print(
                f"attachment retry failed for {locator}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        remaining = connection.execute(open_sql, [locator]).fetchone()[0]
        recovered += count - remaining
        still_failing += remaining
    return recovered, still_failing, missing


def _matches_stored_dedup_key(
    connection: sqlite3.Connection,
    source: MaildirSource,
    path: Path,
    pst_source: str,
    locator: str | None,
) -> bool:
    """#17: is *path* the mail the DB stored under (pst_source, locator)?

    Conservative: two stored mails sharing one locator (legacy reused UID) or a
    file that does not parse to the stored dedup key cannot be told apart, so
    it is not a usable repair source. No stored mail yet = nothing to confuse.
    """
    keys = stored_dedup_keys_at_locator(connection, pst_source, locator)
    if not keys:
        return True
    if len(keys) > 1:
        return False
    parsed = _parse_maildir_message(
        source=source, message_path=path, attachment_root=None
    )
    if parsed.message is None:
        return False
    try:
        fresh = build_normalized_message(
            parsed.message.raw_message,
            source=source,
            folder_path=parsed.message.folder_path,
            logical_locator=parsed.message.logical_locator,
        )
    except Exception:
        return False  # identity unprovable: fail closed
    return fresh.dedup_key == keys[0]


def _local_folder_dirs(account_root: Path) -> dict[str, Path]:
    """Map display folder name -> maildir directory, as the sync download lays it out.

    The download path stages mail in ``<account>/<wire_name>/new``; a wire name
    containing the hierarchy delimiter therefore nests. A folder directory is
    any directory holding ``new``/``cur``; its display name is the mutf7-decoded
    path relative to the account root (the inverse of how it was written).
    """
    found: dict[str, Path] = {}
    for current, dirs, _files in os.walk(account_root):
        if "new" in dirs or "cur" in dirs:
            relative = Path(current).relative_to(account_root).as_posix()
            if relative != ".":
                found[decode_mutf7(relative)] = Path(current)
            dirs[:] = [d for d in dirs if d not in {"cur", "new", "tmp"}]
    return found

from __future__ import annotations

import hashlib
import imaplib
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .imap_runtime import SyncExecutionResult, _append_sync_log, resolve_imap_security
from .imap_state import ImapSyncState
from .ingest import (
    AttachmentRetryResult,
    _local_folder_dirs,
    import_maildir_message,
    retry_attachment_failures,
)
from .models import MaildirSource, build_normalized_message
from .mutf7 import decode_mutf7, encode_mutf7
from .parser import _parse_eml_file
from .quarantine import (
    default_quarantine_root,
    is_held,
    mail_locator,
    quarantine_mail,
)
from .storage import connect_database, initialize_database, set_imap_progress
from .storage import record_sync_run_end as _storage_record_sync_run_end
from .storage import record_sync_run_start as _storage_record_sync_run_start

BULK_FETCH_THRESHOLD = 500
MAILDIR_RETENTION_DAYS = 7

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImapFolder:
    """A folder as carried through the sync path: display label + wire identity.

    Per design Decision 1 (add-mutf7-folder-codec): the wire name is the
    identity, the display name is the label.

    - ``wire_name``: the exact bytes/string the server supplied (or, for a
      folder that never came from LIST — e.g. a ``--folder`` CLI argument —
      the mUTF-7 encoding of that argument). Used for every IMAP command that
      names the mailbox, the sync-state watermark key, and the maildir
      staging directory.
    - ``display_name``: the decoded, human-readable form. Used only for
      persisted fields (``folder_path``, ``pst_source``, ``logical_locator``).
    - ``delimiter``: the hierarchy delimiter as returned by LIST.
    """

    display_name: str
    wire_name: str
    delimiter: str
    # False only for a ``--folder`` value that matched nothing in LIST (#281).
    listed: bool = field(default=True, compare=False)


class _UndecodableFolderNameError(RuntimeError):
    """A LIST entry whose folder name cannot be read; only that entry is lost."""


def skipped_listings(client: object, folder: str | None) -> list[str]:
    """Folder listings the client skipped on its latest ``list_folders`` call.

    Empty under ``--folder``: a run scoped to one folder did not ask for the
    others, so an unreadable name elsewhere is not this run's failure (a bad
    name for the requested folder itself surfaces through ``resolve_folders``).
    """
    if folder is not None:
        return []
    return list(getattr(client, "skipped_listings", []))


class _StatusFailedError(RuntimeError):
    """STATUS got a non-OK / empty reply; message carries the server response."""


def status_uidvalidity(client: NativeImapClient, folder: ImapFolder) -> int:
    """UIDVALIDITY for *folder*; explains a failure on a name LIST never showed.

    Shared by the real sync and the dry-run so both report the same error (#281).
    """
    try:
        return client.get_uidvalidity(folder.wire_name)
    except _StatusFailedError as exc:
        if folder.listed:
            raise
        leaf = folder.display_name.casefold()
        try:
            listed_folders = client.list_folders()
        except (RuntimeError, imaplib.IMAP4.error, OSError):
            # Candidates are a courtesy; losing them must not hide the STATUS reply.
            listed_folders = []
        candidates = [
            f.display_name
            for f in listed_folders
            if (
                f.display_name.rsplit(f.delimiter, 1)[-1]
                if f.delimiter
                else f.display_name
            ).casefold()
            == leaf
        ]
        message = (
            f"{exc}. --folder {folder.display_name!r} did not match any folder "
            "the server listed; use the full path exactly as listed "
            "(parent folders and the hierarchy delimiter included)"
        )
        if candidates:
            message += f". Listed folders ending in that name: {candidates!r}"
        raise RuntimeError(message) from exc


def prune_ingested_maildir(
    maildir_root: Path,
    account_name: str,
    database_path: Path,
    retention_days: int,
    now: datetime,
    *,
    account_email: str | None = None,
    quarantine_root: Path | None = None,
) -> dict[str, int]:
    """Delete stale durably-ingested .eml staging files under the account maildir.

    #256 exception: a file whose mail still has an unresolved attachment failure
    is MOVED to ``quarantine_root`` (default: ``quarantine/<db file name>`` under the
    database's ``.mailattic-work``) instead of deleted. A failed move retains the
    file, and so does a file whose folder directory cannot be resolved.

    For each .eml file under ``<maildir_root>/<account_name>/`` with an mtime
    older than ``now - retention_days`` days, read and parse the file to resolve
    its content identity (Message-ID header, or the fallback dedup_key computed
    the same way the ingestion gate computes it when Message-ID is absent/empty).
    Unlink the file ONLY when that identity is found in ``messages``
    (``message_id`` or ``dedup_key`` column).  Retain on parse failure, missing
    identity, DB lookup error, or unlink error (fail-safe: never delete on
    doubt).

    Only ``*.eml`` files are considered; structure directories, sync-state
    files, and non-``.eml`` files are left untouched.

    Args:
        maildir_root: Root maildir directory (parent of the account directory).
        account_name: Account subdirectory name.
        database_path: Path to the SQLite database (opened read-only here).
        retention_days: Consider only .eml files older than this many days.
        now: Reference datetime for age calculation (injectable for tests).

    Returns:
        dict with integer counts for keys:
        ``deleted``, ``kept_not_in_db``, ``kept_unparseable``, ``skipped_recent``.
    """
    counts: dict[str, int] = {
        "deleted": 0,
        "kept_not_in_db": 0,
        "kept_unparseable": 0,
        "skipped_recent": 0,
    }

    account_root = Path(maildir_root) / account_name
    if not account_root.exists():
        return counts

    cutoff_ts: float = (now - timedelta(days=retention_days)).timestamp()
    quarantine_root = (
        default_quarantine_root(database_path)
        if quarantine_root is None
        else Path(quarantine_root)
    )
    folder_names = {p: name for name, p in _local_folder_dirs(account_root).items()}

    conn = sqlite3.connect(str(Path(database_path).resolve()))
    try:
        for eml_path in account_root.rglob("*.eml"):
            if not eml_path.is_file():
                continue

            # --- age gate ---------------------------------------------------
            try:
                mtime_ts = eml_path.stat().st_mtime
            except OSError:
                counts["kept_unparseable"] += 1
                continue

            if mtime_ts >= cutoff_ts:
                counts["skipped_recent"] += 1
                continue

            # --- content-identity gate (D3) ----------------------------------
            try:
                raw_message = _parse_eml_file(eml_path)
                # Provide a minimal dummy source; source fields do not affect
                # message_id or dedup_key computation.
                dummy_source = MaildirSource(
                    path=account_root,
                    account_email="",
                    account_name=account_name,
                    folder_name="",
                    requested_from=account_root,
                )
                normalized = build_normalized_message(
                    raw_message,
                    source=dummy_source,
                    folder_path="",
                    logical_locator="",
                )
            except Exception:
                counts["kept_unparseable"] += 1
                continue

            message_id = normalized.message_id
            dedup_key = normalized.dedup_key

            # --- DB lookup ---------------------------------------------------
            try:
                row = conn.execute(
                    """
                    SELECT id FROM messages
                    WHERE (? IS NOT NULL AND message_id = ?)
                       OR dedup_key = ?
                    LIMIT 1
                    """,
                    [message_id, message_id, dedup_key],
                ).fetchone()
            except Exception:
                # DB read error: retain (fail-safe)
                counts["kept_not_in_db"] += 1
                continue

            if row is None:
                counts["kept_not_in_db"] += 1
            else:
                try:
                    # #256: an unresolved attachment failure makes this raw file
                    # the only local repair source -> quarantine, don't delete.
                    locator = mail_locator(eml_path, folder_names)
                    if locator is None:
                        # Cannot place it -> cannot tell whether it is held: keep.
                        counts["kept_not_in_db"] += 1
                        continue
                    if is_held(conn, locator):
                        quarantine_mail(
                            eml_path, account_root, quarantine_root / account_name
                        )
                        logger.info("maildir prune: quarantined %s", eml_path.name)
                    else:
                        eml_path.unlink()
                        counts["deleted"] += 1
                except Exception:
                    # Lookup / move / unlink error: retain (fail-safe, D4)
                    counts["kept_not_in_db"] += 1
    finally:
        conn.close()

    return counts


class _BulkGuardRefusedError(RuntimeError):
    """Raised when UIDs exceed the bulk threshold and confirm_fn does not approve."""


class _PartialFolderSyncError(RuntimeError):
    """Base for folder-sync failures that still carry partial progress counts.

    #194: any failure that aborts a folder mid-sync must still report the
    work genuinely completed before it (durably written / committed), not
    reset the folder's contribution to zero.
    """

    def __init__(
        self,
        message: str,
        *,
        pulled_messages: int,
        inserted_messages: int,
        deduplicated_messages: int,
    ) -> None:
        super().__init__(message)
        self.pulled_messages = pulled_messages
        self.inserted_messages = inserted_messages
        self.deduplicated_messages = deduplicated_messages


class _MessageIngestFailedError(_PartialFolderSyncError):
    """Raised when a fetched UID does not persist a durable message row."""

    def __init__(
        self,
        *,
        wire_name: str,
        uid: int,
        pulled_messages: int,
        inserted_messages: int,
        deduplicated_messages: int,
    ) -> None:
        super().__init__(
            f"{wire_name!r}: UID {uid} did not persist a message row",
            pulled_messages=pulled_messages,
            inserted_messages=inserted_messages,
            deduplicated_messages=deduplicated_messages,
        )


class NativeImapClient:
    """Thin IMAP4 wrapper for Windows-native sync.

    Injectable *_connection* allows mock injection in tests (avoids a real
    handshake). Production defaults to ``imaplib.IMAP4_SSL`` with the legacy
    SSL context from imap_sync (D5). A mailbox env file can explicitly opt in
    to unencrypted ``imaplib.IMAP4`` with ``IMAP_SECURITY=plain``.
    """

    def __init__(
        self,
        env: Mapping[str, str],
        _connection=None,
        timeout: float | None = None,
    ) -> None:
        """``timeout`` is the socket timeout (seconds) passed to the selected
        imaplib client. Defaults to ``None`` (blocking, no timeout) to
        keep the sync path's behavior unchanged; the best-effort
        Sent-folder APPEND path (outbound.py) passes a bounded value so a
        server that accepts the connection but never responds cannot hang
        the append -- and therefore a send or batch -- forever (Codex
        semantic review, add-mutf7-folder-codec design Decision 3).
        """
        self.skipped_listings: list[str] = []
        if _connection is not None:
            self._conn = _connection
        else:
            security = resolve_imap_security(env)
            connection_kwargs = {
                "host": env["IMAP_HOST"],
                "port": int(env["IMAP_PORT"]),
                "timeout": timeout,
            }
            if security == "plain":
                self._conn = imaplib.IMAP4(**connection_kwargs)
            else:
                from .imap_sync import _build_legacy_ssl_context

                self._conn = imaplib.IMAP4_SSL(
                    **connection_kwargs,
                    ssl_context=_build_legacy_ssl_context(),
                )
            self._conn.login(env["IMAP_USER"], env["IMAP_PASSWORD"])

    def list_folders(self) -> list[ImapFolder]:
        """Return all accessible folders as display/wire pairs (design Decision 1).

        The wire name is exactly what the server's LIST response supplied
        (mUTF-7 encoded, byte for byte). The display name is its mUTF-7
        decoding — a malformed shift sequence decodes to itself, per
        ``mutf7.decode_mutf7``.
        """
        typ, data = self._conn.list()
        if typ != "OK":
            raise RuntimeError("Unable to list IMAP folders")
        # Listing entries skipped by the latest call; read right after it.
        self.skipped_listings.clear()
        result: list[ImapFolder] = []
        for raw in data or []:
            if raw is None:
                continue
            if isinstance(raw, tuple):
                try:
                    text = _list_text_from_literal(raw)
                except _UndecodableFolderNameError as exc:
                    # #308: lose only this folder; callers report the skip.
                    self.skipped_listings.append(str(exc))
                    continue
            else:
                text = (
                    raw.decode("utf-8", errors="replace")
                    if isinstance(raw, bytes)
                    else str(raw)
                )
            parsed = _parse_list_entry(text)
            if parsed is not None:
                wire_name, delimiter = parsed
                result.append(
                    ImapFolder(
                        display_name=decode_mutf7(wire_name),
                        wire_name=wire_name,
                        delimiter=delimiter,
                    )
                )
        return result

    def get_uidvalidity(self, folder_name: str) -> int:
        """Return UIDVALIDITY via STATUS command (no SELECT needed).

        ``folder_name`` must be the wire name (design Decision 1).
        """
        quoted = _imap_quote(folder_name)
        try:
            typ, data = self._conn.status(quoted, "(UIDVALIDITY)")
        except imaplib.IMAP4.abort:
            raise  # the connection dropped; that says nothing about the folder
        except imaplib.IMAP4.error as exc:
            # imaplib raises on a BAD reply instead of returning it.
            raise _StatusFailedError(
                f"STATUS failed for {folder_name!r}: server replied {exc}"
            ) from exc
        if typ != "OK" or not data:
            raise _StatusFailedError(
                f"STATUS failed for {folder_name!r}: server replied {typ} {data!r}"
            )
        raw = data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
        m = re.search(rb"UIDVALIDITY\s+(\d+)", raw)
        if not m:
            raise RuntimeError(
                f"No UIDVALIDITY in STATUS response for {folder_name!r}: {data[0]!r}"
            )
        return int(m.group(1))

    def select_folder(self, folder_name: str) -> None:
        """Select folder for read-only UID operations.

        ``folder_name`` must be the wire name (design Decision 1).
        """
        quoted = _imap_quote(folder_name)
        typ, _ = self._conn.select(quoted, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Cannot SELECT folder {folder_name!r}")

    def search_uids_after(self, watermark: int | None) -> list[int]:
        """Return UIDs to fetch: all if watermark is None, else > watermark."""
        if watermark is None:
            typ, data = self._conn.uid("SEARCH", "ALL")
        else:
            typ, data = self._conn.uid("SEARCH", f"UID {watermark + 1}:*")
        if typ != "OK":
            raise RuntimeError("UID SEARCH failed")
        raw = data[0] if data and data[0] else b""
        uid_str = (
            raw.decode("ascii", errors="replace")
            if isinstance(raw, bytes)
            else str(raw)
        ).strip()
        if not uid_str:
            return []
        uids = sorted(int(u) for u in uid_str.split() if u.isdigit())
        # RFC 9051 section 6.4.9: a "watermark+1:*" range has its endpoints
        # reversed when watermark+1 exceeds the mailbox's current max UID, so
        # the server may legally answer with a UID at or below the watermark.
        # Trusting that answer directly re-fetches the last synced message
        # and can push the watermark backwards; filter it ourselves.
        if watermark is not None:
            uids = [u for u in uids if u > watermark]
        return uids

    def fetch_rfc822(self, uid: int) -> bytes:
        """Fetch RFC822 body using BODY.PEEK[] to avoid setting \\Seen (D5)."""
        typ, data = self._conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data:
            raise RuntimeError(f"UID FETCH failed for UID {uid}")
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                body = item[1]
                if isinstance(body, bytes):
                    return body
        raise RuntimeError(f"Could not extract RFC822 bytes for UID {uid}: {data!r}")

    def append_message(self, folder_wire_name: str, rfc822: bytes) -> None:
        """Append *rfc822* to *folder_wire_name* with the ``\\Seen`` flag set.

        ``folder_wire_name`` must be the wire name (design Decision 1).
        Raises ``RuntimeError`` on a non-OK server response; does not swallow
        failures itself -- best-effort handling is the caller's job (design
        Decision 3, add-mutf7-folder-codec).
        """
        quoted = _imap_quote(folder_wire_name)
        typ, data = self._conn.append(quoted, "(\\Seen)", None, rfc822)
        if typ != "OK":
            raise RuntimeError(f"APPEND failed for {folder_wire_name!r}: {data!r}")

    def close(self) -> None:
        import contextlib

        for method in (self._conn.close, self._conn.logout):
            with contextlib.suppress(Exception):
                method()


def _imap_quote(folder_name: str) -> str:
    """Quoted-string form of *folder_name*; inverse of ``_read_quoted``."""
    if any(c in folder_name for c in "\r\n\0"):
        raise ValueError(
            f"folder name {folder_name!r} cannot be sent as a quoted string"
        )
    escaped = folder_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _read_quoted(text: str) -> str | None:
    """Unescape an IMAP quoted string occupying all of *text* (starts with ``"``).

    ``\\\\`` and ``\\"`` are restored. Returns None for an unterminated string
    or anything after the closing quote -- never a reshaped name.
    """
    out: list[str] = []
    i = 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            if i + 1 >= len(text):
                return None
            out.append(text[i + 1])
            i += 2
        elif c == '"':
            return "".join(out) if not text[i + 1 :].strip() else None
        else:
            out.append(c)
            i += 1
    return None


def _list_text_from_literal(raw: tuple) -> str:
    """Rewrite an imaplib literal LIST entry ``(head ending ``{N}``, bytes)``
    into the equivalent quoted-string line so one parser handles both.

    imaplib hands a literal as ``(b'() "/" {7}', b'Archive')`` (followed by a
    trailing ``b''`` / ``b')'`` element, which parses to nothing).
    """
    head = raw[0].decode("utf-8", errors="replace").rstrip()
    start = head.rfind("{")
    if start == -1 or not head.endswith("}"):
        return head
    inner = head[start + 1 : -1].removesuffix("+")
    if not inner.isdigit():
        return head
    try:
        name = bytes(raw[1]).decode("ascii")
    except UnicodeDecodeError as exc:
        raise _UndecodableFolderNameError(
            f"LIST returned a folder name that is not ASCII (mUTF-7 is ASCII): "
            f"{raw[1]!r}"
        ) from exc
    return f"{head[:start]}{_imap_quote(name)}"


def _parse_list_entry(text: str) -> tuple[str, str] | None:
    """Parse one IMAP LIST response line → (wire_name, delimiter) or None.

    The returned name is the raw wire form exactly as the server sent it
    (still mUTF-7 encoded where applicable) — decoding happens in
    ``NativeImapClient.list_folders``, not here.
    """
    text = text.strip()
    close = text.find(")")
    if close == -1:
        return None
    rest = text[close + 1 :].strip()
    if rest.upper().startswith("NIL"):
        delimiter = ""
        name_part = rest[3:].strip()
    elif rest.startswith('"'):
        end = rest.find('"', 1)
        if end == -1:
            return None
        delimiter = rest[1:end]
        name_part = rest[end + 1 :].strip()
    else:
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return None
        delimiter = parts[0]
        name_part = parts[1].strip()
    if name_part.startswith('"'):
        name_part = _read_quoted(name_part)
    if not name_part:
        return None
    return name_part, delimiter


def write_rfc822_to_maildir(
    *,
    maildir_root: Path,
    account_name: str,
    folder_name: str,
    uid: int,
    uidvalidity: int,
    rfc822: bytes,
    quarantine_root: Path | None = None,
) -> Path:
    """Atomically write RFC822 bytes for *uid* to the maildir new/ bucket.

    ``folder_name`` must be the wire name, so two folders whose display names
    collide can never share a staging directory (design Decision 1).

    Writes to ``tmp/uid-N.eml`` first, then ``Path.replace`` (os.replace) to
    ``new/uid-N.eml`` so the MaildirParser never sees a partial write (D3).
    Idempotent: a retry with the same uid and bytes rewrites the same file;
    different bytes under a name used in staging or quarantine go to
    ``uid-N.v<uidvalidity>.eml`` (content-hash suffix only if that name is also
    taken by other bytes) (#17).
    """
    folder_dir = Path(maildir_root) / account_name / folder_name
    for bucket in ("cur", "new", "tmp"):
        (folder_dir / bucket).mkdir(parents=True, exist_ok=True)

    # #17: within one UIDVALIDITY a UID's bytes never change, so different
    # bytes under the same name (in staging, or in the quarantine prune moved
    # the old raw mail to) are a reused UID. Never replace or shadow the old raw
    # mail (it may be the only source for a pending repair): the new mail gets
    # a generation-bearing name, hence its own locator.
    def taken(candidate: str) -> bool:
        homes = [folder_dir / "new" / candidate]
        if quarantine_root is not None:
            homes.append(
                Path(quarantine_root) / account_name / folder_name / "new" / candidate
            )
        return any(h.is_file() and h.read_bytes() != rfc822 for h in homes)

    name = f"uid-{uid}.eml"
    if taken(name):
        name = f"uid-{uid}.v{uidvalidity}.eml"
        if taken(name):
            digest = hashlib.sha256(rfc822).hexdigest()[:12]
            name = f"uid-{uid}.v{uidvalidity}.{digest}.eml"
    tmp_path = folder_dir / "tmp" / name
    final_path = folder_dir / "new" / name
    tmp_path.write_bytes(rfc822)
    tmp_path.replace(final_path)
    return final_path


def _write_sync_state_start(database_path: Path, started_at: datetime) -> bool:
    """Ensure the schema and open the sync_state row before folder resolution.

    Returns whether the row now belongs to this run. The caller MUST NOT
    stamp an end on a row it never opened: the end write is a bare UPDATE, so
    running it after a failed start would pair the *previous* run's start
    timestamp with this run's end and outcome, fabricating a run that never
    happened. A missing record is honest; an invented one is not.

    Per design.md "Ensure the schema at the sync entry point": uses the same
    idempotent, additive initializer the other entry points already call, so
    the scheduled machine -- which never otherwise ensures the schema -- gets
    the sync_state table too.

    Recording sync state is bookkeeping (imap-sync spec: "A failure to record
    sync state SHALL NOT alter the outcome of the sync"), so any failure here
    is caught and logged rather than propagated -- same treatment, and the
    same ``logger.warning`` channel, as the maildir prune failure below.

    The two failures are reported separately on purpose: a schema-ensure
    failure is a whole-database problem, not a sync-state one, and labelling
    it as the latter would send the next person debugging it to the wrong
    place.
    """
    try:
        initialize_database(database_path)
    except Exception as exc:  # noqa: BLE001 -- bookkeeping must never fail the sync
        logger.warning(
            "sync-state schema ensure failed (swallowed): %s: %s",
            type(exc).__name__,
            exc,
        )
        return False

    try:
        connection = connect_database(database_path)
        try:
            _storage_record_sync_run_start(connection, started_at=started_at)
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 -- bookkeeping must never fail the sync
        logger.warning(
            "sync-state start record failed (swallowed): %s: %s",
            type(exc).__name__,
            exc,
        )
        return False

    return True


def _write_sync_state_end(
    database_path: Path, ended_at: datetime, outcome: str
) -> None:
    """Stamp the sync_state row's end timestamp and terminal outcome.

    Bookkeeping only: swallowed on failure and logged, see
    ``_write_sync_state_start``.
    """
    try:
        connection = connect_database(database_path)
        try:
            _storage_record_sync_run_end(connection, ended_at=ended_at, outcome=outcome)
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 -- bookkeeping must never fail the sync
        logger.warning(
            "sync-state end record failed (swallowed): %s: %s",
            type(exc).__name__,
            exc,
        )


def sync_native(
    *,
    env: Mapping[str, str],
    database_path: Path,
    maildir_root: Path,
    state_path: Path,
    log_dir: Path,
    attachment_root: Path | None = None,
    folder: str | None = None,
    bulk_threshold: int = BULK_FETCH_THRESHOLD,
    confirm_fn: Callable[[int], bool] | None = None,
    client_factory: Callable[[Mapping[str, str]], NativeImapClient] | None = None,
    quarantine_root: Path | None = None,
) -> SyncExecutionResult:
    """Run native Windows IMAP sync without Docker or WSL.

    Per-UID crash-safe chain (D3):
      atomic maildir write → single-message ingest → atomic watermark advance

    On any failure within a folder: stop that folder for this run, do NOT
    skip to the next UID (D3 fail-stop).  Dedup gate guarantees re-ingesting
    already-stored messages is safe.

    UIDVALIDITY change (D4): unknown key → None watermark → search from UID 1 →
    dedup gate prevents re-inserting existing messages.

    Sync run state (add-mcp-sync-status): the schema is ensured and the
    sync_state row is opened with the ``running`` outcome before folder
    resolution begins, so a run that dies during folder resolution still
    leaves evidence. The row is stamped with its terminal outcome at the
    single point below where the run's outcome is known.
    """
    start_at = datetime.now(UTC)
    state = ImapSyncState(state_path)
    account_name = env["IMAP_ACCOUNT_NAME"]
    account_email = env["IMAP_USER"]
    factory = client_factory or NativeImapClient

    # Before the client, not just before folder resolution: constructing
    # NativeImapClient opens the TLS connection, and an unreachable server is
    # the likeliest way a scheduled sync dies. Writing the record after it
    # would leave that failure invisible -- the exact blind spot this record
    # exists to close.
    sync_state_opened = _write_sync_state_start(database_path, start_at)

    total_pulled = 0
    total_inserted = 0
    total_deduped = 0
    total_failures = 0
    first_failure_stage: str | None = None
    first_failure_message: str | None = None
    folder_count = 0

    # #223: a failed adoption is a recorded failed run (no folder is synced),
    # never an escaped exception that leaves sync_state 'running' with no log.
    try:
        state.adopt_legacy(Path(database_path).resolve(), account_name, account_email)
    except Exception as exc:
        total_failures = 1
        first_failure_stage = "state"
        first_failure_message = f"{type(exc).__name__}: {exc}"

    # #255: retry recorded attachment failures from the local raw mail before
    # any connection is opened. Never affects exit code or watermarks.
    retry = AttachmentRetryResult()
    if total_failures == 0 and sync_state_opened and attachment_root is not None:
        try:
            retry_connection = connect_database(database_path)
            try:
                retry = retry_attachment_failures(
                    connection=retry_connection,
                    maildir_root=maildir_root,
                    account_name=account_name,
                    account_email=account_email,
                    attachment_root=attachment_root,
                    quarantine_root=quarantine_root
                    if quarantine_root is not None
                    else default_quarantine_root(database_path),
                )
            finally:
                retry_connection.close()
        except Exception as exc:
            logger.warning(
                "attachment retry failed (swallowed): %s: %s",
                type(exc).__name__,
                exc,
            )

    # #318: same rule for the connection. Caught: socket/TLS failures (OSError,
    # incl. gaierror/timeout/SSLError) and imaplib protocol errors (login
    # rejected, abort). Not caught: TypeError/KeyError/etc. -- a bug stays loud.
    client = None
    if total_failures == 0:
        try:
            client = factory(env)
        except (OSError, imaplib.IMAP4.error) as exc:
            total_failures = 1
            first_failure_stage = "connect"
            first_failure_message = f"{type(exc).__name__}: {exc}"

    try:
        folders_to_sync = (
            resolve_folders(client=client, folder=folder) if client is not None else []
        )
        # #308: snapshot now -- a later list_folders call resets the list.
        skipped = list(skipped_listings(client, folder))
        total_failures += len(skipped)
        if skipped and first_failure_stage is None:
            first_failure_stage = "list"
            first_failure_message = "; ".join(f"skipped folder: {s}" for s in skipped)
        for imap_folder in folders_to_sync:
            try:
                pulled, inserted, deduped = _sync_one_folder(
                    client=client,
                    state=state,
                    database_path=database_path,
                    maildir_root=maildir_root,
                    account_email=account_email,
                    account_name=account_name,
                    display_name=imap_folder.display_name,
                    wire_name=imap_folder.wire_name,
                    delimiter=imap_folder.delimiter,
                    listed=imap_folder.listed,
                    attachment_root=attachment_root,
                    bulk_threshold=bulk_threshold,
                    confirm_fn=confirm_fn,
                    quarantine_root=quarantine_root
                    if quarantine_root is not None
                    else default_quarantine_root(database_path),
                )
                total_pulled += pulled
                total_inserted += inserted
                total_deduped += deduped
                folder_count += 1
            except _BulkGuardRefusedError as exc:
                total_failures += 1
                if first_failure_stage is None:
                    first_failure_stage = "bulk_guard"
                    first_failure_message = str(exc)
            except _PartialFolderSyncError as exc:
                total_pulled += exc.pulled_messages
                total_inserted += exc.inserted_messages
                total_deduped += exc.deduplicated_messages
                total_failures += 1
                if first_failure_stage is None:
                    first_failure_stage = "sync"
                    first_failure_message = f"{type(exc).__name__}: {exc}"
            except Exception as exc:
                total_failures += 1
                if first_failure_stage is None:
                    first_failure_stage = "sync"
                    first_failure_message = f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            client.close()

    # Run maildir retention prune after per-folder loop (D2, D4).
    # sync_native is only reached from the non-dry-run path in run_sync_imap;
    # the dry-run path (estimate_sync_run) never calls sync_native, so the
    # prune is automatically skipped in dry-run mode.
    try:
        prune_counts = prune_ingested_maildir(
            maildir_root=maildir_root,
            account_name=account_name,
            database_path=database_path,
            retention_days=MAILDIR_RETENTION_DAYS,
            now=start_at,
            account_email=account_email,
            quarantine_root=quarantine_root,
        )
        logger.info(
            "maildir prune: deleted=%d kept_not_in_db=%d kept_unparseable=%d",
            prune_counts["deleted"],
            prune_counts["kept_not_in_db"],
            prune_counts["kept_unparseable"],
        )
    except Exception as exc:
        logger.warning(
            "maildir prune failed (swallowed): %s: %s", type(exc).__name__, exc
        )

    end_at = datetime.now(UTC)
    exit_code = 0 if total_failures == 0 else 1
    result = SyncExecutionResult(
        pulled_messages=total_pulled,
        folder_count=folder_count,
        candidate_messages=total_inserted + total_deduped,
        inserted_messages=total_inserted,
        deduplicated_messages=total_deduped,
        failure_count=total_failures,
        exit_code=exit_code,
        failure_stage=first_failure_stage,
        failure_message=first_failure_message,
        attachments_recovered=retry.recovered,
        attachments_still_failing=retry.still_failing,
        attachments_source_missing=retry.source_missing,
    )
    # Only stamp an end on a row this run actually opened -- see
    # _write_sync_state_start's docstring for why a bare UPDATE after a failed
    # start would invent a run that never happened.
    if sync_state_opened:
        _write_sync_state_end(
            database_path, end_at, "failed" if total_failures > 0 else "completed"
        )
    _append_sync_log(log_dir=log_dir, start_at=start_at, end_at=end_at, result=result)
    return result


def resolve_folders(
    *,
    client: NativeImapClient,
    folder: str | None,
) -> list[ImapFolder]:
    """Resolve which folders to sync, as display/wire pairs.

    ``folder`` (a ``--folder`` CLI argument) is resolved against the folders
    the server actually listed, in this order:

    1. An exact wire-name match wins outright — the wire name is the
       identity (design Decision 1).
    2. Otherwise, a display-name match is accepted only when exactly one
       listed folder has that display name. Two listed folders can share a
       display name (e.g. a well-formed folder whose wire name is ``&-AB``
       decodes to ``&AB``, colliding with a malformed folder whose wire name
       is literally ``&AB``); silently picking one would route the wrong
       identity into STATUS, SELECT, the watermark key, and the maildir
       staging directory, so two-or-more display-name matches raise instead
       of guessing.
    3. If nothing matches at all, ``folder`` did not come from LIST — per
       design Decision 1, encoding is only ever applied in that case, to
       derive a wire name for a folder name that never came from the
       server's listing.

    Calls ``client.list_folders()`` exactly once per invocation; shared by
    ``sync_native`` and ``imap_sync.estimate_sync_run`` so folder-resolution
    logic exists in exactly one place.
    """
    all_folders = client.list_folders()
    if folder is None:
        return all_folders

    wire_match = next((f for f in all_folders if f.wire_name == folder), None)
    if wire_match is not None:
        return [wire_match]

    display_matches = [f for f in all_folders if f.display_name == folder]
    if len(display_matches) == 1:
        return [display_matches[0]]
    if len(display_matches) > 1:
        candidates = ", ".join(repr(f.wire_name) for f in display_matches)
        raise RuntimeError(
            f"--folder {folder!r} matches {len(display_matches)} listed folders "
            f"by display name ambiguously (none matched by wire name); "
            f"candidate wire names: {candidates}. Specify the wire name "
            "directly to disambiguate."
        )

    return [
        ImapFolder(
            display_name=folder,
            wire_name=encode_mutf7(folder),
            delimiter="/",
            listed=False,
        )
    ]


def _sync_one_folder(
    *,
    client: NativeImapClient,
    state: ImapSyncState,
    database_path: Path,
    maildir_root: Path,
    account_email: str,
    account_name: str,
    display_name: str,
    wire_name: str,
    delimiter: str,
    listed: bool = True,
    attachment_root: Path | None,
    bulk_threshold: int,
    confirm_fn: Callable[[int], bool] | None,
    quarantine_root: Path,
) -> tuple[int, int, int]:
    """Sync one folder; returns (pulled, inserted, deduped).

    Raises on folder-level IMAP errors or bulk guard refusal.
    Per-UID errors propagate immediately (D3 fail-stop).

    The wire name is the identity: it is what every IMAP command names the
    mailbox with, what the sync-state watermark is keyed on, and what the
    maildir staging directory is named after. The display name is the label:
    it is what gets persisted into ``folder_path`` / ``pst_source`` /
    ``logical_locator`` (design Decision 1).
    """
    uidvalidity = status_uidvalidity(
        client,
        ImapFolder(display_name, wire_name, delimiter, listed=listed),
    )
    watermark = state.effective_watermark(
        database_path, account_name, account_email, wire_name, delimiter, uidvalidity
    )

    client.select_folder(wire_name)
    uids_to_fetch = client.search_uids_after(watermark)

    if not uids_to_fetch:
        return 0, 0, 0

    # D7 bulk guard: only when starting fresh (watermark is None = new generation)
    if (
        watermark is None
        and len(uids_to_fetch) > bulk_threshold
        and (confirm_fn is None or not confirm_fn(len(uids_to_fetch)))
    ):
        raise _BulkGuardRefusedError(
            f"{wire_name!r}: {len(uids_to_fetch)} UIDs exceeds "
            f"bulk threshold {bulk_threshold}; use --full-resync with confirmation"
        )

    account_root = Path(maildir_root).resolve() / account_name
    folder_dir = Path(maildir_root).resolve() / account_name / wire_name
    source = MaildirSource(
        path=folder_dir,
        account_email=account_email,
        account_name=account_name,
        folder_name=display_name,
        requested_from=account_root,
    )

    pulled = inserted = deduped = 0
    connection = connect_database(database_path)
    try:
        for uid in uids_to_fetch:  # search_uids_after returns sorted ascending
            # D3 chain: fetch → write maildir (atomic) → ingest → commit → watermark
            rfc822 = client.fetch_rfc822(uid)  # propagates on failure (fail-stop)
            message_path = write_rfc822_to_maildir(
                maildir_root=maildir_root,
                account_name=account_name,
                folder_name=wire_name,
                uid=uid,
                uidvalidity=uidvalidity,
                rfc822=rfc822,
                quarantine_root=quarantine_root,
            )
            pulled += 1
            summary = import_maildir_message(
                connection=connection,
                source=source,
                message_path=message_path,
                attachment_root=attachment_root,
            )
            if summary.imported_messages + summary.duplicate_messages == 0:
                connection.commit()
                raise _MessageIngestFailedError(
                    wire_name=wire_name,
                    uid=uid,
                    pulled_messages=pulled,
                    inserted_messages=inserted,
                    deduplicated_messages=deduped,
                )
            # D3: advance watermark only after this message is durably committed.
            # #194 follow-up (Codex round 2 Track B): count as inserted/deduped
            # only once this commit has actually returned -- a prior
            # ingest_failures INSERT (e.g. an attachment CAS-store failure,
            # recorded before this message's own SAVEPOINT) opens an implicit
            # transaction, making the per-message SAVEPOINT/RELEASE nested
            # rather than outermost, so RELEASE does NOT commit; durability
            # for this UID is decided right here, by this commit() call.
            # #223: progress rides the same commit as the message, so it
            # travels with DB backup/restore; the JSON file is only a mirror.
            set_imap_progress(
                connection, account_name, wire_name, delimiter, uidvalidity, uid
            )
            connection.commit()
            inserted += summary.imported_messages
            deduped += summary.duplicate_messages
            state.set_watermark(wire_name, delimiter, uidvalidity, uid)
    except _MessageIngestFailedError:
        raise
    except Exception as exc:
        # Any other failure (FETCH, maildir write, ingest) aborts the folder
        # (fail-stop, unchanged) but must still report what this folder
        # genuinely completed before it (#194). The original exception text is
        # folded into the message because sync_native builds failure_message
        # from str(exc) of the exception it catches.
        raise _PartialFolderSyncError(
            f"{wire_name!r}: UID {uid} interrupted the folder sync "
            f"({type(exc).__name__}: {exc})",
            pulled_messages=pulled,
            inserted_messages=inserted,
            deduplicated_messages=deduped,
        ) from exc
    finally:
        connection.close()

    return pulled, inserted, deduped

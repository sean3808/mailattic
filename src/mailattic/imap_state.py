from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .mutf7 import decode_mutf7
from .storage import connect_database, get_imap_progress, set_imap_progress

_DATABASE_KEY = "__database__"


def _held_uid(
    connection: sqlite3.Connection, account_email: str, wire_folder: str
) -> int | None:
    """Highest UID this DB holds for the account's folder (legacy-file evidence).

    ``pst_source`` pins the rows to this account (``MaildirSource.source_path``);
    the locator alone would let another account's same-named folder count.
    """
    display = decode_mutf7(wire_folder)
    prefix = f"{display}/new/uid-"
    # ponytail: rows carry no UIDVALIDITY, so an older epoch of the same folder
    # still counts; closing that needs the epoch stored per message.
    held = connection.execute(
        "SELECT MAX(CAST(substr(logical_locator, ?) AS INTEGER)) "
        "FROM messages WHERE pst_source = ? AND substr(logical_locator, 1, ?) = ?",
        [len(prefix) + 1, f"imap:{account_email}:{display}", len(prefix), prefix],
    ).fetchone()[0]
    return None if held is None else int(held)


class ImapSyncState:
    """Per-folder UID watermark store backed by an atomic JSON file.

    Key format: ``canonical_folder\\x00delimiter\\x00uidvalidity``
    Values are the integer last-ingested UID for that folder/generation.

    Write path is tmp-then-replace (D8 atomic guarantee).
    """

    def __init__(self, state_path: Path) -> None:
        self._path = Path(state_path)
        self._data: dict[str, int] = {}
        self._database: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._data = {}
            return
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            self._data = {}
            return
        self._data = {
            k: int(v)
            for k, v in loaded.items()
            if isinstance(v, int | float) and not isinstance(v, bool)
        }
        database = loaded.get(_DATABASE_KEY)
        self._database = database if isinstance(database, str) else None

    @property
    def bound_database(self) -> str | None:
        """Database this file's watermarks were last bound to (None = legacy)."""
        return self._database

    def bind_database(self, database_path: str) -> None:
        """Record which database owns these watermarks (#223); persists if non-empty."""
        self._database = database_path
        if self._data:
            self._save_atomic()

    def entries(self) -> list[tuple[str, str, int, int]]:
        """All ``(folder, delimiter, uidvalidity, last_uid)`` watermarks."""
        out = []
        for key, uid in self._data.items():
            folder, delimiter, uidvalidity = key.split("\x00")
            out.append((folder, delimiter, int(uidvalidity), uid))
        return out

    @staticmethod
    def _make_key(canonical_folder: str, delimiter: str, uidvalidity: int) -> str:
        return f"{canonical_folder}\x00{delimiter}\x00{uidvalidity}"

    def get_watermark(
        self, canonical_folder: str, delimiter: str, uidvalidity: int
    ) -> int | None:
        """Return last-ingested UID for this folder/generation, or None if unknown."""
        return self._data.get(self._make_key(canonical_folder, delimiter, uidvalidity))

    def set_watermark(
        self,
        canonical_folder: str,
        delimiter: str,
        uidvalidity: int,
        last_uid: int,
    ) -> None:
        """Advance the watermark and persist atomically (write-tmp-then-replace, D8)."""
        self._data[self._make_key(canonical_folder, delimiter, uidvalidity)] = last_uid
        self._save_atomic()

    def clear_all(self) -> None:
        """Delete the state file (full-resync reset; dedup gate prevents re-insert)."""
        import contextlib

        self._data = {}
        self._database = None
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    def effective_watermark(
        self,
        database_path: Path,
        account_name: str,
        account_email: str,
        canonical_folder: str,
        delimiter: str,
        uidvalidity: int,
    ) -> int | None:
        """Watermark to search from: the DB's own progress (#223).

        The JSON file is a mirror; trusting it over the DB skips mail after a
        snapshot restore or a switch to another ``--database``. A legacy file
        (never bound to a database) is only reached here by dry-run, which
        applies the same DB-evidence limit as :meth:`adopt_legacy` without
        writing, so the estimate matches what the real sync would search.
        """
        mirror = self.get_watermark(canonical_folder, delimiter, uidvalidity)
        try:  # missing DB / pre-#223 schema (dry-run) = no progress yet
            connection = connect_database(database_path, read_only=True)
            try:
                progress = get_imap_progress(
                    connection, account_name, canonical_folder, delimiter, uidvalidity
                )
                if progress is None and self._database is None and mirror is not None:
                    progress = _held_uid(connection, account_email, canonical_folder)
            finally:
                connection.close()
        except (sqlite3.OperationalError, FileNotFoundError):
            progress = None
        if progress is None:
            return None
        # A cleared / older file (full-resync) wins too; only a *newer* one is
        # distrusted, so the minimum is always safe (dedup absorbs rescans).
        return None if mirror is None else min(mirror, progress)

    def adopt_legacy(
        self, database_path: Path, account_name: str, account_email: str
    ) -> None:
        """Seed a never-bound legacy file into this DB once, then bind it.

        A legacy watermark is adopted only up to the highest UID this DB itself
        holds for the folder (``<display folder>/new/uid-N.eml`` in
        ``messages.logical_locator``); a restored or different DB holds less,
        and adopting the file as-is would skip mail it never ingested. Trailing
        dedup hits leave no row, so they are rescanned (dedup absorbs them).
        No row for the folder at all = rescan from None.
        """
        if self._database is not None:
            return
        connection = connect_database(database_path)
        try:
            for folder, delimiter, uidvalidity, uid in self.entries():
                held = _held_uid(connection, account_email, folder)
                if (
                    held is not None
                    and get_imap_progress(
                        connection, account_name, folder, delimiter, uidvalidity
                    )
                    is None
                ):
                    set_imap_progress(
                        connection,
                        account_name,
                        folder,
                        delimiter,
                        uidvalidity,
                        min(uid, held),
                    )
            connection.commit()
        finally:
            connection.close()
        self.bind_database(str(database_path))

    def _save_atomic(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".json.tmp")
        payload: dict[str, int | str] = dict(self._data)
        if self._database is not None:
            payload[_DATABASE_KEY] = self._database
        tmp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(self._path))

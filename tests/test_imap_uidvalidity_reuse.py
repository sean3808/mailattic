"""#17: a reused UID after a UIDVALIDITY reset must not lose or falsely settle repairs.

Real MIME -> sync_native -> ingest -> SQLite/CAS -> retry; only the IMAP client
and the CAS write (OSError) are fake. Both mails carry a same-name, same-size
attachment so ledger matching by (locator, name, size) cannot tell them apart.
"""

from __future__ import annotations

import shutil
import sqlite3
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from mailattic import attachment_store
from mailattic.imap_native import NativeImapClient, sync_native
from mailattic.storage import initialize_database
from tests.test_imap_native import _TEST_ENV, _MockImapConn

REAL_WRITE = attachment_store.write_attachment
LIST_DATA = [b'(\\HasNoChildren) "/" INBOX']


def _mime(message_id: str, payload: bytes) -> bytes:
    msg = EmailMessage()
    msg["From"] = "supplier@example.test"
    msg["To"] = "buyer@example.test"
    msg["Subject"] = "Quotation"
    msg["Message-ID"] = message_id
    msg.set_content("See attached.")
    msg.add_attachment(
        payload, maintype="application", subtype="octet-stream", filename="quote.pdf"
    )
    return msg.as_bytes()


def _broken_cas(*_args, **_kwargs):
    raise OSError("synthetic CAS outage")


class Mailbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = initialize_database(root / "t.db")
        self.cas = root / "cas"
        self.quarantine = root / "quarantine"

    def sync(self, *, epoch: int, uid_to_raw: dict[int, bytes], cas_ok: bool) -> None:
        conn = _MockImapConn(
            list_data=LIST_DATA,
            status_map={"INBOX": epoch},
            search_map={"INBOX": sorted(uid_to_raw)},
            fetch_map=uid_to_raw,
        )

        def run() -> None:
            result = sync_native(
                env=_TEST_ENV,
                database_path=self.db,
                maildir_root=self.root / "maildir",
                state_path=self.root / "state.json",
                log_dir=self.root / "logs",
                attachment_root=self.cas,
                quarantine_root=self.quarantine,
                client_factory=lambda env: NativeImapClient(env, _connection=conn),
            )
            assert result.exit_code == 0  # attachment sub-failure never blocks (#5)

        if cas_ok:
            run()
        else:
            with patch.object(attachment_store, "write_attachment", _broken_cas):
                run()

    def attachments(self) -> dict[str, str | None]:
        with sqlite3.connect(self.db) as c:
            return dict(
                c.execute(
                    "SELECT m.message_id, a.content_hash FROM message_attachments a"
                    " JOIN messages m ON m.id = a.message_id"
                )
            )

    def open_failures(self) -> int:
        with sqlite3.connect(self.db) as c:
            return c.execute(
                "SELECT COUNT(*) FROM ingest_failures WHERE resolved_at IS NULL"
                " AND error_stage LIKE 'attachment-%'"
            ).fetchone()[0]


OLD = "<old@example.test>"
NEW = "<new@example.test>"
OLD_RAW = _mime(OLD, b"old payload")
NEW_RAW = _mime(NEW, b"new payload")  # same 11 bytes, same name


def test_reused_uid_keeps_both_repairs_independent(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    box.sync(epoch=100, uid_to_raw={1: OLD_RAW}, cas_ok=False)
    box.sync(epoch=200, uid_to_raw={1: NEW_RAW}, cas_ok=False)
    assert box.open_failures() == 2

    box.sync(epoch=200, uid_to_raw={1: NEW_RAW}, cas_ok=True)  # healthy retry

    hashes = box.attachments()
    assert hashes[OLD] is not None
    assert hashes[NEW] is not None
    assert box.open_failures() == 0


def test_reused_uid_with_quarantined_old_raw_neither_settles_nor_deletes(
    tmp_path: Path,
) -> None:
    box = Mailbox(tmp_path)
    box.sync(epoch=100, uid_to_raw={1: OLD_RAW}, cas_ok=False)
    # what prune does to a raw mail with an open attachment failure
    staged = tmp_path / "maildir" / "testaccount" / "INBOX" / "new" / "uid-1.eml"
    held = box.quarantine / "testaccount" / "INBOX" / "new" / "uid-1.eml"
    held.parent.mkdir(parents=True)
    shutil.move(staged, held)
    box.sync(epoch=200, uid_to_raw={1: NEW_RAW}, cas_ok=False)

    box.sync(epoch=200, uid_to_raw={1: NEW_RAW}, cas_ok=True)

    assert held.read_bytes() == OLD_RAW  # original kept
    assert box.attachments()[OLD] is None
    assert box.open_failures() >= 1  # the unrepaired old attachment is still owed

"""Tests for issue #72: cross-store PST original export fallback.

When a PST-sourced message has a Message-ID and is cleanly not found in its
recorded PST store, original export searches the other Outlook-mounted PST
stores that the DB knows about (distinct ``pst_source`` values) by Message-ID.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from mailattic.models import MaildirSource, PstSource, build_normalized_message
from mailattic.original_export import export_from_pst, export_original_message
from mailattic.storage import connect_database, initialize_database, persist_message
from tests.test_original_export import (
    FakeOutlookFolder,
    FakeOutlookItem,
    FakeOutlookNamespace,
    FakeOutlookReader,
    FakeOutlookStore,
)


class _SpyOutlookStore(FakeOutlookStore):
    """Fake store that records whether GetRootFolder was called."""

    def __init__(self, file_path: str, root_folder: FakeOutlookFolder) -> None:
        super().__init__(file_path, root_folder)
        self.root_calls = 0

    def GetRootFolder(self) -> FakeOutlookFolder:
        self.root_calls += 1
        return super().GetRootFolder()


def _seed_db_with_pst_rows(
    tmp_path: Path,
    rows: list[dict[str, object]],
) -> tuple[Path, list[int]]:
    db_path = initialize_database(tmp_path / "mail.db")
    conn = connect_database(db_path)
    ids: list[int] = []
    try:
        for row in rows:
            pst_path = str(row["pst_path"])
            source = PstSource(
                path=Path(pst_path),
                input_mode="file",
                requested_from=Path(pst_path),
            )
            msg = build_normalized_message(
                {
                    "subject": str(row.get("subject", "Cross Store Subject")),
                    "sender": {"email": "alice@example.test", "name": "Alice"},
                    "to": [{"email": "bob@example.test"}],
                    "sent_at": "2026-05-10T14:30:00+08:00",
                    "body_text": "Sample body",
                    "message_id": row.get("message_id"),
                },
                source=source,
                folder_path=str(row.get("folder_path", "Inbox")),
                logical_locator=str(row.get("entry_id", "entry-1")),
            )
            result = persist_message(conn, msg)
            ids.append(result.stored_message_id)
        conn.commit()
    finally:
        conn.close()
    return db_path, ids


def test_cross_store_found_once_in_other_known_pst_exports(tmp_path: Path) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_pst = str(tmp_path / "archive-2025.pst")
    target_id = "<moved-1@example.test>"

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    moved_item = FakeOutlookItem(
        entry_id="entry-arch-1",
        subject="Moved Mail",
        message_id=target_id,
    )
    archive_root = FakeOutlookFolder(
        "Root", folders=[FakeOutlookFolder("Archive2025", [moved_item])]
    )
    namespace = FakeOutlookNamespace(
        [
            FakeOutlookStore(recorded_pst, recorded_root),
            FakeOutlookStore(archive_pst, archive_root),
        ]
    )

    dest = tmp_path / "exports" / "mail.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    message = {
        "id": 1,
        "pst_source": recorded_pst,
        "folder_path": "Inbox",
        "logical_locator": "entry-missing",
        "message_id": target_id,
        "subject": "Moved Mail",
        "sender_email": "alice@example.test",
        "sent_at": "2026-05-10T14:30:00+08:00",
    }

    result = export_from_pst(
        message,
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, archive_pst],
    )

    assert result.status == "exported"
    assert result.dest_path == dest
    assert dest.read_bytes().endswith(b"entry-arch-1")
    assert result.detail == "Found in another mounted PST: archive-2025.pst"


def _base_message(
    pst_source: str,
    *,
    message_id: str | None = "<target@example.test>",
    folder_path: str = "Inbox",
    logical_locator: str = "entry-missing",
) -> dict[str, object]:
    return {
        "id": 1,
        "pst_source": pst_source,
        "folder_path": folder_path,
        "logical_locator": logical_locator,
        "message_id": message_id,
        "subject": "Cross Store Mail",
        "sender_email": "alice@example.test",
        "sent_at": "2026-05-10T14:30:00+08:00",
    }


def test_cross_store_found_in_multiple_other_stores_fails_ambiguous(
    tmp_path: Path,
) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_a = str(tmp_path / "archive-a.pst")
    archive_b = str(tmp_path / "archive-b.pst")
    target_id = "<dup-across@example.test>"

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    root_a = FakeOutlookFolder(
        "Root",
        folders=[
            FakeOutlookFolder("Inbox", [FakeOutlookItem("entry-a", "Dup", target_id)])
        ],
    )
    root_b = FakeOutlookFolder(
        "Root",
        folders=[
            FakeOutlookFolder("Inbox", [FakeOutlookItem("entry-b", "Dup", target_id)])
        ],
    )
    namespace = FakeOutlookNamespace(
        [
            FakeOutlookStore(recorded_pst, recorded_root),
            FakeOutlookStore(archive_a, root_a),
            FakeOutlookStore(archive_b, root_b),
        ]
    )
    dest = tmp_path / "exports" / "dup.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id=target_id),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, archive_a, archive_b],
    )

    assert result.status == "failed"
    assert not dest.exists()
    assert result.detail is not None
    assert "ambiguous" in result.detail.casefold()
    assert "2" in result.detail


def test_cross_store_found_twice_in_one_other_store_fails_ambiguous(
    tmp_path: Path,
) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_pst = str(tmp_path / "archive.pst")
    target_id = "<dup-one@example.test>"

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    archive_root = FakeOutlookFolder(
        "Root",
        folders=[
            FakeOutlookFolder(
                "Inbox",
                [
                    FakeOutlookItem("entry-1", "Dup", target_id),
                    FakeOutlookItem("entry-2", "Dup", target_id),
                ],
            )
        ],
    )
    namespace = FakeOutlookNamespace(
        [
            FakeOutlookStore(recorded_pst, recorded_root),
            FakeOutlookStore(archive_pst, archive_root),
        ]
    )
    dest = tmp_path / "exports" / "dup_one.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id=target_id),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, archive_pst],
    )

    assert result.status == "failed"
    assert not dest.exists()
    assert result.detail is not None
    assert "ambiguous" in result.detail.casefold()
    assert "2" in result.detail


def test_cross_store_found_nowhere_returns_not_found(tmp_path: Path) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_pst = str(tmp_path / "archive.pst")

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    archive_root = FakeOutlookFolder(
        "Root",
        folders=[
            FakeOutlookFolder(
                "Inbox",
                [FakeOutlookItem("entry-other", "Other", "<other@example.test>")],
            )
        ],
    )
    namespace = FakeOutlookNamespace(
        [
            FakeOutlookStore(recorded_pst, recorded_root),
            FakeOutlookStore(archive_pst, archive_root),
        ]
    )
    dest = tmp_path / "exports" / "missing.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id="<missing@example.test>"),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, archive_pst],
    )

    assert result.status == "not_found"
    assert result.detail == "Original message not found in PST."


def test_found_in_recorded_store_does_not_touch_other_stores(tmp_path: Path) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_pst = str(tmp_path / "archive.pst")
    target_id = "<present@example.test>"

    recorded_root = FakeOutlookFolder(
        "Root",
        folders=[
            FakeOutlookFolder(
                "Inbox", [FakeOutlookItem("entry-rec", "Here", target_id)]
            )
        ],
    )
    archive_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    archive_store = _SpyOutlookStore(archive_pst, archive_root)
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore(recorded_pst, recorded_root), archive_store]
    )
    archive_store.root_calls = 0
    dest = tmp_path / "exports" / "present.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id=target_id, logical_locator="entry-rec"),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, archive_pst],
    )

    assert result.status == "exported"
    assert result.detail is None
    assert archive_store.root_calls == 0


def test_unlisted_mounted_store_is_never_scanned(tmp_path: Path) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    unrelated_pst = str(tmp_path / "unrelated-personal.pst")
    target_id = "<in-unrelated@example.test>"

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    unrelated_root = FakeOutlookFolder(
        "Root",
        folders=[
            FakeOutlookFolder(
                "Inbox", [FakeOutlookItem("entry-unrel", "Secret", target_id)]
            )
        ],
    )
    unrelated_store = _SpyOutlookStore(unrelated_pst, unrelated_root)
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore(recorded_pst, recorded_root), unrelated_store]
    )
    unrelated_store.root_calls = 0
    dest = tmp_path / "exports" / "unrelated.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id=target_id),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst],
    )

    assert result.status == "not_found"
    assert unrelated_store.root_calls == 0


def test_other_store_with_unreadable_folder_and_no_match_fails(
    tmp_path: Path,
) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_pst = str(tmp_path / "archive.pst")

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    archive_root = FakeOutlookFolder(
        "Root",
        folders=[FakeOutlookFolder("Broken", table_error=RuntimeError("corrupt"))],
    )
    namespace = FakeOutlookNamespace(
        [
            FakeOutlookStore(recorded_pst, recorded_root),
            FakeOutlookStore(archive_pst, archive_root),
        ]
    )
    dest = tmp_path / "exports" / "broken.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id="<missing@example.test>"),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, archive_pst],
    )

    assert result.status == "failed"
    assert result.detail is not None
    assert "could not be read" in result.detail


def test_message_without_message_id_never_searches_other_stores(
    tmp_path: Path,
) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_pst = str(tmp_path / "archive.pst")

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    archive_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    archive_store = _SpyOutlookStore(archive_pst, archive_root)
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore(recorded_pst, recorded_root), archive_store]
    )
    archive_store.root_calls = 0
    dest = tmp_path / "exports" / "no_id.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id=None),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, archive_pst],
    )

    assert result.status == "failed"
    assert archive_store.root_calls == 0


def test_recorded_store_error_does_not_trigger_cross_store_search(
    tmp_path: Path,
) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_pst = str(tmp_path / "archive.pst")
    target_id = "<rec-err@example.test>"

    recorded_root = FakeOutlookFolder(
        "Root",
        folders=[FakeOutlookFolder("Inbox", table_error=RuntimeError("boom"))],
    )
    archive_root = FakeOutlookFolder(
        "Root",
        folders=[
            FakeOutlookFolder(
                "Inbox", [FakeOutlookItem("entry-arch", "Moved", target_id)]
            )
        ],
    )
    archive_store = _SpyOutlookStore(archive_pst, archive_root)
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore(recorded_pst, recorded_root), archive_store]
    )
    archive_store.root_calls = 0
    dest = tmp_path / "exports" / "rec_err.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id=target_id),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, archive_pst],
    )

    assert result.status == "failed"
    assert archive_store.root_calls == 0


def test_end_to_end_export_original_message_searches_db_known_pst_sources(
    tmp_path: Path,
) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_pst = str(tmp_path / "archive-2025.pst")
    unrelated_pst = str(tmp_path / "unrelated.pst")
    target_id = "<e2e-moved@example.test>"

    db_path, ids = _seed_db_with_pst_rows(
        tmp_path,
        [
            {
                "pst_path": recorded_pst,
                "message_id": target_id,
                "entry_id": "entry-gone",
                "subject": "E2E Moved",
            },
            {
                "pst_path": archive_pst,
                "message_id": "<other-in-archive@example.test>",
                "entry_id": "entry-other",
                "subject": "Other Archive Mail",
            },
        ],
    )
    # Also persist a non-PST (IMAP) row to confirm it does not break source listing.
    conn = connect_database(db_path)
    try:
        imap_msg = build_normalized_message(
            {
                "subject": "IMAP Row",
                "sender": {"email": "alice@example.test", "name": "Alice"},
                "to": [{"email": "bob@example.test"}],
                "sent_at": "2026-05-11T10:00:00+08:00",
                "body_text": "IMAP body",
                "message_id": "<imap-row@example.test>",
            },
            source=MaildirSource(
                path=tmp_path / "maildir",
                account_email="user@example.test",
                account_name="user",
                folder_name="INBOX",
                requested_from=tmp_path / "maildir",
            ),
            folder_path="INBOX",
            logical_locator="INBOX/cur/uid-9.eml",
        )
        persist_message(conn, imap_msg)
        conn.commit()
    finally:
        conn.close()

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    moved_item = FakeOutlookItem("entry-found-arch", "E2E Moved", target_id)
    archive_root = FakeOutlookFolder(
        "Root", folders=[FakeOutlookFolder("2025", [moved_item])]
    )
    unrelated_store = _SpyOutlookStore(
        unrelated_pst,
        FakeOutlookFolder(
            "Root",
            folders=[
                FakeOutlookFolder(
                    "Inbox", [FakeOutlookItem("entry-unrel", "E2E Moved", target_id)]
                )
            ],
        ),
    )
    namespace = FakeOutlookNamespace(
        [
            FakeOutlookStore(recorded_pst, recorded_root),
            FakeOutlookStore(archive_pst, archive_root),
            unrelated_store,
        ]
    )
    unrelated_store.root_calls = 0

    result = export_original_message(
        db_path,
        ids[0],
        tmp_path / "exports",
        outlook_reader=FakeOutlookReader(namespace),
    )

    assert result.status == "exported"
    assert result.dest_path is not None
    assert result.dest_path.read_bytes().endswith(b"entry-found-arch")
    assert result.detail == "Found in another mounted PST: archive-2025.pst"
    assert unrelated_store.root_calls == 0


def test_known_pst_source_not_mounted_is_skipped(tmp_path: Path) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    unmounted_pst = str(tmp_path / "offline-2024.pst")
    archive_pst = str(tmp_path / "archive-2025.pst")
    target_id = "<skip-unmounted@example.test>"

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    archive_root = FakeOutlookFolder(
        "Root",
        folders=[
            FakeOutlookFolder(
                "Inbox", [FakeOutlookItem("entry-ok", "Found", target_id)]
            )
        ],
    )
    namespace = FakeOutlookNamespace(
        [
            FakeOutlookStore(recorded_pst, recorded_root),
            FakeOutlookStore(archive_pst, archive_root),
        ]
    )
    dest = tmp_path / "exports" / "skip_unmounted.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id=target_id),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, unmounted_pst, archive_pst],
    )

    assert result.status == "exported"
    assert result.detail == "Found in another mounted PST: archive-2025.pst"


def test_known_pst_source_ambiguous_filename_fails(tmp_path: Path) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    missing_archive = str(tmp_path / "old-dir" / "archive.pst")
    mounted_a = str(tmp_path / "dir-a" / "archive.pst")
    mounted_b = str(tmp_path / "dir-b" / "archive.pst")

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    namespace = FakeOutlookNamespace(
        [
            FakeOutlookStore(recorded_pst, recorded_root),
            FakeOutlookStore(mounted_a, FakeOutlookFolder("Root", [])),
            FakeOutlookStore(mounted_b, FakeOutlookFolder("Root", [])),
        ]
    )
    dest = tmp_path / "exports" / "ambig_name.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id="<id@example.test>"),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, missing_archive],
    )

    assert result.status == "failed"
    assert result.detail is not None
    assert "share its filename" in result.detail


def test_export_original_message_db_failure_listing_pst_sources_fails(
    tmp_path: Path,
) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    db_path, ids = _seed_db_with_pst_rows(
        tmp_path,
        [{"pst_path": recorded_pst, "message_id": "<db-err@example.test>"}],
    )
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore(recorded_pst, FakeOutlookFolder("Root", []))]
    )

    with patch(
        "mailattic.original_export._list_known_pst_sources",
        side_effect=sqlite3.OperationalError("db locked"),
    ):
        result = export_original_message(
            db_path,
            ids[0],
            tmp_path / "exports",
            outlook_reader=FakeOutlookReader(namespace),
        )

    assert result.status == "failed"
    assert result.detail == "could not list other PST sources: db locked"


def test_export_from_pst_none_known_sources_preserves_old_behaviour(
    tmp_path: Path,
) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_pst = str(tmp_path / "archive.pst")
    target_id = "<none-sources@example.test>"

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    archive_store = _SpyOutlookStore(
        archive_pst,
        FakeOutlookFolder(
            "Root",
            folders=[
                FakeOutlookFolder(
                    "Inbox", [FakeOutlookItem("entry-a", "Mail", target_id)]
                )
            ],
        ),
    )
    namespace = FakeOutlookNamespace(
        [FakeOutlookStore(recorded_pst, recorded_root), archive_store]
    )
    archive_store.root_calls = 0
    dest = tmp_path / "exports" / "none_sources.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(recorded_pst, message_id=target_id),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=None,
    )

    assert result.status == "not_found"
    assert archive_store.root_calls == 0


def test_cross_store_ignores_recorded_entry_id_and_dedups_relocated_source(
    tmp_path: Path,
) -> None:
    recorded_pst = str(tmp_path / "account.pst")
    archive_pst_mounted = str(tmp_path / "new-loc" / "archive.pst")
    archive_pst_old_path = str(tmp_path / "old-loc" / "archive.pst")
    target_id = "<relocated@example.test>"

    recorded_root = FakeOutlookFolder("Root", folders=[FakeOutlookFolder("Inbox", [])])
    wrong_same_entry = FakeOutlookItem(
        "entry-recorded-id", "Wrong Mail", "<different@example.test>"
    )
    right_item = FakeOutlookItem("entry-new-id", "Right Mail", target_id)
    archive_root = FakeOutlookFolder(
        "Root",
        folders=[FakeOutlookFolder("SomeOtherFolder", [wrong_same_entry, right_item])],
    )
    namespace = FakeOutlookNamespace(
        [
            FakeOutlookStore(recorded_pst, recorded_root),
            FakeOutlookStore(archive_pst_mounted, archive_root),
        ]
    )
    dest = tmp_path / "exports" / "relocated.msg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = export_from_pst(
        _base_message(
            recorded_pst,
            message_id=target_id,
            logical_locator="entry-recorded-id",
            folder_path="Inbox",
        ),
        dest,
        outlook_reader=FakeOutlookReader(namespace),
        known_pst_sources=[recorded_pst, archive_pst_old_path, archive_pst_mounted],
    )

    assert result.status == "exported"
    assert dest.read_bytes().endswith(b"entry-new-id")
    assert result.detail == "Found in another mounted PST: archive.pst"

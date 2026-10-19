from __future__ import annotations

from pathlib import Path

import pytest

from mailattic.cli import build_parser
from mailattic.identity import add_self_address
from mailattic.outbound import (
    DraftReplyError,
    DraftReplyResult,
    _get_message_by_rfc822_message_id,
    _prepare_draft_for_send,
    assemble_draft_reply,
)
from mailattic.outbound_commands import _handle_draft_reply
from mailattic.storage import connect_database
from tests.test_outbound_draft import (
    PLACEHOLDER_SMTP_SETTINGS,
    _seed_message,
    _set_message_provenance,
)

SEEDED_MESSAGE_ID = "<test-msg-123@mailattic.test>"


def _add_archive_copy(
    db_path: Path, *, row_id: int, archive_is_bracketed: bool
) -> None:
    """Copy the seeded row as an archive-scraped duplicate stored at ``row_id``.

    The two copies spell the Message-ID with and without angle brackets, which
    is how they coexist under the unique ``message_id`` index while both match
    the anchor lookup -- the shape behind #208's two-row anchors.
    ``archive_is_bracketed`` picks which copy gets the bracketed spelling.
    """
    bracketed = SEEDED_MESSAGE_ID
    bare = SEEDED_MESSAGE_ID.strip("<>")
    archive_spelling, original_spelling = (
        (bracketed, bare) if archive_is_bracketed else (bare, bracketed)
    )
    connection = connect_database(db_path)
    try:
        columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(messages)")
            if row["name"] not in {"id", "message_id", "dedup_key"}
        ]
        column_list = ", ".join(columns)
        connection.execute(
            "UPDATE messages SET message_id = ? WHERE message_id = ?",
            (original_spelling, bracketed),
        )
        connection.execute(
            f"INSERT INTO messages (id, message_id, dedup_key, {column_list}) "
            f"SELECT ?, ?, ?, {column_list} FROM messages WHERE message_id = ?",
            (row_id, archive_spelling, "archive-copy-dedup-key", original_spelling),
        )
        connection.execute(
            "UPDATE messages SET source_input_mode = 'archive', "
            "pst_source = 'archive:mail-archive' WHERE id = ?",
            (row_id,),
        )
        connection.commit()
    finally:
        connection.close()


def _seeded_row_id(db_path: Path) -> int:
    connection = connect_database(db_path)
    try:
        row = connection.execute(
            "SELECT id FROM messages WHERE message_id = ?", (SEEDED_MESSAGE_ID,)
        ).fetchone()
    finally:
        connection.close()
    return int(row["id"])


# The archive copy is placed both below and above the original's id, and gets
# each Message-ID spelling in turn, so the pick cannot be satisfied by
# insertion order, rowid order or index order of the spellings (#208).
ARCHIVE_ROW_IDS = pytest.mark.parametrize(
    "archive_row_id", [0, 1000], ids=["archive-first", "archive-last"]
)
ARCHIVE_SPELLINGS = pytest.mark.parametrize(
    "archive_is_bracketed", [True, False], ids=["archive-bracketed", "archive-bare"]
)


@ARCHIVE_ROW_IDS
@ARCHIVE_SPELLINGS
@pytest.mark.parametrize(
    "requested_id",
    [SEEDED_MESSAGE_ID, SEEDED_MESSAGE_ID.strip("<>")],
    ids=["request-bracketed", "request-bare"],
)
def test_anchor_lookup_prefers_non_archive_copy_of_same_message_id(
    tmp_path: Path,
    archive_row_id: int,
    archive_is_bracketed: bool,
    requested_id: str,
) -> None:
    db_path = _seed_message(tmp_path)
    original_row_id = _seeded_row_id(db_path)
    _add_archive_copy(
        db_path, row_id=archive_row_id, archive_is_bracketed=archive_is_bracketed
    )

    anchor = _get_message_by_rfc822_message_id(db_path, requested_id)

    assert anchor["id"] == original_row_id


@ARCHIVE_ROW_IDS
@ARCHIVE_SPELLINGS
def test_draft_reply_succeeds_when_archive_copy_shares_the_message_id(
    tmp_path: Path, archive_row_id: int, archive_is_bracketed: bool
) -> None:
    db_path = _seed_message(tmp_path)
    _add_archive_copy(
        db_path, row_id=archive_row_id, archive_is_bracketed=archive_is_bracketed
    )

    result = assemble_draft_reply(
        db_path,
        SEEDED_MESSAGE_ID,
        draft_dir=tmp_path / "drafts",
        own_address="sean3808@mailattic.test",
        body_text="reply",
    )

    assert result.to == "client@example.com"


def test_anchor_lookup_falls_back_to_archive_copy_when_it_is_the_only_row(
    tmp_path: Path,
) -> None:
    db_path = _seed_message(tmp_path)
    _set_message_provenance(
        db_path, source_input_mode="archive", pst_source="archive:mail-archive"
    )

    with pytest.raises(DraftReplyError, match="(?i)archive-scraped"):
        assemble_draft_reply(
            db_path,
            SEEDED_MESSAGE_ID,
            draft_dir=tmp_path / "drafts",
            own_address="sean3808@mailattic.test",
            body_text="reply",
        )


@pytest.mark.parametrize("write_error", [PermissionError, FileNotFoundError])
def test_draft_write_failure_propagates_instead_of_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_error: type[OSError]
) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("My reply text", encoding="utf-8")
    # Just enough settings to name the sending account; this test is about
    # write-failure propagation, not SMTP config resolution.
    placeholder_env = tmp_path / "placeholder.env"
    placeholder_env.write_text(PLACEHOLDER_SMTP_SETTINGS, encoding="utf-8")
    args = build_parser().parse_args(
        [
            "draft-reply",
            "<test-msg-123@mailattic.test>",
            "--body-from",
            str(body_file),
            "--database",
            str(db_path),
            "--env-file",
            str(placeholder_env),
        ]
    )

    def refuse_write(self: Path, data: bytes) -> int:
        raise write_error("draft folder is unusable")

    monkeypatch.setattr(Path, "write_bytes", refuse_write)

    with pytest.raises(write_error):
        _handle_draft_reply(args)


def test_archive_anchor_callback_fires_before_a_later_failure(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)
    _set_message_provenance(
        db_path, source_input_mode="archive", pst_source="legacy-export.eml"
    )
    calls: list[str] = []

    with pytest.raises(DraftReplyError, match="Could not read reply body file"):
        assemble_draft_reply(
            db_path,
            "<test-msg-123@mailattic.test>",
            draft_dir=tmp_path / "drafts",
            own_address="sean3808@mailattic.test",
            body_from=tmp_path / "missing-body.txt",
            allow_archive_anchor=True,
            on_archive_anchor=lambda: calls.append("warned"),
        )

    assert calls == ["warned"]
    assert not (tmp_path / "drafts").exists()


def test_assemble_draft_reply_writes_to_custom_draft_dir_and_prepares_for_send(
    tmp_path: Path,
) -> None:
    db_path = _seed_message(tmp_path)
    custom_root = tmp_path / "custom_location"
    custom_draft_dir = custom_root / "outbound" / "drafts"

    result = assemble_draft_reply(
        database_path=db_path,
        message_id="<test-msg-123@mailattic.test>",
        draft_dir=custom_draft_dir,
        body_text="Library assembled reply body.",
        own_address="sean3808@mailattic.test",
    )

    assert isinstance(result, DraftReplyResult)
    expected_eml = custom_draft_dir / f"{result.draft_id}.eml"
    assert result.draft_path == expected_eml
    assert expected_eml.is_file()
    assert not (tmp_path / "outbound" / "drafts").exists()

    # Field assertions
    assert result.subject == "Re: Test subject"
    assert result.to == "client@example.com"
    assert result.bcc == "sean3808@mailattic.test"
    assert result.message_id.startswith("<") and result.message_id.endswith(">")

    # Parse and verify with _prepare_draft_for_send
    dummy_prep_db = custom_root / "dummy.db"
    prepared = _prepare_draft_for_send(result.draft_id, dummy_prep_db)
    assert prepared.draft_id == result.draft_id
    assert prepared.message_id == result.message_id
    assert prepared.msg["Subject"] == "Re: Test subject"
    assert "client@example.com" in str(prepared.msg["To"])
    assert "sean3808@mailattic.test" in str(prepared.msg["Bcc"])


def test_reply_all_excludes_every_declared_self_address(tmp_path: Path) -> None:
    # The sending account is one self address; an alias on the original's Cc
    # is also the operator and must not get a copy of their own reply.
    db_path = _seed_message(
        tmp_path, cc_addresses=["Alias@Example.org", "colleague@example.com"]
    )
    connection = connect_database(db_path)
    try:
        add_self_address(connection, "alias@example.org")
        connection.commit()
    finally:
        connection.close()

    result = assemble_draft_reply(
        db_path,
        SEEDED_MESSAGE_ID,
        draft_dir=tmp_path / "drafts",
        own_address="sean3808@mailattic.test",
        body_text="reply",
    )

    assert result.to == "client@example.com"
    assert result.cc == "colleague@example.com"

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from mailattic.attachment_store import (
    build_attachment_export_filename,
    export_attachments,
    write_attachment,
)
from mailattic.cli import main
from mailattic.models import build_participant_search_text
from mailattic.query import SearchFilters, search_attachments
from mailattic.schema import SCHEMA_SQL


def _seed_date_bound_message_and_attachment(
    connection: sqlite3.Connection,
    *,
    label: str,
    sender_email: str,
    sent_at: str,
    content_hash: str | None = None,
) -> None:
    cursor = connection.execute(
        """
        INSERT INTO messages (
            subject, sender_name, sender_email, sent_at, received_at,
            body_text, message_id, pst_source, folder_path,
            logical_locator, dedup_key, recipient_search_text,
            attachment_search_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            label,
            label,
            sender_email,
            sent_at,
            sent_at,
            f"body for {label}",
            f"<{label}@example.com>",
            "fixture.pst",
            "Inbox",
            f"Inbox/{label}",
            f"dedup-{label}",
            "",
            "",
        ],
    )
    message_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO message_attachments (
            message_id, attachment_name, content_hash, size_bytes,
            mime_type, storage_path
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [message_id, f"{label}.pdf", content_hash, None, "application/pdf", None],
    )


@pytest.fixture
def attachment_date_env(tmp_path: Path) -> Path:
    """A dedicated DB pinning the `attachments` subcommand's own Taipei
    date-bound semantics (design.md whole-day rule), kept separate from the
    CAS/export-mechanics ``attachment_env`` fixture above so the two
    concerns (date filtering vs. export write behavior) don't entangle.
    """
    database_path = tmp_path / "date_bounds.db"
    connection = sqlite3.connect(str(database_path))
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.executescript(SCHEMA_SQL)
        fixtures = [
            ("boundary-start", "start@shopa.example", "2026-07-21T00:30:00+08:00"),
            ("boundary-end", "end@shopa.example", "2026-07-21T23:59:00+08:00"),
            (
                "boundary-utc-cross",
                "utccross@shopa.example",
                "2026-07-20T17:00:00+00:00",
            ),
            ("boundary-before", "before@shopa.example", "2026-07-20T23:30:00+08:00"),
            ("boundary-after", "after@shopa.example", "2026-07-22T00:30:00+08:00"),
            ("afternoon", "afternoon@shopa.example", "2026-07-21T15:56:00+08:00"),
            ("instant-eleven", "eleven@shopa.example", "2026-07-21T11:00:00+08:00"),
            (
                "instant-thirteen",
                "thirteen@shopa.example",
                "2026-07-21T13:00:00+08:00",
            ),
        ]
        for label, sender_email, sent_at in fixtures:
            _seed_date_bound_message_and_attachment(
                connection, label=label, sender_email=sender_email, sent_at=sent_at
            )
        connection.commit()
    finally:
        connection.close()
    return database_path


def _attachments_cli_sender_emails(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> set[str]:
    exit_code = main(argv)
    captured = capsys.readouterr()
    assert exit_code == 0, captured.out
    return {row["sender_email"] for row in json.loads(captured.out)}


def test_attachments_taipei_boundary_table_via_cli(
    attachment_date_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sender_emails = _attachments_cli_sender_emails(
        [
            "attachments",
            "--database",
            str(attachment_date_env),
            "--from-date",
            "2026-07-21",
            "--to-date",
            "2026-07-21",
            "--json",
        ],
        capsys,
    )

    assert "start@shopa.example" in sender_emails
    assert "end@shopa.example" in sender_emails
    assert "utccross@shopa.example" in sender_emails
    assert "before@shopa.example" not in sender_emails
    assert "after@shopa.example" not in sender_emails


def test_attachments_non_canonical_date_forms_match_canonical_form(
    attachment_date_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`attachments` still accepts the basic-date and ISO-week-date forms
    `date.fromisoformat` has always taken -- unchanged by this change's
    render-time coercion revert (design.md > "Keep every input form every
    surface already accepts, and define what each one means")."""

    def _result_for(value: str) -> set[str]:
        return _attachments_cli_sender_emails(
            [
                "attachments",
                "--database",
                str(attachment_date_env),
                "--from-date",
                value,
                "--to-date",
                value,
                "--json",
            ],
            capsys,
        )

    canonical = _result_for("2026-07-21")
    basic = _result_for("20260721")
    week = _result_for("2026-W30-2")

    assert basic == canonical
    assert week == canonical


def test_attachments_upper_bound_admits_local_afternoon_message(
    attachment_date_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sender_emails = _attachments_cli_sender_emails(
        [
            "attachments",
            "--database",
            str(attachment_date_env),
            "--to-date",
            "2026-07-21",
            "--json",
        ],
        capsys,
    )

    assert "afternoon@shopa.example" in sender_emails


def test_attachments_offset_aware_timestamp_bound_selects_by_instant(
    attachment_date_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sender_emails = _attachments_cli_sender_emails(
        [
            "attachments",
            "--database",
            str(attachment_date_env),
            "--from-date",
            "2026-07-21T12:00:00+08:00",
            "--json",
        ],
        capsys,
    )

    assert "eleven@shopa.example" not in sender_emails
    assert "thirteen@shopa.example" in sender_emails


def test_attachments_naive_timestamp_bound_denotes_taipei_instant(
    attachment_date_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _result_for(value: str) -> set[str]:
        return _attachments_cli_sender_emails(
            [
                "attachments",
                "--database",
                str(attachment_date_env),
                "--from-date",
                value,
                "--json",
            ],
            capsys,
        )

    naive_result = _result_for("2026-07-21T12:00:00")
    taipei_result = _result_for("2026-07-21T12:00:00+08:00")
    utc_result = _result_for("2026-07-21T12:00:00+00:00")

    assert naive_result == taipei_result
    assert naive_result != utc_result


def test_attachments_malformed_date_bound_is_rejected_with_diagnostic(
    attachment_date_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "attachments",
            "--database",
            str(attachment_date_env),
            "--from-date",
            "2026-201",
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code != 0
    assert "2026-201" in captured.err
    assert "--from-date" in captured.err


def test_attachments_blank_date_bound_is_unsupplied_filter(
    attachment_date_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code_blank = main(
        [
            "attachments",
            "--database",
            str(attachment_date_env),
            "--from-date",
            "   ",
            "--json",
        ]
    )
    out_blank = capsys.readouterr().out
    exit_code_omitted = main(
        [
            "attachments",
            "--database",
            str(attachment_date_env),
            "--json",
        ]
    )
    out_omitted = capsys.readouterr().out

    assert exit_code_blank == 0
    assert exit_code_omitted == 0
    assert out_blank == out_omitted


def test_attachments_padded_date_bound_is_rejected_not_stripped(
    attachment_date_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A whitespace-padded value must be rejected, exactly as it was before
    ``normalize_optional_date_text`` existed -- ``attachments`` never strips
    before parsing (unlike ``mail_search``, which does)."""
    exit_code = main(
        [
            "attachments",
            "--database",
            str(attachment_date_env),
            "--from-date",
            "  2026-07-21  ",
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code != 0
    assert "--from-date" in captured.err


def test_attachments_widened_taipei_day_range_exports_additional_file(
    attachment_env: FixtureEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """design.md > "A wider result set widens what `bes attachments --copy-to`
    writes, not just what it prints": before this change, `--from-date
    2025-04-20 --to-date 2025-04-20` parsed both bounds as the naive midnight
    instant (the pre-existing "pins the upper bound at midnight" bug), which
    excludes "null-name-older" (sent_at 2025-04-20T09:00:00). The Taipei
    whole-day bound now spans 2025-04-19T16:00:00Z .. 2025-04-20T15:59:59Z,
    which includes it -- one additional exported file.
    """
    export_dir = attachment_env.database_path.parent / "taipei-widen-export"
    exit_code = main(
        [
            "attachments",
            "--database",
            str(attachment_env.database_path),
            "--attachment-root",
            str(attachment_env.attachment_root),
            "--from-date",
            "2025-04-20",
            "--to-date",
            "2025-04-20",
            "--copy-to",
            str(export_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "copied: 1" in captured.out
    exported_files = list(export_dir.iterdir())
    assert len(exported_files) == 1


@dataclass(frozen=True, slots=True)
class FixtureEnv:
    database_path: Path
    attachment_root: Path
    outside_payload_path: Path
    attachment_ids: dict[str, int]
    message_ids: dict[str, int]
    content_hashes: dict[str, str]


@pytest.fixture
def attachment_env(tmp_path: Path) -> FixtureEnv:
    database_path = tmp_path / "evidence.db"
    attachment_root = tmp_path / "attachments"
    outside_payload_path = tmp_path / "evil.pdf"
    outside_payload_path.write_bytes(b"outside-store-payload")

    connection = sqlite3.connect(str(database_path))
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.executescript(SCHEMA_SQL)
        seeded = _seed_attachment_fixture(
            connection=connection,
            attachment_root=attachment_root,
            outside_payload_path=outside_payload_path,
        )
        connection.commit()
    finally:
        connection.close()

    return FixtureEnv(
        database_path=database_path,
        attachment_root=attachment_root,
        outside_payload_path=outside_payload_path,
        attachment_ids=seeded["attachment_ids"],
        message_ids=seeded["message_ids"],
        content_hashes=seeded["content_hashes"],
    )


def test_search_attachments_returns_attachment_rows_and_filters(
    attachment_env: FixtureEnv,
) -> None:
    date_filtered_rows = search_attachments(
        database_path=attachment_env.database_path,
        filters=SearchFilters(
            attachment_ext="pdf",
            sent_from=datetime.fromisoformat("2025-04-20T00:00:00"),
            sent_to=datetime.fromisoformat("2025-04-23T23:59:59"),
        ),
    )

    assert {row["attachment_id"] for row in date_filtered_rows} == {
        attachment_env.attachment_ids["null-name-older"],
        attachment_env.attachment_ids["null-name-newer"],
        attachment_env.attachment_ids["unknown-hash"],
        attachment_env.attachment_ids["null-hash-two"],
    }
    for row in date_filtered_rows:
        assert isinstance(row["message_db_id"], int)
        assert isinstance(row["rfc_message_id"], str)
        assert isinstance(row["dedup_key"], str)
        assert row["carrying_messages"] == [
            {
                "message_db_id": row["message_db_id"],
                "rfc_message_id": row["rfc_message_id"],
                "dedup_key": row["dedup_key"],
                "sent_at": row["sent_at"],
                "sender_email": row["sender_email"],
                "subject": row["subject"],
            }
        ]

    outbound_rows = search_attachments(
        database_path=attachment_env.database_path,
        filters=SearchFilters(
            attachment_name="offer",
            attachment_ext="pdf",
            participants_domain="shopa.example",
            sent_from=datetime.fromisoformat("2025-04-15T00:00:00"),
            sent_to=datetime.fromisoformat("2025-04-15T23:59:59"),
        ),
    )

    assert [row["attachment_id"] for row in outbound_rows] == [
        attachment_env.attachment_ids["outbound-offer"]
    ]


def test_attachment_extension_filter_prefers_name_suffix_over_mime_type(
    attachment_env: FixtureEnv,
) -> None:
    attachment_id = attachment_env.attachment_ids["shared-older"]
    connection = sqlite3.connect(attachment_env.database_path)
    try:
        connection.execute(
            "UPDATE message_attachments SET mime_type = 'text/plain' WHERE id = ?",
            [attachment_id],
        )
        connection.commit()
    finally:
        connection.close()

    pdf_ids = {
        row["attachment_id"]
        for row in search_attachments(
            database_path=attachment_env.database_path,
            filters=SearchFilters(attachment_ext="pdf"),
        )
    }
    txt_ids = {
        row["attachment_id"]
        for row in search_attachments(
            database_path=attachment_env.database_path,
            filters=SearchFilters(attachment_ext="txt"),
        )
    }

    assert attachment_id in pdf_ids
    assert attachment_id not in txt_ids


def test_attachment_extension_filter_ignores_mime_for_name_with_trailing_dot(
    attachment_env: FixtureEnv,
) -> None:
    attachment_id = attachment_env.attachment_ids["shared-older"]
    connection = sqlite3.connect(attachment_env.database_path)
    try:
        connection.execute(
            "UPDATE message_attachments "
            "SET attachment_name = 'report.pdf.', mime_type = 'text/plain' "
            "WHERE id = ?",
            [attachment_id],
        )
        connection.commit()
    finally:
        connection.close()

    pdf_ids = {
        row["attachment_id"]
        for row in search_attachments(
            database_path=attachment_env.database_path,
            filters=SearchFilters(attachment_ext="pdf"),
        )
    }
    txt_ids = {
        row["attachment_id"]
        for row in search_attachments(
            database_path=attachment_env.database_path,
            filters=SearchFilters(attachment_ext="txt"),
        )
    }

    assert attachment_id in pdf_ids
    assert attachment_id not in txt_ids


def test_attachment_extension_filter_matches_compound_suffix(
    attachment_env: FixtureEnv,
) -> None:
    attachment_id = attachment_env.attachment_ids["shared-older"]
    connection = sqlite3.connect(attachment_env.database_path)
    try:
        connection.execute(
            "UPDATE message_attachments "
            "SET attachment_name = 'archive.tar.gz', "
            "mime_type = 'application/octet-stream' WHERE id = ?",
            [attachment_id],
        )
        connection.commit()
    finally:
        connection.close()

    matching_ids = {
        row["attachment_id"]
        for row in search_attachments(
            database_path=attachment_env.database_path,
            filters=SearchFilters(attachment_ext="tar.gz"),
        )
    }

    assert attachment_id in matching_ids


def test_search_attachments_dedupe_content_hash_keeps_newest_and_null_hash_rows(
    attachment_env: FixtureEnv,
) -> None:
    deduped_rows = search_attachments(
        database_path=attachment_env.database_path,
        filters=SearchFilters(
            attachment_ext="pdf",
            participants_domain="shopa.example",
        ),
        dedupe="content-hash",
    )

    shared_hash = attachment_env.content_hashes["shared"]
    shared_row = next(row for row in deduped_rows if row["content_hash"] == shared_hash)
    assert shared_row["attachment_id"] == attachment_env.attachment_ids["shared-newer"]
    assert shared_row["sent_at"] == "2025-04-12T10:00:00"
    carrying_ids = [
        message["message_db_id"] for message in shared_row["carrying_messages"]
    ]
    assert carrying_ids == [
        attachment_env.message_ids["shared-newer"],
        attachment_env.message_ids["shared-older"],
        attachment_env.message_ids["shared-undated"],
    ]

    null_hash_rows = [row for row in deduped_rows if row["content_hash"] is None]
    assert {row["attachment_id"] for row in null_hash_rows} == {
        attachment_env.attachment_ids["unknown-hash"],
        attachment_env.attachment_ids["null-hash-two"],
    }


def test_search_attachments_dedupe_content_hash_picks_absolute_time_not_lexical_order(
    tmp_path: Path,
) -> None:
    """content-hash dedupe must pick the winner by absolute instant (the
    same SQL ``unixepoch()`` convention every other ordering in this module
    uses), not by comparing the raw ``sent_at`` string -- a lexical
    comparison across offsets picks the wrong winner even though the
    ``+00:00`` message is chronologically later (issue #193)."""
    database_path = tmp_path / "dedupe_offset.db"
    connection = sqlite3.connect(str(database_path))
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.executescript(SCHEMA_SQL)
        shared_hash = "a" * 64
        _seed_date_bound_message_and_attachment(
            connection,
            label="older-lexically-first",
            sender_email="older@example.com",
            sent_at="2026-09-12T09:00:00+08:00",  # 01:00Z
            content_hash=shared_hash,
        )
        _seed_date_bound_message_and_attachment(
            connection,
            label="newer-lexically-second",
            sender_email="newer@example.com",
            sent_at="2026-09-12T02:00:00+00:00",  # 02:00Z -- actually later
            content_hash=shared_hash,
        )
        connection.commit()
    finally:
        connection.close()

    deduped = search_attachments(
        database_path=database_path,
        filters=SearchFilters(),
        dedupe="content-hash",
    )

    assert len(deduped) == 1
    winner = deduped[0]
    assert winner["sender_email"] == "newer@example.com"
    carrying_senders = [
        message["sender_email"] for message in winner["carrying_messages"]
    ]
    assert carrying_senders == ["newer@example.com", "older@example.com"]


def test_export_helpers_handle_sanitization_fallbacks_and_containment(
    attachment_env: FixtureEnv,
) -> None:
    copy_dir = attachment_env.database_path.parent / "exports"
    rows = search_attachments(
        database_path=attachment_env.database_path,
        filters=SearchFilters(attachment_ext="pdf"),
    )
    rows_by_id = {int(row["attachment_id"]): row for row in rows}

    result = export_attachments(
        attachment_rows=rows,
        store_root=attachment_env.attachment_root,
        destination_dir=copy_dir,
    )

    assert result.copied == 12
    assert result.skipped_non_exportable == 1
    assert result.skipped_existing == 0
    assert result.failed == 1
    assert any("skipped-non-exportable" in line for line in result.report_lines)
    assert any(
        f"att#{attachment_env.attachment_ids['escaping-path']}" in line
        for line in result.report_lines
    )
    assert attachment_env.outside_payload_path.read_bytes() == b"outside-store-payload"

    reserved_filename = build_attachment_export_filename(
        rows_by_id[attachment_env.attachment_ids["reserved-name"]],
        copy_dir,
    )
    assert reserved_filename.endswith("CON_.pdf")
    assert (copy_dir / reserved_filename).exists()

    illegal_filename = build_attachment_export_filename(
        rows_by_id[attachment_env.attachment_ids["illegal-name"]],
        copy_dir,
    )
    assert "bad_name_" in illegal_filename
    assert (copy_dir / illegal_filename).exists()

    long_filename = build_attachment_export_filename(
        rows_by_id[attachment_env.attachment_ids["long-name"]],
        copy_dir,
    )
    assert len(str(copy_dir / long_filename)) <= 240
    assert long_filename.endswith(".pdf")
    assert re.search(r"_[0-9a-f]{8}\.pdf$", long_filename) is not None
    assert (copy_dir / long_filename).exists()

    unknown_date_filename = build_attachment_export_filename(
        rows_by_id[attachment_env.attachment_ids["unknown-date"]],
        copy_dir,
    )
    assert unknown_date_filename.startswith(
        f"unknown-date_att{attachment_env.attachment_ids['unknown-date']}_"
    )
    assert "XA_shopa" in unknown_date_filename
    assert (copy_dir / unknown_date_filename).exists()

    null_name_older_filename = build_attachment_export_filename(
        rows_by_id[attachment_env.attachment_ids["null-name-older"]],
        copy_dir,
    )
    null_name_newer_filename = build_attachment_export_filename(
        rows_by_id[attachment_env.attachment_ids["null-name-newer"]],
        copy_dir,
    )
    assert null_name_older_filename != null_name_newer_filename
    assert (
        f"att{attachment_env.attachment_ids['null-name-older']}"
        in null_name_older_filename
    )
    assert (
        f"att{attachment_env.attachment_ids['null-name-newer']}"
        in null_name_newer_filename
    )
    assert (copy_dir / null_name_older_filename).exists()
    assert (copy_dir / null_name_newer_filename).exists()

    unknown_hash_filename = build_attachment_export_filename(
        rows_by_id[attachment_env.attachment_ids["unknown-hash"]],
        copy_dir,
    )
    assert "unknown-hash.pdf" in unknown_hash_filename
    assert (copy_dir / unknown_hash_filename).exists()

    escaping_filename = build_attachment_export_filename(
        rows_by_id[attachment_env.attachment_ids["escaping-path"]],
        copy_dir,
    )
    assert not (copy_dir / escaping_filename).exists()


def test_export_fallback_extension_uses_metadata_not_storage_path(
    tmp_path: Path,
) -> None:
    digest = "a" * 64
    pdf_name = build_attachment_export_filename(
        {
            "attachment_id": 1,
            "attachment_name": "attachment",
            "content_hash": digest,
            "mime_type": "application/pdf",
            "storage_path": f"attachments/aa/aa/{digest}.dat",
            "sent_at": "2026-08-11T12:00:00+08:00",
        },
        tmp_path,
    )
    unknown_name = build_attachment_export_filename(
        {
            "attachment_id": 2,
            "attachment_name": "",
            "content_hash": digest,
            "mime_type": "application/x-mailattic-unknown",
            "storage_path": f"attachments/aa/aa/{digest}.pdf",
            "sent_at": "2026-08-11T12:00:00+08:00",
        },
        tmp_path,
    )

    assert pdf_name.endswith(f"_{digest}.pdf")
    assert unknown_name.endswith(f"_{digest}.bin")


def test_attachments_cli_export_exit_codes_and_skip_existing(
    attachment_env: FixtureEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = search_attachments(
        database_path=attachment_env.database_path,
        filters=SearchFilters(attachment_name="bad", attachment_ext="pdf"),
    )
    assert len(rows) == 1
    row = rows[0]

    collision_dir = attachment_env.database_path.parent / "collision-export"
    collision_dir.mkdir()
    collision_filename = build_attachment_export_filename(row, collision_dir)
    (collision_dir / collision_filename).write_bytes(b"existing")

    exit_code = main(
        [
            "attachments",
            "--database",
            str(attachment_env.database_path),
            "--attachment-root",
            str(attachment_env.attachment_root),
            "--name-contains",
            "bad",
            "--ext",
            "pdf",
            "--copy-to",
            str(collision_dir),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "failed: 1" in captured.out

    skip_dir = attachment_env.database_path.parent / "skip-export"
    skip_dir.mkdir()
    skip_filename = build_attachment_export_filename(row, skip_dir)
    (skip_dir / skip_filename).write_bytes(b"existing")

    exit_code = main(
        [
            "attachments",
            "--database",
            str(attachment_env.database_path),
            "--attachment-root",
            str(attachment_env.attachment_root),
            "--name-contains",
            "bad",
            "--ext",
            "pdf",
            "--copy-to",
            str(skip_dir),
            "--skip-existing",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "skipped-existing: 1" in captured.out
    assert "failed: 0" in captured.out

    exit_code = main(
        [
            "attachments",
            "--database",
            str(attachment_env.database_path),
            "--attachment-root",
            str(attachment_env.attachment_root),
            "--name-contains",
            "bad",
            "--ext",
            "pdf",
            "--copy-to",
            str(skip_dir),
            "--overwrite",
            "--skip-existing",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "conflicting flags" in captured.err


def _seed_attachment_fixture(
    *,
    connection: sqlite3.Connection,
    attachment_root: Path,
    outside_payload_path: Path,
) -> dict[str, dict[str, object]]:
    message_ids: dict[str, int] = {}
    attachment_ids: dict[str, int] = {}
    content_hashes: dict[str, str] = {}

    def add_message(
        label: str,
        *,
        subject: str,
        sender_email: str,
        sent_at: str | None,
        recipient_emails: list[str],
    ) -> int:
        # Mirror production: `recipient_search_text` is a space-joined,
        # display-name-including projection built the same way
        # `NormalizedMessage.recipient_search_text` builds it -- not the
        # semicolon-delimited raw email list production never stores.
        recipients = [
            (email.split("@", 1)[0].title(), email) for email in recipient_emails
        ]
        recipient_search_text = " ".join(
            build_participant_search_text(name, email) for name, email in recipients
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
                pst_source,
                folder_path,
                logical_locator,
                dedup_key,
                recipient_search_text,
                attachment_search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                subject,
                sender_email.split("@", 1)[0].title(),
                sender_email,
                sent_at,
                sent_at,
                f"Body for {subject}",
                f"<{label}@example.com>",
                "fixture.pst",
                "Inbox",
                f"Inbox/{label}",
                f"dedup-{label}",
                recipient_search_text,
                "",
            ],
        )
        message_id = int(cursor.lastrowid)
        for name, email in recipients:
            connection.execute(
                "INSERT INTO message_recipients "
                "(message_id, recipient_type, display_name, email) "
                "VALUES (?, 'to', ?, ?)",
                [message_id, name, email],
            )
        message_ids[label] = message_id
        return message_id

    def add_attachment(
        label: str,
        *,
        message_label: str,
        attachment_name: str,
        payload: bytes | None,
        mime_type: str = "application/pdf",
        content_hash_override: str | None | object = _UNSET,
        storage_path_override: str | None | object = _UNSET,
    ) -> int:
        record = None
        if payload is not None:
            record = write_attachment(
                payload=payload,
                original_name=attachment_name or "attachment.pdf",
                mime_type=mime_type,
                store_root=attachment_root,
            )

        content_hash = record.content_hash if record is not None else None
        if content_hash_override is not _UNSET:
            content_hash = content_hash_override
        storage_path = record.storage_path if record is not None else None
        if storage_path_override is not _UNSET:
            storage_path = storage_path_override
        size_bytes = len(payload) if payload is not None else None

        cursor = connection.execute(
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
                message_ids[message_label],
                attachment_name,
                content_hash,
                size_bytes,
                mime_type,
                storage_path,
            ],
        )
        attachment_id = int(cursor.lastrowid)
        attachment_ids[label] = attachment_id
        if record is not None and label in {"shared-older", "null-name-older"}:
            key = "shared" if label == "shared-older" else "null-name"
            content_hashes[key] = record.content_hash
        return attachment_id

    shared_payload = b"%PDF shared payload"
    null_name_payload = b"%PDF null name payload"

    add_message(
        "shared-older",
        subject="Older TAG shared",
        sender_email="older@shopa.example",
        sent_at="2025-04-10T09:00:00",
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "shared-older",
        message_label="shared-older",
        attachment_name="TAG_shared_XA_EN.pdf",
        payload=shared_payload,
    )

    add_message(
        "shared-newer",
        subject="Newer TAG shared",
        sender_email="newer@shopa.example",
        sent_at="2025-04-12T10:00:00",
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "shared-newer",
        message_label="shared-newer",
        attachment_name="TAG_shared_XA_EN.pdf",
        payload=shared_payload,
    )

    add_message(
        "shared-undated",
        subject="Undated TAG shared",
        sender_email="undated@shopa.example",
        sent_at=None,
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "shared-undated",
        message_label="shared-undated",
        attachment_name="TAG_shared_XA_EN.pdf",
        payload=shared_payload,
    )

    add_message(
        "outbound-offer",
        subject="Outbound TAG offer",
        sender_email="sean3808@mailattic.test",
        sent_at="2025-04-15T11:00:00",
        recipient_emails=["buyer@shopa.example", "lab@example.com"],
    )
    add_attachment(
        "outbound-offer",
        message_label="outbound-offer",
        attachment_name="TAG_offer_XB_EN.pdf",
        payload=b"%PDF outbound offer",
    )

    add_message(
        "metadata-only",
        subject="Metadata-only TAG",
        sender_email="metadata@archive.example",
        sent_at="2025-04-16T12:00:00",
        recipient_emails=["ops@archive.example"],
    )
    add_attachment(
        "metadata-only",
        message_label="metadata-only",
        attachment_name="TAG_meta_XA_shopa.pdf",
        payload=None,
        mime_type="application/pdf",
        content_hash_override=None,
        storage_path_override=None,
    )

    add_message(
        "reserved-name",
        subject="Reserved filename",
        sender_email="reserved@shopa.example",
        sent_at="2025-04-17T09:00:00",
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "reserved-name",
        message_label="reserved-name",
        attachment_name="CON.pdf",
        payload=b"%PDF reserved",
    )

    add_message(
        "long-name",
        subject="Long TAG filename",
        sender_email="long@shopa.example",
        sent_at="2025-04-18T09:30:00",
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "long-name",
        message_label="long-name",
        attachment_name="TAG_" + ("X" * 230) + ".pdf",
        payload=b"%PDF long name",
    )

    add_message(
        "unknown-date",
        subject="Unknown date TAG",
        sender_email="unknown@shopa.example",
        sent_at=None,
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "unknown-date",
        message_label="unknown-date",
        attachment_name="TAG_unknown_XA_shopa.pdf",
        payload=b"%PDF unknown date",
    )

    add_message(
        "escaping-path",
        subject="Escaping path TAG",
        sender_email="escape@shopa.example",
        sent_at="2025-04-19T08:00:00",
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "escaping-path",
        message_label="escaping-path",
        attachment_name="TAG_escape.pdf",
        payload=None,
        content_hash_override="escapehash",
        storage_path_override="attachments/../evil.pdf",
    )

    add_message(
        "null-name-older",
        subject="Older null-name",
        sender_email="nullname1@shopa.example",
        sent_at="2025-04-20T09:00:00",
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "null-name-older",
        message_label="null-name-older",
        attachment_name="",
        payload=null_name_payload,
    )

    add_message(
        "null-name-newer",
        subject="Newer null-name",
        sender_email="nullname2@shopa.example",
        sent_at="2025-04-21T09:00:00",
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "null-name-newer",
        message_label="null-name-newer",
        attachment_name="",
        payload=null_name_payload,
    )

    add_message(
        "unknown-hash",
        subject="Unknown hash fallback",
        sender_email="unknownhash@shopa.example",
        sent_at="2025-04-22T09:00:00",
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "unknown-hash",
        message_label="unknown-hash",
        attachment_name="",
        payload=b"%PDF unknown hash",
        content_hash_override=None,
    )

    add_message(
        "null-hash-two",
        subject="Null hash retained",
        sender_email="nullhash2@shopa.example",
        sent_at="2025-04-23T09:00:00",
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "null-hash-two",
        message_label="null-hash-two",
        attachment_name="TAG_nullhash2.pdf",
        payload=b"%PDF second null hash",
        content_hash_override=None,
    )

    add_message(
        "illegal-name",
        subject="Illegal char filename",
        sender_email="illegal@shopa.example",
        sent_at="2025-04-24T09:00:00",
        recipient_emails=["sean3808@mailattic.test"],
    )
    add_attachment(
        "illegal-name",
        message_label="illegal-name",
        attachment_name="TAG_bad<name>.pdf",
        payload=b"%PDF illegal",
    )

    assert outside_payload_path.exists()
    assert content_hashes["shared"]
    assert content_hashes["null-name"]

    return {
        "attachment_ids": attachment_ids,
        "message_ids": message_ids,
        "content_hashes": content_hashes,
    }


_UNSET = object()

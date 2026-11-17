from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from email.message import EmailMessage
from hashlib import sha256
from pathlib import Path

from mailattic.ingest import _matches_stored_dedup_key
from mailattic.models import (
    MailboxParticipant,
    MaildirSource,
    NormalizedMessage,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

_TAIPEI = timezone(timedelta(hours=8))
_EST = timezone(timedelta(hours=-5))


def _independent_legacy_fallback_key(
    *,
    body_text: str,
    sender_search_text: str,
    sent_at_text: str,
    subject: str,
) -> str:
    payload = {
        "body_sha256": sha256(body_text.encode("utf-8")).hexdigest(),
        "sender": sender_search_text,
        "sent_at": sent_at_text,
        "subject": subject,
    }
    digest = sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"fallback:{digest}"


def _make_no_id_message(
    sent_at: datetime | None,
    *,
    message_id: str | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        subject="Synthetic quote inquiry",
        sender=MailboxParticipant(
            display_name="Alex Example",
            email="alex@example.com",
        ),
        to_recipients=(
            MailboxParticipant(
                display_name="Pat Example",
                email="pat@example.com",
            ),
        ),
        cc_recipients=(),
        bcc_recipients=(),
        sent_at=sent_at,
        received_at=sent_at,
        body_text="Hello,\nPlease see the synthetic test message body.\n",
        attachments=(),
        message_id=message_id,
        in_reply_to=None,
        references=None,
        pst_source="synthetic.pst",
        folder_path="Inbox",
        logical_locator="synthetic-1",
        source_input_mode="file",
        source_requested_from="synthetic.pst",
    )


def test_fallback_dedup_key_same_instant_different_offsets_match() -> None:
    instant_utc = datetime(2026, 5, 1, 4, 0, 0, tzinfo=UTC)
    instant_taipei = instant_utc.astimezone(_TAIPEI)
    assert instant_utc == instant_taipei

    msg_taipei = _make_no_id_message(instant_taipei)
    msg_utc = _make_no_id_message(instant_utc)

    assert msg_taipei.dedup_key == msg_utc.dedup_key


def _insert_legacy_message(
    connection,
    *,
    sent_at_text: str,
    pst_source: str,
    locator: str,
    source_input_mode: str,
    source_requested_from: str,
    dedup_key: str,
) -> None:
    connection.execute(
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
            "Synthetic quote inquiry",
            "Alex Example",
            "alex@example.com",
            sent_at_text,
            sent_at_text,
            "Hello,\nPlease see the synthetic test message body.\n",
            None,
            None,
            None,
            pst_source,
            "Inbox",
            locator,
            source_input_mode,
            source_requested_from,
            dedup_key,
            "{}",
            "Pat Example pat@example.com",
            "",
            None,
        ],
    )
    connection.commit()


def test_persist_message_matches_legacy_taipei_key_when_utc_arrives(
    tmp_path: Path,
) -> None:
    instant_utc = datetime(2026, 5, 1, 4, 0, 0, tzinfo=UTC)
    instant_taipei = instant_utc.astimezone(_TAIPEI)
    msg_utc = _make_no_id_message(instant_utc)

    legacy_taipei_key = _independent_legacy_fallback_key(
        body_text=msg_utc.body_text,
        sender_search_text="Alex Example alex@example.com",
        sent_at_text=instant_taipei.isoformat(),
        subject=msg_utc.subject,
    )
    assert legacy_taipei_key != msg_utc.dedup_key

    database_path = initialize_database(tmp_path / "legacy_dedup.db")
    connection = connect_database(database_path)
    try:
        _insert_legacy_message(
            connection,
            sent_at_text=instant_taipei.isoformat(),
            pst_source=msg_utc.pst_source,
            locator=msg_utc.logical_locator,
            source_input_mode=msg_utc.source_input_mode,
            source_requested_from=msg_utc.source_requested_from,
            dedup_key=legacy_taipei_key,
        )

        result = persist_message(connection, msg_utc)
        assert result.inserted is False
        row_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert row_count == 1
    finally:
        connection.close()


def test_dedup_key_candidates_order_and_message_id_single_key() -> None:
    instant_utc = datetime(2026, 5, 1, 4, 0, 0, tzinfo=UTC)
    instant_taipei = instant_utc.astimezone(_TAIPEI)
    instant_est = instant_utc.astimezone(_EST)

    def _key_for(dt: datetime) -> str:
        return _independent_legacy_fallback_key(
            body_text="Hello,\nPlease see the synthetic test message body.\n",
            sender_search_text="Alex Example alex@example.com",
            sent_at_text=dt.isoformat(),
            subject="Synthetic quote inquiry",
        )

    key_utc = _key_for(instant_utc)
    key_taipei = _key_for(instant_taipei)
    key_est = _key_for(instant_est)

    # +08:00 input: UTC first, then +08:00 (deduped with Taipei)
    assert _make_no_id_message(instant_taipei).dedup_key_candidates == (
        key_utc,
        key_taipei,
    )
    # UTC input: UTC first, then Taipei +08:00
    assert _make_no_id_message(instant_utc).dedup_key_candidates == (
        key_utc,
        key_taipei,
    )
    # -05:00 input: UTC first, then own -05:00, then Taipei +08:00
    assert _make_no_id_message(instant_est).dedup_key_candidates == (
        key_utc,
        key_est,
        key_taipei,
    )
    # Message-ID message: single key regardless of offset
    msg_with_id = _make_no_id_message(instant_est, message_id="<id@example.com>")
    assert msg_with_id.dedup_key_candidates == ("message-id:<id@example.com>",)


def test_matches_stored_dedup_key_accepts_legacy_taipei_key(tmp_path: Path) -> None:
    instant_utc = datetime(2026, 5, 1, 4, 0, 0, tzinfo=UTC)
    instant_taipei = instant_utc.astimezone(_TAIPEI)

    eml = EmailMessage()
    eml["From"] = "Alex Example <alex@example.com>"
    eml["To"] = "Pat Example <pat@example.com>"
    eml["Subject"] = "Synthetic quote inquiry"
    eml["Date"] = "Fri, 01 May 2026 04:00:00 +0000"
    eml.set_content("Hello,\nPlease see the synthetic test message body.\n")

    maildir_root = tmp_path / "maildir"
    new_dir = maildir_root / "Inbox" / "new"
    new_dir.mkdir(parents=True)
    eml_path = new_dir / "1714536000.msg.eml"
    eml_path.write_bytes(eml.as_bytes())

    source = MaildirSource(
        path=maildir_root,
        account_email="pat@example.com",
        account_name="pat",
        folder_name="Inbox",
        requested_from=maildir_root,
    )
    pst_source = "imap:pat@example.com"
    locator = "Inbox/1714536000.msg.eml"

    legacy_taipei_key = _independent_legacy_fallback_key(
        body_text="Hello,\nPlease see the synthetic test message body.",
        sender_search_text="Alex Example alex@example.com",
        sent_at_text=instant_taipei.isoformat(),
        subject="Synthetic quote inquiry",
    )

    database_path = initialize_database(tmp_path / "matches_legacy.db")
    connection = connect_database(database_path)
    try:
        _insert_legacy_message(
            connection,
            sent_at_text=instant_taipei.isoformat(),
            pst_source=pst_source,
            locator=locator,
            source_input_mode="maildir",
            source_requested_from=str(maildir_root),
            dedup_key=legacy_taipei_key,
        )

        assert (
            _matches_stored_dedup_key(connection, source, eml_path, pst_source, locator)
            is True
        )
    finally:
        connection.close()

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from mailattic.models import MailboxParticipant, NormalizedMessage

_TAIPEI = timezone(timedelta(hours=8))


def _make_no_id_message(sent_at: datetime) -> NormalizedMessage:
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
        message_id=None,
        in_reply_to=None,
        references=None,
        pst_source="synthetic.pst",
        folder_path="Inbox",
        logical_locator="synthetic-1",
        source_input_mode="file",
        source_requested_from="synthetic.pst",
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#85 fallback dedup key uses sent_at.isoformat(), so the same instant "
        "written with different UTC offsets yields different dedup_key values"
    ),
)
def test_fallback_dedup_key_same_instant_different_offsets_match() -> None:
    instant_utc = datetime(2026, 5, 1, 4, 0, 0, tzinfo=UTC)
    instant_taipei = instant_utc.astimezone(_TAIPEI)
    assert instant_utc == instant_taipei

    msg_taipei = _make_no_id_message(instant_taipei)
    msg_utc = _make_no_id_message(instant_utc)

    assert msg_taipei.dedup_key == msg_utc.dedup_key

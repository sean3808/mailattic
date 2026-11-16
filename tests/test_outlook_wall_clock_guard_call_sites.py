from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mailattic import outlook_pst
from mailattic.original_export import _verify_outlook_item
from mailattic.outbound import build_quoted_reply_bodies


def _sample_message_without_id() -> dict[str, object]:
    return {
        "message_id": None,
        "subject": "Wall clock check",
        "sender_email": "alice@example.com",
        "sent_at": "2026-01-21T14:48:00+08:00",
    }


def _sample_outlook_item() -> SimpleNamespace:
    return SimpleNamespace(
        Subject="Wall clock check",
        SenderEmailType="SMTP",
        SenderEmailAddress="alice@example.com",
        SentOn=datetime(2026, 1, 21, 14, 48, tzinfo=UTC),
    )


def _sample_original_for_reply() -> dict[str, object]:
    return {
        "sender_name": "Alice Example",
        "sender_email": "alice@example.com",
        "sent_at": "2026-01-21T14:48:00+08:00",
        "to": ["bob@example.com"],
        "cc": [],
        "subject": "Wall clock check",
        "body_text": "Original message body",
    }


def test_verify_outlook_item_succeeds_when_machine_offset_is_utc_plus_8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        outlook_pst, "get_machine_utc_offset", lambda: timedelta(hours=8)
    )
    assert _verify_outlook_item(_sample_outlook_item(), _sample_message_without_id())


@pytest.mark.parametrize(
    ("simulated_offset", "expected_label"),
    [
        (timedelta(hours=9), "UTC+09:00"),
        (timedelta(0), "UTC+00:00"),
    ],
)
def test_verify_outlook_item_rejects_non_taipei_machine_offset(
    monkeypatch: pytest.MonkeyPatch,
    simulated_offset: timedelta,
    expected_label: str,
) -> None:
    monkeypatch.setattr(outlook_pst, "get_machine_utc_offset", lambda: simulated_offset)
    with pytest.raises(RuntimeError, match=expected_label.replace("+", r"\+")):
        _verify_outlook_item(_sample_outlook_item(), _sample_message_without_id())


def test_build_quoted_reply_bodies_succeeds_when_machine_offset_is_utc_plus_8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        outlook_pst, "get_machine_utc_offset", lambda: timedelta(hours=8)
    )
    plain_body, html_body = build_quoted_reply_bodies(
        _sample_original_for_reply(),
        "Reply text",
    )
    assert "Sent:" in plain_body
    assert "Reply text" in html_body


@pytest.mark.parametrize(
    ("simulated_offset", "expected_label"),
    [
        (timedelta(hours=9), "UTC+09:00"),
        (timedelta(0), "UTC+00:00"),
    ],
)
def test_build_quoted_reply_bodies_rejects_non_taipei_machine_offset(
    monkeypatch: pytest.MonkeyPatch,
    simulated_offset: timedelta,
    expected_label: str,
) -> None:
    monkeypatch.setattr(outlook_pst, "get_machine_utc_offset", lambda: simulated_offset)
    with pytest.raises(RuntimeError, match=expected_label.replace("+", r"\+")):
        build_quoted_reply_bodies(_sample_original_for_reply(), "Reply text")

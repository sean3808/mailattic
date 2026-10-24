"""Sender selectors must see through a packed ``Name<addr>`` sender (#23)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailattic.contact_history import aggregate_contact_history
from mailattic.identity import add_self_address
from mailattic.models import PstSource, build_normalized_message
from mailattic.query import SearchFilters, search_messages
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

_SOURCE = PstSource(
    path=Path("C:/mail/archive.pst"),
    input_mode="file",
    requested_from=Path("C:/mail/archive.pst"),
)
_OWNER = "owner@mailattic.test"

# (message_id tag, stored sender_email)
_SENDERS = [
    ("wrapped", "Vendor<vendor@client.example>"),
    ("plain", "vendor@client.example"),
    ("spaced", "Vendor Ltd <Vendor@Client.Example> "),
    ("evil_suffix", "Vendor<vendor@evilclient.example>"),
    ("evil_tld", "Vendor<vendor@client.example.attacker.test>"),
    ("subdomain", "Vendor<vendor@mail.client.example>"),
    ("name_trick", "client.example<x@other.test>"),
    ("name_trick_plain", "Mallory@client.example <x@other.test>"),
]
_EXPECTED = {"wrapped", "plain", "spaced"}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = initialize_database(tmp_path / "t.db")
    conn = connect_database(path)
    try:
        add_self_address(conn, _OWNER)
        for index, (tag, sender) in enumerate(_SENDERS):
            msg = build_normalized_message(
                {
                    "subject": f"s-{tag}",
                    "sender": {"email": sender},
                    "to": [{"email": _OWNER}],
                    "sent_at": "2026-01-02T10:00:00",
                    "body_text": "body",
                    "message_id": f"<{tag}@test>",
                },
                source=_SOURCE,
                folder_path="Inbox",
                logical_locator=f"Inbox/{index}",
            )
            storage_persist_message(conn, msg)
        conn.commit()
    finally:
        conn.close()
    return path


def _subjects(db: Path, **filters: str) -> set[str]:
    rows = search_messages(
        database_path=db, filters=SearchFilters(**filters), limit=100
    )
    return {str(r["subject"]).removeprefix("s-") for r in rows}


def test_sender_domain_sees_packed_sender(db: Path) -> None:
    assert _subjects(db, sender_domain="client.example") == _EXPECTED


def test_participants_domain_sees_packed_sender(db: Path) -> None:
    assert _subjects(db, participants_domain="client.example") == _EXPECTED


def test_contact_history_contact_sees_packed_sender(db: Path) -> None:
    summary = aggregate_contact_history(
        database_path=db, contact="vendor@client.example"
    )
    assert summary["total_messages"] == len(_EXPECTED)


def test_contact_history_domain_sees_packed_sender(db: Path) -> None:
    summary = aggregate_contact_history(database_path=db, domain="client.example")
    assert summary["total_messages"] == len(_EXPECTED)

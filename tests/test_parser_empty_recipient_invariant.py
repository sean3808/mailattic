"""Regression tests for issue #64: no recipient may reach storage without an
email address.

`email.policy.default` emits address entries with no email address for several
header shapes, and which shapes it does that for has changed between CPython
3.13 patch releases (3.13.12 keeps a bare semicolon list truncated to its first
address; 3.13.14 returns a second, empty entry for the same header). These
tests lock our own interface shape instead of the interpreter's current quirk:
whatever the parse produced, an entry without an email address never becomes a
`message_recipients` row.
"""

from __future__ import annotations

import sqlite3
from email import policy
from email.parser import BytesParser
from pathlib import Path

from mailattic.models import PstSource, build_normalized_message
from mailattic.parser import (
    _drop_addresses_without_email,
    _parse_email_message,
)
from mailattic.storage import (
    connect_database,
    initialize_database,
    persist_message,
)

_DUMMY_SOURCE = PstSource(
    path=Path("dummy.pst"), input_mode="file", requested_from=Path("dummy.pst")
)

# A display name with an empty angle-addr. Chosen because it is the shape that
# survives models._coerce_participants (which only drops entries whose name AND
# email are both empty) and therefore actually reaches the database. Verified to
# parse this way on CPython 3.11 through 3.13.
_NAMED_EMPTY_TO_HEADER = "Alice <>, bob@y.com"


def _build_raw_message(to_header: str) -> bytes:
    return (
        "From: Alice <alice@example.com>\r\n"
        f"To: {to_header}\r\n"
        "Message-ID: <invariant-probe@example.com>\r\n"
        "Subject: test message\r\n"
        "Date: Mon, 1 Jan 2024 00:00:00 +0800\r\n"
        "\r\n"
        "Body text\r\n"
    ).encode()


def _parse_to(to_header: str, *, raw_bytes_override: object = "__use_default__"):
    raw_bytes = _build_raw_message(to_header)
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    effective_raw_bytes = (
        raw_bytes if raw_bytes_override == "__use_default__" else raw_bytes_override
    )
    return _parse_email_message(message, raw_bytes=effective_raw_bytes)["to"]


def _persist_and_read_recipients(
    to_header: str, database_path: Path
) -> list[sqlite3.Row]:
    raw_bytes = _build_raw_message(to_header)
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    normalized = build_normalized_message(
        _parse_email_message(message, raw_bytes=raw_bytes),
        source=_DUMMY_SOURCE,
        folder_path="INBOX",
        logical_locator="invariant-probe.eml",
    )

    initialize_database(database_path)
    connection = connect_database(database_path)
    try:
        persist_message(connection, normalized)
        connection.commit()
        return list(
            connection.execute(
                "SELECT recipient_type, display_name, email FROM message_recipients"
            )
        )
    finally:
        connection.close()


def test_named_address_without_email_never_reaches_message_recipients(
    tmp_path: Path,
) -> None:
    """A `Name <>` entry is dropped; the real address beside it is kept.

    Reverse verification: neutralize `_drop_addresses_without_email` (return
    its argument unchanged) and this test fails on the row count and on the
    empty-email assertion — the entry survives `_coerce_participants` because
    it carries a display name, and lands in `message_recipients` with a NULL
    email.
    """
    rows = _persist_and_read_recipients(
        _NAMED_EMPTY_TO_HEADER, tmp_path / "invariant.db"
    )

    assert [(row["recipient_type"], row["email"]) for row in rows] == [
        ("to", "bob@y.com")
    ]
    assert all((row["email"] or "").strip() for row in rows)


def test_parse_drops_email_less_entries_from_the_to_header() -> None:
    """The parser output itself already holds the invariant."""
    recipients = _parse_to(_NAMED_EMPTY_TO_HEADER)

    assert recipients == [{"name": None, "email": "bob@y.com"}]


def test_drop_addresses_without_email_filters_every_empty_shape() -> None:
    """Interpreter-independent unit check on the filter itself.

    The input is synthetic rather than parsed, so this test states the contract
    without depending on which header shapes the current CPython patch release
    mangles: `None`, empty string, and whitespace-only are all "no address",
    with or without a display name.
    """
    addresses: list[dict[str, str | None]] = [
        {"name": None, "email": "keep@x.com"},
        {"name": "Alice", "email": None},
        {"name": None, "email": None},
        {"name": "Bob", "email": ""},
        {"name": None, "email": "   "},
        {"name": "Carol", "email": "carol@y.com"},
    ]

    assert _drop_addresses_without_email(addresses) == [
        {"name": None, "email": "keep@x.com"},
        {"name": "Carol", "email": "carol@y.com"},
    ]

"""Operator-declared self addresses (issue #168).

Which email addresses mean "me" is an operator setting, not a code
constant: the set lives in the ``self_addresses`` table, is maintained by
``bes identity``, and is read by every consumer (CLI / MCP / Python API)
through :func:`resolve_self_addresses` -- one definition, no second
implementation.

An absent table (a database created before this table existed) or an empty
one means "not declared": there is no built-in address to fall back to
(issue #268), so nothing is classified as sent by the operator until
``bes identity add`` has run.
"""

from __future__ import annotations

import re
import sqlite3

# Matches the trailing `<address>` of a `Display Name<address>` sender value.
# Real mail can carry sender values stored this way (issue #169) --
# exact string comparison against such a row always misses.
_ANGLE_ADDRESS_RE = re.compile(r"<([^<>]*)>\s*$")


def normalise_address(value: str) -> str:
    """Return *value* trimmed and lowercased, rejecting non-addresses.

    Stored addresses are compared with ``LOWER(sender_email)``, so the
    normalisation has to happen on the way in -- otherwise ``Sean@X`` and
    ``sean@x`` become two rows that behave differently.
    """
    address = value.strip().lower()
    # rpartition, not partition: a quoted local part may legally contain '@'
    # (`"a@b"@example.com`), and real mail can carry those.
    local, separator, domain = address.rpartition("@")
    if not local or not separator or not domain or any(c.isspace() for c in address):
        raise ValueError(f"Not an email address: {value!r}")
    return address


def _table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'self_addresses'"
    ).fetchone()
    return row is not None


def list_self_addresses(connection: sqlite3.Connection) -> list[str]:
    """Return the stored addresses (empty list when unset or pre-migration)."""
    if not _table_exists(connection):
        return []
    rows = connection.execute(
        "SELECT address FROM self_addresses ORDER BY address"
    ).fetchall()
    return [row[0] for row in rows]


def resolve_self_addresses(connection: sqlite3.Connection) -> frozenset[str]:
    """Return the declared self-address set for *connection*, lowercased.

    Empty when nothing is declared. Callers compare against
    ``LOWER(sender_email)``, so the lowercasing is guaranteed here rather
    than trusted to hold: ``normalise_address`` already lowercases
    everything written through ``bes identity``, but a row inserted by hand
    would otherwise never match and silently classify as inbound.
    """
    return frozenset(a.lower() for a in list_self_addresses(connection))


def extract_address(value: str) -> str:
    """Return the bare, lowercased email portion of a raw sender value.

    Handles the ``Display Name<email>`` shape (angle brackets win when
    present); a plain address is just trimmed and lowercased.
    """
    match = _ANGLE_ADDRESS_RE.search(value)
    address = match.group(1) if match else value
    return address.strip().lower()


def compute_direction(
    sender_email: str | None, self_addresses: frozenset[str]
) -> str | None:
    """Return ``"outbound"`` / ``"inbound"`` for *sender_email*, or ``None``.

    ``None`` only when the sender is missing -- direction is never guessed
    (issue #169). A sender in *self_addresses* (already normalised, see
    :func:`resolve_self_addresses`) is ``"outbound"``, including a message
    the operator sent to themselves.
    """
    if not sender_email or not sender_email.strip():
        return None
    return "outbound" if extract_address(sender_email) in self_addresses else "inbound"


def add_self_address(connection: sqlite3.Connection, value: str) -> bool:
    """Declare *value* as self. Returns False when it was already declared."""
    address = normalise_address(value)
    cursor = connection.execute(
        "INSERT OR IGNORE INTO self_addresses (address) VALUES (?)", (address,)
    )
    return cursor.rowcount > 0


def remove_self_address(connection: sqlite3.Connection, value: str) -> bool:
    """Undeclare *value*. Returns False when it was not declared."""
    address = normalise_address(value)
    cursor = connection.execute(
        "DELETE FROM self_addresses WHERE address = ?", (address,)
    )
    return cursor.rowcount > 0

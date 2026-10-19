"""Operator-declared self addresses (issue #168).

Covers the three things the ticket actually promises: the ``bes identity``
subcommand works, an unset set resolves to nothing (no built-in address,
#268), and ``contact-history`` reclassifies once an address is declared.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from mailattic.cli import main
from mailattic.contact_history import aggregate_contact_history
from mailattic.identity import (
    add_self_address,
    list_self_addresses,
    normalise_address,
    remove_self_address,
    resolve_self_addresses,
)
from mailattic.models import PstSource, build_normalized_message
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

_SOURCE = PstSource(
    path=Path("C:/mail/archive.pst"),
    input_mode="file",
    requested_from=Path("C:/mail/archive.pst"),
)

# Deliberately not a real operator address: the ticket keeps operator
# identity out of the repository, tests included.
_PROBE = "probe@example.test"


def _run_cli(*argv: str) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(list(argv))
    return code, buffer.getvalue()


def _seed_probe_sent_message(db: Path) -> None:
    """Seed one message sent *by* the probe address to an outside contact."""
    connection = connect_database(db)
    try:
        message = build_normalized_message(
            {
                "subject": "quote",
                "sender": {"email": _PROBE},
                "to": [{"email": "alice@acme.com"}],
                "sent_at": "2024-03-01T10:00:00",
                "body_text": "hi",
                "message_id": "<probe-1@example.test>",
            },
            source=_SOURCE,
            folder_path="Inbox",
            logical_locator="Inbox/1",
        )
        storage_persist_message(connection, message)
        connection.commit()
    finally:
        connection.close()


class NormaliseAddressTests(unittest.TestCase):
    def test_trims_and_lowercases(self) -> None:
        self.assertEqual(normalise_address("  Sean@Example.TEST "), "sean@example.test")

    def test_rejects_non_addresses(self) -> None:
        for value in ("", "   ", "no-at-sign", "@example.test", "sean@", "a b@c.test"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalise_address(value)

    def test_accepts_a_quoted_local_part_containing_at(self) -> None:
        """The database holds a few of these; splitting on the first @ loses them."""
        self.assertEqual(normalise_address('"a@b"@Example.TEST'), '"a@b"@example.test')


class ResolveSelfAddressesTests(unittest.TestCase):
    def test_empty_table_resolves_to_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = initialize_database(Path(tmp) / "test.db")
            connection = connect_database(db)
            try:
                self.assertEqual(list_self_addresses(connection), [])
                self.assertEqual(resolve_self_addresses(connection), frozenset())
            finally:
                connection.close()

    def test_missing_table_resolves_to_nothing(self) -> None:
        """A database created before self_addresses existed still resolves."""
        with tempfile.TemporaryDirectory() as tmp:
            db = initialize_database(Path(tmp) / "test.db")
            connection = connect_database(db)
            try:
                connection.execute("DROP TABLE self_addresses")
                connection.commit()
                self.assertEqual(resolve_self_addresses(connection), frozenset())
            finally:
                connection.close()

    def test_declared_addresses_replace_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = initialize_database(Path(tmp) / "test.db")
            connection = connect_database(db)
            try:
                self.assertTrue(add_self_address(connection, "  A@Example.TEST "))
                self.assertFalse(add_self_address(connection, "a@example.test"))
                self.assertTrue(add_self_address(connection, "b@example.test"))
                self.assertEqual(
                    resolve_self_addresses(connection),
                    frozenset({"a@example.test", "b@example.test"}),
                )
                self.assertTrue(remove_self_address(connection, "B@EXAMPLE.TEST"))
                self.assertFalse(remove_self_address(connection, "b@example.test"))
                self.assertEqual(
                    resolve_self_addresses(connection), frozenset({"a@example.test"})
                )
            finally:
                connection.close()


class IdentityCliTests(unittest.TestCase):
    def test_add_list_remove_round_trip_emits_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            initialize_database(db)

            code, out = _run_cli("identity", "list", "--database", str(db))
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(out),
                {"addresses": [], "source": "unset"},
            )

            code, out = _run_cli("identity", "add", _PROBE, "--database", str(db))
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["action"], "add")
            self.assertTrue(payload["changed"])
            self.assertEqual(payload["addresses"], [_PROBE])
            self.assertEqual(payload["source"], "database")

            code, out = _run_cli("identity", "list", "--database", str(db))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["addresses"], [_PROBE])

            code, out = _run_cli("identity", "remove", _PROBE, "--database", str(db))
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["changed"])
            self.assertEqual(payload["source"], "unset")

    def test_malformed_address_is_refused_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            initialize_database(db)
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                code = main(
                    ["identity", "add", "not-an-address", "--database", str(db)]
                )
            self.assertEqual(code, 2)
            self.assertIn("not-an-address", buffer.getvalue())
            connection = connect_database(db)
            try:
                self.assertEqual(list_self_addresses(connection), [])
            finally:
                connection.close()

    def test_bare_identity_prints_help_and_exits_two(self) -> None:
        code, out = _run_cli("identity")
        self.assertEqual(code, 2)
        self.assertIn("add", out)


class ContactHistoryReadsDeclaredIdentityTests(unittest.TestCase):
    """The red-green tripwire: drop the table read and this goes red."""

    def test_declaring_the_sender_flips_it_from_inbound_to_outbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = initialize_database(Path(tmp) / "test.db")
            _seed_probe_sent_message(db)

            before = aggregate_contact_history(database_path=db, contact=_PROBE)
            self.assertEqual(before["outbound_count"], 0)
            self.assertEqual(before["inbound_count"], 1)
            self.assertIn(_PROBE, {c["email"] for c in before["contacts"]})

            self.assertEqual(
                main(["identity", "add", _PROBE, "--database", str(db)]), 0
            )

            after = aggregate_contact_history(database_path=db, contact=_PROBE)
            self.assertEqual(after["outbound_count"], 1)
            self.assertEqual(after["inbound_count"], 0)
            # Declared self is no longer listed as a counterparty.
            self.assertNotIn(_PROBE, {c["email"] for c in after["contacts"]})

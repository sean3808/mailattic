from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import toons

from mailattic.cli import main
from mailattic.contact_history import (
    aggregate_contact_history,
    serialise_summary,
)
from mailattic.identity import add_self_address
from mailattic.models import PstSource, build_normalized_message
from mailattic.storage import connect_database, initialize_database
from mailattic.storage import persist_message as storage_persist_message

_SOURCE = PstSource(
    path=Path("C:/mail/archive.pst"),
    input_mode="file",
    requested_from=Path("C:/mail/archive.pst"),
)


def _make_db(temp_dir: str) -> Path:
    return initialize_database(Path(temp_dir) / "test.db")


def _insert(conn, raw: dict, *, folder: str = "Inbox", locator: str = "Inbox/1") -> int:
    add_self_address(conn, "sean3808@mailattic.test")
    msg = build_normalized_message(
        raw, source=_SOURCE, folder_path=folder, logical_locator=locator
    )
    result = storage_persist_message(conn, msg)
    return result.stored_message_id


def _assert_malformed_date_from_rejected(db: Path, value: str) -> None:
    """Shared body for the two `date_from` malformed-value spec examples.

    Both must raise naming the offending value and this module's own
    `date_from` keyword -- not the CLI's `--date-from` flag spelling,
    since `aggregate_contact_history` is a public Python entry point a
    non-CLI caller can call directly (contact_history.py fix: own keyword
    names in the error message).
    """
    try:
        aggregate_contact_history(database_path=db, domain="acme.com", date_from=value)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError(f"expected ValueError for date_from={value!r}")
    assert value in message
    assert "date_from" in message


class _ReadOnlyProbeConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.write_attempted = False
        self.write_error: sqlite3.OperationalError | None = None

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        if not self.write_attempted:
            self.write_attempted = True
            try:
                self._connection.execute(
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
                        "probe",
                        "",
                        "probe@example.com",
                        "2024-01-01T00:00:00",
                        None,
                        "probe",
                        f"<probe-{id(self)}@example.com>",
                        None,
                        None,
                        "probe.pst",
                        "Inbox",
                        f"Inbox/probe/{id(self)}",
                        "file",
                        "probe",
                        f"probe:{id(self)}",
                        "message-id",
                        "",
                        "",
                        None,
                    ],
                )
            except sqlite3.OperationalError as exc:
                self.write_error = exc
                # Python's implicit BEGIN before the failed INSERT would stay
                # open and collide with the aggregation's own BEGIN.
                self._connection.rollback()
        return self._connection.execute(sql, parameters)

    def create_function(self, *args: object, **kwargs: object) -> None:
        self._connection.create_function(*args, **kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        self._connection.close()


def _make_contact_history_connect_probe() -> tuple[
    list[bool],
    list[_ReadOnlyProbeConnection],
    object,
]:
    read_only_calls: list[bool] = []
    probes: list[_ReadOnlyProbeConnection] = []

    def _connect(database_path, *, create=True, read_only=False):
        read_only_calls.append(read_only)
        probe = _ReadOnlyProbeConnection(
            connect_database(database_path, create=create, read_only=read_only)
        )
        probes.append(probe)
        return probe

    return read_only_calls, probes, _connect


def _assert_read_only_probes(
    testcase: unittest.TestCase,
    read_only_calls: list[bool],
    probes: list[_ReadOnlyProbeConnection],
) -> None:
    testcase.assertTrue(read_only_calls)
    testcase.assertTrue(all(read_only_calls))
    testcase.assertTrue(probes)
    testcase.assertTrue(all(probe.write_attempted for probe in probes))
    for probe in probes:
        testcase.assertIsInstance(probe.write_error, sqlite3.OperationalError)


class TestDomainAggregation(unittest.TestCase):
    """AC1: domain aggregation with correct inbound/outbound split."""

    def test_domain_inbound_outbound_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                # outbound: self sends to acme.com
                _insert(
                    conn,
                    {
                        "subject": "Out1",
                        "sender": {"email": "sean3808@mailattic.test"},
                        "to": [{"email": "alice@acme.com", "display_name": "Alice"}],
                        "sent_at": "2024-01-01T10:00:00",
                        "body_text": "hi",
                        "message_id": "<out1@x>",
                    },
                    locator="Inbox/1",
                )
                # inbound: acme sends to self
                _insert(
                    conn,
                    {
                        "subject": "In1",
                        "sender": {"email": "alice@acme.com", "display_name": "Alice"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2024-01-02T10:00:00",
                        "body_text": "reply",
                        "message_id": "<in1@x>",
                    },
                    locator="Inbox/2",
                )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(database_path=db, domain="acme.com")

        self.assertEqual(summary["query_type"], "domain")
        self.assertEqual(summary["total_messages"], 2)
        self.assertEqual(summary["outbound_count"], 1)
        self.assertEqual(summary["inbound_count"], 1)


class TestDomainRecipientBoundaryCorrection(unittest.TestCase):
    """anchor-recipient-domain-filter: `--domain` recipient-side matching
    anchors at the domain boundary. Each assertion fails if the recipient
    condition reverts to the unanchored substring pattern."""

    def _seed_boundary_fixtures(self, conn) -> None:
        _insert(
            conn,
            {
                "subject": "Overmatch only recipient",
                "sender": {"email": "someone1@other.com"},
                "to": [{"email": "alice@acme.com.evil"}],
                "sent_at": "2026-01-01T10:00:00",
                "body_text": "body",
                "message_id": "<overmatch@test>",
            },
            locator="Inbox/1",
        )
        _insert(
            conn,
            {
                "subject": "Packed blob genuine recipient",
                "sender": {"email": "someone2@other.com"},
                "to": [{"email": "'sally@acme.com'; '某人'; 'other@ext.com'"}],
                "sent_at": "2026-01-02T10:00:00",
                "body_text": "body",
                "message_id": "<packed@test>",
            },
            locator="Inbox/2",
        )
        _insert(
            conn,
            {
                "subject": "Non-ASCII overmatch only recipient",
                "sender": {"email": "someone3@other.com"},
                "to": [{"email": "alice@acme.com海灣"}],
                "sent_at": "2026-01-03T10:00:00",
                "body_text": "body",
                "message_id": "<nonascii@test>",
            },
            locator="Inbox/3",
        )
        # Exact-`--contact` path must stay unaffected by this change.
        _insert(
            conn,
            {
                "subject": "Exact contact still matches",
                "sender": {"email": "bob@acme.com"},
                "to": [{"email": "sean3808@mailattic.test"}],
                "sent_at": "2026-01-04T10:00:00",
                "body_text": "body",
                "message_id": "<exact@test>",
            },
            locator="Inbox/4",
        )

    def test_domain_excludes_overmatch_and_non_ascii_includes_packed_genuine(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                self._seed_boundary_fixtures(conn)
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(database_path=db, domain="acme.com")

        contact_emails = {c["email"] for c in summary["contacts"]}
        self.assertNotIn("alice@acme.com.evil", contact_emails)
        self.assertNotIn("alice@acme.com海灣", contact_emails)
        # The packed-blob message's message_recipients row carries the whole
        # blob as its email verbatim (that row is never parsed apart); the
        # message is still counted because recipient_domain_match finds the
        # embedded genuine `@acme.com` address in `recipient_search_text`.
        self.assertIn("'sally@acme.com'; '某人'; 'other@ext.com'", contact_emails)
        self.assertIn("bob@acme.com", contact_emails)
        self.assertEqual(summary["total_messages"], 2)

    def test_domain_leading_at_is_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                self._seed_boundary_fixtures(conn)
                conn.commit()
            finally:
                conn.close()

            plain = aggregate_contact_history(database_path=db, domain="acme.com")
            at_prefixed = aggregate_contact_history(
                database_path=db, domain="@acme.com"
            )

        self.assertEqual(plain["total_messages"], at_prefixed["total_messages"])
        self.assertEqual(
            {c["email"] for c in plain["contacts"]},
            {c["email"] for c in at_prefixed["contacts"]},
        )

    def test_exact_contact_match_unaffected_by_domain_boundary_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                self._seed_boundary_fixtures(conn)
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(
                database_path=db, contact="bob@acme.com"
            )

        self.assertEqual(summary["query_type"], "contact")
        self.assertEqual(summary["total_messages"], 1)


class TestContactExactMatch(unittest.TestCase):
    """AC2: contact exact match."""

    def test_contact_matches_sender_and_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                # alice is sender
                _insert(
                    conn,
                    {
                        "subject": "From alice",
                        "sender": {"email": "alice@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2024-02-01T10:00:00",
                        "body_text": "hello",
                        "message_id": "<a1@x>",
                    },
                    locator="Inbox/1",
                )
                # alice is recipient
                _insert(
                    conn,
                    {
                        "subject": "To alice",
                        "sender": {"email": "sean3808@mailattic.test"},
                        "to": [{"email": "alice@acme.com"}],
                        "sent_at": "2024-02-02T10:00:00",
                        "body_text": "reply",
                        "message_id": "<a2@x>",
                    },
                    locator="Inbox/2",
                )
                # unrelated message - should not match
                _insert(
                    conn,
                    {
                        "subject": "Unrelated",
                        "sender": {"email": "bob@other.com"},
                        "to": [{"email": "carol@other.com"}],
                        "sent_at": "2024-02-03T10:00:00",
                        "body_text": "other",
                        "message_id": "<b1@x>",
                    },
                    locator="Inbox/3",
                )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(
                database_path=db, contact="alice@acme.com"
            )

        self.assertEqual(summary["query_type"], "contact")
        self.assertEqual(summary["total_messages"], 2)


class TestSelfExcludedFromContacts(unittest.TestCase):
    """AC3: self is excluded from the contacts array."""

    def test_self_not_in_contacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                _insert(
                    conn,
                    {
                        "subject": "Internal",
                        "sender": {"email": "alice@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2024-03-01T10:00:00",
                        "body_text": "hi",
                        "message_id": "<c1@x>",
                    },
                    locator="Inbox/1",
                )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(
                database_path=db, domain="mailattic.test"
            )

        self_lower = {"sean3808@mailattic.test"}
        contact_emails = {c["email"] for c in summary["contacts"]}
        self.assertTrue(
            self_lower.isdisjoint(contact_emails),
            f"self addresses found in contacts: {contact_emails & self_lower}",
        )


class TestSelfSelfOutbound(unittest.TestCase):
    """AC4: self→self BCC-copy counts as outbound, not inbound."""

    def test_self_to_self_is_outbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                # sender = self, recipient also includes self (BCC-self copy)
                _insert(
                    conn,
                    {
                        "subject": "Self copy",
                        "sender": {"email": "sean3808@mailattic.test"},
                        "to": [{"email": "alice@acme.com"}],
                        "bcc": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2024-04-01T10:00:00",
                        "body_text": "sent",
                        "message_id": "<d1@x>",
                    },
                    locator="Inbox/1",
                )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(database_path=db, domain="acme.com")

        self.assertEqual(summary["total_messages"], 1)
        self.assertEqual(summary["outbound_count"], 1)
        self.assertEqual(summary["inbound_count"], 0)


class TestCcOnlyParticipantIncluded(unittest.TestCase):
    """AC5: a cc-only participant is included in contacts."""

    def test_cc_participant_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                _insert(
                    conn,
                    {
                        "subject": "With CC",
                        "sender": {"email": "alice@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "cc": [{"email": "carol@acme.com"}],
                        "sent_at": "2024-05-01T10:00:00",
                        "body_text": "fyi carol",
                        "message_id": "<e1@x>",
                    },
                    locator="Inbox/1",
                )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(database_path=db, domain="acme.com")

        contact_emails = {c["email"] for c in summary["contacts"]}
        self.assertIn("carol@acme.com", contact_emails)


class TestDisplayNameCommaEscaping(unittest.TestCase):
    """AC6: display_name 'Chen, Alice' preserved as one field in TOON output."""

    def test_comma_display_name_toon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                _insert(
                    conn,
                    {
                        "subject": "Comma test",
                        "sender": {
                            "email": "chen@acme.com",
                            "name": "Chen, Alice",
                        },
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2024-06-01T10:00:00",
                        "body_text": "hi",
                        "message_id": "<f1@x>",
                    },
                    locator="Inbox/1",
                )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(database_path=db, domain="acme.com")

        # Verify the display_name is preserved in the summary
        contacts = summary["contacts"]
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["display_name"], "Chen, Alice")

        # TOON output must quote the comma-containing value
        toon_output = serialise_summary(summary, fmt="table")
        # The quoted form should appear somewhere in the TOON output
        self.assertIn('"Chen, Alice"', toon_output)

        # Round-trip: toons.loads should reconstruct intact
        reconstructed = toons.loads(toon_output)
        # Traverse to find the display_name in reconstructed contacts
        recon_contacts = reconstructed.get("contacts", [])
        self.assertTrue(
            any(c.get("display_name") == "Chen, Alice" for c in recon_contacts),
            f"display_name not preserved after round-trip; contacts={recon_contacts}",
        )


class TestZeroMatches(unittest.TestCase):
    """AC7: zero matches → exit 0 + empty summary."""

    def test_zero_matches_returns_empty_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            # Insert no messages

            summary = aggregate_contact_history(database_path=db, domain="nobody.com")

        self.assertEqual(summary["total_messages"], 0)
        self.assertEqual(summary["contacts"], [])
        self.assertIsNone(summary["first_message_at"])
        self.assertIsNone(summary["last_message_at"])

    def test_zero_matches_cli_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            exit_code = main(
                ["contact-history", "--domain", "nobody.com", "--database", str(db)]
            )
        self.assertEqual(exit_code, 0)


class TestMutuallyExclusiveSelectorError(unittest.TestCase):
    """AC8: mutually-exclusive selector error → non-zero exit."""

    def test_no_selector_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            # argparse prints to stderr and raises SystemExit on mutual-exclusion error
            with self.assertRaises(SystemExit) as ctx:
                main(["contact-history", "--database", str(db)])
            self.assertNotEqual(ctx.exception.code, 0)

    def test_both_selectors_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with self.assertRaises(SystemExit) as ctx:
                main(
                    [
                        "contact-history",
                        "--contact",
                        "alice@acme.com",
                        "--domain",
                        "acme.com",
                        "--database",
                        str(db),
                    ]
                )
            self.assertNotEqual(ctx.exception.code, 0)


class TestInvalidDateRejected(unittest.TestCase):
    """Contract failure mode: invalid date format → non-zero exit + stderr."""

    def test_bad_date_format_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            exit_code = main(
                [
                    "contact-history",
                    "--domain",
                    "acme.com",
                    "--date-from",
                    "not-a-date",
                    "--database",
                    str(db),
                ]
            )
        self.assertNotEqual(exit_code, 0)


class TestContactsOrderedByMsgCount(unittest.TestCase):
    """Spec Example: alice 40 > bob 30 > carol 5 ordering."""

    def test_contacts_descending_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                # Insert 40 messages from alice, 30 from bob, 5 from carol
                for i in range(40):
                    _insert(
                        conn,
                        {
                            "subject": f"Alice msg {i}",
                            "sender": {"email": "alice@acme.com"},
                            "to": [{"email": "sean3808@mailattic.test"}],
                            "sent_at": f"2024-01-{(i % 28) + 1:02d}T10:00:00",
                            "body_text": "alice",
                            "message_id": f"<alice{i}@x>",
                        },
                        locator=f"Inbox/{i}",
                    )
                for i in range(30):
                    _insert(
                        conn,
                        {
                            "subject": f"Bob msg {i}",
                            "sender": {"email": "bob@acme.com"},
                            "to": [{"email": "sean3808@mailattic.test"}],
                            "sent_at": f"2024-02-{(i % 28) + 1:02d}T10:00:00",
                            "body_text": "bob",
                            "message_id": f"<bob{i}@x>",
                        },
                        locator=f"Inbox/b{i}",
                    )
                for i in range(5):
                    _insert(
                        conn,
                        {
                            "subject": f"Carol msg {i}",
                            "sender": {"email": "carol@acme.com"},
                            "to": [{"email": "sean3808@mailattic.test"}],
                            "sent_at": f"2024-03-{i + 1:02d}T10:00:00",
                            "body_text": "carol",
                            "message_id": f"<carol{i}@x>",
                        },
                        locator=f"Inbox/c{i}",
                    )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(database_path=db, domain="acme.com")

        contacts = summary["contacts"]
        emails = [c["email"] for c in contacts]
        self.assertEqual(emails[0], "alice@acme.com")
        self.assertEqual(emails[1], "bob@acme.com")
        self.assertEqual(emails[2], "carol@acme.com")
        self.assertEqual(contacts[0]["msg_count"], 40)
        self.assertEqual(contacts[1]["msg_count"], 30)
        self.assertEqual(contacts[2]["msg_count"], 5)


class TestAttachmentsTruncation(unittest.TestCase):
    """Spec scenario: 250 distinct attachments → total_count 250, recent len 20."""

    def test_attachments_truncated_to_default_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                for i in range(250):
                    _insert(
                        conn,
                        {
                            "subject": f"Att msg {i}",
                            "sender": {"email": "alice@acme.com"},
                            "to": [{"email": "sean3808@mailattic.test"}],
                            "sent_at": f"2024-01-{(i % 28) + 1:02d}T10:00:00",
                            "body_text": "body",
                            "message_id": f"<att{i}@x>",
                            "attachments": [
                                {
                                    "attachment_name": f"file_{i:04d}.pdf",
                                    "content_hash": None,
                                    "size_bytes": None,
                                    "mime_type": None,
                                    "storage_path": None,
                                }
                            ],
                        },
                        locator=f"Inbox/att{i}",
                    )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(database_path=db, domain="acme.com")

        att = summary["attachments"]
        self.assertEqual(att["total_count"], 250)
        self.assertEqual(len(att["recent"]), 20)


class TestJsonEquivalentFields(unittest.TestCase):
    """Spec: JSON fallback produces same top-level fields and contact entries."""

    def test_json_has_same_fields_as_toon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                _insert(
                    conn,
                    {
                        "subject": "JSON test",
                        "sender": {"email": "alice@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2024-07-01T10:00:00",
                        "body_text": "hi",
                        "message_id": "<j1@x>",
                    },
                    locator="Inbox/1",
                )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(database_path=db, domain="acme.com")

        json_output = serialise_summary(summary, fmt="json")
        parsed = json.loads(json_output)

        expected_keys = {
            "query",
            "query_type",
            "date_from",
            "date_to",
            "first_message_at",
            "last_message_at",
            "total_messages",
            "inbound_count",
            "outbound_count",
            "unknown_count",
            "distinct_contacts",
            "contacts",
            "attachments",
        }
        self.assertEqual(set(parsed.keys()), expected_keys)
        self.assertEqual(parsed["total_messages"], summary["total_messages"])
        self.assertEqual(len(parsed["contacts"]), len(summary["contacts"]))


class TestDateRangeInclusiveFilter(unittest.TestCase):
    """Spec: --date-from / --date-to inclusively filter messages by send time."""

    def test_date_range_includes_boundaries_and_excludes_outside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                for idx, sent_at in enumerate(
                    [
                        "2024-01-15T10:00:00",
                        "2024-02-01T10:00:00",
                        "2024-02-15T10:00:00",
                        "2024-02-28T10:00:00",
                        "2024-03-15T10:00:00",
                    ]
                ):
                    _insert(
                        conn,
                        {
                            "subject": f"msg {idx}",
                            "sender": {"email": "alice@acme.com"},
                            "to": [{"email": "sean3808@mailattic.test"}],
                            "sent_at": sent_at,
                            "body_text": "body",
                            "message_id": f"<d{idx}@x>",
                        },
                        locator=f"Inbox/d{idx}",
                    )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(
                database_path=db,
                domain="acme.com",
                date_from="2024-02-01",
                date_to="2024-02-28",
            )

        self.assertEqual(summary["total_messages"], 3)
        self.assertEqual(summary["first_message_at"], "2024-02-01T10:00:00")
        self.assertEqual(summary["last_message_at"], "2024-02-28T10:00:00")


class TestAttachmentsExplicitLimit(unittest.TestCase):
    """Spec: --attachments-limit <N> overrides default 20."""

    def test_explicit_limit_truncates_to_n(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                for i in range(50):
                    _insert(
                        conn,
                        {
                            "subject": f"att {i}",
                            "sender": {"email": "alice@acme.com"},
                            "to": [{"email": "sean3808@mailattic.test"}],
                            "sent_at": f"2024-01-{(i % 28) + 1:02d}T10:00:00",
                            "body_text": "body",
                            "message_id": f"<el{i}@x>",
                            "attachments": [
                                {
                                    "attachment_name": f"file_{i:04d}.pdf",
                                    "content_hash": None,
                                    "size_bytes": None,
                                    "mime_type": None,
                                    "storage_path": None,
                                }
                            ],
                        },
                        locator=f"Inbox/el{i}",
                    )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(
                database_path=db,
                domain="acme.com",
                attachments_limit=7,
            )

        att = summary["attachments"]
        self.assertEqual(att["total_count"], 50)
        self.assertEqual(len(att["recent"]), 7)


class TestTaipeiOvershootExcluded(unittest.TestCase):
    """design.md > "Set the upper bound at the last whole second of the
    Taipei day"; spec scenario "The upper bound stops at the end of the
    Taipei day". Task 3.1/3.2: the aggregation's overshoot into the
    following Taipei morning is gone.
    """

    def test_message_after_taipei_midnight_excluded_by_date_to(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                _insert(
                    conn,
                    {
                        "subject": "Overshoot",
                        "sender": {"email": "alice@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2026-06-18T06:45:05+08:00",
                        "body_text": "body",
                        "message_id": "<overshoot@x>",
                    },
                    locator="Inbox/1",
                )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(
                database_path=db, domain="acme.com", date_to="2026-06-17"
            )

        self.assertEqual(summary["total_messages"], 0)


class TestTaipeiDayIncludesUtcOffsetMessage(unittest.TestCase):
    """spec scenario "The Taipei calendar day defines inclusion regardless
    of a message's stored offset".
    """

    def test_utc_offset_message_counted_for_taipei_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                _insert(
                    conn,
                    {
                        "subject": "Taipei crossing",
                        "sender": {"email": "alice@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2026-07-20T17:00:00+00:00",
                        "body_text": "body",
                        "message_id": "<crossing@x>",
                    },
                    locator="Inbox/1",
                )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(
                database_path=db,
                domain="acme.com",
                date_from="2026-07-21",
                date_to="2026-07-21",
            )

        self.assertEqual(summary["total_messages"], 1)


class TestAcceptedSyntaxUnchanged(unittest.TestCase):
    """spec scenario "The accepted date syntax of this command is
    unchanged": `2026-7-1` is still accepted (as the Taipei day
    `2026-07-01`), `20260721` is still rejected -- the same two answers
    this command gave before this change, proving no parsing policy was
    imposed on it.
    """

    def test_single_digit_form_still_accepted_and_resolves_to_taipei_day(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                _insert(
                    conn,
                    {
                        "subject": "On the bound",
                        "sender": {"email": "alice@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2026-07-01T00:30:00+08:00",
                        "body_text": "body",
                        "message_id": "<onbound@x>",
                    },
                    locator="Inbox/1",
                )
                _insert(
                    conn,
                    {
                        "subject": "Before the bound",
                        "sender": {"email": "alice@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2026-06-30T23:30:00+08:00",
                        "body_text": "body",
                        "message_id": "<beforebound@x>",
                    },
                    locator="Inbox/2",
                )
                conn.commit()
            finally:
                conn.close()

            summary = aggregate_contact_history(
                database_path=db, domain="acme.com", date_from="2026-7-1"
            )

        self.assertEqual(summary["total_messages"], 1)

    def test_basic_date_form_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _assert_malformed_date_from_rejected(_make_db(tmp), "20260721")

    def test_full_iso8601_timestamp_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _assert_malformed_date_from_rejected(_make_db(tmp), "2026-07-21T12:00:00")

    def test_padded_date_still_rejected_not_stripped(self) -> None:
        """A whitespace-padded value must be rejected, exactly as it was
        before ``normalize_optional_date_text`` existed -- contact-history
        never stripped before parsing (unlike ``mail_search``, which does).
        """
        with tempfile.TemporaryDirectory() as tmp:
            _assert_malformed_date_from_rejected(_make_db(tmp), "  2026-07-21  ")


class TestMalformedDateIsErrorNotZeroSummary(unittest.TestCase):
    """spec scenario "Malformed date is an error, not a zero-result
    summary": a value this command's own parser does not accept raises
    naming both the offending value and the parameter, and produces no
    summary object -- not a well-formed zero-count summary.
    """

    def test_malformed_date_raises_naming_value_and_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _assert_malformed_date_from_rejected(_make_db(tmp), "2026-201")

    def test_malformed_date_via_cli_exits_nonzero_with_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "contact-history",
                        "--domain",
                        "acme.com",
                        "--date-from",
                        "2026-201",
                        "--database",
                        str(db),
                    ]
                )
        self.assertNotEqual(exit_code, 0)
        self.assertIn("2026-201", stderr.getvalue())
        self.assertIn("--date-from", stderr.getvalue())


class TestBlankDateBoundIsUnsupplied(unittest.TestCase):
    """spec scenario "A blank date bound is an unsupplied filter, not a
    malformed one": an empty or whitespace-only bound produces the same
    summary as omitting the flag entirely.
    """

    def test_whitespace_only_date_from_behaves_as_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                _insert(
                    conn,
                    {
                        "subject": "Any day",
                        "sender": {"email": "alice@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2024-01-01T10:00:00",
                        "body_text": "body",
                        "message_id": "<anyday@x>",
                    },
                    locator="Inbox/1",
                )
                conn.commit()
            finally:
                conn.close()

            omitted_summary = aggregate_contact_history(
                database_path=db, domain="acme.com"
            )
            blank_summary = aggregate_contact_history(
                database_path=db, domain="acme.com", date_from="   ", date_to=""
            )

        self.assertEqual(
            blank_summary["total_messages"], omitted_summary["total_messages"]
        )
        self.assertEqual(blank_summary["total_messages"], 1)
        self.assertIsNone(omitted_summary["date_from"])
        self.assertIsNone(omitted_summary["date_to"])
        self.assertIsNone(blank_summary["date_from"])
        self.assertIsNone(blank_summary["date_to"])


class _MidAggregationWriter:
    """Connection proxy: right before the first-message edge query (after the
    core message fetch), a second connection commits one more message."""

    def __init__(self, connection: sqlite3.Connection, hook, fired: list) -> None:
        self._connection = connection
        self._hook = hook
        self._fired = fired

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        if not self._fired and "ORDER BY unixepoch(m.sent_at) ASC" in sql:
            self._fired.append(sql)
            self._hook()
        return self._connection.execute(sql, parameters)

    def create_function(self, *args: object, **kwargs: object) -> None:
        self._connection.create_function(*args, **kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        self._connection.close()


class TestAggregationUsesOneReadSnapshot(unittest.TestCase):
    """#25: a sync commit between the aggregation's SELECTs must not leak in."""

    def _run(self, *, read_only: bool, fail: bool = False) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                _insert(
                    conn,
                    {
                        "subject": "One",
                        "sender": {"email": "buyer1@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2024-10-20T10:00:00+08:00",
                        "body_text": "b",
                        "message_id": "<one@x>",
                        "attachment_names": ["one.pdf"],
                    },
                    locator="Inbox/1",
                )
                conn.commit()
            finally:
                conn.close()

            def write_second() -> None:
                writer = connect_database(db)
                try:
                    _insert(
                        writer,
                        {
                            "subject": "Two",
                            "sender": {"email": "sean3808@mailattic.test"},
                            "to": [{"email": "buyer2@acme.com"}],
                            "sent_at": "2024-10-21T10:00:00+08:00",
                            "body_text": "b",
                            "message_id": "<two@x>",
                            "attachment_names": ["two.pdf"],
                        },
                        locator="Inbox/2",
                    )
                    writer.commit()
                finally:
                    writer.close()

            def boom() -> None:
                raise RuntimeError("boom")

            fired: list = []

            def fake_connect(database_path, *, create=True, read_only=False):
                return _MidAggregationWriter(
                    connect_database(database_path, create=create, read_only=read_only),
                    boom if fail else write_second,
                    fired,
                )

            try:
                with patch(
                    "mailattic.contact_history.connect_database", new=fake_connect
                ):
                    result = aggregate_contact_history(
                        database_path=db, domain="acme.com", read_only=read_only
                    )
            except RuntimeError:
                if not fail:
                    raise
                # The failed aggregation must have released its read
                # transaction: a writer commits and a TRUNCATE checkpoint
                # (which needs zero live readers) is not busy.
                write_second()
                check = connect_database(db)
                try:
                    busy = check.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                finally:
                    check.close()
                self.assertEqual(busy[0], 0)
                self.assertTrue(fired, "interleave hook never fired")
                return {}
            self.assertFalse(fail, "expected the aggregation to raise")
            # Guards against the edge SQL being reworded: a hook that never
            # ran would let the snapshot assertions pass vacuously.
            self.assertTrue(fired, "interleave hook never fired")
            return result

    def _assert_pre_write_snapshot(self, summary: dict[str, object]) -> None:
        self.assertEqual(summary["total_messages"], 1)
        self.assertEqual(summary["inbound_count"], 1)
        self.assertEqual(summary["outbound_count"], 0)
        self.assertEqual(summary["last_message_at"], "2024-10-20T10:00:00+08:00")
        self.assertEqual(summary["attachments"]["total_count"], 1)
        self.assertEqual([c["email"] for c in summary["contacts"]], ["buyer1@acme.com"])

    def test_writer_commit_mid_aggregation_is_not_visible(self) -> None:
        self._assert_pre_write_snapshot(self._run(read_only=False))

    def test_writer_commit_mid_aggregation_is_not_visible_read_only(self) -> None:
        self._assert_pre_write_snapshot(self._run(read_only=True))

    def test_exception_mid_aggregation_releases_read_transaction(self) -> None:
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                self._run(read_only=read_only, fail=True)


class TestReadOnlyAndBlankSelectorValidation(unittest.TestCase):
    def test_aggregate_contact_history_read_only_connection_rejects_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                _insert(
                    conn,
                    {
                        "subject": "Read only",
                        "sender": {"email": "alice@acme.com"},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2024-01-01T10:00:00",
                        "body_text": "body",
                        "message_id": "<readonly@x>",
                    },
                    locator="Inbox/1",
                )
                conn.commit()
            finally:
                conn.close()

            read_only_calls, probes, fake_connect = (
                _make_contact_history_connect_probe()
            )
            with patch(
                "mailattic.contact_history.connect_database",
                new=fake_connect,
            ):
                summary = aggregate_contact_history(
                    database_path=db,
                    domain="acme.com",
                    read_only=True,
                )

        self.assertEqual(summary["total_messages"], 1)
        _assert_read_only_probes(self, read_only_calls, probes)

    def test_blank_domain_selector_raises_inside_aggregation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)

            for value in ("", "   "):
                with (
                    patch(
                        "mailattic.contact_history.connect_database"
                    ) as mocked_connect,
                    self.assertRaisesRegex(ValueError, "domain"),
                ):
                    aggregate_contact_history(database_path=db, domain=value)
                mocked_connect.assert_not_called()

    def test_blank_contact_selector_raises_inside_aggregation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)

            for value in ("", "   "):
                with (
                    patch(
                        "mailattic.contact_history.connect_database"
                    ) as mocked_connect,
                    self.assertRaisesRegex(ValueError, "contact"),
                ):
                    aggregate_contact_history(database_path=db, contact=value)
                mocked_connect.assert_not_called()

    def test_contact_history_cli_blank_domain_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            stderr = io.StringIO()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "contact-history",
                        "--domain",
                        "",
                        "--database",
                        str(db),
                    ]
                )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("domain", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")


class TestAttachmentsLimitRejectsNegative(unittest.TestCase):
    """Contract: --attachments-limit must be non-negative integer."""

    def test_negative_attachments_limit_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            with self.assertRaises(SystemExit) as ctx:
                main(
                    [
                        "contact-history",
                        "--domain",
                        "acme.com",
                        "--attachments-limit",
                        "-1",
                        "--database",
                        str(db),
                    ]
                )
            self.assertNotEqual(ctx.exception.code, 0)


class TestPackedSelfSenderMatchesPerMessageDirection(unittest.TestCase):
    """#232: aggregation reads a sender the same way `compute_direction` does."""

    def _summary(self, senders: list[str]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = connect_database(db)
            try:
                for i, sender in enumerate(senders):
                    _insert(
                        conn,
                        {
                            "subject": f"s{i}",
                            "sender": {"email": sender},
                            "to": [{"email": "buyer@client.example"}],
                            "sent_at": f"2026-09-0{i + 1}T08:00:00+08:00",
                            "body_text": "x",
                            "message_id": f"<m{i}@x>",
                        },
                        locator=f"Inbox/{i}",
                    )
                conn.commit()
            finally:
                conn.close()
            return aggregate_contact_history(database_path=db, domain="client.example")

    def test_self_sender_shapes_all_count_outbound_and_stay_out_of_contacts(
        self,
    ) -> None:
        summary = self._summary(
            [
                "Sean<sean3808@mailattic.test>",
                "sean3808@mailattic.test",
                "  SEAN3808@Mailattic.test ",
                "buyer@client.example",
            ]
        )
        self.assertEqual(summary["total_messages"], 4)
        self.assertEqual(summary["outbound_count"], 3)
        self.assertEqual(summary["inbound_count"], 1)
        self.assertEqual(
            [c["email"] for c in summary["contacts"]], ["buyer@client.example"]
        )
        self.assertEqual(summary["distinct_contacts"], 1)

    def test_non_self_colleague_stays_inbound_and_listed(self) -> None:
        summary = self._summary(["Ava<ava@mailattic.test>"])
        self.assertEqual(summary["outbound_count"], 0)
        self.assertEqual(summary["inbound_count"], 1)
        self.assertIn("ava@mailattic.test", {c["email"] for c in summary["contacts"]})


class TestUnknownSenderCount(unittest.TestCase):
    """#232 round 2: a missing / blank sender is `unknown_count`, not inbound."""

    def _db(self, tmp: str) -> Path:
        db = _make_db(tmp)
        conn = connect_database(db)
        try:
            for i, sender in enumerate(
                [None, "   ", "buyer@client.example", "sean3808@mailattic.test"]
            ):
                _insert(
                    conn,
                    {
                        "subject": f"s{i}",
                        "sender": {"email": sender},
                        "to": [{"email": "buyer@client.example"}],
                        "sent_at": f"2026-09-0{i + 1}T08:00:00+08:00",
                        "body_text": "x",
                        "message_id": f"<u{i}@x>",
                    },
                    locator=f"Inbox/{i}",
                )
            conn.commit()
        finally:
            conn.close()
        return db

    def _assert_counts(self, summary: dict) -> None:
        self.assertEqual(summary["total_messages"], 4)
        self.assertEqual(summary["inbound_count"], 1)
        self.assertEqual(summary["outbound_count"], 1)
        self.assertEqual(summary["unknown_count"], 2)
        self.assertEqual(
            summary["total_messages"],
            summary["inbound_count"]
            + summary["outbound_count"]
            + summary["unknown_count"],
        )

    def test_api_counts_null_and_blank_sender_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            summary = aggregate_contact_history(
                database_path=db, domain="client.example"
            )
        self._assert_counts(summary)
        self.assertEqual(
            [c["email"] for c in summary["contacts"]], ["buyer@client.example"]
        )

    def test_empty_result_carries_zero_unknown_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = aggregate_contact_history(
                database_path=_make_db(tmp), domain="nobody.example"
            )
        self.assertEqual(summary["unknown_count"], 0)

    def test_cli_json_and_text_carry_unknown_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            outputs = {}
            for fmt in ("json", "table"):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            "contact-history",
                            "--domain",
                            "client.example",
                            "--format",
                            fmt,
                            "--database",
                            str(db),
                        ]
                    )
                self.assertEqual(code, 0)
                outputs[fmt] = stdout.getvalue()
        self._assert_counts(json.loads(outputs["json"]))
        self.assertEqual(toons.loads(outputs["table"])["unknown_count"], 2)

    def test_mcp_output_carries_unknown_count(self) -> None:
        import asyncio

        from mailattic.mcp_server import create_server

        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            server = create_server(database_path=db)
            result = asyncio.run(
                server.call_tool("mail_contact_history", {"domain": "client.example"})
            )
        structured = result.structured_content
        assert structured is not None
        self._assert_counts(structured)


if __name__ == "__main__":
    unittest.main()

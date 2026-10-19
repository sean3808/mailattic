"""Regression tests for issue #15: semicolon-separated recipient truncation.

`email.policy.default`'s headerregistry address-list parser silently
truncates a bare, semicolon-separated `To`/`Cc`/`Bcc` header to its first
address. These tests lock the recovery behavior described in
`openspec/changes/fix-semicolon-recipient-truncation/design.md` (Decisions
1-3) and its spec scenarios.
"""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

from mailattic.models import PstSource, build_normalized_message
from mailattic.parser import _parse_email_message


def _build_normalized_to(to_header: str) -> tuple:
    raw_bytes = _build_raw_message(to_header)
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    raw_message = _parse_email_message(message, raw_bytes=raw_bytes)
    normalized = build_normalized_message(
        raw_message,
        source=_DUMMY_SOURCE,
        folder_path="INBOX",
        logical_locator="dummy.eml",
    )
    return normalized.to_recipients


_DUMMY_SOURCE = PstSource(
    path=Path("dummy.pst"), input_mode="file", requested_from=Path("dummy.pst")
)


def _build_raw_message(to_header: str) -> bytes:
    return (
        "From: Alice <alice@example.com>\r\n"
        f"To: {to_header}\r\n"
        "Subject: test message\r\n"
        "\r\n"
        "Body text\r\n"
    ).encode()


def _parse_to(to_header: str, *, raw_bytes_override: object = "__use_default__"):
    raw_bytes = _build_raw_message(to_header)
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    effective_raw_bytes = (
        raw_bytes if raw_bytes_override == "__use_default__" else raw_bytes_override
    )
    result = _parse_email_message(message, raw_bytes=effective_raw_bytes)
    return result["to"]


def test_bare_semicolon_list_recovers_all_addresses() -> None:
    """A bare semicolon-separated To header lists multiple addresses.

    Spec example: a four-address list must recover all four `to` recipients
    (design.md Decision 1 + 2; spec.md "four-address list" example).
    """
    to_header = (
        "elaine@org-a.example; carol@org-a.example; "
        "octavia@org-a.example; tessa@org-a.example"
    )

    recipients = _parse_to(to_header)

    emails = [entry["email"] for entry in recipients]
    assert emails == [
        "elaine@org-a.example",
        "carol@org-a.example",
        "octavia@org-a.example",
        "tessa@org-a.example",
    ]


def test_named_pairs_joined_by_semicolons_recovered() -> None:
    """`Name <addr>` pairs joined by semicolons are both recovered."""
    to_header = "Alice <alice@x.com>; Bob <bob@y.com>"

    recipients = _parse_to(to_header)

    assert recipients == [
        {"name": "Alice", "email": "alice@x.com"},
        {"name": "Bob", "email": "bob@y.com"},
    ]


def test_quoted_comma_display_name_in_semicolon_list_stays_intact() -> None:
    """A quoted display name containing a comma is not split by recovery."""
    to_header = '"Example, Sean" <sean3808@mailattic.test>; other@x.com'

    recipients = _parse_to(to_header)

    assert recipients == [
        {"name": "Example, Sean", "email": "sean3808@mailattic.test"},
        {"name": None, "email": "other@x.com"},
    ]


def test_well_formed_group_syntax_not_altered() -> None:
    """A well-formed group header parses cleanly and is left untouched.

    No `InvalidHeaderDefect` is raised for this header, so the strict
    replace condition (Decision 1) never fires; both addresses come through
    the pre-existing default-parse path with no duplication.
    """
    to_header = "Team: alice@x.com, bob@y.com;"

    recipients = _parse_to(to_header)

    assert recipients == [
        {"name": None, "email": "alice@x.com"},
        {"name": None, "email": "bob@y.com"},
    ]


def test_empty_named_group_yields_zero_recipients() -> None:
    """An empty named group collapses to zero recipients (Decision 3).

    The header carries no `InvalidHeaderDefect`, so parser-level recovery
    never fires here (pre-existing shape unaffected by this fix); the
    existing `_coerce_participants` falsy filter inside
    `build_normalized_message` is what actually drops the empty placeholder
    entry, per Decision 3.
    """
    to_header = "Undisclosed recipients:;"

    to_recipients = _build_normalized_to(to_header)

    assert to_recipients == ()


def test_defect_bearing_header_without_semicolon_is_not_altered() -> None:
    """A defect-bearing header without a semicolon is left unchanged.

    This header triggers the headerregistry address-list invalidity defect
    (a bare address followed by a bracketed address with no delimiter) but
    contains no semicolon, so condition (b) of the strict replace condition
    fails and recovery must not apply.
    """
    to_header = "alice@x.com <bob@y.com>"

    raw_bytes = _build_raw_message(to_header)
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)

    # Confirm the defect actually fires for this header, so the test is
    # exercising the intended guard rather than a header that never defects.
    to_header_value = message.get_all("To", [])[0]
    from email.errors import InvalidHeaderDefect

    assert any(
        isinstance(defect, InvalidHeaderDefect) for defect in to_header_value.defects
    )
    assert ";" not in to_header

    # Baseline = recovery disabled (raw_bytes=None). Comparing the
    # recovery-enabled parse against this true no-recovery baseline is what
    # proves recovery did NOT fire for a defect-bearing header with no
    # semicolon; comparing two recovery-enabled parses would prove nothing.
    baseline_recipients = _parse_to(to_header, raw_bytes_override=None)
    recovered_recipients = _parse_to(to_header)

    assert recovered_recipients == baseline_recipients


def _build_raw_message_multi_to(to_headers: list) -> bytes:
    header_lines = "".join(f"To: {value}\r\n" for value in to_headers)
    return (
        "From: Alice <alice@example.com>\r\n"
        f"{header_lines}"
        "Subject: test message\r\n"
        "\r\n"
        "Body text\r\n"
    ).encode()


def test_cross_instance_defect_and_semicolon_do_not_spuriously_trigger_recovery() -> (
    None
):
    """Two repeated `To:` lines: the defect is on one instance, `;` on another.

    Regression guard for the whole-field correlation bug: the defect check
    and the semicolon check must be satisfied by the SAME header instance
    (Decision 1), not merely somewhere across the header's repeated
    instances. Instance 0 defects but has no semicolon; instance 1 has a
    semicolon (valid group syntax) but carries no defect — recovery must not
    fire for either, so the recovery-enabled parse must match the
    recovery-disabled (raw_bytes=None) baseline exactly.
    """
    from email.errors import InvalidHeaderDefect

    defect_only_header = "alice@x.com <bob@y.example.com>"
    semicolon_only_header = "Team: carol@example.com, dave@example.com;"

    raw_bytes = _build_raw_message_multi_to([defect_only_header, semicolon_only_header])
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)

    to_header_values = message.get_all("To", [])
    assert len(to_header_values) == 2
    assert any(
        isinstance(defect, InvalidHeaderDefect)
        for defect in to_header_values[0].defects
    )
    assert not any(
        isinstance(defect, InvalidHeaderDefect)
        for defect in to_header_values[1].defects
    )
    assert ";" not in defect_only_header
    assert ";" in semicolon_only_header

    recovered_result = _parse_email_message(message, raw_bytes=raw_bytes)["to"]
    baseline_result = _parse_email_message(message, raw_bytes=None)["to"]

    assert recovered_result == baseline_result


def test_raw_bytes_none_returns_prior_parse_without_raising() -> None:
    """When no raw byte source is available, recovery is skipped, no crash.

    The assertion describes the shape this parser owns — the default-policy
    parse, minus any entry with no email address — rather than the number of
    addresses the current CPython patch release happens to return for a bare
    semicolon list. That number is not stable: 3.13.12 truncates the header to
    its first address, while 3.13.14 returns a second entry with no address at
    all (issue #64).

    The comparison value is derived here from what `email.policy.default` and
    `getaddresses` actually do on the running interpreter, not from what
    `_parse_email_message` returned, so a change on our side cannot move both
    sides of the assertion at once (PC-079).
    """
    to_header = "a@x.com; b@y.com; c@z.com"

    recipients = _parse_to(to_header, raw_bytes_override=None)

    message = BytesParser(policy=policy.default).parsebytes(
        _build_raw_message(to_header)
    )
    expected = [
        {"name": name or None, "email": address}
        for name, address in getaddresses(message.get_all("To", []))
        if address.strip()
    ]

    assert recipients == expected
    # Recovery was skipped, so at most the default parse's addresses survive;
    # and whatever survived carries a real address.
    assert all(entry["email"] for entry in recipients)
    assert "a@x.com" in {entry["email"] for entry in recipients}

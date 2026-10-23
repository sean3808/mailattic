from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import toons

from .datetime_bounds import (
    format_invalid_date_message,
    normalize_optional_date_text,
    whole_day_bound,
)
from .identity import compute_direction, extract_address, resolve_self_addresses
from .query import _to_domain_suffix_pattern
from .storage import connect_database


def aggregate_contact_history(
    *,
    database_path: str | Path,
    contact: str | None = None,
    domain: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    attachments_limit: int = 20,
    read_only: bool = False,
) -> dict[str, object]:
    """Return a single aggregated contact-history summary dict."""
    trimmed_contact = contact.strip() if contact is not None else None
    trimmed_domain = domain.strip() if domain is not None else None
    normalized_date_from = normalize_optional_date_text(date_from)
    normalized_date_to = normalize_optional_date_text(date_to)

    if contact is not None and not trimmed_contact:
        raise ValueError("contact selector must not be blank.")
    if domain is not None and not trimmed_domain:
        raise ValueError("domain selector must not be blank.")

    if (trimmed_contact is None) == (trimmed_domain is None):
        raise ValueError("Exactly one of contact or domain must be provided.")

    query_value: str
    query_type: str
    if trimmed_contact is not None:
        query_value = trimmed_contact
        query_type = "contact"
    else:
        assert trimmed_domain is not None
        query_value = trimmed_domain
        query_type = "domain"

    # Parsed here, next to the selector validation above, rather than three
    # layers down inside the SQL clause builder -- so a malformed date is
    # rejected before the database is opened, at the same depth as the
    # sibling contact/domain validation. Uses this module's own keyword
    # names (date_from / date_to), not the CLI's flag spellings: this is a
    # public Python entry point and a non-CLI caller never used --date-from.
    # The CLI names its own flags via its own pre-validation in cli.py.
    parsed_date_from = parse_date_bound(
        normalized_date_from, parameter_name="date_from"
    )
    parsed_date_to = parse_date_bound(normalized_date_to, parameter_name="date_to")

    conn = connect_database(database_path, create=False, read_only=read_only)
    try:
        # Deferred BEGIN: the first SELECT pins one WAL read snapshot for the
        # whole aggregation (#25) without blocking a concurrent sync writer.
        # close() in `finally` ends the transaction, including on exceptions.
        conn.execute("BEGIN")
        result = _run_aggregation(
            conn,
            query_value=query_value,
            query_type=query_type,
            date_from=normalized_date_from,
            date_to=normalized_date_to,
            parsed_date_from=parsed_date_from,
            parsed_date_to=parsed_date_to,
            attachments_limit=attachments_limit,
        )
    finally:
        conn.close()
    return result


def parse_date_bound(value: str | None, *, parameter_name: str) -> date | None:
    """Parse a `contact-history` date bound (`strptime` with `%Y-%m-%d`).

    Shared by :func:`aggregate_contact_history` (``parameter_name`` =
    ``date_from`` / ``date_to``, this module's own keyword names) and the
    CLI's pre-validation in ``cli.py`` (``parameter_name`` = ``--date-from``
    / ``--date-to``, the flag spelling) -- one parser, each caller raising
    with the name its own callers actually used. Unchanged in what it
    accepts -- see design.md > "Hold the whole-day rule in one dedicated
    module, and keep parsing out of it". A `None`, empty, or
    whitespace-only *value* means the filter was not supplied and returns
    ``None`` rather than raising (design.md > "Reject a malformed date
    instead of letting it match nothing, and treat a blank value as an
    absent filter"). Any other value this parser does not accept raises
    ``ValueError`` naming both the offending value and *parameter_name*.
    """
    checked = normalize_optional_date_text(value)
    if checked is None:
        return None
    try:
        # strptime does not tolerate surrounding whitespace, so a padded
        # value is rejected here exactly as it was before this helper
        # existed.
        return datetime.strptime(checked, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            format_invalid_date_message(value, parameter_name, expected="YYYY-MM-DD")
        ) from exc


def _build_match_clause(
    query_type: str,
    query_value: str,
    parsed_date_from: date | None,
    parsed_date_to: date | None,
) -> tuple[str, list[object]]:
    """Return (WHERE clause fragment, parameters) for the message selector."""
    params: list[object] = []
    if query_type == "contact":
        norm = query_value.strip().lower()
        # Exact match: sender OR recipient row exists
        clause = (
            "(LOWER(m.sender_email) = ? "
            "OR EXISTS ("
            "  SELECT 1 FROM message_recipients r "
            "  WHERE r.message_id = m.id AND LOWER(r.email) = ?"
            "))"
        )
        params.extend([norm, norm])
    else:
        clause = (
            "(m.sender_email LIKE ? ESCAPE '\\' "
            "OR recipient_domain_match(m.recipient_search_text, ?))"
        )
        params.append(_to_domain_suffix_pattern(query_value))
        params.append(query_value)

    if parsed_date_from is not None:
        clause += " AND unixepoch(m.sent_at) >= unixepoch(?)"
        params.append(whole_day_bound(parsed_date_from).isoformat())
    if parsed_date_to is not None:
        clause += " AND unixepoch(m.sent_at) <= unixepoch(?)"
        params.append(whole_day_bound(parsed_date_to, end_of_day=True).isoformat())

    return clause, params


def _run_aggregation(
    conn: sqlite3.Connection,
    *,
    query_value: str,
    query_type: str,
    date_from: str | None,
    date_to: str | None,
    parsed_date_from: date | None,
    parsed_date_to: date | None,
    attachments_limit: int,
) -> dict[str, object]:
    match_clause, params = _build_match_clause(
        query_type, query_value, parsed_date_from, parsed_date_to
    )
    # Resolved once per aggregation: every direction/self decision below
    # reads the same operator-declared set (issue #168).
    self_addresses = resolve_self_addresses(conn)

    self_p = sorted(self_addresses)
    self_ph = ",".join("?" for _ in self_p)

    # --- Core message stats ---
    # Sender identity is decided in Python by the same helpers the per-message
    # direction uses (compute_direction / extract_address), not by SQL string
    # equality: a `Display Name<addr>` sender must read as self here too (#232).
    msg_rows = conn.execute(
        f"""
        SELECT
            m.id AS msg_id,
            m.sender_email AS sender_email,
            m.sender_name AS display_name,
            m.sent_at AS sent_at,
            unixepoch(m.sent_at) AS sent_epoch
        FROM messages m
        WHERE {match_clause}
        """,
        params,
    ).fetchall()
    directions = [
        compute_direction(r["sender_email"], self_addresses) for r in msg_rows
    ]
    total: int = len(msg_rows)

    # Earliest / latest by absolute instant (unixepoch), not by the stored
    # string: offsets differ, so lexical order is not chronological (#231).
    # NULL / unparseable dates are skipped, as MIN/MAX skipped NULL.
    def _edge(direction: str) -> str | None:
        edge = conn.execute(
            f"SELECT m.sent_at FROM messages m WHERE {match_clause} "
            "AND unixepoch(m.sent_at) IS NOT NULL "
            f"ORDER BY unixepoch(m.sent_at) {direction}, m.id {direction} LIMIT 1",
            params,
        ).fetchone()
        return edge["sent_at"] if edge else None

    first_at = _edge("ASC")
    last_at = _edge("DESC")
    outbound_count: int = directions.count("outbound")
    inbound_count: int = directions.count("inbound")
    # Missing / blank sender: neither direction is knowable (#232).
    unknown_count: int = directions.count(None)

    # --- Contacts aggregation ---
    # Collect from sender + to + cc roles, excluding self and bcc
    # Sender as participant (where sender is NOT self)
    sender_rows = [
        {**r, "email": extract_address(r["sender_email"] or "")}
        for r in msg_rows
        if compute_direction(r["sender_email"], self_addresses) == "inbound"
    ]

    # Recipients (to + cc), excluding self and bcc
    recip_rows = conn.execute(
        f"""
        SELECT
            LOWER(r.email) AS email,
            r.display_name AS display_name,
            r.message_id AS msg_id,
            m.sent_at AS sent_at,
            unixepoch(m.sent_at) AS sent_epoch
        FROM message_recipients r
        JOIN messages m ON m.id = r.message_id
        WHERE {match_clause}
          AND r.recipient_type IN ('to', 'cc')
          AND LOWER(r.email) NOT IN ({self_ph})
        """,
        params + self_p,
    ).fetchall()

    # Aggregate: email → {msg_ids, last_seen, display_name}
    contact_msgs: dict[str, set[int]] = defaultdict(set)
    contact_last: dict[str, tuple[int, int, str]] = {}
    contact_name: dict[str, str] = {}

    for row in (*sender_rows, *recip_rows):
        em = row["email"] or ""
        if not em:
            continue
        msg_id_val = int(row["msg_id"])
        contact_msgs[em].add(msg_id_val)
        sent = row["sent_at"]
        if sent and row["sent_epoch"] is not None:
            # Latest by instant; same-instant ties go to the higher message id.
            key = (int(row["sent_epoch"]), msg_id_val, sent)
            if em not in contact_last or contact_last[em][:2] < key[:2]:
                contact_last[em] = key
        if not contact_name.get(em):
            dn = row["display_name"] or ""
            if dn:
                contact_name[em] = dn

    contacts = [
        {
            "email": em,
            "display_name": contact_name.get(em, ""),
            "msg_count": len(msgs),
            "last_seen": contact_last[em][2] if em in contact_last else None,
        }
        for em, msgs in contact_msgs.items()
    ]
    contacts.sort(key=lambda c: (-c["msg_count"], c["email"]))
    distinct_contacts = len(contacts)

    # --- Attachments ---
    att_rows = conn.execute(
        f"""
        SELECT
            ma.attachment_name,
            m.sent_at
        FROM message_attachments ma
        JOIN messages m ON m.id = ma.message_id
        WHERE {match_clause}
          AND ma.attachment_name IS NOT NULL
          AND ma.attachment_name != ''
        ORDER BY COALESCE(unixepoch(m.sent_at), -62135596800) DESC, ma.id DESC
        """,
        params,
    ).fetchall()

    # Dedup filenames keeping most-recent occurrence
    recent_names = list(
        dict.fromkeys(att_row["attachment_name"] for att_row in att_rows)
    )
    total_att_count = len(recent_names)
    recent_truncated = recent_names[:attachments_limit]

    return {
        "query": query_value,
        "query_type": query_type,
        "date_from": date_from,
        "date_to": date_to,
        "first_message_at": first_at,
        "last_message_at": last_at,
        "total_messages": total,
        "inbound_count": inbound_count,
        "outbound_count": outbound_count,
        "unknown_count": unknown_count,
        "distinct_contacts": distinct_contacts,
        "contacts": contacts,
        "attachments": {"total_count": total_att_count, "recent": recent_truncated},
    }


def serialise_summary(summary: dict[str, object], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(summary, ensure_ascii=False, indent=2)
    return toons.dumps(summary)

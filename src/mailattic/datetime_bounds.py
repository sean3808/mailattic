"""Shared, parse-free definition of an Asia/Taipei calendar day.

This module holds the one rule the ``search`` / ``attachments`` CLI
subcommands, the ``contact-history`` aggregation, and the ``mail_search``
MCP tool use to turn a calendar date into an absolute time interval: "the
whole of that day in Asia/Taipei, both bounds inclusive". Each of those
four surfaces parses a date string with its own existing parser (they
genuinely accept different syntaxes) and hands the resulting
:class:`datetime.date` to :func:`whole_day_bound` here. The dormant
``archive_scrape`` module still builds its own Taipei midnight separately
and is intentionally left alone -- see ``cli.py`` > ``_parse_optional_date``.

Deliberately absent: any date-string parsing. See
``openspec/changes/unify-date-filter-timezone/design.md`` > "Hold the
whole-day rule in one dedicated module, and keep parsing out of it".

This module imports nothing from ``query``, ``contact_history``, or
``cli``, so all three (and the MCP server) can import it without
creating a cycle.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

#: Asia/Taipei is a fixed UTC+8 offset with no daylight-saving rules, so a
#: plain :class:`datetime.timezone` is exact and needs no tz database.
TAIPEI = timezone(timedelta(hours=8), name="Asia/Taipei")


def whole_day_bound(day: date, *, end_of_day: bool = False) -> datetime:
    """Return the offset-aware Asia/Taipei instant bounding ``day``.

    ``end_of_day=False`` (the default) returns the day's first instant,
    ``00:00:00+08:00``. ``end_of_day=True`` returns the day's last whole
    second, ``23:59:59+08:00`` -- not a microsecond-precision midnight.
    That whole-second bound is exactly inclusive for every stored row
    because the SQL comparison truncates to whole seconds (verified:
    SQLite's ``unixepoch()`` maps both ``...T23:59:59.999999+08:00`` and
    ``...T23:59:59+08:00`` to the same epoch second), not because of any
    property of the stored corpus -- switching to ``unixepoch(x,'subsec')``,
    to string comparison, or to another database engine would break that
    inclusivity (see design.md > "Set the upper bound at the last whole
    second of the Taipei day").

    Accepts only an already-parsed :class:`datetime.date`. This function
    never parses a string.
    """
    if end_of_day:
        return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=TAIPEI)
    return datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=TAIPEI)


def coerce_to_taipei_aware(value: datetime) -> datetime:
    """Return ``value`` unchanged if it already carries a UTC offset.

    If ``value`` is naive (``tzinfo`` is ``None`` or reports no offset),
    return the same clock reading with Asia/Taipei attached -- the reading
    itself is not shifted, only labeled (design.md > "Normalize date
    strings at the entry boundary and coerce naive datetimes at the query
    layer").
    """
    if value.utcoffset() is not None:
        return value
    return value.replace(tzinfo=TAIPEI)


def normalize_optional_date_text(value: str | None) -> str | None:
    """Return ``None`` for an absent/blank date filter, else ``value`` unchanged.

    ``None``, an empty string, or a whitespace-only string all mean "this
    filter was not supplied" (design.md > "Reject a malformed date instead
    of letting it match nothing, and treat a blank value as an absent
    filter"). That blank test is the one piece of the rule every caller
    shares, so it lives here instead of being re-implemented at each of the
    four entry points. Whitespace *tolerance* around an otherwise non-blank
    value is not part of this shared rule -- it is each surface's own
    pre-existing behavior (``mail_search`` has always stripped before
    parsing; ``search``, ``attachments``, and ``contact-history`` never
    have), so a non-blank value is returned exactly as received and each
    caller decides for itself whether to strip before handing it to its own
    parser.
    """
    if value is None:
        return None
    return value if value.strip() else None


def format_invalid_date_message(
    value: object, parameter_name: str, *, expected: str
) -> str:
    """Build the shared rejection message for a malformed date bound.

    Every entry point that rejects an unparseable date string raises
    through its own parser, but all of them word the message the same
    *shape* by building it here: the offending value and the name of the
    parameter it arrived through (``--from-date``, ``--to-date``,
    ``--date-from``, ``--date-to``, ``from_date``, or ``to_date``).

    This function does not own the accepted-set wording, only the shape.
    The four callers run three different parsers with three genuinely
    different accepted sets (see module docstring), so each caller must
    supply its own ``expected`` clause describing what it actually
    accepts -- e.g. ``"YYYY-MM-DD"`` for ``contact-history``'s
    ``strptime("%Y-%m-%d")``, versus a wider clause naming the basic-date
    and timestamp forms for ``search`` / ``attachments``. A caller that
    hardcodes a set here would mislead the surfaces that accept a
    different one.
    """
    return f"Invalid {parameter_name}: {value!r}. Expected {expected}."

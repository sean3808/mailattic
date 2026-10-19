from __future__ import annotations

import ast
import inspect
import unittest
from datetime import UTC, date, datetime, timedelta

from mailattic import datetime_bounds
from mailattic.datetime_bounds import (
    TAIPEI,
    coerce_to_taipei_aware,
    format_invalid_date_message,
    normalize_optional_date_text,
    whole_day_bound,
)


def _imported_module_names(module: object) -> set[str]:
    """Return every module name reachable via ``import`` in ``module``'s source."""
    source = inspect.getsource(module)  # type: ignore[arg-type]
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        # Relative imports (``from .query import x``) have ``module`` set
        # to the target name with ``level`` indicating the dot count.
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class WholeDayBoundTests(unittest.TestCase):
    """design.md > 'Set the upper bound at the last whole second of the Taipei day'."""

    def test_lower_bound_is_midnight_taipei(self) -> None:
        result = whole_day_bound(date(2026, 7, 21))
        self.assertEqual(result, datetime(2026, 7, 21, 0, 0, 0, tzinfo=TAIPEI))
        self.assertEqual(result.utcoffset(), timedelta(hours=8))

    def test_upper_bound_is_last_whole_second_taipei(self) -> None:
        result = whole_day_bound(date(2026, 7, 21), end_of_day=True)
        self.assertEqual(result, datetime(2026, 7, 21, 23, 59, 59, tzinfo=TAIPEI))
        self.assertEqual(result.utcoffset(), timedelta(hours=8))

    def test_builder_has_no_string_parsing_entry_point(self) -> None:
        # A bare string is not a date; the builder must not silently parse
        # it, so passing one is expected to fail rather than succeed.
        with self.assertRaises((TypeError, AttributeError)):
            whole_day_bound("2026-07-21")  # type: ignore[arg-type]

    def test_module_contains_no_string_parsing(self) -> None:
        # Checked as actual call sites, not a raw text scan: the module's
        # own docstrings legitimately *name* the other surfaces' parsers
        # (e.g. "contact-history's strptime(...)") to explain why parsing
        # is deliberately kept out of this module -- that prose reference
        # must not itself trip this test.
        source = inspect.getsource(datetime_bounds)
        tree = ast.parse(source)
        banned_calls = {"fromisoformat", "strptime"}
        called_names = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        offending_calls = called_names & banned_calls
        self.assertEqual(
            offending_calls,
            set(),
            msg=(
                "datetime_bounds must not call a date-string parser, "
                f"found: {offending_calls}"
            ),
        )
        self.assertNotIn("dateutil", _imported_module_names(datetime_bounds))


class NaiveDatetimeCoercionTests(unittest.TestCase):
    """design.md > 'Normalize date strings at the entry boundary and coerce
    naive datetimes at the query layer'."""

    def test_aware_datetime_returned_unchanged(self) -> None:
        aware = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
        result = coerce_to_taipei_aware(aware)
        self.assertEqual(result, aware)
        self.assertEqual(result.tzinfo, UTC)

    def test_naive_datetime_gets_taipei_attached(self) -> None:
        naive = datetime(2026, 7, 21, 12, 0, 0)
        result = coerce_to_taipei_aware(naive)
        self.assertIsNotNone(result.tzinfo)
        self.assertEqual(result.utcoffset(), timedelta(hours=8))
        # The clock reading itself must not shift -- only the label changes.
        self.assertEqual(result.replace(tzinfo=None), naive)


class DiagnosticFormatterTests(unittest.TestCase):
    """design.md > Implementation Contract > Interface: 'A diagnostic
    formatter builds the rejection message from the offending value and the
    name of the parameter it arrived through'.

    The formatter owns only the message *shape*, not the accepted-set
    wording: the four calling surfaces run three different parsers with
    three genuinely different accepted sets (design.md > "Hold the
    whole-day rule in one dedicated module, and keep parsing out of it"),
    so each caller supplies its own ``expected`` clause rather than the
    formatter asserting one on their behalf.
    """

    def test_message_contains_offending_value_and_parameter_name(self) -> None:
        message = format_invalid_date_message(
            "2026-201", "from_date", expected="YYYY-MM-DD or an ISO-8601 timestamp"
        )
        self.assertIn("2026-201", message)
        self.assertIn("from_date", message)

    def test_message_names_a_different_parameter(self) -> None:
        message = format_invalid_date_message(
            "not-a-date", "--to-date", expected="YYYY-MM-DD or an ISO-8601 timestamp"
        )
        self.assertIn("not-a-date", message)
        self.assertIn("--to-date", message)

    def test_message_contains_the_caller_supplied_expectation(self) -> None:
        # contact-history's strptime("%Y-%m-%d") does not accept 20260721,
        # 2026-W30-2, or any timestamp -- so its own expectation clause must
        # appear verbatim, not a formatter-owned generic one that would
        # mislead this surface's caller into retrying with a rejected form.
        message = format_invalid_date_message(
            "20260721", "--date-from", expected="YYYY-MM-DD"
        )
        self.assertIn("20260721", message)
        self.assertIn("--date-from", message)
        self.assertIn("YYYY-MM-DD", message)
        self.assertNotIn("ISO-8601 timestamp", message)


class ModuleIsolationTests(unittest.TestCase):
    """design.md > 'Hold the whole-day rule in one dedicated module, and
    keep parsing out of it': the module must import nothing from query,
    contact_history, or cli, so all of them can import it without a cycle."""

    def test_module_imports_nothing_from_query_contact_history_or_cli(self) -> None:
        imported = _imported_module_names(datetime_bounds)
        banned = {
            "query",
            "contact_history",
            "cli",
            "mailattic.query",
            "mailattic.contact_history",
            "mailattic.cli",
        }
        offending = imported & banned
        self.assertEqual(
            offending,
            set(),
            msg=(
                "datetime_bounds must not import from query/contact_history/cli, "
                f"found: {offending}"
            ),
        )


class NormalizeOptionalDateTextTests(unittest.TestCase):
    """design.md > 'Reject a malformed date instead of letting it match
    nothing, and treat a blank value as an absent filter': the one piece
    of that rule shared across all four entry points.
    """

    def test_none_is_absent(self) -> None:
        self.assertIsNone(normalize_optional_date_text(None))

    def test_empty_string_is_absent(self) -> None:
        self.assertIsNone(normalize_optional_date_text(""))

    def test_whitespace_only_is_absent(self) -> None:
        self.assertIsNone(normalize_optional_date_text("   "))

    def test_non_blank_value_is_returned_unstripped(self) -> None:
        # Whitespace *tolerance* is not this shared rule -- each of the four
        # surfaces owns that decision for itself (search/attachments/
        # contact-history never stripped; mail_search always has).
        self.assertEqual(
            normalize_optional_date_text("  2026-07-21  "), "  2026-07-21  "
        )


if __name__ == "__main__":
    unittest.main()

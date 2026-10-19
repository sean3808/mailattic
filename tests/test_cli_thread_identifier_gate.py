"""CLI-level tests for the `thread` command's identifier magnitude gate.

Change fix-thread-resolver-out-of-range-identifier, task 3.2 (design.md >
"Map the exception at each entry to that entry's native error idiom", CLI
bullet): `_handle_thread` no longer pre-classifies the identifier itself --
it passes the stripped raw string straight to `walk_thread` and lets the
shared resolver's magnitude gate decide, mapping the dedicated
`ThreadIdentifierError` to exit status 2 (distinct from not-found's exit 1).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mailattic.cli import build_parser
from mailattic.query_commands import _handle_thread
from mailattic.storage import initialize_database


def _thread_args(database_path: Path, identifier: str) -> object:
    """Build a parsed Namespace for `thread` via the real argparse parser,
    so every attribute `_handle_thread` reads is populated exactly as a
    live CLI invocation would populate it."""
    return build_parser().parse_args(
        ["thread", "--database", str(database_path), identifier]
    )


def test_thread_over_bound_identifier_exits_2_with_one_line_diagnostic(
    capsys,
) -> None:
    """A 19-20 digit string one past the signed 64-bit max exits 2 with a
    single-line stderr message and no traceback."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = initialize_database(Path(tmp) / "gate.db")

        exit_code = _handle_thread(_thread_args(db_path, "9223372036854775808"))

    assert exit_code == 2
    captured = capsys.readouterr()
    stderr_lines = captured.err.strip().splitlines()
    assert len(stderr_lines) == 1
    assert "Traceback" not in captured.err


def test_thread_4301_effective_digit_identifier_exits_2(capsys) -> None:
    """A string of 4301 effective digits exits 2 -- rejected by length
    alone, never reaching CPython's int() digit-limit crash."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = initialize_database(Path(tmp) / "gate.db")

        exit_code = _handle_thread(_thread_args(db_path, "9" * 4301))

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.err.strip() != ""
    assert "Traceback" not in captured.err


def test_thread_in_range_unknown_identifier_still_exits_1(capsys) -> None:
    """An in-range but unknown identifier keeps exiting 1, distinct from
    the over-bound gate's exit 2."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = initialize_database(Path(tmp) / "gate.db")

        exit_code = _handle_thread(_thread_args(db_path, "999999"))

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "999999" in captured.err

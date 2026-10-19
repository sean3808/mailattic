"""tests/test_raw_headers.py — End-to-end acceptance tests for the
preserve-raw-headers change (task 9, coverage points a-f).

Coverage:
  (a) ingest with multiple Received headers (including one 8-bit non-UTF-8 byte)
      → raw_headers.encode('latin-1') is byte-for-byte identical to the source
      header section bytes (order / repeated Received / folding / 8-bit byte).
  (b) degenerate message with no header/body separator ingests without failure
      and raw_headers is NULL (not empty string).
  (c) `show --headers` on a row with header data prints the raw text, exit 0;
      on a NULL row prints the "no data" message, exit 0.
  (d) `show --headers --format json` and `show --headers --json` both exit non-0.
  (e) backfill script: unique-match row gets filled + idempotent (updated=0 on
      second run); multi-match row is counted as skipped, not updated.
  (f) raw_headers text does NOT enter FTS — searching for a token present only
      in the raw header section returns no results.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure src/ and scripts/ are importable (mirrors tests/__init__.py approach
# used by test_backfill_thread_key.py and test_attachment_backfill.py).
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
for _p in (str(SRC_ROOT), str(SCRIPTS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backfill_raw_headers import run_backfill  # noqa: E402

from mailattic.cli import main as cli_main  # noqa: E402
from mailattic.models import (  # noqa: E402
    PstSource,
    build_normalized_message,
)
from mailattic.query import (  # noqa: E402
    SearchFilters,
    get_message,
    search_messages,
)
from mailattic.storage import (  # noqa: E402
    connect_database,
    initialize_database,
)
from mailattic.storage import (  # noqa: E402
    persist_message as storage_persist_message,
)

# ---------------------------------------------------------------------------
# Shared fixture constants
# ---------------------------------------------------------------------------

# A raw RFC5322 message bytes fixture with:
#   - multiple Received headers (order matters, folding preserved)
#   - one 8-bit non-UTF-8 byte (\xff) embedded in a Received header comment
#   - CRLF line endings (RFC-compliant)
#
# The \xff byte cannot be decoded as UTF-8 (it is the start byte of a 2-byte
# sequence but is orphaned here).  Latin-1 maps it to U+00FF bijectively.
_MULTI_RECEIVED_HEADERS_BYTES: bytes = (
    b"Received: from relay2.example.com (relay2 [10.0.0.2])\r\n"
    b"\tby mx.example.com with ESMTP id abc123; Mon, 01 Jan 2024 12:00:00 +0000\r\n"
    b"Received: from client.example.com (unknown [\xff.0.0.1])\r\n"
    b"\tby relay2.example.com with ESMTP id def456; Mon, 01 Jan 2024 11:59:00 +0000\r\n"
    b"Message-ID: <test-raw-headers-8bit@example.com>\r\n"
    b"From: Alice <alice@example.com>\r\n"
    b"To: Bob <bob@example.com>\r\n"
    b"Subject: Raw headers 8-bit test\r\n"
    b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
    b"\r\n"
    b"Body text here.\r\n"
)

# Expected header section bytes (everything before the first \r\n\r\n separator).
_EXPECTED_HEADER_BYTES: bytes = _MULTI_RECEIVED_HEADERS_BYTES.split(b"\r\n\r\n", 1)[0]

# A degenerate message with no blank-line separator — no body at all.
_NO_SEPARATOR_BYTES: bytes = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: Bob <bob@example.com>\r\n"
    b"Subject: Degenerate no-separator\r\n"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pst_source(path: Path) -> PstSource:
    return PstSource(
        path=path,
        input_mode="file",
        requested_from=path,
    )


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    """Invoke cli main() capturing stdout/stderr. Returns (exit_code, out, err)."""
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = StringIO()
    sys.stderr = StringIO()
    try:
        exit_code = cli_main(args)
    except SystemExit as e:
        exit_code = int(e.code) if e.code is not None else 0
    finally:
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        sys.stdout, sys.stderr = old_stdout, old_stderr
    return exit_code, out, err


def _persist_raw_message(
    db_path: Path,
    raw_message_dict: dict,
    pst_source: PstSource,
    folder_path: str = "Inbox",
    logical_locator: str = "Inbox/1",
) -> int:
    """Build NormalizedMessage from dict, persist to DB, return db_id."""
    msg = build_normalized_message(
        raw_message_dict,
        source=pst_source,
        folder_path=folder_path,
        logical_locator=logical_locator,
    )
    conn = connect_database(db_path)
    try:
        result = storage_persist_message(conn, msg)
        conn.commit()
    finally:
        conn.close()
    return result.stored_message_id


def _get_raw_headers_from_db(db_path: Path, db_id: int) -> str | None:
    conn = connect_database(db_path)
    try:
        row = conn.execute(
            "SELECT raw_headers FROM messages WHERE id = ?", [db_id]
        ).fetchone()
        if row is None:
            raise KeyError(f"No row with id={db_id}")
        return row["raw_headers"]
    finally:
        conn.close()


def _write_eml_fixture(output_dir: Path, filename: str, raw_bytes: bytes) -> Path:
    """Write raw message bytes as a .eml file inside output_dir."""
    eml_path = output_dir / filename
    eml_path.write_bytes(raw_bytes)
    return eml_path


def _write_fake_readpst_for_eml(temp_root: Path, fixture_dir: Path) -> Path:
    """Write a fake readpst .cmd that copies fixture_dir into the -o output dir.

    The fixture_dir should already contain the .eml files to be served.
    Uses robocopy to copy the entire fixture tree (same pattern as
    test_attachment_backfill.py) so that ReadpstParser.iter_extraction_results
    picks up the *.eml files via rglob.
    """
    fake_readpst = temp_root / "fake-readpst.cmd"
    fake_readpst.write_text(
        "\n".join(
            [
                "@echo off",
                "setlocal EnableExtensions",
                'set "out="',
                ":parse",
                'if "%~1"=="" goto done',
                'if /I "%~1"=="-o" (',
                '  set "out=%~2"',
                "  shift",
                "  shift",
                "  goto parse",
                ")",
                "shift",
                "goto parse",
                ":done",
                "if not defined out exit /b 2",
                f'robocopy "{fixture_dir}" "%out%" /E /NFL /NDL /NJH /NJS /NP >nul',
                "if %ERRORLEVEL% GEQ 8 exit /b %ERRORLEVEL%",
                "exit /b 0",
            ]
        ),
        encoding="utf-8",
    )
    return fake_readpst


# ---------------------------------------------------------------------------
# (a) 8-bit byte preservation — ingest → raw_headers byte-for-byte round-trip
# ---------------------------------------------------------------------------


class TestRawHeadersByteForByte(unittest.TestCase):
    """(a) After ingesting a message with multiple Received headers (including
    one 8-bit non-UTF-8 byte \\xff), raw_headers.encode('latin-1') must be
    byte-for-byte identical to the source header section bytes.
    """

    def test_raw_headers_byte_for_byte_roundtrip_with_8bit_byte(self) -> None:
        # Build a NormalizedMessage that carries raw_headers via the parser
        # internal function.  We invoke _extract_raw_headers directly (it is
        # the tested boundary) and then persist it via build_normalized_message
        # with the raw_headers pre-supplied in the dict.
        from mailattic.parser import (
            _extract_raw_headers,  # type: ignore[attr-defined]
        )

        extracted = _extract_raw_headers(_MULTI_RECEIVED_HEADERS_BYTES)

        self.assertIsNotNone(extracted, "Expected non-None for well-formed fixture")
        assert extracted is not None  # narrow type

        # Round-trip: encoding back to bytes must be identical to source header bytes.
        roundtrip_bytes = extracted.encode("latin-1")
        self.assertEqual(
            roundtrip_bytes,
            _EXPECTED_HEADER_BYTES,
            "raw_headers.encode('latin-1') must be byte-for-byte identical to "
            "the source header section",
        )

    def test_raw_headers_preserved_through_ingest_and_query(self) -> None:
        """After a full ingest → persist → get_message cycle, raw_headers
        round-trips correctly (byte-for-byte via latin-1 encode)."""
        from mailattic.parser import (
            _extract_raw_headers,  # type: ignore[attr-defined]
        )

        extracted = _extract_raw_headers(_MULTI_RECEIVED_HEADERS_BYTES)
        assert extracted is not None

        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "evidence.db")
            fake_pst = Path(tmp) / "fake.pst"
            fake_pst.write_bytes(b"")
            source = _make_pst_source(fake_pst)

            db_id = _persist_raw_message(
                db_path,
                {
                    "subject": "8-bit raw headers",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-01T12:00:00",
                    "body_text": "Body text here.",
                    "message_id": "<test-raw-headers-8bit@example.com>",
                    # Supply pre-extracted raw_headers so we exercise the storage
                    # path without needing a real readpst binary.
                    "raw_headers": extracted,
                },
                source,
            )

            stored = get_message(db_path, db_id)

        stored_raw = stored.get("raw_headers")
        self.assertIsNotNone(stored_raw)
        self.assertEqual(
            stored_raw.encode("latin-1"),  # type: ignore[union-attr]
            _EXPECTED_HEADER_BYTES,
        )

    def test_repeated_received_headers_and_folding_preserved(self) -> None:
        """Both Received header lines (with folded continuation) must survive
        the latin-1 round-trip unchanged."""
        from mailattic.parser import (
            _extract_raw_headers,  # type: ignore[attr-defined]
        )

        extracted = _extract_raw_headers(_MULTI_RECEIVED_HEADERS_BYTES)
        assert extracted is not None

        # The folded whitespace (\\t) must be present in the round-trip.
        # Each Received header has a folded continuation line starting with \\t + "by".
        roundtrip = extracted.encode("latin-1")
        self.assertIn(b"\tby mx.example.com with ESMTP id abc123", roundtrip)
        self.assertIn(b"\tby relay2.example.com with ESMTP id def456", roundtrip)

        # The 8-bit byte \\xff must be present exactly once (in the second
        # Received header).
        self.assertEqual(roundtrip.count(b"\xff"), 1)

    def test_lf_only_separator_also_works(self) -> None:
        """LF-only separator (Unix-style) is also recognised."""
        from mailattic.parser import (
            _extract_raw_headers,  # type: ignore[attr-defined]
        )

        lf_bytes = b"From: alice@example.com\nTo: bob@example.com\n\nBody\n"
        extracted = _extract_raw_headers(lf_bytes)
        self.assertIsNotNone(extracted)
        expected = b"From: alice@example.com\nTo: bob@example.com"
        self.assertEqual(extracted.encode("latin-1"), expected)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# (b) Degenerate message — no header/body separator → raw_headers is NULL
# ---------------------------------------------------------------------------


class TestDegenerateNoSeparator(unittest.TestCase):
    """(b) A message with no blank-line separator ingests without failure
    and raw_headers is NULL (never empty string).
    """

    def test_extract_raw_headers_returns_none_for_no_separator(self) -> None:
        from mailattic.parser import (
            _extract_raw_headers,  # type: ignore[attr-defined]
        )

        result = _extract_raw_headers(_NO_SEPARATOR_BYTES)
        self.assertIsNone(
            result,
            "Expected None when no blank-line separator is present",
        )

    def test_extract_raw_headers_returns_none_for_empty_input(self) -> None:
        from mailattic.parser import (
            _extract_raw_headers,  # type: ignore[attr-defined]
        )

        self.assertIsNone(_extract_raw_headers(b""))
        self.assertIsNone(_extract_raw_headers(None))  # type: ignore[arg-type]

    def test_extract_raw_headers_returns_none_when_separator_at_byte_zero(
        self,
    ) -> None:
        """When bytes start with the separator (\\r\\n\\r\\n or \\n\\n at position 0),
        the header section slice is empty bytes — must return None, not ''."""
        from mailattic.parser import (
            _extract_raw_headers,  # type: ignore[attr-defined]
        )

        # CRLF separator at position 0: header slice = b"" → must be None.
        result_crlf = _extract_raw_headers(b"\r\n\r\nbody text here")
        self.assertIsNone(
            result_crlf,
            "Expected None when CRLF separator is at byte 0 (empty header section)",
        )
        self.assertNotEqual(
            result_crlf,
            "",
            "Must not return empty string — NULL sentinel rule applies",
        )

        # LF-only separator at position 0: header slice = b"" → must be None.
        result_lf = _extract_raw_headers(b"\n\nbody text here")
        self.assertIsNone(
            result_lf,
            "Expected None when LF separator is at byte 0 (empty header section)",
        )

    def test_ingest_degenerate_message_does_not_raise(self) -> None:
        """Ingesting a degenerate message (raw_headers=None) must not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "evidence.db")
            fake_pst = Path(tmp) / "fake.pst"
            fake_pst.write_bytes(b"")
            source = _make_pst_source(fake_pst)

            # raw_headers deliberately omitted (will be None via
            # _coerce_optional_raw_headers).
            db_id = _persist_raw_message(
                db_path,
                {
                    "subject": "Degenerate no-separator",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-02T00:00:00",
                    "body_text": "some body",
                    "message_id": "<degenerate-no-sep@example.com>",
                    # raw_headers absent → None
                },
                source,
                logical_locator="Inbox/2",
            )

            raw = _get_raw_headers_from_db(db_path, db_id)

        self.assertIsNone(raw, "raw_headers must be NULL (None), not empty string")

    def test_raw_headers_null_is_not_empty_string(self) -> None:
        """Explicit check: the DB column value must be NULL, not ''."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "evidence.db")
            fake_pst = Path(tmp) / "fake.pst"
            fake_pst.write_bytes(b"")
            source = _make_pst_source(fake_pst)

            db_id = _persist_raw_message(
                db_path,
                {
                    "subject": "Null check",
                    "sender": {"email": "null@example.com"},
                    "sent_at": "2024-01-03T00:00:00",
                    "body_text": "body",
                    "message_id": "<null-check@example.com>",
                },
                source,
                logical_locator="Inbox/3",
            )

            conn = connect_database(db_path)
            try:
                row = conn.execute(
                    "SELECT raw_headers FROM messages WHERE id = ?", [db_id]
                ).fetchone()
            finally:
                conn.close()

        # SQLite returns Python None for NULL; empty string would be falsy but
        # technically not None.
        self.assertIsNone(row["raw_headers"])
        self.assertNotEqual(row["raw_headers"], "")


# ---------------------------------------------------------------------------
# (c) CLI: show --headers output modes
# ---------------------------------------------------------------------------


class TestShowHeadersCLI(unittest.TestCase):
    """(c) `bes show <id> --headers`:
    - row with raw_headers → prints verbatim, exit 0
    - row with raw_headers NULL → prints "no header" message, exit 0
    """

    def _setup_db_with_two_rows(self, tmp: str) -> tuple[Path, int, int]:
        """Returns (db_path, id_with_headers, id_without_headers)."""
        from mailattic.parser import (
            _extract_raw_headers,  # type: ignore[attr-defined]
        )

        db_path = initialize_database(Path(tmp) / "evidence.db")
        fake_pst = Path(tmp) / "fake.pst"
        fake_pst.write_bytes(b"")
        source = _make_pst_source(fake_pst)

        extracted = _extract_raw_headers(_MULTI_RECEIVED_HEADERS_BYTES)

        id_with = _persist_raw_message(
            db_path,
            {
                "subject": "Has headers",
                "sender": {"email": "alice@example.com"},
                "to": [{"email": "bob@example.com"}],
                "sent_at": "2024-01-01T12:00:00",
                "body_text": "body with headers",
                "message_id": "<cli-headers-present@example.com>",
                "raw_headers": extracted,
            },
            source,
            logical_locator="Inbox/10",
        )
        id_null = _persist_raw_message(
            db_path,
            {
                "subject": "No headers",
                "sender": {"email": "carol@example.com"},
                "to": [{"email": "dave@example.com"}],
                "sent_at": "2024-01-02T12:00:00",
                "body_text": "body no headers",
                "message_id": "<cli-headers-null@example.com>",
                # raw_headers absent → NULL
            },
            source,
            logical_locator="Inbox/11",
        )
        return db_path, id_with, id_null

    def test_show_headers_with_value_exits_0_and_prints_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path, id_with, _ = self._setup_db_with_two_rows(tmp)
            code, out, _ = _run_cli(
                ["show", "--database", str(db_path), "--headers", str(id_with)]
            )

        self.assertEqual(code, 0)
        # The raw header text must contain the Received headers from our fixture.
        self.assertIn("Received:", out)
        self.assertIn("relay2.example.com", out)

    def test_show_headers_output_is_verbatim_no_extra_trailing_newline(self) -> None:
        """show --headers must NOT add an extra trailing newline beyond what is
        already in the stored raw_headers string.  The output must equal the
        stored value exactly (sys.stdout.write, not print)."""
        from mailattic.parser import (
            _extract_raw_headers,  # type: ignore[attr-defined]
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "evidence.db")
            fake_pst = Path(tmp) / "fake.pst"
            fake_pst.write_bytes(b"")
            source = _make_pst_source(fake_pst)

            extracted = _extract_raw_headers(_MULTI_RECEIVED_HEADERS_BYTES)
            assert extracted is not None

            db_id = _persist_raw_message(
                db_path,
                {
                    "subject": "Verbatim output test",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-01T12:00:00",
                    "body_text": "body",
                    "message_id": "<verbatim-output-test@example.com>",
                    "raw_headers": extracted,
                },
                source,
                logical_locator="Inbox/50",
            )

            code, out, _ = _run_cli(
                ["show", "--database", str(db_path), "--headers", str(db_id)]
            )

        self.assertEqual(code, 0)
        # stdout.write(raw_headers) must produce output equal to the stored value —
        # no extra trailing newline appended by print().
        self.assertEqual(
            out,
            extracted,
            "show --headers output must be byte-for-byte equal to stored raw_headers "
            "(sys.stdout.write must not add an extra trailing newline)",
        )

    def test_show_headers_null_row_exits_0_and_prints_no_data_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _, id_null = self._setup_db_with_two_rows(tmp)
            code, out, _ = _run_cli(
                ["show", "--database", str(db_path), "--headers", str(id_null)]
            )

        self.assertEqual(code, 0)
        # The output must indicate no header data is available.
        self.assertIn("No raw header data available", out)

    def test_show_headers_null_row_output_is_not_empty(self) -> None:
        """The user must receive an explicit message, not silence."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _, id_null = self._setup_db_with_two_rows(tmp)
            code, out, _ = _run_cli(
                ["show", "--database", str(db_path), "--headers", str(id_null)]
            )

        self.assertEqual(code, 0)
        self.assertTrue(out.strip(), "Output must not be blank for NULL row")


# ---------------------------------------------------------------------------
# (d) CLI: show --headers --format json / --json must exit non-0
# ---------------------------------------------------------------------------


class TestShowHeadersMutualExclusion(unittest.TestCase):
    """(d) `--headers` is mutually exclusive with `--format` (any value) and
    `--json`; combining them must exit non-0 with a stderr diagnostic.
    """

    def _make_minimal_db(self, tmp: str) -> tuple[Path, int]:
        db_path = initialize_database(Path(tmp) / "evidence.db")
        fake_pst = Path(tmp) / "fake.pst"
        fake_pst.write_bytes(b"")
        source = _make_pst_source(fake_pst)
        db_id = _persist_raw_message(
            db_path,
            {
                "subject": "Mutual exclusion test",
                "sender": {"email": "test@example.com"},
                "sent_at": "2024-01-01T00:00:00",
                "body_text": "body",
                "message_id": "<mutex-test@example.com>",
            },
            source,
            logical_locator="Inbox/20",
        )
        return db_path, db_id

    def test_headers_with_format_json_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = self._make_minimal_db(tmp)
            code, _, err = _run_cli(
                [
                    "show",
                    "--database",
                    str(db_path),
                    "--headers",
                    "--format",
                    "json",
                    str(db_id),
                ]
            )

        self.assertNotEqual(code, 0)
        self.assertTrue(err.strip(), "stderr must contain a diagnostic message")

    def test_headers_with_json_flag_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = self._make_minimal_db(tmp)
            code, _, err = _run_cli(
                [
                    "show",
                    "--database",
                    str(db_path),
                    "--headers",
                    "--json",
                    str(db_id),
                ]
            )

        self.assertNotEqual(code, 0)
        self.assertTrue(err.strip(), "stderr must contain a diagnostic message")

    def test_headers_with_format_raw_exits_nonzero(self) -> None:
        """--headers --format raw must also be a conflict."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = self._make_minimal_db(tmp)
            code, _, err = _run_cli(
                [
                    "show",
                    "--database",
                    str(db_path),
                    "--headers",
                    "--format",
                    "raw",
                    str(db_id),
                ]
            )

        self.assertNotEqual(code, 0)

    def test_headers_with_format_text_exits_nonzero(self) -> None:
        """--headers --format text must also be a conflict."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = self._make_minimal_db(tmp)
            code, _, err = _run_cli(
                [
                    "show",
                    "--database",
                    str(db_path),
                    "--headers",
                    "--format",
                    "text",
                    str(db_id),
                ]
            )

        self.assertNotEqual(code, 0)

    def test_headers_alone_exits_zero(self) -> None:
        """Sanity: --headers without --format / --json must exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path, db_id = self._make_minimal_db(tmp)
            code, _, _ = _run_cli(
                ["show", "--database", str(db_path), "--headers", str(db_id)]
            )

        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# (e) Backfill script — unique match, idempotency, multi-match skipped
# ---------------------------------------------------------------------------


class TestBackfillRawHeaders(unittest.TestCase):
    """(e) backfill_raw_headers.run_backfill:
    - unique match fills NULL row
    - idempotent: second run reports updated=0
    - multi-match row counted as skipped / unmatched, not updated
    """

    def _write_fake_readpst_for_eml(self, temp_root: Path, fixture_dir: Path) -> Path:
        return _write_fake_readpst_for_eml(temp_root, fixture_dir)

    def _setup_eml_fixture_dir(
        self, temp_root: Path, raw_bytes: bytes, name: str = "msg.eml"
    ) -> Path:
        """Create a fixture_dir containing one .eml file."""
        fixture_dir = temp_root / "fixture_eml"
        fixture_dir.mkdir(exist_ok=True)
        (fixture_dir / name).write_bytes(raw_bytes)
        return fixture_dir

    def test_unique_match_fills_null_raw_headers(self) -> None:
        """A row ingested without raw_headers gets updated by the backfill."""
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            db_path = initialize_database(temp_root / "evidence.db")
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_bytes(b"placeholder")

            source = _make_pst_source(fake_pst)

            # Ingest a row WITHOUT raw_headers (simulates historical PST row).
            db_id = _persist_raw_message(
                db_path,
                {
                    "subject": "Raw headers 8-bit test",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-01T12:00:00",
                    "body_text": "Body text here.",
                    "message_id": "<test-raw-headers-8bit@example.com>",
                    # raw_headers deliberately absent → NULL
                },
                source,
                logical_locator="Inbox/1",
            )

            # Confirm it is NULL before backfill.
            self.assertIsNone(_get_raw_headers_from_db(db_path, db_id))

            # Build EML fixture dir and fake readpst.
            fixture_dir = self._setup_eml_fixture_dir(
                temp_root, _MULTI_RECEIVED_HEADERS_BYTES
            )
            fake_readpst = self._write_fake_readpst_for_eml(temp_root, fixture_dir)

            updated, unmatched, skipped = run_backfill(
                db_path,
                [fake_pst],
                readpst_path=fake_readpst,
                work_root=temp_root,
            )

            self.assertEqual(updated, 1, "Expected exactly 1 row updated")
            self.assertEqual(skipped, 0)
            self.assertGreaterEqual(unmatched, 0)

            # Verify the value stored is the correct header bytes (round-trip).
            # Must be done INSIDE the with-block while tempdir still exists.
            stored = get_message(db_path, db_id)
            stored_raw = stored.get("raw_headers")
            self.assertIsNotNone(stored_raw)
            self.assertEqual(
                stored_raw.encode("latin-1"),  # type: ignore[union-attr]
                _EXPECTED_HEADER_BYTES,
            )

    def test_backfill_is_idempotent(self) -> None:
        """Running the backfill twice: second run reports updated=0."""
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            db_path = initialize_database(temp_root / "evidence.db")
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_bytes(b"placeholder")

            source = _make_pst_source(fake_pst)
            _persist_raw_message(
                db_path,
                {
                    "subject": "Raw headers 8-bit test",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-01T12:00:00",
                    "body_text": "Body text here.",
                    "message_id": "<test-raw-headers-8bit@example.com>",
                },
                source,
                logical_locator="Inbox/1",
            )

            fixture_dir = self._setup_eml_fixture_dir(
                temp_root, _MULTI_RECEIVED_HEADERS_BYTES
            )
            fake_readpst = self._write_fake_readpst_for_eml(temp_root, fixture_dir)

            # First run — fills the row.
            updated1, _, _ = run_backfill(
                db_path,
                [fake_pst],
                readpst_path=fake_readpst,
                work_root=temp_root,
            )

            # Second run — must not update anything (idempotent).
            updated2, _, skipped2 = run_backfill(
                db_path,
                [fake_pst],
                readpst_path=fake_readpst,
                work_root=temp_root,
            )

        self.assertEqual(updated1, 1, "First run must update 1 row")
        self.assertEqual(updated2, 0, "Second run must update 0 rows (idempotent)")
        self.assertGreater(
            skipped2, 0, "Second run must count the already-filled row as skipped"
        )

    def test_body_text_unchanged_after_backfill(self) -> None:
        """Backfill must update ONLY raw_headers; body_text must be unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            db_path = initialize_database(temp_root / "evidence.db")
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_bytes(b"placeholder")

            source = _make_pst_source(fake_pst)
            db_id = _persist_raw_message(
                db_path,
                {
                    "subject": "Raw headers 8-bit test",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-01T12:00:00",
                    "body_text": "Body text here.",
                    "message_id": "<test-raw-headers-8bit@example.com>",
                },
                source,
                logical_locator="Inbox/1",
            )

            fixture_dir = self._setup_eml_fixture_dir(
                temp_root, _MULTI_RECEIVED_HEADERS_BYTES
            )
            fake_readpst = self._write_fake_readpst_for_eml(temp_root, fixture_dir)

            run_backfill(
                db_path,
                [fake_pst],
                readpst_path=fake_readpst,
                work_root=temp_root,
            )

            msg = get_message(db_path, db_id)

        # body_text must be unchanged.
        self.assertIn("Body text here", str(msg.get("body_text", "")))

    def test_multi_match_counts_as_skipped_not_updated(self) -> None:
        """Two rows sharing the same dedup identity → multi-match → counted in
        unmatched (>1 match), not updated; no row is incorrectly modified.

        Strategy: build a custom DB without the UNIQUE indexes so we can
        insert two rows with the same dedup_key.  Both the production schema's
        dedup_key and message_id columns carry UNIQUE constraints that prevent
        duplicate values via normal INSERT, so we create a minimal schema that
        mirrors the column layout but omits those constraints.

        The EML fixture has a known Message-ID; the backfill lookup will find
        both rows (matching on message_id OR dedup_key) and report unmatched.
        """
        # Minimal schema mirroring messages columns but WITHOUT UNIQUE indexes.
        _NO_UNIQUE_SCHEMA = """
        CREATE TABLE messages (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            subject          TEXT NOT NULL DEFAULT '',
            sender_name      TEXT,
            sender_email     TEXT,
            sent_at          TEXT,
            body_text        TEXT NOT NULL DEFAULT '',
            message_id       TEXT,
            pst_source       TEXT NOT NULL DEFAULT '',
            folder_path      TEXT NOT NULL DEFAULT '',
            logical_locator  TEXT NOT NULL DEFAULT '',
            source_input_mode TEXT NOT NULL DEFAULT '',
            source_requested_from TEXT NOT NULL DEFAULT '',
            dedup_key        TEXT NOT NULL DEFAULT '',
            dedup_basis      TEXT NOT NULL DEFAULT '',
            recipient_search_text TEXT NOT NULL DEFAULT '',
            attachment_search_text TEXT NOT NULL DEFAULT '',
            raw_headers      TEXT
        );
        """

        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_bytes(b"placeholder")

            # Build a DB without UNIQUE constraints so we can force >1 match.
            db_path = temp_root / "multi_match.db"
            raw_conn = sqlite3.connect(str(db_path))
            raw_conn.row_factory = sqlite3.Row
            try:
                raw_conn.executescript(_NO_UNIQUE_SCHEMA)
                raw_conn.commit()
            finally:
                raw_conn.close()

            _SHARED_MSG_ID = "<multi-match-dup@example.com>"
            _SHARED_DEDUP_KEY = f"message-id:{_SHARED_MSG_ID}"

            # Insert two rows with the same message_id + dedup_key.
            raw_conn = sqlite3.connect(str(db_path))
            raw_conn.row_factory = sqlite3.Row
            try:
                for i in range(2):
                    raw_conn.execute(
                        """
                        INSERT INTO messages (
                            subject, sender_email, sender_name,
                            sent_at, body_text, message_id,
                            pst_source, folder_path, logical_locator,
                            dedup_key, dedup_basis,
                            source_input_mode, source_requested_from,
                            recipient_search_text, attachment_search_text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            "Multi match dup",
                            "alice@example.com",
                            "Alice",
                            "2024-01-01T12:00:00",
                            "multi match body",
                            _SHARED_MSG_ID,
                            str(fake_pst),
                            "Inbox",
                            f"Inbox/{i + 60}",
                            _SHARED_DEDUP_KEY,
                            "{}",
                            "file",
                            str(fake_pst),
                            "",
                            "",
                        ],
                    )
                raw_conn.commit()
            finally:
                raw_conn.close()

            # EML fixture with the shared Message-ID so the backfill matches
            # both rows via the message_id OR dedup_key lookup.
            eml_bytes = (
                b"Message-ID: <multi-match-dup@example.com>\r\n"
                b"From: Alice <alice@example.com>\r\n"
                b"To: Bob <bob@example.com>\r\n"
                b"Subject: Multi match dup\r\n"
                b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
                b"\r\n"
                b"multi match body\r\n"
            )
            fixture_dir = self._setup_eml_fixture_dir(temp_root, eml_bytes, "multi.eml")
            fake_readpst = self._write_fake_readpst_for_eml(temp_root, fixture_dir)

            updated, unmatched, skipped = run_backfill(
                db_path,
                [fake_pst],
                readpst_path=fake_readpst,
                work_root=temp_root,
            )

        # Multi-match must NOT produce any updates.
        self.assertEqual(updated, 0, "Multi-match must not update any row")

        # Per spec: >1 match is "ambiguous" → counted in skipped (not unmatched).
        # unmatched is reserved for 0-match only.
        self.assertGreater(
            skipped,
            0,
            "Multi-match row must appear in skipped count (ambiguous), not unmatched",
        )
        self.assertEqual(
            unmatched,
            0,
            "unmatched must be 0 when the only PST message is a multi-match "
            "(ambiguous)",
        )


# ---------------------------------------------------------------------------
# (f) raw_headers does NOT enter FTS
# ---------------------------------------------------------------------------


class TestRawHeadersNotInFTS(unittest.TestCase):
    """(f) Text that appears only in raw_headers (not in subject/body/sender)
    must not be findable via `bes search --keyword`.
    """

    # A token that exists ONLY in the raw header section of our fixture.
    # "relay2.example.com" appears in the Received header, not in the body.
    _HEADER_ONLY_TOKEN = "XUNIQUEXHEADERXTOKENX"

    def _build_eml_bytes_with_unique_token(self) -> bytes:
        """Build raw message bytes where the unique token appears ONLY in
        a Received header, never in the body or subject."""
        return (
            b"Received: from "
            + self._HEADER_ONLY_TOKEN.encode("ascii")
            + b" (host [10.0.0.1])\r\n"
            b"\tby mx.example.com with ESMTP; Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            b"Message-ID: <fts-exclusion-test@example.com>\r\n"
            b"From: Alice <alice@example.com>\r\n"
            b"To: Bob <bob@example.com>\r\n"
            b"Subject: FTS exclusion test\r\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            b"\r\n"
            b"The body does not mention the unique header token.\r\n"
        )

    def test_header_only_token_not_found_in_fts_search(self) -> None:
        """Searching for a token present only in raw_headers returns no hits."""
        from mailattic.parser import (
            _extract_raw_headers,  # type: ignore[attr-defined]
        )

        eml_bytes = self._build_eml_bytes_with_unique_token()
        extracted = _extract_raw_headers(eml_bytes)
        self.assertIsNotNone(extracted)
        # Confirm the unique token IS in the extracted raw_headers.
        self.assertIn(self._HEADER_ONLY_TOKEN, extracted)  # type: ignore[operator]

        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "evidence.db")
            fake_pst = Path(tmp) / "fake.pst"
            fake_pst.write_bytes(b"")
            source = _make_pst_source(fake_pst)

            _persist_raw_message(
                db_path,
                {
                    "subject": "FTS exclusion test",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-01T12:00:00",
                    "body_text": "The body does not mention the unique header token.",
                    "message_id": "<fts-exclusion-test@example.com>",
                    "raw_headers": extracted,
                },
                source,
            )

            # Search by the header-only token — must return empty.
            results = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword=self._HEADER_ONLY_TOKEN),
            )

        self.assertEqual(
            len(results),
            0,
            f"Token '{self._HEADER_ONLY_TOKEN}' is in raw_headers only and "
            "must NOT be findable via FTS search",
        )

    def test_body_token_still_findable(self) -> None:
        """Sanity: a token in the body IS findable (FTS itself is working)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = initialize_database(Path(tmp) / "evidence.db")
            fake_pst = Path(tmp) / "fake.pst"
            fake_pst.write_bytes(b"")
            source = _make_pst_source(fake_pst)

            _persist_raw_message(
                db_path,
                {
                    "subject": "FTS sanity check",
                    "sender": {"email": "alice@example.com"},
                    "sent_at": "2024-01-02T00:00:00",
                    "body_text": "XUNIQUEXBODYTOKENX present in body",
                    "message_id": "<fts-sanity@example.com>",
                },
                source,
                logical_locator="Inbox/99",
            )

            results = search_messages(
                database_path=db_path,
                filters=SearchFilters(keyword="XUNIQUEXBODYTOKENX"),
            )

        self.assertEqual(len(results), 1, "Body token must be findable via FTS")


# ---------------------------------------------------------------------------
# (g)(h) mbox branch — CRLF round-trip + backfill via real mbox path
# ---------------------------------------------------------------------------


class TestMboxBranchRawHeaders(unittest.TestCase):
    """(g) ReadpstParser mbox branch correctly captures raw_headers with CRLF.

    Tests that:
    - mbox-sourced messages have raw_headers != None after ingest
    - raw_headers.encode('latin-1') is byte-for-byte identical to the mbox
      entry headers (envelope From line excluded), CRLF preserved
    - Two Received headers survive the round-trip

    (h) backfill script works when re-parsing goes through the mbox branch
    (not just the .eml branch), guarding against 'fake green' from .eml-only
    backfill tests.
    """

    # Expected header bytes for the CRLF mbox fixture (no envelope line).
    # Matches what _extract_raw_headers returns from the mbox entry bytes
    # after from_=False strips the envelope From line.
    # _extract_raw_headers slices up to (but NOT including) the \r\n\r\n separator,
    # so the final header line's trailing \r\n is not included in the slice.
    _CRLF_MBOX_HEADER_BYTES: bytes = (
        b"Received: from relay2.example.com (relay2 [10.0.0.2])\r\n"
        b"\tby mx.example.com with ESMTP id abc123; Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        b"Received: from client.example.com (unknown [192.168.0.1])\r\n"
        b"\tby relay2.example.com with ESMTP id def456; "
        b"Mon, 01 Jan 2024 11:59:00 +0000\r\n"
        b"Message-ID: <mbox-crlf-test@example.com>\r\n"
        b"From: Alice <alice@example.com>\r\n"
        b"To: Bob <bob@example.com>\r\n"
        b"Subject: CRLF mbox test\r\n"
        b"Date: Mon, 01 Jan 2024 12:00:00 +0000"
    )

    def _get_fixture_dir(self) -> Path:
        return REPO_ROOT / "tests" / "fixtures" / "readpst-mbox-crlf"

    def _run_parser_with_mbox_fixture(
        self, temp_root: Path, fake_pst: Path
    ) -> list[dict]:
        """Run ReadpstParser using the CRLF mbox fixture via fake readpst.

        Returns a list of raw_message dicts from mbox-sourced ExtractionResults.
        """
        fixture_dir = self._get_fixture_dir()
        fake_readpst = _write_fake_readpst_for_eml(temp_root, fixture_dir)

        source = _make_pst_source(fake_pst)
        from mailattic.parser import ReadpstParser

        parser = ReadpstParser(readpst_path=fake_readpst, work_root=temp_root)
        results = list(parser.iter_extraction_results(source))

        # Filter to mbox-sourced successful extractions (locator contains "mbox")
        return [
            r.message.raw_message
            for r in results
            if r.message is not None and "mbox" in r.message.logical_locator
        ]

    # --- (g) mbox raw_headers not NULL ---

    def test_mbox_sourced_raw_headers_not_null(self) -> None:
        """mbox-sourced messages must have raw_headers != None after ingest."""
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            fake_pst = temp_root / "fake.pst"
            fake_pst.write_bytes(b"placeholder")
            mbox_messages = self._run_parser_with_mbox_fixture(temp_root, fake_pst)

        self.assertGreater(
            len(mbox_messages),
            0,
            "At least one mbox-sourced message must be extracted",
        )
        raw_headers = mbox_messages[0].get("raw_headers")
        self.assertIsNotNone(
            raw_headers, "mbox-sourced message raw_headers must not be None"
        )

    def test_mbox_raw_headers_crlf_preserved(self) -> None:
        """CRLF line endings in mbox headers must be preserved byte-for-byte.

        This test specifically guards against the get_bytes() CRLF->LF bug:
        if mbox.get_bytes() were used instead of get_file(key, from_=False).read(),
        Windows Python 3.13 would normalize CRLF to LF and this assertion fails.
        """
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            fake_pst = temp_root / "fake.pst"
            fake_pst.write_bytes(b"placeholder")
            mbox_messages = self._run_parser_with_mbox_fixture(temp_root, fake_pst)

        self.assertGreater(len(mbox_messages), 0, "Expected mbox-sourced messages")
        raw_headers = mbox_messages[0].get("raw_headers")
        self.assertIsNotNone(raw_headers)

        roundtrip_bytes = raw_headers.encode("latin-1")  # type: ignore[union-attr]
        self.assertEqual(
            roundtrip_bytes,
            self._CRLF_MBOX_HEADER_BYTES,
            "mbox raw_headers.encode('latin-1') must be byte-for-byte identical "
            "to source header bytes WITH CRLF preserved (\\r\\n not \\n)",
        )

    def test_mbox_raw_headers_has_repeated_received_headers(self) -> None:
        """Both Received headers and their CRLF-folded continuations must survive."""
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            fake_pst = temp_root / "fake.pst"
            fake_pst.write_bytes(b"placeholder")
            mbox_messages = self._run_parser_with_mbox_fixture(temp_root, fake_pst)

        self.assertGreater(len(mbox_messages), 0)
        raw_headers = mbox_messages[0].get("raw_headers")
        self.assertIsNotNone(raw_headers)
        roundtrip = raw_headers.encode("latin-1")  # type: ignore[union-attr]

        # Both Received folded continuations must be present
        self.assertIn(b"\tby mx.example.com with ESMTP id abc123", roundtrip)
        self.assertIn(b"\tby relay2.example.com with ESMTP id def456", roundtrip)
        # CRLF folding must be intact (not collapsed to LF-only)
        self.assertIn(b"\r\n\t", roundtrip)

    # --- (h) backfill via real mbox path ---

    def test_backfill_via_mbox_path_fills_null_row(self) -> None:
        """Backfill script fills a NULL row when re-parsing goes through mbox branch.

        Guards against 'fake green' from existing (e) tests which use .eml fixtures:
        the backfill must also work when the readpst output contains mbox files.
        """
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            db_path = initialize_database(temp_root / "evidence.db")
            fake_pst = temp_root / "sample.pst"
            fake_pst.write_bytes(b"placeholder")

            source = _make_pst_source(fake_pst)

            # Ingest WITHOUT raw_headers (simulates historical PST row).
            db_id = _persist_raw_message(
                db_path,
                {
                    "subject": "CRLF mbox test",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": "2024-01-01T12:00:00",
                    "body_text": "CRLF mbox body text.",
                    "message_id": "<mbox-crlf-test@example.com>",
                    # raw_headers deliberately absent → NULL
                },
                source,
                logical_locator="Inbox/1",
            )

            self.assertIsNone(_get_raw_headers_from_db(db_path, db_id))

            fixture_dir = self._get_fixture_dir()
            fake_readpst = _write_fake_readpst_for_eml(temp_root, fixture_dir)

            updated, unmatched, skipped = run_backfill(
                db_path,
                [fake_pst],
                readpst_path=fake_readpst,
                work_root=temp_root,
            )

            self.assertEqual(updated, 1, "Expected exactly 1 row updated via mbox path")

            stored = get_message(db_path, db_id)
            stored_raw = stored.get("raw_headers")
            self.assertIsNotNone(
                stored_raw, "raw_headers must be filled after backfill"
            )

            # Verify CRLF is preserved in the stored value
            roundtrip = stored_raw.encode("latin-1")  # type: ignore[union-attr]
            self.assertIn(
                b"\r\n", roundtrip, "CRLF must be preserved in stored raw_headers"
            )
            self.assertIn(b"Received:", roundtrip)


if __name__ == "__main__":
    unittest.main()

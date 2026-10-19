from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

import pytest

from mailattic.cli import main
from mailattic.contact_history import aggregate_contact_history
from mailattic.ingest import import_pst_sources
from mailattic.models import ImportSummary, PstSource
from mailattic.outlook_pst import (
    PstExtractionAccount,
    RawOutlookFailure,
    RawOutlookMessage,
    RawOutlookRecipient,
    normalize_outlook_recipients,
    write_pst_extraction_summary,
)
from mailattic.query import (
    SearchFilters,
    search_messages,
)
from mailattic.storage import initialize_database

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeOutlookReader:
    def __init__(self, messages: list[RawOutlookMessage]) -> None:
        self._messages = messages

    def iter_messages(self, source: PstSource):
        del source
        yield from self._messages


def test_import_pst_sources_selects_outlook_parser_and_existing_persistence(
    tmp_path: Path,
) -> None:
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={
                    "subject": "Recipient table source",
                    "sender": {"name": "Sean", "email": "sean@example.com"},
                    "body_text": "Evidence",
                    "message_id": "<outlook-source@example.com>",
                },
                folder_path="Sent Items",
                logical_locator="entry-1",
                display_recipients={"to": "Only a name", "cc": "", "bcc": ""},
                recipients=(
                    RawOutlookRecipient(
                        "Client", "client@example.com", None, "SMTP", 1
                    ),
                ),
            )
        ]
    )

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=tmp_path / "recipient-extraction",
    )

    connection = sqlite3.connect(database_path)
    try:
        stored = connection.execute(
            "SELECT subject, pst_source FROM messages"
        ).fetchone()
    finally:
        connection.close()
    assert summary.imported_messages == 1
    assert summary.failure_count == 0
    assert stored == ("Recipient table source", str(pst_path))


def test_import_pst_sources_keeps_named_message_failure_nonfatal(
    tmp_path: Path,
) -> None:
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    good_message = RawOutlookMessage(
        raw_message={"subject": "Still imported", "body_text": "Evidence"},
        folder_path="Inbox",
        logical_locator="entry-good",
        display_recipients={"to": "", "cc": "", "bcc": ""},
        recipients=(),
    )
    reader = FakeOutlookReader(
        [
            RawOutlookFailure(
                folder_path="Inbox",
                logical_locator="entry-broken",
                error_stage="read-recipient-table",
                error_type="RuntimeError",
                error_message="recipient table unavailable",
            ),
            good_message,
        ]
    )

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=tmp_path / "recipient-extraction",
    )

    connection = sqlite3.connect(database_path)
    try:
        stored_subjects = connection.execute("SELECT subject FROM messages").fetchall()
        failure = connection.execute(
            "SELECT error_stage, logical_locator FROM ingest_failures"
        ).fetchone()
    finally:
        connection.close()
    assert summary.imported_messages == 1
    assert summary.failure_count == 1
    assert stored_subjects == [("Still imported",)]
    assert failure == ("read-recipient-table", "entry-broken")


def test_import_pst_sources_backend_argument_selects_parser_without_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11.2: the `backend` argument alone must decide which parser class is
    constructed -- no `reader=` injection into either call. Deleting the
    dispatch collapses both calls onto the same parser class and this test
    fails.
    """
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    calls: list[str] = []

    class _StubReadpstParser:
        def __init__(self, **_kwargs) -> None:
            calls.append("readpst")

        def iter_extraction_results(self, _source):
            return iter(())

    class _StubOutlookComReader:
        def __init__(self, **_kwargs) -> None:
            calls.append("outlook-reader")

        def iter_messages(self, _source):
            return iter(())

    monkeypatch.setattr("mailattic.ingest.ReadpstParser", _StubReadpstParser)
    monkeypatch.setattr("mailattic.ingest.OutlookComReader", _StubOutlookComReader)

    import_pst_sources(
        database_path=database_path,
        sources=[source],
        backend="readpst",
        readpst_path=tmp_path / "stub-readpst.exe",
        work_root=tmp_path / "readpst-work",
    )
    import_pst_sources(
        database_path=database_path,
        sources=[source],
        backend="outlook",
        summary_root=tmp_path / "recipient-extraction",
    )

    assert calls == ["readpst", "outlook-reader"]


def test_import_records_ingest_failure_for_unstorable_attachment_binary(
    tmp_path: Path,
) -> None:
    """11.4 / criterion 3: an attachment binary that cannot be stored or
    decoded records an `ingest_failures` row under a named stage instead of
    silently vanishing from the persisted attachment row.
    """
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={
                    "subject": "Broken attachment",
                    "body_text": "Evidence",
                    "attachments": [
                        {
                            "attachment_name": "evidence.pdf",
                            "content_hash": None,
                            "size_bytes": None,
                            "mime_type": None,
                            "storage_path": None,
                            "_store_error": True,
                        }
                    ],
                    "attachment_names": ["evidence.pdf"],
                },
                folder_path="Inbox",
                logical_locator="entry-1",
                display_recipients={"to": "", "cc": "", "bcc": ""},
                recipients=(),
            )
        ]
    )

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=tmp_path / "recipient-extraction",
    )

    connection = sqlite3.connect(database_path)
    try:
        failure = connection.execute(
            "SELECT error_stage FROM ingest_failures"
        ).fetchone()
    finally:
        connection.close()
    assert summary.imported_messages == 1
    assert failure == ("attachment-store",)


def test_persisted_recipients_and_search_projections_share_normalized_source(
    tmp_path: Path,
) -> None:
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    raw_recipients = (
        RawOutlookRecipient("乙小姐", "yi@org-c.example", None, "SMTP", 1),
        RawOutlookRecipient("Copy", "copy@example.com", None, "SMTP", 2),
        RawOutlookRecipient("Exchange Person", None, "/O=ORG/CN=USER", "EX", 1),
    )
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={
                    "subject": "Projection agreement",
                    "body_text": "Evidence",
                    "message_id": "<projection@example.com>",
                },
                folder_path="Sent Items",
                logical_locator="entry-projection",
                display_recipients={"to": "乙小姐", "cc": "Copy", "bcc": ""},
                recipients=raw_recipients,
            )
        ]
    )

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=tmp_path / "recipient-extraction",
    )

    connection = sqlite3.connect(database_path)
    try:
        recipient_rows = connection.execute(
            "SELECT recipient_type, display_name, email "
            "FROM message_recipients ORDER BY id"
        ).fetchall()
        recipient_search_text = connection.execute(
            "SELECT recipient_search_text FROM messages"
        ).fetchone()[0]
        fts_recipients = connection.execute(
            "SELECT recipients FROM message_fts"
        ).fetchone()[0]
    finally:
        connection.close()
    normalization = normalize_outlook_recipients(raw_recipients)

    assert recipient_rows == [
        ("to", "乙小姐", "yi@org-c.example"),
        ("cc", "Copy", "copy@example.com"),
    ]
    assert recipient_search_text == "乙小姐 yi@org-c.example Copy copy@example.com"
    assert "yi@org-c.example" in fts_recipients
    assert "copy@example.com" in fts_recipients
    assert "Exchange Person" not in fts_recipients
    assert normalization.unresolved_by_reason["non_smtp_addrtype"] == 1
    assert summary.recipient_extraction_complete is True
    summary_payload = json.loads(
        Path(summary.recipient_extraction_summary_path or "").read_text(
            encoding="utf-8"
        )
    )
    assert (
        summary_payload["sources"][0]["unresolved_by_reason"]["non_smtp_addrtype"] == 1
    )
    assert all("/O=ORG" not in str(value) for row in recipient_rows for value in row)

    recipient_results = search_messages(
        database_path=database_path,
        filters=SearchFilters(recipient="Exchange Person"),
    )
    keyword_results = search_messages(
        database_path=database_path,
        filters=SearchFilters(keyword="Exchange Person"),
    )
    exact_contact = aggregate_contact_history(
        database_path=database_path,
        contact="Exchange Person",
    )
    domain = aggregate_contact_history(
        database_path=database_path,
        domain="person",
    )

    assert recipient_results == []
    assert keyword_results == []
    assert exact_contact["total_messages"] == 0
    assert domain["total_messages"] == 0


def test_blank_unresolved_display_name_adds_no_search_token(tmp_path: Path) -> None:
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={"subject": "Blank unresolved recipient"},
                folder_path="Sent Items",
                logical_locator="entry-blank-unresolved",
                display_recipients={"to": "", "cc": "", "bcc": ""},
                recipients=(RawOutlookRecipient("   ", None, None, "EX", 1),),
            )
        ]
    )

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=tmp_path / "recipient-extraction",
    )

    connection = sqlite3.connect(database_path)
    try:
        recipient_rows = connection.execute(
            "SELECT recipient_type, display_name, email FROM message_recipients"
        ).fetchall()
        search_projection = connection.execute(
            "SELECT m.recipient_search_text, f.recipients "
            "FROM messages AS m JOIN message_fts AS f ON f.rowid = m.id"
        ).fetchone()
    finally:
        connection.close()

    assert recipient_rows == []
    assert search_projection == ("", "")
    assert summary.recipient_extraction_complete is True
    summary_payload = json.loads(
        Path(summary.recipient_extraction_summary_path or "").read_text(
            encoding="utf-8"
        )
    )
    assert (
        summary_payload["sources"][0]["unresolved_by_reason"]["non_smtp_addrtype"] == 1
    )


def test_import_writes_single_source_summary_with_contracted_shape(
    tmp_path: Path,
) -> None:
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={"subject": "Summary", "body_text": "Evidence"},
                folder_path="Inbox",
                logical_locator="entry-summary",
                display_recipients={"to": "Client", "cc": "", "bcc": ""},
                recipients=(
                    RawOutlookRecipient(
                        "Client", "client@example.com", None, "SMTP", 1
                    ),
                ),
            )
        ]
    )
    summary_root = tmp_path / "runtime" / "recipient-extraction"

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=summary_root,
        run_id="run-1",
    )

    summary_path = Path(summary.recipient_extraction_summary_path or "")
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary_path == summary_root / "run-1" / "summary.json"
    assert summary.recipient_extraction_complete is True
    assert payload["schema_version"] == 1
    assert payload["run_id"] == "run-1"
    assert payload["sources"][0]["source_path"] == str(pst_path)
    assert payload["sources"][0]["message_reconciliation_difference"] == 0
    assert payload["sources"][0]["recipient_reconciliation_difference"] == 0
    assert payload["totals"]["messages_seen"] == 1
    assert payload["totals"]["recipients_emitted"] == 1


def test_import_summary_aggregates_two_exact_sources(tmp_path: Path) -> None:
    sources = [
        PstSource(tmp_path / "one.pst", "file", tmp_path / "one.pst"),
        PstSource(tmp_path / "two.pst", "file", tmp_path / "two.pst"),
    ]
    database_path = initialize_database(tmp_path / "evidence.db")
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={"subject": "Summary", "body_text": "Evidence"},
                folder_path="Inbox",
                logical_locator="entry-summary",
                display_recipients={"to": "", "cc": "", "bcc": ""},
                recipients=(),
            )
        ]
    )

    summary = import_pst_sources(
        database_path=database_path,
        sources=sources,
        reader=reader,
        summary_root=tmp_path / "runtime",
        run_id="run-2",
    )

    payload = json.loads(
        Path(summary.recipient_extraction_summary_path or "").read_text(
            encoding="utf-8"
        )
    )
    assert [entry["source_path"] for entry in payload["sources"]] == [
        str(source.path) for source in sources
    ]
    assert payload["totals"]["source_count"] == 2
    assert payload["totals"]["messages_seen"] == 2
    assert payload["totals"]["message_reconciliation_difference"] == 0
    assert payload["totals"]["recipient_reconciliation_difference"] == 0


def test_import_fails_when_summary_destination_cannot_be_created(
    tmp_path: Path,
) -> None:
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("blocked", encoding="utf-8")
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")

    with pytest.raises(OSError):
        import_pst_sources(
            database_path=database_path,
            sources=[source],
            reader=FakeOutlookReader([]),
            summary_root=blocking_file,
            run_id="run-3",
        )


def test_import_reconciles_and_cli_exits_zero_when_message_parse_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """A per-message parse failure alone does not make the account incomplete.

    Issue #151 D6 makes the account decide, not the incident count: this
    source's single failure is fully accounted for (messages_seen ==
    messages_emitted + parse_failures + skipped_non_mail_items), so
    reconciliation holds and both the summary and the CLI exit status must
    say so. A second, successfully imported message keeps this distinct from
    the retained "run imported nothing at all" rule, which is intentionally
    still non-zero on its own.
    """
    import mailattic.cli as cli

    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    reader = FakeOutlookReader(
        [
            RawOutlookFailure(
                folder_path="Inbox",
                logical_locator="entry-broken",
                error_stage="read-recipient-table",
                error_type="RuntimeError",
                error_message="recipient table unavailable",
            ),
            RawOutlookMessage(
                raw_message={"subject": "Still imported", "body_text": "Evidence"},
                folder_path="Inbox",
                logical_locator="entry-good",
                display_recipients={"to": "", "cc": "", "bcc": ""},
                recipients=(),
            ),
        ]
    )

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=tmp_path / "runtime",
        run_id="partial",
    )

    payload = json.loads(
        Path(summary.recipient_extraction_summary_path or "").read_text(
            encoding="utf-8"
        )
    )
    assert summary.recipient_extraction_complete is True
    assert payload["complete"] is True
    assert payload["sources"][0]["parse_failures"] == 1
    assert payload["sources"][0]["message_reconciliation_difference"] == 0

    monkeypatch.setattr(cli, "import_pst_sources", lambda **kwargs: summary)
    exit_code = main(
        ["import", "--database", str(database_path), "--file", str(pst_path)]
    )
    assert exit_code == 0


def test_import_cli_exit_status_follows_reconciliation_not_failure_count(
    tmp_path: Path, monkeypatch
) -> None:
    """Both directions of the exit-status contract in one place (issue #151 D6).

    `cli._handle_import` must decide from `recipient_extraction_complete`,
    not from `failure_count`: zero when the account reconciles despite a
    recorded parse failure, non-zero when a reconciliation difference left
    the account incomplete.
    """
    import mailattic.cli as cli

    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    database_path = tmp_path / "evidence.db"

    balanced_with_parse_failure = ImportSummary(
        discovered_psts=1,
        imported_messages=9,
        duplicate_messages=0,
        failure_count=1,
        recipient_extraction_summary_path=str(
            tmp_path / "runtime" / "balanced" / "summary.json"
        ),
        recipient_extraction_complete=True,
    )
    monkeypatch.setattr(
        cli, "import_pst_sources", lambda **kwargs: balanced_with_parse_failure
    )
    exit_code_balanced = main(
        ["import", "--database", str(database_path), "--file", str(pst_path)]
    )

    unbalanced = ImportSummary(
        discovered_psts=1,
        imported_messages=9,
        duplicate_messages=0,
        failure_count=0,
        recipient_extraction_summary_path=str(
            tmp_path / "runtime" / "unbalanced" / "summary.json"
        ),
        recipient_extraction_complete=False,
    )
    monkeypatch.setattr(cli, "import_pst_sources", lambda **kwargs: unbalanced)
    exit_code_unbalanced = main(
        ["import", "--database", str(database_path), "--file", str(pst_path)]
    )

    assert exit_code_balanced == 0
    assert exit_code_unbalanced != 0


def test_import_skips_non_mail_item_and_cli_exits_zero(
    tmp_path: Path, monkeypatch
) -> None:
    """A non-mail item is skipped, not a parse failure, and does not fail the run.

    Real PST stores carry calendar, contact, and NDR items. `_read_item`
    still classifies these the same way it classifies a genuine failure, but
    the parser must account for them separately so a whole-store import does
    not fail reconciliation on every non-mail item it encounters.
    """
    import mailattic.cli as cli

    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={"subject": "Mail item", "body_text": "Evidence"},
                folder_path="Inbox",
                logical_locator="entry-mail",
                display_recipients={"to": "", "cc": "", "bcc": ""},
                recipients=(),
            ),
            RawOutlookFailure(
                folder_path="Inbox",
                logical_locator="entry-meeting",
                error_stage="unsupported-item-class",
                error_type="UnsupportedOutlookItemClass",
                error_message="Unsupported Outlook item class: 26",
            ),
        ]
    )

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=tmp_path / "runtime",
        run_id="skip-non-mail",
    )

    payload = json.loads(
        Path(summary.recipient_extraction_summary_path or "").read_text(
            encoding="utf-8"
        )
    )
    entry = payload["sources"][0]
    assert entry["skipped_non_mail_items"] == 1
    assert entry["parse_failures"] == 0
    assert entry["message_reconciliation_difference"] == 0
    assert summary.recipient_extraction_complete is True
    assert summary.failure_count == 0

    monkeypatch.setattr(cli, "import_pst_sources", lambda **kwargs: summary)
    exit_code = main(
        ["import", "--database", str(database_path), "--file", str(pst_path)]
    )
    assert exit_code == 0


def test_write_pst_extraction_summary_names_the_failing_equation(
    tmp_path: Path,
) -> None:
    """A failed reconciliation names which equation broke, not just `complete: false`.

    `write_pst_extraction_summary` used to discard `account.validate()`'s
    RuntimeError message, so the summary recorded an unqualified incomplete
    state with no way to tell which side of the ledger was off.
    """
    account = PstExtractionAccount(
        source_path="archive.pst",
        messages_seen=2,
        messages_emitted=1,
        parse_failures=0,
        completed=True,
    )

    summary_path, complete = write_pst_extraction_summary(
        [account],
        database_path=tmp_path / "evidence.db",
        summary_root=tmp_path / "runtime",
        run_id="broken-equation",
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    entry = payload["sources"][0]
    assert complete is False
    assert payload["complete"] is False
    assert "message reconciliation difference: 1" in entry["reconciliation_error"]


def test_import_surfaces_unexpected_reader_defect_instead_of_swallowing_it(
    tmp_path: Path,
) -> None:
    """design.md > "Surface extraction failures instead of absorbing them":
    an untyped reader defect (not OSError/RuntimeError/ValueError -- the set
    a legitimate extraction failure raises) must propagate out of
    `import_pst_sources`, not become a silent `ingest_failures` row. This
    replaces a prior test that asserted the opposite: a bug in the COM
    adapter used to be swallowed into a routine "incomplete" summary with no
    signal that it was a code defect rather than a data problem.
    """

    class BuggyReader:
        def iter_messages(self, source: PstSource):
            del source
            raise LookupError("COM folder enumeration failed")
            yield

    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")

    with pytest.raises(LookupError, match="COM folder enumeration failed"):
        import_pst_sources(
            database_path=database_path,
            sources=[source],
            reader=BuggyReader(),
            summary_root=tmp_path / "runtime",
            run_id="com-defect",
        )


def test_import_records_typed_reader_failure_and_still_writes_summary(
    tmp_path: Path,
) -> None:
    """A typed, legitimate extraction failure (RuntimeError, matching
    `OutlookComReader.resolve_store`'s own failure type) is still caught and
    recorded -- unlike an untyped adapter defect, this is expected to
    degrade to an incomplete summary rather than propagate.
    """

    class UnmountedReader:
        def iter_messages(self, source: PstSource):
            del source
            raise RuntimeError("PST source must be already mounted")
            yield

    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=UnmountedReader(),
        summary_root=tmp_path / "runtime",
        run_id="unmounted-source",
    )

    payload = json.loads(
        Path(summary.recipient_extraction_summary_path or "").read_text(
            encoding="utf-8"
        )
    )
    assert summary.failure_count == 1
    assert summary.recipient_extraction_complete is False
    assert payload["complete"] is False
    assert payload["sources"][0]["completed"] is False


def test_import_records_outlook_guard_failure_to_stderr_and_ingest_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When a source-level guard (e.g. Outlook not running) blocks extraction,
    the failure is printed to stderr for operator visibility and recorded in
    the ingest_failures table (issue #186).
    """
    guard_message = (
        "Outlook is not running. Start Outlook and mount the PST first; "
        "this reader never launches Outlook itself."
    )

    class GuardedReader:
        def iter_messages(self, source: PstSource):
            del source
            raise RuntimeError(guard_message)
            yield

    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=GuardedReader(),
        summary_root=tmp_path / "runtime",
        run_id="guard-failure",
    )

    captured = capsys.readouterr()
    assert guard_message in captured.err
    assert source.source_path in captured.err
    assert captured.err.splitlines() == [f"{source.source_path}: {guard_message}"]
    assert "Traceback" not in captured.err

    connection = sqlite3.connect(database_path)
    try:
        failure_rows = connection.execute(
            "SELECT pst_source, error_stage, error_type, error_message "
            "FROM ingest_failures"
        ).fetchall()
    finally:
        connection.close()

    assert summary.failure_count == 1
    assert failure_rows == [
        (
            source.source_path,
            "extract-pst",
            "RuntimeError",
            guard_message,
        )
    ]


def test_import_reconciles_when_recipient_has_unknown_role(tmp_path: Path) -> None:
    """A named unresolved reason is a normal, reconciled outcome (D6).

    `unknown_recipient_type` is one of the four allowed keys in
    `unresolved_by_reason`, counted on both sides of the recipient
    reconciliation equation exactly like `missing_address` or
    `non_smtp_addrtype`. It must not, by itself, mark the account
    incomplete — only an unbalanced equation or a key outside the allowed
    set does that.
    """
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={"subject": "Unknown role"},
                folder_path="Inbox",
                logical_locator="entry-unknown-role",
                display_recipients={"to": "", "cc": "", "bcc": ""},
                recipients=(
                    RawOutlookRecipient(
                        "Unknown", "unknown@example.com", None, "SMTP", 99
                    ),
                ),
            )
        ]
    )

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=tmp_path / "runtime",
        run_id="unknown-role",
    )

    payload = json.loads(
        Path(summary.recipient_extraction_summary_path or "").read_text(
            encoding="utf-8"
        )
    )
    assert summary.recipient_extraction_complete is True
    assert payload["complete"] is True
    assert payload["sources"][0]["unresolved_by_reason"]["unknown_recipient_type"] == 1
    assert payload["sources"][0]["recipient_reconciliation_difference"] == 0


def test_packed_address_failure_is_named_and_next_message_is_imported(
    tmp_path: Path,
) -> None:
    pst_path = tmp_path / "archive.pst"
    source = PstSource(pst_path, "file", pst_path)
    database_path = initialize_database(tmp_path / "evidence.db")
    reader = FakeOutlookReader(
        [
            RawOutlookMessage(
                raw_message={"subject": "Packed"},
                folder_path="Sent Items",
                logical_locator="entry-packed",
                display_recipients={"to": "A; B", "cc": "", "bcc": ""},
                recipients=(
                    RawOutlookRecipient(
                        None,
                        "a@example.com; b@example.com",
                        None,
                        "SMTP",
                        1,
                    ),
                ),
            ),
            RawOutlookMessage(
                raw_message={"subject": "Good"},
                folder_path="Sent Items",
                logical_locator="entry-good",
                display_recipients={"to": "Good", "cc": "", "bcc": ""},
                recipients=(
                    RawOutlookRecipient("Good", "good@example.com", None, "SMTP", 1),
                ),
            ),
        ]
    )

    summary = import_pst_sources(
        database_path=database_path,
        sources=[source],
        reader=reader,
        summary_root=tmp_path / "runtime",
        run_id="packed",
    )

    connection = sqlite3.connect(database_path)
    try:
        subjects = connection.execute("SELECT subject FROM messages").fetchall()
        failure = connection.execute(
            "SELECT error_stage, logical_locator FROM ingest_failures"
        ).fetchone()
    finally:
        connection.close()
    assert subjects == [("Good",)]
    assert failure == ("normalize-recipients", "entry-packed")
    assert summary.imported_messages == 1
    assert summary.failure_count == 1
    # The failed message is fully accounted for in parse_failures, so the
    # account still reconciles (issue #151 D6: a per-message parse failure
    # alone does not make the account incomplete).
    assert summary.recipient_extraction_complete is True


def test_import_cli_returns_nonzero_and_prints_incomplete_summary_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import mailattic.cli as cli

    pst_path = tmp_path / "archive.pst"
    pst_path.write_bytes(b"pst")
    summary_path = tmp_path / "runtime" / "run-4" / "summary.json"
    fake_summary = ImportSummary(
        discovered_psts=1,
        imported_messages=1,
        recipient_extraction_summary_path=str(summary_path),
        recipient_extraction_complete=False,
    )
    monkeypatch.setattr(cli, "import_pst_sources", lambda **kwargs: fake_summary)

    exit_code = main(
        [
            "import",
            "--database",
            str(tmp_path / "evidence.db"),
            "--file",
            str(pst_path),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code != 0
    assert str(summary_path) in output


def test_outlook_platform_dependency_and_operator_contract(capsys) -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert any(
        dependency.startswith("pywin32>=312")
        and "sys_platform == 'win32'" in dependency
        for dependency in project["project"]["dependencies"]
    )

    # The operator contract lives in the CLI reference; README is the public
    # front page (English) and must keep linking to it, or the contract below
    # would hold in a file no reader reaches from the entry point (#261).
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/cli-reference.md" in readme
    operator_docs = readme + (REPO_ROOT / "docs" / "cli-reference.md").read_text(
        encoding="utf-8"
    )
    for expected in (
        "Windows",
        "active profile",
        "已由操作者掛載",
        "最小",
        ".project-runtime/recipient-extraction/<run-id>/summary.json",
        "8 小時",
        "5%",
    ):
        assert expected in operator_docs

    with pytest.raises(SystemExit) as help_exit:
        main(["import", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "already-running Windows" in help_text
    assert "Outlook profile" in help_text
    assert "already be mounted" in help_text
    assert "--readpst-path" not in help_text

"""Tests for `bes import --maildir` (issue #154).

The maildir ingestion path already exists and runs daily under `sync-imap`;
this ticket only adds the CLI entry point. So these tests assert two things:
the entry reaches the *same* `resolve_maildir_sources` / `import_maildir_sources`
pair the IMAP path uses (dedup gate, attachment CAS and the discovery audit
columns therefore come along for free), and that the ways an operator can hold
unsupported or malformed inputs fail loudly rather than importing part of
the archive and printing success.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mailattic.attachment_store import read_attachment, resolve_store_root
from mailattic.cli import main
from mailattic.models import ImportSummary

_ACCOUNT_EMAIL = "operator@example.com"

_PLAIN_EML = """\
Message-ID: <plain-001@example.com>
Date: Mon, 3 Mar 2025 09:15:00 +0800
From: Sender One <sender@example.com>
To: First Recipient <first@example.com>, second@example.com
Cc: Third <third@example.com>
Subject: Quarterly pricing

Body of the plain message.
"""

_CJK_EML = """\
Message-ID: <cjk-002@example.com>
Date: Tue, 4 Mar 2025 10:20:00 +0800
From: =?UTF-8?B?6Zmz5aSn5paH?= <cjk@example.com>
To: buyer@example.com
Subject: =?UTF-8?B?5aCx5YO55Zau6Kqq5piO?=
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 8bit

商品報價的中文內文。
"""

_NO_MESSAGE_ID_EML = """\
Date: Wed, 5 Mar 2025 11:25:00 +0800
From: anonymous@example.com
To: buyer@example.com
Subject: No message id header

This message carries no Message-ID header.
"""

_ATTACHMENT_EML = """\
Message-ID: <attach-003@example.com>
Date: Thu, 6 Mar 2025 12:30:00 +0800
From: sender@example.com
To: buyer@example.com
Subject: With attachment
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset="us-ascii"

See attached quote.

--BOUND
Content-Type: text/plain; charset="us-ascii"; name="quote.txt"
Content-Disposition: attachment; filename="quote.txt"
Content-Transfer-Encoding: base64

cXVvdGUgYm9keQ==

--BOUND--
"""


def _build_maildir(root: Path, messages: dict[str, str]) -> Path:
    """Create an account root holding one INBOX folder with `cur` messages."""
    account_root = root / "local-archive"
    cur = account_root / "INBOX" / "cur"
    cur.mkdir(parents=True)
    for name, content in messages.items():
        (cur / name).write_text(content, encoding="utf-8")
    return account_root


def _all_messages(database: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute("SELECT * FROM messages ORDER BY id"))
    finally:
        connection.close()


def _run_import(account_root: Path, database: Path, *extra: str) -> int:
    return main(
        [
            "import",
            "--database",
            str(database),
            "--maildir",
            str(account_root),
            "--account-email",
            _ACCOUNT_EMAIL,
            *extra,
        ]
    )


def test_maildir_import_lands_messages_recipients_and_attachments(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = _build_maildir(
        tmp_path,
        {
            "plain.eml": _PLAIN_EML,
            "cjk.eml": _CJK_EML,
            "no-id.eml": _NO_MESSAGE_ID_EML,
            "attach.eml": _ATTACHMENT_EML,
        },
    )
    database = tmp_path / "import.db"

    assert _run_import(account_root, database) == 0

    rows = _all_messages(database)
    assert len(rows) == 4

    subjects = {row["subject"] for row in rows}
    assert "報價單說明" in subjects, "CJK subject must survive the entry point"
    assert "No message id header" in subjects

    # The discovery audit columns are what tell a later reader where a message
    # came in through -- an empty value here is indistinguishable from an old
    # row written before the column existed.
    assert {row["source_input_mode"] for row in rows} == {"maildir"}
    assert all(row["source_requested_from"] for row in rows)

    connection = sqlite3.connect(database)
    try:
        recipients = connection.execute(
            "SELECT recipient_type, email FROM message_recipients "
            "JOIN messages ON messages.id = message_recipients.message_id "
            "WHERE messages.message_id = ?",
            ("<plain-001@example.com>",),
        ).fetchall()
        attachments = connection.execute(
            "SELECT attachment_name, storage_path FROM message_attachments"
        ).fetchall()
    finally:
        connection.close()

    # Two To: plus one Cc: -- the packed To: header must be expanded per address.
    assert sorted(recipients) == sorted(
        [
            ("to", "first@example.com"),
            ("to", "second@example.com"),
            ("cc", "third@example.com"),
        ]
    )

    assert len(attachments) == 1
    attachment_name, storage_path = attachments[0]
    assert attachment_name == "quote.txt"
    store_root = resolve_store_root(
        database.resolve(), cli_override=None, env_override=None
    )
    assert read_attachment(str(storage_path), store_root) == b"quote body"


def test_reimporting_the_same_maildir_is_deduplicated(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    database = tmp_path / "import.db"

    assert _run_import(account_root, database) == 0
    assert _run_import(account_root, database) == 0

    assert len(_all_messages(database)) == 1


def test_json_summary_reports_maildir_counts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    database = tmp_path / "import.db"

    assert _run_import(account_root, database, "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["imported_messages"] == 1
    assert payload["duplicate_messages"] == 0
    assert payload["failure_count"] == 0
    # `discovered_psts` is a PST-path word; a maildir run reports folders.
    assert payload["discovered_maildir_folders"] == 1


def test_maildir_and_pst_directory_together_are_rejected(
    tmp_path: Path, capsys
) -> None:
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    database = tmp_path / "import.db"

    exit_code = main(
        [
            "import",
            "--database",
            str(database),
            "--maildir",
            str(account_root),
            "--account-email",
            _ACCOUNT_EMAIL,
            "--directory",
            str(tmp_path / "psts"),
        ]
    )

    assert exit_code == 2
    assert "cannot be combined" in capsys.readouterr().err
    assert not database.exists(), "a rejected invocation must not create a database"


def test_maildir_without_account_email_is_rejected(tmp_path: Path, capsys) -> None:
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    database = tmp_path / "import.db"

    exit_code = main(
        ["import", "--database", str(database), "--maildir", str(account_root)]
    )

    assert exit_code == 2
    assert "requires --account-email" in capsys.readouterr().err


def test_directory_without_maildir_layout_is_rejected_not_silently_empty(
    tmp_path: Path, capsys
) -> None:
    """A flat directory of .eml files has no cur/new/tmp, so the parser would
    read nothing. Reporting "imported 0 messages" and exit 0 there is the one
    outcome an operator cannot distinguish from an empty archive."""
    flat = tmp_path / "flat-eml"
    flat.mkdir()
    (flat / "plain.eml").write_text(_PLAIN_EML, encoding="utf-8")
    database = tmp_path / "import.db"

    exit_code = _run_import(flat, database)

    assert exit_code == 2
    error = capsys.readouterr().err
    assert "would be imported as nothing" in error, (
        "the reconcile check must own this message"
    )
    assert "cur" in error, "the error must name the layout it expected"


def test_missing_maildir_path_is_rejected(tmp_path: Path, capsys) -> None:
    database = tmp_path / "import.db"

    exit_code = _run_import(tmp_path / "does-not-exist", database)

    assert exit_code == 2
    # Not just the path: the layout guard also interpolates it, so asserting
    # the path alone stays green when this guard is deleted.
    assert "account root not found" in capsys.readouterr().err


def _stub_summary(monkeypatch, summary: ImportSummary) -> None:
    monkeypatch.setattr(
        "mailattic.cli.import_maildir_sources",
        lambda **_kwargs: summary,
    )


def test_total_failure_exits_non_zero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    _stub_summary(monkeypatch, ImportSummary(failure_count=3))

    assert _run_import(account_root, tmp_path / "import.db") == 2


def test_partial_failure_exits_zero_matching_the_pst_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Pins the deliberately asymmetric rule inherited from the PST handler:
    once anything landed, per-message failures are accounted in
    `ingest_failures` and reported in the summary rather than failing the run.
    Changing it is a decision about both entry points, not just this one."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    _stub_summary(monkeypatch, ImportSummary(imported_messages=1, failure_count=500))

    assert _run_import(account_root, tmp_path / "import.db") == 0


def test_unreadable_account_root_exits_two_instead_of_raising(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`main()` carries no top-level handler, so anything escaping the handler
    reaches the operator as a traceback. Folder listing stats a path the
    operator named, so it has to sit inside the exception boundary."""
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})

    def raise_permission_error(**_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("mailattic.cli.resolve_maildir_sources", raise_permission_error)

    assert _run_import(account_root, tmp_path / "import.db") == 2
    assert "Permission denied" in capsys.readouterr().err


def test_one_malformed_folder_rejects_the_whole_run(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A well-formed INBOX next to a flat Archive/ used to import INBOX and
    exit 0, leaving Archive's messages with no row, no `ingest_failures` entry
    and no failure count -- indistinguishable from an archive that never held
    them. The guard is per folder for exactly that reason."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    flat_folder = account_root / "Archive"
    flat_folder.mkdir()
    (flat_folder / "loose.eml").write_text(_NO_MESSAGE_ID_EML, encoding="utf-8")
    database = tmp_path / "import.db"

    assert _run_import(account_root, database) == 2
    assert "loose.eml" in capsys.readouterr().err
    assert not database.exists(), "nothing may land when part of the tree is unreadable"


def test_database_path_pointing_at_a_directory_exits_two(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`sqlite3.Error` is not an `OSError` subclass, so it escapes an
    OSError-only except tuple. Every other database-touching handler in cli.py
    lists it; `bes search` on the same path exits 2 with a clean message."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    not_a_database = tmp_path / "a-directory"
    not_a_database.mkdir()

    assert _run_import(account_root, not_a_database) == 2
    assert "unable to open database file" in capsys.readouterr().err


def test_loose_file_beside_a_valid_bucket_is_reported(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A folder can be well formed and still hide messages: `Archive/cur/ok.eml`
    next to `Archive/loose.eml` passed every earlier shape of this check, and
    the loose one imported as nothing at exit 0."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    (account_root / "INBOX" / "loose.eml").write_text(_CJK_EML, encoding="utf-8")
    database = tmp_path / "import.db"

    assert _run_import(account_root, database) == 2
    assert "loose.eml" in capsys.readouterr().err
    assert not database.exists()


def test_maildir_bookkeeping_dotfiles_are_not_counted_as_losses(
    tmp_path: Path, monkeypatch
) -> None:
    """Real mbsync trees carry `.uidvalidity` / `.mbsyncstate` per folder and
    this project's `.imap_watermarks.json` at the account root. Counting those
    as skipped messages would reject every genuine maildir."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    (account_root / ".imap_watermarks.json").write_text("{}", encoding="utf-8")
    (account_root / "INBOX" / ".uidvalidity").write_text("1", encoding="utf-8")
    database = tmp_path / "import.db"

    assert _run_import(account_root, database) == 0
    assert len(_all_messages(database)) == 1


def test_message_inside_a_dot_prefixed_folder_is_reported(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`resolve_maildir_sources` never returns a dot-prefixed directory as a
    folder, so nothing under `.Archive/` is ever read. Scanning only the
    resolved folders made that invisible; scanning the whole tree and
    subtracting what the reader sees makes it a reported loss."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = _build_maildir(tmp_path, {"plain.eml": _PLAIN_EML})
    hidden = account_root / ".Archive"
    hidden.mkdir()
    (hidden / "loose.eml").write_text(_CJK_EML, encoding="utf-8")
    database = tmp_path / "import.db"

    assert _run_import(account_root, database) == 2
    assert "loose.eml" in capsys.readouterr().err
    assert not database.exists()


def test_bucket_that_is_a_plain_file_is_reported(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A plain file named `cur` satisfies `exists()` and then yields nothing,
    so the folder read as empty and the run exited 0 having imported nothing."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = tmp_path / "local-archive"
    (account_root / "INBOX").mkdir(parents=True)
    (account_root / "INBOX" / "cur").write_text("not a directory", encoding="utf-8")
    database = tmp_path / "import.db"

    assert _run_import(account_root, database) == 2
    assert "cur" in capsys.readouterr().err
    assert not database.exists()


def test_folders_without_any_bucket_are_rejected_not_reported_as_empty(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """No message is lost here -- there are no files. What the check buys is
    that "you pointed at the wrong directory" stops looking identical to
    "this archive is empty", which is what `imported 0 messages` at exit 0
    said for both."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = tmp_path / "local-archive"
    (account_root / "INBOX").mkdir(parents=True)
    database = tmp_path / "import.db"

    assert _run_import(account_root, database) == 2
    assert "does not look like a maildir" in capsys.readouterr().err
    assert not database.exists()


def test_a_genuinely_empty_but_well_formed_maildir_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    """The other side of the same check: an archive with real buckets and no
    messages is empty, not malformed, and must still import cleanly."""
    monkeypatch.delenv("MAILATTIC_ATTACHMENT_ROOT", raising=False)
    account_root = tmp_path / "local-archive"
    for bucket in ("cur", "new", "tmp"):
        (account_root / "INBOX" / bucket).mkdir(parents=True)
    database = tmp_path / "import.db"

    assert _run_import(account_root, database) == 0
    assert _all_messages(database) == []

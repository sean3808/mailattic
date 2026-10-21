from __future__ import annotations

import sqlite3
from email.message import EmailMessage
from pathlib import Path

from mailattic.ingest import import_maildir_sources
from mailattic.models import (
    MaildirSource,
    PstSource,
    build_normalized_message,
)
from mailattic.parser import (
    MaildirParser,
    iter_maildir_message_paths,
)
from mailattic.storage import initialize_database


def test_minimum_fields(tmp_path: Path) -> None:
    folder_root = tmp_path / "maildir" / "sean3808" / "INBOX"
    for bucket_name in ("cur", "new", "tmp"):
        (folder_root / bucket_name).mkdir(parents=True, exist_ok=True)

    for index in range(10):
        bucket_name = ("cur", "new", "tmp")[index % 3]
        _write_message(
            folder_root / bucket_name / f"message-{index}.eml",
            subject=f"Subject {index}",
            sender="alice@example.com",
            recipient="bob@example.com",
            message_id=f"<msg-{index}@example.com>",
        )

    parser = MaildirParser()
    source = MaildirSource(
        path=folder_root,
        account_email="sean3808@mailattic.test",
        account_name="sean3808",
        folder_name="INBOX",
        requested_from=folder_root.parent,
    )

    results = list(parser.iter_extraction_results(source))

    assert len(results) == 10
    assert all(result.failure is None for result in results)
    sample = results[0].message
    assert sample is not None
    assert sample.folder_path == "INBOX"
    assert sample.logical_locator.startswith("INBOX/")
    assert sample.raw_message["subject"].startswith("Subject")
    assert sample.raw_message["sender"]["email"] == "alice@example.com"
    assert sample.raw_message["to"][0]["email"] == "bob@example.com"
    assert sample.raw_message["sent_at"].isoformat() == "2024-01-02T03:04:05+00:00"
    assert sample.raw_message["received_at"].isoformat() == "2024-01-02T03:04:10+00:00"
    assert sample.raw_message["body_text"] == "Body text"
    assert sample.raw_message["attachment_names"] == ["evidence.txt"]
    assert sample.raw_message["message_id"].startswith("<msg-")
    assert sample.raw_message["in_reply_to"] == "<thread@example.com>"
    assert sample.raw_message["references"] == "<root@example.com> <thread@example.com>"


def test_source_identifier_format() -> None:
    inbox_source = MaildirSource(
        path=Path("C:/maildir/sean3808/INBOX"),
        account_email="sean3808@mailattic.test",
        account_name="sean3808",
        folder_name="INBOX",
        requested_from=Path("C:/maildir/sean3808"),
    )
    sent_source = MaildirSource(
        path=Path("C:/maildir/sean3808/Sent Items"),
        account_email="sean3808@mailattic.test",
        account_name="sean3808",
        folder_name="Sent Items",
        requested_from=Path("C:/maildir/sean3808"),
    )
    pst_source = PstSource(
        path=Path("C:/archive/archive_2024-2025.pst"),
        input_mode="file",
        requested_from=Path("C:/archive/archive_2024-2025.pst"),
    )

    assert inbox_source.source_path == "imap:sean3808@mailattic.test:INBOX"
    assert sent_source.source_path == "imap:sean3808@mailattic.test:Sent Items"
    normalized_message = build_normalized_message(
        {"subject": "Preserved", "body_text": "Body"},
        source=pst_source,
        folder_path="Inbox",
        logical_locator="Inbox/1",
    )
    assert normalized_message.pst_source == str(
        Path("C:/archive/archive_2024-2025.pst")
    )


def test_partial_failure(tmp_path: Path, monkeypatch) -> None:
    folder_root = tmp_path / "maildir" / "sean3808" / "INBOX"
    (folder_root / "cur").mkdir(parents=True, exist_ok=True)

    for index in range(9):
        _write_message(
            folder_root / "cur" / f"good-{index}.eml",
            subject=f"Good {index}",
            sender="alice@example.com",
            recipient="bob@example.com",
            message_id=f"<good-{index}@example.com>",
        )
    broken_path = folder_root / "cur" / "broken.eml"
    broken_path.write_text("broken", encoding="utf-8")

    import mailattic.parser as parser_module

    original_parse = parser_module._parse_eml_file

    def failing_parse(path: Path):
        if path.name == "broken.eml":
            raise ValueError("corrupt maildir message")
        return original_parse(path)

    monkeypatch.setattr(parser_module, "_parse_eml_file", failing_parse)
    database_path = initialize_database(tmp_path / "evidence.db")
    summary = import_maildir_sources(
        database_path=database_path,
        sources=[
            MaildirSource(
                path=folder_root,
                account_email="sean3808@mailattic.test",
                account_name="sean3808",
                folder_name="INBOX",
                requested_from=folder_root.parent,
            )
        ],
    )

    assert summary.imported_messages == 9
    assert summary.failure_count == 1
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 9
        failure_row = connection.execute(
            "SELECT logical_locator FROM ingest_failures"
        ).fetchone()
    finally:
        connection.close()
    assert failure_row is not None
    assert "broken.eml" in failure_row[0]


def _write_message(
    path: Path,
    *,
    subject: str,
    sender: str,
    recipient: str,
    message_id: str | None,
) -> None:
    message = EmailMessage()
    message["From"] = f"Alice <{sender}>"
    message["To"] = f"Bob <{recipient}>"
    message["Subject"] = subject
    message["Date"] = "Tue, 02 Jan 2024 03:04:05 +0000"
    message["Received"] = (
        "from mx.example.com by mail.example.com with ESMTP id 12345; "
        "Tue, 02 Jan 2024 03:04:10 +0000"
    )
    if message_id is not None:
        message["Message-ID"] = message_id
    message["In-Reply-To"] = "<thread@example.com>"
    message["References"] = "<root@example.com> <thread@example.com>"
    message.set_content("Body text")
    message.add_attachment(
        b"attachment",
        maintype="text",
        subtype="plain",
        filename="evidence.txt",
    )
    path.write_bytes(message.as_bytes())


def test_iter_maildir_message_paths_yields_every_bucket(tmp_path: Path) -> None:
    """This function is the single answer to "which files does the reader
    see?" -- the CLI reconciles an operator's directory against it, so its
    contract is pinned here rather than only through a caller."""
    folder = tmp_path / "INBOX"
    for bucket in ("cur", "new", "tmp"):
        (folder / bucket).mkdir(parents=True)
        (folder / bucket / f"{bucket}.eml").write_text("x", encoding="utf-8")
    (folder / "nested").mkdir()
    (folder / "nested" / "deep.eml").write_text("x", encoding="utf-8")
    (folder / "cur" / "sub").mkdir()
    (folder / "cur" / "sub" / "nested.eml").write_text("x", encoding="utf-8")

    found = {path.name for path in iter_maildir_message_paths(folder)}

    assert found == {"cur.eml", "new.eml", "tmp.eml", "nested.eml"}
    assert "deep.eml" not in found, "only bucket subtrees are read"


def test_batch_walk_keeps_walked_locator_through_a_junction(tmp_path: Path) -> None:
    """A link inside the walked tree that points outside it keeps the locator
    of the path that was walked, and does not abort the batch (#7 review)."""
    import subprocess

    folder = tmp_path / "INBOX"
    (folder / "cur").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_message(
        outside / "linked.eml",
        subject="Linked",
        sender="alice@example.com",
        recipient="bob@example.com",
        message_id="<linked@example.com>",
    )
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(folder / "cur" / "link"), str(outside)],
        check=True,
        capture_output=True,
    )
    source = MaildirSource(
        path=folder,
        account_email="sean3808@mailattic.test",
        account_name="sean3808",
        folder_name="INBOX",
        requested_from=folder.parent,
    )

    results = list(MaildirParser().iter_extraction_results(source))

    assert [r.message.logical_locator for r in results if r.message] == [
        "INBOX/cur/link/linked.eml"
    ]


def test_iter_maildir_message_paths_yields_nothing_for_a_file_named_cur(
    tmp_path: Path,
) -> None:
    """Characterisation, not a guard: a plain file named `cur` yields no
    messages. The CLI reconcile is what turns that into a visible error --
    this only records that the reader reports nothing rather than raising."""
    folder = tmp_path / "INBOX"
    folder.mkdir(parents=True)
    (folder / "cur").write_text("not a directory", encoding="utf-8")

    assert list(iter_maildir_message_paths(folder)) == []

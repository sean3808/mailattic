"""#243: message/rfc822 without Content-Disposition routed by outer type.

- multipart/report: inner mail body is kept in the outer body / FTS index,
  and no attachment record is created.
- Other containers (e.g. multipart/mixed): inner mail is stored as a
  message/rfc822 attachment; its body text is kept out of the outer body
  and search index. When no filename is present, it is named after the
  attached message's subject + '.eml'.
"""

from __future__ import annotations

import sqlite3
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

from mailattic import attachment_store
from mailattic.ingest import import_maildir_sources
from mailattic.models import MaildirSource
from mailattic.query import SearchFilters, search_messages
from mailattic.storage import initialize_database

OUTER = "OUTERBOUNCETOKEN"
INNER = "INNERORIGINALTOKEN"


def _child(*, subject: str = "Original Mail Subject") -> EmailMessage:
    child = EmailMessage()
    child["From"] = "original-sender@example.com"
    child["To"] = "recipient@example.com"
    child["Subject"] = subject
    child["Message-ID"] = "<original-evidence@example.com>"
    child.set_content(INNER)
    return child


def _import(tmp_path: Path, parent: EmailMessage) -> tuple[Path, Path]:
    folder = tmp_path / "maildir" / "INBOX"
    (folder / "new").mkdir(parents=True, exist_ok=True)
    if "From" not in parent:
        parent["From"] = "parent@example.com"
    if "To" not in parent:
        parent["To"] = "parent-rcpt@example.com"
    if "Subject" not in parent:
        parent["Subject"] = "Parent notification"
    if "Message-ID" not in parent:
        parent["Message-ID"] = "<parent-evidence@example.com>"
    (folder / "new" / "parent.eml").write_bytes(parent.as_bytes())
    db = initialize_database(tmp_path / "data.db")
    root = tmp_path / "attachments"
    summary = import_maildir_sources(
        database_path=db,
        sources=[
            MaildirSource(
                path=folder,
                account_name="audit",
                account_email="parent-rcpt@example.com",
                folder_name="INBOX",
                requested_from=folder.parent,
            )
        ],
        attachment_root=root,
    )
    assert summary.imported_messages == 1
    return db, root


def _hits(db: Path, keyword: str) -> list[dict[str, object]]:
    return search_messages(database_path=db, filters=SearchFilters(keyword=keyword))


def test_multipart_report_keeps_original_mail_in_outer_body_and_fts(
    tmp_path: Path,
) -> None:
    """multipart/report containing message/rfc822 without disposition:

    Inner body is merged into outer body and searchable; no attachment row is created.
    """
    parent = EmailMessage()
    parent.set_type("multipart/report")
    parent.set_param("report-type", "delivery-status")
    parent["Subject"] = "Delivery Status Notification (Failure)"

    human = EmailMessage()
    human.set_content(f"Delivery failed notice: {OUTER}")
    parent.attach(human)

    dsn_part = EmailMessage()
    dsn_part.set_type("message/delivery-status")
    dsn_part.set_content("Action: failed\nStatus: 5.1.1\n")
    parent.attach(dsn_part)

    child = _child()
    rfc822_part = EmailMessage()
    rfc822_part.set_type("message/rfc822")
    rfc822_part.set_payload([child])
    # rfc822_part has no Content-Disposition header
    assert rfc822_part.get_content_disposition() is None
    parent.attach(rfc822_part)

    db, root = _import(tmp_path, parent)

    with sqlite3.connect(db) as conn:
        body = conn.execute("SELECT body_text FROM messages").fetchone()[0]
        attachments = conn.execute(
            "SELECT attachment_name FROM message_attachments"
        ).fetchall()
        failures = conn.execute("SELECT COUNT(*) FROM ingest_failures").fetchone()[0]

    assert OUTER in body
    assert INNER in body
    assert attachments == []
    assert failures == 0

    assert len(_hits(db, OUTER)) == 1
    assert len(_hits(db, INNER)) == 1


def test_multipart_mixed_stores_rfc822_no_disposition_as_attachment(
    tmp_path: Path,
) -> None:
    """multipart/mixed containing message/rfc822 without disposition:

    Inner body is NOT merged into outer body and NOT searchable via outer message.
    The embedded mail is stored as a message/rfc822 attachment named after its subject.
    """
    parent = EmailMessage()
    parent.make_mixed()
    parent["Subject"] = "Forwarded conversation"

    body = EmailMessage()
    body.set_content(f"Here is the forwarded thread: {OUTER}")
    parent.attach(body)

    child = _child(subject="Project Alpha Quote")
    rfc822_part = EmailMessage()
    rfc822_part.set_type("message/rfc822")
    rfc822_part.set_payload([child])
    # rfc822_part has no Content-Disposition header
    assert rfc822_part.get_content_disposition() is None
    parent.attach(rfc822_part)

    db, root = _import(tmp_path, parent)

    with sqlite3.connect(db) as conn:
        body_text = conn.execute("SELECT body_text FROM messages").fetchone()[0]
        rows = conn.execute(
            "SELECT attachment_name, mime_type, content_hash, storage_path "
            "FROM message_attachments"
        ).fetchall()
        failures = conn.execute("SELECT COUNT(*) FROM ingest_failures").fetchone()[0]

    assert OUTER in body_text
    assert INNER not in body_text
    assert failures == 0

    assert len(rows) == 1
    att_name, mime_type, content_hash, storage_path = rows[0]
    assert att_name == "Project Alpha Quote.eml"
    assert mime_type == "message/rfc822"
    assert content_hash and storage_path

    # Verify attachment content is readable and preserved whole
    stored_bytes = attachment_store.read_attachment(storage_path, root)
    stored = message_from_bytes(stored_bytes, policy=policy.default)
    assert stored["Subject"] == "Project Alpha Quote"
    assert stored["Message-ID"] == "<original-evidence@example.com>"
    assert INNER in stored.get_body(("plain",)).get_content()

    # Search behavior
    assert len(_hits(db, OUTER)) == 1
    assert _hits(db, INNER) == []


def test_multipart_mixed_rfc822_no_disposition_fallback_name_when_no_subject(
    tmp_path: Path,
) -> None:
    """When child subject is empty and no disposition filename exists,

    the attachment is named 'attached-message.eml'.
    """
    parent = EmailMessage()
    parent.make_mixed()

    body = EmailMessage()
    body.set_content(OUTER)
    parent.attach(body)

    child = _child(subject="")
    rfc822_part = EmailMessage()
    rfc822_part.set_type("message/rfc822")
    rfc822_part.set_payload([child])
    parent.attach(rfc822_part)

    db, root = _import(tmp_path, parent)

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT attachment_name, mime_type FROM message_attachments"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "attached-message.eml"
    assert rows[0][1] == "message/rfc822"

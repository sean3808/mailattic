"""#237: message/rfc822 attachment is stored whole and never leaks into the parent."""

from __future__ import annotations

import sqlite3
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

import pytest

from mailattic import attachment_store
from mailattic.ingest import import_maildir_sources
from mailattic.models import MaildirSource
from mailattic.query import SearchFilters, search_messages
from mailattic.storage import initialize_database

OUTER = "OUTERORDERKEY"
INNER = "INNERORDERKEY"


def _child() -> EmailMessage:
    child = EmailMessage()
    child["From"] = "child@example.com"
    child["To"] = "child-rcpt@example.com"
    child["Subject"] = "Attached evidence"
    child["Message-ID"] = "<child-evidence@example.com>"
    child.set_content(INNER)
    # ordinary attachment inside the child must not surface on the parent
    child.add_attachment(
        b"inner-bytes",
        maintype="application",
        subtype="octet-stream",
        filename="inner.bin",
    )
    return child


def _import(tmp_path: Path, parent: EmailMessage) -> tuple[Path, Path]:
    folder = tmp_path / "maildir" / "INBOX"
    (folder / "new").mkdir(parents=True)
    parent["From"] = "parent@example.com"
    parent["To"] = "parent-rcpt@example.com"
    parent["Subject"] = "Parent email"
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
    assert summary.failure_count == 0
    return db, root


def _hits(db: Path, keyword: str) -> list[dict[str, object]]:
    return search_messages(database_path=db, filters=SearchFilters(keyword=keyword))


def _plain_parent() -> EmailMessage:
    parent = EmailMessage()
    parent.set_content(OUTER)
    return parent


def _html_parent() -> EmailMessage:
    parent = EmailMessage()
    parent.set_content(f"<html><body><p>{OUTER}</p></body></html>", subtype="html")
    return parent


@pytest.mark.parametrize("make_parent", [_plain_parent, _html_parent])
def test_rfc822_attachment_stored_whole_and_not_in_parent_body(
    tmp_path: Path, make_parent
) -> None:
    parent = make_parent()
    parent.add_attachment(_child(), filename="evidence.eml")
    db, root = _import(tmp_path, parent)

    with sqlite3.connect(db) as conn:
        body = conn.execute("SELECT body_text FROM messages").fetchone()[0]
        rows = conn.execute(
            "SELECT attachment_name, content_hash, storage_path "
            "FROM message_attachments"
        ).fetchall()
        failures = conn.execute("SELECT COUNT(*) FROM ingest_failures").fetchone()[0]

    assert OUTER in body
    assert INNER not in body
    assert failures == 0
    # child's own attachment is part of the child, not the parent's attachment list
    assert [r[0] for r in rows] == ["evidence.eml"]
    _, content_hash, storage_path = rows[0]
    assert content_hash and storage_path

    stored = message_from_bytes(
        attachment_store.read_attachment(storage_path, root), policy=policy.default
    )
    assert stored["Subject"] == "Attached evidence"
    assert stored["Message-ID"] == "<child-evidence@example.com>"
    assert INNER in stored.get_body(("plain",)).get_content()

    assert len(_hits(db, OUTER)) == 1
    assert _hits(db, INNER) == []


def test_non_ascii_rfc822_child_round_trips(tmp_path: Path) -> None:
    child = EmailMessage()
    child["From"] = "child@example.com"
    child["Subject"] = "中文主旨"
    child["Message-ID"] = "<child-zh@example.com>"
    child.set_content("內文標記", charset="utf-8", cte="8bit")
    parent = _plain_parent()
    parent.add_attachment(child, filename="轉寄.eml")
    db, root = _import(tmp_path, parent)

    with sqlite3.connect(db) as conn:
        body, storage_path = conn.execute(
            "SELECT m.body_text, a.storage_path FROM messages m "
            "JOIN message_attachments a ON a.message_id = m.id"
        ).fetchone()

    assert "內文標記" not in body
    stored = message_from_bytes(
        attachment_store.read_attachment(storage_path, root), policy=policy.default
    )
    assert stored["Subject"] == "中文主旨"
    assert "內文標記" in stored.get_body(("plain",)).get_content()


def test_ordinary_attachment_and_alternative_unchanged(tmp_path: Path) -> None:
    parent = EmailMessage()
    parent.set_content(OUTER)
    parent.add_alternative(f"<p>{OUTER}-html</p>", subtype="html")
    parent.add_attachment(
        b"payload", maintype="application", subtype="pdf", filename="a.pdf"
    )
    db, root = _import(tmp_path, parent)
    with sqlite3.connect(db) as conn:
        body = conn.execute("SELECT body_text FROM messages").fetchone()[0]
        row = conn.execute(
            "SELECT attachment_name, storage_path FROM message_attachments"
        ).fetchone()
    assert body == OUTER
    assert row[0] == "a.pdf"
    assert attachment_store.read_attachment(row[1], root) == b"payload"


def test_rfc822_attachment_without_filename_is_still_stored_whole(
    tmp_path: Path,
) -> None:
    # Marked as an attachment, so its text stays out of the parent body; with no
    # filename it must not fall through both paths and vanish.
    parent = _plain_parent()
    parent.add_attachment(_child())
    assert parent.get_payload()[1].get_filename() is None
    db, root = _import(tmp_path, parent)

    with sqlite3.connect(db) as conn:
        body = conn.execute("SELECT body_text FROM messages").fetchone()[0]
        rows = conn.execute(
            "SELECT attachment_name, storage_path FROM message_attachments"
        ).fetchall()

    assert INNER not in body
    assert [r[0] for r in rows] == ["Attached evidence.eml"]
    stored = message_from_bytes(
        attachment_store.read_attachment(rows[0][1], root), policy=policy.default
    )
    assert stored["Message-ID"] == "<child-evidence@example.com>"

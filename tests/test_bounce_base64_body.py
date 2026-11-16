"""Regression tests for #92: bounce reports with base64 message/delivery-status.

When readpst emits a bounce as multipart/mixed containing:
  [0] text/plain or text/html (human-readable notice)
  [1] message/rfc822 (original message)
  [2] message/delivery-status (CTE: base64, Content-Disposition: attachment)
Python's email parser treats message/delivery-status as multipart and exposes
its undecoded base64 payload as a synthetic text/plain child part. Walking into
that part leaked raw base64 into body_text (and for text/html bounces, caused
the synthetic plain part to shadow the HTML notice completely).
"""

from __future__ import annotations

import sqlite3
from email import policy
from email.parser import BytesParser
from pathlib import Path

from mailattic.ingest import import_maildir_sources
from mailattic.models import MaildirSource
from mailattic.parser import _extract_attachments, _extract_body_text
from mailattic.storage import initialize_database

_FIXTURES = Path(__file__).parent / "fixtures" / "bounce-base64"
_BASE64_SNIPPET = "UmVwb3J0aW5n"


def _parse_fixture(name: str):
    return BytesParser(policy=policy.default).parsebytes(
        (_FIXTURES / name).read_bytes()
    )


def _import_eml_bytes(tmp_path: Path, raw: bytes) -> str:
    folder = tmp_path / "maildir" / "INBOX"
    (folder / "new").mkdir(parents=True, exist_ok=True)
    (folder / "new" / "sample.eml").write_bytes(raw)
    db = initialize_database(tmp_path / "data.db")
    summary = import_maildir_sources(
        database_path=db,
        sources=[
            MaildirSource(
                path=folder,
                account_name="audit",
                account_email="sender@mailattic.test",
                folder_name="INBOX",
                requested_from=folder.parent,
            )
        ],
        attachment_root=tmp_path / "attachments",
    )
    assert summary.imported_messages == 1
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT body_text FROM messages").fetchone()[0]


def test_plain_bounce_with_base64_delivery_status_excludes_base64(
    tmp_path: Path,
) -> None:
    raw = (_FIXTURES / "bounce-plain-dsn-base64.eml").read_bytes()
    extracted = _extract_body_text(_parse_fixture("bounce-plain-dsn-base64.eml"))
    stored = _import_eml_bytes(tmp_path, raw)

    expected = "Your message could not be delivered to recipient@mailattic.test."
    for body in (extracted, stored):
        assert expected in body
        assert _BASE64_SNIPPET not in body
        assert "Original message body placeholder." not in body


def test_html_bounce_with_base64_delivery_status_keeps_html_and_excludes_base64(
    tmp_path: Path,
) -> None:
    raw = (_FIXTURES / "bounce-html-dsn-base64.eml").read_bytes()
    extracted = _extract_body_text(_parse_fixture("bounce-html-dsn-base64.eml"))
    stored = _import_eml_bytes(tmp_path, raw)

    for body in (extracted, stored):
        assert "Mail delivery failed for recipient@mailattic.test." in body
        assert _BASE64_SNIPPET not in body
        assert "Original HTML bounce target placeholder." not in body


def test_multipart_report_with_base64_delivery_status_without_disposition(
    tmp_path: Path,
) -> None:
    """Even in multipart/report without Content-Disposition on delivery-status,
    base64 DSN bytes must not leak into body_text while rfc822 body stays."""
    raw = (
        b"From: mailer-daemon@mailattic.test\r\n"
        b"To: sender@mailattic.test\r\n"
        b"Subject: Delivery Status Notification (Failure)\r\n"
        b"Date: Mon, 16 Nov 2026 10:10:00 +0800\r\n"
        b"Message-ID: <bounce-report-1@mailattic.test>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/report; report-type=delivery-status; "
        b'boundary="RPT92"\r\n'
        b"\r\n"
        b"--RPT92\r\n"
        b'Content-Type: text/plain; charset="us-ascii"\r\n'
        b"\r\n"
        b"Human-readable report notice.\r\n"
        b"\r\n"
        b"--RPT92\r\n"
        b"Content-Type: message/delivery-status\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"UmVwb3J0aW5nLU1UQTogZG5zOyBteC5tYWlsYXR0aWMudGVzdA0K\r\n"
        b"\r\n"
        b"--RPT92\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n"
        b"From: sender@mailattic.test\r\n"
        b"To: recipient@mailattic.test\r\n"
        b"Subject: Original\r\n"
        b'Content-Type: text/plain; charset="us-ascii"\r\n'
        b"\r\n"
        b"Inner original message text.\r\n"
        b"\r\n"
        b"--RPT92--\r\n"
    )
    stored = _import_eml_bytes(tmp_path, raw)
    assert "Human-readable report notice." in stored
    assert "Inner original message text." in stored
    assert _BASE64_SNIPPET not in stored


def test_dsn_fixture_preserves_expected_attachments(tmp_path: Path) -> None:
    """L1.d: Pin attachment extraction for the DSN fixture (unchanged from base)."""
    msg = _parse_fixture("bounce-plain-dsn-base64.eml")
    attachments = _extract_attachments(msg, attachment_root=tmp_path / "att")
    assert [(a["attachment_name"], a["mime_type"]) for a in attachments] == [
        ("Placeholder Subject.eml", "message/rfc822")
    ]
    size = attachments[0]["size_bytes"]
    assert isinstance(size, int) and size > 0
    assert attachments[0]["content_hash"] is not None

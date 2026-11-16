"""Regression tests for #96: non-rfc822 message/* report containers in body extraction.

Python's email parser treats every non-rfc822 message/* part (such as
message/disposition-notification, message/feedback-report,
message/global-delivery-status, as well as message/delivery-status from #92)
as multipart and exposes its payload as synthetic text/plain child parts.
When encoded as base64, raw base64 leaked into body_text (and in HTML-led
messages, the synthetic text/plain part shadowed the HTML notice).
"""

from __future__ import annotations

import base64
import sqlite3
from email import policy
from email.parser import BytesParser
from pathlib import Path

from mailattic.ingest import import_maildir_sources
from mailattic.models import MaildirSource
from mailattic.parser import _extract_attachments, _extract_body_text
from mailattic.storage import initialize_database

_MDN_PAYLOAD = (
    b"Reporting-UA: mail.example.com; ExampleClient 1.0\r\n"
    b"Final-Recipient: rfc822; recipient@example.net\r\n"
    b"Disposition: manual-action/MDN-sent-manually; displayed\r\n"
)
_MDN_BASE64 = base64.b64encode(_MDN_PAYLOAD)
_MDN_SNIPPET = _MDN_BASE64[:12].decode("ascii")

_FEEDBACK_PAYLOAD = (
    b"Feedback-Type: abuse\r\nUser-Agent: FeedbackTool/1.0\r\nVersion: 1\r\n"
)
_FEEDBACK_BASE64 = base64.b64encode(_FEEDBACK_PAYLOAD)
_FEEDBACK_SNIPPET = _FEEDBACK_BASE64[:12].decode("ascii")

_GLOBAL_DSN_PAYLOAD = (
    b"Reporting-MTA: dns; mx.example.com\r\n"
    b"Final-Recipient: utf-8; user@example.net\r\n"
    b"Action: failed\r\n"
    b"Status: 5.1.1\r\n"
)
_GLOBAL_DSN_BASE64 = base64.b64encode(_GLOBAL_DSN_PAYLOAD)
_GLOBAL_DSN_SNIPPET = _GLOBAL_DSN_BASE64[:12].decode("ascii")


def _parse_bytes(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


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
                account_email="sender@example.com",
                folder_name="INBOX",
                requested_from=folder.parent,
            )
        ],
        attachment_root=tmp_path / "attachments",
    )
    assert summary.imported_messages == 1
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT body_text FROM messages").fetchone()[0]


def test_plain_mdn_report_with_base64_excludes_base64(tmp_path: Path) -> None:
    raw = (
        b"From: recipient@example.net\r\n"
        b"To: sender@example.com\r\n"
        b"Subject: Read: Quarterly Notice\r\n"
        b"Date: Mon, 16 Nov 2026 10:15:00 +0800\r\n"
        b"Message-ID: <mdn-plain-1@example.net>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/report; report-type=disposition-notification; "
        b'boundary="MDN96A"\r\n'
        b"\r\n"
        b"--MDN96A\r\n"
        b'Content-Type: text/plain; charset="us-ascii"\r\n'
        b"\r\n"
        b"Your message was displayed by recipient@example.net.\r\n"
        b"\r\n"
        b"--MDN96A\r\n"
        b"Content-Type: message/disposition-notification\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + _MDN_BASE64 + b"\r\n"
        b"--MDN96A--\r\n"
    )
    extracted = _extract_body_text(_parse_bytes(raw))
    stored = _import_eml_bytes(tmp_path, raw)

    for body in (extracted, stored):
        assert "Your message was displayed by recipient@example.net." in body
        assert _MDN_SNIPPET not in body


def test_html_led_mdn_message_keeps_html_and_excludes_base64(tmp_path: Path) -> None:
    raw = (
        b"From: recipient@example.net\r\n"
        b"To: sender@example.com\r\n"
        b"Subject: Read: Project Update\r\n"
        b"Date: Mon, 16 Nov 2026 10:20:00 +0800\r\n"
        b"Message-ID: <mdn-html-1@example.net>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/report; report-type=disposition-notification; "
        b'boundary="MDN96B"\r\n'
        b"\r\n"
        b"--MDN96B\r\n"
        b'Content-Type: text/html; charset="utf-8"\r\n'
        b"\r\n"
        b"<html><body><p>HTML read receipt for Project Update.</p></body></html>\r\n"
        b"\r\n"
        b"--MDN96B\r\n"
        b"Content-Type: message/disposition-notification\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + _MDN_BASE64 + b"\r\n"
        b"--MDN96B--\r\n"
    )
    extracted = _extract_body_text(_parse_bytes(raw))
    stored = _import_eml_bytes(tmp_path, raw)

    for body in (extracted, stored):
        assert "HTML read receipt for Project Update." in body
        assert _MDN_SNIPPET not in body


def test_feedback_report_and_global_dsn_exclude_base64(tmp_path: Path) -> None:
    feedback_raw = (
        b"From: abuse@example.net\r\n"
        b"To: postmaster@example.com\r\n"
        b"Subject: Abuse Feedback Report\r\n"
        b"Date: Mon, 16 Nov 2026 10:25:00 +0800\r\n"
        b"Message-ID: <arf-1@example.net>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/report; report-type=feedback-report; "
        b'boundary="ARF96"\r\n'
        b"\r\n"
        b"--ARF96\r\n"
        b'Content-Type: text/plain; charset="us-ascii"\r\n'
        b"\r\n"
        b"Human-readable feedback summary.\r\n"
        b"\r\n"
        b"--ARF96\r\n"
        b"Content-Type: message/feedback-report\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + _FEEDBACK_BASE64 + b"\r\n"
        b"--ARF96--\r\n"
    )
    feedback_body = _extract_body_text(_parse_bytes(feedback_raw))
    assert "Human-readable feedback summary." in feedback_body
    assert _FEEDBACK_SNIPPET not in feedback_body

    global_dsn_raw = (
        b"From: mailer-daemon@example.net\r\n"
        b"To: sender@example.com\r\n"
        b"Subject: Internationalized Delivery Failure\r\n"
        b"Date: Mon, 16 Nov 2026 10:30:00 +0800\r\n"
        b"Message-ID: <global-dsn-1@example.net>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/report; report-type=global-delivery-status; "
        b'boundary="GDSN96"\r\n'
        b"\r\n"
        b"--GDSN96\r\n"
        b'Content-Type: text/plain; charset="us-ascii"\r\n'
        b"\r\n"
        b"Global delivery failure notice.\r\n"
        b"\r\n"
        b"--GDSN96\r\n"
        b"Content-Type: message/global-delivery-status\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + _GLOBAL_DSN_BASE64 + b"\r\n"
        b"--GDSN96--\r\n"
    )
    global_body = _import_eml_bytes(tmp_path, global_dsn_raw)
    assert "Global delivery failure notice." in global_body
    assert _GLOBAL_DSN_SNIPPET not in global_body


def test_rfc822_attachment_still_excluded_from_body_and_extracted_as_eml(
    tmp_path: Path,
) -> None:
    raw = (
        b"From: forwarder@example.com\r\n"
        b"To: colleague@example.com\r\n"
        b"Subject: Fwd: Budget Draft\r\n"
        b"Date: Mon, 16 Nov 2026 10:35:00 +0800\r\n"
        b"Message-ID: <fwd-rfc822-1@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="FWD96"\r\n'
        b"\r\n"
        b"--FWD96\r\n"
        b'Content-Type: text/plain; charset="us-ascii"\r\n'
        b"\r\n"
        b"Please see the forwarded message.\r\n"
        b"\r\n"
        b"--FWD96\r\n"
        b"Content-Type: message/rfc822\r\n"
        b'Content-Disposition: attachment; filename="Budget Draft.eml"\r\n'
        b"\r\n"
        b"From: author@example.net\r\n"
        b"To: forwarder@example.com\r\n"
        b"Subject: Budget Draft\r\n"
        b'Content-Type: text/plain; charset="us-ascii"\r\n'
        b"\r\n"
        b"Attached rfc822 body should stay in the attachment.\r\n"
        b"\r\n"
        b"--FWD96--\r\n"
    )
    msg = _parse_bytes(raw)
    body = _extract_body_text(msg)
    assert "Please see the forwarded message." in body
    assert "Attached rfc822 body should stay in the attachment." not in body

    attachments = _extract_attachments(msg, attachment_root=tmp_path / "att")
    assert [(a["attachment_name"], a["mime_type"]) for a in attachments] == [
        ("Budget Draft.eml", "message/rfc822")
    ]
    assert attachments[0]["content_hash"] is not None


def test_report_attachment_extraction_unchanged(tmp_path: Path) -> None:
    """Attachment walk must still see attachments alongside non-rfc822 message/*."""
    raw = (
        b"From: recipient@example.net\r\n"
        b"To: sender@example.com\r\n"
        b"Subject: Read: Attachment Check\r\n"
        b"Date: Mon, 16 Nov 2026 10:40:00 +0800\r\n"
        b"Message-ID: <mdn-att-1@example.net>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="MDNATT96"\r\n'
        b"\r\n"
        b"--MDNATT96\r\n"
        b'Content-Type: text/plain; charset="us-ascii"\r\n'
        b"\r\n"
        b"Notice body.\r\n"
        b"\r\n"
        b"--MDNATT96\r\n"
        b"Content-Type: message/disposition-notification\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + _MDN_BASE64 + b"\r\n"
        b"--MDNATT96\r\n"
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="receipt.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"JVBERi0xLjQK\r\n"
        b"\r\n"
        b"--MDNATT96\r\n"
        b"Content-Type: message/rfc822\r\n"
        b'Content-Disposition: attachment; filename="original.eml"\r\n'
        b"\r\n"
        b"From: sender@example.com\r\n"
        b"To: recipient@example.net\r\n"
        b"Subject: Original\r\n"
        b'Content-Type: text/plain; charset="us-ascii"\r\n'
        b"\r\n"
        b"Original body.\r\n"
        b"\r\n"
        b"--MDNATT96--\r\n"
    )
    msg = _parse_bytes(raw)
    attachments = _extract_attachments(msg, attachment_root=tmp_path / "att")
    assert [(a["attachment_name"], a["mime_type"]) for a in attachments] == [
        ("receipt.pdf", "application/pdf"),
        ("original.eml", "message/rfc822"),
    ]
    assert all(a["content_hash"] is not None for a in attachments)

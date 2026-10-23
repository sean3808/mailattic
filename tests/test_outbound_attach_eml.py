"""Issue #21: an attached .eml must be legal MIME on the wire and keep its bytes."""

from __future__ import annotations

import hashlib
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from mailattic.attachment_store import read_attachment, resolve_store_root
from mailattic.outbound import _extract_draft_attachments
from mailattic.storage import initialize_database
from tests.test_outbound_sent_attachments import _draft_reply, _seed_message

ASCII_EML = (
    b"From: nested@mailattic.test\r\n"
    b"To: sean3808@mailattic.test\r\n"
    b"Subject: Nested evidence\r\n"
    b"\r\n"
    b"Nested body\r\n"
)
UTF8_EML = (
    "From: nested@mailattic.test\r\n"
    "Subject: 測試\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "Content-Transfer-Encoding: 8bit\r\n"
    "\r\n"
    "內容\r\n"
).encode()
MALFORMED_EML = b"no headers here\x00\xff\xfe\r\njust garbage"


@pytest.fixture(autouse=True)
def _host_maps_eml_to_rfc822(monkeypatch: pytest.MonkeyPatch) -> None:
    # `.eml` is not in the repo table, so resolution falls through to the host
    # map; fix that answer so the test checks semantics, not the OS registry.
    monkeypatch.setattr(
        "mailattic.outbound.mimetypes.guess_type",
        lambda _name: ("message/rfc822", None),
    )


def _attach_and_store(tmp_path: Path, name: str, data: bytes):
    db_path = initialize_database(tmp_path / "mailattic.db")
    seed = _seed_message(db_path)
    attached = tmp_path / name
    attached.write_bytes(data)
    result = _draft_reply(db_path, seed, attachments=[attached])
    draft_bytes = result.draft_path.read_bytes()
    msg = BytesParser(policy=policy.default).parsebytes(draft_bytes)
    (part,) = list(msg.iter_attachments())
    store_root = resolve_store_root(db_path)
    (record,) = _extract_draft_attachments(msg, store_root, "d", "<m@x>")
    return result, draft_bytes, part, read_attachment(record.storage_path, store_root)


def test_ascii_eml_is_legal_rfc822_and_round_trips(tmp_path: Path) -> None:
    _, _, part, stored = _attach_and_store(tmp_path, "forward.eml", ASCII_EML)

    assert part.get_content_type() == "message/rfc822"
    assert part["Content-Transfer-Encoding"] in (None, "7bit", "8bit", "binary")
    assert part.get_payload(0)["Subject"] == "Nested evidence"
    # Allowed difference: CRLF -> LF normalisation, nothing else.
    assert stored.replace(b"\r\n", b"\n") == ASCII_EML.replace(b"\r\n", b"\n")


@pytest.mark.parametrize("data", [UTF8_EML, MALFORMED_EML])
def test_eml_that_cannot_be_nested_faithfully_keeps_exact_bytes(
    tmp_path: Path, data: bytes
) -> None:
    _, _, part, stored = _attach_and_store(tmp_path, "odd.eml", data)

    # Never an illegal base64 message/rfc822; the original bytes survive exactly.
    assert not (
        part.get_content_type() == "message/rfc822"
        and part["Content-Transfer-Encoding"] == "base64"
    )
    assert stored == data


def test_pdf_attachment_and_fingerprint_unchanged(tmp_path: Path) -> None:
    pdf = b"%PDF-1.4 regression payload"
    result, draft_bytes, part, stored = _attach_and_store(tmp_path, "q.pdf", pdf)

    assert part.get_content_type() == "application/pdf"
    assert stored == pdf
    assert result.fingerprint == hashlib.sha256(draft_bytes).hexdigest()

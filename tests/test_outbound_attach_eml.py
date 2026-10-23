"""Issue #21: an attached .eml must be a legal message/rfc822 part that round-trips."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from mailattic.attachment_store import read_attachment, resolve_store_root
from mailattic.outbound import _extract_draft_attachments
from mailattic.storage import initialize_database
from tests.test_outbound_sent_attachments import _draft_reply, _seed_message

NESTED = (
    b"From: nested@mailattic.test\r\n"
    b"To: sean3808@mailattic.test\r\n"
    b"Subject: Nested evidence\r\n"
    b"\r\n"
    b"Nested body\r\n"
)


def test_attached_eml_is_legal_rfc822_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the host mapping so the test checks semantics, not the OS registry.
    monkeypatch.setattr(
        "mailattic.outbound.mimetypes.guess_type",
        lambda _name: ("message/rfc822", None),
    )
    db_path = initialize_database(tmp_path / "mailattic.db")
    seed = _seed_message(db_path)
    eml = tmp_path / "forward.eml"
    eml.write_bytes(NESTED)

    result = _draft_reply(db_path, seed, attachments=[eml])

    msg = BytesParser(policy=policy.default).parsebytes(result.draft_path.read_bytes())
    (part,) = list(msg.iter_attachments())
    assert part.get_content_type() == "message/rfc822"
    assert part["Content-Transfer-Encoding"] in (None, "7bit", "8bit", "binary")
    assert part.get_payload(0)["Subject"] == "Nested evidence"

    store_root = resolve_store_root(db_path)
    (record,) = _extract_draft_attachments(msg, store_root, "d", "<m@x>")
    stored = read_attachment(record.storage_path, store_root)
    assert stored.replace(b"\r\n", b"\n") == NESTED.replace(b"\r\n", b"\n")

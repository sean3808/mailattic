"""Tests for the From line in the send-draft/send-batch review summary (#199).

The sending account must be visible at review time, not just inferred from
--env-file elsewhere: `_prepare_draft_for_send` builds the summary shared by
`send_draft`'s "Draft to send:" output and `send_batch`'s combined summary,
so covering it here exercises both callers' review text in one place.
"""

from __future__ import annotations

from pathlib import Path

from mailattic.outbound import (
    _prepare_draft_for_send,
    assemble_draft_reply,
)
from tests.test_outbound_draft import _seed_message


def test_prepared_draft_summary_shows_from_before_subject(tmp_path: Path) -> None:
    db_path = _seed_message(tmp_path)
    body_file = tmp_path / "body.txt"
    body_file.write_text("Reply body", encoding="utf-8")

    result = assemble_draft_reply(
        database_path=db_path,
        message_id="<test-msg-123@mailattic.test>",
        draft_dir=tmp_path / "outbound" / "drafts",
        own_address="sean3808@mailattic.test",
        body_from=body_file,
    )

    prepared = _prepare_draft_for_send(result.draft_id, tmp_path / "dummy.db")

    assert "  From: sean3808@mailattic.test\n" in prepared.summary_line
    assert prepared.summary_line.index("From:") < prepared.summary_line.index(
        "Subject:"
    )

"""Pin the two evidence rules of triage-labels.md `Watch periods are not acceptance`.

Each rule has its own assertion keyed to wording unique to that rule, so deleting one
rule turns only its own test red.
"""

import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "agents" / "triage-labels.md"


def _watch_section() -> str:
    markdown = DOC.read_text(encoding="utf-8")
    match = re.search(
        r"^### Watch periods are not acceptance\n(.*?)(?=^### )", markdown, re.S | re.M
    )
    assert match, "Watch periods section not found"
    return match.group(1)


def test_watch_ticket_states_in_what_form_evidence_is_kept() -> None:
    section = _watch_section()
    assert "in what form the evidence is kept" in section
    assert "state which one you chose" in section


def test_checker_reads_comments_and_timeline_before_denying() -> None:
    section = _watch_section()
    assert "timeline" in section
    assert "`closedAt`" in section

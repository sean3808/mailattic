#!/usr/bin/env python3
"""Validate the machine-checkable structure of a pull-request body.

This gate detects OMISSION, not forgery. The bodies it reads are written by
cooperating agents that can forget a field, not by an adversary crafting
Markdown that renders one way and parses another.

An earlier revision emulated CommonMark to resist forgery -- inline code
spans, HTML comments, HTML tags, Setext section boundaries. Measured against
every pull-request body this repository has ever had, that emulation changed
no verdict, while diverging from GitHub's own renderer in the direction that
costs the most: it rejected compliant bodies over ordinary prose containing
`a<b`, and over a `---` thematic break inside the squash-message section.
A false rejection turns the gate into noise; a forged structure still has to
survive the pre-merge read of the squash message itself (the agent shows it
to Sean when asking for merge authorization). So the
emulation is gone and the trust boundary is stated instead of implied.
"""

from __future__ import annotations

import os
import re
import sys

SQUASH_HEADING = "## Squash message"
CLOSING_REFERENCE = "Closes #n"

_FENCE_OPEN = re.compile(r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")
_SQUASH_HEADING = re.compile(
    r"^[ ]{0,3}##[ \t]+Squash[ \t]+message(?:[ \t]+#+)?[ \t]*$"
)
_CLOSING_REFERENCE = re.compile(r"(?i)(?<!\w)Closes[ \t]+#[1-9]\d*\b")


def missing_fields(body: str) -> list[str]:
    """Return missing contract fields in their canonical display order."""
    fence: str | None = None
    has_heading = False
    has_closing_reference = False

    for line in body.splitlines():
        if fence is not None:
            closing = _FENCE_CLOSE.match(line)
            if (
                closing
                and closing.group("fence")[0] == fence[0]
                and len(closing.group("fence")) >= len(fence)
            ):
                fence = None
            elif has_heading and _CLOSING_REFERENCE.search(line):
                # Squash messages in this repository conventionally live in a
                # fenced block, so fenced content counts -- but the opening
                # line does not: an info string is fence metadata, not content.
                has_closing_reference = True
            continue

        opening = _FENCE_OPEN.match(line)
        if opening:
            fence = opening.group("fence")
            continue

        if _SQUASH_HEADING.match(line):
            has_heading = True
            continue

        if has_heading and _CLOSING_REFERENCE.search(line):
            has_closing_reference = True

    missing: list[str] = []
    if not has_heading:
        missing.append(SQUASH_HEADING)
    if not has_closing_reference:
        missing.append(CLOSING_REFERENCE)
    return missing


def main() -> int:
    missing = missing_fields(os.environ.get("PR_BODY", ""))
    if not missing:
        print("PR contract structure is complete.")
        return 0

    print("PR contract structure is incomplete:", file=sys.stderr)
    messages = {
        SQUASH_HEADING: "missing `## Squash message` heading",
        CLOSING_REFERENCE: "missing `Closes #n` reference",
    }
    for field in missing:
        print(f"- {messages[field]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

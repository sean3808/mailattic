"""Tests for `.github/pull_request_template.md` and `scripts/check_pr_contract.py`."""

from __future__ import annotations

from pathlib import Path

import scripts.check_pr_contract as check_pr_contract

REPO_ROOT = Path(__file__).resolve().parents[1]
PR_TEMPLATE = REPO_ROOT / ".github" / "pull_request_template.md"

RELEASE_NOTE_LABELS = (
    "breaking-change",
    "enhancement",
    "bug",
    "documentation",
    "dependencies",
    "skip-changelog",
)


def test_pr_template_unfilled_shows_squash_heading_and_not_contract_json() -> None:
    content = PR_TEMPLATE.read_text(encoding="utf-8")

    assert "## Squash message" in content
    assert "Pull request delivery contract" in content
    assert "pr_delivery_contract" not in content
    for label in RELEASE_NOTE_LABELS:
        assert label in content
    # Unfilled placeholder must not accidentally satisfy the numeric issue check.
    assert check_pr_contract.missing_fields(content) == ["Closes #n"]


def test_pr_template_filled_passes_pr_contract_check() -> None:
    content = PR_TEMPLATE.read_text(encoding="utf-8")
    filled = (
        content.replace("<type>(<scope>): <subject>", "docs(github): add templates")
        .replace(
            "<Explain why this change is needed.>",
            "Provide standard community templates for PRs and issues.",
        )
        .replace(
            "Closes #<issue> — <Explain why this PR closes the issue.>",
            "Closes #87 — Add PR template, issue forms, and SECURITY.md.",
        )
    )

    assert check_pr_contract.missing_fields(filled) == []

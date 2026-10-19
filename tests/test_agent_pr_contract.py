"""Guards for the agent-facing harness contracts: PR delivery and the brief."""

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS = REPO_ROOT / "AGENTS.md"
TRIAGE_LABELS = REPO_ROOT / "docs" / "agents" / "triage-labels.md"

JSON_BLOCK = re.compile(r"```json\s*\n(?P<body>.*?)\n```", re.DOTALL)
MARKDOWN_HEADING = re.compile(
    r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*#*\s*$", re.MULTILINE
)

EXPECTED_CONTRACT = {
    "pull_request_body": {
        "squash_message": {
            "heading": "## Squash message",
            "must_explain_why": True,
            "per_issue": {
                "closing_reference": "Closes #n",
                "must_explain_why": True,
            },
        }
    },
    "completion": {"pull_request_state": "ready_for_review"},
}


def _heading_section_ranges(markdown: str) -> dict[str, tuple[int, int]]:
    """Map GitHub-style heading anchors to their Markdown section ranges."""
    headings = list(MARKDOWN_HEADING.finditer(markdown))
    sections = {}
    seen: dict[str, int] = {}
    for index, match in enumerate(headings):
        title = match.group("title").lower()
        anchor = re.sub(r"[^\w\- ]", "", title)
        fragment = re.sub(r" +", "-", anchor)
        # GitHub suffixes repeated headings -1, -2, ...; without it the later
        # twin silently overwrites the first and `#dup-1` looks dead.
        seen[fragment] = seen.get(fragment, -1) + 1
        if seen[fragment]:
            fragment = f"{fragment}-{seen[fragment]}"
        level = len(match.group("marks"))
        end = next(
            (
                later.start()
                for later in headings[index + 1 :]
                if len(later.group("marks")) <= level
            ),
            len(markdown),
        )
        sections[fragment] = (match.start(), end)
    return sections


def _section(path: Path, fragment_prefix: str) -> str:
    """Return the Markdown of the one section whose anchor starts with the prefix."""
    markdown = path.read_text(encoding="utf-8")
    spans = [
        span
        for fragment, span in _heading_section_ranges(markdown).items()
        if fragment.startswith(fragment_prefix)
    ]
    assert len(spans) == 1, f"{path.name}: {fragment_prefix!r} matched {len(spans)}"
    start, end = spans[0]
    return markdown[start:end]


def test_ready_for_agent_brief_is_declared_to_be_the_review_baseline():
    """The brief reaches the reviewer, so 不做 entries need a diff-only bar.

    Honest boundary: this asserts the rule is stated, not that any real brief
    obeys it. That is the ceiling for a documentation rule — #122 already
    rejected the stronger version (scanning issue comments needs the API and
    stays red forever against history).
    """
    section = _section(TRIAGE_LABELS, "applying-ready-for-agent")
    assert "second reader" in section
    assert "intent baseline" in section
    assert "reading only the diff" in section


def test_pr_delivery_contract_has_one_canonical_body_in_the_whole_repo():
    tracked = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z", "*.md"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout.split("\0")
    documents = {
        path: path.read_text(encoding="utf-8")
        for name in tracked
        if name and (path := REPO_ROOT / name).is_file()
    }
    assert AGENTS in documents

    contract_blocks = [
        (path, json.loads(match.group("body")))
        for path, text in documents.items()
        for match in JSON_BLOCK.finditer(text)
        if "pr_delivery_contract" in match.group("body")
    ]
    assert len(contract_blocks) == 1
    assert contract_blocks[0][0] == AGENTS
    assert contract_blocks[0][1] == {"pr_delivery_contract": EXPECTED_CONTRACT}


def test_claude_md_only_imports_agents_md():
    """Claude Code 只經這一行載入 AGENTS.md；它壞掉時 Claude 會靜默失去整份正典。"""
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    # 只容許結尾空白：行首縮排四格會讓 Markdown 把它當成程式碼區塊、import 失效。
    assert claude.rstrip() == "@AGENTS.md"

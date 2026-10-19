"""Behavior tests for the pull-request contract structure gate.

The gate detects omission, not forgery (see the module docstring of
`scripts/check_pr_contract.py`). The tests are split accordingly: the omission
rules must go red the moment their rejection is removed, and the deliberately
out-of-scope forgery shapes are pinned so that re-adding CommonMark emulation
is a visible decision rather than a quiet one.
"""

from __future__ import annotations

from pathlib import Path

import scripts.check_pr_contract as check_pr_contract

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/pr-contract.yml"

# The shape this repository actually writes: squash message inside a fenced
# text block, with the closing reference inside the fence.
COMPLIANT = """\
Summary of the change.

## Squash message

```text
feat(ci): validate pull request structure

Closes #148 — Automate the mechanical half of the delivery contract.
```
"""


# --- Omission: the two rules this gate exists for -------------------------


def test_complete_body_passes() -> None:
    assert check_pr_contract.missing_fields(COMPLIANT) == []


def test_missing_squash_message_heading_is_rejected() -> None:
    body = "Closes #148 - Automate the mechanical contract checks.\n"

    assert check_pr_contract.missing_fields(body) == [
        "## Squash message",
        "Closes #n",
    ]


def test_missing_closing_reference_is_rejected() -> None:
    body = "## Squash message\n\nfeat(ci): validate pull request structure\n"

    assert check_pr_contract.missing_fields(body) == ["Closes #n"]


def test_main_reports_each_missing_field(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PR_BODY", "")

    assert check_pr_contract.main() == 1
    error = capsys.readouterr().err
    assert "missing `## Squash message` heading" in error
    assert "missing `Closes #n` reference" in error


def test_main_accepts_a_complete_body(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PR_BODY", COMPLIANT)

    assert check_pr_contract.main() == 0
    assert capsys.readouterr().out == "PR contract structure is complete.\n"


# --- Fence handling: the one structural rule that pays for itself ---------


def test_heading_text_inside_a_fenced_block_does_not_count() -> None:
    """A body that only *quotes* the heading has not written one."""
    body = """\
```markdown
## Squash message
```

Closes #148 - This still lacks a real heading.
"""

    assert check_pr_contract.missing_fields(body) == [
        "## Squash message",
        "Closes #n",
    ]


def test_heading_after_a_closed_fence_counts() -> None:
    body = """\
```text
Earlier evidence
```

## Squash message
Closes #148 — why
"""

    assert check_pr_contract.missing_fields(body) == []


def test_fenced_closing_reference_before_heading_does_not_count() -> None:
    body = """\
```text
Closes #99 — an earlier example
```

## Squash message
feat(ci): validate body
"""

    assert check_pr_contract.missing_fields(body) == ["Closes #n"]


def test_closing_reference_in_fence_info_string_does_not_count() -> None:
    """An info string is fence metadata, not squash-message content."""
    body = """\
## Squash message

```Closes #148
```
"""

    assert check_pr_contract.missing_fields(body) == ["Closes #n"]


def test_closing_reference_before_squash_section_does_not_count() -> None:
    body = "Closes #148\n\n## Squash message\n\nfeat(ci): validate body\n"

    assert check_pr_contract.missing_fields(body) == ["Closes #n"]


def test_hashes_joined_to_heading_text_do_not_count_as_closing_markers() -> None:
    """`message###` is heading text, so the heading is not `Squash message`."""
    body = "## Squash message###\n\nCloses #148\n"

    assert check_pr_contract.missing_fields(body) == [
        "## Squash message",
        "Closes #n",
    ]


def test_visible_closing_reference_does_not_need_to_start_its_line() -> None:
    body = "## Squash message\n\nReason: Closes #148\n"

    assert check_pr_contract.missing_fields(body) == []


# --- Regressions against re-introducing CommonMark emulation --------------
#
# Each of these was a real rejection of a compliant body by the emulating
# revision, confirmed against GitHub's own renderer via `gh api /markdown`.


def test_angle_bracket_in_prose_does_not_hide_a_later_heading() -> None:
    """`a<b` is text to GitHub, not an unterminated HTML tag."""
    body = "Threshold a<b holds.\n\n" + COMPLIANT

    assert check_pr_contract.missing_fields(body) == []


def test_thematic_break_does_not_end_the_squash_section() -> None:
    """`---` after a blank line is an `<hr>`, not a Setext heading."""
    body = """\
## Squash message

---

```text
feat(ci): validate body

Closes #148 — why
```
"""

    assert check_pr_contract.missing_fields(body) == []


def test_unmatched_backtick_does_not_hide_a_later_heading() -> None:
    body = "Missing closing tick: `oops\n\n" + COMPLIANT

    assert check_pr_contract.missing_fields(body) == []


# --- Trust boundary: shapes this gate deliberately does not catch ---------


def test_forged_structure_is_out_of_scope() -> None:
    """These pass on purpose. Making them fail is a scope change, not a fix.

    Every shape below requires an author who is actively working around the
    gate, which is not the threat this gate was built for. Catching them cost
    a hand-rolled CommonMark emulator that rejected compliant bodies (see the
    two regressions above) and changed no verdict on any real body. What
    catches them instead is the pre-merge read of the squash message, which
    judges whether it explains why -- a forged section fails there.
    """
    forged = {
        "html block wrapper": "<div>\n\n## Squash message\n\nCloses #148\n\n</div>\n",
        "html comment": "<!--\n## Squash message\nCloses #148\n-->\n",
        "closes only in inline code": "## Squash message\n\nAdd `Closes #148`.\n",
        "closes after the section": (
            "## Squash message\n\nfeat: x\n\n## Notes\n\nCloses #148\n"
        ),
    }

    assert {
        name: check_pr_contract.missing_fields(body) for name, body in forged.items()
    } == {name: [] for name in forged}


# --- Workflow wiring ------------------------------------------------------


def test_workflow_rechecks_body_edits() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert (
        "types: [opened, synchronize, reopened, ready_for_review, edited]" in workflow
    )


def test_workflow_runs_parser_through_uv_on_python_313() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "astral-sh/setup-uv@v9.0.0" in workflow
    assert (
        "uv run --python 3.13 --no-project python scripts/check_pr_contract.py"
        in workflow
    )
    assert "run: python scripts/check_pr_contract.py" not in workflow

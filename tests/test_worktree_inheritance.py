"""Contract for what `claude -w` copies into a fresh worktree."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREE_INCLUDE = REPO_ROOT / ".worktreeinclude"
SKILLS_LOCK = REPO_ROOT / "skills-lock.json"
AGENT_SKILLS = ".agents/skills"


# The two file shapes an installed skill actually has on disk, one nested.
# Probing both means a pattern cannot satisfy this contract by covering only
# the top-level definition while dropping a skill's sidecar files.
SKILL_FILE_SHAPES = ("SKILL.md", "agents/openai.yaml")


def _git(*args: str) -> list[str]:
    output = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in output.splitlines() if line]


def _ignored_under_agent_skills(*exclude_args: str) -> list[str]:
    return _git("ls-files", "--others", "--ignored", *exclude_args, "--", AGENT_SKILLS)


def _locked_skill_paths() -> list[Path]:
    locked = json.loads(SKILLS_LOCK.read_text(encoding="utf-8"))["skills"]
    root = REPO_ROOT / AGENT_SKILLS
    return [
        root / name / shape for name in sorted(locked) for shape in SKILL_FILE_SHAPES
    ]


def test_worktreeinclude_carries_every_locked_codex_skill() -> None:
    """Every skill in skills-lock.json must be copied into a new worktree.

    The paths are taken from the lock file rather than from disk because a
    plain `git worktree add` checkout does not contain the skill set at all --
    that absence is the very symptom being fixed, and an assertion needing
    those files present would fail in exactly the checkout PRs are verified
    in. Where a file is missing it is created as a probe at its real path, so
    a pattern can only satisfy this contract by covering the real thing.
    """
    created: list[Path] = []
    try:
        expected = set()
        for path in _locked_skill_paths():
            expected.add(path.relative_to(REPO_ROOT).as_posix())
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("probe\n", encoding="utf-8")
                created.append(path)

        ignored = set(_ignored_under_agent_skills("--exclude-standard"))
        copied = set(_ignored_under_agent_skills(f"--exclude-from={WORKTREE_INCLUDE}"))

        assert expected <= ignored, (
            f"{AGENT_SKILLS} is no longer gitignored; this contract assumes it is"
        )
        assert expected <= copied, (
            f".worktreeinclude does not cover {len(expected - copied)} locked skill "
            f"path(s), e.g. {sorted(expected - copied)[:3]} -- a fresh worktree's "
            "Codex agent would be missing them"
        )

        # Anything else already installed there has to come along too: the
        # lock file names skills, not every file a skill ships.
        rest = ignored - {p.relative_to(REPO_ROOT).as_posix() for p in created}
        assert rest <= copied, (
            f".worktreeinclude misses {len(rest - copied)} installed file(s), "
            f"e.g. {sorted(rest - copied)[:3]}"
        )
    finally:
        for path in created:
            path.unlink(missing_ok=True)
            for parent in path.parents:
                if (
                    parent == REPO_ROOT / AGENT_SKILLS
                    or not parent.is_dir()
                    or any(parent.iterdir())
                ):
                    break
                parent.rmdir()


def test_no_skills_under_agents_skills_are_tracked() -> None:
    """Tracking a skill silently retires the worktree copy mechanism.

    `.worktreeinclude` only ever copies gitignored files, so any vendored copy
    that gets committed stops being carried by that mechanism while the
    pattern still sits there looking effective. Following the removal of
    legacy Spectra skills, no paths under `.agents/skills` may be tracked in
    version control.
    """
    tracked = _git("ls-files", "--", AGENT_SKILLS)
    assert tracked == [], (
        f"tracked path(s) under {AGENT_SKILLS}: {tracked}; tracked files "
        "are never copied into a worktree, so .worktreeinclude no longer covers them"
    )

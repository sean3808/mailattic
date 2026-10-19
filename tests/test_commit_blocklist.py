"""Tests for pre-commit blocklist guard (scripts/check_commit_blocklist.py)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_commit_blocklist.py"
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
QUALITY_GATES_DOC = REPO_ROOT / "docs" / "agents" / "quality-gates.md"


def _init_git_repo(path: Path) -> Path:
    """Initialize a bare-minimum git repository for commit testing."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test Committer"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "committer@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Provide a freshly initialized git repository."""
    return _init_git_repo(tmp_path / "repo")


def _stage_file(repo: Path, relative_path: str, content: str) -> Path:
    """Write content to file and stage it."""
    file_path = repo / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "add", relative_path],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return file_path


def _run_guard(
    repo_root: Path,
    blocklist_path: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute the blocklist guard script in a target repository."""
    env = os.environ.copy()
    if blocklist_path is not None:
        env["MAILATTIC_COMMIT_BLOCKLIST"] = str(blocklist_path)
    else:
        env.pop("MAILATTIC_COMMIT_BLOCKLIST", None)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_refusal_when_env_var_unset(git_repo: Path) -> None:
    """Refuse commit when MAILATTIC_COMMIT_BLOCKLIST is not set."""
    _stage_file(git_repo, "clean.txt", "hello\n")

    result = _run_guard(git_repo, blocklist_path=None)

    assert result.returncode != 0
    assert "MAILATTIC_COMMIT_BLOCKLIST environment variable is not set" in result.stderr
    assert "Set MAILATTIC_COMMIT_BLOCKLIST" in result.stderr


def test_refusal_when_blocklist_file_missing(git_repo: Path, tmp_path: Path) -> None:
    """Refuse commit when the blocklist file does not exist."""
    _stage_file(git_repo, "clean.txt", "hello\n")
    missing_path = tmp_path / "nonexistent_blocklist.txt"

    result = _run_guard(git_repo, blocklist_path=missing_path)

    assert result.returncode != 0
    assert "Commit blocklist file does not exist" in result.stderr
    assert "Set MAILATTIC_COMMIT_BLOCKLIST" in result.stderr


def test_refusal_when_blocklist_file_unreadable(git_repo: Path, tmp_path: Path) -> None:
    """Refuse commit when the blocklist file cannot be decoded as UTF-8."""
    _stage_file(git_repo, "clean.txt", "hello\n")
    unreadable_file = tmp_path / "corrupt_blocklist.txt"
    # Invalid UTF-8 byte sequence
    unreadable_file.write_bytes(b"\x80\x81\xff\xfe")

    result = _run_guard(git_repo, blocklist_path=unreadable_file)

    assert result.returncode != 0
    assert "Commit blocklist file cannot be read" in result.stderr
    assert "Ensure the file is readable UTF-8 text" in result.stderr
    assert "Set MAILATTIC_COMMIT_BLOCKLIST" in result.stderr


def test_refusal_when_blocklist_has_zero_usable_lines(
    git_repo: Path, tmp_path: Path
) -> None:
    """Refuse commit when the blocklist contains only comments and blanks."""
    _stage_file(git_repo, "clean.txt", "hello\n")
    empty_blocklist = tmp_path / "empty_blocklist.txt"
    empty_blocklist.write_text(
        "# Header comment\n\n   # Another comment\n\n", encoding="utf-8"
    )

    result = _run_guard(git_repo, blocklist_path=empty_blocklist)

    assert result.returncode != 0
    assert "Commit blocklist file contains zero usable lines" in result.stderr
    assert (
        "Ensure the blocklist contains at least one non-empty, non-comment entry"
        in result.stderr
    )
    assert "Set MAILATTIC_COMMIT_BLOCKLIST" in result.stderr


def test_staged_clean_dirtied_working_tree_passes(
    git_repo: Path, tmp_path: Path
) -> None:
    """Passes when staged version is clean, even if working tree is dirty."""
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_text("blocked.example\n", encoding="utf-8")

    tracked_file = _stage_file(git_repo, "note.txt", "clean version\n")

    # Dirty working tree copy without staging
    tracked_file.write_text("clean version\nblocked.example\n", encoding="utf-8")

    result = _run_guard(git_repo, blocklist_path=blocklist)

    assert result.returncode == 0
    assert result.stderr == ""


def test_staged_dirty_cleaned_working_tree_refuses(
    git_repo: Path, tmp_path: Path
) -> None:
    """Refuses when staged content is dirty, even if working tree is clean."""
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_text("blocked.example\n", encoding="utf-8")

    tracked_file = _stage_file(git_repo, "note.txt", "dirty blocked.example\n")

    # Revert working tree to clean content without staging
    tracked_file.write_text("clean content\n", encoding="utf-8")

    result = _run_guard(git_repo, blocklist_path=blocklist)

    assert result.returncode != 0
    assert "matches blocklist line 1" in result.stderr


def test_output_never_prints_literal_and_prints_location(
    git_repo: Path, tmp_path: Path
) -> None:
    """Output reports path:line:blocklist_line and never reveals the literal."""
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_text(
        "# Header\n# Line 2 comment\nnobody@blocked.example\n",
        encoding="utf-8",
    )

    secret_literal = "nobody@blocked.example"
    domain_part = "blocked.example"

    content = f'def handle():\n    addr = "{secret_literal}"\n    return addr\n'
    _stage_file(git_repo, "handler.py", content)

    result = _run_guard(git_repo, blocklist_path=blocklist)

    assert result.returncode != 0
    # Location is accurately reported
    assert "handler.py:2: matches blocklist line 3" in result.stderr
    # The literal MUST NOT be printed in stdout or stderr
    assert secret_literal not in result.stdout
    assert secret_literal not in result.stderr
    assert domain_part not in result.stdout
    assert domain_part not in result.stderr


def test_refusal_when_staged_file_path_matches_blocklist(
    git_repo: Path, tmp_path: Path
) -> None:
    """Refuse commit when staged file path contains a blocked literal."""
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_text("blocked.example\n", encoding="utf-8")

    _stage_file(git_repo, "docs/blocked.example.txt", "clean content\n")

    result = _run_guard(git_repo, blocklist_path=blocklist)

    assert result.returncode != 0
    assert "file path matches blocklist line 1" in result.stderr
    expected_line = "docs/blocked.example.txt: file path matches blocklist line 1"
    assert expected_line in result.stderr


def test_case_insensitive_substring_matching(git_repo: Path, tmp_path: Path) -> None:
    """Matching is case-insensitive substring."""
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_text("Blocked.Domain.Example\n", encoding="utf-8")

    _stage_file(
        git_repo,
        "config.txt",
        "URL=https://sub.blocked.domain.example/api\n",
    )

    result = _run_guard(git_repo, blocklist_path=blocklist)

    assert result.returncode != 0
    assert "config.txt:1: matches blocklist line 1" in result.stderr


def test_deleted_lines_with_blocked_literal_are_not_refused(
    git_repo: Path, tmp_path: Path
) -> None:
    """Deleting a line containing a blocked literal is not refused."""
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_text("blocked.example\n", encoding="utf-8")

    file_path = _stage_file(
        git_repo,
        "data.txt",
        "line 1\nblocked.example\nline 3\n",
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit", "--no-verify"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    # Now remove the blocked line
    file_path.write_text("line 1\nline 3\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "data.txt"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    result = _run_guard(git_repo, blocklist_path=blocklist)

    assert result.returncode == 0
    assert result.stderr == ""


def test_pre_commit_config_hook_registered() -> None:
    """Hook is registered in .pre-commit-config.yaml with required properties."""
    content = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    local_repos = [r for r in content.get("repos", []) if r.get("repo") == "local"]
    assert local_repos, "No local repo found in .pre-commit-config.yaml"

    hooks = [h for r in local_repos for h in r.get("hooks", [])]
    hook_ids = {h["id"]: h for h in hooks}

    assert "check-commit-blocklist" in hook_ids
    hook = hook_ids["check-commit-blocklist"]
    assert hook.get("entry") == "uv run python scripts/check_commit_blocklist.py"
    assert hook.get("language") == "system"
    assert "pre-commit" in hook.get("stages", [])
    assert hook.get("always_run") is True
    assert hook.get("pass_filenames") is False


def test_quality_gates_doc_has_guard_section() -> None:
    """quality-gates.md documents the hook, the env var, and recovery steps."""
    doc_text = QUALITY_GATES_DOC.read_text(encoding="utf-8")
    assert "### Commit blocklist guard" in doc_text
    assert "MAILATTIC_COMMIT_BLOCKLIST" in doc_text
    assert "scripts/check_commit_blocklist.py" in doc_text


@pytest.mark.parametrize(
    "separator",
    ["\r", "\x0b", "\x0c", "\x1c", "\x85", " "],
    ids=["CR", "VT", "FF", "FS", "NEL", "LS"],
)
def test_literal_after_in_line_separator_is_still_refused(
    git_repo: Path, tmp_path: Path, separator: str
) -> None:
    """Only LF ends a diff line: other separators must not hide what follows."""
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_text("blocked.example\n", encoding="utf-8")
    target = git_repo / "mixed.txt"
    target.write_bytes(f"header{separator}host blocked.example\n".encode())
    subprocess.run(
        ["git", "add", "mixed.txt"], cwd=git_repo, check=True, capture_output=True
    )

    result = _run_guard(git_repo, blocklist_path=blocklist)

    assert result.returncode != 0
    assert "mixed.txt:1: matches blocklist line 1" in result.stderr


def test_blocklist_with_bom_keeps_its_first_entry(
    git_repo: Path, tmp_path: Path
) -> None:
    """A BOM written by an editor must not turn the first entry into a no-op."""
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_bytes(b"\xef\xbb\xbfblocked.example\n")
    _stage_file(git_repo, "notes.txt", "see blocked.example\n")

    result = _run_guard(git_repo, blocklist_path=blocklist)

    assert result.returncode != 0
    assert "notes.txt:1: matches blocklist line 1" in result.stderr


def test_external_diff_driver_does_not_bypass_the_check(
    git_repo: Path, tmp_path: Path
) -> None:
    """A configured diff.external replaces the patch text the check parses."""
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_text("blocked.example\n", encoding="utf-8")
    _stage_file(git_repo, "notes.txt", "see blocked.example\n")
    subprocess.run(
        ["git", "config", "diff.external", "true"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    result = _run_guard(git_repo, blocklist_path=blocklist)

    assert result.returncode != 0
    assert "notes.txt:1: matches blocklist line 1" in result.stderr

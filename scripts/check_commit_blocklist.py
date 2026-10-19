#!/usr/bin/env python3
"""Pre-commit check: refuse staged content or file paths matching a private blocklist.

See docs/agents/quality-gates.md for documentation and recovery steps.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_VAR_NAME = "MAILATTIC_COMMIT_BLOCKLIST"

HUNK_HEADER_RE = re.compile(r"^@@\s+-[0-9]+(?:,[0-9]+)?\s+\+([0-9]+)(?:,[0-9]+)?\s+@@")


class BlocklistError(Exception):
    """Base error for blocklist configuration and execution issues."""


@dataclass(frozen=True)
class BlocklistEntry:
    line_no: int
    pattern: str


@dataclass(frozen=True)
class PathViolation:
    path: str
    blocklist_line: int


@dataclass(frozen=True)
class ContentViolation:
    path: str
    file_line: int
    blocklist_line: int


def load_blocklist() -> list[BlocklistEntry]:
    """Load and parse the blocklist file specified by the environment variable.

    Returns a list of BlocklistEntry(line_no, pattern).
    Raises BlocklistError on missing env var, missing file, unreadable file,
    or file with zero usable lines.
    """
    raw_path = os.environ.get(ENV_VAR_NAME)
    if not raw_path or not raw_path.strip():
        raise BlocklistError(
            f"{ENV_VAR_NAME} environment variable is not set. "
            f"Set {ENV_VAR_NAME} to the path of the commit blocklist file."
        )

    path = Path(raw_path.strip())
    if not path.is_file():
        raise BlocklistError(
            f"Commit blocklist file does not exist: '{path}'. "
            f"Set {ENV_VAR_NAME} to a valid blocklist file path."
        )

    try:
        # utf-8-sig: a BOM left by an editor would otherwise glue itself to the
        # first entry and turn it into a literal that never matches.
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise BlocklistError(
            f"Commit blocklist file cannot be read: '{path}' ({exc}). "
            f"Ensure the file is readable UTF-8 text. "
            f"Set {ENV_VAR_NAME} to a readable UTF-8 blocklist file."
        ) from exc

    usable_entries: list[BlocklistEntry] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        usable_entries.append(BlocklistEntry(line_no=line_no, pattern=stripped.lower()))

    if not usable_entries:
        raise BlocklistError(
            f"Commit blocklist file contains zero usable lines: '{path}'. "
            "Ensure the blocklist contains at least one non-empty, non-comment entry. "
            f"Set {ENV_VAR_NAME} to a blocklist file with usable entries."
        )

    return usable_entries


def get_repo_root() -> Path:
    """Resolve the Git repository root."""
    res = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        raise BlocklistError(
            "Cannot resolve Git repository root (not a git repository?)"
        )
    return Path(res.stdout.strip()).resolve()


def get_staged_paths(repo_root: Path) -> list[str]:
    """Retrieve all staged file paths, excluding deleted files."""
    res = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=d",
            "-z",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if res.returncode != 0:
        raise BlocklistError(
            f"git diff --cached --name-only failed: {res.stderr.strip()}"
        )
    raw = res.stdout
    if not raw:
        return []
    return [p for p in raw.split("\0") if p]


def check_staged_paths(
    staged_paths: list[str], blocklist: list[BlocklistEntry]
) -> list[PathViolation]:
    """Check staged file names against the blocklist."""
    violations: list[PathViolation] = []
    seen: set[PathViolation] = set()

    for path in staged_paths:
        normalized_path = path.replace("\\", "/")
        path_lower = normalized_path.lower()
        for entry in blocklist:
            if entry.pattern in path_lower:
                violation = PathViolation(
                    path=normalized_path,
                    blocklist_line=entry.line_no,
                )
                if violation not in seen:
                    seen.add(violation)
                    violations.append(violation)
    return violations


def get_staged_diff(repo_root: Path) -> str:
    """Retrieve unified diff of staged changes."""
    res = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "diff",
            "--cached",
            "--no-color",
            # A configured external diff or textconv driver replaces the patch
            # text this script parses; with either active nothing would match.
            "--no-ext-diff",
            "--no-textconv",
            "-U0",
            "--src-prefix=a/",
            "--dst-prefix=b/",
        ],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        stderr = res.stderr.decode("utf-8", errors="replace").strip()
        raise BlocklistError(f"git diff --cached failed: {stderr}")
    # Bytes, not text mode: universal-newline decoding would turn a lone CR
    # inside a line into a line break.
    return res.stdout.decode("utf-8", errors="replace")


def check_staged_content(
    diff_text: str, blocklist: list[BlocklistEntry]
) -> list[ContentViolation]:
    """Scan added lines in staged unified diff against blocklist."""
    violations: list[ContentViolation] = []
    seen: set[ContentViolation] = set()
    current_file: str | None = None
    in_deleted_file: bool = False
    current_line_no: int = 0
    in_hunk: bool = False

    # Only LF ends a diff line. str.splitlines() also breaks on CR, FF, VT, FS,
    # NEL and U+2028, and the tail of such a line would lose its "+" prefix and
    # go unscanned.
    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            current_file = None
            in_deleted_file = False
            in_hunk = False
            continue

        if not in_hunk:
            if line.startswith("--- "):
                continue
            if line.startswith("+++ "):
                if line == "+++ /dev/null":
                    in_deleted_file = True
                    current_file = None
                elif line.startswith("+++ b/"):
                    current_file = line[len("+++ b/") :].strip()
                    in_deleted_file = False
                else:
                    current_file = line[4:].strip()
                    in_deleted_file = False
                continue

        if in_deleted_file:
            continue

        hunk_match = HUNK_HEADER_RE.match(line)
        if hunk_match:
            current_line_no = int(hunk_match.group(1))
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if line.startswith("+"):
            added_content = line[1:]
            added_content_lower = added_content.lower()
            for entry in blocklist:
                if entry.pattern in added_content_lower:
                    normalized_path = (current_file or "<unknown>").replace("\\", "/")
                    violation = ContentViolation(
                        path=normalized_path,
                        file_line=current_line_no,
                        blocklist_line=entry.line_no,
                    )
                    if violation not in seen:
                        seen.add(violation)
                        violations.append(violation)
            current_line_no += 1
        elif line.startswith("-"):
            # Old deleted line does not advance line number in new file
            pass
        elif line.startswith(" "):
            # Context line (if present)
            current_line_no += 1
        elif line.startswith("\\"):
            # e.g. \ No newline at end of file
            pass

    return violations


def main() -> int:
    """Pre-commit entrypoint."""
    try:
        blocklist = load_blocklist()
        repo_root = get_repo_root()
        staged_paths = get_staged_paths(repo_root)
        path_violations = check_staged_paths(staged_paths, blocklist)
        diff_text = get_staged_diff(repo_root)
        content_violations = check_staged_content(diff_text, blocklist)
    except BlocklistError as exc:
        print(f"[commit-blocklist] ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[commit-blocklist] UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 1

    if not path_violations and not content_violations:
        return 0

    print(
        "[commit-blocklist] Commit blocked: staged changes violate "
        "the commit blocklist:",
        file=sys.stderr,
    )
    for pv in path_violations:
        print(
            f"  {pv.path}: file path matches blocklist line {pv.blocklist_line}",
            file=sys.stderr,
        )
    for cv in content_violations:
        print(
            f"  {cv.path}:{cv.file_line}: matches blocklist line {cv.blocklist_line}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

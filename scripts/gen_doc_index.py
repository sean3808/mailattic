#!/usr/bin/env python3
"""
doc-index generator — marker-injection for docs/decisions.md (ADR index)
and docs/INDEX.md (docs index).

Usage:
    python scripts/gen_doc_index.py --target {adr,docs,all} [--check] [--repo-root PATH]
    python scripts/gen_doc_index.py --print-docs-dir [--repo-root PATH]

Target repo resolution (precedence, highest first):
    1. --repo-root PATH argument
    2. DOC_INDEX_REPO_ROOT environment variable
    3. Upward search from CWD for a .docindex.toml marker
    4. git rev-parse --show-toplevel from CWD
    (No rung resolves → exit non-zero)

Per-project config lives in <resolved_root>/.docindex.toml (optional).
When absent, defaults: docs_dir = "docs".
The generator source file is NOT project-specific — it is byte-identical
across all vendored copies.  All per-project values live in .docindex.toml.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Sentinel markers
# ---------------------------------------------------------------------------
MARKER_BEGIN = "<!-- BEGIN GENERATED {tag}-INDEX -->"
MARKER_END = "<!-- END GENERATED {tag}-INDEX -->"


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------
def _parse_frontmatter(text: str) -> dict[str, object]:
    """
    Parse the flat YAML-like frontmatter delimited by ``---`` fences.

    Supported field types
    ---------------------
    Scalars     : id, date, status, title, superseded_by, supersede_note
    List field  : supersedes  (block sequence  ``- item`` after the key line)

    Returns a dict; may raise ``ValueError`` on parse errors.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("frontmatter must start with ---")

    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("frontmatter closing --- not found") from None

    fm: dict[str, object] = {}
    i = 1
    while i < end:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # Check for key: value  OR  key:   (list follows)
        m = re.match(r"^(\w[\w_-]*):\s*(.*?)\s*$", stripped)
        if not m:
            raise ValueError(f"unparseable frontmatter line: {line!r}")
        key, value = m.group(1), m.group(2)

        if value == "":
            # might be a block-sequence list
            items: list[str] = []
            i += 1
            while i < end and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:].strip())
                i += 1
            fm[key] = items
        else:
            # strip optional inline brackets (list on one line)
            # not used in spec but defensive
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1]
                fm[key] = [
                    v.strip().strip("'\"") for v in inner.split(",") if v.strip()
                ]
            else:
                fm[key] = value
            i += 1

    return fm


def read_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    """
    Return ``(frontmatter_dict, body_text)`` where body_text is everything
    after the closing ``---`` fence (including the newline after it).

    Raises ``SystemExit`` (via _hard_error) on parse failure.
    """
    text = path.read_text(encoding="utf-8")
    try:
        fm = _parse_frontmatter(text)
    except ValueError as exc:
        _hard_error(f"{path}: malformed frontmatter — {exc}")

    lines = text.splitlines(keepends=True)
    try:
        fence_end = [line.rstrip("\n\r") for line in lines].index("---", 1) + 1
    except ValueError:
        _hard_error(f"{path}: frontmatter closing --- not found")
    body = "".join(lines[fence_end:])
    return fm, body


# ---------------------------------------------------------------------------
# Marker-injection helpers
# ---------------------------------------------------------------------------
def _markers(tag: str) -> tuple[str, str]:
    return MARKER_BEGIN.format(tag=tag), MARKER_END.format(tag=tag)


def _inject(file_text: str, tag: str, new_content: str) -> str:
    """
    Replace the text BETWEEN the marker pair with ``new_content``.
    The markers themselves are preserved.  ``new_content`` should NOT
    include the markers; a leading/trailing newline is normalised.
    """
    begin, end = _markers(tag)
    begin_idx = file_text.find(begin)
    end_idx = file_text.find(end)
    if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
        return None  # caller detects missing markers

    after_begin = begin_idx + len(begin)
    # Ensure we include the newline right after the begin marker
    block = "\n" + new_content.strip("\n") + "\n"
    return file_text[:after_begin] + block + file_text[end_idx:]


def _extract_region(file_text: str, tag: str) -> str | None:
    """Return the text between the marker pair (exclusive), or None if absent."""
    begin, end = _markers(tag)
    b = file_text.find(begin)
    e = file_text.find(end)
    if b == -1 or e == -1 or e < b:
        return None
    return file_text[b + len(begin) : e]


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------
def _hard_error(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Target repo resolution
# ---------------------------------------------------------------------------


def _search_upward_for_marker(start: Path) -> Path | None:
    """
    Walk from start upward; return the first directory that contains
    a .docindex.toml file, or None if none found.
    """
    for candidate in (start, *start.parents):
        if (candidate / ".docindex.toml").is_file():
            return candidate
    return None


def _git_toplevel(cwd: Path) -> Path | None:
    """
    Run git rev-parse --show-toplevel from cwd.
    Returns the Path, or None if not inside a git worktree or git not available.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=str(cwd),
        )
        if result.returncode == 0:
            toplevel = result.stdout.strip()
            if toplevel:
                return Path(toplevel)
    except (FileNotFoundError, OSError):
        pass
    return None


def _git_ignored(paths: list[Path], cwd: Path) -> set[Path]:
    """
    Return the subset of paths that git ignores (local-only working notes).
    Returns an empty set when git is unavailable or cwd is not a worktree.
    """
    if not paths:
        return set()
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            input="\0".join(str(p) for p in paths),
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(cwd),
        )
    except (FileNotFoundError, OSError):
        return set()
    # exit 1 = nothing ignored; 128 = not a worktree
    if result.returncode != 0:
        return set()
    return {Path(p) for p in result.stdout.split("\0") if p}


def resolve_repo_root(
    repo_root_arg: str | None = None,
    env_var: str | None = None,
    cwd: Path | None = None,
) -> Path:
    """
    Resolve the target repo root using the precedence chain:
      1. repo_root_arg (--repo-root argument)
      2. env_var (DOC_INDEX_REPO_ROOT)
      3. Upward search from cwd for .docindex.toml
      4. git rev-parse --show-toplevel from cwd

    Exits non-zero (fail-loud) if no rung resolves.

    The generator's own file location (__file__) is NEVER used.
    """
    if cwd is None:
        cwd = Path.cwd()

    # Rung 1: explicit --repo-root argument
    if repo_root_arg is not None:
        return Path(repo_root_arg).resolve()

    # Rung 2: DOC_INDEX_REPO_ROOT environment variable
    if env_var is not None:
        return Path(env_var).resolve()

    # Rung 3: upward search for .docindex.toml from CWD
    marker_root = _search_upward_for_marker(cwd)
    if marker_root is not None:
        return marker_root

    # Rung 4: git rev-parse --show-toplevel from CWD
    git_root = _git_toplevel(cwd)
    if git_root is not None:
        return git_root

    _hard_error(
        "Cannot resolve target repo root: no --repo-root argument, "
        "no DOC_INDEX_REPO_ROOT env var, no .docindex.toml found upward from CWD, "
        "and CWD is not inside a git worktree. "
        "Use --repo-root PATH to specify the target repo explicitly."
    )


# ---------------------------------------------------------------------------
# .docindex.toml config reader
# ---------------------------------------------------------------------------

_DEFAULT_DOCS_DIR = "docs"


def read_docindex_config(repo_root: Path) -> dict[str, object]:
    """
    Read .docindex.toml at repo_root if present; return config dict.
    Falls back to documented defaults when the marker is absent.

    Documented defaults:
      docs_dir = "docs"

    The generator reads per-project config ONLY from the resolved root.
    The current working directory is NOT scanned for config once a root is
    determined (the CWD marker was already consumed in root resolution if
    relevant, or the explicit root was given via --repo-root / env).
    """
    marker_path = repo_root / ".docindex.toml"
    config: dict[str, object] = {
        "docs_dir": _DEFAULT_DOCS_DIR,
    }
    if marker_path.is_file():
        try:
            raw = marker_path.read_bytes()
            parsed = tomllib.loads(raw.decode("utf-8"))
        except Exception as exc:
            _hard_error(f"{marker_path}: failed to parse .docindex.toml — {exc}")
        # Read [doc_index] section
        section = parsed.get("doc_index", {})
        if "docs_dir" in section:
            config["docs_dir"] = str(section["docs_dir"])
    return config


# ---------------------------------------------------------------------------
# Validation helpers (fail-loud)
# ---------------------------------------------------------------------------


def _validate_docs_dir(docs_dir_str: str, repo_root: Path) -> None:
    """
    Fail loud if docs_dir_str is unsafe (absolute path or escapes repo_root).

    Accepts a relative path that resolves strictly inside repo_root.
    """
    if Path(docs_dir_str).is_absolute():
        _hard_error(
            f"docs_dir {docs_dir_str!r} is an absolute path; "
            "it must be a relative path inside the project."
        )
    resolved = (repo_root / docs_dir_str).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        _hard_error(
            f"docs_dir {docs_dir_str!r} resolves to {resolved} "
            f"which is outside the project root {repo_root}."
        )


def _validate_resolved_root(repo_root: Path) -> None:
    """
    Fail loud if the resolved root is neither a git worktree nor marked
    by a .docindex.toml.
    """
    is_git = (repo_root / ".git").exists()
    is_marked = (repo_root / ".docindex.toml").is_file()
    if not is_git and not is_marked:
        _hard_error(
            f"Resolved root {repo_root!r} is neither a git worktree "
            "(no .git) nor marked by a .docindex.toml file. "
            "Ensure --repo-root points at a valid project root."
        )


def _validate_docs_structure(docs_dir: Path) -> None:
    """Fail loud if the docs directory or its decisions/ subdir is missing."""
    if not docs_dir.is_dir():
        _hard_error(
            f"Resolved docs directory does not exist: {docs_dir}. "
            "Ensure the docs directory is present, or configure 'docs_dir' "
            "in .docindex.toml."
        )
    decisions_dir = docs_dir / "decisions"
    if not decisions_dir.is_dir():
        _hard_error(
            f"decisions/ subdirectory missing under docs dir {docs_dir}. "
            "Create the directory or configure 'docs_dir' in .docindex.toml."
        )


# ---------------------------------------------------------------------------
# ADR index generation
# ---------------------------------------------------------------------------
ADR_ID_RE = re.compile(r"^D-(\d{8})-(\d+)$")

REQUIRED_ADR_FIELDS = {"id", "date", "status", "title"}
VALID_STATUSES = {"accepted", "superseded", "deprecated"}


def _parse_adr_id(id_str: str) -> tuple[str, int]:
    m = ADR_ID_RE.match(id_str)
    if not m:
        raise ValueError(f"id does not match D-YYYYMMDD-N: {id_str!r}")
    return m.group(1), int(m.group(2))


def _read_adr(path: Path) -> dict[str, object]:
    """Read and validate a single ADR file; hard-error on any issue."""
    fm, _ = read_frontmatter(path)

    missing = REQUIRED_ADR_FIELDS - fm.keys()
    if missing:
        _hard_error(
            f"{path}: missing required frontmatter field(s): "
            f"{', '.join(sorted(missing))}"
        )

    status = fm["status"]
    if status not in VALID_STATUSES:
        _hard_error(
            f"{path}: invalid status {status!r}; must be one of "
            f"{sorted(VALID_STATUSES)}"
        )

    id_str = fm["id"]
    try:
        _parse_adr_id(str(id_str))
    except ValueError as exc:
        _hard_error(f"{path}: {exc}")

    return fm


def _render_adr_row(fm: dict[str, object], path: Path, link_base: Path) -> str:
    """
    Render one Markdown table row for an ADR entry.

    ``path`` is the ADR file this entry was read from (already known by the
    caller), used to build the link relative to ``link_base`` (the directory
    that contains the index file, i.e. docs/).

    Dead   : status is ``superseded`` or ``deprecated``
    Partial: status is ``accepted`` AND superseded_by + supersede_note present
    Live   : everything else
    """
    id_str = str(fm["id"])
    title = str(fm["title"])
    date = str(fm["date"])
    status = str(fm["status"])
    suped_by = str(fm.get("superseded_by", ""))
    suped_note = str(fm.get("supersede_note", ""))

    rel = path.relative_to(link_base).as_posix()
    link = f"[{id_str}]({rel})"

    dead = status in ("superseded", "deprecated")

    if dead:
        # Strike-through title
        display = f"~~{title}~~"
        status_col = f"~~{status}~~"
        annotation = ""
    elif status == "accepted" and suped_by and suped_note:
        display = title
        status_col = status
        annotation = f" *(partially superseded by `{suped_by}`: {suped_note})*"
    else:
        display = title
        status_col = status
        annotation = ""

    return f"| {link} | {date} | {display}{annotation} | {status_col} |"


def generate_adr_index(decisions_dir: Path, link_base: Path) -> str:
    """
    Return the generated ADR index block (the lines that go BETWEEN the markers).
    Does NOT write any file.

    ``link_base`` is the directory that contains the index file (docs/), so
    generated links are relative to the index file's own directory.
    """
    adr_files = sorted(decisions_dir.glob("*.md"))
    entries: list[tuple[tuple[str, int], dict[str, object], Path]] = []
    for f in adr_files:
        fm = _read_adr(f)
        sort_key = _parse_adr_id(str(fm["id"]))
        entries.append((sort_key, fm, f))

    entries.sort(key=lambda x: x[0])

    header = "| ID | Date | Title | Status |\n| --- | --- | --- | --- |"
    rows = [_render_adr_row(fm, path, link_base) for _, fm, path in entries]
    return header + "\n" + "\n".join(rows)


# ---------------------------------------------------------------------------
# Docs index generation
# ---------------------------------------------------------------------------
EXCLUDE_DIRS = {"decisions"}  # relative to docs/
EXCLUDE_FILES = {"decisions.md", "INDEX.md"}  # relative to docs/


def _description_from_file(path: Path) -> str:
    """
    Derive a description from a markdown file using the precedence:
    1. frontmatter ``description`` field
    2. first level-1 heading (# …)
    3. first non-empty line
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""

    lines = text.splitlines()

    # Check for frontmatter
    if lines and lines[0].strip() == "---":
        try:
            fence = [line.rstrip("\n\r") for line in lines].index("---", 1)
        except ValueError:
            fence = -1

        if fence > 0:
            fm_lines = lines[1:fence]
            for fl in fm_lines:
                m = re.match(r"^description:\s*(.+)\s*$", fl.strip())
                if m:
                    return m.group(1).strip()
            # No description in frontmatter; search body below
            body_lines = lines[fence + 1 :]
        else:
            body_lines = lines
    else:
        body_lines = lines

    # First H1 in body
    for bl in body_lines:
        m = re.match(r"^#\s+(.+)", bl)
        if m:
            return m.group(1).strip()

    # First non-empty line in body
    for bl in body_lines:
        stripped = bl.strip()
        if stripped:
            return stripped

    return ""


def generate_docs_index(docs_dir: Path, link_base: Path) -> str:
    """
    Return the generated docs index block (lines between the markers).

    ``link_base`` is the directory that contains the index file (docs/), so
    generated links are relative to the index file's own directory.
    """
    all_md: list[Path] = []
    for p in sorted(docs_dir.rglob("*.md")):
        rel = p.relative_to(docs_dir)
        parts = rel.parts
        # Exclude decisions/ subtree
        if parts[0] == "decisions":
            continue
        # Exclude the two generated index files
        if len(parts) == 1 and parts[0] in EXCLUDE_FILES:
            continue
        all_md.append(p)
    # Local working files are git-ignored and absent from a fresh clone; skip them.
    ignored = _git_ignored(all_md, docs_dir)
    all_md = [p for p in all_md if p not in ignored]

    header = "| File | Description |\n| --- | --- |"
    rows: list[str] = []
    for p in all_md:
        rel_link = p.relative_to(link_base).as_posix()
        desc = _description_from_file(p)
        rows.append(f"| [{p.name}]({rel_link}) | {desc} |")

    return header + "\n" + "\n".join(rows)


# ---------------------------------------------------------------------------
# Core write / check logic
# ---------------------------------------------------------------------------
def _process_target(
    tag: str,
    target_file: Path,
    new_content: str,
    check_mode: bool,
) -> bool:
    """
    Inject (or check) the new_content into target_file between the markers.

    Returns True if the file is fresh (or was just written); False if stale
    and check_mode is True (caller should exit non-zero).
    """
    file_text = target_file.read_text(encoding="utf-8")
    begin, end = _markers(tag)
    if begin not in file_text or end not in file_text:
        _hard_error(
            f"{target_file}: missing marker pair "
            f"{begin!r} / {end!r} — cannot generate index"
        )

    injected = _inject(file_text, tag, new_content)
    if injected is None:
        _hard_error(f"{target_file}: marker pair is malformed or reversed")

    if check_mode:
        existing_region = _extract_region(file_text, tag)
        new_region = _extract_region(injected, tag)
        if existing_region == new_region:
            return True
        else:
            print(
                f"STALE: {target_file} — index is out of date, "
                f"run without --check to regenerate",
                file=sys.stderr,
            )
            return False
    else:
        target_file.write_text(injected, encoding="utf-8")
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate doc indexes via marker-injection."
    )
    parser.add_argument(
        "--target",
        choices=["adr", "docs", "all"],
        default=None,
        help="Which index to regenerate: adr, docs, or all",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check freshness only; exit non-zero if stale; write nothing",
    )
    parser.add_argument(
        "--repo-root",
        dest="repo_root",
        default=None,
        help=(
            "Explicit path to the target repo root. "
            "When supplied, takes precedence over DOC_INDEX_REPO_ROOT and "
            "any .docindex.toml found upward from CWD."
        ),
    )
    parser.add_argument(
        "--print-docs-dir",
        action="store_true",
        dest="print_docs_dir",
        help=(
            "Print the resolved docs directory name to stdout and exit 0. "
            "Does not generate or check any index files. "
            "Used by the pre-commit hook to obtain docs_dir without awk parsing."
        ),
    )
    args = parser.parse_args(argv)

    # ---- Resolve target repo root (precedence chain, fail-loud) ----
    env_root = os.environ.get("DOC_INDEX_REPO_ROOT")
    repo_root = resolve_repo_root(
        repo_root_arg=args.repo_root,
        env_var=env_root,
        cwd=Path.cwd(),
    )

    # ---- --print-docs-dir mode: resolve config and print, then exit ----
    if args.print_docs_dir:
        config = read_docindex_config(repo_root)
        # Validate before printing: the pre-commit hook consumes this value to
        # build the index paths it guards, so an unsafe docs_dir (absolute or
        # escaping repo_root) must fail loud here too, not leak to the hook.
        _validate_docs_dir(str(config["docs_dir"]), repo_root)
        print(config["docs_dir"])
        return 0

    # ---- Normal mode requires --target ----
    if args.target is None:
        parser.error("--target is required (choose from: adr, docs, all)")

    # ---- Validate root ----
    _validate_resolved_root(repo_root)

    # ---- Read per-project config from resolved root ----
    config = read_docindex_config(repo_root)

    # ---- Validate docs_dir is a safe relative path inside repo_root ----
    _validate_docs_dir(str(config["docs_dir"]), repo_root)

    docs_dir = repo_root / config["docs_dir"]

    # ---- Validate docs structure ----
    _validate_docs_structure(docs_dir)

    decisions_dir = docs_dir / "decisions"
    adr_index_file = docs_dir / "decisions.md"
    docs_index_file = docs_dir / "INDEX.md"

    all_ok = True

    if args.target in ("adr", "all"):
        content = generate_adr_index(decisions_dir, docs_dir)
        ok = _process_target("ADR", adr_index_file, content, args.check)
        if not ok:
            all_ok = False

    if args.target in ("docs", "all"):
        content = generate_docs_index(docs_dir, docs_dir)
        ok = _process_target("DOCS", docs_index_file, content, args.check)
        if not ok:
            all_ok = False

    if not all_ok:
        # Already printed the stale messages
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

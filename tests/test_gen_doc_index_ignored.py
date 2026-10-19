from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gen_doc_index import generate_docs_index  # noqa: E402


def _repo_with_docs(tmp_path: Path, names: list[str], ignored: str) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(f"docs/{ignored}\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in names:
        (docs / name).write_text(f"# {name}\n\n{name} body\n", encoding="utf-8")
    return docs


# The ignored note sits first, in the middle, and last in sorted order, so a
# filter that only drops some positions of the `check-ignore` stream shows up.
@pytest.mark.parametrize("ignored", ["a-local.md", "m-local.md", "z-local.md"])
def test_git_ignored_note_is_left_out_of_docs_index(
    tmp_path: Path, ignored: str
) -> None:
    names = sorted({"b.md", "n.md", ignored})
    docs = _repo_with_docs(tmp_path, names, ignored)

    index = generate_docs_index(docs, docs)

    assert ignored not in index
    for name in names:
        if name != ignored:
            assert f"[{name}]({name})" in index

"""The README quick start must query the database it just created (#293)."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("readme", ["README.md", "README.zh-TW.md"])
def test_quickstart_bes_commands_all_target_the_same_database(readme: str) -> None:
    text = (REPO_ROOT / readme).read_text(encoding="utf-8")
    block = text.split("```powershell", 1)[1].split("```", 1)[0]
    bes_lines = [line for line in block.splitlines() if line.startswith("uv run bes ")]

    assert len(bes_lines) >= 4
    # Without --database a command falls back to an absolute default path that
    # only exists on the maintainer's machine.
    assert all(r"--database .\mailattic.db" in line for line in bes_lines), bes_lines

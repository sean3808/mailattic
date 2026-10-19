"""AGENTS.md 的架構地圖必須與 src/ 的模組清單一致。

地圖是每個 agent 開工時找檔的起點；新增或改名模組而沒登記，agent 只會多花時間找，
不會有任何東西報錯。這支測試讓那件事變紅。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "src" / "mailattic"
NOT_ON_THE_MAP = {"__init__.py"}


def _map_section() -> str:
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    start = text.index("## Architecture map")
    return text[start : text.index("\n## ", start + 1)]


def test_architecture_map_lists_exactly_the_package_modules():
    on_disk = {p.name for p in PACKAGE.glob("*.py")} - NOT_ON_THE_MAP
    # 只認「模組」欄：說明欄順口提到的檔名不算登記。
    rows = [line for line in _map_section().splitlines() if line.startswith("| ")]
    module_cells = [row.split("|")[2] for row in rows]
    on_map = set(re.findall(r"`(\w+\.py)`", "\n".join(module_cells)))

    assert on_disk - on_map == set(), "src/ 有、架構地圖沒列的模組"
    assert on_map - on_disk == set(), "架構地圖列了、src/ 已不存在的模組"

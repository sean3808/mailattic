---
id: D-20260821-2
date: 2026-08-21
status: accepted
title: -RuntimeRoot 覆寫不搬 pytest cache —— 記為已知邊界而非修復
---

## Context

2026-08-21 把 pytest 與 ruff 的 cache 收進 `.project-runtime/`（`e95aa57`），補完 2026-07 收納工作漏掉的最後兩個 repo 根目錄。做法是兩行設定：`pyproject.toml` 的 `[tool.pytest.ini_options] cache_dir` 與 `ruff.toml` 的 `cache-dir`（ruff 的必須寫在 `ruff.toml`——本 repo 有獨立 `ruff.toml` 時 ruff 完全忽略 `pyproject.toml` 的 `[tool.ruff]`）。

同一輪的 `/codex-review` 指出一個不對稱：`scripts/invoke-pytest.ps1` 在 `-RuntimeRoot` 覆寫時只 append `--basetemp`，而 `pytest-cache` 不在 `scripts/project-tool-environment.ps1` 的七個 managed leaf 內。因此 override 時 basetemp 搬走、cache 仍留在 `<repo>/.project-runtime/pytest-cache`。

三個減輕的事實：

1. **不是回歸。** 這次改動之前 `.pytest_cache/` 位在 repo 根，同樣不隨 override 搬。改動把它移進 runtime root 底下，並沒有讓 override 的行為變差。
2. **`-RuntimeRoot` 的動機情境當天稍早才退役。** 它的 docstring 寫明用途是「for a future checkout whose repository volume is unsuitable」——那正是 [D-20260821-1](D-20260821-1-retire-system-drive-hardware-guard.md) 判定在現行硬體上不再重現的處境。今天實際走這條路的只有它自己的契約測試。
3. **同一個檔已有同型先例。** `invoke-pytest.ps1` 的 docstring 早就把「不同 `-RuntimeRoot` 的兩次啟動各持不同的鎖，故不可並行」記為已知邊界而非缺陷。

本決定的來歷值得記下：**問題由外部語意審查提出、Sean 拍板選擇不修**。這類「審查說該修、人判斷不修」的取捨最需要留下紀錄，否則下一輪審查會重提同一條，而讀者無從得知它已被裁決過。

## Decision

**記為已知邊界，不修。** `scripts/invoke-pytest.ps1` 的 `.PARAMETER RuntimeRoot` 段落寫明三件事：cache 不隨 override 搬、不修的理由（情境已退役 + cache 是可重建狀態不是證據）、以及情境回來時的補法。

判準是 cache 的性質：它是**可重建狀態，不是證據**。留在 repo 內不會讓任何完工證據失真，最壞情況只是下一次跑測試慢一點。

## Rejected alternatives

- **照原設計修好**——在 `project-tool-environment.ps1` 宣告第八個 managed leaf `pytest-cache`、在 launcher 的 override branch 於 basetemp 旁 append `-o cache_dir=<該 leaf>`、並擴充 `test_project_tool_environment.py` 的 leaf 互斥契約測試。這正是 parked change `consolidate-tool-caches-under-project-runtime` 的 tasks 1.2 + 2.2（該 change 已於 2026-08-21 放棄，內容改為直接 commit）。否決理由：為一個當天才判定不再存在的處境加防護。
- **把 `cache_dir` 指到既有的 `pytest` leaf 底下**——那個 leaf 是 basetemp，每輪清空，cache 會連同被毀，等於變相關閉 cache。
- **維持 `.pytest_cache/` 在 repo 根不動**——直接違反 `.project-runtime` 的第一意圖（repo 根目錄可讀性，見 [`GLOSSARY.md`](../../GLOSSARY.md) 的 `runtime root（.project-runtime）` 詞條）。這條收納正是本輪要完成的事。

## Restoration condition

**這是「情境已退役」觸發的不修，不是「這個對稱從一開始就不必要」的結論。** 哪天 repo 所在的磁碟區又變得不適合跑測試 I/O（`-RuntimeRoot` 的原始用途回來），這個邊界要**重新評估**。重新評估的起點是 `invoke-pytest.ps1` 的 `.PARAMETER RuntimeRoot` 段——補法已經寫在那裡，不必重推一遍。

## Consequences

- `docs/current-status.md` 的 pytest gate 那一列由「僅剩一個邊界」改為列出兩條，並帶上不修的理由與補法所在（`90e4980`）。
- 反轉成本集中在三處：`scripts/project-tool-environment.ps1` 的 leaf 清單、`scripts/invoke-pytest.ps1` 的 override branch、`tests/test_project_tool_environment.py` 的 leaf 互斥契約測試。
- `docs/decisions.md` 由既有機制重生，不手動編輯。

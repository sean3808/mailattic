---
id: D-20260820-4
date: 2026-08-20
status: accepted
title: skill→SSOT 自動部署的觸發改為「main commit + 內容不一致」
---

## Context

[D-20260602-2](D-20260602-2-skill-dev-canonical-in-repo.md) 把 skill 的 dev-canonical 定在本 repo、SSOT（另一個 repo）定為下游 release copy，並以 **archive commit** 作為部署時點 —— archive 是 Spectra 鏈的「change 完成」訊號，用它保證 SSOT 只拿到完成品。

[D-20260820-3](D-20260820-3-migrate-to-contract-anchored-flowback.md) 退出 Spectra 鏈後，archive commit 不再發生（parked 的 `consolidate-tool-caches` 是最後一發）。`scripts/deploy-skill-to-ssot.py` 的 gate 1 寫死認 `openspec/changes/archive/` 路徑，**部署自此靜默失效** —— 沒有錯誤、沒有警告，其他專案的 subagent 只會逐漸拿到過期 skill。這是遷移的漏網（issue #172），也是 PC-156「機構產物失聯」的又一例：觸發器死了沒有任何東西會發現。

## Decision

gate 1 從「HEAD commit 觸及 archive 路徑」改為「**當前分支是 `main`**」；gate 2（repo skill 與 SSOT 的遞迴內容比對）不變。語意：**SSOT 跟隨 main 的 skill 內容** —— 新流程下 main 上的 commit 就是完成品（S 直接進 main、M/L squash merge 進 main），「完成時點」的訊號從 archive 移到 main 本身。

**gate 1.5（round-2 review 補上）**：skill 目錄的 working tree 必須乾淨才部署 —— 部署源是磁碟目錄（可變），不是 HEAD 快照；skill 目錄有未 commit 改動時，任何無關 commit 都會把半成品掃出去。dirty → 跳過，等 WIP 自己 commit 後的下一次 main commit 自癒。

**部署與比對邊界 = `git ls-files` 的 tracked 清單（round-3 review 補上）**：porcelain 看不見 ignored 檔，整目錄 `copytree` 卻會把 `__pycache__` / `.env` 這類未提交產物甚至秘密搬進 SSOT。改為逐檔比對與複製 tracked 清單內的檔案 —— 清單外的東西不比對、不搬運、不觸發部署；SSOT 側清單外的既有檔案也不因此被誤判為差異。

順帶修掉舊實作的盲點：原 diff-tree 只看 HEAD 一個 commit，ff 合併多個 commit 時中段的 skill 變更會漏偵測；改為內容比對後不存在這個問題，漏過的部署會在下一次 main commit 自癒。

## Rejected alternatives

- **改偵測 `skills/mailattic/` 路徑（任何分支）**：feature branch 的 WIP commit 會把半成品部署出去 —— archive 觸發原本擋住的正是這個，分支 gate 以更便宜的方式保留同一保護。
- **每次 commit 無條件部署（不設 gate 1）**：同上，且 detached HEAD／worktree 情境行為不明。
- **手動命令**：回到「靠人記得」，正是 PC-156 要消滅的形狀。

## Consequences

- D-20260602-2 的 dev-canonical 方向（repo → SSOT）與「hook 只 cp 不 commit」**不變**，被取代的只有觸發子句。
- main 上任何 commit 都會跑一次 skill 目錄的內容比對（十餘個小檔、shallow=False），成本毫秒級。
- SSOT 的 serve 即時性提升：skill 改動合進 main 當下即部署，不再等 change 收尾。
- 若未來出現「skill 內容已合進 main 但暫不想 release 給其他專案」的需求，這個設計沒有擋板 —— 屆時要嘛回到顯式 release 時點，要嘛在 SSOT 側加版本控制，開新 ADR 決定。

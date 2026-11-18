---
id: D-20261118-1
date: 2026-11-18
status: accepted
title: 發行改認 HEAD 的 hosted-full，不再重跑本機 Full
---

## Context

`main` 的每次 push 都會透過 `.github/workflows/ci.yml` 的 `push: branches: [main]` 執行 `hosted-full`（Windows runner 跑 canonical launcher `-Route Full`），而 `scripts/release.ps1` 本來就要求在乾淨 `main` 上針對 `HEAD` 發行。[D-20261113-1](D-20261113-1-hosted-windows-full-is-merge-evidence.md) 當初將合併證據移至 `hosted-full` 時保留了「release 證據不變」（仍於本機重跑 `-Route Full`），理由是 `hosted-full` 只證明該次執行的 commit；但發行的對象正是 `main` 上已測過的同一個 `HEAD` commit，本機再跑一次約 5–6 分鐘的全套測試屬於重複勞動。

此外，開發目錄中有兩個被 `.gitignore` 忽略的本機限定收件人重建測試檔（`tests/test_recipient_rebuild_audit.py` 與 `tests/test_recipient_rebuild_apply.py`），CI 跑不到，發行前仍須於本機確認。

Sean 於 2026-11-18 在 issue #134 逐題裁定（原文）：

- Q1「發行時改成『main HEAD 這棵樹已有綠的 hosted-full 就不跑本機 Full；查不到才退回跑本機 Full』，並寫一條新 ADR 取代 D-20261113-1 那句『release 證據不變』」→ **yes**
- Q2「發行時固定用 Targeted 跑那 2 個本機限定的收件人重建測試檔（49 項），每次都跑」→ **ok**
- Q3「發行當下 main HEAD 的 hosted-full 還沒跑完，就直接退回跑本機 Full、不等；hosted-full 失敗則一律停下、不發行」→ **ok**

## Decision

`scripts/release.ps1` 第 2 階段改為查詢 `HEAD` 在 `ci.yml` 的最新 `hosted-full` job 狀態，並一律執行本機限定測試：

1. **`hosted-full` 已成功（`completed` + `success`）**：跳過本機 `-Route Full`，於 release 輸出印出採用的 run URL。
2. **還在跑（`in progress`）、排隊中（`queued`）或查不到（`not found`）**：不等待，於 release 輸出印出 `falling back to local Full` 與原因後，退回執行本機 `-Route Full`。
3. **失敗（`failure`）或被取消（`cancelled`）**：立即丟出錯誤並印出該 run URL 停下，不執行本機測試、不重裝、不寫入 release evidence、不打 tag、不 push。
4. **本機限定收件人重建測試**：無論走哪一條路徑，皆以 `-Route Targeted tests/test_recipient_rebuild_audit.py tests/test_recipient_rebuild_apply.py` 執行；任一失敗即停下。

`.git/bes-release-state.json`（schema version 1）與 `scripts/guard-release-tag.py` 維持不變，不額外寫入持久化欄位，決策結果與採用的 run URL 直接呈現於 release 輸出（見 #134 裁定說明）。

取代 [D-20261113-1](D-20261113-1-hosted-windows-full-is-merge-evidence.md) 中「release 證據不變」一句。

## Rejected alternatives

- **繼續每次發行都在本機重跑 `-Route Full`**：與 `main` push 的 `hosted-full` 重複，浪費約 5–6 分鐘本機時間與電量。
- **當 `hosted-full` 還在跑或排隊時輪詢等待**：增加發行腳本複雜度與等待時間；直接退回本機 `-Route Full` 即可確保立即驗證。
- **將採用的 `hosted-full` run URL 寫入 `.git/bes-release-state.json`**：需修改 `guard-release-tag.py` 的 schema 檢查且無持久化必要；改以 release 輸出紀錄即可。

## Consequences

- `main` `HEAD` 已有綠燈 `hosted-full` 時，發行只需跑 `-Route Targeted` 的兩個本機限定測試檔，大幅縮短發行耗時。
- `hosted-full` 失敗或被取消時，發行會在測試階段即被擋下，不會誤將未通過 CI 的版本發行出去。
- 本機限定、未進版控的 `test_recipient_rebuild_*.py` 仍於每次發行前受 `-Route Targeted` 保護。

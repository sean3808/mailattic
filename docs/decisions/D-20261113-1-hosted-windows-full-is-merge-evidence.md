---
id: D-20261113-1
date: 2026-11-13
status: accepted
title: GitHub hosted Windows Full 作為合併證據
---

## Context

Sean 要把測試負載移出開發用筆電。[D-20260820-2](D-20260820-2-excluded-ci-infrastructure-options.md) 排除 hosted Full 與必要檢查時，repo 還是私有的（Sean 於 2026-11-13 確認）。[D-20261010-1](D-20261010-1-showcase-ships-ahead-of-remoteization-in-a-fresh-repo.md) 決定以公開 repo 為主場；前提改變，並非當初判斷錯誤。

## Decision

以 PR 的 GitHub-hosted `windows-latest`、canonical launcher `-Route Full` 綠燈作為合併證據。非 draft PR 與 main push 執行 `hosted-full`，保留排程與手動執行；本機 Full 仍是開發檢查及備援，release 證據不變。workflow 合併並在 PR 實際回報後，將 `hosted-full` 設為 main 必要檢查。

D-20260820-2 理由 (1) 費用與 (2) 必要檢查因公開 repo 的前提改變而不再適用；理由 (3) 不在開發機自架 runner 仍成立，同一台機器沒有宿主差異。

## Evidence（2026-11-13）

- [GitHub Actions 計費文件](https://docs.github.com/en/billing/concepts/product-billing/github-actions) 說標準 GitHub-hosted runner 在公開 repositories 免費；該句沒有列作業系統，`windows-latest` 是標準 GitHub-hosted runner。
- 唯讀查詢 `gh api repos/sean3808/mailattic/branches/main/protection` 得 HTTP 404「Branch not protected」，不是 403 升級限制；`gh api repos/sean3808/mailattic/rulesets` 得 `[]`，repo `visibility=public`。必要檢查實際可否設定，須等設定成功才證明。
- Sean 當面確認照此執行，包含設定必要狀態檢查，並確認原決定時 repo 為私有；記錄於 #70 的 2026-11-13 triage 留言。

## Rejected alternatives

- 維持本機 Full 作為合併證據：無法把測試負載移出筆電。
- 在開發機自架 runner：沒有不同宿主的訊號，還增加維護負擔。

## Consequences

- hosted Full 驗證該次執行的 commit，不證明後續 commit 或 release；其他 CI 訊號的界線見 [quality-gates.md](../agents/quality-gates.md)。
- Draft PR 不自動執行；需要在 ready 前驗證時，對其 head ref 手動 dispatch。
- 必要檢查只能在 workflow 已合併且 PR 已回報後設定，避免阻擋本次變更的 PR。closeout 整合由 #73 處理。

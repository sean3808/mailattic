---
id: D-20260820-2
date: 2026-08-20
status: accepted
title: 三個 CI 基礎設施選項明文排除，「Free 方案」是所有 CI 決策的固定前提
---

> **2026-10-03 註記**：`/session-merge-queue` 已退役（`#207`），三個排除選項的決定不變。文中「四道 gate 由該 session 自己執行」改讀為：合併前的把關是 `/closeout` 加上 Sean 的合併授權。

## Context

這個 repo 的 CI 討論反覆撞到同一批限制，而每次都有人（人或 agent）重新提案同樣的三個解法。它們在 2026-08-11 至 08-14 之間陸續被 Sean 逐一否決，但**否決只活在對話與交接檔裡，沒有正典家** —— 搜過 `docs/decisions/`、`docs/current-status.md`、`CLAUDE.md`，三者一個字都沒有。

沒有正典的否決會被重提。這份 ADR 存在的唯一目的，是讓下一個看到 CI 限制的人先讀到「這三條走過了」，而不是再走一次。

被反覆撞到的限制本身是方案層級的事實：GitHub Free 的私有 repo 沒有 branch protection（`branches/main/protection` 回 403，這是實測而非推斷）、每月 2,000 分鐘額度、Windows runner 2 倍計費（實際約 1,000 Windows 分鐘/月）。

## Decision

**以下三個選項明文排除，不得在沒有新事實的情況下重新提案。**

### 1. 不買 GitHub Pro

2026-08-14 決定不買。

Pro 解的是 #150 那類需要 required status check 的問題；**它解不了 #156 的 Windows 分鐘量級** —— 那是額度問題，換方案不會讓 Full route 變便宜。所以「買 Pro 就好了」只覆蓋兩個痛點裡的一個。

### 2. 轉公開走另建新 repo 的路

轉公開能一次解掉額度與 branch protection 兩件事，但**當時這個 repo 轉不了**：重寫歷史會改寫所有 commit SHA，而彼時有多張 PR 與多個 worktree 掛在現有歷史上；issue / PR comment 的修訂歷史也無法由 API 刪除。

2026-08-14 拍板：**歷史重寫另案**。

後續的解法不是洗歷史，而是[另建全新的公開 repo、舊歷史不帶](D-20261010-1-showcase-ships-ahead-of-remoteization-in-a-fresh-repo.md)。開源的長期定位另見 #153（展示成果而非可用產品）。

### 3. 自架 Windows runner 語意無效

**技術上可行**（私有 repo 目前仍可掛 self-hosted runner，不另計費），**但它產生不出想要的訊號**。

hosted Full 的全部價值是**宿主差異** —— 在一台不是開發機的乾淨 Windows 上跑，才能抓到「只在我這台過」的缺陷（#60 spike 的結論）。而這裡唯一的 Windows 機器就是開發機，同機自架 = 同一個 runtime = **零宿主差異訊號**。付出維運成本換來一份與本機 `-Route Full` 同義的綠燈。

## Rejected alternatives

- **不寫這份 ADR，繼續讓否決住在交接檔**：交接檔每次 park 都整份重寫且有段落上限，這三條會在某次壓縮中消失，然後被重新提案。它們已經在 2026-08-20 那次 park 撞到上限（`docs/current-status.md` 20 行滿、交接檔模板無對應段落）。
- **併進 `docs/current-status.md`**：那是穩態快照、內容行上限 20 且已滿，要加得先 drain 一行。而且這三條是「不要再提案」的守門，不是「現在是什麼」的狀態描述 —— 抽象層不對。
- **寫進 CLAUDE.md 硬性規則**：CLAUDE.md 只放操作硬約束與穩定行為脈絡；「為什麼當初這樣選、現在還能不能改」按本專案既有分工是 ADR 的職責。

## Consequences

- **「Free 方案」是所有 CI 決策的固定前提**，不是可以繞過的參數。任何以「假設我們有 branch protection」或「假設額度夠」為前提的設計，在提出時就已經不成立。
- **沒有伺服器端的第二道網。** `/session-merge-queue` 的四道 gate 全部由該 session 自己執行，漏掉一道沒有任何東西會發現。[D-20260820-1](D-20260820-1-review-trigger-is-change-size-not-pr.md) 放寬流程包裝之後，這一點更重要。
- **CI 的綠燈語意只有一件事**：「這份 commit 在乾淨 Windows 上裝得起來並啟得動」。它**不跑任何測試**，不能代替 `-Route Full`；draft PR 不觸發。
- **這三條的翻案條件各不相同，翻案時要指名是哪一條**：升級付費方案要擁有者決定；轉公開另有前置條件；自架 runner 要等出現一個不是開發機的 Windows 執行環境 —— 只有第三條是「事實改變就自動翻案」，前兩條需要人重新拍板。

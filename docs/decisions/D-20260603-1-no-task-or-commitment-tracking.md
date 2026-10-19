---
id: D-20260603-1
date: 2026-06-03
status: accepted
title: 不做承諾偵測 / 未結案 thread 追蹤，那是任務管理工具的職責
---

## Context

roadmap 上原本掛著兩條 todo：「承諾關鍵字 surfacer」（掃信件找出我對誰承諾過什麼）與「未結案 thread 偵測」（找出誰欠誰一個回覆）。

兩條都很誘人，因為 DB 裡確實有資料可以做。

## Decision

**移除這兩條 todo，並凍結專案邊界**：本專案是郵件**記憶後端**，不是任務 / 承諾 / 待辦追蹤系統。

「該做什麼、誰欠誰一個回覆」屬於任務管理工具的職責。郵件 DB 提供事實查詢（誰在什麼時候說了什麼），不提供「所以你現在該做什麼」的判斷。

## Rejected alternatives

- **做一個弱版的承諾偵測**：關鍵字 heuristic 的偽陽性會很高，而使用者對「盯催」類功能的信任門檻遠高於「查詢」類。做不準比不做更糟。
- **做完整版**：那是另一個產品，會把這個 repo 從 infrastructure 拉成 application。

## Consequences

- 寫進 CLAUDE.md「這個專案不是什麼」的 scope boundary。
- 未來若真的需要盯催，正確做法是外部任務工具**消費** bes 的查詢結果，而不是 bes 自己長出待辦清單。

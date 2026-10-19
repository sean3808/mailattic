---
id: D-20260728-1
date: 2026-07-28
status: accepted
title: MCP server 暫不做發行版、跟隨 working tree；開發結案時改為 build 快照
---

## Context

全域 `bes` CLI 是 non-editable build 快照，與 working tree 解耦（[D-20260527-1](D-20260527-1-global-bes-is-build-snapshot.md)）。MCP server 卻不是：`claude_desktop_config.json` 用 `uv run --project <repo> python -m mailattic.mcp_server` 拉起它，載入的是 repo 當下的程式碼。

兩者為何不對稱，過去沒寫下來，結果被誤讀過一次 —— GitHub issue `#31` 假設全域 `bes-mcp` console script 是實際入口（那條路吃 wheel metadata、不吃 lockfile），查證才發現 Claude Desktop 根本沒在用它。全域確實裝著 `bes-mcp`，只是沒有任何 client 指向它。

決定的關鍵是 **MCP server 的形態**：它不是被呼叫時才啟動的指令，而是 client 啟動時拉起的長駐 process，靠 stdio 管線對話。2026-07-28 實測，機器上兩組 server process 的啟動時間是當天 09:22 與 11:30，載入的是那個時刻的程式碼；同日稍後對 `mcp_server.py` 的多次修改完全沒有影響它們。

因此「跟隨 working tree」的實際語意不是「改檔即時生效」，而是**改檔後要重啟 client 才生效**。換版閘門已經存在，只是形式是重啟而不是發行。

## Decision

MCP server 維持跟隨 working tree，**不**比照 `bes` 建立發行流程。理由是兩者的換版時機由誰掌握：

- `bes` 的使用者是**其他專案的 subagent**，隨時可能在本 repo 重構到一半時呼叫。閘門必須是發行，否則開發中的半成品會外溢。
- MCP server 的使用者是本人的 CoWork / Claude Desktop，換版時機是**本人重開 app 的那一刻**。閘門已經存在且由本人掌握。

加上 MCP 能力仍在擴張（Phase 2 sync、Phase 3 write tier 未動），發行步驟此刻只會拖慢迭代。

**觸發改為 build 快照的條件（任一成立即重新評估）**：

1. **CoWork 開始承擔本人不在場時執行的工作**（排程任務、非同步流程）—— 換版時機不再由本人掌握，重啟閘門失效。
2. **出現第二個消費者**（另一台機器、另一個 client、他人使用）—— 同上。
3. **MCP 能力凍結、進入維護期** —— 迭代速度不再是成本，穩定性成為主要考量。

切換成本很低且可逆：全域已裝有 `bes-mcp` console script（wheel 快照入口），改 `claude_desktop_config.json` 的 `command` / `args` 指向它即可完成切換。**正因為改一行就能切，不需要提前買保險。**

## Rejected alternatives

- **現在就比照 `bes` 建立 MCP 發行流程**：在能力擴張期為尚未出現的風險付出每次改動都要發行的代價；且該風險的觸發條件明確、可觀察，不必預先防範。
- **移除全域 `bes-mcp` console script 以消除混淆**：那是未來切換的現成入口，移掉等於把可逆決定變成不可逆。混淆的解法是把 canonical 路徑寫清楚（已寫在 `docs/current-status.md`），不是砍掉備用路徑。

## Consequences

- **改了 MCP 程式碼但沒重開 Claude Desktop → CoWork 仍是舊行為。** 這是刻意的，不是 bug。判斷「這個修正 CoWork 吃到了沒」要看 server process 的啟動時間，不是看檔案修改時間。
- 全域 CLI 發行（`release.ps1`）與 MCP server 換版是**兩件獨立的事**。發行 `bes` 不會更新正在跑的 MCP server；重開 Claude Desktop 也不會更新全域 `bes`。
- 全域 `bes-mcp` 與 config 指向的 working-tree 路徑並存，是刻意保留的切換餘裕。任何「全域安裝端會拿到什麼版本」的推論，都必須先確認**哪條路是實際入口**——issue `#31` 就是漏了這一步。
- 上述三個觸發條件成立時，本 ADR 需以新 ADR 取代，而不是就地修改。

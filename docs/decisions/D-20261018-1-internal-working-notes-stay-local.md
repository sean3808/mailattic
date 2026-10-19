---
id: D-20261018-1
date: 2026-10-18
status: accepted
title: 內部工作筆記只留本機，不進版控、不隨公開版帶出
---

## Context

`#262` 第 1 步的獨立掃描把公開版要帶出去的檔逐項列出。機器掃描都乾淨，但有一類檔不是字串比對能判斷的：開發過程的經過紀錄與內部工作筆記 —— 穩態快照（`docs/current-status.md`）、教訓 backlog（`.claude/promote-candidates.md`）、歷史快照（`docs/status-context-*.md`）與設計筆記（`docs/ddd-notes/`）。內容是開發過程的經過紀錄，公開讀者用不到，逐份審也要 Sean 花時間。

`export/mailattic` 是唯一本體、公開時以最終的樹當第一個 commit（D-20261010-1），所以「公開時再排除」會讓私有樹與公開樹不同：fresh clone 的連結檢查與測試只在公開那份上才會壞，平常看不到。

## Decision

**四類檔改為本機工作檔：從版控移出、寫進 `.gitignore`，檔案留在原位。** Sean 2026-10-18 同意（「照建議」）。

**同日追加（`#331` 內容清查）：** 另有八份開發過程的敘事性紀錄比照辦理（路徑見 `.gitignore`）—— 公開讀者不需要。

- session-park 照常讀寫 `docs/current-status.md`；`/promote-lesson` 照常從 `.claude/promote-candidates.md` drain。
- 指向它們的 markdown 連結改成純文字並註明「本機工作檔」，讓私有樹與公開樹一致。
- 決策紀錄裡提到這些檔的句子是當時的事實，不改。

**同日再追加（`#331` 停止步驟，Sean 同意）：** 已經執行完的一次性救援工具（四支腳本與綁著它們的兩支測試）也比照辦理。它們的常數綁著本機語料，放在公開樹裡只會成為「換成虛構值後就不能跑」的殘留來源；重建功能本身（`bes rebuild`）與重建手冊保留在版控內。

## Rejected alternatives

- **公開時才排除**：私有樹與公開樹不一致，公開那份的壞連結平常驗不到。
- **全部刪除**：current-status 與 promote-candidates 每天在用；歷史筆記刪了換不到什麼，留在本機不佔公開面。
- **逐份審過再公開**：花 Sean 的時間，換來的是公開讀者用不到的內容。

## Consequences

- 偏離全域 memory-architecture「`current-status.md` 與 promote ledger 要 committed」的預設：那條是為了跨機保存，本專案只有一台機器，且公開面的隱私優先。換機時要手動搬這四類檔。
- fresh clone 沒有這些檔；AGENTS.md 的指引表已註明。
- **移出版控的那個 commit 會把檔案從其他工作區刪掉**：別的 clone 或 worktree pull 到它、或切到移出前的本地分支再快轉時，git 當成一般刪除，直接移除硬碟上的檔。合併這類改動前，先用 `git diff --name-only --diff-filter=D <base> <head>` 列出全部被刪路徑，在每個要同步的工作區先備份、同步後放回（2026-10-18 曾漏備 8 份，從 git 歷史救回）。

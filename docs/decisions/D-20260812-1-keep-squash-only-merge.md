---
id: D-20260812-1
date: 2026-08-12
status: accepted
title: 維持 squash-only merge，理由不是歷史整潔而是多輪 PR 的 churn 量體
---

> **2026-10-03 註記**：`/session-merge-queue` 已退役（`#207`），squash-only 的決定不變。文中 gate 1 的第二個時點（併之前重讀 squash message）改由 agent 請 Sean 授權合併時呈上最終訊息承接。

## Context

這個 repo 從一開始就是 squash-only：`allow_squash_merge = true`、`allow_merge_commit = false`、`allow_rebase_merge = false`（2026-08-12 查證）。CLAUDE.md 一直寫著「那是刻意的 repo 設定」，但**沒有任何決策紀錄**——搜過 `docs/decisions/` 全部，`squash` / `merge_commit` / `no-ff` / `rebase_merge` 一個字都沒有。於是這條規則只有結論、沒有理由，任何人都可以在下一次覺得它礙事時重開它。

重開發生在 2026-08-12：決定 Spectra archive 時機（#131）的過程中，問題被提成「放棄 squash 會不會讓 spec 的 base drift 問題變好」。

**答案是不會，而且理由是結構性的。** #131 決定 B 之後，PR 不再碰 `openspec/specs/`——它只帶 `openspec/changes/<name>/`，主 spec 只有 main 上那個序列化的 archive commit 會動。git 的合併演算法因此從頭到尾看不到 spec 衝突，換任何合併策略都一樣。剩下的風險是語意的（delta 寫在 baseline B0 上、archive 套到 B1 上），而**沒有任何合併策略偵測得到語意漂移**；能看到它的只有 #131 記下的那個 `merge-base` diff。所以合併策略與 archive 那條線是兩條互不相交的線，這件事本身值得寫下來，免得下次再被當成同一題。

squash 目前確實在收費，而且帳單看得見：

- `## Squash message` 交付契約（正典在 AGENTS.md）整條存在的理由就是 squash——逐輪 commit 消失後，它是唯一留下來的設計紀錄。
- `/session-merge-queue` gate 1 因此有**兩個時點**，第二個是併之前對最終 head 重讀那段訊息、且「預設視為過期」。
- 全域 Task Sizing 對 L-size 規定的 `--no-ff` 在此做不到，CLAUDE.md 帶著一條偏離註記。
- 2026-08-12 那一輪，`#118` 與 `#119` 兩張都因為缺 `## Squash message` 被退回。

反方向的資料是 churn 量體。查證最近併掉的三張 PR 在 squash 前各自帶幾個 commit：`#124` 十六個、`#104` 八個、`#110` 三個。`#124` 正是跑了五輪 review 的那張——放棄 squash，它會在 main 上留下十六個 commit，其中絕大多數是「修 review 第三輪」這類 churn。

還有一個容易被誤用的考量先寫清楚：**這個 repo 的變更史正典不是 `git log`**，是 `openspec/changes/archive/`（[D-20260609-1](D-20260609-1-no-changelog-file.md) 刻意不設 CHANGELOG）。所以「保持 git log 可讀」這個在別處最強的論點，在這裡明顯弱一截，**不足以單獨支撐 squash-only**。本決定不靠它。

## Decision

**維持 squash-only。`allow_merge_commit` 與 `allow_rebase_merge` 保持 false。**

理由按重要性：

1. **它與觸發這次重開的問題無關。** 換掉不會讓 archive 那條線好一分，而那正是提問的動機。
2. **churn 是永久的，squash message 是一次性的。** `#124` 的十六個 commit 一旦落在 main 上就不會消失；squash 的成本是每個 change 寫一段訊息，付一次。
3. **`#118` / `#119` 的摩擦是遷移殘留，不是穩態成本。** 兩張都建立於 2026-08-11T09:23 / 09:51，而集中交付契約的 `#110` 併於同日 10:03——它們寫在契約落地**之前**，被罰的是溯及既往，不是契約太貴。

## Rejected alternatives

- **開啟 merge commit（`--no-ff`）**：能拿回全域 Task Sizing 的寫法、能讓多輪 review 的歷史進 git、能讓 `## Squash message` 契約與 gate 1 的第二時點一起退休——但它用一次性的文書成本，換來 `#124` 形狀的十六個 churn commit 永久留在 main 上。而且它對本次提問的動機（spec base drift）零貢獻。
- **開啟 rebase merge**：保留了全部 WIP commit 的雜訊，卻連 merge commit 那個「看得見的整合點」都沒有，也不再有一個 commit 對應一個 change。被審過的那些 OID 重寫之後也不會以原樣出現在 main 上，事後要指認「哪幾個 commit 是當初被 review 的那份」比另外兩種都難。沒有任何一項優於前一個選項。
- **保留 squash、但放寬 `## Squash message` 契約**：那條契約正是讓 squash 可承受的東西。拿掉它，squash 照樣抹掉逐輪 commit，而設計理由變成沒有任何載體——這是三個選項裡唯一會讓現況變糟的。

## Consequences

- **`--no-ff` 在本 repo 仍然做不到**，CLAUDE.md 那條偏離註記留著；`gh pr merge --merge` 會被 GraphQL 直接擋下，不要改用本機 `git merge --no-ff` 再 push 繞過。
- **`## Squash message` 契約與 gate 1 的兩個時點都是承重牆**，不是可以順手簡化的樣板文書。要動它們，先回來讀本決定的 Rejected alternatives 第三條。
- **多輪 review 的歷史只活在 PR 頁面上。** 想知道一個 change 為什麼長成現在這樣，讀的是 `openspec/changes/archive/` ＋ ADR ＋ PR 頁面，不是 `git log`。
- **反轉的成本主要不在設定，在 harness。** 設定是 GitHub 上一個開關，但跟著要清的是：AGENTS.md 的 `## Squash message` 契約區塊、`tests/test_agent_pr_contract.py` 對它的斷言、`/session-merge-queue` gate 1 的第二時點與 Step 3 那段「預設視為過期」、以及 CLAUDE.md 的偏離註記。先列出來，日後真要反轉時不必重新推導一遍。
- **repository settings 不是 agent 可以碰的東西**（`#63` 的既有裁決）。本決定的任何反轉都是人的動作。
- 本決定不依賴「git log 要好看」。日後若 `openspec/changes/archive/` 不再是變更史正典，本決定的第 2 點仍然成立，但這個前提的變動值得回來重讀一次。

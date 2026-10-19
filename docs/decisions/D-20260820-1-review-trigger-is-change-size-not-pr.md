---
id: D-20260820-1
date: 2026-08-20
status: accepted
title: 語意 review 的觸發條件是變更 size，不是走不走 PR
---

> **2026-10-03 註記**：`/session-merge-queue` 與 `/session-dispatch` 已退役（`#207`）。「語意 review 由 size 觸發」的決定不變；文中關於兩份 SKILL.md 與 PR 佇列 writer 的段落只剩歷史意義。

## Context

2026-08-11 訂下「所有進 main 的變更一律走 PR，沒有本機直接合併分支這條路」，寫在 CLAUDE.md 硬性規則裡，並宣稱理由是「跳過 PR 就等於跳過 `/session-merge-queue` 的 gate 3 語意 review，而 **gate 3 沒有第二個執行地點**」。

**那條規則綁的其實是交付拓撲，不是審查。** Sean 2026-08-20 說明了它的成因：當時 Codex 那邊額度多，打算由 Codex 產出、開 PR、這邊 merge，「一律走 PR」是為那個工作流寫的。Codex 額度縮減之後那個前提就不存在了。

**而它寫下的那句理由本身是錯的。** `/codex-review` 吃的是 diff，不是 PR —— 本機分支的 diff 一樣餵得進去。「沒有第二個執行地點」是這條規則能被讀成「非 PR 不可」的主要支撐，它從一開始就不成立。

規則落地當下就已經有反例。`465bb4b`（2026-08-12）把三個 park 產物直接 push 上 main、未走 PR、Sean 明確授權 —— 而那個 commit 的 CLAUDE.md diff **正是加入這條規則本身**。#134 就是為這個矛盾開的：`git log` 上存在一個反例而沒有任何正典解釋它，日後引用者只能在「規則從一開始就沒人當真」與「有一個沒寫下來的例外」之間二選一。

實務上這條規則也已經在被繞過。2026-08-20 一天之內就繞了兩次：`#155` 的 gate 3 第 3 輪依 Sean 指示跳過、archive commit 直接 push 上 main。**一條被例行繞過的絕對規則，保護力低於它的字面。**

反方向的資料同樣清楚：**語意 review 本身確實在攔東西**。`#155` 第 1 輪抓到的那條，是一次改動無聲覆蓋了 issue 明文的 Out of Scope、兩條未被列為 Modified Capability 的既有 SHALL、以及一份 ADR（`D-20260724-1`）。它測試全綠、CI 全綠、對現存資料的影響是 0 筆 —— 沒有任何機械檢查看得見那一類。所以要保留的是審查，不是 PR 這層包裝。

還有一個獨立的成本：Sean 明確指出，這個專案在 workflow 互相干擾的問題上花掉的時間，已經遠大於專案實質進度，SDD 的 spec 互相覆蓋尤其嚴重。

## Decision

**觸發語意 review 的是變更 size，不是走不走 PR。**

- **M / L 一律要語意 review**，不論工作在哪產出、也不論走不走 PR。
- **S（設定、文件、archive、chore、已知 one-liner）直接進 main。**
- size 判定沿用全域 Task Sizing，**判不出來就當 M**。
- **PR 從「必須」降為選用** —— 需要外部協作交接、或想把討論留成可追溯的串時才開。

已落地於 `e577196`（CLAUDE.md 硬性規則 ＋ `/session-merge-queue`、`/session-dispatch` 兩份 SKILL.md 的複本同步）。

於是「例外」這個概念消失了：archive commit 與 park 產物都是 S，S 本來就直接進 main，不需要被列為任何東西的例外。`465bb4b` 在新規則下不再是反例。

## Rejected alternatives

- **(A) 窄例外，逐項列舉**（只放行 `spectra archive` 產物、park 產物，其餘一律走 PR）：每個例外都要經過一次拍板是它的優點，但每遇到新情形就要再改一次規則，規則會慢慢變成一張清單。而清單型規則的失敗模式本專案已經記過 —— 規則只列 pattern 不列 intent，會被「我做的不在明列範圍」偷渡。
- **(B) 以 intent 為判準**（規則改成「不得讓未經 gate 3 審查的內容進 main」）：`#134` 建議的就是這個，方向對 —— 那條規則自己已經把 intent 寫出來了，問題是被「一律」這個絕對量詞蓋過去。**但它的判準需要事後判斷**（「這個 commit 的內容有沒有被審過」），而目前沒有任何機械檢查守得住它。size 是動工前就能定的，而且它已經是全域 Task Sizing 既有的詞彙，不必新發明一套。本決定採用 (B) 的精神、換掉它的判準。
- **(C) 不帶例外，改掉現有做法**（archive 與 park 產物都走獨立 PR）：規則維持絕對，代價是每個 change 多一個瑣碎 PR，且要推翻 #131 已拍板的「archive 由 merge-queue 在 main 上直接 commit」。在「workflow 已經吃掉太多時間」這個前提下，這是三個選項裡成本最高的。
- **維持原樣**：規則已被例行繞過，維持等於讓正典與實際做法持續分岔 —— `#134` 的存在本身就是這個分岔的成本。

## Consequences

- **`/session-merge-queue` 不再是 main 的唯一 writer**，只是 **PR 佇列的唯一 writer**。因此「main 動了」不再等於「merge-queue 做了什麼」，該 skill 內每一處判斷落點的地方都必須重查實際狀態，不能從「我這輪沒 merge」推論 main 沒變。
- **併發紀律變得更重要而非更寬鬆。** main 有多個 writer 之後，共用 repo 的既有規則是唯一防線：逐檔明列路徑（不用 `git add -A` / `git add .`）、commit 前 `git diff --cached --name-only` 逐檔確認、push 前重查 `git branch --show-current`。三者的正典在 CLAUDE.md 的 Trigger→Resolution hot 段（PC-088 / PC-133）。
- **`--ff-only` 的角色不變**：squash merge 或直接 commit 之後把本機 main 同步到 `origin/main`，不是審查的替代路徑。
- **本決定不豁免審查。** 「S 直接進 main」放行的是**流程包裝**，不是判斷。size 判不出來就當 M 這條是承重牆，拿掉它整條規則就退化成「自己說了算」。
- **`#134` 與 `#131` 的驗收尚未滿足**：`465bb4b` 這個先例在正典裡的解釋（本 ADR 即是）、archive 時機的硬性規則、以及兩條紅綠斷言都還沒做。兩張票要改 CLAUDE.md 的同一個區塊，應由同一個工作單元落地、各自 `Closes`。
- **沒有伺服器端的第二道網。** 這個 repo 用免費方案，沒有 branch protection（見 [D-20260820-2](D-20260820-2-excluded-ci-infrastructure-options.md)）。本決定放寬的每一道都只由執行者自己守，漏掉沒有任何東西會發現。
- **反轉成本主要不在規則文字。** 要收回這條，跟著要改的是：CLAUDE.md 硬性規則、`/session-merge-queue` 與 `/session-dispatch` 兩份 SKILL.md 的 writer 歸屬條款、以及 `#131` 對 archive 落點的安排。先列出來，日後真要反轉時不必重新推導。

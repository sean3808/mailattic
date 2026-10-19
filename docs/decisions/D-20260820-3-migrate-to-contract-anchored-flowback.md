---
id: D-20260820-3
date: 2026-08-20
status: accepted
title: 退出 Spectra 鏈，遷移 Contract-Anchored Flow-Back
---

## Context

本 repo 是 Spectra 鏈最重度的使用者（18 份 specs、73 筆 archived changes），也因此在跨專案流程正典改寫時被明文 carve-out：跨專案流程正典的下游預設已改為 `/implement` → PR → `/spec-reconcile`（Contract-Anchored Flow-Back，另一個專案的試點三樣本畢業，2026-08-20 裁決），但既有 openspec/ 資產大的 repo 維持 Spectra 鏈直到另案裁決（C17/C20）。#165 就是那個另案。

裁決時的實況（2026-08-20 grilling，12 題全程記錄在 #165 comment）：

- **觸發條件滿出來**：C20 訂的觸發是「下一個有行為影響的真 change」，當日 open backlog 38 張、其中十餘張 ready-for-agent。
- **痛點是真的**：小 change 也要走 propose/design/tasks 全鏈，per-change 固定成本高（Sean 明示 Q2=A，並指認 一份外部研究筆記為整套概念正典）。
- **specs 的殘值低於帳面**：人類不讀 specs，只有 agent 讀；多數情境詞彙檔 + 輔助文件就夠，路由 agent 去讀 18 份 spec 是實質 token 浪費（Q3）。
- **試點證據分層**：reconcile 抓到的三類東西（issue 留言期待被改寫、AI 推導掛使用者名字的假歸屬、契約 delta 牴觸）是 code-review 與 codex 結構上抓不到的；但樣本 1 的四項裁決 4/4 追認——價值常是「知情」不是「攔截」。買的是知情權 + 三類獨有攔截，成本是契約-touching change 多一輪 PR + reconcile。

## Decision

**遷移。** 本 repo 退出 Spectra 鏈，改走 issue brief → `/implement` → 契約相關才 PR + `/spec-reconcile` → merge。執行票 #170。

連帶裁決：

- **specs**：18 份原地凍結唯讀（檔頭標記 + CLAUDE.md 路由降級為歷史參考）。不物理搬動——省 token 的是路由降級，不是移檔案；搬走只會斷 ADR/docs 互鏈。
- **changes/**：73 筆 archive 凍結為唯讀史料；變更史路由改「遷移前查 archive、之後查 PR / git log」。D-20260609-1（無 CHANGELOG 檔）不動——merge 承載變更史與它相容。Parked 的 `consolidate-tool-caches` 照舊鏈跑完，成為第 74 筆也是最後一筆 archive。

  > **2026-08-21 補記**：第 74 筆沒有發生，archive 停在 73 筆。當時漏看的一點是 `spectra archive` 會把 change 的 spec delta 寫回 `openspec/specs/`，而本決定同一段已宣告那 18 份凍結唯讀——補跑 archive 等於為了計數去寫一份宣告不再更新的正典。實際收法：`consolidate-tool-caches` 的內容直接 commit（`e95aa57`），另一個掛著的 `schedule-hosted-full-ci` cherry-pick 進 main（`e9c2f4f`），兩個都不走 archive。設計理由由 commit 訊息承載。
- **LANGUAGE.md**：`git mv openspec/LANGUAGE.md CONTEXT.md`（repo root）+ 修 stale 檔頭。matt 系 skills 原生讀 CONTEXT.md，改名後從孤兒變回活檔。CLAUDE.md「不要建 CONTEXT.md」規則的立法理由是「避免兩份正典」——遷移後單一正典恰好就叫 CONTEXT.md，規則改寫而非違反。
- **種子契約**：5 條池（MCP 唯讀性、outbound TTY 授權 gate、APPEND 不重寄、台北整日日期語意、sync fail-loud），符合 C3 三類（對外可觀察／安全不變量／語意陷阱）且都咬過人。紀律：缺 anchor test 先補才准入契約；驗不出 anchor 的降回普通條文。日期那條**釘 D-20260723-1 現狀、語意不動**——遷移動作若順手改行為，正是這套機制自己要攔的「未意圖＋已實作」。
- **防護全套繼承試點的畢業形態**：gate 段放 CLAUDE.md 高熱度位（含派工鐵則 + 開工導向點，堵試點唯一連兩次逐字重演的「協定投遞失效」——本 repo CLAUDE.md 比試點的長，這個洞在這裡只會更大）；hook 對 Bash 編輯全盲的修法一併 port，實作為 Python pre-commit hook（本 repo 禁 `.sh`）。
- **與 D-20260820-1 的銜接**：契約-touching change 強制 PR + reconcile，正是該 ADR 保留的「想留可追溯討論串」情境，不構成反轉；其餘 change 照 size 規則 direct commit。

## Rejected alternatives

- **維持 Spectra 鏈（carve-out 常態化）**：若痛點只是「想跨專案統一」這會是對的答案——但 Q2 確認痛是真的（全鏈固定成本），且 Q3 確認 specs 殘值低，carve-out 的保護對象比帳面小得多。
- **雙軌並行**（新工作走新流程、Spectra 機構保留可用）：#165 票面明文排除「不雙軌」；兩套變更史正典並存會讓「最近改了什麼」這個問題沒有單一答案。
- **只瘦身 Spectra**（小 change 跳過 proposal/design）：修的是同一個痛，但沒有試點數據，而 Flow-Back 有三樣本畢業記錄 + 明文降級梯子；自創變體等於放棄已驗證的形狀。
- **LANGUAGE.md 凍結或歸檔**（票面原列選項）：兩者都讓它維持孤兒；改名 CONTEXT.md 讓 matt 系 skills 直接消費它，是唯一讓 14KB 內容重新有讀者的選項。

## Consequences

- **CLAUDE.md 是遷移的主戰場**：Spectra Instructions 段移除、行為契約 gate 段新增、路由表兩處降級、CONTEXT.md 規則改寫——`tests/test_tag_based_release.py` 對 CLAUDE.md 有三件套斷言，動之前先讀該測試。
- **#131、#134 被 supersede**（Spectra archive 時機失去標的；PR 例外已由 D-20260820-1 + 本決定共同解決），由 #170 關閉；#134 comment 裡的兩條紅綠斷言工作不做。**#136 反轉為活票**：「契約在佇列進行中落地時既有 PR 怎麼算」正是新流程會遇到的實例收集。
- **遷移落地前不派新 change**（parked 例外），避免半路換軌；落地後全 backlog 走新流程，issue brief lazy 補、不批次改寫 38 張票。
- **降級梯子明文存在**（S3）：連跑三個 change 仍覺 reconcile 重 → 退階段批次模式（reconcile 收斂到階段收尾掃 merged diff）。遷移不是單行道，但退的是 reconcile 頻率，不是退回 Spectra。
- **反轉成本**：真要回 Spectra，要復活的是 CLAUDE.md 的 Spectra Instructions、路由表、以及「specs 隨 change 同步 delta」的維護義務；凍結期間 specs 不再更新，回頭時 specs 與 code 的落差是回頭者要自己補的帳。先寫在這，日後翻案不必重新推導。
- **`/spectra-*` skills 在本 repo 失去觸發場景**但不用拆——skill 是全域資產，其他 repo 或 parked change 收尾仍會用到。

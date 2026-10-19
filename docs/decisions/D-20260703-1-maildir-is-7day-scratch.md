---
id: D-20260703-1
date: 2026-07-03
status: accepted
title: .mailattic-work/maildir/ 是 7 天暫存跳板，不是 raw archive
---

## Context

native sync 每封信先原子寫 `.eml` 到 `.mailattic-work/maildir/` 再 ingest 進 DB。這批 `.eml` 累積下來就是一份 raw RFC822 副本，看起來很像可以當 forensic 存檔用。

不 prune 的話它會無限長大；而一旦有人開始把它當存檔依賴，就再也刪不掉了。

## Decision

`.mailattic-work/maildir/` 定位為 **7 天暫存跳板**。每次 sync 跑完 prune 掉 mtime > 7 天的 `.eml`。

Prune 的安全條件：**只刪「內容已 durable 進 DB」的檔**。對每個 stale `.eml` 解析 content identity（`Message-ID`，缺則 fallback `dedup_key`），只有該 identity 存在於 `messages` 才刪。ingest 失敗、解析失敗、DB 查不到 → **一律保留**（那是唯一未入庫的本地副本）。

DR 靠 PST + DB 備份，**不靠 maildir**。

## Rejected alternatives

- **無限保留當 raw archive**：磁碟無限成長，且會養出「maildir 是備份」的錯誤依賴。DB 才是正典。
- **無條件按 mtime 刪**：會刪掉 ingest 失敗、只存在於 maildir 的那份副本 —— 正好是最不能刪的那些。

## Consequences

- 手動 mbsync / PST backfill **必須用隔離的 work maildir**（顯式 `--maildir-root`），不可依賴 `.mailattic-work/maildir/` 的歷史內容 —— 已 ingest 的舊 `.eml` 會被 prune 清掉。
- 設計正典：`openspec/changes/archive/2026-07-03-prune-maildir/`（歷史檔案，本 repo 不含）。

## 2026-10-13 補記：隔離區例外（#239 / #256）

七天暫存不變，但多一個例外：清理時若這封信在 `ingest_failures` 仍有任何未解決的附件失敗（`bes db doctor` 列為未解決的那一組，有沒有檔名都算），原始檔改為**搬進隔離區**而不是刪除，直到附件補回或有人手動放棄那筆失敗為止（Sean 2026-10-09 於 #239 拍板）。上方「無限保留當 raw archive」的拒絕理由仍然成立：隔離區只收這一類，不是備份。名詞見 `GLOSSARY.md` 的「隔離區」。

隔離區預設位置跟著資料庫走：`<資料庫目錄>/.mailattic-work/quarantine/<資料庫檔名>/`，不再跟暫存 maildir 走；用資料庫複本做實驗時，預設碰不到正式的隔離區。資料庫搬家或改名後，新位置看不到舊的隔離區（重試會回報找不到來源），要連同目錄一起搬。

**回滾步驟（隔離區的檔怎麼處理）**：回滾這個變更後，舊程式不認得隔離區目錄，也不會自動刪其中任何檔。要讓隔離檔仍能被重試補回，回滾**之前**先把它們依原相對路徑搬回暫存 maildir（`<帳號>/<資料夾>/new/<檔名>.eml`）；否則留在原地不動，或確認不需要後手動刪除。

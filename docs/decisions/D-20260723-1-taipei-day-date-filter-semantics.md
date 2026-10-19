---
id: D-20260723-1
date: 2026-07-23
status: accepted
title: Date filter bound 語意統一為 Asia/Taipei 整天（inclusive），不統一各介面的輸入語法
---

## Context

四個查詢介面（`search` / `attachments` 兩個 CLI 子命令共用一個 parser、`contact-history` 聚合、`mail_search` MCP tool）各自組裝 `YYYY-MM-DD` bound 的方式不一致：`search`/`attachments` 對 bare date 產出 naive datetime、把上界釘在午夜（等同只涵蓋一個瞬間，不是整天）；`contact-history` 對日期字串串接一段 naive time，比較時當 UTC，實際上界會溢出到隔天台北時間清晨；`mail_search` 已經是唯一組出 Asia/Taipei-aware bound 的介面。查詢一律經 SQLite `unixepoch()` 比較絕對時刻，沒有 offset 的 bound 一律被當 UTC 解讀 —— 這點已用專案 interpreter 直接驗證過（naive literal 與帶 `+00:00` 的同一段文字算出同一個 epoch）。

四個介面各自的日期字串 parser 天生就接受不同的輸入集合（`search`/`attachments` 用 `datetime.fromisoformat`；`contact-history` 用 `strptime("%Y-%m-%d")`；`mail_search` 用 `date.fromisoformat`），design.md 的量測表對此有完整紀錄，本次改動不處理這個差異。

## Decision

**只統一「一個 `YYYY-MM-DD` bound 代表什麼」，不統一「哪些輸入被接受」。**

- 一個純日期值（date-only）代表 **Asia/Taipei（UTC+8，無日光節約）該日整天，兩端 inclusive**：下界 `00:00:00+08:00`、上界 `23:59:59+08:00`，兩者皆 offset-aware。
- 一個**帶 offset 的 timestamp bound** 仍然選中它本來指的那個絕對時刻，不會被拓寬成整天。
- 一個**沒帶 offset（naive）的 timestamp bound**，語法不變，但語意發生**破壞性位移**：從原本被當作 UTC 的那個時刻，改為被解讀成同一段文字所寫的台北時刻 —— 這不是「維持原意只是換個算法」，是真的移動了八小時，跟純日期 bound 的修正屬於同一類破壞性變更，明白記錄為 breaking。
- 四個接受日期輸入的介面，各自跑三種不同的 parser、各自接受不同的輸入集合（`search`/`attachments` 共用一種、`contact-history` 一種、`mail_search` 一種），這個現況**刻意保留、不做語法統一** —— 本次改動只統一「一天」的意義，統一語法是另一個有自己 breaking 宣告的獨立改動，不在這裡處理。
- **儲存端**沒有 offset 的 `sent_at`（目前少數幾筆，原始 Date header 本身就沒帶 offset）被解讀為 **UTC**，作為一個有邊界、盡力而為（best-effort）的既定限制 —— 那些寄件人真正的本地 offset 從未被記錄，沒有規則能回推；套用 UTC 剛好與 SQLite `unixepoch()` 對 naive literal 已經在做的事一致，也不會對語料庫中另外多種 offset 的寄件人硬猜出一個很可能是錯的時區。
- 開放問題（本次不解決）：ingestion 端未來要不要在寄件人沒帶 offset 時主動附上一個 offset，留給日後的 write-path 決策；本次的 query 規則對現有及未來新增的 naive 儲存列都已定義清楚，不受這個問題是否解決影響。

## Rejected alternatives

- **只寫文件、不動程式**：`mail_search` 的規則其實早就寫在文件裡，另外三個介面依然各行其是；只留在 prose 裡的規則等於沒有規則。
- **強制四個介面套用同一種 parser / 同一組可接受語法**：量測結果顯示這會讓至少兩個介面失去現在能接受的輸入（`mail_search` 會失去 `20260721` / `2026-W30-2`；`contact-history` 會失去 `2026-7-1`），為了整齊犧牲既有相容輸入，沒有 caller 要求過這件事。
- **naive stored send time 直接猜測為 Asia/Taipei**：語料庫涵蓋多種不同 offset 的寄件人，對非台北寄件人一定猜錯，UTC 才是唯一不需要猜的讀法。
- **拒絕 naive datetime 傳進 Python query API**：欄位型別一直是 plain `datetime`，直接拒絕會打破既有 caller，而既有的 coercion 已經正確表達了「無 offset 就當台北」這條規則。

## Consequences

- 三個既有介面的可觀察行為改變：`search` / `attachments` 的下界往前移 8 小時（涵蓋原本被漏掉的當地清晨信件）、上界變成真正的整天（涵蓋原本被漏掉的當地下午／晚間信件）；`contact-history` 的上界不再溢出到隔天台北清晨。任何釘死日期區間的排程或腳本，第一次跑之前應該重新檢查一次 —— 尤其 `attachments --copy-to` 搭配 `--overwrite` 時，比對範圍變寬多寫出／多覆蓋的檔案不只是多印幾行。
- naive timestamp bound（非純日期）的語意位移是本次唯一「語法不變、意義真的改變」的輸入形式，用專屬的 CLI-level 測試釘住，不能只靠 query 層的 coercion 測試去推論。
- 四個介面各自接受的日期語法保持原樣不變，實測結果如下：`contact-history` 接受 `2026-7-1`、拒絕 `20260721`；`mail_search` 接受 `20260721` 與 `2026-W30-2`、拒絕 `2026-7-1`；`search` 與 `attachments` 兩者都接受，並且額外接受完整 timestamp。**沒有任何一個介面窄化成只接受 canonical 拼法**。
- 設計正典：`openspec/changes/unify-date-filter-timezone/design.md`；三份 delta spec：`openspec/changes/unify-date-filter-timezone/specs/{message-query-surface,attachment-search-export,contact-history}/spec.md`。

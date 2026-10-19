---
id: D-20260714-2
date: 2026-07-14
status: accepted
title: 接受中文 寄件備份 為 Sent folder of record，bes 自己處理 mUTF7 編解碼
---

## Context

Outlook 改走 IMAP 後，中文語系的 Outlook 在 server 上建的特殊資料夾是中文名（`寄件備份` = Sent、`草稿` = Drafts）。

IMAP 線上格式（RFC 3501）用 **modified-UTF-7** 編碼非 ASCII 名稱，`寄件備份` 的 wire name 是 `&W8RO9lCZTv0-`。bes 目前把它當 UTF-8 硬解，結果是 `folder_path` 存進 DB 的是那串亂碼。

兩條出路：讓 Outlook 改用 ASCII 資料夾名，或讓 bes 學會 mUTF7。

## Decision

**接受中文 `寄件備份` 為 Sent folder of record**，由 bes 實作 mUTF7 encode / decode。

理由：

- 若 server **不支援 SPECIAL-USE**，就無法向 client 宣告哪個資料夾是 Sent，所以 client 只能靠自己的 heuristic 決定 —— server 端改不了。
- 目前版本的 Outlook **沒有 Sent Items 設定分頁**，改不了它選的資料夾。就算用其他手段改掉，也會在版本更新 / 帳號重設時被打回原狀。

靠 bes 這一側吸收編碼差異，比靠 Outlook UI 設定穩定得多。

## Rejected alternatives

- **改 Outlook 指向 ASCII `sent-mail`**：UI 沒有這個設定；繞道改出來的設定不耐版本更新。
- **在 server 端建 alias / 用 SPECIAL-USE 宣告**：該 server 不支援。

## Consequences

三項待實作（GitHub issue #21）：

1. `imap_native.py:list_folders()` 加 mUTF7 decoder —— `folder_path` 存中文而非亂碼
2. backfill 既有 DB 的 `folder_path`（wire format → 解碼後）
3. mUTF7 encoder + IMAP APPEND-to-Sent —— bes 送出的信也 APPEND 進 `寄件備份`，Outlook 才看得到（取代 D-20260528-2 的 BCC-self）。APPEND 為 best-effort，不阻擋送出；dedup 靠共同 Message-ID 收斂。

下游影響：另一個下游專案的 tier-1 folder selector 要改認中文 `folder_path`，不是 ASCII `sent-mail`。

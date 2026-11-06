---
id: D-20261106-1
date: 2026-11-06
status: accepted
title: 內文重做在原位置對不上時，改以 Message-ID 在所有 PST 找原信
---

## Context

[D-20261105-1](D-20261105-1-table-fidelity-without-storing-html.md) 定下：PST 匯入的舊信回 PST 重新解包做[[body re-derivation]]，「依原位置配對並以 Message-ID 驗證」。

2026-11-05 在正式庫的唯讀複本上試跑，發現入庫之後兩個 PST 都變動過：

- `archive_2024-2025.pst` 由 18,378 封長到 29,300 封，整個資料夾的編號偏移：依位置找到、但 Message-ID 不符的有 2,240 封。原因是這個封存檔本來就是半成品：它在 2026 年初建立，當時沒把 2025 年的信全部放進去；後來業務量暴增，Sean 在 2026 年整理信箱時才把 2025 年的信補進去（Sean，2026-11-06）。
- POP 設定檔的 Outlook 本機 PST 裡，大量舊信已被搬走：依位置找到但 Message-ID 不符的有 10,919 封，找不到位置的有 5,417 封。

Message-ID 驗證全數擋下了這些不符的信，沒有寫錯任何一封；但只靠位置配對，能重做的信大多會漏掉。

## Decision

位置配對失敗、且本身有 Message-ID 的信，改在這次解開的所有 PST 裡找同一個 Message-ID。**只有找到的候選內文完全一致時才採用**；內文不一致就不寫入（計為 `message_id_ambiguous`）。

不變的事：

- 沒有 Message-ID 的 PST 信一律不重做，也不走後備配對（它們的 `dedup_key` 含內文雜湊，改內文會讓日後重匯對不上）。
- 寫入時照舊核對 Message-ID，以及正式庫原內文從暫存到寫入之間沒被改過。
- 重做後內文變差就保留原內文。

採用後正式庫試跑多重做 16,830 封。這項由 agent 先依技術判斷實作，因為它改變了 Sean 拍板的配對方式、又決定了正式資料的寫入範圍，交 Sean 追認；Sean 於 2026-11-06 追認（PR #58）。

## Rejected alternatives

- **只依位置配對**（原決定）：安全，但 PST 一變動就漏掉大多數信；而 PST 會隨日常整理信箱持續變動，不是一次性的意外。
- **只依 Message-ID 配對、不看位置**：位置對得上時，位置是比 Message-ID 更直接的證據（Message-ID 可能重複）；位置留作第一順位，Message-ID 只當後備。
- **找到同一 Message-ID 就採用、不比內文**：兩封不同的信共用 Message-ID 時會寫錯內容。要求候選內文一致，就讓「共用 Message-ID」的情況只可能寫出相同結果。

## Consequences

- 「Message-ID 重複且內文一致」會被當成同一封信；因為內文一致，重做的結果相同，不會寫錯內容。
- 伺服器已刪、但仍在 PST 的 IMAP 信，同樣可以依 Message-ID 從 PST 取原信重做；這部分另開 [#59](https://github.com/sean3808/mailattic/issues/59)。
- D-20261105-1 中「依原位置配對並以 Message-ID 驗證」一句由本決策補充：位置仍是第一順位，對不上時才改以 Message-ID 後備。

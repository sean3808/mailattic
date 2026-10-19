---
id: D-20260724-1
date: 2026-07-24
status: accepted
title: 收件人網域比對走查詢層邊界函式（Option A），資料正規化（Option B）延後
---

## Context

`bes contact-history --domain`、`bes search` / `bes attachments` 的 `--recipient-domain` / `--participants-domain`，收件人側都用無右錨的 `recipient_search_text LIKE '%@<domain>%'` substring 比對，導致 `acme.com` 誤命中 `alice@acme.com.evil`（GitHub issue #25）。寄件人側已用尾端錨定 suffix、不受影響。

修法有兩條路，經 codex 三輪 plan-review 才收斂：

- 第一版設計把收件人側改走 `message_recipients` 正規表、per-row `email` 尾端錨定。**被 review 否決**：canonical DB 有數百筆 **legacy packed row**，其 `email` 欄位其實是一整包 `'name'; 'email'; 'name'; 'email'` 字串（例：形如 `'alice@a.example'; '王小明'; '陳大華'; 'bob@b.example'`）。對這種 blob 做尾端錨定會讓中段的真收件人回 false —— 把良性的假陽性換成**靜默的假陰性**，更糟。當初「大量資料零 coverage gap」只證明兩種表示都存在，沒證明 `email` 是原子單一地址。
- 這批 packed row 同時也讓 **exact `--contact` 比對**壞掉（整包 blob 不等於單一地址）並劣化 FTS，是 #22 家族的髒資料。

## Decision

**分兩階段：先 Option A（查詢層邊界函式，不動資料）落地修 domain over-match；Option B（資料正規化）另開 change 延後處理。** Sean 於 2026-07-24 拍板「先 A 再 B」。

### Option A（本 change：`anchor-recipient-domain-filter`）

在唯一連線工廠 `connect_database` 註冊一個 deterministic SQL 函式 `recipient_domain_match(recipient_text, domain)`：

- 先 `lstrip('@')` + strip whitespace 正規化 domain（對齊 sender 側 `_to_domain_suffix_pattern` 的 `lstrip('@')`，讓 `@acme.com` == `acme.com`、兩側不歧異）；空文字或 strip 後空 domain 回 0。
- 用 case-insensitive regex `@<re.escape(domain)>(?![\w.-])` 比對。**邊界規則是「網域後面不接網域標籤字元才算命中」**，不是「後面接特定分隔符」—— 分隔符在 packed blob 裡不固定（空白 / `'` / `;` / `>`），但「不是網域字元」這條對乾淨地址與 packed blob 都對。`\w` 用 Unicode-aware（Python 3 預設），所以 `海` 這種非 ASCII 續接字元會被當成「延續標籤」而正確排除（`acme.com` 不誤命中 `alice@acme.com海灣`）。

套在三處收件人側 call site（`contact_history` domain 分支、`query.py` 的 `--recipient-domain` 與 `--participants-domain` 收件人側），繼續比對 `recipient_search_text`（不 JOIN、不依賴 `message_recipients` 原子性）。寄件人側、`--sender-domain`、自由 `--recipient` substring 全不動。MCP `mail_search` 包 `search_messages` 自動繼承。

**驗證**：10-case 矩陣 + 真實 packed row 實測全過（範例網域仍命中、`.evil` / `海灣` / subdomain 排除、leading-`@` 等價、空回 0）。

**已知 micro-limitation**：Python `\w` 不含 Unicode combining marks（Mn 類），故 `alice@acme.com` 後緊接裸組合附加符號這種退化輸入仍會被當邊界而命中。實務不會出現（TLD 後不會裸接組合符號），stdlib `re` 無 `\p{M}`，為此加第三方 `regex` 套件不划算；仍嚴格優於原本全 substring 過度比對。

### 被否決 / 延後

- **Option B（`message_recipients` 資料正規化）**：用 `email.utils.getaddresses` 把這些 packed blob 拆成一列一地址，之後乾淨尾端錨定即成立，且一次治好 domain 比對 + exact-contact + FTS。**延後另開 change**：它會動 canonical DB（需 `bes db backup` 先備份），且重新解析「人名+地址」blob 本身易錯（blob 內純人名項如 `'王小明'` 無 email 可救，只能保留），屬 L-size。Sean 已確認 DR floor（PST 全在 + `bes rebuild`）解除「動 DB 不可逆」的顧慮，故 B 可行、只是排在 A 之後。
- **候選：欄內列舉分隔符邊界比對**（OR 多個 `'%@<domain>;%'` 等）：分隔符集合開放且脆弱，漏一個就是靜默假陰性；「不是網域字元」一條 predicate 覆蓋所有終止符，故不採。

## Consequences

- `--recipient-domain` / `--participants-domain`（收件人側）與 `contact-history --domain` 的結果集**縮小**：只除假陽性、不損真陽性（含 packed blob 中段真收件人）。此為刻意的正確性修正，兩份 spec（`contact-history`、`message-query-surface`）已更新為此行為，backward-compat scenario 加了 carve-out。
- 新增一個 per-connection 註冊的 SQL 函式；非 `connect_database` 開的 raw 連線跑這些查詢會 fail-loud（`no such function`），不會靜默漏。
- 這批 packed row 對 exact `--contact` 與 FTS **仍壞**，本 change 不改善也不惡化 —— 留給 Option B。

## Option B follow-up → GitHub issue #27

> 本節即 task 4.3 的 follow-up 記錄。已於 2026-07-24 開立 issue #27。

**標題**：正規化 `message_recipients.email` legacy packed blob（拆成一列一地址）以修復 exact-contact 與 FTS。

**範圍**：`bes db backup` 先備份 → 用 `email.utils.getaddresses(strict=False)` 解析 `email LIKE '%@%@%'` 的資料列 → 每個可抽出的地址建獨立 row（純人名項無地址、保留原樣或標記）→ 同步重建該些 message 的 `recipient_search_text` 與 FTS → 不變量檢查（拆解前後地址集合一致、無新增/遺漏）→ 既有 DB migration。屬 L-size，走 R5 備份 gate。相關：#22（收件人保真度）、#25（本 change 修的 over-match）。

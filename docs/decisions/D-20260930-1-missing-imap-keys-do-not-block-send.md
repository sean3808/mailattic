---
id: D-20260930-1
date: 2026-09-30
status: accepted
title: 寄件設定缺 IMAP 欄位不擋寄信；寄件備份遺漏由 BCC-self 兜底
---

## Context

`#114`（PR `#220`）讓「完全沒帶寄件設定」（`env=None`）在寄出前拒寄。另一種情況維持舊行為：設定有帶，但缺 `IMAP_HOST` / `IMAP_PORT` / `IMAP_USER` / `IMAP_PASSWORD` 任一項，或 IMAP 帳號與 SMTP 寄件帳號不符。這時信照寄，寄出後 APPEND 才發現問題，只印 stderr warning，寄件備份看不到這封信。

`#220` 的對帳指出契約 #2 字面「寄出一律要帶寄件設定」比實作寬。`#221` 問的是要不要連這種情況也改成寄前預檢、缺欄位就拒寄。

## Decision

維持現狀：缺 IMAP 欄位**不擋寄信**。APPEND 仍是寄出後的 best-effort（`_append_sent_copy` 不當 preflight），失敗只印 warning、不改 exit code（契約 #3）。

契約 #2 的「一律要帶寄件設定」指的是 `env` 不得省略或傳 `None`，不包含欄位齊全檢查。

## Why

Sean 拍板（2026-09-30，`#221`）：「信寄出去了但寄件備份看不到」可以接受，「整封寄不出去」不行。

損失有兜底：每封草稿在擬稿時都自動加上 BCC-self（詞彙見 [`GLOSSARY.md`](../../GLOSSARY.md)；`outbound.py` 收件人組裝，呼叫端關不掉；[D-20260528-2](D-20260528-2-outbound-visibility-via-bcc-self.md)），寄件備份漏掉時，Sean 的收件匣仍有一份，回流時經 message-id dedup gate 進 DB。

## Rejected alternatives

- **寄前預檢欄位、缺就拒寄**：設定檔少一欄，連本來寄得出去的信都寄不出去，只為了換回一份已經有兜底的可見性。

## Consequences

- 看到 APPEND 缺欄位的 warning 時，照契約 #3 不重寄。要補救的是設定檔，不是那封信。
- BCC-self 若被移除或改成可選，本決策的前提就不成立，要重開。

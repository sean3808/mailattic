---
id: D-20260528-2
date: 2026-05-28
status: accepted
title: bes 寄出的信靠 BCC 寄件人自己讓 Outlook 看得到，不走 IMAP APPEND
superseded_by: D-20260714-2
supersede_note: POP3 前提已於 2026-07-14 失效（Outlook 改走 IMAP），BCC-self 機制仍在跑，待 APPEND-to-Sent 取代，見 GitHub issue #21
---

## Context

`bes send-draft` 經 SMTP 送出的信，Outlook 的寄件備份看不到 —— 因為 Outlook 對 `sean3808@mailattic.test` 走 **POP3**，POP3 只拉 INBOX，server 端 Sent folder 的內容它根本不看。

## Decision

每封 outbound 自動 BCC 寄件人自己。副本經 server 進 INBOX，POP3 拉得到。

前置條件：Outlook POP3 須勾「在伺服器保留訊息複本」（已設 30 天保留）。

## Rejected alternatives

- **IMAP APPEND 到 server Sent folder**：技術上可行，但 POP3 的 Outlook 看不到，等於白做。
- **不管 Outlook 可見性**：Sean 的人類介面就是 Outlook，寄出去的信在自己的信箱裡查不到，日常操作會斷。

## Consequences

- **失效窗口**：bes IMAP 同步若連續故障超過 30 天，server 端複本會被 POP3 保留期限清掉，那段期間的 outbound 副本會遺失。
- **前提已在 2026-07-14 消失**：Outlook 已改走 IMAP（連線上限 100→1000 之後），POP3 這個原始理由不再成立。機制目前仍在運作，取代方案（mUTF7 APPEND-to-Sent）見 D-20260714-2 與 GitHub issue #21。

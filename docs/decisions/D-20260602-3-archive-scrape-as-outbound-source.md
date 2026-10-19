---
id: D-20260602-3
date: 2026-06-02
status: superseded
title: 用 archive-scrape 定期抓 server archive 當作 outbound 郵件來源
superseded_by: D-20260714-1
---

## Context

Outlook 當時走 POP3，只拉 INBOX。Sean 從手機 / webmail 寄出的信，本地 Outlook 完全看不到，PST 也不會有，等於 DB 缺一塊 outbound。

自架 mail server 有 archive 介面可以撈到所有進出信件。

## Decision

實作 `bes scrape-archive`，掛 Windows Task Scheduler 排程（`MailatticArchiveScrape`）定期抓 server archive，當作 outbound 的補漏來源。與 IMAP / PST 來源靠 message-id dedup gate 共存。

## Consequences（為何被取代）

- archive 匯出的信件 header 是**有損的**（非原始完整 header），與 IMAP 來源競爭 dedup 的「first-arrival」時會贏，反而讓 DB 拿到品質較差的那份。
- 2026-07-14 Outlook 改走 IMAP 後，`寄件備份` folder 直接提供完整原始 header 的 outbound 來源，archive-scrape 的存在理由消失。

被 **D-20260714-1** 取代：排程停用（Disabled），程式碼保留供手動 backfill 與災難復原。

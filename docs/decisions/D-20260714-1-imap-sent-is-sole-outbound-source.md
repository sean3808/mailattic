---
id: D-20260714-1
date: 2026-07-14
status: accepted
title: IMAP Sent folder 是 outbound 唯一常規來源，archive-scrape 降為手動備援
supersedes:
  - D-20260602-3
---

## Context

2026-07-14 Outlook 的 `sean3808@mailattic.test` 帳號從 POP3 改成 **IMAP**（根因是 server 連線上限已放寬）。

Outlook 一旦走 IMAP，它寄出的信會落在 server 的 Sent folder（`寄件備份`），bes 的 IMAP sync 直接就能拿到 —— 而且是**完整原始 header**。

這讓 outbound 有了兩個來源：IMAP Sent（完整 header）與 archive-scrape（有損 header）。兩者競爭 dedup 的 first-arrival，誰先到誰贏，DB 可能拿到品質較差的那份。

## Decision

採用 **Plan A'**：IMAP Sent folder 是 outbound 的**唯一常規來源**。

- `MailatticArchiveScrape` 排程**停用**（Disabled）。
- archive-scrape **程式碼保留**，供手動 backfill 與災難復原。

理由：寄件幾乎都走這台機器的 Outlook。移掉競爭來源就直接消滅 dedup first-arrival 問題，**不需要動 dedup precedence 邏輯**。

## Rejected alternatives

- **改 dedup precedence，讓 IMAP 來源優先於 archive**：要動核心 dedup 邏輯，風險與複雜度都高，只為了保住一個幾乎沒在用的來源。
- **兩個來源都留、排程照跑**：first-arrival 是時序賽跑，行為不可預測。

## Consequences

- **殘留風險**：偶爾從手機 / webmail 寄的信，若沒經過 Outlook，就不會進 IMAP Sent。緩解方式是需要時手動跑 archive-scrape backfill。
- 正式記錄在 GitHub issue #22。

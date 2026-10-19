---
id: D-20260528-1
date: 2026-05-28
status: accepted
title: bes 不接 LLM，outbound 只做機械步驟，內文一律由 caller 提供
---

## Context

`bes draft-reply` 要產生回信草稿。看起來很自然的下一步是讓 bes 自己接 API 生內文（`bes draft-reply --auto` 之類）。

## Decision

bes 只做**機械步驟**：header 組裝、reply-all 收件人推導、In-Reply-To / References 接續、SMTP 送出、DB insert、TTY 確認。

內文草擬由 caller AI 提供（`--body-from <file>`）。**任何讓 bes 自己生內容的 flag 都禁止存在。**

## Rejected alternatives

- **bes 內建 LLM 呼叫**：會讓「送出」這個不可逆動作與「生成」這個不確定動作在同一個進程裡耦合。人類授權的對象會從「這封信」變成「這個 prompt」，授權門失效。
- **bes 提供 template 引擎**：template 只是弱化版的生成，同樣把內容責任搬進 bes。

## Consequences

- 職責邊界乾淨：caller（Claude / Codex）負責想寫什麼，bes 負責正確地送出並記帳。
- ~~送出永遠要求 TTY + typed confirmation，沒有非互動 send flag（`send-draft` / `send-batch` 共同契約）。~~ 2026-09-14 由 [D-20260914-1](D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md) 推翻：寄出須帶與草稿相符的指紋，或真 TTY 明文授權。本 ADR 的主決策（bes 不生內容）不受影響。

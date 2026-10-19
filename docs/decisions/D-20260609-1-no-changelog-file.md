---
id: D-20260609-1
date: 2026-06-09
status: accepted
title: 不維護 CHANGELOG.md，變更史以 openspec archive 為正典
---

## Context

功能一條條上線，很自然會想開一個 `CHANGELOG.md`。

但這個 repo 已經有三個必須互相同步的面：CLI（`bes --help`）、skill（對外使用介面正典）、spec（`openspec/specs/`）。

## Decision

**不開 CHANGELOG.md。** 分工如下：

- **現狀能力**（bes 現在能做什麼、怎麼呼叫）→ `mailattic` skill + `bes <cmd> --help`
- **變更史**（何時新增什麼、為何這樣設計）→ `openspec/changes/archive/YYYY-MM-DD-<name>/`（每筆含 proposal / design / spec，比手寫 changelog richer），輔以 Conventional Commits git log
- **跨 change 的標準決策** → 本 ADR 目錄

## Rejected alternatives

- **手寫 CHANGELOG.md**：會成為第 4 個要同步的漂移面，而且它記的東西 archive 已經記得更完整。漂移的 changelog 比沒有 changelog 更誤導。

## Consequences

- 要回答「這個能力什麼時候上的、為什麼這樣設計」→ 去 `openspec/changes/archive/`，不要期待 repo 根有 changelog。

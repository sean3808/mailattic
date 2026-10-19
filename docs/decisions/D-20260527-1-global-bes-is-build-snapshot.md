---
id: D-20260527-1
date: 2026-05-27
status: accepted
title: 全域 bes 裝成 non-editable build 快照，與開發 working tree 解耦
---

## Context

其他專案的 subagent 要能在任意 cwd 直接 `bes search ...` 查中央 DB。最省事的做法是 `uv tool install --editable`，讓全域指令直接指向 repo working tree。

但這條路把「開發中的半成品」與「跨專案正在依賴的查詢工具」綁在一起：切 branch、改到一半、測試中的 breaking change，別的專案此刻來撈資料就會吃到。

## Decision

全域 `bes` 一律裝成 **non-editable wheel 快照**（`uv build` → `uv tool install --force <wheel>`），與 repo working tree 解耦。

- 發布：`scripts/reinstall-global.ps1`（清 dist → rebuild wheel → uninstall → `--force` 重裝）。不動 dev `.venv`、不跑 `uv sync`。
- 開發本專案自身仍在 repo 內用 `uv run bes` 吃 working tree。
- 「versioned」指的是 build 快照解耦，**不是** semver —— `pyproject.toml` 版本靜態，靠清 `dist/` + 精確選 wheel 保證乾淨安裝。

後續（D-20260603 期間）加上 archive commit 觸發的 auto-publish hook（`scripts/publish-cli-on-archive.py`），但手動 reinstall 路徑永遠保留。

## Rejected alternatives

- **editable install**：開發污染跨專案查詢。被否決的核心理由。
- **每個專案各自 `uv run` 進本 repo**：要求呼叫端知道 repo 路徑並自帶 venv，跨專案 subagent 的 Bash tool 用起來太重。

## Consequences

- 改了 CLI 但沒 reinstall → 全域 `bes` 與每日排程（吃全域 build 快照）仍是舊行為。這是刻意的，不是 bug。
- 每日 sync / archive-scrape 排程都以絕對路徑呼叫全域 `bes.exe`，因此同樣受此快照語義約束。

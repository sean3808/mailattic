---
id: D-20260602-1
date: 2026-06-02
status: accepted
title: 每日 IMAP 同步走 Windows-native Python，不 spawn container 或 WSL shell
---

## Context

最初的 IMAP 同步是 mbsync 跑在 Docker container 裡（WSL 內），由排程觸發 `docker compose run mbsync` + `run_sync.py`。

排程每天要開 WSL、開 container、掛 `/mnt/d`。三層之中每一層都會壞：WSL 沒起來、Docker daemon 沒起來、`/mnt/d` 的 reparse point 被污染（PC-017）。而且 `/mnt/d` 正是 D-20260525-1 禁止的寫入路徑。

## Decision

每日同步改成 **Windows Task Scheduler 直接觸發 `bes sync-imap`**（native Python `imaplib`，跑在 host）。

排程契約：**每日同步不得 spawn container 或 WSL shell**。archive-scrape 排程（`MailatticArchiveScrape`，2026-06-13 harden-sync-scheduling）後來也依同一條契約改成 Windows-native，以絕對路徑呼叫全域 `bes.exe`。

Docker / mbsync 路徑保留，但只供 PST backfill / 歷史匯入這類手動、重型、一次性的作業。

## Rejected alternatives

- **繼續走 container**：三層依賴鏈太脆，且踩在 `/mnt/d` 的儲存事故向量上。
- **在 WSL 內裝排程（cron）**：WSL 不常駐，開機不保證起來，排程可靠性比 Windows Task Scheduler 差。

## Consequences

- 排程吃的是**全域 `bes` build 快照**（D-20260527-1），所以改動 sync CLI 後必須 reinstall 才會反映到每日排程。
- 設計正典：`openspec/changes/archive/2026-06-02-add-native-windows-imap-sync/design.md`、`openspec/changes/archive/2026-06-13-harden-sync-scheduling/`。

---
id: D-20260603-2
date: 2026-06-03
status: accepted
title: bes db backup 產的是 staging 快照，不是 DR 終點
---

## Context

DB 是 WAL 模式的 live 檔（數 GB），直接 `cp` 會拿到不一致的快照。需要一個安全的備份指令。

但「備份指令」很容易被誤讀成「災難復原方案已經有了」。

## Decision

`bes db backup` 用 `sqlite3.Connection.backup()` 產**一致性單檔快照**，預設輸出到 DB 父目錄 `<db_dir>/backups/<timestamp>.db`。

明確定位：這是 **staging 快照，不是 DR 終點**。它防的是手滑刪檔 / 覆寫，**擋不住磁碟整體損壞**（快照跟原檔同一顆碟）。

真正的 DR 靠既有的「定期複製到外部儲存」人工流程。工具**不**自管 retention、**不**自動外部複製。

## Rejected alternatives

- **工具自帶 retention + 自動外部複製**：等於在 repo 裡長出一個備份系統。Sean 已有可運作的外部備份流程，工具去搶這件事只會多一個要維護的東西。
- **只提供 `cp` 說明**：WAL 模式下不一致，會產出看似成功實則壞掉的備份。

## Consequences

- 快照含完整郵件本文 + 附件 metadata（敏感資料）。`backups/` 已 git-ignored，但 `--output` 不要指向雲端 / 共享資料夾。
- DB 連同所有備份皆失時的深層 fallback 是 `bes rebuild`（從 PST 重建），正典程序見 `docs/rebuild-db-from-pst.md`。首選永遠是**還原備份**，不是 rebuild。

---
id: D-20260525-1
date: 2026-05-25
status: accepted
title: WSL 內禁止用 Linux 工具直寫 /mnt/<drive>，改走 robocopy 或 tar bundle
---

## Context

用 WSL 的 `rsync` 把 PST 抽出的 attachments / CAS 目錄（大量小檔）同步到 `/mnt/d/...` 時，D: 槽在 Windows 視角直接消失，NTFS 延遲寫入失敗、資料部分損毀。

I/O path 是 drvfs → Windows FS stack → 儲存控制器 → 磁碟。大量小檔的 metadata 操作經 drvfs 轉譯後，已驗證會觸發儲存控制器 timeout/reset。這不是「慢」，是硬體層掉線。

## Decision

寫入端在 WSL、目的地是 `/mnt/<drive>` → 一律禁用 `rsync` / `cp -a` / `tar -x` 這類 Linux 工具直寫。改用：

- Windows 端跑 `robocopy "\\wsl$\Ubuntu-24.04\..." "D:\..." /E /Z /R:2 /W:2 /MT:8`（不要激進參數）
- 或先在 WSL 打包成單一 `tar.gz`，Windows 端解壓

判斷標準不看檔案數量 —— 只看「寫入端在 WSL 且目的地是 /mnt」。

## Rejected alternatives

- **調 rsync 參數 / 降並行**：治標。drvfs 轉譯層本身就是風險源，減量只是降低觸發機率。
- **改儲存控制器的驅動設定**：動 storage driver 影響整台機器開機，成本遠高於換一條搬檔路徑。

## Consequences

- 重型 I/O（PST extraction、bulk backfill）改成純 WSL ext4 內完成，收工才一次性 robocopy 回 D:。
- 事故記錄與復原 protocol 留在本機，不在 repo 內。

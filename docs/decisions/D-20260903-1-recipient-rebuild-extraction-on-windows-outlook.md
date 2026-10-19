---
id: D-20260903-1
date: 2026-09-03
status: accepted
title: 收件人 rebuild 的抽取走 Windows 上的 Outlook COM，stage 落 Windows 本機；issue #175 的 WSL/ext4 data plane 邊界改寫
---

## Context

GitHub issue `#175`（收件人 rebuild：把 `#151` 的抽取層修正灌進正式庫）原文列了兩條與程式碼事實矛盾的條款：

1. **方法**：「本票不是要造新機制，是要用 `bes rebuild` 跑一次」。但 `rebuild.py` 的 import phase 寫死 `backend="readpst"`（`388fe7c` 刻意保留，`docs/cli-reference.md` 明寫「遷移 rebuild 是獨立工作單元」），照字面跑出來仍是舊的 display-string 抽取結果，`#151` 的修正不會進庫。
2. **雙環境邊界（票面標為硬性）**：「WSL data plane：解包 PST、重建來源證據一律落在 ext4」。但 `#151` 的抽取器是 `outlook_pst.OutlookComReader`（`win32com` → `Outlook.Application`），只能在 Windows、Outlook 執行中、PST 已掛載於 profile 時跑；`#151` D7 已查證 libpst / pypff 都讀不到 recipient table，沒有 WSL 可跑的替代抽取器。

Codex 語意 review 連續三輪以「plan 偏離票面硬性條款」擋 Track B，並正確指出這不是 agent 可以自行弱化的規則，必須由票主裁決。

## Decision

Sean 於 2026-09-03 拍板走 (A)「改票面」（記錄於 issue `#175` 留言「拍板：走 (A)」）：

- **抽取走 `bes import`（Outlook COM backend）在 Windows 執行**，不走 `bes rebuild`；「rebuild 遷移到 Outlook backend」另開票，不在 `#175`。
- **stage DB 與 dry-run 產物落 Windows 本機磁碟**，位置固定為 repo 的 `.project-runtime/recipient-rebuild/`（gitignored）；dry-run 預設不抽附件二進位（避免大量小檔 I/O）。審計腳本 `recipient_rebuild_audit.py`（本機工作檔）的 `dry-run` 對此路徑強制守門（其他位置 exit 2）。
- **兩條不變**：WSL 端不寫 `/mnt/<drive>`（[D-20260525-1](D-20260525-1-no-wsl-write-to-mnt-drive.md) 完全不受影響）；Windows control plane 對正式 DB 永遠唯讀（審計腳本以 `mode=ro` 開啟並附開關前後 stat，含非空 WAL 邊車）。
- apply 路徑（R 全量 rebuild／T 目標式收件人置換）**尚未拍板**，等 dry-run 的配對率出來再決定；少數 quoted-local atomic address 依票面逐字保留，T 的置換集合整封排除帶該列的 PST 家族訊息。

## Rejected

- **(B) 守票面字面**：先開「rebuild 遷 Outlook backend」票當前置，本輪縮成設計 + 基線 + 審計腳本、拿掉 dry-run。被否決的理由：前置票做完之後仍然碰到同一條 WSL 邊界（Outlook 不在 WSL），只是把裁決往後推。
- **維持邊界、另找 WSL 抽取器**：`#151` D7 已查證兩個開源 PST 讀取器都拿不到 recipient table，沒有候選。

## Consequences

- `docs/rebuild-db-from-pst.md` 的「純 WSL ext4 跑」前提對**收件人修正這條路**不適用；DR 全量 rebuild 仍走原 runbook，直到 rebuild 遷移 Outlook backend 的票落地（屆時該 runbook 要重寫）。
- 設計與 dry-run 程序正典：`docs/recipient-rebuild-plan.md`（本機工作檔，未入版控）（PR `#179`）。
- 「Outlook 沒在跑就拒絕」的守門目前只住在審計腳本；`outlook_pst._load_outlook_namespace()` 對任何呼叫者仍會靜默拉起 Outlook，是否下沉待另開票（plan §10）。

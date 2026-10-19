# 全域安裝、DB 路徑解析、每日排程與 CLI 發行

## 一次性設定（跨專案使用）

讓其他專案的 Claude Code / Codex / Copilot subagent 直接 `bes search ...` 而不必 cd 過來：

```powershell
# 1. 全域 build-snapshot install（清 dist → uv build wheel → uv tool install --force <wheel>，非 editable）
pwsh -ExecutionPolicy Bypass -File C:\IT_Projects\mailattic\scripts\install-global.ps1

# 2. 指向中央 DB（重開 shell 後生效）
setx MAILATTIC_DB C:\IT_Projects\mailattic\mailattic.db

# 3. 驗證（任意 cwd 開新 shell 跑）
bes search --keyword test --limit 1
```

移除：`uv tool uninstall mailattic`；清 env var 走 Windows 環境變數面板，或 `setx MAILATTIC_DB ""`。

**全域 = build 快照、開發 = working tree**：全域 `bes` 是 non-editable wheel 快照，不跟 repo working tree 走。開發本專案自身仍用 `uv run bes`。取捨理由見 [D-20260527-1](decisions/D-20260527-1-global-bes-is-build-snapshot.md)。

## 路徑解析

**DB 路徑 fallback 順序**：`--database <path>` flag > `MAILATTIC_DB` env var > absolute default 指向 repo root（`C:\IT_Projects\mailattic\mailattic.db`）。空字串或 whitespace-only env var 視為 unset，退回 absolute default。

**`bes sync-imap` 各 default 寫死指向 repo root，呼叫端 cwd 不影響行為**：`env_file` / `maildir_root` / `log_dir` 三個 active sync default 都是 absolute path，從任意 cwd 呼叫（含跨專案 subagent 與 Windows Task Scheduler）行為一致。要切位置請走顯式 flag（`--env-file` / `--maildir-root` / `--log-dir` / `--database`）或 env var。此設計是有意為之，避免 default 路徑被 `.resolve()` 接到呼叫端 cwd 而失敗或產出 stray 目錄。

## 每日 IMAP sync 排程（Windows Task Scheduler）

排程直接呼叫**全域 `bes.exe sync-imap`**（native，不走 Docker / WSL；source-of-truth 是 `scripts/mbsync-daily.xml`，安裝時代換 placeholder 並轉 UTF-16 LE BOM 後 `schtasks /create /xml`）。`BesPath` 未指定時由 `uv tool dir --bin` 解析，路徑不存在直接 fail loud —— 先跑 `install-global.ps1`。

```powershell
pwsh -ExecutionPolicy Bypass -File scripts/install-task-scheduler.ps1
# 預設每日 08:45 (local)；-StartTime "09:00" 可調；-Uninstall 移除

Get-ScheduledTaskInfo -TaskPath '\MyTasks\' -TaskName 'mailattic mbsync daily'   # LastRunTime / LastTaskResult
Start-ScheduledTask   -TaskPath '\MyTasks\' -TaskName 'mailattic mbsync daily'   # 手動觸發測試
# 觸發後檢查 .mailattic-work/logs/mbsync-<今日>.log 應出現新一行五欄位
```

不走 container 內 cron 的理由：WSL2 distro idle shutdown 會 SIGTERM 所有 long-running container（出處是 Spectra 時期 `add-mbsync-imap-sync` change 的 design Decision 5，不隨本 repo 帶出）。日常 sync 已不需 Docker（[D-20260602-1](decisions/D-20260602-1-native-windows-daily-sync.md)），Docker/mbsync 路徑僅保留給 PST backfill。

## 發行流程

**⚠️ 因果方向（最常記反的一條）：打 tag 不會觸發任何 rebuild。** 反過來 —— 先由 `release.ps1` 建好並寫下 release evidence，pre-push guard 才放行那個 tag。手動 `git tag` + `git push` 一定被擋：

```
[bes-release-guard] BLOCKED bes-vX: release tags must be annotated and created by the release workflow
[bes-release-guard] Recovery: pwsh -ExecutionPolicy Bypass -File scripts/release.ps1 -Tag bes-vX
```

guard（`scripts/guard-release-tag.py`，掛 pre-push）是**唯讀 fail-closed 擋門，只擋不建**；它的存在就是防止手打 tag 假裝發行過。

**要哪一個？** 只想本機／本 repo 用到新行為 → 什麼都不用做（`uv run bes` 吃 working tree）。想讓**其他專案的 subagent** 吃到新行為 → 必須發行，因為全域 `bes` 是 build 快照。

| 情境 | 指令 |
|---|---|
| 正式發行（從乾淨 `main`） | `pwsh -ExecutionPolicy Bypass -File scripts\release.ps1 -Tag bes-vYYYY.MM.DD.N` |
| 未定案的本機更新（只重建全域 bes，不寫 evidence / 不建 tag / 不 push） | `pwsh -ExecutionPolicy Bypass -File scripts\reinstall-global.ps1` |
| 新 checkout 裝 guard（一次） | `pwsh -ExecutionPolicy Bypass -File scripts\install-release-pre-push.ps1` |

- tag 格式固定：日期 + 當日序號。`release.ps1` 一支包辦：完整測試 → non-editable reinstall → `bes --help` + package manifest 驗證 → skill SSOT 同步與逐檔核對 → Git-local evidence → annotated tag → atomic push。
- **發行完成包含 skill**：同一個乾淨 main commit 的 BES、`skills/mailattic/` 操作說明與附帶工具一起交付。release 呼叫 `deploy-skill-to-ssot.py --verify-required`，只部署 Git tracked 檔案，再核對內容；目標沿用 `MAILATTIC_SKILL_SSOT` 或部署腳本內建 SSOT 路徑。ignored/untracked 產物不複製，SSOT 清單外檔案不動，也不 commit/push SSOT repo。
- **SSOT 階段失敗即停止**：複製失敗、來源檢查而跳過、同步後內容仍不同，都不得新增成功 evidence/tag/push。此時 BES 已安裝，不會自動回復；修正錯誤後以原 `-Tag` 和 `-Remote` 重跑 release。已一致的 skill 可安全重驗。
- **普通 `git push` 只是遠端備份，不代表 CLI 發行。** 因為要求乾淨 main，**文件／程式碼改動要先 commit 完再發行**，tag 才會指向含該改動的 commit。
- pre-commit framework 會接管 `.git/hooks/pre-push`，guard 以 `pre-push.legacy` 形式被串接執行。
- **main commit hook 是即時同步，不能取代 release 核對**：Spectra `archive` 觸發已退役，現行 hook 在 main commit 後同步 skill（[D-20260820-4](decisions/D-20260820-4-skill-ssot-deploy-trigger-main-commit.md)），但不發布全域 CLI。正式 release 自己確認 SSOT 一致；不走 GitHub Actions / self-hosted runner，也不依 tag 修改 `pyproject.toml` package version。

## 暫存 maildir 與憑證檔

- **`.mailattic-work/maildir/` 是 7 天暫存跳板**：sync 後 prune mtime > 7 天且已 durable 進 DB 的 `.eml`（失敗的保留）；DR 靠 PST + DB 備份。手動 mbsync / PST backfill 用隔離的 work maildir（顯式 `--maildir-root`）（[D-20260703-1](decisions/D-20260703-1-maildir-is-7day-scratch.md)）。
- **憑證檔**：outbound 三命令（`draft-reply` / `send-draft` / `send-batch`）的寄件設定檔比照 DB 路徑解析：`--env-file` > `MAILATTIC_SMTP_ENV_FILE` > 寫死指向 repo 根 `.env.sean3808` 的 absolute default；寄件帳號靠呈審時的 `From:` 被看見（[D-20260915-1](decisions/D-20260915-1-smtp-settings-file-resolved-by-bes-with-from-shown-at-review.md)）。`MAIL_SERVER_ENV_PATH` 只在 WSL 內手動跑 `bes scrape-archive` 時需要（排程已停用）。

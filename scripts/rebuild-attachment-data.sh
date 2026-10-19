#!/usr/bin/env bash
#
# scripts/rebuild-attachment-data.sh
#
# 編排 mailattic 一次性 rebuild 的 WSL 側流程。
# Windows 側動作（關 Outlook / disable scheduled tasks / re-copy hot PSTs /
# swap DB / robocopy CAS / re-enable tasks / 重啟 Outlook）由操作員手動執行；
# 本 script 只負責 WSL 內可自動化的部分，並在 stdout 印明確的操作員提示。
#
# Usage（WSL 內）:
#   DRY_RUN=1 bash scripts/rebuild-attachment-data.sh   # 預演，不實際 ingest
#   bash scripts/rebuild-attachment-data.sh             # 正式跑
#
# 對應 design Decision: Scripts 放 scripts/ 目錄，不放 src/ tree
#       design Decision: 不切 feature branch
#       design Migration Plan 全步驟
#
set -euo pipefail

# ---- 可調 env var（覆寫預設值） ----
REPO="${REPO:-/root/projects/mailattic}"
WORK_DIR="${WORK_DIR:-$REPO/work}"
PST_DIR="${PST_DIR:-$WORK_DIR/pst}"
STAGE_DB="${STAGE_DB:-$WORK_DIR/stage.db}"
ATTACHMENT_ROOT="${ATTACHMENT_ROOT:-$REPO/attachments}"
PRE_REBUILD_SOURCE_DB="${PRE_REBUILD_SOURCE_DB:-/mnt/d/IT_Projects/mailattic/mailattic.db.pre-rebuild-2026-05-27.bak}"
MAILDIR_ROOT="${MAILDIR_ROOT:-$WORK_DIR/maildir-stage}"
MBSYNC_TEMPLATE="${MBSYNC_TEMPLATE:-$REPO/docker/mbsyncrc.template}"
MBSYNC_CONFIG="${MBSYNC_CONFIG:-$WORK_DIR/mbsync/mbsyncrc-stage}"
MBSYNC_LOG_DIR="${MBSYNC_LOG_DIR:-$WORK_DIR/logs}"
DRY_RUN="${DRY_RUN:-0}"

# 4 個 PST 預期檔名（hot 兩個 + cold 兩個）
HOT_PSTS=(
  "sean3808@mailattic.test.pst"
  "archive.pst"
)
COLD_PSTS=(
  "archive_2020-2023.pst"
  "archive_2024-2025.pst"
)

# ---- 工具 ----
say() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '    [WARN] %s\n' "$*" >&2; }
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '    [dry-run] %s\n' "$*"
  else
    printf '    [exec] %s\n' "$*"
    eval "$@"
  fi
}

# ---- Step 1: Pre-flight banner + operator gate confirmations ----
say "[1/9] Pre-flight banner"
note "REPO=$REPO"
note "WORK_DIR=$WORK_DIR"
note "STAGE_DB=$STAGE_DB"
note "ATTACHMENT_ROOT=$ATTACHMENT_ROOT"
note "PRE_REBUILD_SOURCE_DB=$PRE_REBUILD_SOURCE_DB"
note "DRY_RUN=$DRY_RUN"
note ""
note "操作員必須在執行本 script 前確認下列 Windows 側 gate（task 1.1 / 3.1 / 3.2）："
note "  - System Event Log 近一日無新 disk error：Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='disk','iaStorVD','stornvme','storahci'; Level=2; StartTime=(Get-Date).AddDays(-1)} 應為空"
note "  - Outlook 已關閉（Task Manager 確認 OUTLOOK.EXE 不存在）"
note "  - 兩個 scheduled task 已 disabled："
note "      schtasks /change /tn \"mailattic mbsync daily\" /disable"
note "      schtasks /change /tn \"MailatticArchiveScrape\" /disable"
note "  - 兩個 hot PST 已 robocopy 進 $PST_DIR/（與本機最新版對齊）"

# ---- Step 2: WSL 工作目錄 + PST 完備性檢查 ----
say "[2/9] WSL 工作目錄與 PST 完備性"
MBSYNC_CONFIG_DIR="$(dirname "$MBSYNC_CONFIG")"
run "mkdir -p \"$WORK_DIR\" \"$PST_DIR\" \"$ATTACHMENT_ROOT\" \"$MAILDIR_ROOT\" \"$MBSYNC_LOG_DIR\" \"$MBSYNC_CONFIG_DIR\""

if [[ "$DRY_RUN" != "1" ]]; then
  missing=0
  for pst in "${HOT_PSTS[@]}" "${COLD_PSTS[@]}"; do
    if [[ ! -f "$PST_DIR/$pst" ]]; then
      warn "缺少 PST：$PST_DIR/$pst"
      missing=$((missing + 1))
    else
      note "✓ $pst（$(stat -c '%s bytes  mtime=%y' "$PST_DIR/$pst")）"
    fi
  done
  if (( missing > 0 )); then
    warn "$missing 個 PST 缺失。Hot PST 由操作員 robocopy 進來；cold PST 應已在 WSL 先前那批。請補齊後重跑。"
    exit 2
  fi
else
  for pst in "${HOT_PSTS[@]}" "${COLD_PSTS[@]}"; do
    note "[dry-run] 預期 $PST_DIR/$pst 存在"
  done
fi

# ---- Step 3: Fresh stage.db init ----
say "[3/9] Fresh stage.db init（對應 design Decision: Pure WSL ext4 hot path，禁 /mnt/c /mnt/d 進 hot path）"
if [[ -f "$STAGE_DB" ]]; then
  warn "$STAGE_DB 已存在 — 預期是 fresh init。請手動 mv / rm 後重跑（避免誤刪 forensic）。"
  exit 2
fi
run "cd \"$REPO\" && uv run bes init-db --database \"$STAGE_DB\""

# ---- Step 4: Serial 4-PST ingest（無 --parallel） ----
say "[4/9] Serial 4-PST ingest（對應 design Decision: 4 PSTs 序列 ingest，禁用 parallel flag）"
note "PC-005: readpst -j 在 libpst 5.x 路線下會 deadlock / partial extract，禁用 --parallel"
for pst in "${HOT_PSTS[@]}" "${COLD_PSTS[@]}"; do
  say "  -> ingest $pst"
  run "cd \"$REPO\" && uv run bes import --database \"$STAGE_DB\" --file \"$PST_DIR/$pst\" --attachment-root \"$ATTACHMENT_ROOT\""
done

# ---- Step 5: mbsync 進 stage.db（獨立 channel 不動 production） ----
say "[5/9] mbsync 進 stage.db（對應 design Open Question Q3 resolution）"
note "用獨立 mbsync config (\$MBSYNC_CONFIG) + 同 IMAP server，但 database 寫 stage.db。"
note "Maildir 寫 \$MAILDIR_ROOT/（與 production maildir 隔離）。"
note ""
note "操作員預先要備好："
note "  - \$MBSYNC_CONFIG 指向 IMAP server credentials（可從 $REPO/docker/mbsyncrc.template 派生）"
note "  - \$MBSYNC_TEMPLATE 存在（預設 $REPO/docker/mbsyncrc.template）"
run "cd \"$REPO\" && uv run python docker/run_sync.py \\
  --database \"$STAGE_DB\" \\
  --maildir-root \"$MAILDIR_ROOT\" \\
  --template-path \"$MBSYNC_TEMPLATE\" \\
  --config-path \"$MBSYNC_CONFIG\" \\
  --log-dir \"$MBSYNC_LOG_DIR\" \\
  --attachment-root \"$ATTACHMENT_ROOT\" \\
  --full-resync"

# ---- Step 6: Archive-scraper outbound rows scripted transplant ----
say "[6/9] Archive transplant（對應 design Decision: Archive-scraper outbound rows 用 scripted transplant，禁 bulk SELECT-INSERT）"
if [[ ! -f "$PRE_REBUILD_SOURCE_DB" ]]; then
  warn "缺少 source DB：$PRE_REBUILD_SOURCE_DB"
  warn "  該檔應為 swap 前 main DB 的 snapshot。操作員若還沒做 snapshot，必須先："
  warn "  Windows 側 cmd: copy \"C:\\IT_Projects\\mailattic\\mailattic.db\" \"C:\\IT_Projects\\mailattic\\mailattic.db.pre-rebuild-2026-05-27.bak\""
  exit 2
fi
say "  -> dry-run"
run "cd \"$REPO\" && uv run python scripts/transplant-archive-rows.py --source-db \"$PRE_REBUILD_SOURCE_DB\" --target-db \"$STAGE_DB\" --dry-run"
say "  -> commit"
run "cd \"$REPO\" && uv run python scripts/transplant-archive-rows.py --source-db \"$PRE_REBUILD_SOURCE_DB\" --target-db \"$STAGE_DB\""

# ---- Step 7: Verify-rebuild 五項檢查 ----
say "[7/9] Verify-rebuild（對應 design Decision: Verify-before-swap 五項檢查必須全綠才 swap）"
note "此 wrapper 屬已退役的 WSL 路線；現行流程見 docs/rebuild-db-from-pst.md 步驟 2。"
note "verify-rebuild 需要操作員核可的來源筆數基準檔（BASELINE=<path>）；沒有基準不會給 PASS，也不可換檔。"
if [[ -z "${BASELINE:-}" ]]; then
  warn "未設定 BASELINE，不執行 verify-rebuild（沒有基準的結果不能當換檔證據）。請設 BASELINE 後重跑。"
  exit 2
fi
run "cd \"$REPO\" && uv run python scripts/verify-rebuild.py --db \"$STAGE_DB\" --attachment-root \"$ATTACHMENT_ROOT\" --baseline \"$BASELINE\""

# ---- Step 8: Operator hand-off — swap + robocopy + re-enable + restart Outlook ----
say "[8/9] Operator hand-off：Windows 側收尾"
note "如果 Step 7 印 overall=PASS 才繼續下列動作；FAIL 則 STOP，不要 swap。PASS 不代表逐封保全，收件人抽樣仍須人工驗收。"
note ""
note "Windows 側 PowerShell（依序執行）："
note ""
note "  # 8.1 確認無長住 bes/agent/search process 鎖 DB（避免 sharing violation）"
note "  Get-Process | Where-Object {\$_.ProcessName -match 'bes|claude|cursor'} | Format-Table"
note ""
note "  # 8.2 Snapshot + swap"
note "  Rename-Item 'C:\\IT_Projects\\mailattic\\mailattic.db' \\"
note "              'mailattic.db.pre-rebuild-2026-05-27.bak'"
note "  Copy-Item '\\\\wsl.localhost\\Ubuntu-24.04$STAGE_DB' \\"
note "            'C:\\IT_Projects\\mailattic\\mailattic.db'"
note ""
note "  # 8.3 Robocopy CAS（對應 design Decision: Windows 側 robocopy CAS，禁 WSL 寫 /mnt/d）"
note "  robocopy '\\\\wsl.localhost\\Ubuntu-24.04$ATTACHMENT_ROOT' \\"
note "           'C:\\IT_Projects\\mailattic\\attachments' \\"
note "           /E /Z /R:2 /W:2 /MT:8"
note ""
note "  # 8.4 Re-enable scheduled tasks"
note "  schtasks /change /tn 'mailattic mbsync daily' /enable"
note "  schtasks /change /tn 'MailatticArchiveScrape' /enable"
note ""
note "  # 8.5 重啟 Outlook，手動確認 sean3808 mailbox 可讀"

# ---- Step 9: Post-swap spot-check（操作員在 Windows 側跑） ----
say "[9/9] Post-swap spot-check（Windows 側執行）"
note "  uv run bes search --keyword \"<信庫裡確定存在的詞>\" --limit 3   # 要有結果；0 筆視為換檔失敗"
note "  uv run python -c \"import sqlite3; c=sqlite3.connect(r'C:\\IT_Projects\\mailattic\\mailattic.db'); print(c.execute('SELECT source_input_mode, COUNT(*) FROM messages GROUP BY source_input_mode').fetchall())\""
note ""
note "兩項結果合理才視同 rebuild 完成。若有任何 anomaly，依 design Migration Plan rollback steps：將失敗版本 rename，copy snapshot 回原位，re-enable scheduled tasks。"

say "DONE — WSL 側 step 1..7 已完成，operator 側 step 8..9 待手動執行。"

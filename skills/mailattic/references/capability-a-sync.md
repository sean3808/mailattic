# Capability A — Sync（拉新鮮信件）

> 此檔由 `SKILL.md` 的 Capability 路由表 MANDATORY load 進來。
> 若任務只跑 Sync，**不要載入** `capability-b-find.md` / `capability-c-reply.md`。

## 一條軌道：sync-imap 同時帶收件與寄件（2026-07-14 起）

| 方向 | 來源 | 涵蓋期間 |
|---|---|---|
| **inbound** | `bes sync-imap`（INBOX） | 連續，無缺口 |
| **outbound** | `bes sync-imap`（IMAP `寄件備份` folder） | **2026-07-14 起**；2026-05-27 以前靠 PST 匯入 |

Outlook 2026-07-14 改走 IMAP 後，寄出的信落在 server 的 `寄件備份` folder，`sync-imap` 直接同步、帶完整原始 header（`docs/decisions/D-20260714-1-imap-sent-is-sole-outbound-source.md`）。`bes scrape-archive` 的排程已停用、降為手動備援（只在 WSL 內人工跑），**不是** caller agent 的常規 outbound 路徑，不要自動呼叫。

殘留：沒經過 Outlook 寄出的信不會進 `寄件備份`。

## 何時用

- User query 涉及 **24 小時內** 的信件（「今天有沒有收到」「剛剛某某有寄信給我嗎」「最新」「最近一兩天」）→ 先跑 `--pre-task` 再進 Capability B
- User 找的是自己**寄出**的信（「我剛寄給 X 的信」「我今天寄出那封」）→ 一樣跑 `--pre-task`（`寄件備份` 由它同步）
- User 明確說「同步」「拉信」「更新 DB」「sync mailbox」
- Capability B 找信時發現 `SELECT MAX(sent_at) FROM messages` 跟今天差超過 1 天，且 query 對時效性敏感
- **不要無條件每次都跑**：歷史檢索（「以前那封」「2024 年某月」）DB 已涵蓋，sync 純浪費

## 怎麼用

正常 caller agent 入口用 `--pre-task`，內建 15 分鐘 throttle 安全可重複呼叫：

```bash
bes sync-imap --pre-task
# 或以 JSON 輸出：
bes sync-imap --pre-task --json
```

`--pre-task` 行為：若上次 sync **嘗試**距現在 < 15 分鐘，直接 skip 印 throttle 訊息；否則跑正常 sync。設計目的是讓 caller 不用自己判斷「該不該 sync」，呼叫即可。加 `--json` 旗標（只能搭配 `--pre-task`，單獨用會 exit 2）則以 JSON 輸出結果。`status` 為 `skipped` / `synced` / `failed`（設定錯誤）；`skipped` 時固定帶 `last_sync_failed`（bool）、`last_sync_exit_code`、`last_sync_failure_stage`、`last_sync_failure_message`（不適用時為 `null`），只看 `last_sync_failed` 即可。`synced` / `failed` 帶 `exit_code`。

**「嘗試」不是「成功」** —— throttle 讀的是上次 run 的結束時間，不篩 exit status，所以**失敗的 run 一樣會讓接下來 15 分鐘的呼叫被 skip**。上一次同步失敗時，skip 輸出會帶上 `last sync failed`；後來又有一次同步開始卻沒有完成記錄（寫紀錄前就中斷，或仍在跑）時帶 `last sync did not complete`；同步狀態讀不到時帶 `last sync status unknown`（JSON 三者都是 `last_sync_failed: true`，stage 分別為記錄值 / `unfinished` / `state_unreadable`）。caller 只有在「略過且三者皆無」時才能把查無當成真的沒有，否則代表信庫可能不完整。下面「IMAP timeout 後重試」那條要照它寫的等 5-10 分鐘以上，馬上重打只會拿到 throttle 訊息而不是一次真正的重試。

需要先估量會抓幾封：

```bash
bes sync-imap --dry-run
```

只算數，不寫 DB；用在 sync 量看起來很大時先看再決定。

bulk_guard 卡住時（例：某 folder UIDs 超過 500 threshold，正常 sync 會 exit=1 + 印 `bulk_guard` 訊息）才用 `--full-resync`：

```bash
bes sync-imap --full-resync
```

會有 confirmation prompt（要 typed 確認）+ 清掉 JSON 水位鏡像（DB 內 `imap_sync_progress` 不先砍，隨重抓推進被覆寫）+ 全量重抓。**這是 escape hatch，不是常規路徑**。

## 失敗模式

- **`bulk_guard 'INBOX': N UIDs exceeds bulk threshold 500`** → 不是 sync 壞掉，是 guardrail 主動擋。先看 N 是多少：< 1000 可考慮 `--full-resync` 確認過後跑；>= 1000 先回報 user 評估是不是 mailbox 出事
- **`exit=1` 且訊息含 IMAP timeout / connection reset** → 網路或 server 抖動；過 5-10 分鐘重試 `--pre-task`，throttle 會避免 burst
- **`exit=1` 且某 UID 訊息無法入庫（message-level ingest 失敗）** → sync 現在會**故意停在該 UID**（fail-loud）：watermark 不越過、失敗記進 `ingest_failures`、下次 sync 從同一 UID 重試。這是刻意設計（擋靜默漏信），不是 sync「壞掉」；若同一 UID 每次都卡 → 該信可能永久無法解析，回報 user 由人工介入（修根因或人工推進 watermark），不要自動繞過。附件解碼失敗**不會**觸發此停止（信件本體仍入庫）
- **`exit=1` 且訊息為 `list: skipped folder: LIST returned a folder name that is not ASCII ...`**（`--dry-run` 也會印 `ERROR: skipped folder` 並 exit 1）→ 伺服器用 literal 寫法送出了一個解不開的資料夾名稱；只有那個資料夾被跳過，其餘資料夾照常同步、信照常進庫。這次 run 記為失敗（sync 紀錄 `failed`），水位線沒動；回報 user 該資料夾名稱由人處理，不要自動重試
- **DB 剛從備份還原 / 換了另一顆 `--database`** → 同步進度存在 DB 內（`imap_sync_progress`），不是只看 maildir 旁的 `.imap_watermarks.json`；JSON 比 DB 新時不被信任，下次 sync 會自動補回缺信，不需手動動 watermark；舊版遺留、未綁定 DB 的 JSON 只採用到該 DB 自己實際持有的最大 UID 為止，該資料夾在 DB 內一封都沒有就從 UID 1 重掃。刪掉或弄丟 JSON 鏡像也會從 UID 1 重掃。DB 內有進度、JSON 鏡像也在時，缺口再大也直接補抓；只有從 UID 1 重掃（該資料夾在 DB 內沒有進度，或 JSON 鏡像遺失）且超過 500 封時才會 `bulk_guard` 非零退出，那時才走 `--full-resync`
- **`exit=0` 但 `new=0 deduped=0`** → 正常無新信，不是失敗
- **完全沒 log**（例：`.mailattic-work/logs/mbsync-<today>.log` 不存在）→ Windows Task Scheduler 沒觸發；不是 sync 本身問題，是排程層；提醒 user 檢查 Task Scheduler 狀態，不要在 skill 內想辦法繞過

- **附件寫入失敗的信**（`attachment-retry:` 行的 `still failing` > 0，或 `bes db doctor` 的 `unresolved_attachment_failures` > 0）→ 每次 sync 開頭會自動用本機原始信重試；原始信超過七天會被搬進隔離區（預設在資料庫旁的 `.mailattic-work/quarantine/<資料庫檔名>/`，跟著資料庫走；doctor 的 `quarantined_mail` 數的是該目錄下所有 `.eml`）而不是刪掉，直到補回或被放棄。修不回來的那筆：`bes db doctor` 列出 `id=`，**由人**決定後下 `bes db abandon-attachment-failure --id <id> --reason "..."`（破壞性，需 user 確認；agent 非互動環境不可自行加 `--yes`，系統也從不自動放棄）

不要試圖在 sync 失敗時自動 retry 多次或自動升 `--full-resync` — 那是不可逆 + 大量 IO 操作，必須 user 授權。

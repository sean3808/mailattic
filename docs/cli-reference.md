# `bes` CLI 參考（穩定面）

> 這份列「已 merge 到 main 並穩定的子命令」。in-progress change 的子命令在該 change 的 issue / PR 內描述，不寫這裡 — 避免 doc 與 branch HEAD 漂移。
>
> 權威 flag 列表以 `uv run bes <cmd> --help` 為準；這份只列**用途**與**主要 flag**，以及 grep 不到的**行為契約**。
>
> **沒有 CHANGELOG.md（刻意）**：現狀能力以 skill + `bes <cmd> --help` 為正典；變更史查 PR / `git log`（該決策當時指定為變更史正典的 change archive 不隨本 repo 帶出）。理由見 [D-20260609-1](decisions/D-20260609-1-no-changelog-file.md)。

## 子命令表

| 子命令 | 用途 | 主要 flag |
|---|---|---|
| `init-db` | 建 schema（首次部署或新 DB） | `--database <path>` |
| `import` | 透過已啟動 Outlook profile 的 recipient table，從已掛載 PST 匯入郵件到 DB；或用 `--maildir` 從本機 maildir 匯入（不需 Outlook / readpst） | `--file <file>`（可重複）/ `--directory <dir>`（可重複）/ `--maildir <account-root>` / `--account-email <addr>` / `--database <path>` / `--attachment-root <path>` / `--json` |
| `search` | FTS5 + 結構化欄位混合查詢；`--full-body --json` 時升級成 ingestion-grade 輸出 | `--keyword` / `--sender` / `--recipient` / `--sender-domain` / `--recipient-domain` / `--participants-domain` / `--from-date` / `--to-date` / `--attachment-name` / `--limit N` / `--json` / `--full-body` |
| `attachments` | 附件優先查詢／匯出：一列一附件，支援檔名/副檔名/domain/date filter；加 `--copy-to` 走 CAS 匯出 | `--name-contains` / `--ext` / `--participants-domain` / `--sender-domain` / `--recipient-domain` / `--from-date` / `--to-date` / `--dedupe content-hash` / `--copy-to <dir>` / `--overwrite` / `--skip-existing` / `--json` / `--attachment-root` |
| `show` | 取單封信完整內容（subject / body / recipients / attachments）；`--format text --new-only` 僅保留新內容（去除引用歷史；支援置頂回覆、下方回覆與夾敘回覆）；保守 fallback：無法識別邊界時回傳全文不截斷 | `<message_id>` / `--format raw\|json\|text` / `--json` (alias) / `--new-only`（僅 `--format text` 有效；搭配 `raw`/`json` exit 非 0） |
| `thread` | 重建同 thread 全部信件按時序排（走 in_reply_to + references_header）；`--format text --new-only` 每封只保留新內容；保守 fallback 同上 | `<message_id_or_db_id>` / `--format raw\|json\|text` / `--new-only` |
| `sync-imap` | 從 IMAP server 增量同步（mbsync container 走 `run_sync.py`，這支是 CLI fallback） | `--account <name>` / `--database <path>` / `--pre-task`（caller 任務前主動同步，含 15 分鐘最小間隔閘門：距上次同步 < 15min 則 skip，避免連續任務 burst sync；上次若失敗，skip 輸出會帶 `last sync failed`；之後有 run 沒記到完成帶 `last sync did not complete`；狀態讀不到帶 `last sync status unknown`）/ `--json`（僅搭配 `--pre-task`：以 JSON 輸出結果；未帶 `--pre-task` 時 exit 2） |
| `draft-reply` | 對 DB 內某封信產生 **reply-all** 草稿（原寄件人非自己時進 To + 原 To/CC 去重排自己、In-Reply-To + References 接續、bracketed Message-ID、BCC 寄件人自己），寫 `.eml` 到 `outbound/drafts/`，預設只產不送；stdout 印 draft_id / From / Subject / To / Cc / Bcc / Message-ID / 附件（名稱與位元組數）/ `fingerprint:`（草稿 `.eml` 全 bytes 的 SHA-256，供呈審後寄出用） | `<message_id>` / `--body-from <file>` / `--attach <path>`（可重複）/ `--subject <text>`（選填，原樣取代預設的「Re: 原主旨」，不影響 thread header）/ `--env-file <smtp-settings-path>`（選填，解析順序見下方「outbound 發信運維前提」）|
| `send-draft` | 經 SMTP submission 送出草稿。授權（契約 #2）二擇一：`--fingerprint <sha>`（使用者看過草稿並說寄之後帶上；與當下草稿不符即拒寄、不寫 marker；不跳提示），或不帶指紋在**真 TTY** 輸入非空 typed confirmation。兩者皆無（例：agent shell 工具、Claude Code 的 `!`）拒寄並列出兩條出路；指紋以外沒有非互動 send flag。送出 Bcc 不上線但 envelope 含自己、寫一列 `pst_source='outbound-bes'` 紀錄到 DB（若有附件一併寫入附件庫與 DB 附件列）；草稿摘要含 From。SMTP 在 RCPT 階段拒了部分收件人（`#224`）：stdout `Partially sent: message_id=...`、stderr 列被拒地址與 code/reason、**exit 3**；信已寄出、marker 為 `sent`（記下 refused），不可整封重寄；全部被拒仍是 exit 1 拒寄。伺服器已接受 DATA 但結束連線（QUIT）失敗（`#230`）：照常印 `Sent: message_id=...`、**exit 0**，stderr 多一行 `SMTP close failed after accept: ...`；信已寄出並記為 `sent`，不可重寄。marker 停在 `sending`（上次寄送沒走完）時拒寄，訊息帶 marker 路徑與處理步驟（同 `list-drafts` 列）；SMTP 登入被拒時訊息明說信沒寄出（`#304`） | `<draft_id>` / `--fingerprint <sha256>` / `--env-file <smtp-settings-path>`（選填）|
| `send-batch` | 單次授權後依序批次送出多個草稿（per-draft 失敗隔離；send-once marker 狀態機沿用 `send-draft` 契約）。每個參數寫成 `<draft_id>=<fingerprint>`（全部帶或全部不帶；逐封驗，不符只拒該封、其餘照寄、不跳提示），或全不帶改走單次 TTY 授權。摘要含 From，並對 From/Subject/收件人/附件名/draft_id 做 terminal-safe escaping（中和 ANSI 控制字元、保留 CJK），且顯示的收件者集合＝實際 SMTP envelope 集合。逐封 outcome：`sent` / `partial`（已寄出但部分收件人被 SMTP 拒收，`#224`；stderr 列 `Partial:` 與被拒地址 code/reason，整體 exit 1，不可整封重寄）/ `skipped`（`sending` 的 reason 帶 marker 路徑與處理步驟，`#304`）/ `failed`（DATA 已接受但收尾 QUIT 失敗仍算 `sent`，stderr 另印 `SMTP close failed after accept`，`#230`） | `<draft_id[=sha256] ...>` / `--database <path>` / `--env-file <smtp-settings-path>`（選填）|
| `list-drafts` | 列出 `outbound/drafts/` 待送草稿（draft_id / 收件人 / 主旨 / 建立時間）；已送草稿留檔標記不再列為待送；寄送失敗後停在 `sending` 的草稿多帶 `status: sending`、marker 路徑與處理步驟（`--json` 的鍵為 `status` / `marker_path` / `next_step`）。處理原則：信可能已寄給部分或全部收件人、也可能還在寄；只有確定沒有寄送還在跑、且沒有任何一位 To/Cc/Bcc 收到，才刪 marker 重寄，任一人收到或不確定就保留 marker、不重寄 | `--database <path>`、`--json` |
| `contact-history` | 對單一聯絡人 email 或公司網域的歷年往來**聚合摘要**（時間跨度 / 收發比例 / 聯絡窗口清單 / 附件概覽），輸出單一摘要物件供 AI agent 消費；direction 由 self-address 判定、contacts 排除 bcc 與 self；`total_messages = inbound_count + outbound_count + unknown_count`，寄件人缺漏或空白的信計入 `unknown_count`、不算 inbound | `--contact <email>` 與 `--domain <domain>`（互斥擇一必填）/ `--date-from` / `--date-to`（YYYY-MM-DD）/ `--format table\|json`（table 預設＝TOON）/ `--attachments-limit N`（預設 20）/ `--database <path>` |
| `identity` | 宣告哪些 email 地址是「我」（`add` / `remove` / `list`，皆輸出 JSON）。集合存 `self_addresses` 表，是 CLI / MCP / Python API 共用的單一來源；**集合為空就是未宣告，程式碼沒有內建地址可退**（`#268`）：未宣告時沒有信會被判成自己寄的，`draft-reply` 讀不到寄件設定時也擬不出草稿。`list` 的 `source` 欄是 `database`（已宣告）或 `unset`（未宣告，`addresses` 為空） | `identity add <email>` / `identity remove <email>` / `identity list` / `--database <path>` |
| `db backup` | 為 WAL 模式 live DB 產**一致性快照單檔**（`sqlite3.Connection.backup()`，非複製 .db）；預設輸出 `<db_dir>/backups/<timestamp>.db`；temp→atomic rename、拒覆寫既有、拒 output==source、設 busy_timeout 容忍並行 sync | `--database <path>` / `--output <path>` |
| `db doctor` | 低成本健檢：`PRAGMA integrity_check` / `quick_check` / `journal_mode` / `foreign_key_check`；另印 `unresolved_attachment_failures: <N>`（`ingest_failures` 裡未解決的附件失敗，含沒有檔名與大小的舊紀錄；其下逐筆列 `id=` / 信的 locator / 檔名 / 大小，全部列出，供逐筆放棄；沒有 `ingest_failures` 附件欄位的舊資料庫也如實列出，且不會被改寫）與 `quarantined_mail: <N>`（隔離區根目錄下所有 `.eml` 檔數，不論資料夾形狀）。**任一問題或未解決筆數 > 0 → exit 1**（既有慣例：有發現就非 0；無法執行 exit 2）；隔離區信件數只是資訊、不影響 exit code | `--database <path>` / `--quarantine-root <path>`（預設是資料庫旁的 `.mailattic-work/quarantine/<資料庫檔名>/`，跟著資料庫走）|
| `db abandon-attachment-failure` | 人工放棄一筆修不回來的附件失敗（`#256`；系統從不自動放棄）：標記已放棄並記理由與時間，之後不再重試、不再計入 doctor；該信在隔離區的原始檔，於沒有其他未解決失敗時刪除（還有則保留）。**破壞性**，比照 `db prune-cas`：真 TTY 輸入 `yes`，或帶 `--yes`；非互動又沒帶 `--yes` 則拒絕 exit 1、資料庫與隔離區不動。確認前會先列出將被刪除的隔離區檔案路徑。id 不存在、已解決或 `--reason` 空白 exit 2（兩種各有獨立訊息） | `--id <ingest_failures id>`（見 `db doctor`）/ `--reason <text>` / `--yes` / `--database <path>` / `--quarantine-root <path>`（預設值同 `db doctor`，跟著資料庫走） |
| `db verify-cas` | 驗證附件 CAS 完整性：重算每檔 SHA-256 與檔名 hash 比對，印 total/ok/bad/mismatched/read_error/non_cas，將損壞附件（bad files）與不符 CAS 形狀的非附件檔（non-CAS files）分開回報；另對帳 DB 所有非 NULL `storage_path` 引用是否存在於 CAS（只查存在、不讀 payload；印 referenced/missing_refs 與缺檔的 attachment_id/db_id/path）；唯讀、有壞檔、非 CAS 檔或缺檔 exit 1、DB 不存在 exit 2；跨平台 | `--attachment-root <path>` |
| `db prune-cas` | 刪除 CAS 驗證標記的壞檔；僅刪除符合 CAS 形狀但內容雜湊不符的壞檔；若根目錄存在非 CAS 檔則拒絕執行 exit 1，絕不刪除資料庫或非附件檔。**破壞性**，要求顯式確認（非 TTY 且無 `--yes` 則拒絕刪除 exit 1） | `--attachment-root <path>` / `--yes` |
| `rebuild` | 災難復原**深層 fallback** 編排（DB 連同所有備份皆失時才用；首選仍是還原 DB 備份＝restore）。phase-based：preflight guard（擋 /mnt、驗 PST 存在、**每顆 PST 都掛在執行中的 Outlook**）→ coverage-report → import（PST，走 `import` 的 Outlook 抽取層）→ backfill（IMAP 最近窗口）→ verify-cas → validate。正典程序見 [rebuild-db-from-pst.md](rebuild-db-from-pst.md) | `--all`（不加 `--resume` 即新的一輪，重寫 checkpoint）/ `--resume`（先核對 checkpoint 與 stage DB / `--pst` 同輪，不符 exit 1 並印復原動作；一律重跑 preflight）/ `<phase>` / `--work-dir <path>` / `--pst <file>`（多值）|

## `sync-imap` 附件重試與三個計數（`#255`）

每次同步**開頭**（連線之前），對 `ingest_failures` 裡仍開著的附件失敗（`resolved_at IS NULL`）、且原始信還在本機 maildir 暫存區的信重試一次；只讀本機檔案、不連線。補回規則：只補目前是空的附件列，且檔名與位元組數都要對上帳上記的那筆；已完整的列與歷史上沒有失敗紀錄的空 placeholder 不動。重試失敗不停同步、不影響 watermark 與 exit code（契約 #5）。輸出多一行，三個計數各自獨立：

```text
attachment-retry: <N> recovered, <N> still failing, <N> source missing
```

- `recovered`：這次補回成功（對應紀錄標為已解決）。
- `still failing`：原始信在，但仍補不回（每次重試都會再算一次）。
- `source missing`：原始信已不在本機（既不在暫存區也不在隔離區）。

`#256`：清理七天暫存時，只要同一個位置還有任何未解決的附件失敗（`db doctor` 列為未解決的那一組，有沒有檔名都算），信的原始檔改搬進隔離區（預設 `<資料庫目錄>/.mailattic-work/quarantine/<資料庫檔名>/`，跟著資料庫走）而不刪除；認不出資料夾的原始檔一律留在暫存區不刪。重試也從隔離區找原始信，補回後交給同一條規則判斷放不放行。隔離區檔案只在補回成功或人工放棄、且該位置已無未解決失敗時刪除；失敗被別條路徑解決的信不會被自動清掉，會留在 `db doctor` 的 `quarantined_mail` 數裡。搬移或清理失敗不停同步、不影響 watermark 與 exit code（契約 #5）。放棄時，只要任何帳號對同一個位置的信還有未解決的失敗，該位置的隔離檔一律不刪（帳上記的是信箱地址、隔離區記的是帳號目錄名，兩者對不起來）；寧可多留，不誤刪。資料庫搬家或改名後，新位置看不到舊的隔離區，需連同目錄一起搬。

JSON 結果（`--pre-task` 與 log 之外的程式介面）對應欄位 `attachments_recovered` / `attachments_still_failing` / `attachments_source_missing`。

已知行為（`#274`，Sean 2026-10-13 接受，不另外修）：

- 完全解不開的附件，帳上沒有位元組數，日後即使變得可解也補不回，每次同步都算進 `still failing`；出口是 `db abandon-attachment-failure` 人工放棄。
- 檔名或位元組數對不上帳上那筆時不補（2026-10-09 裁決）；重新抽出的檔案仍留在附件儲存區，沒有任何信引用它。
- 只有 IMAP 同步會自動重試；PST 等檔案匯入留下的附件失敗不在重試範圍內。
- 同一封信裡檔名與位元組數都相同的兩個附件，帳上分不開，計數時算一個。

## `sync-imap` transport security

Windows-native IMAP sync 從這次指定的 `--env-file` 讀取 `IMAP_SECURITY`：省略或設為 `tls` 時走 implicit TLS（`IMAP4_SSL`，既有預設）；只有明示 `plain` 時才走未加密 IMAP（`IMAP4`，通常搭配 `IMAP_PORT=143`）。未知值會在連線前直接報設定錯誤。這是 mailbox env-file scope 的 sync 設定，不讀取 process-global fallback，也不改變 outbound 寄件備份 APPEND 的既有 TLS 行為。

> **安全警告：**`IMAP_SECURITY=plain` 會讓登入帳密與完整郵件內容以明文在網路上傳輸。只可用於受信任的封閉網路或受保護 tunnel；能提供 IMAPS 時一律保留預設 `tls`。

## `import` 行為

- 只支援 Windows + 已啟動且登入 active profile 的 Outlook；每個 `--file` / `--directory` 找到的 PST 必須已由操作者掛載，程式不代為掛載、卸載或修改。**Outlook 未執行時直接拒絕，不代為啟動**（`#181`）—— 舊行為會靜默拉起一個 Outlook，而那個 profile 不一定掛著你要的 PST，於是匯入可能悄悄跑在一個你從未掛載的 profile 上。**但主控台訊息與改動前逐字相同**：`Imported 0 messages (0 duplicates, 1 failures)`，exit 2。說明「Outlook is not running」的那句只落在 `ingest_failures` 的 `error_message`（`error_stage=extract-pst`），要查 DB 才看得到 —— 這是既有錯誤路徑的性質（`ingest` 把 `RuntimeError` 一律計入每來源失敗帳），不是本次改動引入的。
- 收件人真值只取 Outlook recipient table；display-string 不作 fallback。每個 SMTP 地址保持原子值，非 SMTP、無地址與未知角色會明確計帳。
- 每次執行原子寫入 `.project-runtime/recipient-extraction/<run-id>/summary.json`，CLI 顯示實際路徑；訊息失敗、對帳不完整或 summary 寫入失敗皆以非零結束。
- 先跑最小 PST；若推估全庫（數萬封）超過 8 小時，或非 SMTP `addrtype` 超過 5%，停止並回報量測。
- `rebuild` 災難復原編排的 import phase 與本命令走**同一條 Outlook 抽取層**（`#180`），因此災難復原不會退回 readpst 那套損壞的收件人結果；readpst 在 rebuild 只剩 `coverage-report` phase 用得到。操作依 [rebuild-db-from-pst.md](rebuild-db-from-pst.md)。

### `--maildir`（平台中立入口，`#154`）

- `--maildir <account-root>` 走的是每日 IMAP sync 天天在跑的同一組函式（`resolve_maildir_sources` + `import_maildir_sources`），**不另建解析邏輯**：dedup gate、`ingest_failures`、附件 CAS 一律沿用，所以 CLI 匯入與 sync 匯入不會漂開。
- **`<account-root>` 是帳號根目錄，不是散裝 `.eml` 目錄**：它的子目錄才是 folder，每個 folder 底下要有 `cur` / `new` / `tmp`（`<account-root>/<folder>/cur/<message>`）。
- **匯入前會先對帳，對不上就不匯**：掃過你指定的整個目錄，任何看起來像信、卻不在 `cur` / `new` / `tmp` 底下的檔案都會被列出來並以非零結束，錯誤訊息點名是哪些檔（超過 10 個只列前 10 個加總數）。**這是量測不是預測**——不去猜 parser 會讀到什麼，而是數你給的目錄裡有什麼。三種會漏信的形狀（整個目錄散裝、好資料夾旁邊有壞資料夾、同一資料夾內 bucket 旁邊有散裝檔）都由這一條涵蓋。
- **點開頭的檔案不算信**：`.uidvalidity` / `.mbsyncstate` / 本專案的 `.imap_watermarks.json` 是 maildir 的記帳檔，不列入對帳，否則每一棵真實的 mbsync 樹都會被擋下。
- `--account-email` 為必填：它是 `pst_source` 記的來源身分（`imap:<account-email>:<folder>`），留空等於把來源出處丟掉。
- 與 `--directory` / `--file`（PST 路徑）**互斥**，混用直接報錯，不做隱式優先序。
- `--database` 一律顯式指定或走既有解析順序（flag > `MAILATTIC_DB` > 寫死的 absolute default）。**測試與試跑務必傳明確的 `--database` 指向暫存路徑**——repo 根那顆數 GB 的正式庫就是 default target。
- **exit code 沿用 PST 路徑的同一條規則**：只有「完全沒進任何一封（`imported` 與 `duplicates` 皆為 0）且有失敗」才非零。**已經進了一封、同時有 500 封失敗仍然 exit 0**——那些失敗記在 `ingest_failures`、也出現在 summary 的 `failure_count`，不以整體結束碼表達。這個不對稱是刻意與 PST 入口一致，要改是兩個入口一起改。

**本機 PST 來源**（測試與災難復原重建用；程式碼不寫死，正典清單與程序見 [rebuild-db-from-pst.md](rebuild-db-from-pst.md)）：`%LOCALAPPDATA%\Microsoft\Outlook\` 下的 `archive_2020-2023.pst` / `archive_2024-2025.pst` / `sean3808@mailattic.test.pst`（換機後路徑可能已變）。

## 查詢共通行為

- 查詢指令**不會偷偷建立新的 SQLite 檔案**（DB 不存在就報錯）
- `--limit` 必須是 `0` 以上整數
- sender / recipient / attachment filter 會正確 escape `%` / `_`

## `--keyword` boolean syntax

- 支援大寫 `AND` / `OR` / `NOT`、括號分組、雙引號 phrase；例如 `A AND (B OR C OR D)`
- Operator 大小寫敏感：小寫 `and` / `or` / `not` 是 literal term，不是 operator
- CJK 查詢仍會逐字 segment；含 hyphen 的料號請用 phrase quote（例：`'"PEN-100"'`）
- 目前不支援 `*` prefix、column filter、`NEAR`

## 日期 bound 語意（跨所有查詢介面）

一個純日期值代表 **Asia/Taipei 該日整天、兩端 inclusive**（下界 `00:00:00+08:00`、上界 `23:59:59+08:00`）。`search` / `attachments` 另外接受完整 ISO-8601 timestamp，帶 offset 者選中該瞬間、**不帶 offset 者解讀為台北**（此為 breaking 變更，原本被當 UTC）。四個介面接受的日期語法本來就不同且刻意保持不同（`contact-history` 收 `2026-7-1` 不收 `20260721`；`mail_search` 相反），統一的只有「一天」的意義。儲存端沒有 offset 的 `sent_at` 一律解讀為 UTC（best-effort 既定限制）。完整規則見 [D-20260723-1](decisions/D-20260723-1-taipei-day-date-filter-semantics.md)。

## `search --full-body --json` contract

- 用途：agent / ingestion pipeline 一次取回可直接處理的完整本文與 thread linkage；**不用**再對每個 hit 做 N+1 `bes show`
- JSON-only：未搭配 `--json` 直接 exit 2，避免把全文灌到人類終端
- 額外欄位：`rendered_body_text`、`message_id`、`dedup_key`、`in_reply_to`、`references`、`to`、`cc`、`bcc`
- 預設 `bes search`（未帶 `--full-body`）維持 lightweight summary shape；下游要穩定引用 message，優先用 `message_id` 或 `dedup_key`，不是 `id`

## `attachments` contract

- 一列一附件；輸出同時帶 `attachment_id`、`message_db_id`、穩定引用用的 `rfc_message_id` / `dedup_key`、`exportable`、`carrying_messages`
- 以附件形式轉寄的信（`message/rfc822`）整封算一列附件、可匯出（沒有檔名時以附信主旨加 `.eml` 命名）；其內文不併入外層信的本文與檢索，其內夾帶的附件不另列。退信通知（`multipart/report`）夾帶的原信則維持併入外層本文與檢索、不另立為附件，此為刻意設計以方便依原信內容檢索退信
- `--copy-to` 只會複製 `storage_path` 非空的列；metadata-only attachment 會逐列回報 `skipped-non-exportable`，不是靜默忽略
- 預設 export 不覆寫；`--overwrite` 覆寫、`--skip-existing` 跳過，兩者同時給會在 copy 前 exit 非 0
- export run 完成後，**只有** `failed > 0` 會讓整體 exit 非 0；`copied` / `skipped-non-exportable` / `skipped-existing` 都不單獨構成失敗
- `--participants-domain` / `--recipient-domain` 收件人側走**網域邊界比對**（`recipient_domain_match` SQL 函式，比對 `recipient_search_text`）：只在 `@<domain>` 後面不接網域標籤字元（Unicode letter/digit/底線、hyphen、dot 以外）或到字串結尾時才命中，`acme.com` 不再誤收 `x@acme.com.evil`；packed blob 中段的真收件人（後接 `'` / `;`）仍命中。`@domain` 與 `domain` 等價。這是**查詢面比對邊界**，不代表 parser 已把 packed blob 拆乾淨 —— 那批 legacy blob 的資料正規化見 [D-20260724-1](decisions/D-20260724-1-recipient-domain-boundary-match-option-a.md) 的 Option B follow-up

## `db backup` 運維前提

產出是 **staging 快照、非 DR 終點**（同碟、不自管 retention、不自動外部複製）；快照含完整郵件本文（敏感資料），`--output` 勿指向雲端 / 共享資料夾。理由與 DR 分工見 [D-20260603-2](decisions/D-20260603-2-db-backup-is-staging-not-dr.md)。

## outbound 發信運維前提

- **SMTP 認證**：`draft-reply` / `send-draft` / `send-batch` 的寄件設定檔依序解析：`--env-file` > `MAILATTIC_SMTP_ENV_FILE`（空字串或全空白視為 unset）> 寫死指向 repo 根 `.env.sean3808` 的 absolute default（常見設定是與 IMAP 共用同一組登入）。檔案須含 `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`；解析出的檔案不存在時在 DB／網路 I/O 前 exit 2 並列出三條出路。檔案在但讀不出寄件帳號時，`draft-reply` 退回 `self_addresses` 表裡唯一的那一筆當 From，零筆或多筆就 exit 2（[D-20261013-1](decisions/D-20261013-1-mcp-draft-from-follows-smtp-account.md)）。不讀 archive-scrape 專用的 `MAIL_SERVER_ENV_PATH`。寄件帳號在 `draft-reply` stdout 與寄出摘要的 `From:` 露出，呈審時可見（[D-20260915-1](decisions/D-20260915-1-smtp-settings-file-resolved-by-bes-with-from-shown-at-review.md)）。TLS 走與既有 IMAP 相同的 legacy SSL context。
- **回覆信字體**（#13）：同一個寄件設定檔可選填 `MAIL_FONT_LATIN`（例 `Aptos`）、`MAIL_FONT_CJK`（例 `微軟正黑體`）、`MAIL_FONT_SIZE`（例 `12pt`，只接受 `pt`／`px`）。有填時 `draft-reply` 與 MCP `mail_draft_reply` 的 HTML 部分（回覆與引用原信）整份套用：英文缺字退 Calibri，中文退 Microsoft JhengHei／PingFang TC，Outlook 桌面版另以 `mso-fareast-font-family` 指定中文字體；只填英文或只填中文其中一項時，另一邊也套上這組備援。全都沒填時 HTML 不帶字體，與先前相同；設定檔的 SMTP 設定不完整（擬稿退回 `self_addresses` 當 From）時也不套字體。字體名稱含引號、分號等字元或字級格式不對時，擬稿 exit 2、不產草稿。純文字部分不受影響。
- **bes 不接 LLM**：outbound 只做機械步驟（header 組裝 / SMTP / DB insert / 確認），內文草擬由 caller AI 提供（`--body-from`）。任何讓 bes 自己生內容的 flag 都禁止存在（[D-20260528-1](decisions/D-20260528-1-bes-never-generates-content.md)）。
- **Outlook 寄件可見性靠 APPEND-to-Sent（best-effort）+ BCC-self（兜底）**：SMTP 送出成功後，`send-draft` / `send-batch` 會把 `.eml` APPEND 進 IMAP `寄件備份`（帶 `\Seen`），Outlook 寄件匣才看得到 bes 寄的信（[D-20260714-2](decisions/D-20260714-2-chinese-sent-folder-of-record.md)）。**APPEND 失敗只印 stderr warning，不改寄送結果也不改 exit code** —— 信已經送出去了，把可見性失敗回報成寄送失敗只會誘發重寄。看到 append warning ≠ 信沒寄出。APPEND 前會檢查 `IMAP_USER` 是否等於 SMTP user（不符則跳過 + warning），避免設錯帳號把整封 outbound 副本歸檔進別人信箱。BCC-self 保留為記錄兜底（[D-20260528-2](decisions/D-20260528-2-outbound-visibility-via-bcc-self.md)）。
- **寄送成功的語意**：SMTP 接受 ≠ 最終投遞（仍可能 bounce），也 ≠ 出現在寄件匣（只有 APPEND 成功才可見）。
- **outbound 來源正典是 IMAP `寄件備份` folder**：archive-scrape 排程已停用，程式碼保留供手動 backfill / DR（[D-20260714-1](decisions/D-20260714-1-imap-sent-is-sole-outbound-source.md)）。

## Python API

```python
from mailattic.query import search_messages, SearchFilters, get_message
```

- `SearchFilters(keyword, sender, recipient, sender_domain, recipient_domain, participants_domain, sent_from, sent_to, attachment_name)` — 全 optional
- `search_messages(filters, full_body=False)` — 預設 lightweight summary；`full_body=True` 加上 `rendered_body_text` / `dedup_key` / thread 欄位 / `to` / `cc` / `bcc`
- `get_message(message_id_or_db_id)` — 完整 body / recipients / attachments

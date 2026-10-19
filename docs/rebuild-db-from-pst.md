# Disaster Recovery — 從原始 PST + IMAP 重建 mailattic.db

> **這份文件的定位**：當主 DB（`mailattic.db`）損毀、遺失，或需要乾淨重建時，從**原始 PST + IMAP server** 從零重建的程序。
>
> **為什麼存在**：根目錄的 `*.bak` 便利快照已於 2026-06-02 刪除。重建能力不依賴那些快照，而依賴本程序 + 原始來源還在。
>
> **詳細正典（forensic 等級）**：本檔是 from-scratch 的乾淨入口；2026-05-27 實際做過的全量 rebuild 完整決策與逐步驗收條件屬 Spectra 鏈時期的 change archive，不隨本 repo 帶出；**執行面以本檔為準** —— 那次是 readpst + WSL 路線，已於 2026-09-10 退役。

## ⚠️ 2026-09-10 起：抽取改走 Windows Outlook，不再走 WSL

`bes rebuild` 的 import phase 已改用 Outlook backend（`#180`），與 `bes import` 走同一條抽取層。這件事**推翻了本檔舊版的三個前提**：

| 舊版前提 | 現況 |
|---|---|
| PST 複製進 WSL ext4、readpst 解包 | 抽取只能在 **Windows** 跑：讀的是**執行中 Outlook profile 已掛載的 store**（`outlook_pst.OutlookComReader`），程式不代為掛載、也不代為啟動 Outlook |
| 重建前要**關** Outlook | 重建全程 Outlook **必須開著**，且三顆 PST 都掛在 profile 裡 —— preflight 逐顆檢查，缺一顆就 exit 非零 |
| stage.db 落 WSL，完成後 robocopy 回 Windows | stage.db 與 CAS 直接落 **Windows 本機磁碟**，沒有跨界搬遷 |

為什麼非得如此：`#151` D7 查過 libpst / pypff 都讀不到 Outlook 的 recipient table，收件人只能經 COM 拿。readpst 路線抽出來的收件人是 `PR_DISPLAY_TO` 那套損壞結果（packed blob / 只剩人名 / 缺收件人），`#175` 才剛把正式庫修回來 —— 用舊路線重建等於把修復沖掉。

`/mnt` 禁令（[D-20260525-1](decisions/D-20260525-1-no-wsl-write-to-mnt-drive.md)）不受影響：preflight 仍硬擋 `/mnt` 工作目錄，只是新路線本來就不會走到那裡。

## 真正的 rollback 來源

1. **原始 PST** — `%LOCALAPPDATA%\Microsoft\Outlook\`（見下表）
2. **IMAP server** — `sean3808@mailattic.test` 增量信件來源
3. **外部備份** — 定期手動複製（DB 快照層級）；`backups/` 也留近期快照。還原快照後的 IMAP 水位線處置見下方「3d」之後的說明（進度在 DB 內，免手動處理）
4. **本重建程序**

## 來源 PST 清單

重建前先列出本 DB 使用的每一顆 PST（檔名、位置、初次匯入訊息數）。實際清單是操作者的本機資料，不寫進本檔；格式示意：

| PST 檔名 | 初次 import 訊息數 | 位置 |
|---|---|---|
| `archive_2020-2023.pst` | （量級） | `%LOCALAPPDATA%\Microsoft\Outlook\` |
| `archive_2024-2025.pst` | （量級） | 同上 |
| `sean3808@mailattic.test.pst` | （量級） | 同上 |

- 同目錄下可能另有不屬本 DB 語料的 PST，不要餵給 rebuild。
- 已確認不可得、且經擁有者核可為可缺的來源（`#228` 裁決）不作為重建的阻擋項。清單裡的數字只是初次匯入觀察值；實際差額應依 [GLOSSARY.md](../GLOSSARY.md) 定義的 rebuild 來源筆數基準記錄，不能把它當固定守門數字。
- PST 由操作者手動保存並更新；重建前先唯讀確認每顆 PST 存在且是最新。PST 存在與來源筆數達標是兩種證據，重建後仍須來源筆數對帳。

## 前置環境

| 項目 | 值 | 出處 |
|---|---|---|
| 平台 | Windows 11 + Python 3.13 + `uv`（**不是** WSL） | `CLAUDE.md` |
| Outlook | 執行中，且上表三顆 PST 都掛載於 active profile | `#151` D8 / `#180` |
| 工作目錄 | 任一 Windows 本機磁碟的可寫目錄，例如 `C:\IT_Projects\mailattic\.project-runtime\rebuild\`；**不得**是 `/mnt/<drive>` 或網路磁碟 | D-20260525-1 |
| 空間 | stage.db ≈ 4 GB + CAS ≈ 20 GB（2026-09-10 正式庫實測 71,519 檔 / 19.9 GB），連同正式庫至少留 60 GB | 實測 |
| `.env.sean3808` 必要欄位 | `IMAP_HOST` / `IMAP_PORT`(993) / `IMAP_USER` / `IMAP_PASSWORD` / `IMAP_ACCOUNT_NAME`(sean3808)。預設路徑 `C:\IT_Projects\mailattic\.env.sean3808`（absolute default，與 cwd 無關） | `imap_sync.py` |
| readpst | **只有 coverage-report phase 還用得到**（`.tools\libpst\tool\bin\readpst.exe`）。缺了那一 phase 只印 WARNING 不擋重建；import 完全不碰它 | `rebuild.py` |

## 重建步驟

> 機械核心由單一命令 **`bes rebuild --all`** 編排：preflight → coverage-report → import → backfill → verify-cas → validate。步驟 0（開 Outlook、停排程）與步驟 3–5（swap、CAS 就位、恢復排程）仍須人工 —— 那是程式無法擁有的閘門。

### 步驟 0 — Pre-flight（人工）

1. 確認 SSD 健康：`Get-WinEvent` 查近一日 System log 無新 disk error：`Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='disk','iaStorVD','stornvme','storahci'; Level=2; StartTime=(Get-Date).AddDays(-1)}` 應為空
2. **開 Outlook**，確認三顆 PST 都出現在資料夾窗格（缺的用「檔案 → 開啟 → Outlook 資料檔」掛上）
3. 停同步排程（避免重建中途 fire，也避免它在 swap 時握著 DB）。現行任務在 `\MyTasks\` 資料夾下 —— **缺 `-TaskPath` 會回「系統找不到指定的檔案」**，不能省。任一行失敗就停下，**不要繼續往 swap 走**：

   ```powershell
   $ErrorActionPreference = 'Stop'
   # 先記下原本狀態（Ready = 原本啟用；Disabled = 原本就停用）。步驟 5 只恢復原本啟用的
   $before = (Get-ScheduledTask -TaskPath '\MyTasks\' -TaskName 'mailattic mbsync daily').State
   "原本狀態：$before"   # 抄下來，步驟 5 要用（步驟 5 多半是另一個 session，變數帶不過去）
   Disable-ScheduledTask -TaskPath '\MyTasks\' -TaskName 'mailattic mbsync daily'
   Stop-ScheduledTask -TaskPath '\MyTasks\' -TaskName 'mailattic mbsync daily'   # Disable 不會中斷已在跑的那次
   $after = (Get-ScheduledTask -TaskPath '\MyTasks\' -TaskName 'mailattic mbsync daily').State
   if ($after -ne 'Disabled') { throw "同步任務狀態是 $after，不是 Disabled；不得繼續" }
   ```

   `MailatticArchiveScrape` 已依 [D-20260714-1](decisions/D-20260714-1-imap-sent-is-sole-outbound-source.md) 停用、不屬於這份流程：**不用停、也不要啟用**。
4. 重建期間**不要關 Outlook**，也不要讓機器睡著

### 步驟 1 — 跑 rebuild

```powershell
uv run bes rebuild --all `
  --work-dir C:\IT_Projects\mailattic\.project-runtime\rebuild `
  --pst "$env:LOCALAPPDATA\Microsoft\Outlook\archive_2020-2023.pst" `
  --pst "$env:LOCALAPPDATA\Microsoft\Outlook\archive_2024-2025.pst" `
  --pst "$env:LOCALAPPDATA\Microsoft\Outlook\sean3808@mailattic.test.pst"
```

各 phase 的行為與已知代價：

| Phase | 做什麼 | 注意 |
|---|---|---|
| `preflight` | 擋 `/mnt` 工作目錄、每顆 `--pst` 存在、**每顆都掛在執行中的 Outlook**、工作目錄可寫 | Outlook 沒開 → `Outlook is not running`，exit 非零，**不會**代為啟動 |
| `coverage-report` | 估 PST 覆蓋到哪天、與 server 保留窗口的缺口 | ⚠️ 它用 readpst **整批解包一次**才拿得到日期，時間與 import 同量級。只要缺口估計不重要，就改跑單一 phase 跳過它（見下方 fallback） |
| `import` | 逐顆 PST 經 Outlook 抽取寫入 stage.db + CAS | 逐顆 checkpoint。**`--resume` 只續跑「還沒開始」的 PST** —— 已寫入但沒被 checkpoint 接受的那顆會被擋下（見下方「中斷之後」）。收件人抽取不完整（`recipient_extraction_complete=false`）該顆直接失敗、不記 checkpoint |
| `backfill` | `bes sync-imap --full-resync` 補 PST 之後的增量 | best-effort：失敗只印 WARNING，不擋後續 phase |
| `verify-cas` | 重算 CAS 每檔 SHA-256 與檔名比對；再對帳 stage DB 每筆非 NULL 的附件引用在 CAS 內是否有檔（輸出 `referenced` / `missing_refs`） | 有壞檔、或有 DB 引用找不到檔 → 該 phase 失敗 |
| `validate` | doctor + `messages`/`message_fts` 列數一致 + keyword spot-check + 印 `ingest_failures` 筆數 | 通過只代表結構健康，**不代表可以 swap** —— swap 前跑步驟 2 |

**中斷之後**：同一 `--work-dir`、**同一組 `--pst`** 加 `--resume`（`--resume` 會先核對、可能拒絕，見下方第二個 ⚠️）。不加 `--resume` 的 `--all` 是新的一輪，會丟掉 checkpoint 記的進度。單跑一個 phase：`uv run bes rebuild import --work-dir ... --pst ...`。

⚠️ **死在某顆 PST 匯入途中的那次，`--resume` 不會幫你修好它** —— 會直接擋下並要你從乾淨的 stage DB 重來。原因是重匯不會覆寫已存在的信：`persist_message` 認得 `message_id` / `dedup_key`，命中就跳過，所以第一次寫進去的收件人會原封不動留著；而第二次的抽取帳算的是抽取器讀到什麼、不是什麼進了資料庫，因此可能平帳過關、把那顆 PST 標成完成。擋下來的訊息會點名是哪一顆。**復原動作**：刪掉 `<work-dir>\stage.db`（連同 checkpoint）重跑；CAS 是 content-addressed、重跑冪等，不用刪。

⚠️ **`--resume` 先核對 checkpoint 的宣稱，再跳過已完成的工作**（#236）。checkpoint 記著這一輪的 stage DB 路徑、`--pst` 集合，以及每顆已接受 PST 在 stage DB 內的列數。下列任一情形 `--resume` 直接 exit 1、stderr 印出 `[rebuild] resume: REFUSED ...` 與復原動作，不會建空 DB、也不會把 `import` 標成完成：checkpoint 記有匯入成果但 stage DB 不存在或沒有信；stage DB 路徑或 `--pst` 集合與記錄的那一輪不同；stage DB 內某顆已接受 PST 的列數與記錄不符（DB 被換掉）；stage DB 的總信數比任一已完成 phase 當時記下的少（逐顆 PST 列數看不到 `backfill` 補進來的信：DB 被換回只含 PST 匯入的舊快照、或各顆都匯入 0 封而 DB 被換成空的，都靠這條擋）。**復原動作**：刪掉 `rebuild-checkpoint.json` 與 `stage.db`，不加 `--resume` 重新開一輪。核對通過的同輪 resume 仍跳過已抽取的 PST，且**一律重跑 live preflight**（即使六個 phase 都已完成）。不加 `--resume` 的完整跑（不指定 phase）一律視為新的一輪、重寫 checkpoint。`#236` 落地之前寫下的舊格式 checkpoint（無輪次識別或逐顆列數）一律拒絕續跑（`#245`），必須刪除 `rebuild-checkpoint.json` 與 `stage.db` 後不加 `--resume` 從頭重建。PST 的大小與修改時間刻意不納入識別（Outlook 掛載會改動 PST）。

- 全量 wall-clock：Outlook COM 抽取很慢，全 corpus 粗估要**數小時**（不含 coverage-report）。瓶頸是 COM 單線程，平行的加速有限。
- **smoke first**：第一次跑先只餵最小的一顆 PST 驗通 pipeline，再餵其餘兩顆。smoke 那次是**獨立的一輪**：`--pst` 集合不同，接著用 `--resume` 餵三顆會被拒絕。smoke 驗通後刪掉 `rebuild-checkpoint.json` 與 `stage.db`，三顆一起重新開一輪（代價是重抽最小的那顆）。

### 步驟 2 — Verify（五項全綠才能 swap）

**`#228` 的入口裁決（2026-10-03，Sean，Q3=A、Q4=A）**：保留 `scripts/verify-rebuild.py` 作獨立換檔前驗證入口；`bes rebuild validate` 繼續提供結構健康檢查。來源筆數基準由 Sean 人工核可，跟 PST 一起手動保存，verifier 只讀取、驗證，不產生候選基準、不自行調整門檻。呼叫方式與基準檔格式見下方「verifier 呼叫與來源基準檔」。下列收件人內容抽樣仍是人工驗收，不能被腳本 PASS 取代。

1. `PRAGMA integrity_check` = ok（`bes db doctor` 已含）
2. 對照已核可且可比較的 rebuild 來源筆數基準，逐來源檢查最低入庫筆數與明示差額；`archive.pst` 缺失已核可（`#228` 裁決）。歷史初次匯入數、舊 DB 分布與本次抽取數不能直接互換，來源達標也不代表舊郵件逐封保留。沒有可信基準時，可產出 stage 並檢查結構健康，但不得給換檔驗證 PASS（`#228` Q1=A、Q2=A）；`verify-rebuild.py` 在沒有 `--baseline`、或基準檔不合格時一律 FAIL 並在報告寫出原因，沒有繞過旗標。
3. NULL hash count — **pattern-aware gate**：PST family 允許「三欄均 NULL」（`mime_type` / `size_bytes` / `storage_path` 同時 NULL，特徵為 TNEF/DMARC zip 類合法 NULL；**判斷標準是「三欄全 NULL = 合法」而非硬背數字**）；IMAP family = 0
4. 隨機抽最多 3 個 CAS 檔比對 sha256 與大小（附件不足 3 個就有幾個查幾個，一個都沒有才略過；`bes db verify-cas` 已全掃，這裡是抽驗）
5. 收件人抽樣：挑幾封已知多收件人的信，`bes show <id>` 看 `to` / `cc` 是**乾淨的 SMTP 位址**而非人名或 packed blob —— 這是本次遷移要保住的東西，`#175` 的損壞形狀就是收件人欄位被存成人名或 packed blob

任一 FAIL → 停，**不 swap**。

**verifier 呼叫與來源基準檔**

```powershell
uv run python scripts/verify-rebuild.py `
  --db <work-dir>\stage.db `
  --attachment-root <work-dir>\attachments `
  --baseline <approved-baseline>.json
```

exit 0 = PASS、1 = FAIL；stdout 是 JSON 報告（`per_source_counts` 內有每個來源的 expected / actual / threshold / 核可差額 / `refusal_reason`）。候選 DB 以唯讀開啟，腳本不寫、不產生、不調低基準；DB 旁若留有 `-wal`，SQLite 讀取時會建出 `-shm`（`.db` 與 `-wal` 內容不變）。

上面清單與腳本的「五項」不是同一組：腳本機械檢查的是第 1–4 項加上 **FTS rowid 對齊**（`message_fts` 與 `messages` 逐列對得上）；第 5 項收件人抽樣是人工驗收，腳本不做。兩邊都過才算驗完。

基準檔由 Sean 人工核可，**跟 PST 一起手動保存**（腳本不匯出、不從 stage 反推）。口徑是「去重後已寫進 DB 的 `messages` 列數，依 `pst_source` 分來源」—— 不是抽取器的 `messages_emitted`，也不是歷史初次匯入數。格式（數字僅為示意，不是正式值）：

```json
{
  "version": 1,
  "sources": [
    {"name": "archive_2020-2023.pst", "min_count": 1000},
    {"name": "archive_2024-2025.pst", "min_count": 1000},
    {"name": "sean3808@mailattic.test.pst", "min_count": 1000, "approved_gap": 5, "gap_reason": "核可差額的理由"},
    {"name": "archive.pst", "min_count": 1, "optional": true}
  ]
}
```

- 每個來源**各自**對 `min_count - approved_gap` 比較；其他來源、IMAP（`imap:`）、archive-scrape（`archive:`）、`outbound-bes` 都補不了缺口（後三者只在報告的 `excluded_non_pst` 計數）。DB 裡基準沒列的 PST 進 `unlisted_pst_sources`，不參與比較。
- `optional: true` = 該來源可缺（`archive.pst` 的裁決就是這樣表達，不寫死在腳本）；有出現就仍須達到它的下限。至少要有一個非 optional 來源。
- `approved_gap` 需要非空 `gap_reason`；`name` 是不含路徑的 `*.pst` 檔名。來源用檔名比對（不分目錄、平台分隔符、大小寫），所以 Linux 舊路徑與 Windows 路徑都對得上。
- 同一個檔名在 DB 裡對到兩個不同的 `pst_source` → **拒絕**（不默默合併）。要消歧，在該來源加 `"pst_source_paths": ["<DB 內實際的 pst_source>"]`，只有列出的那些算數。
- 缺檔、非 JSON、空、版本不符、未知欄位、數字不合法 → FAIL 並指出哪個欄位。

**PASS 證明什麼、不證明什麼**：證明基準列的各來源達到核可下限、且 integrity / NULL placeholder / CAS 抽樣 / FTS 對齊成立。**不**證明逐封或 bytes 保全，**不**取代上面第 5 項人工收件人抽樣，也不是 CAS 全掃（那是 `bes db verify-cas`）。

### 步驟 3 — Swap stage.db → 正式 DB

> ⚠️ **為什麼不能只換 `.db` 檔**：正式 DB 是 WAL 模式，沒 checkpoint 的已 commit 資料還躺在 `mailattic.db-wal`。如果只把 `.db` 改名、再把 stage 搬進同名位置，舊的 `-wal` 還在原地，SQLite 下次開檔會把它**重放到新換進來的 stage 上** —— 重建好的資料被舊資料蓋回，而且 `PRAGMA integrity_check` 仍回 `ok`，沒有任何錯誤訊號（合成 crash-WAL 已重現，`#225`）。所以 `.db`、`-wal`、`-shm` 必須**當成一組**搬走。

**3a. 確認沒有任何東西還握著正式 DB**（任何一項沒做到就不要進 3b）

- scheduled writer：步驟 0 已 Disable 並 Stop 同步任務，且 State 不是 Running。
- CLI：沒有進行中的 `bes ...` / `uv run bes ...`（含別的終端機、別的專案的 subagent）。
- **長駐 MCP server**：`bes-mcp` 由 Claude Desktop bridge 常駐，持有 SQLite connection。**完全結束 Claude Desktop**（含系統匣），不是只關視窗。其他開著這顆 DB 的工具（DB 檢視器、IDE 插件）同理。

```powershell
# 列出仍可能握著 DB 的行程；清單必須是空的
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'mailattic|\bbes(-mcp)?(\.exe)?\b' -and $_.ProcessId -ne $PID } |
  Select-Object ProcessId, Name, CommandLine
```

不確定就當作還有人握著、先問清楚是誰，不要用殺行程硬換。3b 的改名在 Windows 上本身就是第二道閘：還有 client 握著檔案時 `Move-Item` 會失敗，而腳本設了 `Stop`，會直接中止、不會做半套。

**3b. 換檔**（整段在同一個 pwsh session 跑；`$ErrorActionPreference = 'Stop'` 讓任何一步失敗就中止，**失敗時不要手動接著做後面的步驟**，先看「3d. 失敗或要回滾」）

```powershell
$ErrorActionPreference = 'Stop'
$db    = 'C:\IT_Projects\mailattic\mailattic.db'
$stage = '<work-dir>\stage.db'
$bak   = "$db.pre-rebuild-$(Get-Date -Format 'yyyyMMdd').bak"

# 前置檢查：rollback 名稱不能已存在（別蓋掉上一份），stage 必須是乾淨關閉的單檔
if (Test-Path $bak)           { throw "rollback 目標已存在：$bak" }
if (Test-Path "$bak-wal")     { throw "rollback 目標已存在：$bak-wal" }
if (Test-Path "$bak-shm")     { throw "rollback 目標已存在：$bak-shm" }
if (-not (Test-Path $stage))  { throw "找不到 stage：$stage" }
if (Test-Path "$stage-wal")   { throw "stage 有 -wal，代表沒有乾淨關閉；回步驟 1 處理" }
if (Test-Path "$stage-shm")   { throw "stage 有 -shm，代表沒有乾淨關閉；回步驟 1 處理" }

# 舊 DB 連同 sidecar 一起搬成 rollback 組（-wal 要和 .db 同名配對才讀得回未 checkpoint 的資料）
Move-Item $db $bak
if (Test-Path "$db-wal") { Move-Item "$db-wal" "$bak-wal" }
if (Test-Path "$db-shm") { Move-Item "$db-shm" "$bak-shm" }

# 新檔開檔前，原名下不得殘留任何舊 sidecar
if ((Test-Path "$db-wal") -or (Test-Path "$db-shm")) { throw "原名下仍有舊 -wal/-shm，不得搬入 stage" }
Move-Item $stage $db
```

**3c. 換檔後驗證**：先確認 `mailattic.db-wal` / `-shm` 都**不存在**（還沒有人開過新 DB），再開檔讀；讀到的必須是重建資料，不是舊資料。拿**新舊 DB 答案不同**的值來比：`SELECT COUNT(*), MAX(sent_at) FROM messages` 要等於進 3b 之前對 stage 查同一句記下的值。舊 DB 也查得到的關鍵字（步驟 5 的 spot-check 屬此類）在這裡不算證據 —— 舊 WAL 被重放時它照樣有結果。

**3d. 失敗或要回滾**：只在確認沒有 client 握著新舊 DB 時做，**同樣整組一起搬回**：

```powershell
$ErrorActionPreference = 'Stop'
# 回滾常在另一個 session、甚至隔天做：重新指定路徑，$bak 填 3b 實際產生的檔名（不要用日期重算）
$db  = 'C:\IT_Projects\mailattic\mailattic.db'
$bak = '<3b 產生的 .pre-rebuild-YYYYMMDD.bak 完整路徑>'
if (-not (Test-Path $bak)) { throw "找不到 rollback 素材：$bak" }
$failed = "$db.failed-$(Get-Date -Format 'yyyyMMddHHmmss')"
if (Test-Path $db) {   # stage 已經搬進來：把它和它自己的 sidecar 讓開，不直接刪
  Move-Item $db $failed
  if (Test-Path "$db-wal") { Move-Item "$db-wal" "$failed-wal" }
  if (Test-Path "$db-shm") { Move-Item "$db-shm" "$failed-shm" }
}
# 若 3b 停在半途（$db 不存在），原名下殘留的 -wal/-shm 仍屬舊 DB，留著不動，下面把 .bak 搬回即可
Move-Item $bak $db
if (Test-Path "$bak-wal") { Move-Item "$bak-wal" "$db-wal" }
if (Test-Path "$bak-shm") { Move-Item "$bak-shm" "$db-shm" }
```

回滾後舊 DB 開檔時 SQLite 會重放它自己的 `-wal`，換檔前最後一刻的資料就回來了。

**還原 / 回滾後的 IMAP 水位線（#223）**：同步進度存在 DB 內（`imap_sync_progress`，與信件同一個 commit），隨 DB 檔一起回到舊值；maildir 旁的 `.imap_watermarks.json` 只是鏡像，比 DB 新時 sync 不信任它。因此**還原後不需要手動刪或改 `.imap_watermarks.json`**，直接跑 `bes sync-imap` 會從 DB 的進度補回缺信；DB 內有進度、JSON 鏡像也在時，缺口再大也直接補抓；只有從 UID 1 重掃（該資料夾在 DB 內沒有進度，或 JSON 鏡像遺失）且超過 bulk threshold（500）時才會 `bulk_guard` 非零退出，這時再依訊息用 `--full-resync`（dedup gate 保證不重複入庫）。把 DB 換成**另一顆** `--database` 沿用同一個 maildir 也是同理，不會承襲前一顆的進度；舊版遺留的、尚未綁定任何 DB 的 JSON，只採用到該 DB 自己實際持有的最大 UID 為止（該資料夾在 DB 內一封都沒有就從 UID 1 重掃，dedup 吸收重複）。**刪掉或弄丟 JSON 鏡像**也一樣：sync 會從 UID 1 重掃，超過 500 時 `bulk_guard` 非零退出，這時改用 `--full-resync`。

**rollback 素材**：`.bak`、`.bak-wal`、`.bak-shm` 是一組，至少留 7 天再刪，不要只留 `.bak`。要**讀**這組備份時，先把整組複製到別的目錄再開 —— 直接開原檔，SQLite 會做 checkpoint 而改動 rollback 素材本身。

（「sidecar 跟著 `.db` 一起搬才不會被重放、漏搬就會被舊資料蓋回」這個 SQLite 行為的合成驗證，不是對上面 PowerShell 逐行的測試：[`tests/test_dr_swap_runbook.py`](../tests/test_dr_swap_runbook.py)。）

### 步驟 4 — CAS（attachments）就位

`message_attachments.storage_path` 指向 CAS，**CAS 必須跟 DB 一起換**，否則欄位有值但檔案不存在。

```powershell
robocopy "<work-dir>\attachments" "C:\IT_Projects\mailattic\attachments" /E /Z /R:2 /W:2 /MT:8
bes db verify-cas
```

robocopy exit ∈ {0,1,2,3} 為正常。有損壞附件 → `bes db prune-cas --yes` 後重搬（若附件根目錄含有非 CAS 檔會拒絕執行以防誤刪）。

### 步驟 5 — 恢復排程 + spot-check

**只恢復步驟 0 記下「原本是啟用」的同步任務**。`MailatticArchiveScrape` 已依 [D-20260714-1](decisions/D-20260714-1-imap-sent-is-sole-outbound-source.md) 退役、本來就是 Disabled，**不要啟用它**；步驟 0 記下原本是 Disabled 的任務也不要啟用。

先 spot-check，再恢復排程（恢復後第一次 sync 才會寫進新 DB）：

```powershell
bes search --keyword "<信庫裡確定存在的詞>" --limit 3   # 要有結果；0 筆視為換檔失敗
Enable-ScheduledTask -TaskPath '\MyTasks\' -TaskName 'mailattic mbsync daily'   # 僅當步驟 0 記到原本不是 Disabled
(Get-ScheduledTask -TaskPath '\MyTasks\' -TaskName 'mailattic mbsync daily').State   # 應為 Ready
```

重新開回使用者的 app（Claude Desktop / MCP 等）也在這一步之後，它們會對新 DB 開新的 connection。

### Fallback — 逐步跑（排錯用）

`bes rebuild` 只是把下面這串串起來，任何一段卡住都可以單獨跑：

```powershell
uv run bes init-db --database <work-dir>\stage.db
uv run bes import --database <work-dir>\stage.db --file "<pst path>"   # 逐顆
uv run bes sync-imap --database <work-dir>\stage.db --full-resync
uv run bes db verify-cas --attachment-root <work-dir>\attachments
```

- `bes import` 吃的是 `--file`（或 `--directory`），**不是** `--pst` —— `--pst` 只有 `bes rebuild` 有。權威 flag 以 `uv run bes <cmd> --help` 為準。
- `bes sync-imap --full-resync` 會**互動詢問**才清掉 JSON 水位鏡像（DB 內 `imap_sync_progress` 不先砍，隨重抓推進被覆寫）；rebuild 的 backfill phase 直接呼叫底層函式，不會問。
- `bes import` 與 rebuild 的 import phase 走同一條抽取層，因此逐步跑不會得到不同的收件人結果。
- ⚠️ **但 `bes import` 沒有 rebuild 的「半途失敗」守門**：對一顆上次匯到一半的 PST 直接跑 `bes import`，那些列會被當 duplicate 跳過、錯誤的收件人留著，而且沒有人會擋你。逐步路線只在**乾淨的 stage DB** 上用；已經死過一次的 stage DB 一律刪掉重來。

## Post-rebuild notice

- **整數 `messages.id` 重排**：重建後整數 id 重新分配。外部 caller 若存了整數 id 的 reference 會失效 → 改用 `message_id` 或 `dedup_key`。
- **archive-scraper outbound sentinel 歸零**：from-scratch 後 `pst_source LIKE 'archive:%'` 的歷史 outbound row 會消失，**且不會有排程把它補回** —— `MailatticArchiveScrape` 已依 [D-20260714-1](decisions/D-20260714-1-imap-sent-is-sole-outbound-source.md) 停用，IMAP Sent folder 是 outbound 唯一常規來源，重建**不得**為了補這批 row 而重新啟用該排程。PST 或 IMAP Sent 本來就有的信會由 import / backfill 取回；只存在於 archive-scrape 的歷史 row 屬不可復原範圍。真有需要才手動補：`uv run bes scrape-archive --once --from YYYY-MM-DD --to YYYY-MM-DD`（先加 `--dry-run` 看會寫哪些），這是可選的人工 backfill，不是重建的必要步驟。
- **`thread_key` / `thread_bridge_*`**：`thread_key` 是 derivable cache，重建後由 ingest 重算；inferred bridge 兩欄不會自動重建，需要時重跑 `scripts/bridge_orphan_outbound.py`。

## 不需要的舊邏輯（2026-05-27 特有，from-scratch 跳過）

- **archive-scraper transplant** / **IMAP orphan 接受損失** / **row-scripted transplant（new IDs + remap recipients/attachments/FTS5 rowids）** —— 這三項都是「保留舊 DB 特殊資料」才需要，from-scratch 無舊 DB。

## 待補 GAP

| # | GAP | 影響步驟 |
|---|---|---|
| 2 | 三顆 PST 是否掛在 active profile 只能人工看 Outlook 窗格；preflight 會擋，但沒有「列出目前掛了哪些 store」的命令 | 步驟 0 |
| 3 | `coverage-report` 用 readpst 整批解包一次才給缺口估計，代價與 import 同量級 —— 值不值得留在 `--all` 預設序列未定案 | 步驟 1 |
| 4 | 全量重建自換 Outlook backend 後**尚未實跑過**，3.5–4 小時是從 dry-run 抽取速率外推 | 整體 |
| 5 | **掛載中的 PST 能不能被別的行程開起來讀，未量測**（要量測必須先開 Outlook）。preflight 已不再做位元組可讀性檢查，正是因為若 Outlook 不放行外部開檔，「掛載成功」反而會讓檢查變紅。下次重建開著 Outlook 時順手驗一次，結論回填這裡 | 步驟 0 / 1 |

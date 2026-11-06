# 舊信內文重做（#14 階段二）— 正式庫執行手冊

> **給誰**：Sean 親跑正式庫的那一次（2026-10-09 拍板：資料回補由 Sean 執行）。agent 只在複本上試跑。
> **做什麼**：用今天的內文轉換（表格保真，[D-20261105-1](decisions/D-20261105-1-table-fidelity-without-storing-html.md)）重算舊信的 `body_text`，讓表格欄位對得回去。只改內文與搜尋索引，不動 Message-ID、寄件人、`dedup_key` 等任何其他欄位。
> **兩支工具**：`scripts/body_rederive_stage.py`（產生暫存庫，從不寫正式庫）、`scripts/body_rederive_apply.py`（預設試算；`--confirm` 才寫；`--restore` 還原）。用法與計數的定義以兩支腳本開頭的說明為準。

## 範圍（Sean 拍板，#14）

- 重做：PST 匯入且有 Message-ID 的信（readpst 重新解包）、IMAP 同步的信（向伺服器重抓）。
- 不重做：沒有 Message-ID 的 PST 信（`dedup_key` 含內文雜湊，改內文會讓日後重匯對不上）、`archive`、`outbound`。
- 重做後內文變差（中文變亂碼、內文變 base64，#57）的信保留原內文，計為 `degraded_kept`。

## 前置

1. **Outlook 關閉**時，把三個 PST 複製成副本（Outlook 開著會獨佔鎖住）。目前的對照（正式庫 `pst_source` → 檔案）：
   - `.../archive_2020-2023.pst`、`.../archive_2024-2025.pst`：封存檔
   - `.../sean3808@<domain>.pst`：Outlook 本機檔（`%LOCALAPPDATA%\Microsoft\Outlook\`）
   - `.../archive.pst`（36 封）已無原檔，會計為 `pst_missing`，正常。
2. WSL（Ubuntu-24.04）已裝 `pst-utils`（readpst、lspst）：`wsl -d Ubuntu-24.04 -u root -- apt-get install -y pst-utils`。WSL 操作前讀 [`wsl-environment.md`](wsl-environment.md)：**WSL 不得寫 `/mnt/c`**（D-20260525-1）。
3. 程式取已合併的 main（或本分支）clone 到 WSL 的 `/root/projects/...`，在那裡 `uv sync`。

## 步驟

### 1. 備份正式庫（Windows）

```powershell
bes db backup
```

### 2. PST 暫存（WSL）

把 PST 副本與正式庫的一份快照都**複製進 WSL 磁碟**（只讀 `/mnt/c`、寫 WSL 磁碟），再在 WSL 跑：

```bash
uv run python scripts/body_rederive_stage.py pst \
  --database <WSL 上的正式庫快照> --stage <WSL>/stage-pst.db \
  --pst-map '<pst_source 原路徑>=<WSL 上的 PST 副本>' ...（每個 PST 一個）\
  --overwrite
```

- **結束碼必須是 0**。2 = 某個 PST 解包不完整（readpst 偶發中途停止、仍回報成功，#56）、讀不到，或連封數帳本都拿不到（lspst 與重數都失敗，摘要印 `WARNING: no completeness ledger`）→ 直接重跑同一行。
- 跑完以 Windows 端 `robocopy "\\wsl$\Ubuntu-24.04\<路徑>" "<Windows 目的地>" stage-pst.db stage-pst-report.json /Z /R:2 /W:2` 搬回。

### 3. IMAP 暫存（Windows）

```powershell
uv run python scripts/body_rederive_stage.py imap --database mailattic.db --stage <路徑>\stage-imap.db --env-file <寄件設定檔>
```

唯讀開正式庫；向伺服器只做唯讀抓信。約 16–20 分鐘。**結束碼必須是 0**；2 = 連線中斷或抓信失敗 → 重跑同一行（加 `--overwrite`）。

### 4. 試算 → 寫入（Windows，每個暫存庫各做一次）

```powershell
uv run python scripts/body_rederive_apply.py --canonical mailattic.db --stage <暫存庫>            # 試算，看計數
uv run python scripts/body_rederive_apply.py --canonical mailattic.db --stage <暫存庫> --confirm  # 寫入
```

套用工具只接受「已證明完整」的暫存庫（暫存時的判定存在暫存庫裡；暫存結束碼是 2 的，試算與寫入都會被拒，訊息列出原因，請重建暫存庫）。試算印出暫存時的排除計數（`[stage] ...`）與套用計數。`drifted`（暫存後正式庫內文又變了）與 `message_id_changed` 會被排除、不寫。寫入在單一交易內完成，並在暫存庫旁留一份逐封原內文（`*-apply-preimage.db`）。

### 5. 抽查

`bes show <db_id>` 看幾封含報價表格的信，欄位應以 `|` 分隔對齊。

## IMAP 抓不到的信改從 PST 重做（#59）

步驟 3 的 IMAP 暫存抓不到的信（伺服器已刪、資料夾不在、舊 mbsync 檔名）原本維持原樣；其中不少仍在 PST 裡。步驟 3 跑完時，暫存庫旁會多一份 `<暫存庫名>-unreachable-ids.txt`（每行一個正式庫 `messages.id`，只含這三類信；報告的 `unreachable_ids_count` 是封數）。新子命令 `imap-from-pst` 只處理這份清單上的信，依 Message-ID 到 PST 找內文；**伺服器還抓得到的信不在清單上，不會被 PST 版本覆蓋**。

1. 步驟 3 照舊（會產生清單檔）。
2. 把清單檔與 PST 副本、正式庫快照一併複製進 WSL 磁碟，在 WSL 跑（PST 的 `--pst-map` 與步驟 2 相同）：

```bash
uv run python scripts/body_rederive_stage.py imap-from-pst \
  --database <WSL 上的正式庫快照> --stage <WSL>/stage-imap-pst.db \
  --ids-file <WSL>/stage-imap-unreachable-ids.txt \
  --pst-map '<pst_source 原路徑>=<WSL 上的 PST 副本>' ...（每個 PST 一個）\
  --overwrite
```

3. **結束碼必須是 0**。2 = 某個 PST 解包不完整或沒有封數帳本：找不到的信會計為 `extraction_incomplete`（不是 `not_in_pst`），重跑同一行。清單裡不是 IMAP 來源的 id 計為 `not_imap_row`，同一 Message-ID 內文不一致計為 `message_id_ambiguous`，皆不寫入。
4. 搬回 Windows 後照步驟 4 試算、寫入（套用工具認得這種暫存庫，並核對對應的正式庫列確實是 IMAP 來源）。

## 還原

```powershell
uv run python scripts/body_rederive_apply.py --canonical mailattic.db --restore <暫存庫>-apply-preimage.db            # 試算
uv run python scripts/body_rederive_apply.py --canonical mailattic.db --restore <暫存庫>-apply-preimage.db --confirm  # 還原
```

套用後又被改過的信會被排除（`drifted_since_apply`）。整份退回則用步驟 1 的備份。

## 2026-11-05/06 試跑結果（正式庫的唯讀複本）

- IMAP：範圍 7,900 封；改寫 2,236、不變 4,256、伺服器已刪 1,301、舊 mbsync 檔名 107、內文變差 0。
- PST：範圍 57,357 封；改寫 47,229、不變 1,997、內文變差而保留 304；有 Message-ID 的信除 1 封（同一 Message-ID 內文不一致）外全部配對成功（其中 16,830 封靠 Message-ID 後備配對）；無 Message-ID 7,790 封不重做；`archive.pst` 36 封無原檔。三個 PST 的封數帳本都相符（兩個封存檔用 lspst，Outlook 本機檔用重數）。約 34 分鐘。
- 套用後檢查：亂碼 0、變成 base64 0、`PRAGMA quick_check` ok、搜尋索引筆數 = 信件數。
- 還原實測：一次 47,533 封的寫入以 `--restore` 還原，內文與搜尋索引逐封與原值相同（0 不符）。
- 試跑中 readpst 曾三次中途停止仍回報成功（兩次在 2024–2025 封存檔、一次在 Outlook 本機檔），封數帳本就是為此而設；遇到結束碼 2 重跑即可。
- 精確數字與過程記在 #14 留言。

### 無法重做的信（#14 留言要求的封數）

| 類別 | 封數 | 說明 |
|---|---|---|
| IMAP：伺服器已刪 | 1,301 | 其中 1,054 封仍在 Outlook 本機 PST（依 Message-ID 比對），**247 封伺服器已刪且不在任何 PST** |
| IMAP：舊 mbsync 檔名無法定位 | 107 | 其中 91 封不在 PST |
| PST：無 Message-ID（不重做，Sean 拍板） | 7,790 | 含表格估計 2,221 封（2020–2023 封存檔 701 封可靠；其餘 PST 的位置有偏移，只是估計），另 1,745 封無法判斷 |
| PST：重做後內文變差而保留（#57） | 304 | 亂碼或 base64，待 #57 修好後可再重做 |
| PST：同一 Message-ID 內文不一致 | 1 | 不寫入 |
| PST：`archive.pst` 原檔已不存在 | 36 | |

比對「是否在 PST」用的是三個 PST 副本以 `ReadpstParser` 全量解出的 Message-ID 清單（2026-11-05）；伺服器已刪的 1,301 封全是 2026 年 5 月以後的信，兩個封存檔不會有。

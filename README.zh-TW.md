# mailattic

[English](README.md)

mailattic 把多年的 Outlook PST 存檔和 IMAP 增量同步收進一顆本機 SQLite 資料庫，建好全文檢索，讓 AI agent 透過 CLI、Python API 或 MCP server 查信。

它是為一個長期使用的信箱做的：信箱在自架郵件伺服器上，那台伺服器只保留近期的信。

## 快速開始

```powershell
git clone https://github.com/sean3808/mailattic.git
cd mailattic
uv sync
uv run bes init-db --database .\mailattic.db
uv run bes import --database .\mailattic.db --maildir C:\path\to\maildir-account --account-email you@example.com
uv run bes search --database .\mailattic.db --keyword '"PEN-100" AND quote' --sender alice@example.com --limit 10 --json
uv run bes thread 1 --database .\mailattic.db --format json
```

需要 Windows 11、Python 3.13 與 [`uv`](https://docs.astral.sh/uv/)。上面匯入的是 maildir，用不到 Outlook；要匯入 PST 請先看[環境需求](#環境需求)。

### 在任何目錄使用 `bes`

其他專案的 agent 呼叫的是全域 `bes`。在 clone 下來的目錄安裝，再指向你的資料庫：

```powershell
pwsh -ExecutionPolicy Bypass -File scripts\reinstall-global.ps1
setx MAILATTIC_DB C:\path\to\mailattic.db
```

全域 `bes` 是建置當下的快照，拉了新程式碼要再跑一次腳本。移除方式與資料庫路徑的優先順序見 [`docs/global-install-and-release.md`](docs/global-install-and-release.md)；要把 MCP server 加進 Claude Desktop，見 [`docs/cowork-mcp-bridge-validation.md`](docs/cowork-mcp-bridge-validation.md)。

## 為什麼從存檔查

給 AI agent 用的郵件工具多半是即時代理：agent 一問，server 才去連 IMAP 或 Gmail，查得到的只有郵件伺服器上還留著的信。要找五年前的某份報價、一個只用 POP 收過的信箱，或一台有保留期限的伺服器上的舊信，這條路就走不通。那些信唯一的副本在某個人硬碟裡的存檔，mailattic 查的就是那一份。

## 三個介面

| 介面 | 給誰用 | 說明 |
|---|---|---|
| CLI `bes` | 同一台機器上的 coding agent 與腳本 | `search`、`show`、`thread`、`attachments`、`contact-history`、`sync-imap`、`draft-reply`、`send-draft` 等。查詢類子命令可以輸出 JSON；各子命令的輸出選項見 [`docs/cli-reference.md`](docs/cli-reference.md) |
| Python API | 同一個程序內的呼叫端 | `search_messages`、`SearchFilters`、`get_message` |
| MCP server `bes-mcp` | 跑不了主機 CLI 的沙箱助理 | 六個讀取工具，加上附件匯出、擬回覆、寄出 |

三個介面走同一份查詢程式，日期篩選和關鍵字在哪個介面的意思都一樣。

```python
from pathlib import Path
from mailattic.query import SearchFilters, get_message, search_messages

results = search_messages(
    database_path=Path("mailattic.db"),
    filters=SearchFilters(keyword="contract", sender="alice@example.com"),
    limit=20,
)
message = get_message(Path("mailattic.db"), results[0]["id"])
```

## 設計上的選擇

### 關鍵字檢索（SQLite FTS5）

業務信件要找的東西多半得一字不差：料號、訂單編號、人名、地址、日期。命中的就是含有那些字的信，結果容易查核。代價是查詢得用信裡出現過的字，沒有語意上的聯想。這個選擇針對的是單機上的單一存檔。

### agent 擬稿，人同意，寄出去的就是人同意的那一份

回信先寫成草稿檔，草稿有一個 SHA-256 指紋。agent 寄出時要帶這個指紋，審閱之後被改過的草稿會被拒寄。人在真正的終端機前，也可以改用輸入授權詞的方式。工具無從得知人是不是真的被問過，那一部分是 agent 的義務，文件裡也照這樣寫。

### 以 Windows 為主

PST 匯入要經由執行中的 Outlook，測試啟動器是 PowerShell。maildir 匯入（`bes import --maildir`）不需要 Outlook，也不需要 Windows 專屬的工具。

## 限制

- 只在 Windows 11 上開發與測試，不承諾跨平台，也不承諾支援。
- 真正的資料庫、附件與帳密從來沒有進過版控。文件與測試裡的地址都是 `mailattic.test`、`example.com` 這類佔位名稱。
- commit 歷史從這裡重新開始，先前私有 repo 的歷史沒有帶過來。
- repo 裡沒有附資料，要查就得自己匯入。預計會補一顆去識別化的小樣本資料庫。
- 一顆資料庫就是一個人的信箱。讓好幾個信箱共用一份存檔、一次查，需要重新定義「哪些地址算自己人」，還要加上存取權限，這些還沒做。
- 不做：企業郵件伺服器、eDiscovery、圖形介面客戶端、推播通知、語意向量檢索、附件 OCR、任務追蹤。

## 環境需求

- Windows 11、Python 3.13、[`uv`](https://docs.astral.sh/uv/)
- 含 FTS5 的 SQLite
- 只有 PST 匯入需要：執行中的 Outlook，且目標 PST 已經掛載。匯入器只讀取既有的 store，不會自行掛載、卸載或修改 PST。先拿最小的 PST 試跑；每次執行的帳目摘要與該停下來的條件見 [`docs/cli-reference.md`](docs/cli-reference.md)。

## 文件

| 想知道 | 去 |
|---|---|
| 全部子命令、旗標與查詢規則 | [`docs/cli-reference.md`](docs/cli-reference.md)、`uv run bes <cmd> --help` |
| MCP 工具與它們的限制 | [`docs/mcp-server-contract.md`](docs/mcp-server-contract.md) |
| 資料庫的表 | [`docs/db-schema.md`](docs/db-schema.md) |
| 全域安裝、發行與排程 | [`docs/global-install-and-release.md`](docs/global-install-and-release.md) |
| 專案規則、架構地圖、行為契約 | [`AGENTS.md`](AGENTS.md) |
| 專案用詞的精確意思 | [`GLOSSARY.md`](GLOSSARY.md) |
| 某個設計當初為什麼這樣選 | [`docs/decisions.md`](docs/decisions.md)（紀錄以繁體中文撰寫） |
| 全部文件 | [`docs/INDEX.md`](docs/INDEX.md) |

## 致謝

開發流程使用 Matt Pocock 的 [skills](https://github.com/mattpocock/skills)。

## 授權

[MIT](LICENSE)。維護者：[@sean3808](https://github.com/sean3808)。

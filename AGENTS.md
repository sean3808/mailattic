# mailattic

> 所有 coding agent 共用的專案正典：Codex 直讀本檔，Claude Code 經 `CLAUDE.md` 的 `@AGENTS.md` 載入。只放跨 change、跨 session 穩定的事實；in-flight 進度與 change 專屬決策住 GitHub issue（意圖）與 PR（討論串）。

## 這是什麼

**歷史郵件語料的查詢後端**：自架 mail server（`mailattic.test`）的歷史 PST ＋ IMAP 增量同步 → SQLite + FTS5（多年累積、數萬封）。它答的是伺服器上已經不在的那一段 —— live IMAP 代理結構上拿不到的東西。消費端一律是程式介面：其他專案的 agent 走 `bes` / skill，CoWork 走 MCP，Sean 自己也走這兩條。

- **對外介面**：CLI `bes`（子命令吐 JSON）＋ Python API。跨專案樞紐是 **`mailattic` skill**（dev-canonical 在本 repo `skills/`，SSOT 是下游 release copy），不是裸呼叫 `bes`。
- **CoWork**：沙箱跑不了 host CLI，所以另有 MCP server 經 Claude Desktop bridge（讀取六工具、寫入三工具）→ [`docs/mcp-server-contract.md`](docs/mcp-server-contract.md)。沙箱看不看得到 DB 與 CAS 目錄取決於 per-session 掛載，不能當介面用（`#28` §1／§2.2）；host subagent 不受影響。
- **dual-track**：Outlook 管日常收發（人類介面），本 DB 管長期累積；兩軌靠 message-id dedup gate 共存。
- **本 repo 是公開的主場**（`sean3808/mailattic`，[D-20261010-1](docs/decisions/D-20261010-1-showcase-ships-ahead-of-remoteization-in-a-fresh-repo.md)）：日常開發與 issue 都在這裡。先前的私有 repo 凍結成歷史檔案，commit 歷史與原始討論串沒有帶過來，所以文件裡不帶連結的 `#NNN` 多半是私有時期的票號，在這裡查不到。issue、PR 與 commit 一律不貼真實信件內容、真實網域或地址（提交前檢查會擋）；需要真實信件當證據的除錯，證據留在本機。
- **給人看的門面**是 [`README.md`](README.md)（英文為主，[`README.zh-TW.md`](README.zh-TW.md) 為繁中版，兩份一起改）；本檔是給 agent 的規則。

**不是**：enterprise mail server / eDiscovery / GUI client / 推播系統 / 語意向量搜尋（只有 FTS5 keyword）/ 附件 OCR / 任務追蹤系統（承諾盯催屬任務管理工具，[D-20260603-1](docs/decisions/D-20260603-1-no-task-or-commitment-tracking.md)）/ 給第三方匯入自己信箱的產品（開源定位是對外展示，不為假想使用者付跨平台成本，[D-20260813-2](docs/decisions/D-20260813-2-open-source-is-showcase-not-product.md)）。需求落在這張清單上，先回報再動手。

## 常用指令

```powershell
uv sync                                    # 同步 venv（Python 3.13）；fresh worktree 開工第一步
uv run bes <subcommand> [flags]            # CLI，吃 working tree

# pytest 唯一入口是 launcher（契約測試巡邏 launcher 外的 raw 呼叫）
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Quick            # 日常；不算 release evidence
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Full             # 收尾 / merge / release 證據
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route FullSequential   # 懷疑平行度時
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Targeted tests\test_query.py::test_x

uv run --locked --exact ruff check --fix   # pre-commit 也跑
uv run --locked --exact ruff format        # 只檢查不改：加 --check
uv run --locked --exact pyright            # 只掃 mcp_server.py / mcp_delivery.py（pyproject include）
```

Route 怎麼選、deptry / pip-audit 的 canonical 命令與 exit code 語意 → [`docs/agents/quality-gates.md`](docs/agents/quality-gates.md)。**gate 非 0 ≠ 找到缺陷**，也可能是 gate 沒跑起來 —— 讀輸出。

正式 DB `mailattic.db`（repo 根，數 GB）是每個入口的**預設目標**：prototype、實驗、一次性腳本一律顯式 `--database` 指向暫存路徑。備份快照住 `backups/`。

## 架構地圖（`src/mailattic/`）

| 分區 | 模組 | 說明 |
|---|---|---|
| 入口 | `cli.py`, `__main__.py` | 所有子命令的 argparse dispatch；entry points `bes` / `bes-mcp`（`__main__.py` 只轉呼叫 `cli.main`） |
| Ingest | `discovery.py` → `parser.py` / `outlook_pst.py` → `ingest.py` → `storage.py` / `schema.py` | PST/`.eml` 發現 → 解析（`outlook_pst.py` 經 Outlook 讀 PST，含收件人抽取帳驗證）→ dedup gate + 正規化 → 寫入；單封失敗記 `ingest_failures`。三條匯入路徑的實際形狀見 `GLOSSARY.md`「匯入路徑」 |
| Query | `query.py`, `query_commands.py`, `contact_history.py`, `thread_key.py`, `cjk_segmentation.py`, `datetime_bounds.py` | FTS5 + 結構化欄位；CLI 與 Python API 共用同一層。`query_commands.py` 是 CLI 查詢命令族（search / attachments / show / thread / contact-history）：自註冊 parser、自帶 handler 與日期輸入轉換，`cli.py` 只呼叫它的 `register_*` |
| IMAP | `imap_native.py`, `imap_runtime.py`, `imap_state.py`, `imap_sync.py`, `mutf7.py` | UID-based 增量同步（native imaplib） |
| Outbound | `outbound.py`, `outbound_commands.py` | draft/send/APPEND-to-Sent；人工授權 gate → 契約 #2。`outbound_commands.py` 是 CLI 寄件命令族（draft-reply / list-drafts / send-draft / send-batch）：自註冊 parser、自帶 handler、寄件設定檔錯誤、呈審列印、指紋 pair 語法與 exit code 映射，`cli.py` 只呼叫它的 `register_outbound_commands` |
| MCP | `mcp_server.py`, `mcp_delivery.py`, `mcp_readonly.py`, `mcp_writer.py` | CoWork bridge：讀取六工具 + 寫入三工具（附件匯出、擬回覆、寄出）+ 回應預算降級階梯。讀取 → 契約 #1（單點綁定在 `mcp_readonly.py` 的 `ReadOnlyMailReader`）；寫入只經 `mcp_writer.py` 這一個 accessor，寄出 → 契約 #7。改輸出欄位或降級順序時，`mcp_server.py` 依序讀四段：回應預算拒絕（`_*_over_budget_error`／`_*_terminal_refusal`）→ 欄位白名單（`shape_*`／`_shape_*`）→ 降級階梯組合（`_*_ladder_states`）→ `create_server` 內的工具 handler（各自呼叫 `mcp_delivery.walk_ladder`）（#327）。**工具面改動走 probe loop 驗證**（[`docs/mcp-client-probe-loop.md`](docs/mcp-client-probe-loop.md)）：headless `claude -p` 打 working tree，不需重啟 Claude Desktop（重啟只在 closeout 驗收；`codex exec` 明文排除） |
| 附件 | `attachment_store.py`, `quarantine.py` | content-addressed store（SHA-256）；隔離區：清理時附件失敗未解決的暫存原始信搬到這裡而非刪除（#256） |
| 共用 | `models.py`, `identity.py` | `models.py`：跨層 dataclass 與檢索文字正規化；`identity.py`：哪些地址算「我」（`self_addresses` 表，`bes identity` 維護） |
| 維運 | `db_maintenance.py`, `rebuild.py`, `archive_scrape.py` | backup / doctor / verify-cas；rebuild 是 DR 深層 fallback |

## DB schema

寫 SQL、動 schema、碰 thread-bridge 之前讀 [`docs/db-schema.md`](docs/db-schema.md)（各表欄位設計、哪些欄是可重建的 cache）。行數與最新 cutoff 用 SQL 查（`SELECT MAX(sent_at) FROM messages`）。

## CLI 與 Python API

子命令總表、flag 與 Python API（`search_messages` / `SearchFilters` / `get_message`）→ [`docs/cli-reference.md`](docs/cli-reference.md)；權威以 `uv run bes <cmd> --help` 為準。

兩條語意用錯不會報錯，只會回一批看起來對的錯答案：

- **日期 → 契約 #4**：台北整日、兩端 inclusive；`search` / `attachments` 吃的 ISO-8601 timestamp 不帶 offset 就解讀為台北。邊界日的信會悄悄多一天或少一天。
- **`--keyword` 的 boolean operator 大小寫敏感**：`AND` / `OR` / `NOT` 大寫，小寫 `and` 是 literal term。CJK 逐字 segment；含 hyphen 的料號用 phrase quote（`'"PEN-100"'`）。

## 硬性規則

### 環境

- **Windows 11 / Python 3.13 / `uv` / `.venv/`**，本地執行；pip / poetry / 直呼 `python` / 舊版 Python 都不用。原始 PST 唯讀。
- **腳本一律 Python `.py`**（含 git hook）：git hook 走 pre-commit framework local hook（`entry: uv run python scripts/xxx.py`、`language: system`），shell 動作用 stdlib（`subprocess` / `shutil` / `filecmp`）。
- **`PYTHONIOENCODING=utf-8`**：直接跑 Python 或用 Python API 時必設；`bes` CLI 已在程式內處理。
- **WSL 寫入端 ＋ `/mnt/<drive>` 目的地 → 一律 robocopy 或 tar bundle，不論檔案多少**：Windows 端 `robocopy "\\wsl$\Ubuntu-24.04\..." "C:\..." /E /Z /R:2 /W:2 /MT:8`（別加激進參數），或 WSL 端先 `tar -czf` 再搬。`rsync` / `cp -a` / `tar -x` 直寫 `/mnt/c/...` 曾觸發儲存控制器 reset、資料部分損毀（[D-20260525-1](docs/decisions/D-20260525-1-no-wsl-write-to-mnt-drive.md)）。任何 WSL 操作前先讀 [`docs/wsl-environment.md`](docs/wsl-environment.md)（distro、mirror 路徑、uv 位置、docker mount、`run_sync.py` 路徑陷阱）。
- **非 Windows 環境（Linux 雲端 agent / WSL / container）交回的一律是 draft**：launcher 與 `pyright` 的 canonical 命令只在 Windows 跑得動，那裡只跑得動 `ruff check` / `ruff format --check` / `deptry src` / `pip-audit`。產出開 **draft PR**，Windows 本機 `-Route Full` 過了才轉 ready for review；launcher 外的 raw pytest 綠燈不算證據（宿主差異類 bug 在 Linux 不復現）。派工前先問：這張票要驗的東西碰得到 pwsh / launcher / registry / `.tools/` 嗎？碰得到就派 Windows 環境（機制 → quality-gates.md、`#61`）。

### 測試與一次性產物

- **pytest 只走 `scripts\invoke-pytest.ps1` 的四條 route**，不需任何環境變數（系統碟守門已退役，[D-20260821-1](docs/decisions/D-20260821-1-retire-system-drive-hardware-guard.md)）。exit 18 只代表 volume 無法確立（懸空 junction、UNC）。
- **一次性產物留在專案內的 `.project-runtime/`**：測試 fixture、暫存 DB、測試產物用 `tmp_path`（launcher 導到 `.project-runtime/pytest`）。正式 artifact 的契約要求落在 working tree 外時，測試仍以 test-only override 在 `.project-runtime/pytest` 驗內容，另寫拒絕測試證明正式入口不收 repo 內路徑。其他工具輸出優先用既有 managed leaf；沒有合適的，先讀 `scripts/project-tool-environment.ps1` 的 runtime 契約再新增已忽略的路徑。

### git、review 與 merge

- **main 有多個 writer**：動手前看 `git status --short`；與本任務無關的既有變更一律保持原樣 —— 不還原、不覆蓋、不 stage。暫存逐檔列路徑（`git add -A` / `git add .` 會掃進別人的檔）、commit 前 `git diff --cached --name-only` 逐檔確認、落點以 `git commit` 輸出的 `[<branch> <sha>]` 為準、push 前重查 `git branch --show-current`。加害方完全無症狀，只有事後看 `--stat` 才發現（PC-088 / PC-133）。
- **語意 review 由 size 觸發，不由 PR 觸發**（[D-20260820-1](docs/decisions/D-20260820-1-review-trigger-is-change-size-not-pr.md)）：M/L 一律語意 review，不論在哪產出、走不走 PR；S（設定、文件、archive、chore、已知 one-liner）直進 main；size 判不出來就當 M。PR 只在需要外部協作交接、或想留可追溯討論串時開。放寬的是流程包裝，不是審查。CI 的三個排除選項 → [D-20260820-2](docs/decisions/D-20260820-2-excluded-ci-infrastructure-options.md)。
- **只允許 squash merge**（[D-20260812-1](docs/decisions/D-20260812-1-keep-squash-only-merge.md)；squash 訊息的要求見 `## Pull Request 交付契約`）：全域 L-size 的 `--no-ff` 在此做不到，`gh pr merge --merge` 會被 GraphQL 擋下；這是刻意的 repo 設定，維持它、不用本機 merge 繞過。`--ff-only` 只用來把本機 main 同步到 `origin/main`。

### 文件同步

- **改 CLI surface（`bes` 子命令 / flag）→ 同一個 change 內更新 `skills/mailattic/`**（`SKILL.md` + `references/capability-*.md`）一起 review。判斷句：這個 change 改了 subagent 該怎麼呼叫 bes 嗎？
- **本檔有四支測試綁著**，搬段落前先看：`tests/test_architecture_map.py`（`## 架構地圖` 列的模組 ＝ `src/` 實際模組）、`tests/test_agent_pr_contract.py`（PR 交付契約 JSON 區塊全 repo 只能有一份，住本檔該段）、`tests/test_contract_gate_reminder.py`（`## 行為契約` 表列出的 anchor test ＝ `.claude/hooks/contract_gate_reminder.py` 的 `ANCHOR_TESTS`）、`tests/test_tag_based_release.py`（README + 本檔 + [`docs/global-install-and-release.md`](docs/global-install-and-release.md) 三件套講清楚 push ≠ release ≠ skill archive，且前兩者要連到第三者）。
- **改檔名、搬檔、改標題會弄紅 `tests/test_markdown_pointers.py`**（`#116`）：它掃所有 tracked markdown 的相對連結與 `#錨點`，目標檔或標題不在就紅 —— 修法是把指過來的連結一起改，不是改測試。既有死錨凍結在 `tests/fixtures/dead_markdown_pointers.txt`（只能縮：新死錨會紅，修好的也要從清單刪掉）；確實要重產清單時 `uv run python -m tests.test_markdown_pointers`，產出的差異要人看過。
- **給 AI agent 的回報是事實 + 定位**：`subject` / `sender` / `recipients` / `sent_at` / `pst_source` / `message_id` / `db_id`；需要時附 body 片段（關鍵字 ±N 行）；矛盾比對給兩邊各自的 `message_id`。
- **看到 APPEND warning 不重寄 → 契約 #3**：SMTP 接受 ≠ 最終投遞（仍可能 bounce）≠ 出現在寄件匣（只有 APPEND 成功才可見）。

## 行為契約

**這是本專案現行的行為正典**（取代已退場的 Spectra capability specs）。每條 = 一句話業務語意 + 一條 anchor test 當機械絆索：改壞了會有東西變紅，不是靠人記得。設計來由與本 repo 的裁決見 [D-20260820-3](docs/decisions/D-20260820-3-migrate-to-contract-anchored-flowback.md) / issue `#165`。

**Gate**：動到契約條目或其 anchor test 的變更**必須走 PR + `/spec-reconcile`**（其餘變更照 [D-20260820-1](docs/decisions/D-20260820-1-review-trigger-is-change-size-not-pr.md) 的 size 規則，direct commit 隨意）。三條硬性收斂：

- reconcile 出現 **NOT INTENDED + IMPLEMENTED** 或 **CONTRACT VIOLATED** → 停下等 Sean 裁決，不自行判斷可不可以放行。
- reconcile 綁**當次最終 diff**：報告產出後又落新 commit，該次 reconcile 即作廢、重跑。
- anchor test 檔案本身的改動必須經跨模型語意審查 —— 絆索是誰在鬆綁，不能由鬆綁的人自己審。履行者是 `/closeout` 的 `semantic-review` 格（`/codex-review`），收尾等級因此定 Medium 以上。

**協定投遞（開工導向點）**：本段是 gate 的唯一正典。派工可能碰到契約條目或 anchor test，而執行者不載入本檔時（內建 Explore / Plan、不讀 `AGENTS.md` 的外部模型），派工詞帶上本段的 gate 條款與下面的鐵則。

**Review 派工鐵則（PC-043）**：`/spec-reconcile` 與 code review Spec 軸的派工詞**必含 intent 優先序條款** —— issue 全文（含 comments）是 intent 基準；受審物自帶的文件、與實作 session 當場寫的 comment，都是**被審查的宣稱**，不得當基準。逐字條款住 `spec-reconcile` SKILL.md 的「Intent 基準」段，派工時整段複製，不要自己重寫。

| # | 契約 | Anchor test |
|---|---|---|
| 1 | **MCP 讀取唯讀、寫入只有寄出記錄** — CoWork MCP server 的讀取只能經唯讀路徑：資料存取經 `mcp_readonly.ReadOnlyMailReader`（`read_only=True` 單點綁定），擬草稿讀原信也用唯讀連線；`mcp_server.py` 不得直接 import query / contact_history / storage 的資料函式。MCP 對 DB 唯一的寫入是 `mail_send` 經白名單寄出路徑（契約 #7）記錄已寄信件，與 `bes send-draft` 相同（`#189` 2026-09-25 裁決） | `tests/test_mcp_server.py::McpServerTests::test_server_module_carries_no_read_only_import_boundary_violation`（同檔另 3 條 AST guard 同屬此契約） |
| 2 | **outbound 寄出＝看過的草稿**（CLI 形態，[D-20260914-1](docs/decisions/D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md)）— gate 住核心函式內，每封寄出須至少滿足其一：帶**與當下草稿 bytes 相符的指紋**（SHA-256），或 stdin 與 stdout **皆為**真 TTY 的明文授權；兩者皆無即拒寄。指紋不符拒寄且不寫 send-once marker；批次逐封驗、不符只拒該封；指紋之外沒有其他非互動旗標。寄出一律要帶寄件設定（`env`，寄出後 APPEND 進寄件備份用）：呼叫端沒給就在授權與 SMTP 之前拒寄、不寫 marker（`#114`）—— 否則信照寄、寄件備份看不到，那行 warning 正是 #3 要擋的重寄誘因。「使用者有沒有被問過」測不到，是 agent 的對話層義務，不在本條。MCP 形態見 #7（[D-20260924-1](docs/decisions/D-20260924-1-mcp-send-binds-reviewed-draft.md)） | `tests/test_outbound_send.py::test_fingerprint_mismatch_refused_without_marker`（+ `test_non_tty_without_fingerprint_refused_listing_both_ways_out`、`test_batch_fingerprint_mismatch_refuses_only_that_draft`、`test_non_tty_stdin_refused`、`test_non_tty_stdout_refused`、`test_send_cli_has_no_authorization_bypass_beyond_fingerprint`；`tests/test_imap_append.py::test_send_draft_env_none_refused_before_smtp_and_marker`、`test_send_batch_env_none_refuses_whole_batch_before_smtp`、`test_submit_prepared_draft_env_none_refused_before_marker`） |
| 3 | **APPEND 失敗不重寄** — 送出後 APPEND 進寄件備份失敗只印 stderr warning、不改 exit code：信已經寄出去了，把可見性失敗當寄送失敗處理就會對外重複發信 | `tests/test_imap_append.py::test_append_failure_does_not_fail_the_send` |
| 4 | **台北整日日期語意** — 日期 filter 是台北整日、兩端 inclusive（`00:00:00+08:00` ～ `23:59:59+08:00`）；不帶 offset 的 timestamp 解讀為台北、不是 UTC（釘住 [D-20260723-1](docs/decisions/D-20260723-1-taipei-day-date-filter-semantics.md) 現狀） | `tests/test_date_bounds.py::WholeDayBoundTests::test_lower_bound_is_midnight_taipei`（+ `test_upper_bound_is_last_whole_second_taipei`） |
| 5 | **sync fail-loud** — message 本體未 persist 時 sync 停在該 UID：watermark 不越過、記 `ingest_failures`、exit 非零；**附件子失敗不觸發停止**（[D-20260709-1](docs/decisions/D-20260709-1-sync-fails-loud-on-ingest-failure.md)） | `tests/test_imap_sync_ingest_accounting.py::test_message_stage_failure_blocks_watermark_and_retries_failed_uid`（+ `test_attachment_sub_failure_still_advances_watermark`） |
| 6 | **rebuild 抽取帳不平即停** — `bes rebuild` 的 import phase 在某顆 PST 的收件人抽取被判定為不完整時（`recipient_extraction_complete=false`，例如列舉沒跑完、訊息數或收件人數對不上）讓該顆 PST 直接失敗、不記 checkpoint：帶著不完整的抽取繼續會 stage 出收件人靜默錯誤的 DB，後續 phase 都驗不出來；**不得為了讓一次重建跑完而降級成 WARNING**。本條只釘 rebuild 對該判定的反應；判定本身怎麼算出來（`outlook_pst.py` 的抽取帳驗證）不在 anchor 範圍內（`#180` 落地、`#185` 升格） | `tests/test_rebuild.py::test_import_phase_fails_when_recipient_extraction_is_incomplete` |
| 7 | **outbound 對話層授權（MCP 形態，[D-20260924-1](docs/decisions/D-20260924-1-mcp-send-binds-reviewed-draft.md)）** — CoWork 只能寄出 `mail_draft_reply` 擬出、且內容指紋與寄出當下相符的草稿；CLI 草稿不可經 MCP 寄出；能直接觸發寄信核心步驟的只有白名單上三個呼叫者。指紋不符拒寄且不寫 send-once marker。「使用者有沒有被問過」測不到，是 agent 的對話層義務，不在本條 | `tests/test_mcp_send.py::test_mcp_send_fingerprint_mismatch_refused_without_marker`（+ `test_mcp_send_refuses_cli_drafts`、`test_submit_prepared_draft_caller_whitelist`） |

## 交付流程

**Contract-Anchored Flow-Back**（[D-20260820-3](docs/decisions/D-20260820-3-migrate-to-contract-anchored-flowback.md)）：意圖住 issue、行為正典住上方 `## 行為契約`、對帳在 merge 之前。一輪的樣子：

1. **issue brief** —— 做什麼、為什麼、什麼算做完；講清楚就夠，不寫規格文件。issue 全文（含 comments）是後續審查的 intent 基準。
2. **`/implement`** —— 內建 TDD 與 code review，實作在這裡收斂。
3. **動到契約條目或 anchor test → PR + `/spec-reconcile`**；其餘照硬性規則的 size 規則。
4. **squash merge**。

Spectra 鏈時期的 capability specs 與 change archive（73 筆）是 2026-08-20 以前的歷史，不隨本 repo 帶出（[D-20261010-1](docs/decisions/D-20261010-1-showcase-ships-ahead-of-remoteization-in-a-fresh-repo.md) 第 5 點）；Spectra 指令在本專案沒有適用對象（終止經過見 D-20260820-3 的 2026-08-21 補記）。每輪 reconcile 太重時，降級梯子是累積數輪一起對帳，對帳本身保留。

## Pull Request 交付契約

下列機器可讀區塊是唯一正文；欄位名稱就是驗收語意，不要在其他 agent 文件改寫或複製。

```json
{
  "pr_delivery_contract": {
    "pull_request_body": {
      "squash_message": {
        "heading": "## Squash message",
        "must_explain_why": true,
        "per_issue": {
          "closing_reference": "Closes #n",
          "must_explain_why": true
        }
      }
    },
    "completion": {
      "pull_request_state": "ready_for_review"
    }
  }
}
```

此 repo 只允許 squash merge，因此 squash message 是逐輪 commit 消失後保留的設計理由；ready for review 則表示可以請 Sean 授權合併，並觸發只在 ready PR 執行的 CI。

## Issue 與 PR comment 的角色標記

在 issue 或 PR 留言的**第一行**放角色標記，整行照抄：Claude Code 寫的用 ``> `Claude Code` ``，Codex 寫的用 ``> `Codex implementer` ``。未標記的留言歸屬不明、要問；規則全文 → [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md#留言的角色標記)。

## 部署與發行

全域安裝、DB 路徑解析、每日排程、暫存 maildir、release 流程與 pre-push guard → [`docs/global-install-and-release.md`](docs/global-install-and-release.md)；發行、動排程、查「全域 `bes` 行為跟 repo 不一樣」之前讀。

- **打 tag 不會 rebuild** —— `release.ps1` 先建好，guard 才放行 tag。
- **全域 `bes` 是 build 快照**：repo 內 `uv run bes` 吃 working tree，其他專案的 subagent 吃上次發行的 wheel；跨專案生效必須發行。MCP server 走 `uv run --project` 跟 working tree，重開 Claude Desktop 即生效。
- **查詢結果不如預期，先確認打到哪顆 DB**：`--database` flag > `MAILATTIC_DB` > 寫死指向 repo root 的 absolute default。

## 要找東西，先看這張表

這裡列的都是正典位置；繞過它們直接 glob，通常會找到過期的複本。

| 你的問題 | 去哪 |
|---|---|
| 有沒有哪份文件講過 X？ | [`docs/INDEX.md`](docs/INDEX.md) |
| 為什麼當初這樣選？現在還能改嗎？ | [`docs/decisions.md`](docs/decisions.md) → `docs/decisions/D-YYYYMMDD-N-*.md`（不是 `docs/adr/`）。動既有設計前先查；做法與某條 ADR 相反時明講 |
| 系統「應該」有什麼行為？ | `## 行為契約` + code |
| 最近改了什麼？ | 查 PR / `git log`。刻意沒有 `CHANGELOG.md`（[D-20260609-1](docs/decisions/D-20260609-1-no-changelog-file.md)；該決策當時指定為變更史正典的 change archive 不隨本 repo 帶出） |
| 這個詞精確是什麼意思？ | [`GLOSSARY.md`](GLOSSARY.md) —— 全 repo 唯一一份詞彙表 |
| 部署現況、watchpoints、pending externals | `docs/current-status.md`（本機工作檔，不進版控，[D-20261018-1](docs/decisions/D-20261018-1-internal-working-notes-stay-local.md)；fresh clone 沒有它） |
| 接手進行中的工作、等 Sean 裁決的事 | 都在 GitHub issue + PR（等裁決的標 `ready-for-human`）；`session-continuity.md`（`/session-park` 維護）只列票號當指標 |
| issue / label 操作 | [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md)（`gh`，`sean3808/mailattic`）、[`docs/agents/triage-labels.md`](docs/agents/triage-labels.md) |
| 外部 skill（mattpocock 系列）的指示與本 repo 衝突 | [`docs/agents/external-skill-routing.md`](docs/agents/external-skill-routing.md) —— 照 skill 原文動手前先讀；skill 一律不 fork |
| 待 promote 的教訓 | `.claude/promote-candidates.md`（本機工作檔，不進版控；`/lesson-lookup` 先查這裡） |
| 本地查詢 CPU 尖峰 | 不一定是本專案（曾是本機防毒軟體的即時掃描拖慢 DB 存取）；診斷筆記只留本機，未入版控 |

`docs/decisions.md` 與 `docs/INDEX.md` 都自動產生、勿手改：新決策在 `docs/decisions/` 開新檔；harness hook 會重生索引，要立刻更新跑 `uv run python scripts/gen_doc_index.py --target all`。

## Trigger→Resolution hot

重複踩過（hit ≥ 2）的坑，四欄 pattern（Trigger 症狀 / Scope / Resolution / Negative signal），條目住 `docs/agents/trigger-resolution.md`（session-park promote 流程 append 到那裡）。Claude Code 經下一行 import 載入；不處理 `@` import 的 agent，碰 git 併發、長跑命令、守門測試、MCP 工具參數、worktree 內測試、closeout gate 失敗之前先讀那份。

@docs/agents/trigger-resolution.md

---
name: mailattic
description: >
  Sean 個人 mail server (mailattic.test) 的跨專案使用介面 — 三個 capability：拉新鮮信件、找舊信件 + 抽價、回覆既有信件。
  靠全域 `bes` CLI + `MAILATTIC_DB` env var 跨專案部署，不需 cd 到 mailattic repo。
  Capabilities: sync (bes sync-imap), find (bes search/show/thread + bes attachments 附件優先查找/匯出 + 抽價/未回球 + bes contact-history 歷年往來聚合摘要 + bes search --full-body --json 批次取回), reply (bes draft-reply → 呈審使用者 → bes send-draft/send-batch 帶草稿指紋寄出).
  Use when: user 要查歷史信件 / 報價 / 承諾 / 歷年往來摘要 / 確認今天有沒有收到某封信 / 對既有信件擬回覆 / 確認最新同步狀態。
  Do not trigger for: 從零起新 thread 寄信給新對象（platform 不支援 compose-from-scratch，見「不支援的能力」段）。
---

# mailattic — Sean 個人 mail 的跨專案介面

這支 skill 是 mailattic 在跨專案 subagent 端的對外介面層。Platform 本身是 SQLite + FTS5 mail DB（數萬封、多年累積）+ IMAP daily sync + outbound reply pipeline，CLI 名 `bes`，已裝成全域工具（安裝方式見專案 CLAUDE.md），靠 `MAILATTIC_DB` env var 解析中央 DB 路徑。

## 問題回報入口

若使用本 skill 或 `bes` CLI 時遇到 bug、文件與實作不一致、查詢結果明顯不合理、或需要新增能力，直接在原始 repo 開 issue：

```bash
gh issue create --repo sean3808/mailattic
```

回報時保留可重現命令、關鍵輸出、期望行為、實際行為；不要只把問題留在當前 session 對話裡。

三個 capability：
- **A. Sync**：把 mailbox 最新狀態拉進 DB（必要時呼叫，不是每次都跑）
- **B. Find**：從模糊記憶逐步具象成 db_id → 取原信 → 抽價 / 抽承諾 / 標未回球；另含 `bes attachments` 的附件優先 search + `--copy-to` export（副檔名語意來自 `attachment_name` / `mime_type`，不從 CAS 路徑猜）、`bes contact-history` 歷年往來聚合摘要（關係全貌）與 `bes search --full-body --json` 批次取回完整本文 + thread linkage（ingestion-grade，消除 N+1 `bes show`）；查 mail forensics header（來源 IP / Received 鏈 / SPF / DKIM）用 `bes show <id> --headers`（詳見 find.md，含 privacy 守則）
- **C. Reply**：對 DB 內既有信件擬 reply-all 草稿（可 `--attach` 夾附件）→ **把草稿呈給使用者、使用者明說寄出**（必經步驟）→ 帶草稿指紋 SMTP 送出

三者常 chain：Sync → Find → Reply。每個 capability 完整內容在 `references/` 對應檔案，依下方路由表 MANDATORY load。

---

## 共用背景（所有 capability）

> 預設全域 `bes` 已裝、`MAILATTIC_DB` 已指向中央 DB（安裝 / env var setup 屬專案層，見專案 CLAUDE.md）。本 skill 只管操作。

- DB 規模 reference：數 GB、數萬封、多年累積。其他 64 KB 的 `prompt-import*.db` 是早期 smoke test 殘留，永遠忽略；`*.bak-*` / `*.pre-mbsync-*` 是歷史備份也忽略。
- 環境是 Windows 11 / Python 3.13 / `uv`。
- 附帶工具以已發行 BES 為版本邊界：`ball-in-court.py` 透過 `uv tool dir --bin` 找全域 BES，呼叫 `thread` / `show`；直接用 `uv run {SKILL_DIR}/scripts/ball-in-court.py`，不依賴平台 working tree。正式發行會同步並核對本 skill 的 SSOT 內容。

## 核心 DB schema 摘要

```
messages(id, subject, sender_email, sent_at, body_text, message_id,
         in_reply_to, references_header, pst_source, folder_path, ...)
message_recipients(message_id, recipient_type{to|cc|bcc}, email, display_name)
message_fts(...)        -- FTS5 over subject/body/recipients/attachments
message_attachments(...)
```

`messages.id` 是 db_id（int）；`messages.message_id` 是 RFC822 Message-ID 字串（可能 None）。`message_recipients.message_id` 指向 `messages.id`（不是 RFC822 字串）— 命名 confusing 但實情如此。`bes show <id>` / `bes thread <id>` 可用整數 db_id 定位；`bes draft-reply <message_id>` 則必須傳原信儲存的 RFC822 `messages.message_id`，不接受 DB / archive row id。

## 致命陷阱（先看再動手）

這些是過去實戰踩過的坑，不看會重複浪費 turn。三個 capability 都吃這份共用清單。

1. **cp950 UnicodeEncodeError**：`bes` CLI 自身在 `main()` 入口已 `sys.stdout/stderr.reconfigure(encoding='utf-8')`，所以**直接呼叫 `bes search ...` 不需要 prefix `PYTHONIOENCODING`**。但跑 raw Python（`uv run python -c "..."` 直接讀 DB / 印中文）時仍需要：
   ```bash
   PYTHONIOENCODING=utf-8 uv run python -c "..."
   ```
   不能用 `$env:PYTHONIOENCODING=...`（PowerShell 語法，Bash tool 的 `/usr/bin/bash` 不認）。
2. **HTML body 是 80% CSS noise**：Outlook export 的 body_text 含完整 `@font-face` / `<style>` / `<!-- -->`，沒清乾淨等於沒讀。永遠用 `bes show <id> --format text` 拿乾淨版（內部走 `query.render_text()`），不要自己讀 raw body。批次多封（agent / ingestion）用 `bes search --full-body --json` 回的 `rendered_body_text`（同款清洗），不要逐封 `bes show`。
3. **FTS5 `--keyword` 語法**：空白分隔的多個 term 等價 AND；組多群組可用大寫 `AND` / `OR` / `NOT` 與括號，例如 `A AND (B OR C OR D)`。Operator **大小寫敏感**：小寫 `and` / `or` / `not` 會當 literal term。支援雙引號 phrase；含 hyphen 數字的 token（例 `PEN-100`）要用 phrase quote：`'"PEN-100"'`。目前**不支援** `*` prefix、column filter、`NEAR`。
4. **重複主題 thread 會淹沒結果**：「RE: RE: RE: [客戶]Order#xxx」這種 thread 每封都含同樣關鍵字。**帶 `--keyword` 時 `bes search` 已改為相關性排序（bm25）而非時間新到舊**，且每筆回傳 `match_in`（`new` / `quoted`）標出 snippet 顯示的那個命中落在本封新內容還是引用歷史 —— 找「最早報價 / 原始出處」時直接看 `match_in: new`，不必逐封讀 body。但這**不取代** cross-filter：要找「MUST 詞與 CONTEXT 詞在同一封共現」這種條件，仍要用 `scripts/find-cross-filter.py`（見 Stage 3）。
5. **同關鍵字多客戶混雜**（customer disambiguation 陷阱）：商品 / 產品代號常被多客戶同時詢問。不能只看 keyword 命中就回報——先用 `--participants-domain` 切客戶，跟 user 確認鎖哪一邊，再讀 body。三個 domain 旗標（`--sender-domain` / `--recipient-domain` / `--participants-domain`）皆**可重複，多值之間 OR**（如某客戶集團跨多個 domain：`--participants-domain a.com --participants-domain b.com`），單值用法 100% 向後相容；細節見 `references/capability-b-find.md`。
6. **Ball-in-court（未回球）盲區**：user 問「我有沒有報過 X 給 Y」時，命中 thread 最後一封可能是**對方在催、我方沒回**。光抓「最近一封信」會誤判「已回覆」。Capability B 報告必含「未回球」段，用 `scripts/ball-in-court.py`。**該腳本已改讀 `bes` 輸出的 `direction` 欄位判定我方／對方**（不再自己比對網域，`--our-domains` 旗標已移除）。`direction` 吃的是操作者宣告的 self 地址集合，**不是網域** —— 同網域的同事來信一律算 inbound，這是刻意的。集合未設定時沒有內建地址可退（`bes identity list` 的 `source` 為 `unset`），此時所有信都會被判成 inbound 而不報錯；未回球結果不合預期時先跑 `bes identity list` 確認集合，細節見 `references/capability-b-find.md`。thread 達 CLI 100 封上限時腳本只看得到最早 100 封，會輸出 `[indeterminate]` 並 exit 3、不下「輪到誰」結論（改用 `bes search` 依日期縮小範圍查最新信）；direction 缺失輸出 `[unknown-direction]`，不當作我方。
7. **DB cutoff 不一定是現在**：DB 最新 `sent_at` 受 IMAP sync 週期影響；若 query 涉及「今天 / 24h / 最近 / 剛剛」，Capability B 必先觸發 Capability A 的 `--pre-task`。**但這個措辭門檻有盲區**：DB stale 的真正條件是「目標信很新」，不是「使用者講了時間詞」。措辭像歷史檢索、目標卻是剛進來的新信時會漏觸發、打到 stale DB。所以**五階段主搜尋 0 命中時不可直接斷言「查無此信」**——須先跑一次 throttle-gated `bes sync-imap --pre-task` 驗證，依 stdout 首行分分支（`mbsync: pulled` → 重搜後仍 0 才是真陰性；`sync-imap: skipped` 且括號內只有時間（未含 `last sync failed` / `did not complete` / `status unknown`）→ 上次同步成功、DB 已新鮮、0 命中即真陰性；若 skipped 帶上述任一標示或 exit != 0 或 `mbsync: ERROR` → 回報「無法確認，上次同步失敗／sync 失敗，信庫可能不完整」不宣稱查無）。完整流程見 `capability-b-find.md` 失敗模式「0 命中 sync 驗證」段。
8. **寄信要先問使用者，而且在 Claude Code 裡一律帶指紋寄**：寄不寄由使用者在對話裡決定——把草稿編號、寄件帳號（`From:`）、主旨、完整 To/Cc/Bcc、內文、附件、指紋呈給使用者，使用者明說寄出才寄（多封一次呈總表、一次授權）。bes 驗不了「有沒有問過」，只驗「寄出的＝看過的」：`send-draft <draft_id> --fingerprint <sha>` / `send-batch <id>=<sha> ...`，指紋與當下草稿不符即拒寄。**Claude Code 的 `!` 不是 TTY**，agent 的 shell tool 也不是；不帶指紋在這些地方一律被拒。不要 import `_prepare_draft_for_send` / `_submit_prepared_draft` 繞過。`send-draft` 印 `Partially sent`（exit 3）／`send-batch` outcome `partial`／MCP `status="partial"` = 信已寄出但部分收件人被 SMTP 拒收，**不可整封重寄**，只對 `refused` 地址另開新草稿（#224）。stderr 出現 `SMTP close failed after accept`／結果帶 `close_warning` = 伺服器已收下信、只是結束連線失敗：**信已寄出，不可重寄**（#230）。草稿停在 `sending`（`list-drafts` 顯示 `status: sending`、`send-draft` 拒寄、`send-batch` 略過）= 上次寄送沒走完、可能已寄出：照訊息印的 marker 路徑與處理步驟走，處理步驟見 `references/capability-c-reply.md`「草稿停在「寄送中」」（#304）。完整流程見 `references/capability-c-reply.md` Step 3–4。
   **bes 不接 LLM 寫信內容**：Capability C 只做機械步驟（header 組裝 / SMTP / DB insert / 指紋驗證或 TTY 授權）。寄件設定檔由 bes 依序解析：`--env-file` → `MAILATTIC_SMTP_ENV_FILE` → 內建預設（repo 根 `.env.sean3808`），平常不必帶 flag；解析出的檔案不存在時，三個 outbound 命令在 I/O 前 exit 2 並列出三條出路。archive-scrape 的 `MAIL_SERVER_ENV_PATH` 不是 SMTP 設定來源。內文必須由 caller AI 自己寫成檔案，靠 `--body-from <file>` 餵進 `draft-reply`。收件人可用 repeatable `--to` / `--cc` / `--bcc` 追加、`--exclude` 排除推導地址，或以 `--replace-recipients` 完全取代 reply-all 推導。顯式 `--to` / `--cc` 的 bucket 優先於推導 placement；同址同時顯式列在 To/Cc 時以 To 為準。所有模式的 final To 都必須非空，否則 exit 2 且不產 draft。`--exclude` 只過濾 reply-all 推導，禁止與 `--replace-recipients` 併用；矛盾時會在讀 DB 前 exit 2，stderr 固定為 `--exclude only filters reply-all-derived recipients; --replace-recipients skips derivation entirely. Remove --exclude, or drop --replace-recipients.`。此外，`--exclude` 不得與任何顯式 `--to` / `--cc` / `--bcc` 地址不分大小寫重疊；重疊時同樣會在 env / DB I/O 前 exit 2 且不產 draft，stderr 會列出排序後的正規化衝突地址：`--exclude conflicts with explicit recipient address(es): <addresses>. Remove each address from --exclude or from --to/--cc/--bcc.`。caller 必須從 exclusion 或顯式 placement 任一側移除每個衝突，不能期待 `--exclude` 去過濾顯式清單。顯式地址必須是單一、語法完整的地址且 addr-spec 必須為 ASCII；malformed raw syntax 或非 ASCII addr-spec 會在 env / DB I/O 前 exit 2 且不產 draft，Unicode display name 搭配 ASCII addr-spec 則受支援。`message_id` 仍只負責 thread 的 `In-Reply-To` / `References`，成功時必須拿 stdout 的最終 To/Cc/Bcc 與核可 envelope 比對。主旨預設為「`Re: ` + 原主旨」；`--subject <text>` 會原樣換掉主旨（不加 `Re: `、thread 不變，空白或多行會 exit 2），錨點信主旨與這次要談的事不符時使用。任何讓 bes 自己生內容的需求都不存在，不要嘗試。
9. **跨專案呼叫契約**：跨專案 subagent 呼叫 `bes` 時，必須遵守以下四條規則，缺一都會讓呼叫耦合進 mailattic dev venv，dev venv 一壞（如 WSL/Windows 混用留下損壞的 reparse point）下游就 exit 2 crash：

   1. **用全域 `bes`（uv-tool wheel 快照執行檔）**，透過 `MAILATTIC_DB` 解析 DB。bin dir 由 `uv tool dir --bin` 取得（Windows 上本機預設為 `C:\Users\example\.local\bin\bes.exe`）。須確認解析到的是 uv-tool binary，**不是** caller venv 的 `.venv/Scripts/bes` shadow（若 caller 專案把 `mailattic` 裝進自己 venv 當 dependency，`bes` 入口點會出現在 `.venv/Scripts/` 並 PATH-shadow 全域 binary）——用絕對路徑最穩。code 內組路徑須用 `Path.home()` / `os.path.expanduser` 展開家目錄、Windows 加 `.exe`，**不可傳 literal `~`**（`subprocess.run(..., shell=False)` 不展開 tilde，照抄會 file-not-found）。
   2. **禁用 `uv run bes` 作跨專案呼叫**（`uv run` 會把執行綁到 project-local `.venv`）。此禁令只針對 `bes` CLI；bundled helper script 用 `uv run python ...` 不在此限。
   3. **禁子程序 `cwd` 指向 mailattic repo**（或任何會讓 `uv` 探測到該 repo `.venv` 的目錄）。
   4. **呼叫前 scrub 繼承的 `VIRTUAL_ENV` 環境變數**，避免 caller 自身環境干擾全域工具。

   **`uv-run-with-cwd` anti-pattern（點名禁止）**：`["uv","run","bes",...]` + `cwd=<mailattic repo>` 這組合是本契約明文禁止的耦合模式。實例：某下游腳本自組此呼叫，當 dev `.venv` 因 WSL/Windows 混用損壞時，下游 scan 直接 exit 2 crash。
10. **找寄件信：`sync-imap` 已涵蓋**：2026-07-14 起 Outlook 走 IMAP，寄件落在 server `寄件備份` folder，`bes sync-imap --pre-task` 一併同步；`scrape-archive` 排程已停用、只是人工備援，不要自動呼叫。0 命中驗證見 `references/capability-b-find.md`。
11. **archive-scrape 寄件列預設不可當 `draft-reply` 錨點**：archive 有時按投遞批次拆列，DB 的 To/Cc 可能只剩同一 domain，拿來 reply-all 會靜默漏掉跨網域收件人。優先改選同一 thread 的**進站（inbound）信件**；`bes draft-reply` 預設會拒絕 archive-scrape anchor。只有 caller 已人工核對最終收件人時，才可明示 `--allow-archive-anchor` 放行，並核對 stdout 的 To/Cc/Bcc；有人工核可清單時應搭配 `--replace-recipients --to ...`。完整送信與 90 秒節流／續跑規則見 `references/capability-c-reply.md`。
12. **附件失敗要「人」放棄，agent 不可自行放棄**：`bes db doctor` 的 `unresolved_attachment_failures` > 0 表示有附件沒補回（含沒有檔名的舊紀錄）；修不回來時由 user 決定，才執行 `bes db abandon-attachment-failure --id <id> --reason "..."`（破壞性、要 user 確認，不要代加 `--yes`）。原始信在隔離區（預設在資料庫旁的 `.mailattic-work/quarantine/<資料庫檔名>/`，跟著資料庫走；doctor 的 `quarantined_mail` 數該目錄下所有 `.eml`）直到補回或放棄。細節見 capability A 的失敗模式。

---

## Capability 路由（MANDATORY load triggers）

依 user 任務分支載入對應 reference 檔案。Reference 路徑相對本 skill 根目錄。

### Sync 任務

Trigger：user 說「同步」「拉信」「更新 DB」「sync mailbox」，或單獨要確認最新同步狀態。

**MANDATORY** — 完整讀 `references/capability-a-sync.md`。**Do NOT load** `capability-b-find.md` / `capability-c-reply.md`。

### Find 任務

Trigger：user 要找 / 列信件、查歷史報價、抽承諾、確認「今天有沒有 X」、確認對某客戶的未回球。

**MANDATORY** — 完整讀 `references/capability-b-find.md`。

子情境：
- 若 Stage 0 判斷需 sync prelude（query 含「今天 / 24h / 最近 / 剛剛 / 最新」）→ 補載 `references/capability-a-sync.md`，跑完 `--pre-task` 再回 Find 流程
- 否則 **Do NOT load** sync / reply 兩檔

### Reply 任務

Trigger：user 說「回覆」「draft 回信」「對 X 那封回信」。

**MANDATORY** — 完整讀 `references/capability-c-reply.md`。

前置：要回覆的原信 RFC822 `message_id` 必須已透過 Find 取得。
- 若 caller 已有原信的 RFC822 `message_id`（user 在前一輪指過、或從外部給）→ 直接進 Reply 流程
- 若 caller 沒有 RFC822 `message_id` → 先補載 `references/capability-b-find.md`，以 `bes search ... --full-body --json` 找到原信並取用 hit 的 `message_id` 欄位，再回 Reply 流程；不可拿整數 `id` / `db_id` 當 `draft-reply` 錨點
- **Do NOT load** `capability-a-sync.md`（reply 不依賴 fresh sync）

---

## Capability chain 表

常見 user 意圖對應的 capability chain：

| User 說 | Chain | 載入順序 |
|---|---|---|
| 「今天有沒有收到 X 客戶的信」 | A → B（時間=今天） | sync.md → find.md |
| 「以前那封 Y 報價是多少」 | B（含 Stage 3.5 抽價） | find.md |
| 「我跟 X 往來多久了 / 都誰在跟我聯絡」 | B contact-history aggregate mode | find.md |
| 「找 Z 那封信，然後我要回他」 | B → C | find.md → reply.md |
| 「最近 24h 有沒有 W 的新信，有的話回他」 | A → B → C | sync.md → find.md → reply.md |
| 「列今天所有信」 | A → B Stage 0.5 browse mode | sync.md → find.md |
| 「同步一下」 | A only | sync.md |
| 「對 Message-ID `<abc123@example.com>` 寫個 reply」 | C only（caller 已給 RFC822 `message_id`） | reply.md |

每個 chain 結束時主動問 user 下一步：找信完問「要回信嗎？」是 → 進 C；不是 → 收尾。

---

## 不支援的能力（明文）

防止 caller AI 浪費 turn 嘗試這些 platform 不做的事：

- **Compose from scratch / 指定新 To 起新 thread**：platform outbound-send capability 仍要求 DB 內既有信件的 `<message_id>` 作 thread anchor。`draft-reply --replace-recipients --to ...` 只能替換該 reply 的 envelope，不會建立無 anchor 的新 thread；仍然**沒有** `bes draft-new` / `bes compose`。User 要寄全新信 → 告訴 user「請開 Outlook 自己寄」，不要嘗試繞過
- **改既有 draft 內容**：`send-draft` 前可以刪 `.eml` 重產，但**不要編輯 `.eml`**（bes 不重新計算 In-Reply-To / References / Message-ID）
- **送資料夾內全部 draft**：`send-batch <draft_id>=<sha> ...` 支援對**明確列舉的 draft_id 清單**一次呈審、一次授權批次送（逐封驗指紋）（逐封套用 send-once/from-invariant/SMTP，部分失敗不中止、已送自動 skip）；但**沒有**「送 outbound/drafts 裡全部 draft」的模式（避免誤送殘留草稿），一律要逐一列出 draft_id
- **語意 / 向量搜尋**：Capability B 只走 FTS5 keyword + SQL LIKE；沒有 embedding / semantic search
- **附件 OCR / 內文抽取**：`message_attachments` 只索引檔名，附件內容不在 DB
- **MCP server / API 服務化**：**你（host subagent）的對外接面只有 CLI + Python API**；沒有 HTTP 服務，也不要嘗試走 MCP。唯一例外是一個**唯讀** MCP bridge，只供 CoWork 沙箱使用（沙箱看不到 host CLI／DB），已上線六個唯讀工具 —— 那條路你走不到也不需要走，同樣的查詢用 `bes` 子命令即可。見專案 [D-20260721-1](../../docs/decisions/D-20260721-1-revisit-mcp-for-cowork.md) 與 [MCP server 契約](../../docs/mcp-server-contract.md)
- **問「資料有多新 / 上次同步何時跑的」**：CoWork 那條 MCP 路有 `mail_sync_status`，但 CLI 這邊**沒有對應子命令**。你要確認新鮮度就照 Capability A 直接跑 `bes sync-imap --pre-task`（15 分鐘內會自己 skip 並印出上次同步時間與是否失敗；要結構化結果加 `--json`，看 `status` 與 `last_sync_failed`（skipped 時固定有此欄、為 bool）），不要去讀 log 檔或 DB 表自己拼

---

## 收尾紀律（所有 capability 共用）

- 暫存檔（`.tmp_search.json` 之類）查完即刪：`rm .tmp_*.json`
- 若 user 之後要持續引用原信，把它存到 **user 當前專案根** `originals_<topic>_<keyword>.txt`，告訴 user 路徑；**不要**寫回 mailattic repo（跨專案使用要保持來源 repo 唯讀）
- Reply 用的 body 檔（`reply_<topic>.txt`）同理放 user 當前專案根
- 不要試圖把找到的信寫進 MEMORY — 信件 canonical 就在 DB，pointer 即可

---

## 為什麼這 skill 存在（跟直接呼叫 bes 比的差異化價值）

bes CLI 已涵蓋大部分機械步驟：HTML 清洗（`bes show --format text`）、thread 重建（`bes thread`）、IMAP sync（`bes sync-imap`）、reply pipeline（`bes draft-reply` / `list-drafts [--json]` / `send-draft`）。但跨專案 subagent 不知道：

- **何時該 sync vs 不該 sync**（Capability A）
- **找信什麼時候五階段、什麼時候 browse mode**（Capability B Stage 0.5 escape hatch）
- **多客戶混雜時怎麼切分 + 抽價**（Capability B Stage 3.5）
- **未回球 / negative evidence 報告紀律**（Capability B Stage 4 回報格式）
- **Reply 流程跟前提**（Capability C：呈審必經步驟 + 草稿指紋、`!` 不是 TTY、SMTP / BCC-self / 寄件備份可見性、`--body-from` 寫檔規矩）
- **不存在的能力**（compose-from-scratch、bulk send、語意搜尋）

skill 把這些**判斷規則 + 失敗模式 + 邊界**寫進 reference 檔案 + bundled scripts，避免每次重新發現 bes 的瓶頸。CLI 已能做的純 delegate，不重複實作。

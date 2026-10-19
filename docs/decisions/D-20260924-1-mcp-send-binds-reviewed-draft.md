---
id: D-20260924-1
date: 2026-09-24
status: accepted
title: MCP 寄信能力（mail_draft_reply + mail_send）採對話層指紋授權、草稿儲存物理隔離與顯式設定檔
---

## Context

CoWork 沙箱在 Windows host 經 Claude Desktop bridge 呼叫 MCP server。在唯讀查詢（六個工具）與附件匯出（`mail_attachment_export`，issue `#28`）之後，CoWork agent 需要具備郵件回覆能力（issue `#189`：`feat(mcp): mail_draft_reply + mail_send — 對話層授權寄出`）。

在 CLI 形態中，[D-20260914-1](D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md) 定案了對話層授權模型：agent 呈審草稿與指紋（SHA-256），使用者於對話中明說寄出後，agent 帶草稿指紋送出；核心函式驗證「寄出的＝看過的」。

MCP 環境下實作寄信能力，面臨四個設計決策：
1. **授權形態**：MCP 是否沿用指紋授權？
2. **草稿儲存隔離**：MCP 產生的草稿應放在哪裡？與 CLI 草稿是否共用目錄？
3. **SMTP 設定來源**：MCP server 是否沿用 CLI 的預設 `.env.sean3808` 還是要求顯式設定？
4. **架構分層與契約**：如何維持行為契約 #1（MCP 唯讀性），並為 MCP 送信建立機械絆索？

## Decision

### 1. 對話層指紋授權（沿用 D-20260914-1 模型）
- 分割為兩個獨立工具：`mail_draft_reply`（產出草稿與指紋）與 `mail_send`（驗證指紋並發送）。
- `mail_draft_reply` 回傳 `draft_id`、完整 envelope（To/Cc/Bcc）、主旨、草稿指紋（`fingerprint`，`.eml` bytes SHA-256）以及依字元預算降級的 `body_text`。
- `mail_send(draft_id, fingerprint)` 依指紋比對當下草稿檔案：
  - 指紋相符才允許寄出。
  - 指紋不符時**立即拒寄**且**不寫入已寄出標記（send-once marker）**，讓修正或重新呈審後的草稿仍可正常處理。
  - 錯誤訊息刻意不印出當下計算之正確指紋，防止 LLM agent 自行抄寫新指紋繞過呈審。

### 2. 草稿目錄物理隔離（`outbound/mcp-drafts/`）
- MCP 草稿目錄預設指向 `outbound/mcp-drafts/`，與 CLI 使用的 `outbound/drafts/` 物理隔離。
- 納入 `.gitignore`，避免草稿位元組污染 git tree。
- **交叉拒絕**：CLI 產生的草稿無法透過 `mail_send` 寄出；MCP 產生的草稿也無法透過 CLI `bes send-draft` 寄出。防止跨通道 confused deputy。

### 3. SMTP 設定採顯式環境變數（Option B: Fail Closed）
- MCP server 不提供對本機預設檔（`.env.sean3808`）的隱式 fallback（Sean 2026-09-24 拍板，見 `#189` 留言：CoWork 配 MCP 本來就靠 `env` 區塊設定，不讓 MCP 繼承 CLI 的隱式預設）。
- `mail_send` 僅讀取 `MAILATTIC_SMTP_ENV_FILE` 環境變數：
  - 未設定、空字串／全空白、檔案不存在或讀不到、或解析失敗時，立即回報工具錯誤、不寄、不寫 send-once marker。
  - 設定檔只在寄出當下載入；`mail_draft_reply` 不讀它（草稿 `From:` 固定為本人地址，與設定帳號不符時由寄出端的 from-invariant 拒寄）。**本條於 2026-10-13 由 [D-20261013-1](D-20261013-1-mcp-draft-from-follows-smtp-account.md) 推翻**：擬稿時讀寄件設定，From 用設定的帳號。
  - 載入之 env mapping 一併傳入 `_submit_prepared_draft(..., env=env_mapping)`，確保後續 IMAP APPEND 能取得到憑證資訊（避免 issue #114 之 APPEND 靜默跳過）。

### 4. 寫入存取層與行為契約 #7
- 維持行為契約 #1：`mcp_readonly.ReadOnlyMailReader` 維持唯讀不變，`mcp_server.py` 不得直接 import 底層修改函式。
- 所有寫入動作（`export_attachments`、`draft_reply`、`send_draft`）單點收斂於 `mcp_writer.MailWriter`。
- 擬草稿查錨定信時以唯讀連線開 DB（`assemble_draft_reply(read_only=True)`）。寄出則走 CLI 共用的送出路徑（issue 指定），其送出後的收尾會把已寄信件記進 DB，行為與 `bes send-draft` 相同 —— 這是 MCP 第一個會寫 DB 的路徑，經 `MailWriter` 而非 reader。契約 #1 原文「對 DB 永遠唯讀」與此衝突，Sean 於 `#189` 2026-09-25 裁決改寫條文：讀取只能經唯讀路徑，唯一的寫入是 `mail_send` 記錄已寄信件（否決「擬草稿也收回 reader」與「MCP 寄出不記 DB」）。
- 附件路徑可由 CoWork 指向任意 host 檔，防線是呈審時列出完整來源路徑與大小、由使用者在對話中把關 —— 明文接受的殘留風險（同上裁決）。
- 新增**行為契約 #7（MCP outbound 寄出＝看過的草稿）**，並由 3 支 anchor test 釘住：
  1. `test_mcp_send_fingerprint_mismatch_refused_without_marker`
  2. `test_mcp_send_refuses_cli_drafts`
  3. `test_submit_prepared_draft_caller_whitelist`（AST 白名單嚴格限制 `_submit_prepared_draft` 生產調用者僅有 `outbound.send_draft`、`outbound.send_batch`、`mcp_writer.MailWriter.send_draft`）

### 5. 字元預算降級階梯
- `mail_draft_reply` 輸出受 `MCP_RESPONSE_BUDGET_CHARS`（120,000 字元）約束。
- 當包含超長本文時，優先對 `body_text` 執行截斷降級階梯（保留前後摘要），並在回傳結果設置 `response_truncated=true`。
- 內文一旦被截斷，回應的 `fingerprint` 為 `null`：使用者看到的不是指紋涵蓋的那份草稿，給出指紋就等於允許寄出沒看過的內容。實務上觸發門檻是 12 萬字元的回應預算，一般回信碰不到。
- envelope 與 `draft_id` 為定位關鍵欄位，永不截斷。

## 與既有決策的關係

- **補強、未推翻 [D-20260730-1](D-20260730-1-mcp-capability-parity-and-v2-first-sequencing.md)**：該決策定「寄信工具只吃 `draft_id`、不吃內容」。指紋不是內容，是對「使用者看過的那份草稿」的綁定，所以原則不變，只是補上 `draft_id` 單獨擋不住的兩個洞。
- **沿用 [D-20260914-1](D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md)** 的 CLI 指紋模型；MCP 多出的是「分資料夾」這一半。

## 被否決的做法

- **`mail_send` 只吃 `draft_id`**：留下兩個洞 ——（1）看完後被改：呈審後草稿檔被換掉，寄出的不是使用者看過的；（2）來源洗白：CoWork 拿 CLI 擬的、使用者沒在這段對話看過的草稿編號來寄。指紋擋前者，分資料夾擋後者。
- **工具層結構化核可（MRTR）**：`#174` 已關，Claude Desktop 未宣告 elicitation capability，工具層問不到使用者。

## Consequences

- CoWork / Claude Desktop agent 可完整執行「擬稿呈審 → 使用者確認 → 帶指紋寄出」的閉環。
- 新增 `outbound/mcp-drafts/` 目錄並列入 `.gitignore`。
- Claude Desktop MCP 設定檔需在 `env` 區塊配置 `MAILATTIC_SMTP_ENV_FILE`。
- 行為契約總數擴增至 7 條，`.claude/hooks/contract_gate_reminder.py` 與 `AGENTS.md` 同步註冊。

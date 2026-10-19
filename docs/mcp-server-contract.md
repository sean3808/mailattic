# MCP server 契約（CoWork bridge）

> 對 host subagent 不做 MCP（CLI 走 Bash tool 已足夠）。**對 CoWork 開例外並已驗證成立**：CoWork 沙箱看不到 host CLI／DB／網路，故在 Windows host 跑一個 MCP server，CoWork 經 Claude Desktop app bridge 呼叫它。Spike A 已於 2026-07-23 實測 `GO` —— 見 [D-20260721-1](decisions/D-20260721-1-revisit-mcp-for-cowork.md)。能力契約的現行正典是本檔（Spectra 時期的 `cowork-mcp-bridge` spec 不隨本 repo 帶出）。

## 目前能力面

讀取工具六個：`mail_search`（找）／`mail_show`（開單封）／`mail_thread`（看整串）／`mail_contact_history`（歷年往來摘要；封數含 `inbound_count` / `outbound_count` / `unknown_count`，三者相加等於 `total_messages`）／`mail_attachments`（附件 metadata，永不回傳檔案內容）／`mail_sync_status`（同步新鮮度，零參數）。

寫入工具三個：
- `mail_attachment_export`（issue `#28`，D-20260730-1 第 4 步 write tier）—— 把 CAS 已存的附件複製到呼叫端指定的 **host 絕對路徑**，回傳每筆落地後的絕對路徑，不回檔案內容。
  - 輸入：`attachment_ids`（`mail_attachments` 的 `attachment_id`，或 `mail_show`／`mail_thread` 的 `attachments[].id`），每次 1～50 筆，重複的 id 只回一筆；`dest_dir` 必須是絕對路徑、至多 200 字元，不存在就建立。相對路徑、超量直接拒絕，不寫任何檔。
  - 路徑由呼叫端決定（#167 定案模型 A）：CoWork 知道自己連進了哪些資料夾，傳一個自己讀得回來的 host 路徑；傳錯當場就看得到。
  - `dest_dir` 落在附件庫（CAS）內一律拒絕：本工具對 CAS 唯讀。每筆寫出前先比對 SHA-256 與 `content_hash`，CAS 檔損毀的那筆回 `failed`、不寫任何檔；寫到一半失敗會刪掉自己剛建的殘檔，避免下次被誤判成 `skipped-existing`。
  - **永不覆寫，也沒有覆寫選項**：目的檔已存在回 `skipped-existing`，原檔一個 byte 都不動 —— 防的是 CoWork 被信件內容操控後蓋掉 host 既有檔案。
  - **不匯出開啟即執行的檔案類型**（issue `#205`，Sean 2026-09-29 拍板 (A)）：落地檔名（含無副檔名時依 MIME 推得的副檔名，大小寫與 Windows 會吃掉的結尾點／空白一併正規化）屬於 Windows 開啟即執行、安裝或追隨的類型（執行檔與安裝檔、批次與腳本、Python 含 `.pth`、捷徑、`.reg`／`.inf`／`.chm`／`.theme`、Office add-in 等，清單在 `mcp_writer._BLOCKED_EXPORT_EXTENSIONS`；比對前以大寫正規化，與 Windows 相同，`.vbſ` 視同 `.VBS`）時回 `skipped-blocked-type`、`dest_path` 為 `null`，連 CAS bytes 都不讀。防的是 CoWork 被信件內容誘導，把寄件者可控的 `.bat`／`.lnk` 匯出到 Startup 等開機自動執行位置；自動執行位置列舉不完，所以擋檔案類型而不擋目錄。**只擋 MCP 路徑**：CLI `bes attachments --copy-to` 不受影響（使用者自己下指令不在此威脅模型內）。代價（明文接受）：寄件者寄來的安裝檔／腳本要由使用者自己從 Outlook 存檔。開啟後仍需使用者決定才會執行的類型（預設「停用並通知」下的巨集文件、壓縮檔）不在清單內。會拿掉那道決定的資料夾另外整個拒絕：`dest_dir` 落在 `%APPDATA%\Microsoft` 下的 Startup、Excel `XLSTART`、Word `STARTUP`、`Templates`、`Addins`（前三者開機或啟動 Office 時自動開啟；五者皆為 Office 預設受信任位置，巨集不經提示就執行）時直接報錯、不寫任何檔（含子目錄；先建目錄再以檔案系統身分比對，所以大小寫、`\\?\` 前綴、結尾點／空白、8.3 短檔名等 Windows 會正規化掉的寫法都算同一個資料夾；`APPDATA` 缺席時退回使用者設定檔的預設位置，不會因環境變數被裁掉而失效）。已知殘留：使用者自訂的 Office 受信任位置／替代啟動資料夾不在清單內；被擋的呼叫仍可能先建出空目錄（不含任何檔案）。
  - 逐筆狀態：`exported`／`skipped-existing`／`skipped-non-exportable`（只有 metadata、CAS 沒有 bytes）／`skipped-blocked-type`（開啟即執行的類型，見上）／`failed`（含查無此 id），附 `dest_path`、`attachment_name`、`size_bytes`、`content_hash`、`detail`。檔名規則與 CLI `bes attachments --copy-to` 相同。
- `mail_draft_reply`（issue `#189`，D-20260924-1）—— 對指定信件建立回信草稿（預設存於 `outbound/mcp-drafts/`），輸出 `draft_id`、完整 envelope、主旨、`fingerprint`（`.eml` bytes SHA-256）與呈審內文。草稿的 From 是 `MAILATTIC_SMTP_ENV_FILE` 寄件設定的帳號；讀不到設定時退回 `self_addresses` 表裡唯一的那一筆，零筆或多筆就拒絕擬稿（[D-20261013-1](decisions/D-20261013-1-mcp-draft-from-follows-smtp-account.md)）。
  - 輸入：`message_id`、`body_text`，選填 `to`、`cc`、`bcc`、`exclude`、`replace_recipients`、`attachments`。不提供 CLI 的 `--allow-archive-anchor`：那是「呼叫者已人工核對收件人」的聲明，CoWork 被信件內容誘導時不該能代為聲明。
  - 輸出包含：`draft_id`、`fingerprint`、`message_id`、`from`、`subject`、`to`、`cc`、`bcc`、`body_text`、`attachments`（含來源 host 路徑與大小），並帶 `response_truncated`：當 `body_text` 龐大時啟動降級階梯縮減內文（保留前後摘要），其餘 metadata 不截斷。**內文一旦被截斷，`fingerprint` 回 `null`**：使用者沒看到全文，就沒有「看過的草稿」可寄；草稿仍留在資料夾，但無法經 `mail_send` 寄出。
- `mail_send`（issue `#189`，D-20260924-1，契約 #7）—— 帶草稿指紋執行對話層授權寄出。
  - 輸入：`draft_id`、`fingerprint`。
  - 嚴格比對指紋：指紋不符立即拒寄且不寫 send-once marker。
  - 拒送 CLI 草稿：MCP 只能寄 `outbound/mcp-drafts/` 內草稿，與 CLI 草稿目錄隔離。
  - 僅吃顯式 `MAILATTIC_SMTP_ENV_FILE` 設定，無隱式本機 fallback。
  - 失敗訊息（`#292`）：我方撰寫的寄出失敗訊息一律把「結果」（沒寄出／可能已寄出）與「下一步」放在最前面、識別碼與伺服器回覆放在最後，因此經 200 字上限後兩者仍在；上限本身不變。寄出前的兩種系統錯誤（`#307`）——讀不了草稿檔、寫 send-once marker 失敗（事後確認 marker 仍不存在才算）——訊息開頭同樣是「Not sent」加「修好後以同一組 draft_id 與 fingerprint 重呼叫 `mail_send`」；這兩處都在 SMTP 之前，沒有重複寄出的風險。準備階段讀 marker 就失敗的，維持既有的「draft preparation failed; not sent. Retry, or create a new draft.」。連線中斷或收尾失敗屬「結果不明」，訊息要求重寄前先確認投遞狀態，不可自行重擬再寄。
  - 輸出：`status="sent"`、`message_id`、`draft_id`。SMTP 在 RCPT 階段拒了部分收件人時（`#224`），`status="partial"` 並多帶 `accepted`（地址清單）、`refused`（`[{address, code, reason}]`）、`warning`：信**已寄給**被接受者、marker 為 `sent` 且記下 `refused`，不可對同一草稿再呼叫 `mail_send`，只能對被拒地址另擬新草稿。全部收件人被拒則是工具錯誤（marker 停在 `sending`；錯誤訊息帶 marker 路徑與處理步驟，`#304`）。`sent` 不是投遞保證，`partial` 也不是整封失敗。DATA 已被接受、只有收尾的 QUIT/關連線失敗時（`#230`），仍是 `sent`（或 `partial`）並多帶 `close_warning`：信已寄出、已記錄，不可重寄。
- DB 讀取唯讀、CAS 唯讀：DB 讀取經 `ReadOnlyMailReader`（擬草稿讀原信也用唯讀連線）；磁碟寫入與送信經另一個 accessor `mcp_writer.MailWriter`。MCP 對 DB 唯一的寫入是 `mail_send` 寄出成功後記錄已寄信件，與 `bes send-draft` 相同（契約 #1；`mcp_server.py` 只經由該 accessor 觸及寫入動作，由 AST guard 釘住）。

`mail_sync_status` 回四個扁平事實，**恆常四個欄位都在，值不明時是 `null` 而不是消失**：`last_sync_started_at`（上次同步開始）／`last_sync_ended_at`（上次同步結束，未結束為 `null`）／`last_sync_status`（`never_synced`｜`running`｜`completed`｜`failed`）／`latest_message_at`（DB 內最新信件時間，查詢時即席由 `messages` 導出，不是另存的計數器）。

**它不算任何新鮮度結論** —— 沒有布林、沒有過期秒數、沒有綜合分數。「夠不夠新」的門檻是情境相依的，寫死在工具裡等於替所有呼叫端決定。兩個時間戳也刻意分開：成功但零新信會讓 `latest_message_at` 原地不動，那跟「同步停了」是不同的事。

**`running` 卡住不是 bug，是訊號**：狀態在 IMAP client 建構之前就寫下（連不上伺服器是排程最可能的死法），結束時才補上終態。所以 `last_sync_started_at` 是好幾小時前而 `last_sync_ended_at` 仍為 `null`，代表那趟同步死了。沒有任何機制會去回收它 —— 自動回收需要一個 timeout 門檻，而實測 run 長度從 2m43s 到 22m56s，沒有不武斷的值可選。

**這個時間戳不是 `bes sync-imap --pre-task` 節流閘讀的那一個。** 節流閘讀的是同步 log 檔，語意是「最後一次*嘗試*結束」（不篩 exit status，失敗的 run 一樣會讓它跳過 15 分鐘）；本工具讀的是 DB 內的 run 記錄，帶明確終態。兩者在同一趟 run 內各自寫入，是兩個來源、不是同一個事實 —— 收斂成單一來源是後續工作。（節流閘的門檻仍只讀 log；但 skipped 輸出的失敗標示另外會讀本工具這列 run 記錄，抓出寫 log 前就中斷的 run，`#305`。）

舊 DB 沒有 `sync_state` 表時一律回 `never_synced`，不報資料庫錯誤：MCP server 唯讀開啟、永遠建不出那張表，在下一次同步把它建起來之前，「表不存在」與「沒有那筆記錄」是同一件事。

**能力面仍在擴張中**（方向是 Phase 2 sync → Phase 3 write tier，讓 CoWork 承擔更多非 coding 工作），最終邊界尚未收斂，勿當成已凍結的完整能力。host subagent 仍照舊走 `bes` CLI，不受此例外影響。

## 回應大小的保證是三層，不可壓成一句「一律不超過預算」

Claude Desktop 在約 150,000 字元截斷 tool result，且是用一個檔案指標字串**取代整份 JSON**（呼叫端收到的不是 JSON，症狀是 parse error 而非「資料太多」）。

本專案的量測標準＝**交付結果序列化成 JSON 文字後的字元長度、非 ASCII 不跳脫**（貼近實際上線大小，中文信箱不會被算成六倍）；預算＝模組常數 `MCP_RESPONSE_BUDGET_CHARS = 120_000`，與觀測到的 host 上限之間的餘額留給本量測不含的 JSON-RPC 外框，以及「host 自己怎麼數並未公開」這件事。

**這個量測是 wire 的上界，不是 wire 的鏡像**（mcp 2.x 起）：框架送出前會過一道 per-protocol-version sieve，把協商版本 schema 沒有的欄位剝掉。stdio 最高只協商到 `2025-11-25`（`2026-07-28` modern wire 只走 header-routed transport），該版沒有 `resultType`，所以公式恆比 wire 多 `,"resultType":"complete"` 這 24 個字元。Windows 實測：公式 31,049 / wire 31,025 / 差 24，其餘欄位逐一相等、wire 上沒有公式漏算的欄位。**高估是安全方向**——只會誤拒一個本來裝得下的結果，不會放行裝不下的。另：in-process client 走完整協定往返，會多蓋一個 `_meta.serverInfo` 戳記（約 94 字元），stdio 交付沒有；錯誤路徑的量測走 client，故同樣偏保守。two-channel 加倍比例實測 2.095（logical 31,694 → delivered 66,394）。

三層是：

1. **成功結果 —— 執行期量測**：每個工具各自組出要交付的 `CallToolResult` 並當場量。超標者依自己的降級階梯逐步縮小重量，取第一個進預算的狀態；階梯走完仍不進則拒絕。四個 wrapper 形狀的工具帶恆常存在的 `response_truncated` 布林，用來區分「是交付預算砍的」與「是你自己參數砍的」；`mail_search` 的契約不容許靜默縮短列表，超標一律報錯而不降級，也不帶此欄位。`mail_sync_status` 同樣不帶，但理由不同：它的 payload 是四個短純量、大小固定，沒有可縮小的東西，也就沒有階梯。`mail_attachment_export` 也沒有階梯：檔案在量測前就已寫下，縮短清單等於藏起已落地的檔案，所以改由輸入上限（50 筆、`dest_dir` 200 字元）保證進預算（最壞實測約 95,550）；萬一仍超標，拒絕訊息明說檔案可能已寫入、請以較少 id 重呼叫取回路徑。`mail_send` 的成功回條（`sent` / `partial`）同樣走階梯，但**階梯尾端是成功、不是拒絕**（信已寄出，報錯會誘使重寄）：未超標時與原先逐位元組相同、不帶 `response_truncated`；超標時依序縮 `refused[].reason` 到 100 字 → 丟掉 reason → `accepted` 與 `refused` 各只列前 25 筆 → 兩份清單清空、只留 `accepted_count` / `refused_count`（恆為精確值）。縮減後的回條帶 `response_truncated=true`，`warning` 另明說清單可能不完整、請勿重寄；完整的 `refused` 清單記在 host 上該封信的 send-once marker。
2. **本 server 自建的錯誤訊息 —— 靜態上界**：錯誤只能經過恰好兩個具名 construction point 建立：一般的 `build_tool_error` 會截斷所有 keyword fragments，環境錯誤專用的 `build_environment_error` 只額外放行已解析的 DB 路徑不截斷；這是封閉的 construction path，不是靠每個 call site 記得呼叫共用 cap helper。訊息＝固定模板＋四類受限片段（呼叫端回音一律截斷並標示省略、具名的儲存記錄只用數字 id／`message_db_id`／`attachment_id`、SQLite 引擎與非預期例外的診斷字串同樣截斷），唯一放行不截的是環境錯誤裡已解析的 DB 路徑（由 Windows 32,767 字元延伸路徑上限兜底）。
3. **框架自己的參數驗證錯誤 —— 只有經驗上界＋回歸測試釘住**：型別不合的參數由框架的 Pydantic 驗證在工具本體執行前就擋掉，訊息是框架的，我們改不了。**mcp 2.x 起它的交付形式是一般的 `isError=true` 結果**（1.x 是協定層錯誤），所以現在跟前兩層走同一條量測路徑——但**歸屬沒變**，訊息仍是框架在我方任何程式碼跑之前產生的，故仍屬第三層而非第二層。`mcp>=2,<3` 是 generation constraint，用來把解析鎖在已量測過的世代（排除介面不同的 1.x 與未量測的 3.x），不是精確版本 pin。**實測值只記在回歸測試裡，不寫進本文件**——上一代把數字寫死在敘述裡，正是它過期後還讀起來像保證的原因。目前 lockfile 解析到 2.0.0；另以 disposable wheel 安裝驗證實際 package-metadata resolution（specifier `mcp<3,>=2`）落在 2.x 並在 budget 內，兩邊量到同一個值，而不宣稱所有可解析版本都有相同格式。

識別碼與 locator 類欄位（Message-ID／References／儲存路徑／email）仍是無長度限制的 `TEXT`、寫入不驗長度、**任何階梯步驟都不得截斷** —— 這正是少數記錄「真的交付不了」而非「只是太大」的原因，那種記錄會走完階梯後拒絕。實作見 GitHub issue `#26`。

**「不得截斷」講的是同一個工具帶了這個欄位時的處理方式，不是每個工具都必須帶它。** `mail_search` 的附件項目**整個不帶** `content_hash` 與 `storage_path`（issue `#178`）：那是 allowlist 決定，不是階梯截斷。理由是職責分工 —— `mail_search` 找的是候選**信件**，定位檔案位元組是 `mail_show` 與 `mail_attachments` 的事，那兩個工具照舊完整帶出兩個欄位。代價量測過：一個簽名檔五張內嵌圖的信箱，那兩欄佔掉 `mail_search` 摘要交付預算的三分之一，30 封摘要因此撐破 120,000 而整批失敗。

## 驗證分兩層：程式化測試在下、LLM client probe 在上

`tests/test_mcp_server.py`（in-process client）與 `tests/test_mcp_stdio_contract.py`（真 subprocess 走 JSON-RPC）是下層，兩者**都預先知道要呼叫哪個工具、帶什麼參數**。它們證明的是「照這樣呼叫，server 行為正確」。

上層是 [`mcp-client-probe-loop.md`](mcp-client-probe-loop.md)：真的 headless LLM client 只讀工具的 title / description / JSON schema，自己決定要叫哪個工具、參數怎麼帶。issue `#54` 就是只有這一層抓得到的形狀 —— client 把整數 `message_id` 字串化，server 拒絕，而下層測試沒有理由產生那個輸入。改動任何工具描述或 schema 之後，下層全綠**不代表**描述對 LLM 讀者清楚。

> probe loop 是**諮詢性的、非決定性的**，不擋 commit 也不擋 release。與它相鄰的 `tests/test_probe_fixture.py`（fixture validator）與 `tests/test_probe_supervisor.py`（安全閘門）則是一般確定性測試，照常跑在 canonical pytest routes 上。界線是：**會啟動 headless LLM client 的才是諮詢層**。

## 對框架的兩處耦合（刻意，不是疏漏）

1. 所有工具維持 sync `def`，mcp 2.x 會把它們派到 worker thread 執行，安全性來自「每次呼叫各開一條 DB 連線」而非 sqlite3 本身 thread-safe —— 共用連線跨 thread 仍會拋 `ProgrammingError`。
2. `mcp_delivery.build_call_tool_result` 要拿工具的 `fn_metadata` 才能把 two-channel 組裝**委派**給框架，這條路只有私有的 `server._tool_manager.get_tool()` 走得到（公開的列舉方法回的是協定層描述，不帶那個轉換），生產程式碼有 5 處這樣用（工具數是七，此處仍是五：`mail_sync_status` 與 `mail_attachment_export` 沿用共用的成功結果 helper，沒有自己再伸手一次 —— 兩個數字不必同步移動）。委派優於自己重做，因為重做只能事後偵測漂移、委派則讓漂移不可能發生——代價就是框架換代時本 repo 會是 spec 級變更而非單純升版。

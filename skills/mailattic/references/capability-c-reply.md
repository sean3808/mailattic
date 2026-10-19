# Capability C — Reply（回覆既有信件）

> 此檔由 `SKILL.md` 的 Capability 路由表 MANDATORY load 進來。
> 前置：要回覆的原信必須已透過 Capability B 找到，並取得其儲存的 RFC822 `message_id`（不是整數 DB / archive row id）。若 caller 未先跑 Find，**先載入** `capability-b-find.md`。
> 不需載入 `capability-a-sync.md`（reply 不依賴 fresh sync）。

## 何時用

- User 說「幫我回覆 X 那封」「draft 一個 reply 給 Y」「我要對某封信回信」
- Capability B 找到原信後，user 跟著說「OK 對這封寫個回覆」
- **前提**：要回覆的對象必須是 **DB 內既有信件**（thread 已存在）。從零起新 thread 寄新對象 → 見 SKILL.md「不支援的能力」段

## 怎麼用

四步走，**內文 caller AI 自己寫成檔案，bes 純機械步驟；寄不寄由使用者在對話裡決定**：

**寄件設定檔由 bes 自己解析，平常不必帶 `--env-file`**：`draft-reply` / `send-draft` / `send-batch` 依序取 `--env-file <path>` → 環境變數 `MAILATTIC_SMTP_ENV_FILE` → 內建預設（repo 根 `.env.sean3808`）。只有要改用別的寄件帳號時才帶 flag。設定檔須含 `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD`；archive-scrape 的 `MAIL_SERVER_ENV_PATH` 不是 SMTP 設定來源。設定檔在但讀不出寄件帳號時，`draft-reply` 改用 `bes identity list` 裡唯一的那一筆當 From，零筆或多筆就 exit 2 並說明要設哪一項（bes 沒有內建的寄件地址）。寄件帳號在 Step 2 stdout 的 `From:` 露出，呈審時一併給使用者看（[D-20260915-1](../../../docs/decisions/D-20260915-1-smtp-settings-file-resolved-by-bes-with-from-shown-at-review.md)）。

### Step 1：caller AI 把要回的內文寫成檔案

```
<user 當前專案根>/reply_<topic>.txt
```

純文字檔，UTF-8 編碼，不含 header（To/From/Subject 全部 bes 自動組；主旨要換用 `--subject`，見 Step 2）。user 視情況改完後進 Step 2。

### Step 2：產 draft 草稿

**先選安全錨點**：archive-scrape 寄件列只是找信證據，不保證是完整的 reply-all 證據。伺服器有時會把同一封寄件按投遞批次拆列，該列的 To/Cc 可能漏掉其他 domain。優先錨定同一 thread 的**進站（inbound）信件**；若傳入 archive-scrape 列，bes 預設拒絕且不產 `.eml`。

**取得錨點值**：Find 階段以 `bes search ... --full-body --json` 取回命中信件，從該 hit 的 `message_id` 欄位取出完整 RFC822 Message-ID（例如 `<abc123@example.com>`），再把它原樣當作 `bes draft-reply` 的第一個參數。不要傳 search/show 顯示的整數 `id` / `db_id`。

若沒有 inbound anchor，但 caller 已人工核對正確 envelope，可明示 `--allow-archive-anchor`。這個 flag 是「我已人工核對」的安全聲明，不代表 archive 天生完整；bes 仍會在 stderr 警告，並在 stdout 印出最終 To/Cc/Bcc。若 caller 手上有核可清單，應同時用 `--replace-recipients`，讓 archive message_id 只負責 thread anchoring：

```bash
bes draft-reply <rfc822_message_id> --body-from <user 當前專案根>/reply_<topic>.txt

# 夾附件（--attach 可重複）：
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --attach "supplier-quote.pdf" --attach price-list.xlsx

# 在 reply-all 推導結果上加人（各 flag 都可重複）：
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --to buyer@client.example --cc sales@example.com --bcc audit@example.com

# 顯式 placement 優先：即使 buyer 原本在推導 Cc，也會移到 To，不必另寫 --exclude：
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --to buyer@example.com

# 從 reply-all 推導結果減人（--exclude 只作用於推導地址）：
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --exclude former-contact@example.com

# 完全使用人工核可清單，不採用 reply-all 推導（至少要一個 --to）：
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --replace-recipients --to approved@example.com --cc approved-cc@example.com

# 借既有 thread 開新話題：換掉主旨，In-Reply-To / References 仍接錨點信：
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --subject "索取文件（範例）" --replace-recipients --to info@example.com

# 已人工核對 envelope，允許 archive message_id 只作 thread anchor：
bes draft-reply <archive_rfc822_message_id> --body-from <...>/reply.txt \
    --allow-archive-anchor --replace-recipients \
    --to approved@example.com --cc approved-cc@example.com
```

bes 行為（這些都不需 caller 介入）：
- **預設 To / Cc**：對原信做 reply-all 去重，排除自己（避免寄給自己）
- **追加與顯式 placement**：repeatable `--to ADDR` / `--cc ADDR` / `--bcc ADDR` 加在推導結果上，跨 bucket 以不分大小寫的 email 去重。顯式 To/Cc 是權威 placement：同址會先從所有推導 bucket 移除，再放進顯式 bucket；若同址同時顯式列在 To 與 Cc，To 優先且只保留一份
- **排除推導地址**：repeatable `--exclude ADDR` 只從 reply-all 推導的 To/Cc 移除地址。它不會過濾顯式清單；若同一地址也出現在任何顯式 `--to` / `--cc` / `--bcc`，即使大小寫不同也屬參數矛盾，命令會在 env / DB I/O 前 exit 2 且不產 draft
- **完全替換**：`--replace-recipients` 忽略 reply-all 推導，To/Cc 及額外 Bcc 只取本次顯式值（既有自動 BCC-self 仍保留）
- **final To invariant**：所有模式 resolve 完的 To 都必須至少一個地址；若 To 為空（包含 replace 未給 `--to`，或 `--exclude` 移除唯一推導 To），exit 2 且不產 draft
- **矛盾旗標提早拒絕**：`--exclude` 只過濾 reply-all 推導，但 `--replace-recipients` 完全跳過推導，因此兩者禁止併用。handler 會在讀 DB 前 exit 2、不產 draft，stderr 固定為 `--exclude only filters reply-all-derived recipients; --replace-recipients skips derivation entirely. Remove --exclude, or drop --replace-recipients.`；caller 應移除 `--exclude`，或拿掉 `--replace-recipients`
- **排除與顯式收件人重疊也提早拒絕**：`--exclude` 與任何顯式 To/Cc/Bcc 地址會不分大小寫比對；只要有交集，handler 會在 env / DB I/O 前 exit 2、不產 draft，並以正規化地址的固定排序列出所有衝突。stderr 格式為 `--exclude conflicts with explicit recipient address(es): <address-1>, <address-2>. Remove each address from --exclude or from --to/--cc/--bcc.`。caller 必須依意圖把每個地址從 `--exclude` 或顯式 `--to` / `--cc` / `--bcc` 任一側移除，不得期待 exclusion 靜默過濾顯式清單
- **顯式地址 parser 邊界**：每個 `--to` / `--cc` / `--bcc` / `--exclude` 值都必須是單一、語法完整的地址，且 addr-spec 必須是 ASCII。未閉合 angle bracket 等 malformed raw syntax，或 `audit@例え.テスト` 這類非 ASCII addr-spec，會在 env / DB I/O 前 exit 2 且不產 draft；`"王小明" <audit@example.com>` 這類 Unicode display name + ASCII addr-spec 則受支援
- **archive anchor override**：archive-scrape anchor 預設仍 exit 2 且不產 draft；只有明示 `--allow-archive-anchor` 才放行並在 stderr 警告。此 flag 只解除 anchor provenance gate，不會自動修正收件人
- **Subject**：預設為原 subject 前加 `Re: `（已有不重複加）。給 `--subject <text>` 就原樣使用該主旨（**不**自動加 `Re: `），只換主旨、thread 照舊由錨點信決定；中文主旨會正確編碼。空字串／只有空白，或含換行／控制字元，一律 exit 2 且不產 draft。錨點信的主旨跟這封要談的事不相符時（例如借舊 thread 向同一窗口索取別的文件），就用 `--subject`，對方從主旨看得出來意，日後搜尋也對得上
- **In-Reply-To + References**：永遠由 `<rfc822_message_id>` 指到的原信接續 thread chain，與上述收件人選擇完全分開
- **From**：解析出的寄件設定檔中 `SMTP_USER` 指定的寄件地址
- **BCC**：自動加自己，再加入顯式 `--bcc`（記錄兜底；寄件匣可見性主要靠送出後 APPEND 進 `寄件備份`，見下方「Outlook 寄件匣可見性」）
- **`--attach <path>`（可重複）**：把本地檔夾進 draft。檔名保留原始（含中文），MIME 自動推斷。任一路徑不存在 / 是目錄 / 讀不到 → 明確報錯 + 非零 exit + 不產出 `.eml`（不會產生半成品）。用 bes 寄出的附件在送出入庫時會寫入附件庫並建立索引，日後可用 `bes attachments` 與 `bes search --attachment-name` 搜尋
- 產出 `.eml` 寫到資料庫所在目錄下的 `outbound\drafts\<draft_id>.eml`，**不送出**
- 成功時 stdout 印出 `draft_id:` / `From:` / `Subject:` / 最終 `To:` / `Cc:` / `Bcc:` / `Message-ID:` / `Attachments:`（每個附件的名稱與位元組數，無附件印 `none`）/ `fingerprint:`（草稿 `.eml` 全 bytes 的 SHA-256）。caller 必須拿 To/Cc/Bcc 三行與人工核可清單比對，不能只靠 anchor 原信推測收件人

### Step 3：呈審（必經步驟，不可省略）

**寄不寄由使用者在對話裡決定，這是 agent 的義務。** bes 只驗「寄出的＝使用者看過的」（指紋），驗不了「你有沒有問過」——所以跳過這一步不會被任何工具擋下，但就是違規。

**單封**：把以下內容一次呈給使用者，然後停下來等回覆：

- 草稿編號（`draft_id`）——一定要標，寄出時用的就是這一組
- 寄件帳號（Step 2 stdout 的 `From:`）
- 主旨
- 完整 `To` / `Cc` / `Bcc`（照 Step 2 stdout，不要摘要成「客戶等 3 人」）
- 你寫的內文（Step 1 的檔案內容）
- 附件名稱與大小（照 Step 2 stdout；無附件就說無）
- 指紋（Step 2 stdout 的 `fingerprint:`）

使用者明說寄出（「寄」「送出」「幫我寄」）才進 Step 4。使用者要改 → 刪掉舊 `.eml`、改內文檔、重跑 Step 2、**重新呈審新的草稿編號與指紋**。

**多封：一次呈審、一次授權**。不要每封問一次。呈一張總表，每封一列：草稿編號、From、主旨、完整 To/Cc/Bcc、附件。內文同一模板時，模板只呈一次，再列每封變動的欄位；內文各不相同時，每封附內文。使用者對整張表說寄，就整批寄。

**已知缺口（靠紀律補）**：bes 不會自動淘汰舊草稿——改稿時沒刪掉的舊 `.eml` 仍在待寄清單。指紋擋得住「編號與指紋配錯」和「呈審後草稿被改」，擋不住「舊編號＋舊指紋整組拿錯」。寄出時只用最近一次呈審那一組 `draft_id` + `fingerprint`。

### Step 4：帶指紋寄出

```bash
bes send-draft <draft_id> --fingerprint <sha256>
bes send-batch <id1>=<sha1> <id2>=<sha2> ...   # 全部帶指紋，或全部不帶
bes list-drafts [--json]                                     # 看 draft_id / 收件人 / 主旨 / 建立時間；停在寄送中的多帶 status / marker 路徑
```

**Claude Code 的 `!` 不是 TTY**。`! bes send-draft ...` 看起來像在終端機裡跑，實際沒有 TTY，不帶指紋一樣會被拒。agent 的 Bash / PowerShell tool、背景任務、pipe 也都不是 TTY。**在 Claude Code 裡寄信一律帶 `--fingerprint`**，不要叫使用者改用 `!` 跑。不帶指紋的互動授權只在使用者自己開的真正終端機（例如 Windows Terminal）有效。

`send-draft` 行為：
- **授權二擇一**：帶 `--fingerprint` → 重算當下草稿的指紋，相符才寄、不跳任何提示；不帶 → 必須是真 TTY，印 draft 摘要後要求輸入非空確認詞
- **指紋不符**：拒寄、不寫 send-once marker，exit 1。代表草稿在呈審後被改過，或編號與指紋配錯。**錯誤訊息刻意不印新指紋**——不要去算新指紋重寄，回到 Step 3 重新呈審
- **不帶指紋又非 TTY**：拒寄，訊息列出兩條出路（帶指紋／在真正的終端機跑）
- 帶指紋時仍會把 draft 摘要印到 stdout，留在對話紀錄裡當寄出證據
- SMTP 走解析出的寄件設定檔（SMTPS 465 / submission）；摘要含 `From:`
- 送出後 BCC 副本不上線顯示，但 envelope 含自己
- DB 新增一列 `pst_source='outbound-bes'` 紀錄
- SMTP 送出成功後，再把 `.eml` APPEND 進 IMAP `寄件備份`（帶 `\Seen`），讓 Outlook 寄件匣看得到這封信

`send-batch` 行為（用於一次核可、整批送，如一批相同內容的通知）：
- 吃**明確列舉的 draft_id 清單**（不是「送全部」——避免誤送殘留草稿）；清單有重複 id 直接拒
- **指紋全部帶或全部不帶**：`<draft_id>=<sha>` 與裸 `<draft_id>` 混用 → exit 2、一封都不寄
- 帶指紋：逐封驗，**某封不符只拒該封（`failed`）、其餘照寄**，不跳提示；不帶：印整批摘要（每封 draft_id / 完整 To·Cc·Bcc / 主旨 / 附件檔名），在真 TTY **單次**授權整批
- 逐封套用與 `send-draft` 相同的 gate；**部分失敗不中止**：某封失敗記錄後續續送，最後回報逐封 `sent`/`partial`/`skipped`/`failed`（`partial` = 已寄出但部分收件人被 SMTP 拒收，見失敗模式）
- 第一封立即嘗試，之後每個可送 draft 之間固定等待 **90 秒**，避免短時間累計約 20+ 收件人後回 `450 4.7.1 too many recipients` / `421 too many errors`；這個 pacing 是 core 行為，不要繞過
- 已送過的 draft（marker 為 sent/sending）自動 `skipped`、不當失敗；有任一 `failed` 才 exit 非零。`sending` 的 skip reason 帶 marker 路徑與處理步驟（見下方「草稿停在「寄送中」」）
- 全部不可送（皆 skip/prep 失敗/指紋不符）時不問 token，直接印報告；不帶指紋又非 TTY 整批拒送
- 每封成功後，DB 的 `outbound-bes` 列與 `sent` marker 都會先各自落盤才進下一封。若等待期間中斷，先跑 `bes list-drafts` 重新列出未送草稿；遇到 `sending` marker 必須先查 SMTP／DB 狀態，**不要盲目重送**（步驟見下方「草稿停在「寄送中」」）。已是 `sent` 的項目可安全略過，未開始的 draft 再明確列舉續跑

## 失敗模式 + 前提核對

- **草稿停在「寄送中」（`list-drafts` 顯示 `status: sending`；`--json` 有 `status` / `marker_path` / `next_step`）**：寄送失敗後 send-once marker 刻意留在 `sending`（信可能已寄出，所以不自動放行）；`send-draft` 會拒、`send-batch` 會 `skipped` 且 exit 0。處理步驟：①訊息與 `list-drafts` 都印出 marker 檔的實際路徑；②事實：這封可能已寄給部分或全部收件人（To、Cc、Bcc），也可能還有寄送在跑；③**只有確定沒有任何寄送還在跑、而且沒有任何一位收件人收到，才能重寄** → 刪掉那個 marker 檔再重寄（重寄前仍走 Step 3 呈審、帶指紋）；任一人已收到、不確定、或要放棄 → 保留 marker、不重寄整封。怎麼確定由使用者判斷（問對方、查寄件備份／伺服器記錄都只是參考，「對方還沒收到」不等於沒寄出）；拿不準就不重寄。唯一例外：訊息明說 `SMTP login was rejected ... the mail was not sent`（登入被拒，SMTP 從未收下任何東西），修好帳密後可直接刪 marker 重寄，不需查收件。沒有「解除標記」指令，刪檔就是解除

- **`SMTP settings file not found`**：三層解析出的檔案不存在（常見於換機器，或環境變數指錯），三個 outbound 命令都會在 DB 或網路 I/O 前 exit 2，訊息列出三條出路。回報 user 由他決定用哪條，不要用 `MAIL_SERVER_ENV_PATH` 代替
- **`From:` 不是預期的寄件帳號**：停在 Step 3，不要寄；回報 user，改用 `--env-file` 指向正確帳號的設定檔後重產草稿
- **SMTP 認證失敗 / `Could not load SMTP config`**：設定檔存在但內容不對；提醒 user 檢查是否含 `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD`；不要自己嘗試填內容
- **`interactive authorization required`**（不帶指紋、在非 TTY 環境跑 send）：使用者已在對話裡看過並說寄 → 補上 `draft-reply` 印的 `--fingerprint` 重跑；還沒呈審 → 先做 Step 3。不要叫使用者用 `!` 跑（`!` 不是 TTY），也不要 import `_prepare_draft_for_send` / `_submit_prepared_draft` 這類私有 helper 繞過
- **`draft fingerprint mismatch`**：草稿在呈審後變了，或編號與指紋配錯。`send-draft`：這封沒寄、marker 未寫。`send-batch`：只有不符的那封被拒（`failed`、marker 未寫），其餘指紋相符的**照常寄出** —— 重送前先看逐封報告，只對 `failed` 那封處理，不要整批重跑。回到 Step 3 對**當下**草稿重新呈審，拿新的 `draft-reply` 輸出，不要自己算新指紋
- **Outlook 寄件匣可見性 / append warning 不等於寄送失敗**：Outlook 對 sean3808@mailattic.test 已改走 IMAP，寄件匣就是 IMAP 的 `寄件備份`。bes 送出成功後會 best-effort APPEND 一份進去。**若 stderr 印出 append warning，信已經寄出去了** —— warning 只代表「Outlook 寄件匣可能看不到這封」，不代表沒寄。**絕對不要因為看到 append warning 就重寄**（會真的寄兩封）。要確認是否送達，查 DB 那列 `pst_source='outbound-bes'` 或 BCC-self 副本，不要看寄件匣有沒有出現。
- **append 會在 IMAP 帳號不是寄件帳號時主動跳過**：`IMAP_USER` 與 `SMTP_USER` 不一致時 bes 拒絕 APPEND 並 warning（防止設錯帳號把整封 outbound 副本歸檔進別人信箱）。此時信仍已寄出。
- **部分收件人被 SMTP 拒收（`Partially sent` / `partial`，#224）**：server 在 RCPT 階段拒了部分地址（例如 `450 4.7.1 too many recipients`），其餘已接受，信**已寄出**。`send-draft` 印 `Partially sent: message_id=...`、stderr 列每個被拒地址與 code/reason，**exit 3**；`send-batch` 該封 outcome 為 `partial`（stderr 同樣列出），整體 exit 1；MCP `mail_send` 回 `status="partial"` 並帶 `accepted` / `refused[{address,code,reason}]` / `warning`；Python API 回傳多 `delivery="partial"` 等同名欄位。marker 為 `sent` 並記下 `refused`，所以**不可整封重寄**（會對已接受者重複發信，且重寄會被 marker 擋）——只對 `refused` 地址另開新草稿。被拒的可能是客戶、也可能只有 BCC-self（此時 DB／收件匣兜底不存在，要回報 user）。DB 的收件人是「原定名單」，不是投遞保證。**DATA 已被接受、只有收尾 QUIT 失敗（#230）**：信照樣記為 `sent`，結果多帶 `close_warning`（CLI／batch 另在 stderr 印 `SMTP close failed after accept`，exit code 不變）；信已寄出，**不可重寄**。若 SMTP 把**全部**收件人都拒了，視同 `SMTP submission failed`（marker 停在 `sending`，需人工介入；訊息帶 marker 路徑與處理步驟，見上方「草稿停在「寄送中」」）。
- **SMTP 接受 ≠ 投遞成功**：`sent` 只代表 submission server 收下了，之後仍可能 bounce。
- **`draft-reply` 不接 LLM 寫內容**：任何「讓 bes 自己生內容」的需求都不存在；caller AI 必須自己把內文寫成檔案餵進 `--body-from`
- **收件人參數矛盾（replace）**：看到 `--exclude only filters reply-all-derived recipients; --replace-recipients skips derivation entirely. Remove --exclude, or drop --replace-recipients.` 代表命令在讀 DB 前已拒絕。依意圖移除 `--exclude`，或拿掉 `--replace-recipients` 回到 reply-all 推導模式
- **收件人參數矛盾（地址重疊）**：看到 `--exclude conflicts with explicit recipient address(es): <addresses>. Remove each address from --exclude or from --to/--cc/--bcc.` 代表列出的地址同時存在於 exclusion 與某個顯式 To/Cc/Bcc 清單（比對不分大小寫），命令已在 env / DB I/O 前 exit 2 且不產 draft。逐一從 `--exclude` 或顯式 placement 任一側移除衝突地址後重產；不要期待 exclusion 過濾顯式清單
- **收件人地址不合契約**：未閉合 angle bracket 等 malformed raw syntax，或任何非 ASCII addr-spec，都會在 env / DB I/O 前 exit 2 且不留 draft。Unicode 只可用於 display name，實際 addr-spec 仍必須是 ASCII
- **final To 為空**：任何 recipient mode 的最終 To 都不得為空；看到 `at least one To recipient is required` 時，補上顯式 `--to` 或調整 exclusion 後重產。該次 exit 2 且不會留下 draft
- **改 draft**：產出後**直接編輯 `.eml` 檔不安全**（bes 不重新計算 header）；改法是刪掉 draft 重產：`rm outbound/drafts/<draft_id>.eml` → 重跑 `bes draft-reply` 用新 body 檔
- **draft 已送的 idempotency**：`send-draft` 內部有 send-once 標記，重複呼叫已送 draft 會拒送並印訊息（停在 `sending` 的另帶 marker 路徑與處理步驟）

## 收尾

- Reply 用的 body 檔（`reply_<topic>.txt`）放 user 當前專案根，不寫回 mailattic repo
- `outbound/drafts/<draft_id>.eml` 由 platform 自己管理，caller 不要清
- 送出後 DB 內已有 `pst_source='outbound-bes'` 紀錄，可被 Capability B 找到

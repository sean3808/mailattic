# Project Vocabulary

> mailattic 唯一的詞彙正典，住 repo root 供 matt 系 skills（`/domain-modeling`、`/grilling`、`/grill-with-docs`、`/wait-what`）原生讀取（前身 `openspec/LANGUAGE.md`，[D-20260820-3](docs/decisions/D-20260820-3-migrate-to-contract-anchored-flowback.md)）。
>
> 寫契約條目、文件、issue／PR、測試名時用 canonical term。討論中出現 `Avoid` 同義詞 = vocabulary drift：在結論摘要點名，改回 canonical。
>
> 分兩個 scope，由 H2 表達：`domain`（標準郵件協定與外部概念）與 `mechanism`（本平台自造的查詢／儲存語意）。

---

## 標準郵件協定與外部概念（scope: domain）

> PST、IMAP、Maildir、thread、UIDVALIDITY 這類外部概念，首次需要釐清語意時入庫。

### 回復（reply-all）

- **Definition**：對既有信件產生一封續在同一 thread 的回信，caller 以 DB 內儲存的 RFC822 Message-ID（`messages.message_id`，不是數字 row id）選定錨定信，並帶 `In-Reply-To` + `References` 接續。收件人預設由原信 sender / To / Cc 推導（去重、排除 [[self-address]]），caller 可明示追加、排除推導地址或完全替換 envelope；最終 To 不得為空。**失效信箱在未明示排除時照填不過濾**，退信交由 SMTP 自然處理。
- **Avoid 同義詞**：回覆（單指回寄件人時）、reply、reply-to-sender、回信
- **Why**：口語的「回復」在 Outlook 對應兩個按鈕；本平台預設「全部回覆」，caller 持有人工核可清單時可明示調整 envelope，thread continuity 仍由錨定信決定。Owner: Sean（2026-08-11 確認 #84 / #85 與 PR #102 review 邊界）；SoT: `bes draft-reply` 收件人組裝；source: `openspec/changes/archive/2026-08-12-control-draft-reply-recipients-and-archive-anchors/ddd-note.md` §2–§4。

---

## 本平台自造機制（scope: mechanism）

### launch-isolation preflight

- **Definition**：probe supervisor 在建立 client process 前，針對自己即將使用的 config、argv、child environment、fixture identity、cwd 與 record destinations 所做的 fail-closed 靜態驗證。通過只代表 supervisor-owned launch inputs 已受隔離；啟動後仍由 canary 作第二層的 probe 可用性與 fixture provenance 確認。
- **Avoid 同義詞**：tool-surface attestation、inventory gate、pre-turn inventory check、client compliance proof、完全無工具
- **Why**：舊契約把安全證據綁在 client 必須先輸出的 session inventory，但受支援 client 要先收到 turn 才輸出 init，且該 init 不枚舉可用 probe tools，造成 ordering deadlock 與證據誤稱。此 term 把責任放回能在 spawn 前決定實際 launch inputs 的 supervisor，同時保留 canary 作動態第二層，不把 flags 的請求誇稱成 client runtime attestation。Owner: Sean（#97）；SoT: `scripts/probe_supervisor.py` 的 launch-plan boundary；source: `openspec/changes/archive/2026-08-09-enforce-mcp-probe-launch-isolation/design.md`。

### self-address

- **Definition**：代表信箱擁有者本人的 email 地址集合，由**操作者宣告**並存在 DB 的 `self_addresses` 表，以 `bes identity add / remove / list` 維護。集合為空（或表尚未建立）就是「未宣告」：程式碼沒有內建地址可退（`#268`），未宣告時沒有任何信會被判成自己寄的。
- **Avoid 同義詞**：owner-email、my-address、self-email、own-account
- **Why**：查詢層以「sender 是否屬 self-address」判定 [[direction]]（contact-history 起，commitments / 未結案 thread 將複用），需要一個跨消費端（CLI / MCP / Python API）共用的單一定義。2026-08-20 由「程式碼凍結單一條目」升級為「使用者可定義」：使用者可能同時有多個地址，開源使用者也不該為了宣告自己而改程式碼。身分存在 DB 裡，MCP 端讀同一顆 DB 即零設定繼承。三種被拒的判定方式見 Rejected framings。Owner: Sean（#166 grilling 拍板）；SoT: `self_addresses` 表，解析入口 `src/mailattic/identity.py > resolve_self_addresses`；source: #166 Implementation Decisions、#168。

### direction（inbound / outbound）

- **Definition**：一封信相對 [[self-address]] 的方向：sender ∈ self-address → `outbound`（本人寄出，含 self→self 自寄），否則 → `inbound`；sender 缺失時為 null，不硬猜（`null` 只在子票 B 的欄位輸出上成立；`contact-history` 目前只吐 outbound / inbound 兩個計數）。收發比例 = outbound 數 / inbound 數。判定**即時計算**、不落欄位、不 backfill —— 改 self-address 集合即全庫生效。
- **Avoid 同義詞**：sent/received、in/out、方向性、收發向
- **Why**：與 [[self-address]] 強綁，是收發比例與往來分析的語意根，跨 contact-history / commitments / 未結案 thread 共用；以 sender 比對是因為它單一且穩定（folder 判定為何被拒見 Rejected framings）。**現行實作是精確字串比對**（`LOWER(sender_email) IN (...)`），庫內有 sender 存成 `sean3808<sean3808@mailattic.test>` 的髒值會漏判 ——「比對前先抽出 email 部分」是 #44 子票 B 的範圍、尚未落地，引用時標明是待辦。Owner: Sean；SoT: 同 [[self-address]]，比對點在 `src/mailattic/contact_history.py > _run_aggregation`；source: `openspec/changes/archive/2026-05-30-add-contact-history/design.md`「direction 由 sender 是否屬 self-address 判定」Decision、#166。

### thread-bridge

- **Definition**：一種 inferred、low-confidence 的 outbound thread-membership edge，把 header-less outbound copy（[[self-address]] sender，且沒有 `in_reply_to` / `references_header`）連回目標 thread；存放在獨立欄位，真實 header fields 保持原樣。它參與 `thread_key` connected-component 計算，但以 `source='subject_heuristic'` 標記，與 header-based reference edge 區隔。
- **Avoid 同義詞**：subject-merge、fuzzy-thread-link、soft-edge、inferred-reply
- **Why**：大量 header-less outbound replies（MAPI Sent Items）要接回 conversation，ball-in-court 判斷才看得到完整 thread，同時 forensic header fields（`raw_headers` posture）不能被污染。獨立 edge 讓 inferred membership 可稽核、可逆、可由 backfill 重建，對齊 Thunderbird gloda Bugzilla #668491 的分層原則：index layer 信任 headers，subject inference 放在分離且有標記的 layer。Owner: Sean；SoT: `scripts/bridge_orphan_outbound.py` + `messages.thread_bridge_target/thread_bridge_source`；source: `openspec/changes/archive/2026-06-30-bridge-orphan-outbound-threads/design.md` Decisions 1–4。

### rebuild

- **Definition**：從原始 PST（+ mail server 最近窗口的 IMAP backfill）**重新 ingest** 出本平台 DB 的災難復原動作。屬**深層 fallback**，完整度受來源新鮮度限制（PST 涵蓋上界 + server 的保留窗口；兩者之間若都沒有則不可復原）。與 `restore`（從外部備份還原一份完整 DB 備份，為主 DR）**語意不同且不可互換**。
- **Avoid 同義詞**：restore、recover、還原、復原（這些指「還原既有 DB 備份」）
- **Why**：DR 討論中混淆兩者是真實風險 ——「電腦壞掉那天」的首選是 restore（完整、近期），rebuild 是 restore 與所有備份皆失時的退路（有結構性缺口）。兩者並存正當，但必須明確區分，才不會在事故當下選錯路徑、誤以為復原完整。Owner: Sean（主張 PST 與 DB 同時備份以消缺口）；SoT: `docs/rebuild-db-from-pst.md`（runbook）+ `bes rebuild`；source: `openspec/changes/archive/2026-06-11-add-disaster-recovery-rebuild/design.md` Context 與 D1–D7。

### rebuild 來源筆數基準

- **Definition**：對指定 PST 來源群，以已核可且可比較的口徑設定的最低入庫筆數與明示差額。符合基準只代表來源數量達標，不代表舊郵件逐封保留；缺少可信基準時，結構健康仍可成立，但來源筆數對帳不能宣告通過。
- **Avoid 同義詞**：郵件完整性證明、逐封保全、PST 原始訊息總數、抽取帳
- **Why**：同一郵件跨 PST 重複、抽取方式與來源歸屬會影響入庫筆數，歷史初次匯入數、舊 DB 分布與本次抽取數不能直接互換。Owner: Sean（2026-10-03 `#228` grilling，Q1=A、Q2=A）；SoT: 來源基準討論（本機工作檔，未入版控）中的逐題裁決；整體實作 brief 尚待確認。

### 匯入路徑（import path）

- **Definition**：把外部郵件來源寫進 DB 的三條路，彼此不是同一個維度。**兩條吃 PST**，由 `import_pst_sources` 的 `backend` 參數二選一：`"outlook"`（Outlook COM，收件人完整，`bes rebuild` 與 `bes import` 的正式路徑）與 `"readpst"`（libpst，**收件人已知損壞**，無 production caller，只用於「PST 在手但 Outlook 不可用」的救急）。**第三條吃 maildir**，是獨立函式 `import_maildir_sources`（`bes import --maildir`），不需 Outlook 也不需 readpst，就是每日 IMAP sync 天天在走的那條路。
- **Avoid 同義詞**：「三條路線」（把 Windows readpst 與 WSL readpst 誤算成兩條）、backend（單獨使用時語意不足，`backend` 只涵蓋兩條吃 PST 的路，不含 maildir）
- **Why**：Windows readpst 與 WSL readpst **是同一段程式碼**（`resolve_readpst_path` 先找 `readpst.exe` 再找 `readpst`）解析到不同 binary，是環境差異不是分支 —— 數成兩條會得出「有三個 backend」的錯誤結論。maildir 那條又常被漏數，導致「不裝 Outlook 就只剩 readpst」的錯誤推論，進而拿一條收件人已知損壞的路當備援（`#175` 花一整輪重建修的正是那種損壞）。Owner: Sean（2026-09-10 grilling 拍板 `#184` 時釐清）；SoT: `src/mailattic/ingest.py` 的 `import_pst_sources` / `import_maildir_sources`；source: `#184`、`#154`、[D-20260903-1](docs/decisions/D-20260903-1-recipient-rebuild-extraction-on-windows-outlook.md)。

### outbound-send

- **Definition**：透過 mailattic.test SMTP submission（465/SMTPS）提交一封 RFC5322 message 的動作，**僅指投遞本身**。送出後的 local DB insert（`pst_source='outbound-bes'`）、APPEND-to-Sent、[[BCC-self]] 都是獨立的後續副作用，不含在此 verb 內。
- **Avoid 同義詞**：send（未限定時歧義：Outlook 的 send 按鈕、`bes sync-imap` 的接收端都會被口語稱作 send）、寄出、投遞、deliver
- **Why**：Outlook send 與 bes send **必須共存**（Outlook 是人類主路徑，bes 是 agent 補位），都叫「send」會讓事故追查分不出「這封信是誰寄的、走哪條路徑」。副作用切出去，是因為它們各自可以失敗而不影響投遞已成立 —— APPEND 失敗只是看不見，信已經出去了。Owner: Sean；SoT: `src/mailattic/outbound.py`；source: 2026-05-28 寄件能力設計筆記 §2（本機工作檔，不隨公開版帶出）。

### 對話層授權

- **Definition**：agent 把草稿（主旨、完整 To/Cc/Bcc、內文、附件、草稿編號、指紋）呈給使用者、使用者在對話中明說寄出，agent 才帶**草稿指紋**（草稿 `.eml` 全部 bytes 的 SHA-256）寄出。多封一次呈審、一次授權（總表每封一列）。工具層只驗「寄出的＝看過的」：指紋與當下草稿不符即拒寄。CoWork（MCP）形態同樣是「看過才寄、寄的就是看過的」，但一次授權只寄一封（沒有多封一次授權），且只寄得出 CoWork 自己擬的草稿：CLI 擬的草稿不能經 CoWork 寄出。
- **Avoid 同義詞**：指紋授權（指紋不是授權，授權是使用者那句話）、auto-send、`--yes`、確認 token
- **Why**：Claude Code 的 agent shell 工具與 `!` 都不是 TTY，只收 [[明文授權]] 的結果是在 Claude Code 內完全寄不出信。「有沒有問過使用者」工具層測不到，所以寫成 agent 的義務（skill 必經步驟）而不是契約條文；指紋擋的是呈審後草稿被換掉或編號配錯，擋不住 agent 自己不問就寄，也擋不住「舊編號＋舊指紋整組拿錯」。Owner: Sean（2026-09-14 `#195` 拍板）；SoT: `send_draft(fingerprint=)` / `send_batch(fingerprints=)` 與 AGENTS.md 行為契約 #2；source: [D-20260914-1](docs/decisions/D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md)。

### 明文授權

- **Definition**：人類在**真正的**互動式終端（stdin 與 stdout 皆為 TTY）輸入**非空的授權詞**才放行對外投遞，是不帶指紋時唯一的放行形態。敲 Enter 取預設值不算；不帶指紋又非 TTY 一律拒送。門禁住在核心執行函式內，不是 CLI flag。
- **Avoid 同義詞**：explicit confirm、確認、二次確認、`--yes`、auto-confirm
- **Why**：叫「明文」而非「確認」，是要排除「按 Enter 就過」這種形式上的確認。原本是 outbound 唯一的授權形態；2026-09-14 收窄為 TTY 形態，與 [[對話層授權]] 並存。Owner: Sean（2026-05-28 指定，取代原先的「explicit confirm」；2026-09-14 收窄）；SoT: `bes send-draft` / `bes send-batch` 的 TTY gate（AGENTS.md 行為契約 #2）；source: 2026-05-28 寄件能力設計筆記 §2（本機工作檔，不隨公開版帶出）、[D-20260914-1](docs/decisions/D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md)。

### BCC-self

- **Definition**：[[outbound-send]] 時把 [[self-address]] 放進 SMTP envelope（不上 header 可見列）的收件人集合，讓自己收到一份副本作為**記錄兜底**。目前是次要路徑：寄件可見性的主路徑是送出後 APPEND `.eml` 進 IMAP `寄件備份`。
- **Avoid 同義詞**：self-copy、密件副本、自寄一份、BCC loop
- **Why**：bes 寄出的信不經過 Outlook，不做回灌的話 Outlook 寄件匣與本 DB 都沒有這封信的痕跡。APPEND 是 best-effort（失敗只印 warning、不改 exit code），所以保留一條與 IMAP 資料夾狀態無關的兜底。副本回流時走 message-id dedup gate，不會重複 ingest。Owner: Sean；SoT: `src/mailattic/outbound.py`；source: [D-20260528-2](docs/decisions/D-20260528-2-outbound-visibility-via-bcc-self.md)、[D-20260714-2](docs/decisions/D-20260714-2-chinese-sent-folder-of-record.md)。

### pre-task sync

- **Definition**：caller 在開始一項任務前主動觸發的 IMAP 增量同步（`bes sync-imap --pre-task`），帶 15 分鐘最小間隔閘門：距上次同步不足 15 分鐘直接 skip。
- **Avoid 同義詞**：手動 sync、強制同步、refresh、force-sync
- **Why**：agent 查信前需要「夠新」的資料，但連續任務會把它變成 burst sync（實測單次 2m43s–22m56s，且與新信數不成正比）。閘門讓 caller 每次都叫也無妨，不必自己記上次同步時間 —— 語意是「確保夠新」而非「強制拉一次」，所以 skip 不是失敗；但上一次同步失敗或未完成時，skip 輸出會標出來（`last sync failed` 等），此時不能當成資料夠新。Owner: Sean；SoT: `bes sync-imap --pre-task`；source: 2026-05-28 寄件能力設計筆記 §2（本機工作檔，不隨公開版帶出）。

### runtime root（`.project-runtime`）

- **Definition**：repo root 底下**單一**的 ignored 目錄，收納工具產生的一次性產物，每類各佔一個具名 leaf（pytest basetemp、Python temp、uv / XDG / pre-commit cache、marker-ledger collection、cross-process lock）。收錄判準是「**整包刪掉會怎樣**」：刪掉只讓下次重建、不損失任何持久狀態的才屬於這裡。三類明確不在其中 —— 業務執行資料（`.mailattic-work/`）、手動 WSL 暫存（`work-tmp/`）、路徑被全域派工協定寫死的 `.subagent-input/` 與 `.subagent-output/`。
- **Avoid 同義詞**：測試暫存區、系統碟隔離目錄、test artifact root、cache 目錄
- **Why**：**第一意圖是 repo 根目錄的可讀性** —— 一次性資料夾與 cache 收進單一 root，才能一眼分辨產物與專案本體，也讓「整包刪掉」成為安全動作。系統碟隔離是 2026-07-29 附掛上去的**第二**意圖，已於 2026-08-21 隨換機退役（[D-20260821-1](docs/decisions/D-20260821-1-retire-system-drive-hardware-guard.md)），退的只有第二意圖。第一意圖在 2026-08-21 之前沒進過任何記錄（proposal 只寫系統碟飽和，D-20260821-1 甚至寫成「唯一被記錄下來的理由」），照記錄讀必然得出「這個 root 已無存在理由」—— 2026-08-21 當天真的有人這樣建議過。本詞條就是為了擋這個誤讀。Owner: Sean（2026-08-21 grilling 拍板）；SoT: `scripts/project-tool-environment.ps1` 的 managed leaves 清單；source: 2026-08-21 grilling 對話、[D-20260821-1](docs/decisions/D-20260821-1-retire-system-drive-hardware-guard.md)。

### 隔離區（quarantine）

- **Definition**：與同步暫存區分開的目錄，只收一種東西：帳上仍有任何「附件寫入失敗、且尚未補回也尚未被人放棄」紀錄（`bes db doctor` 列為未解決的那一組）的信的原始檔；只要還有任何一筆未解決就留著。信的附件補回，或那筆失敗被人工放棄，且該信沒有其他未解決的附件失敗時，原始檔即移除；其餘暫存原始信照舊七天清掉。
- **Avoid 同義詞**：raw archive、原始信封存、備份區、垃圾桶、待處理匣
- **Why**：七天暫存是設計好的（D-20260703-1），但附件寫入失敗的信，原始檔是本機唯一補得回附件的來源，七天一到就等於替失敗蓋棺。隔離區是這條規則的唯一例外，收容條件窄到不會養出「這裡是備份」的依賴 —— 它只增不減的唯一出口是補回或人工放棄，系統不自動放棄、不設逾期；失敗被別條路徑解決的信不會被自動清掉，仍算在 doctor 的 `quarantined_mail` 數裡。它放在業務執行資料目錄而非 `.project-runtime`，因為後者的判準是「整包刪掉不損失持久狀態」，隔離區正好相反。Owner: Sean（2026-10-09 於 #239 拍板）；SoT: `src/mailattic/quarantine.py`；source: #239、#256。

---

## 維護原則

- 新增 term 須含 `Definition` / `Avoid 同義詞` / `Why` 三欄，缺一不算 canonical；`Why` 帶 Owner / SoT / source。
- `Avoid` 清單沒列到的詞不代表可用；用前想想是否該補進 `Avoid`。
- 既有 spec / 程式碼仍用 `Avoid` 詞彙時，下次修改該檔順手改回 canonical（gradual migration）。
- Term 放進所屬 scope 的 H2。第一個同時屬於兩個 scope 的 term 出現時，再開第三個 H2「跨 scope 概念」，並在其 Why 說明跨哪兩個 scope。
- 本檔由 `/domain-modeling` 與日常討論結論主動更新；發現 drift 時在結論段註明「Capture to: GLOSSARY.md」。
- **被拒的命名 / 設計只收在下方 Rejected framings**（防止舊設計復辟）；term 的 Why 只放一句指標過去，不重述拒絕理由。

---

## Rejected framings（歷史拒絕設計，禁止復辟）

- **自動從 sender 頻次推斷「本人是誰」** — `add-contact-history 2026-05-29` 拒絕，#166 重申。理由：會把高頻外部對方誤判成本人；本人地址由操作者宣告。邊界：[[self-address]]、[[direction]]。
- **用網域判定「我方」** — #166 拒絕。理由：同網域下有多人與角色信箱地址，網域判定語意直接壞掉。邊界：[[self-address]]。
- **用設定檔 / env var 宣告本人地址** — #166 拒絕。理由：身分要跟著信箱 DB 走，MCP 端讀同一顆 DB 才能零設定繼承。邊界：[[self-address]]。
- **用 folder_path（Sent/Inbox）判定收發方向** — `add-contact-history 2026-05-29` 拒絕。理由：跨 PST/IMAP 來源 folder 命名不一致、不可靠。邊界：[[direction]]。
- **不綁草稿內容的非互動送信旗標**（`--yes` / `--auto-confirm` / `--batch` 自動確認 / 敲 Enter 取預設） — `add-outbound-send-capability 2026-05-28` 拒絕，2026-09-14 收窄（[D-20260914-1](docs/decisions/D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md)）。理由：這類旗標連「使用者看完之後草稿被換掉」都擋不住。原本「任何非互動旁路」一律拒絕，實際效果是 Claude Code 內完全寄不出信；帶草稿指紋的寄出現在是正式入口。邊界：[[對話層授權]]、[[明文授權]]。
- **bes 內接 LLM 自行草擬內文** — `add-outbound-send-capability 2026-05-28` 拒絕（[D-20260528-1](docs/decisions/D-20260528-1-bes-never-generates-content.md)）。理由：規則明確的走 script、語意判斷留給 caller AI；讓 bes 生內容會模糊它「查詢後端 + 對外管道」的單一責任。邊界：[[outbound-send]]。
- **reply-to-sender-only 當作「回復」的預設** — `add-outbound-send-capability 2026-05-28` 拒絕。理由：會把原本 10 人的對話悄悄分岔成兩條。邊界：[[回復（reply-all）]]。
- **heuristically backfill `in_reply_to`** — `bridge-orphan-outbound-threads` 拒絕。理由：會把 inference 偽裝成 parsed header fact。邊界：[[thread-bridge]]。
- **另設第二個 root 收「agent 產出的一次性文件」**（與 `.project-runtime` 並列） — 2026-08-21 grilling 拒絕。理由：查證後**沒有住戶**。`.subagent-input/` 與 `.subagent-output/` 的路徑被全域派工協定寫死（`orchestrator-discipline.md` 的 Tier 3 與 `report_write` 模式），只在本專案改位置會讓 subagent 靜默走原路；`Issue/` 也不是一次性產物，而是 PRD、fixture 腳本混放的未分類雜物間。建一個沒有住戶的 root 等於把「沒分類」改名叫「有制度」；`.subagent-*` 真正的問題是保留期限不是位置。邊界：[[runtime root（`.project-runtime`）]]。

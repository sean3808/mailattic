# Capability B — Find（找信 + 抽價 + 未回球）

> 此檔由 `SKILL.md` 的 Capability 路由表 MANDATORY load 進來。
> 若任務只跑 Find（不續接 Reply），**不要載入** `capability-c-reply.md`。
> Stage 0 若觸發 sync，會載入 `capability-a-sync.md`；其他情況不要載入。

> **`{SKILL_DIR}` 約定**：下方所有 `{SKILL_DIR}/scripts/...` 的 `{SKILL_DIR}` = 本 skill 載入時 harness 顯示的「Base directory for this skill」值（本機為 `C:\Users\example\.claude\skills\mailattic`，換機器自動不同）。執行前以該實際路徑取代，**不要**寫死 username 絕對路徑。

## 何時用

User 給「模糊記憶 + 具體業務目標」就是這個 capability：
- 「以前那封 X 客戶的信」「我記得 Y 大概去年談過 Z」「找一下關於 W 的歷史信件」
- 「我對 X 客戶報過 Y 嗎」「歷史報價」「上次怎麼回覆」
- 「今天有沒有收到 X」（先觸發 Capability A `--pre-task` 拉新鮮）

## 心智模型

User 不會給你乾淨的 query，他會給「模糊記憶」+「具體業務目標」。你的工作有兩層：
1. 把模糊記憶**逐步收斂成具體的信件 db_id**
2. 若 user 要找的是「歷史報價/承諾」這類**結構化資訊**，從原信中抽出來並回報（含對方未回 / ball-in-court 狀態）

走五階段：先粗篩 → 用戶聚焦 → 細查 → 客戶切分 + 抽價 → 結構化原信報告。不要一次想到位；每階段拿到結果先給 user 看。

**但五階段是給「模糊記憶」設計的重機制。先過下面的 fast-path gate** —— 強錨點查詢一兩條指令就收斂，不要硬展開五階段。

---

## Fast-path：強錨點直球（先判，命中就跳過五階段）

進五階段前先問一句：**user 有沒有給「具體錨點代號」+「對方 domain」？**（產品代號 / 料號 / 訂單號 + 供應商或客戶 domain）

有 → 直接走兩步收斂，**跳過 Stage 1-3.5**：

```bash
# 1. 強錨點一槍命中（domain × keyword 收斂到極少數 thread）
bes search --participants-domain supplier.example --keyword "PRODUCT-A" --limit 50 --json

# 2. 命中收斂到單一 thread → 直接讀全文手抽
bes thread <db_id> --format text
```

- 命中收斂到**單一 thread** 時，**讀 thread 全文手抽價格**即可 → **跳過 Stage 3.5 的客戶切分**
- 命中仍跨多客戶 / 多 thread（> 5 封且多 domain）→ 退回五階段 Stage 3.5 做客戶切分
- **fast-path `bes search` 回 0 命中 → 不可逕判「查無此信」**：fast-path 跳過了 Stage 0 與 0 命中驗證，但目標若是**新進信**或**使用者寄件**，DB 可能還沒同步到。0 命中時**落回下方失敗模式的「0 命中 sync 驗證」**——時效/新信補 `bes sync-imap --pre-task`（寄件由它同步 `寄件備份`），exit 0 且上一次成功後重搜，仍 0 才是驗證過的真陰性；**若上一次同步失敗，0 命中永遠不是真陰性**（見下方失敗模式）。不要在 fast-path 直接下「查無」結論。

沒有具體錨點（只有「以前那封 X 的信」「大概去年談過 Z」這種模糊記憶）→ 才展開下面五階段。

---

## Stage 0：Sync prelude（時效性敏感才跑）

若 user query 含「今天 / 昨天 / 24h / 最近 / 剛剛 / 最新」之一 → 先載入 `capability-a-sync.md` 跑：

```bash
bes sync-imap --pre-task
```

確認 exit=0 後再進 Stage 1。throttle 內 skip 也算 OK（代表近期已 sync）。Sync 失敗（exit != 0）就停下回報 user，不要硬搜 stale DB 給錯結論。

**找的是 outbound（使用者寄出的信）**：`sync-imap --pre-task` 已同步 IMAP `寄件備份`（2026-07-14 起），不必另跑別的軌道。

歷史檢索（時效不敏感）跳過 Stage 0 直接 Stage 1。

## Stage 0.5：Browse escape hatch（無 MUST 詞時用）

User 只要「列最新 20 封」「列今天的信」「列上週某 domain 的信」這類**無模糊記憶要具象化**的需求，**不要硬走五階段**，直接：

```bash
bes search --from-date 2026-05-29 --to-date 2026-05-31 --limit 20 --json
bes search --participants-domain client.com --from-date 2026-05-01 --limit 50 --json
```

把結果列給 user 看即可結案。五階段是給「模糊記憶 → 具象 db_id」設計的，無模糊記憶就不需要。

## Aggregate mode：contact-history（歷年往來聚合摘要，非逐封）

User 要的不是「某封信」而是「跟某人 / 某公司歷年往來的全貌」（接案 / 回信 / 議約前先攤開避免報價不一致、重複承諾）→ 用聚合摘要子命令，**不要**走五階段逐封撈：

```bash
bes contact-history --contact alice@client.example --format json
bes contact-history --domain client.com --date-from 2024-01-01 --date-to 2025-12-31 --format json
```

- `--contact <email>` 與 `--domain <domain>` **互斥擇一必填**；日期用 `--date-from` / `--date-to`（YYYY-MM-DD，注意與 `bes search` 的 `--from-date` 命名不同）
- 回單一摘要物件：時間跨度（首次 / 最後往來）、總封數、收發比例、該網域所有聯絡窗口（email + display_name + 各自頻次 / 最後往來）、附件概覽
- `--format` 預設 `table`（TOON 緊湊格式），給 agent 消費用 `--format json`；`--attachments-limit N`（預設 20）控附件概覽列數
- direction（收 / 發）由 self-address 判定；contacts 排除 bcc 與 self
- 封數欄位：`total_messages = inbound_count + outbound_count + unknown_count`。`unknown_count` 是寄件人缺漏或空白、方向無法判定的信，不計入 `inbound_count`，也不產生寄件聯絡窗口；`mail_contact_history`（MCP）輸出同樣含此欄
- 何時用：user 問「我跟 X 往來多久了 / 都誰在跟我聯絡 / 收發比例」這種**關係全貌**問題 → contact-history。要找「具體某封信 / 某報價」→ 回五階段 Find。常見 chain：先 contact-history 攤全貌 → 鎖定時間段 / 窗口 → 五階段 drill 到具體 db_id

---

## 五階段流程（模糊記憶具象化）

從 user 的口語抽三類詞，然後一階段一階段收斂，每階段結果先給 user 看再決定下一步。

### Stage 1：捕捉意圖 → 拆關鍵字

- **MUST 詞**：必出現（客戶名、人名、訂單號、產品代號 — 通常 1-2 個）
- **CONTEXT 詞**：主題類詞群（交期 / 規格 / 價格 / 報價 / 單價 / quote — 通常 3-5 個 OR）
- **時間範圍**：user 講「2025 年初」「四五月」「去年」就先記下，第一輪查不到再 narrow

不要強制 clarification gate。keyword 夠具體（產品代號 + 客戶名 / 人名 + 主題詞）直接跑；只有真的不夠（單一通用詞「報價」、單一客戶名沒帶主題）才回頭問 user。寧可先跑粗篩、把命中分布給 user 看再決定要不要 narrow。

### Stage 2：FTS 粗篩

```bash
bes search \
  --keyword "<MUST詞>" \
  --from-date 2025-04-01 --to-date 2025-07-01 \
  --limit 50 --json > .tmp_search.json
```

flag 名是 `--from-date` / `--to-date`（**不是** `--date-from/--date-to`）。其他常用：`--sender` / `--recipient` / `--sender-domain` / `--recipient-domain` / `--participants-domain` / `--attachment-name` / `--limit`。需要顯式指定別的 DB 用 `--database <path>` 蓋掉 env var。

**收發方向**：`bes search --json` 與 `bes thread --format json` 每筆都含 `direction` 欄位（值 `outbound` / `inbound` / `null`）—— 判定 = sender 是否屬 `bes identity` 宣告的自家地址集合，`null` 代表 sender 缺失（不猜）。**不要自己拿 sender domain 比對來判斷收發方向**——同事、同網域他人來信會被誤判成自家；直接讀這個欄位。MCP `mail_search` / `mail_thread` 的回應形狀相同。目前沒有 `--direction inbound|outbound` 這種 server 端 filter，要篩就在拿到 JSON 後自己過濾。

**日期界的意思**：給一個純日期（`2026-07-01`）＝ **Asia/Taipei 該日整天、頭尾都含**，上下界都是。`bes search` / `bes attachments` 另外收完整 timestamp：帶 offset（`2026-07-01T12:00:00+08:00`）選中那個瞬間、不會被拓寬成整天；**不帶 offset 的 timestamp 讀作台北時間**。`bes contact-history` 的 `--date-from` / `--date-to` 只收日期（`2026-7-1` 這種非補零形式也收），不收 timestamp。日期格式錯誤是報錯（帶出錯值與參數名），不會靜默變成零筆。規則正典：`docs/decisions/D-20260723-1-taipei-day-date-filter-semantics.md`。

`--keyword` 支援受控 FTS5 布林語法：大寫 `AND` / `OR` / `NOT`、括號、雙引號 phrase。Operator 大小寫敏感，小寫 `and` / `or` / `not` 是 literal term。含 hyphen 的料號請用 phrase quote（例：`'"PEN-100"'`）；目前不支援 `*` prefix、column filter、`NEAR`。

**帶 `--keyword` 時的排序與命中證據**（MCP `mail_search` 形狀相同）：

- **排序是相關性（bm25），不是時間新到舊。** 無 `--keyword` 時才回到時間序。這件事會改變 `--limit` 的意義：長 reply chain 裡每封都含同樣關鍵字，時間序下**最早那封（通常就是原始報價 / 原始出處）永遠被 limit 切掉**；相關性排序靠 bm25 的長度正規化把短的原文推上來。要「最新的信」就別給 keyword，或給了 keyword 後自己按 `sent_at` 重排。
- **`match_in`** ∈ `new` / `quoted` / `null` —— **snippet 顯示的那個命中**落在本封新內容還是引用歷史。找原始出處直接篩 `new`。它講的是「你看到的那段證據在哪」，不是「所有查詢詞分別出現在哪」——後者會把「新內容寫了完整報價、引用區順帶提過產品名」的原始報價信誤標成兩邊都有。`null` 代表命中不在 body（見下一條）。
- **`match_field`** ∈ `body` / `subject` / `null` —— `null` 表示字面詞在主旨與內文都找不到：這封是靠**收件人或附件檔名**命中的（`message_fts` 也索引那兩者）。看到 `null` 不要以為是 bug，那是「命中不在你看得到的文字裡」的誠實回報。字面比對是**整 token** 比對（跟索引一致），所以查 `art` 不會被 `partial` 裡的 `art` 算成 body 命中；CJK 詞照 unigram 索引語意，子字串命中就算命中。
- **`snippet` 會以命中處為中心**（±120 字元，上限 300），查詢含多個詞時挑**共現最多詞**的那一段，不是最早出現的那個詞。無 keyword 時仍取內文開頭（上限 160）。所以 `bes search --keyword` 的 snippet 現在可以直接當判斷依據，不必為了看關鍵字上下文再去 `bes show`。

**三個 domain 旗標可多值（repeatable，多值之間 OR）**：要一次查跨多個網域（例：某客戶集團好幾個 domain）時，重複該旗標即可，命中為各網域聯集：

```bash
# sender 屬 a.com 或 b.com 的信
bes search --sender-domain a.com --sender-domain b.com --limit 50 --json
```

`--recipient-domain` / `--participants-domain` 同理可重複。Python API 對應收 `str | list[str]`（`SearchFilters(sender_domain=["a.com","b.com"])`）。**單值用法 100% 向後相容**——傳一次旗標或傳單一 `str` 行為與舊版完全相同。同一旗標多值之間 OR、不同旗標之間維持 AND。

讀 `.tmp_search.json` 看 hits。若全是主題重複的訂單 thread → 直接跳 Stage 3，不要在 bes 層繼續調 keyword（會更糟）。

#### 子程序呼叫 `bes search`：正確 vs 錯誤對照

當 caller 是另一個 Python 腳本、需要以子程序方式呼叫 `bes search` 時，有一個常見的致命錯誤。

**錯誤呼叫（禁止）**

```python
import subprocess

# 把 bes 綁進 mailattic dev .venv — dev venv 一壞下游就 crash
subprocess.run(["uv", "run", "bes", "search", "--keyword", "..."], cwd="<mailattic repo>")
```

這是 `uv-run-with-cwd` anti-pattern：`uv run` 在指定 cwd 下會自動尋找最近的 `pyproject.toml`，把執行環境綁進 mailattic 的開發 `.venv`。開發 venv 一旦損壞（例如 Windows/WSL 混用造成 symlink 斷裂），下游查詢立即 exit 2 crash。完整四條規則見 SKILL.md 致命陷阱第 9 條。

**正確呼叫（portable，fail-loud）**

```python
import os
import subprocess
from pathlib import Path

# 1. 取得真實 bin dir（可被 UV_TOOL_BIN_DIR / XDG 覆寫，故不可硬寫路徑）
#    check_output 在 uv 缺席時拋 FileNotFoundError、非零退出時拋 CalledProcessError
#    任一例外都不可吞
bin_dir = subprocess.check_output(["uv", "tool", "dir", "--bin"], text=True).strip()
if not bin_dir:
    raise RuntimeError("uv tool dir --bin returned empty — 不可讓空字串退化成相對路徑 bes.exe/bes")

# 2. 組成 bes 可執行檔的絕對路徑（Windows 帶 .exe）
#    subprocess shell=False 不展開 tilde，literal "~/.local/bin/bes" 會 file-not-found
bes = str(Path(bin_dir) / ("bes.exe" if os.name == "nt" else "bes"))

# 本機預設 illustration（僅示意路徑長相，非跨機器恆等路徑，不可當 uv tool dir --bin 失敗時的 fallback）：
# bes = str(Path.home() / ".local" / "bin" / ("bes.exe" if os.name == "nt" else "bes"))
# 即 C:\Users\example\.local\bin\bes.exe（Windows 本機）

# 3. 移除繼承的 VIRTUAL_ENV，防止 caller 環境干擾全域工具
env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}

# 4. 呼叫全域 bes，不設 repo cwd
result = subprocess.run(
    [bes, "search", "--keyword", "範例客戶", "--limit", "50", "--json"],
    env=env,
    capture_output=True,
    text=True,
    check=True,  # 非零退出時拋 CalledProcessError
)
```

**重點說明**：

- `uv tool dir --bin` 回傳的目錄可被 `UV_TOOL_BIN_DIR` / XDG 環境變數覆寫，非固定 `~/.local/bin`，故必須動態取得。
- `bin_dir` 若為空字串，不可讓它退化成相對路徑 `bes.exe` 或 `bes`——那會繞回 caller cwd/PATH 的 shadow，正是契約要擋的。
- `check_output` 的 `FileNotFoundError`（uv 不存在）與 `CalledProcessError`（非零退出）任一都不可 catch 後吞掉；讓呼叫端知道環境不正常。

**注意**：本頁其他 `uv run python {SKILL_DIR}/scripts/*.py` 這類 helper-script 呼叫**不受**以上禁令影響——禁令只針對 `bes` CLI 本身，`uv run` 用於執行其他腳本不在此限。

### Stage 3：FTS + LIKE cross-filter（主力工具）

呼叫 bundled script，避免 inline Python 跨 shell 的 escape 陷阱：

```bash
uv run {SKILL_DIR}/scripts/find-cross-filter.py \
  --must "範例客戶" \
  --context "交期,規格,價格,報價,單價,數量" \
  --from-date 2025-04-01 \
  --to-date 2025-06-30
```

Script 行為：對日期窗內每封信檢查「MUST 出現在 body OR subject」且「ANY CONTEXT 出現在 body」；命中後印 200 字 snippet 含命中關鍵字。`--snippet-keywords` 可改 anchor 詞（預設用 context list），anchor 比對不分大小寫；找不到 anchor 的 SQL 命中仍會印出（snippet 改取內文開頭並標 `[anchor not found; body start]`，stderr 另有計數），不會消失。

`--from-date` / `--to-date` 與 Stage 2 同語意：`YYYY-MM-DD`、Asia/Taipei 整天、**兩端都含**（同一天可兩端給同值）；格式錯誤 exit 2 並指出參數與錯值。

把 snippet 列給 user 看 — 不要自己挑信，user 看了會直接指「就那封 #21853」。把候選縮到 < 20 封讓 user 能掃即可。

**Thread 上下文捷徑**：若 user 想看「那封信前後對話」，跳過 cross-filter 直接：

```bash
bes thread <db_id> --format text
```

一口氣拿同 thread 全部信件（走 `in_reply_to` + `references_header` 重建，按 `sent_at` ASC 排）。100 封硬上限保護退化 case。

### Stage 3.5：客戶切分 + 報價抽取

何時跑：
- Stage 3 命中 > 5 封 **且** 跨多個客戶 domain（例：同時看到 `@customer-a.example` + `@customer-b.example`）
- User 的問題涉及「報價 / 承諾 / 價格 / quote」這類結構化資訊
- User 問「我對 X 客戶報過 Y 嗎」（即使只命中一個客戶也要跑，要抽報價）

做法：對每個候選客戶 domain 各重跑一次 Stage 3 的交叉篩選腳本（同樣的 `--must` / `--context` / 日期窗），多加 `--participants-domain <domain>`，得到按客戶分群的命中；再讀各群的 thread 全文（`bes thread <db_id> --format text`）手抽價格：

```bash
uv run {SKILL_DIR}/scripts/find-cross-filter.py \
  --must "範例客戶" \
  --context "交期,規格,價格,報價,單價,數量" \
  --from-date 2025-04-01 \
  --to-date 2025-06-30 \
  --participants-domain customer-a.example
```

`--participants-domain` 與 `bes search` 同義：寄件人或任一收件人（含 cc / bcc）的地址網域**完全等於**該值（不分大小寫；子網域與 `notcustomer-a.example` 這類相似網域不算）；可重複，多值之間 OR；值照字面比對，`_` / `%` 不是萬用字元。每個客戶 domain 換掉最後一行跑一次。

手抽價格的注意事項：
- inline「我們報 250」這種無單位無幣別寫法容易漏看
- 發票號內的金額、規格中的純數字不是報價
- 回報務必附原文 snippet 與所在 db_id

回報格式：

```
按客戶分群命中：
- domain A (X 封)：含報價者列 quotes
- domain B (Y 封)：無報價
請問你要鎖哪個客戶？（排除哪邊？）
```

User 拍板後再進 Stage 4。

### Stage 4：抽出原信 + Ball-in-court + Negative Evidence

User 給 db_id 後，**直接呼叫全域 CLI 不要自己讀 DB**：

```bash
# 單封：metadata header + 一行分隔 + clean plain text body
bes show <db_id> --format text

# 整 thread（推薦）：
bes thread <db_id> --format text
```

`--format text` 由 `query.render_text()` 處理 HTML 清洗。多封信件以 `--- (N of M, msg-id=...) ---` 分隔。

## 附件優先搜尋 / 匯出（`bes attachments`）

當 user 的記憶錨點是「那份 PDF / 附件檔名 / 語言版本」而不是信件主旨時，直接走附件面，不要先 message-first 再人工翻 attachments：

```bash
bes attachments \
  --name-contains TAG \
  --participants-domain customer-a.example \
  --ext pdf \
  --from-date 2025-04-01 \
  --to-date 2025-04-30 \
  --json
```

會回 **一列一附件**（不是一列一封信），每列含 `attachment_id`、`message_db_id`、`rfc_message_id`、`dedup_key`、`sent_at`、`sender_email`、`subject`、`attachment_name`、`content_hash`、`storage_path`、`folder_path`、`exportable`、`carrying_messages`。對外引用來源信時用 `rfc_message_id` / `dedup_key`，**不要**用整數 `message_db_id` 當穩定 bookmark。

要把符合條件的 payload 複製出來時，加 `--copy-to`：

```bash
bes attachments \
  --name-contains TAG \
  --participants-domain customer-a.example \
  --ext pdf \
  --copy-to C:\Exports\customer-a-tag
```

語義與失敗模式：

- `exportable=false` 只代表 DB 有無 `storage_path` pointer；**不是** payload 一定讀得到。`storage_path` 非空但 CAS 檔遺失 / 壞掉 / 路徑逃逸時，export 期會列為 `failed`
- metadata-only attachment（常見於 archive-scrape）在 listing 仍會出現；`--copy-to` 時會逐列回報 `skipped-non-exportable`，**不會靜默消失**
- `--dedupe content-hash` 只合併 **non-null** `content_hash`；null-hash rows 全保留。winner = 最新 `sent_at` → 最高 `message_db_id` → 最低 `attachment_id`；所有承載同檔案的來源信仍保留在 `carrying_messages`
- 預設 export **不覆寫**既有檔名；`--overwrite` 才覆蓋，`--skip-existing` 則計到 `skipped-existing`
- `--overwrite` 和 `--skip-existing` 互斥；同時給會 **copy 前就 exit 非 0**
- export 結束後 exit code 規則：**只有** `failed > 0` 才 exit 非 0；`copied` / `skipped-non-exportable` / `skipped-existing` 單獨都不會讓整體失敗

已知限制：

- `--participants-domain` 仍沿用 message query 的 `recipient_search_text LIKE '%@domain%'` 語義，所以既有 semicolon-packed outbound recipients 會命中；這是刻意繼承現況，不是 parser 已經修正
- `--ext` 以 `attachment_name` suffix 為主，檔名無 suffix 時 fallback 看 `mime_type`；CAS 的 `storage_path` 是 hash-only 物理位置，不承載副檔名語意。檔名與 MIME 都失去副檔名訊號的舊資料仍可能 miss
- `--copy-to` 的 fallback 輸出檔名同樣只看 `attachment_name` / `mime_type`；兩者都推不出來時固定用 `.bin`，不從 CAS 路徑猜
- 以附件形式轉寄的信（`.eml`，`message/rfc822`）是**一列附件**、整封存檔可匯出（沒有檔名時以附信主旨加 `.eml` 命名）；它的內文不算外層信的本文（`bes search --keyword` 不會因附信裡的字命中外層信），它自己夾帶的附件也不另外列出。要看內容就 `--copy-to` 匯出後讀 `.eml`。退信通知（`multipart/report`）夾帶的原信則維持併入外層本文與檢索、不另立為附件，此為刻意設計以方便依原信內容檢索退信。

**長 thread 只萃新內容**：`bes show/thread --format text` 加 `--new-only`，每封只保留新內容（去除引用歷史；支援置頂回覆、下方回覆與夾在引用之間的回覆；偵測 `>` 引用、Outlook header block 含自建 CJK `寄件者:`/`收件者:`/`主旨:`、`-----Original Message-----`、外部信 banner 等邊界）。長 thread 逐封層層引用時體積可縮一個量級。**保守 fallback**：無法識別邊界時回傳全文不截斷（寧可少切也不誤切掉真實新內容）。僅 `--format text` 有效，搭配 `raw`/`json` exit 非 0。

**批次取回（ingestion-grade，消除 N+1 `bes show`）**：

當 caller 是 agent / ingestion pipeline、需要一次取回多個 hit 的完整本文 + thread linkage（而非逐封 `bes show`）時，用 `bes search` 的 opt-in full-body 模式：

```bash
bes search --keyword "<MUST詞>" --from-date 2025-04-01 --to-date 2025-07-01 \
  --limit 50 --full-body --json
```

- **JSON-only**：`--full-body` 未搭 `--json` 直接 exit 2（避免把全文灌進人類終端）
- 每個 hit 多回：`rendered_body_text`（= `bes show --format text` 同款清洗本文）、`message_id`、`dedup_key`、`in_reply_to`、`references`、`to`、`cc`、`bcc`
- 下游要穩定引用 message **優先用 `message_id` 或 `dedup_key`**，不要用整數 `id`（rebuild 後 id 會重排）
- 預設 `bes search`（不帶 `--full-body`）維持 lightweight summary，行為不變
- 何時用：呈現給「人」看單封 / 單 thread → `bes show` / `bes thread --format text`（含分隔線排版）；agent 批次吃多封做後處理 → `--full-body --json`

**Mail forensics header（來源 IP / Received 鏈 / SPF / DKIM / Authentication-Results）**：

`--format text` / `raw` / `json` 都只給 body+metadata、**不含原始 RFC5322 header**。要查「這封信怎麼進來的、走哪條路、寄件來源 IP 是誰」時，用獨立的 header mode：

```bash
bes show <db_id> --headers
```

- 輸出該信完整 RFC5322 header 區段原文（verbatim）；exit 0
- 該信無 header 資料時印「No raw header data available」exit 0 —— **只有已回填 / 新 ingest（IMAP 增量）的信件才有資料**，較舊的歷史 PST 信在回填前為 NULL
- `--headers` 與 `--format`（任何值）或 `--json` **互斥**，併用會 exit 非 0 + stderr 診斷（兩者輸出形狀不相容）
- ⚠️ **Privacy 守則**：raw headers 可能含內部基礎設施識別資訊（內部 IP、路由主機名、Authentication-Results）。非必要勿整段貼到外部管道。

**Ball-in-court 偵測**（CLI 沒做，呼叫 bundled script）：

```bash
uv run {SKILL_DIR}/scripts/ball-in-court.py \
  --seed-db-id 123

# 對單封信而非 thread：
uv run {SKILL_DIR}/scripts/ball-in-court.py \
  --seed-db-id 123 --no-thread
```

Script 透過 `uv tool dir --bin` 定位已發行全域 BES，走 `bes thread <id> --format json` 找最後一封；`--no-thread` 改走 `bes show <id> --format json`。可加 `--database <path>`；未指定時由 BES 解析環境變數與內建預設，不隨 cwd 改變。直接讀該封 JSON 的 `direction` 欄位（`inbound` = 對方寄的）+ body 是否含問句 / 客套催促詞（`?`、`？`、`請`、`kindly`、`please`、`報價`、`quote`、`催`）判斷是否我方欠回覆。`direction` 由 self-address 集合判定（見下方「收發方向」段），不再用 domain 比對——已無 `--our-domains` 這個旗標。

「最後一封」依實際時間點（跨時區 offset 換算後）挑，不看清單位置。兩種**無法判定**的輸出，都不是「沒有未回的信」：

- `[indeterminate]`（exit 3），兩個成因，訊息會寫明是哪一個：
  - thread 達到 CLI 的 100 封上限（恰好 100 封也算），拿到的是最早 100 封、真正最新的信看不到。BES 的截斷 warning 會原樣轉到 stderr。
  - thread 多於一封，且其中任何一封的 `sent_at` 缺漏或無法解析：那封可能就是最新的一封，排不出先後。訊息列出這些信的 `db_id`，逐封 `bes show` 交 user 判斷。
- `[unknown-direction]`（exit 0）：最後一封沒有可判定的收發方向（寄件人空白），不當作我方。

#### Stage 4 回報格式（呈現給 user）必含五段

```
## 事實
db_id / subject / From / To / CC / sent_at / PST 來源 / folder_path
清洗後 body 全文（不是摘要）；多封依時序排，標哪封是「定案版」（通常 thread 最後一封自己發的）。

## 抽取的價格 / 承諾
列 Stage 3.5 抽出的報價（原文 snippet + 所在 db_id）。
若 user 問的是 price 但沒抽到：明說「DB 未找到此客戶對此產品的明確報價」（不要含糊帶過）。

## Ball-in-court（未回球）
列「對方在等回覆」的最後一封；給 db_id 讓 user 順手追。
若無 → 寫「無 unanswered inbound」。
script 回 `[indeterminate]` 或 `[unknown-direction]` → 寫「無法判定」並附原因，不可寫成「無 unanswered inbound」。

## Excluded candidates（排除清單，negative evidence）
明列「我看過但排除的 customer / thread」+ 排除理由。
例：「排除 乙商行（msg 123, 124）：同產品但不同案件，與此次客戶無關」。
不寫等於 user 不知道你踩過哪些可能候選，下次同問題會重做。

## Caveats
- HTML cleaning 是 regex，可能殘留少數 inline style
- 報價是人工從原文抽出的（易漏無幣別寫法、別把發票編號當報價）
- DB cutoff 至 `SELECT MAX(sent_at) FROM messages`
```

---

## 失敗模式

- **粗篩 0 命中**：先檢查 keyword 是否含 hyphen 數字未加 phrase quote、時間範圍是否太窄。放寬時間後仍 0 → **不要直接斷言「查無此信」**，先做一次 sync 驗證再下結論（見下方「0 命中 sync 驗證」）。不要硬猜。

  #### 0 命中 sync 驗證（把假陰性轉成驗證過的真陰性）

  Stage 0 的 sync 觸發綁 query 措辭（今天/24h/最近/剛剛/最新），但 DB stale 的真正條件是「目標信很新」。措辭像歷史檢索、目標卻是剛進來的新信時，會打到尚未同步的 DB 回假陰性。所以**五階段主搜尋（Stage 2 粗篩 / Stage 3 cross-filter）放寬後仍 0 命中**時，若本任務尚未跑過 Stage 0 sync，先跑一次 throttle-gated：

  ```bash
  bes sync-imap --pre-task
  ```

  再依 stdout 首行 + exit code 分分支：

  | 結果 | 訊號 | 動作 |
  |---|---|---|
  | 真的 sync 了 | stdout 首行 `mbsync: pulled N new messages`，exit 0 | **重搜一次**；仍 0 → 才是驗證過的真陰性，回報「DB 內無對應記錄」 |
  | 略過且上一次成功 | stdout 首行 `sync-imap: skipped` 且括號內只有時間（未含 `last sync failed` / `did not complete` / `status unknown`；`--json` 則 `last_sync_failed: false`），exit 0（15 分內已 sync 成功，DB 已新鮮） | **不重搜**，0 命中即真陰性，回報「DB 內無對應記錄」 |
  | 略過但上一次失敗或未完成 | stdout 含 `last sync failed`、`last sync did not complete` 或 `last sync status unknown`（`--json` 則 `last_sync_failed: true`），exit 0 | 回報「**無法確認**：上一次同步失敗，DB 可能不完整／stale」，**不要**宣稱查無 |
  | sync 失敗 | exit != 0，或 stdout 含 `mbsync: ERROR` | 回報「**無法確認**：sync 失敗，DB 可能 stale」，**不要**宣稱查無（避免 stale DB + sync 壞雙重假陰性） |

  **適用範圍**：套用於五階段主搜尋的 0 命中**以及上方 fast-path 的 0 命中**（fast-path 跳過 Stage 0，0 命中時一律落回此驗證）。`contact-history` aggregate mode 與 Stage 0.5 browse mode **不**套這層回退（它們不是「找某封特定信」的語意，0 命中本身就是答案）。
- **粗篩爆量 > 200**：通常是 MUST 詞太通用（例只給「報價」）；回頭請 user 補客戶名 / 產品代號
- **Stage 3 cross-filter 仍找不到關鍵句**：可能 body 裡的中文跟 user 講的詞不同（例 user 講「交期兩週」、原信寫「lead time 2 weeks」）；改 `--context` 加入英文同義詞重跑
- **Ball-in-court 偵測誤報**：觸發詞（問號、「請」）在客套句也會命中，標 ball-in-court 不等於確定未回；回報時讓 user 自己判斷

## 收尾

- 暫存檔（`.tmp_search.json` 之類）查完即刪：`rm .tmp_*.json`
- 若 user 之後要持續引用原信，把它存到 **user 當前專案根** `originals_<topic>_<keyword>.txt`，告訴 user 路徑；**不要**寫回 mailattic repo
- 找信完問 user「要回信嗎？」是 → 載入 `capability-c-reply.md` 進 Capability C；不是 → 收尾

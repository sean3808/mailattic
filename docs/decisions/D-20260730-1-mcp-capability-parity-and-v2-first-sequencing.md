---
id: D-20260730-1
date: 2026-07-30
status: accepted
title: MCP 能力終態與 skill 三 capability 對等；技術順序由重構暴露度決定，SDK v2 先行
---

## Context

GitHub issue `#24` 原本把 MCP 能力切成三期：Phase 1 唯讀（已上線五工具）、Phase 2 sync、Phase 3 write tier。那個分期是按**風險遞增**排的 —— 先做沒有副作用的，最後做會寄信的。

2026-07-30 這輪有三項新事實同時到齊，使原分期的前提失效：

1. **MCP 規格 2026-07-28 已正式發布**，Python SDK v2.0.0 同日 stable。協定演化模式從單體式 spec 修訂改為 extensions 獨立版本演化；long-running work 有了官方的 Tasks extension；v2 的內部架構是「純 handler kernel + transport 為薄 driver」。本 repo 目前 pin 在 `mcp>=1.28.1,<2`。
2. **sync 耗時實測**：讀 `.mailattic-work/logs/mbsync-*.log` 最近三日全部 run，區間為 2m43s ~ 22m56s，且與新信數不成正比（新信少的 run 可能比新信多的 run 更慢）—— 主要成本在 IMAP folder 掃描，沒有可預測上界。原提案「Phase 2 = `mail_sync` 包 `pre_task_sync_imap`」在此證據下不成立：MCP 工具呼叫會先 timeout，而子行程仍在跑。
3. **唯讀保證的實際形狀**：`connect_database(..., read_only=True)` 的 `read_only` 是 per-connection 參數（`storage.py:53`）。目前 MCP server 的唯讀性由六處 call site 各自傳 `read_only=True` 維持。全唯讀前提下這是真保證；引入第一個寫入路徑後即降級為「每處記得傳對參數」，與 issue `#32`（`cap_error_echo` 無單一收斂點）同型。

過程中曾提出「邊界收在 MCP 面只做讀，動作面留給 CLI」。該建議被否決：CoWork 的角色是工作助理，動作面留給 CLI 等於它不能主動收信、也不能在使用者說「我看過了，你幫我寄」時寄出。**能力目標不是可調變數。**

## Decision

兩件事分開定案。

### 一、能力終態固定 —— 與 `mailattic` skill 的三個 capability 對等

| Capability | skill 端（Claude Code / CLI） | MCP 端終態 |
|---|---|---|
| **A. Sync** | `bes sync-imap --pre-task` | `mail_sync` |
| **B. Find** | `search` / `show` / `thread` / `contact-history` / `attachments` | 五個唯讀工具（已上線） |
| **C. Reply** | `draft-reply` → `list-drafts` → `send-draft`（TTY 授權） | `mail_draft_reply` → `mail_send` |

這是終態，不接受以「風險太高」或「協定不支援」為理由縮減。協定不支援是**排序問題**，不是範圍問題。

### 二、技術順序由「重構暴露度」決定，不由風險遞增決定

判準：哪個技術在後續重構（v2 遷移、遠端化）中受影響最少，就先做。

```
1. 升 mcp SDK v2（維持 stdio，工具邏輯不動）
2. 唯讀收斂點（加寫入路徑的結構前提）
3. mail_sync — 採官方 Tasks extension
4. mail_draft_reply / mail_send — draft/send 分離 + host 授權提示
5. 遠端化 + OIDC 授權 — transport 換 driver
```

**第 1 步是關鍵倒序。** 它本身不新增任何能力，卻同時降低第 3、4、5 步的重構面：Tasks 讓 `mail_sync` 不必自建輪詢；薄 driver 架構讓遠端化不必重寫工具邏輯；extensions 獨立版本演化讓 target 2026-07-28 之後成為增量遷移。

**送信授權門的設計與協定版本無關。** 授權發生在對話層 —— 使用者讀過草稿、下達「寄出」指令，該指令本身即授權；工具層的 host permission prompt 是留痕與防呆，不是安全牆。形狀沿用 CLI 現有三段，TTY 換成對話：

```
mail_draft_reply → 回傳草稿全文（人類審閱）→ 指令「寄出」→ mail_send（只吃 draft_id，不吃內容）
```

`send-once` marker 狀態機、from-invariant、draft/send 分離、APPEND-to-Sent 可見性、no-bulk-blind-send 全部沿用既有 outbound 契約（[D-20260528-1](D-20260528-1-bes-never-generates-content.md)、[D-20260528-2](D-20260528-2-outbound-visibility-via-bcc-self.md)、[D-20260714-2](D-20260714-2-chinese-sent-folder-of-record.md)）。

## Rejected alternatives

- **MCP 面只做讀，動作面留給 CLI**：砍掉能力目標本身，而非調整路線。CoWork 作為工作助理必須能收信與寄信。
- **自建 background + polling 作為 `mail_sync` 的長跑機制**：語意與 Tasks 相同（觸發回 handle → 另一工具查狀態），但若 v2 先升則完全不必發生。先做等於寫一套註定被官方機制取代的協定層程式碼。
- **用 MRTR（Multi Round-Trip Requests）當送信授權門**：方向錯誤。MRTR 解的是「server 主動向使用者提問」，而本專案的人已經在對話裡。且規格的安全考量只保護 `requestState`，對 `inputResponses` 無任何 provenance 保證 —— 信任模型上等同 `--confirm <token>` flag（既有教訓已判定為 security theater）；另規格明文禁止對未宣告 `elicitation` 能力的 client 發送，異質 client 下不可依賴。
- **直接包 `pre_task_sync_imap` 作為 `mail_sync`**：15 分鐘節流閘只讓多數呼叫變成即時 skip；真正觸發時是無上界長跑（實測最長 22m56s）。
- **維持 `mcp<2` 直到遠端化才升**：遠端化時 transport 層須自行承擔，且期間所有新增工具都建在即將落日的世代上。

## Consequences

- **第 1 步不產生任何使用者可見的新能力**，其價值全在降低後續重構面。若把它當成「沒有產出的技術債清理」而跳過，第 3、4、5 步的成本會各自上升。
- CLAUDE.md 記載的**回應大小三層保證第三層**（框架 Pydantic 驗證錯誤的 293 字元經驗測量）明文釘在 `mcp` 1.28.1 的 lockfile resolution 上。升 v2 後該實測值作廢，須重測並更新 CLAUDE.md 對應段落與回歸測試。
- **唯讀收斂點（第 2 步）不可延後到寫入工具之後補**。一旦 `mail_sync` 或 `mail_send` 落地而收斂點未建立，唯讀性即從結構保證退化為紀律，且退化是靜默的（測試仍會全綠）。
- 本 ADR 取代 issue `#24` 本文的 Phase 2 / Phase 3 分期定義。該 issue 保留為討論脈絡與證據來源，但**分期的正典在此**。
- 遠端化的授權模型（第 5 步）另有獨立約束：Anthropic / OpenAI / Notion 三方客戶端皆由各自雲端發起呼叫，server 必須公網可達，私有網路兜底不成立。詳見 issue `#24` 的相關 comment。
- **日期標記勘誤**：issue `#24` 於 2026-07-30 新增的數則 comment，內文日期標記誤寫為 `2026-07-29`。GitHub 的 comment 時間戳為準，本 ADR 的 2026-07-30 為正確日期。

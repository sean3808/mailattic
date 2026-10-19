---
id: D-20260721-1
date: 2026-07-21
status: accepted
title: 為 CoWork 重評 no-MCP 立場（限定唯讀 read-tier 例外；Spike A 已 GO）
---

## Context

本專案的既有立場是「MCP server 目前不做」（見 CLAUDE.md vision 段與 `mailattic` skill）：其他工具的 subagent 一律透過全域 `bes` CLI + Bash tool 使用本基礎建設。這個立場對**跑在 Windows host 上**的 Claude Code / Codex subagent 是正確的 —— 它們看得到 host 的 CLI、DB 與網路。

CoWork 打破了這個前提。實測（Sean 在「On your computer」CoWork session 親跑）確立三條硬邊界：

1. 即使選本地版，CoWork 仍是 Ubuntu Linux **沙箱**，全域 `bes` 不在 PATH；
2. 只掛載選定資料夾，host 工具/套件不可達；
3. `mailattic.test` **DNS 都解不出**（`Temporary failure in name resolution`），郵件伺服器的 IMAP/SMTP 不可達。

因此 CoWork 沙箱裡唯一能通的能力是「純 `sqlite3` 讀掛載的 `.db` 快照」；sync / reply 三條全踩沙箱邊界，結構性不可能。要讓 CoWork 有真正的 mail 記憶能力，唯一未驗的 load-bearing 假設是：**Windows host 上的 MCP server 能否被 CoWork 沙箱經 Claude Desktop app bridge 呼叫到**。

## Decision

**重評（revisit）no-MCP 立場，但只開一個限定例外：唯讀 read-tier，供 CoWork 使用，且 pending spike 結果。**

- 在 Windows host 上建一個最小的**唯讀** MCP server（單一 `mail_search` tool，wrap 既有 `search_messages` query API）。
- 這個例外的成立與否由 **Spike A** 決定：由真人從 live CoWork session 執行 runbook，記錄 bridge 的 `GO` / `NO-GO`。`NO-GO` 是有效的 spike 結果 —— 若不通，fallback 是 sync/reply 留在 Claude Code、CoWork 只做唯讀 find。
- 在 spike 有結果前，CLAUDE.md / skill 只加**警語指標**指向本 ADR（「read-tier bridge under evaluation, pending spike」），**不**翻寫既有 no-MCP 立場、**不**改動 host subagent 走 `bes` CLI 的既有指示。

唯讀是**連線層強制**的安全屬性，不是慣例：server 經 `file:<path>?mode=ro` URI + `PRAGMA query_only=ON` 開 DB，寫入由 SQLite 本身拒絕（僅靠「只註冊一個 tool」不足以保證，那只是 defense-in-depth 第二層）。

## Rejected alternatives

- **全面翻寫 no-MCP 立場、直接採用 MCP**：spike 尚未證明 bridge 可通。若先翻寫立場，bridge 不通時等於文件宣稱了不存在的能力。故只重評、不採用。
- **同一 change 內一併做 send/sync tier + T3 送信信任分級**：send-tier 的信任分級只有在 read-tier bridge 證明可通、且送信真的上桌時才有意義。放進 Spike A 只會把不可逆的送信授權設計綁在一個尚未驗證的傳輸假設上。故 send / sync / reply / T3 明確留在 scope 外。
- **CoWork 沙箱直接掛本機 stdio connector 連 localhost**：沙箱連不到 host 的 localhost。bridge 必須由 Claude Desktop app 提供（server 跑 host、Desktop 當 bridge），這也正是本 spike 要驗的那條未驗路徑。
- **讓 MCP server 走 subprocess 呼叫 `bes` CLI**：多一個 process + JSON round-trip，且會把 server 耦合到 `bes` CLI surface 與其同步契約。改為直接 import Python query API。

## Consequences

- 新增一個唯讀 MCP server 模組 + `mcp` SDK runtime 依賴；不動每日 IMAP sync、不動 `bes` CLI surface、不動 DB schema。
- `connect_database` / `search_messages` 新增 additive default-off `read_only` 參數，既有 caller 行為不變。
- Spike 的核心答案（bridge 通不通）是**人工 gate**：implementer 無法代跑，必須由 Sean 從 CoWork 執行 runbook 記錄 `GO` / `NO-GO`。「寫了一份沒跑過的 runbook」不算完成。
- 若 spike `GO`：read-tier MCP 成立，才另開後續 change 談 send/sync tier 與 T3 信任分級。若 `NO-GO`：維持既有分工，CoWork 僅唯讀 find。

## Outcome（2026-07-23 補記）

**Spike A 裁決：`GO`。** CoWork 沙箱經 Claude Desktop app bridge 成功呼叫到 Windows host 的唯讀 `mail_search`，回傳 live 中央 DB 結果（命中最新 `sent_at` 晚於 DB snapshot cutoff，證明非掛載快照）。完整執行紀錄與證據存於本機工作檔 `docs/cowork-mcp-bridge-validation.md`（未入版控）；能力契約見 `openspec/specs/cowork-mcp-bridge/spec.md`（不隨本 repo 帶出）。

本節**只陳述 spike 事實，不宣告 no-MCP 立場的最終處置**。理由：bridge 既已證明可通，後續方向是**持續擴張 MCP 能力面**（Phase 1 其餘唯讀工具 → sync tier → write tier），目標是讓 CoWork 能承擔更多非 coding 工作。因此「MCP 例外的最終邊界在哪」尚未收斂，不在本 ADR 定案——待能力面推進到穩定形狀後，另開 ADR 描述最終立場。

在那之前仍然成立的約束：write / send tier 的授權設計（T3 信任分級、no-bulk-blind-send、授權摘要 = 真實 SMTP envelope）**未做**，仍是 scope 外；host subagent 走 `bes` CLI 的既有分工不受影響。

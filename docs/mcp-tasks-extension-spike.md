# MCP Tasks extension 探針結論（`#76`）

日期：2026-08-04
對應 issue：`#76`
對應 ADR：[D-20260730-1](decisions/D-20260730-1-mcp-capability-parity-and-v2-first-sequencing.md) 第 3 步

## 結論：NO-GO（現階段）

**官方 Tasks extension 目前無法用來實作 `mail_sync`，卡點在上游，兩端都還沒到位。**

這不是「此路不通」，是「上游還沒交付」。ADR 第 3 步選 Tasks 的判斷本身沒有被推翻 —— 被推翻的是「協定發布 = 可以開工」這個時間假設。

決定性事實只有一條，其餘都是佐證：

> **`mcp` 2.0.0 沒有 Tasks 實作。** 加上這條，客戶端支不支援已經不影響結論 —— server 這端根本講不出 Tasks。

而要讓 server 講得出來，就得自己寫整套任務生命週期 —— 那正是 ADR 第 3 步寫明「零自建」要避免的東西，也是 `#76` 明文交代「不要繞過」的事。所以我沒有自建，也沒有為了讓探針跑起來而先寫一版。

## 票面前提的修正

`#76` 開票時的三個前提，有兩個已經過時。開票時間（2026-07-30）距離 MCP 2.0 發布（2026-07-28）只有兩天，當時的判斷合理，但生態在這兩天後才展開。

| 票面前提 | 現況 |
|---|---|
| 「Tasks 的協定型別確認存在」 | 存在，但那是 **2025-11-25 世代的形狀**。型別檔 `mcp_types/_v2026_07_28/` 裡**一個 Tasks 型別都沒有** |
| 「未知數不在 server 端有沒有東西可用」 | 反了。**server 端才是明確缺口**，而且是官方 SDK 尚未合併的 PR |
| 「從 Claude Desktop 實際呼叫，看握手宣告」 | 觀測點已經搬家。2026-07-28 起 Tasks 的能力宣告走**每個請求的 `_meta`**，不在 `initialize` 握手裡 |

## 六個問題的答案

### 1. 客戶端有沒有宣告 tasks 能力？

**沒有證據顯示支援；而且這題已經不是 gate 了。**

- 官方 [Extension Support Matrix](https://modelcontextprotocol.io/extensions/client-matrix) 目前**只收錄三個 extension**（MCP Apps、OAuth Client Credentials、Enterprise Auth），**Tasks 連欄位都還沒有**。Claude Desktop 在該表只列 MCP Apps。
- Anthropic 自己的 [MCP 2026-07-28 公告](https://claude.com/blog/bringing-mcp-2026-07-28-to-claude)只說「support is rolling out across Claude products soon」，沒有點名任何 Claude 介面已支援 Tasks。
- 本機 `%APPDATA%\Claude\logs\mcp.log` 的歷史握手（2026-07-18，protocol `2025-11-25`，clientInfo `claude-ai`）只宣告 `extensions.io.modelcontextprotocol/ui`。**這是舊世代紀錄，只能當旁證** —— 新版 log 已將 params 遮蔽，且新協定的宣告根本不在握手裡。

**為什麼沒有跑本機探針**：即使跑出「客戶端支援」，server 端仍然講不出 Tasks，結論不變。而要觀測新的 per-request `_meta` 宣告，必須強制結束並重開 Claude Desktop（該 app 自己會回寫設定檔），代價是中斷當時的工作 session。花這個代價去問一個不會改變結論的問題，不划算。**待 SDK 支援落地後，這題要重問，且探針要照新的 `_meta` 形狀重寫** —— 目前留在 session 暫存區那版是照舊 `initialize` 形狀寫的，已作廢。

### 2. server 端把工具掛成 task 的 API 形狀是什麼？

**`mcp` 2.0.0 沒有這個形狀。** 協定形狀已知，SDK 實作未合併。

SDK 端證據（皆為安裝在 `.venv` 內的 2.0.0 實測）：

- `mcp_types/methods.py` 的四個 dispatch 表，每個協定版本都留一行註解 **「tasks/\* deliberately absent」**，含最新的 `2026-07-28`。
- `mcp_types/_v2026_07_28/` 內沒有任何 Tasks 型別；現存的 `Task` / `CreateTaskResult` / `tasks/list` / `tasks/result` 全部只在 `_v2025_11_25` 與頂層別名裡，是**已被新設計取代的舊形狀**。
- `mcp/server/` 與 `mcp/client/` 全樹沒有任何 Tasks 實作。`mcp/server/extension.py:59` 只把 `tasks/get` 當成「擴充可以自己加的 method」的舉例。

上游進度（`modelcontextprotocol/python-sdk`，查詢日 2026-08-04）：

| PR / issue | 標題 | 狀態 |
|---|---|---|
| [#2714](https://github.com/modelcontextprotocol/python-sdk/pull/2714) | Remove Tasks (SEP-1686) from the SDK | **已合併**（2026-06-02）—— 舊版 Tasks 是被拿掉的 |
| [#3005](https://github.com/modelcontextprotocol/python-sdk/pull/3005) | Add the SEP-2663 Tasks extension (core) | **open**，未合併（開於 2026-06-26） |
| [#3226](https://github.com/modelcontextprotocol/python-sdk/issues/3226) | Client-side support for the tasks extension | **open**（更新於 2026-08-02） |

協定形狀（[官方 Tasks 文件](https://modelcontextprotocol.io/extensions/tasks/overview)）已與 2025-11-25 世代不同，實作時不能照舊型別抄：

- 識別碼 `io.modelcontextprotocol/tasks`，走 extension 協商 —— 客戶端在**每個請求的 `_meta.io.modelcontextprotocol/clientCapabilities.extensions`** 宣告，server 在 `server/discover` 的 capabilities 宣告。
- `tools/call` 回 `CreateTaskResult`（`resultType: "task"`，含 `taskId` / `status` / `ttlMs` / `pollIntervalMs`），**由 server 逐請求決定**要不要開 task，不是逐工具設旗標。
- 方法面：`tasks/get`（輪詢）、`tasks/update`（補件）、`tasks/cancel`。**`tasks/list` 已移除**，阻塞式的 `tasks/result` 已被輪詢式 `tasks/get` 取代。
- 狀態機：`working` / `input_required` / `completed` / `failed` / `cancelled`，後三者為終態。
- server 有明文義務：**回 task 前必須確認客戶端宣告過該 extension**，且 task 必須在回應送出前就 durably 建立。

### 3. 跑超過 20 分鐘的任務，客戶端會不會放棄？門檻在哪？

**未觀測，因為沒有 Tasks 可觀測。**

規格文件把這件事講成 Tasks 存在的理由：客戶端各自實作 tool-call timeout、規格不管、常見落在 30–60 秒。這正是我們 sync 實測 2m43s ~ 22m56s 撞上的那道牆（ADR Context 第 2 點）。**在沒有 Tasks 的情況下重測阻塞呼叫的門檻沒有意義** —— 結論已知是「會 timeout」，那本來就是選 Tasks 的原因。

### 4. 進度／狀態在 CoWork 介面上呈現成什麼樣子？

**未觀測。** 機制不存在。規格層面有兩條路：預設輪詢，或 server 推 `notifications/tasks`（客戶端經 `subscriptions/listen` 訂閱）。實際呈現只能等客戶端支援後實測。

### 5. 任務進行中關掉客戶端再打開，任務還在嗎？

**未觀測。** 規格明文 taskId 是 durable handle、客戶端應持久化以便重啟後續輪 —— 但那是對實作的要求，不是對現況的描述。

### 6. 取消的語意是什麼？

**規格層面有答案，實作層面未觀測。** `tasks/cancel` 是**協作式**的：server 確認收到取消意圖，但沒有義務真的停止工作，任務仍可能走到非 `cancelled` 的終態。對 `mail_sync` 有直接影響 —— 取消不等於 IMAP 掃描會停。

## 與驗收標準的落差

`#76` 要求六題各有一則**實際觀測**的答案。實際交付的是：兩題有結論性證據（1、2），四題（3–6）只有規格層答案，因為**被觀測的機制在 server 端尚未存在**。

這是誠實的結果，不是偷懶的結果：`#76` 明文寫「如果客戶端不支援，回報，不要自己補一套輪詢機制」。同樣的理由適用於 server 端 —— 為了讓探針跑完六題而先自建一版 Tasks，等於把後面兩步的重工偷渡進來。

## 對第 3 步的影響

ADR 第 3 步「`mail_sync` 採官方 Tasks extension」的**方向**沒有被推翻，被推翻的是**可開工時間**。ADR 的 Rejected alternatives 有一條寫著自建 background + polling「若 v2 先升則完全不必發生」—— 那句話的前提是 v2 會帶著 Tasks 一起到，現在已知不會。

三個選項，都是拍板層級，不在本探針的權限內：

1. **等上游**：盯 `python-sdk#3005` 合併，屆時重跑客戶端探針（照新的 `_meta` 形狀）。第 3 步在此期間掛起。
2. **回 `#24` 重選路**：把自建 background + polling 的否決理由重新評估 —— 它當初被否決的前提已經不成立。
3. **繞過第 3 步**：`#77` 不依賴 Tasks（見下），可以先做完，把「資料有多新」這個實際需求先滿足，觸發同步的能力晚點再說。

**`#77` 不受此結論影響。** 它是唯讀狀態查詢，不引入寫入路徑也不需要長跑機制，其前置是 `#58`（已合併）。`#78` 才是真正被卡住的那張。

## 附錄：查證來源

| 主張 | 來源 |
|---|---|
| SDK 無 Tasks dispatch | `.venv/Lib/site-packages/mcp_types/methods.py`（四處 "deliberately absent" 註解） |
| 2026-07-28 型別無 Tasks | `.venv/Lib/site-packages/mcp_types/_v2026_07_28/`（grep `tasks/` 零命中） |
| SDK 無 Tasks 實作 | `mcp/server/`、`mcp/client/` 全樹 grep |
| 上游 PR 狀態 | `gh api repos/modelcontextprotocol/python-sdk/pulls/3005`、issue 搜尋 |
| 協定形狀 | <https://modelcontextprotocol.io/extensions/tasks/overview> |
| 客戶端支援 | <https://modelcontextprotocol.io/extensions/client-matrix> |
| Claude 端說法 | <https://claude.com/blog/bringing-mcp-2026-07-28-to-claude> |
| 歷史握手（旁證） | `%APPDATA%\Claude\logs\mcp.log`，2026-07-18 `initialize` |

過程中未修改 repo 內任何既有檔案；探針 server 寫在 session 暫存區，未進 repo。

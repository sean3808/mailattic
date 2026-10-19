---
id: D-20260914-1
date: 2026-09-14
status: accepted
title: 寄信授權在對話層、工具層以草稿指紋保證「寄出的＝看過的」；推翻「拒絕任何非互動送信旁路」
---

## Context

2026-05-28 `add-outbound-send-capability` 定下送信的硬規則：授權只能是互動終端（TTY）裡輸入的明文授權詞，**拒絕任何非互動的送信旁路**（`--yes`、auto-confirm、caller 直接 import 底層 helper）。行為契約 #2 釘住的正是這條。理由是「AI 驅動的對外副作用只要留一條旁路，防誤發就退化成 security theater」。

2026-09-14 `#195` 在代寄情境實測：**在 Claude Code 裡沒有任何一條路寄得出去**。

| 入口 | 結果 |
|---|---|
| agent 用 Bash / PowerShell tool 跑 `bes send-draft` | 非 TTY，拒寄 |
| Sean 在 Claude Code 輸入框用 `! bes send-draft` | `!` 看起來像終端機但不配 TTY，同樣拒寄 |
| agent 照另一個專案腳本的做法 import `_prepare_draft_for_send` → `_submit_prepared_draft` | 被 Claude Code auto mode classifier 當成削弱安全機制擋下 |

唯一可行的是另開 Windows Terminal 手貼指令，Sean 從未成功過。兩支專案腳本為了能寄，各自 import 私有 helper 繞過 gate。TTY gate 的**實際效果不是防誤寄，而是讓 Claude Code 使用情境完全寄不出去**，並把真正的寄送路徑推向沒有任何檢查的私有 import。

Sean 拍板（`#195` 留言）：「agents 你一定要問我是不是真的可以寄出，當然這是你們的義務，但是你不能搞到現在我好像什麼方法都寄不出去，整個就是把自己給綁死。」

## Decision

**授權在對話層**：agent 把草稿（主旨、完整 To/Cc/Bcc、內文、附件、草稿編號）呈給使用者，使用者明說寄出才寄。這是 agent 的義務，由 skill `mailattic` 的 capability-c-reply 寫成必經步驟。

**工具層只保證「寄出的＝看過的」**：

- 草稿指紋 = 草稿 `.eml` 全部 bytes 的 SHA-256（小寫 hex），`draft-reply` 印在 stdout（連同附件名稱與大小）。
- `send-draft --fingerprint <sha>`／`send_draft(..., fingerprint=)`：重算不符即拒寄、不寫 send-once marker；帶指紋時不論是否 TTY 都不跳提示。指紋在讀檔當下對**同一份 bytes** 計算，送出的訊息就是由那份 bytes 解析而來。
- `send-batch <draft_id>=<sha> ...`（全部帶或全部不帶）／`send_batch(..., fingerprints=)`：逐封驗，某封不符只拒該封，其餘照寄。
- 無指紋且真 TTY → 原本的明文授權照舊；無指紋且非 TTY → 拒寄，訊息列出兩條出路。
- 指紋不符的錯誤訊息**刻意不印當下指紋**：印出來就等於教 caller 抄新指紋重寄、跳過重新呈審。

**多封一次呈審、一次授權**：推翻 `no-bulk-blind-send` 的逐封形態（Sean：「如果之後每封信都要這個樣子的話，我會發瘋」）。總表每封一列（草稿編號、主旨、完整 To/Cc/Bcc、附件），同模板內文只呈一次。「不能送整個草稿資料夾、必須逐一列舉 draft_id」維持不變。

契約 #2 依此改寫為 CLI 形態，條文不寫「使用者有沒有被問」（測不到）。CLI 與 MCP 是同一個需求的兩個入口，授權模型一致；MCP 形態另立契約條目，由 `#189` 負責並共用 `draft_fingerprint`。

## 指紋擋得住／擋不住什麼

套用 [D-20260908-1](D-20260908-1-guards-defend-confused-deputy-not-deliberate-collision.md) 的判準（「要讓它失效，需不需要 principal 違背自己的意圖？」）：

- **擋得住（confused deputy）**：呈審之後草稿檔被改（重產、手改、其他 session 覆寫）；草稿編號與指紋配錯。
- **擋不住，也不假裝擋**：agent 自己連跑 `draft-reply` → `send-draft` 而沒有問使用者。這與「`--confirm <token>` 是 security theater」同一件事——指紋不是認證，它證明不了對話裡發生過什麼。這一半由 agent 的對話紀律承擔，與 D-20260730-1 為 MCP 定的形態同一條件。
- **已知缺口，不加機制**：bes 不自動淘汰舊草稿，改稿時沒刪掉的舊草稿仍在待寄清單，指紋只擋「編號與指紋配錯」，擋不住「舊編號＋舊指紋整組拿錯」。`#195` 正文「擋得住寄出別的草稿」一句過度宣稱，以本段為準。緩解是紀律：呈審時必標草稿編號，寄出時用呈審那一組。要自動淘汰舊草稿時再開票。
- **已知缺口，不加機制**：呈審的內文取自 agent 自己的內文檔，不是從草稿讀回來的；主旨、收件人、附件、指紋則照抄 `draft-reply` 輸出。指紋保證的是「寄出的＝`draft-reply` 產出的那份草稿」，擋不住「改了內文檔卻沒重產草稿」或「呈審時貼錯內文檔」。緩解是紀律：改內文就刪舊草稿、重跑 `draft-reply`、重新呈審。要讓 `draft-reply` 輸出草稿內文或內文雜湊時再開票（2026-09-14 路線檢討時補記）。

## Rejected alternatives

- **維持 TTY-only**：實測等於在 Claude Code 內完全不能寄，並把寄送推向繞過 gate 的私有 import（兩支腳本已經這樣做）。
- **`--yes` / `--confirm <token>`**：不綁草稿內容，連「看完之後被換掉」都擋不住。
- **新增 `show-draft` 子命令**：`draft-reply` 的 stdout 已足夠呈審；要重審舊草稿或懷疑內文被改時再加。

## Consequences

- 推翻 2026-05-28 `add-outbound-send-capability` 的「拒絕任何非互動送信旁路」，以及 [D-20260528-1](D-20260528-1-bes-never-generates-content.md) Consequences 中「送出永遠要求 TTY + typed confirmation」一句。`GLOSSARY.md` 的「明文授權」收窄為 TTY 形態、新增「對話層授權」，Rejected framings 同步修正。
- 兩支繞道腳本（下游專案內）改走公開入口帶指紋，不再 import `_prepare_draft_for_send` / `_submit_prepared_draft`；各自原有的 gate 保留。
- 全域 `bes` 是 build 快照（D-20260527-1）：不發行，其他專案的 agent 仍吃 TTY-only 的舊快照，今天的狀況原樣重演。

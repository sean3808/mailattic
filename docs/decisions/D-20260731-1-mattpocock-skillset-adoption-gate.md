---
id: D-20260731-1
date: 2026-07-31
status: accepted
title: mattpocock skillset 不採用為主工作流；解除條件為「能承載流程強制的累積式能力契約」
---

## Context

`mattpocock/skills`（196k stars，2026-07 起改為 skills.sh installer + Claude Code plugin marketplace 雙軌發行）是一套完整的工作流產品，主鏈為 `grill-with-docs → to-spec → to-tickets → implement → code-review`。本 repo 2026-07-27 起做過三輪評估：

1. **方法論層**（一份外部研究筆記，含 Codex GPT-5.6 對抗審查）—— 結論「不遷移，維持 OpenSpec/Spectra 為 canonical」，但明確指出「我方領先」的說法不成立，正確表述是「我方機制較完整，淨實效未經比較」。
2. **現地層**（一份現地實測筆記）—— 首次實際裝進本 repo 後的分工對照、文件定位圖與實測。
3. **上游證據層**（2026-07-31）—— 直接讀 `mattpocock/skills` 的 issue 與 comment。

前兩輪的否決理由多屬**程度差異**（誰做得比較好、產物落點有無 hook 綁著），這類理由會隨對方迭代而失效，因此不足以支撐一個穩定的決定。第三輪找到的才是**結構性**理由，且由 upstream 自己收斂確認。

期間已發生一次實際代價：本 repo 因 matt pack 的 `CONTEXT.md` 假設踩過一次（issue `#45`）—— `docs/agents/domain.md` 指向一個不存在的根目錄 `CONTEXT.md` 當詞彙表，且明文「不存在就靜默略過」，使 agent 繞開真正的詞彙正典 `openspec/LANGUAGE.md`。這是靜默失效，不報錯。

## Decision

**不採用 mattpocock skillset 為主工作流。** 但這不是永久否決 —— 設一道**可證偽的採用閘門**，滿足任一解除條件即重新評估。

### 閘門判準

> mattpocock skillset 必須能承載「跨 change 累積的能力契約」（系統應該有什麼行為），且該契約的更新是**流程強制、非人工紀律**。
>
> 做不到 → 不得採用為主工作流。

### 為什麼是這一條

因為這是唯一結構性而非程度性的落差。逐階段對照下，每一格都是「誰做得比較好」，只有這一格是「一邊有、一邊沒有」：

| 我方 | 對應物 |
|---|---|
| `openspec/specs/<capability>/spec.md` —— 跨 change 累積的行為契約 | **無** |
| `spectra archive` 把 delta 自動併入契約 | **無** |
| `spectra validate --strict` 結構檢查 | **無** |
| `openspec/changes/archive/` 作為變更史（本 repo 刻意無 `CHANGELOG.md`，見 [D-20260609-1](D-20260609-1-no-changelog-file.md)） | **無** |

**變更史與行為契約都掛在同一個機制上**，所以這一格失守等於同時失去兩樣東西。

### 上游證據（2026-07-31 查證）

**這不是我方推論，是 upstream 自己收斂的結論。**

- **`mattpocock/skills#212`【CLOSED，已收斂】** —— 「PRD 做完之後的生命週期」。提問者開場即點名 Spec-Anchored（Thoughtworks / Martin Fowler）把 spec 當活的治理產物，而「那似乎是**跟這套 skills 不同的哲學**」。四則回覆全數收斂到 **PRD 是拋棄式的**，長壽真相在 `CONTEXT.md` + ADR。**關鍵在最後一則**：提問者 承認收斂後仍明指缺口未解 —— **`CONTEXT.md` 是工程參考、ADR 是架構決策，兩者都不承載產品行為敘事**。其補法為 `docs/features/<domain>.md` 索引 + 加厚 issue body，並自承「最難的紀律是每個 PR 都要更新索引」、飄了得加 CI 強制。
- **`#560`【OPEN，未合併】** —— 社群提議在 closeout 前加「documentation impact gate」。一則 comment 點出實作陷阱：`CONTEXT.md` 若無 schema、只是自由散文，agent 無法可靠定位該更新處，因此需自訂 topic-header 慣例。
- **`#613`【OPEN】** —— spec↔ADR、ticket↔spec 的追溯連結都還沒有；提問者直問「一個全新的 `/implement` session 要如何可靠還原完整上游需求與決策」。
- **`#669`【OPEN，目前壞的】** —— 有人把 OpenSpec 疊在 matt 上（規則要求 `Must use grill-with-docs skill before creating proposal.md`），結果 `grill-with-docs` 有跑但產出零個檔案。疊加路線有人在試但現況不通。

### 解除條件（任一成立即重評，不需全部）

1. upstream 合入一個機制，把 change 的行為要求**累積進一份持久、結構化、可 grep 的契約**，且更新由流程強制（例如 `#560` 合併且擴充到承載行為契約，而非僅止於「影響檢查」）
2. upstream 提供 OpenSpec / Spectra 的正式互通（`#669` 修復且成為受支援路徑），使兩者可疊加而非二選一
3. Sean 明確接受「行為契約只活在關掉的 issue 裡」，並**同時**放棄 `openspec/changes/archive/` 作為變更史 —— 這是放棄兩樣東西，不是一樣

### 什麼**不算**通過（防自我說服）

- ❌ **「`CONTEXT.md` 可以當 spec」** —— 它是詞彙表。`#212` 的提問者已明確排除；`#207` 更在討論是否直接改名 `GLOSSARY.md`
- ❌ **「ADR 可以承載行為契約」** —— ADR 記的是**為什麼選這條路**，不是**系統應該做什麼**。兩者互補不可互換（`CLAUDE.md` 索引段已明文區分：行為問題查 spec、「為什麼是這條路」查 ADR）
- ❌ **「issue body 寫詳細一點就好」** —— 那是 `#212` 中提問者的補法，他自己說最難的是紀律、且可能要靠 CI 才守得住。**用人工紀律換掉機械保證是降級，不是平手**
- ❌ **「我們可以自己寫一個 promote skill」** —— 等於自建 Spectra 最核心的一塊，同時失去 `validate --strict`。若要走這條，該問的是「為什麼不繼續用 Spectra」

## Consequences

### 適用範圍

閘門管的是**整套採用**。**個別 skill 的借鑑不受限制**，仍走一般判準（產物落點有無 hook 綁著、是否與既有工具重疊）。已落地的借鑑：

- `/codex-review` 的 CONCERNS 輸出改為語意正確性／意圖合規**雙軌**—— 源自 matt `code-review` 的雙軸不重排序原則，但未採用其 `Standards`/`Spec` 軸名（那兩軸不涵蓋 security / AI-generated risks / blame-shifting，照搬會掉覆蓋）
- `/codex-review` 第 4 類補**五條具名 Fowler smell**
- GitHub triage label 字串（`ready-for-agent` / `ready-for-human` / `needs-info` / `needs-triage`）借用作**內部分流**，與 upstream 的外部回報分流用途不同

### 已知代價

- 放棄 matt 主鏈「純 skill、無本機 binary、狀態住 GitHub issue」帶來的兩個好處：**天生可上雲**、**天生免疫 git worktree 摩擦**（後者對照 `kaochenlong/spectra-app#94`：`spectra archive` 在 worktree CWD 因路徑分隔符字串比對而必死，上游開了 2.5 個月無回覆）
- 放棄其較強的訪談引擎（`grill-with-docs`）與垂直切片分解術 —— 後者已於 2026-07-28 因「本專案在維護期、前提不成立」結案關閉

### 待辦

- `docs/agents/domain.md` 仍指向不存在的根目錄 `CONTEXT.md`（issue `#45` 的成因）。本決定確立後應改指 `openspec/LANGUAGE.md`，並刪去「不存在就靜默略過」的說明
- `.pre-commit-config.yaml` 對 `skills/` 的註解與 [D-20260602-2](D-20260602-2-skill-dev-canonical-in-repo.md) 相反 → issue `#34`（與本決定無關，順記）

### 重評觸發

- upstream `#212` 被 reopen，或出現承載行為契約的新 skill
- `#560` 合併
- `#669` 修復且 OpenSpec 互通成為受支援路徑

**在以上任一條發生前，不重新評估整套採用。** 完整的分項評估、實測紀錄與逐階段對應表見上述兩份外部筆記。

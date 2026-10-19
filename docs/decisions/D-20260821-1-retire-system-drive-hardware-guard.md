---
id: D-20260821-1
date: 2026-08-21
status: accepted
title: 退役 runtime-root 守門的系統碟（硬體動機）拒絕與其豁免機制
---

## Context

`scripts/project-tool-environment.ps1` 的 `Get-ProjectRuntimeRootRejectionReason` 在同一個函式、同一個 exit code（18）底下疊了兩族動機不同的拒絕：

1. **系統碟拒絕**（2026-07-29 landing，2026-07-31 commit `919664a` 加上 subst/junction/extended-length 真實 volume 解析）：runtime root 解析後落在 `$env:SystemDrive` 就拒絕。[D-20260807-1](D-20260807-1-waivable-system-drive-gate-on-single-disk.md) 把它改為操作者可用 `MAILATTIC_ALLOW_SYSTEM_DRIVE_RUNTIME_ROOT=1` 明示豁免。
2. **volume 無法確立的 integrity refusals**（同樣是 2026-07-31 commit `919664a`，issue #46）：UNC 路徑、懸空 reparse point、任何解析失敗但非「找不到」的錯誤碼。design.md 原句："A guard that cannot see MUST NOT wave the caller through." 這族的動機是正確性，不是磁碟快慢；2026-08-07 的豁免 change 自己把它稱為 **"the integrity refusals"**，並刻意讓豁免不涵蓋它們。

系統碟拒絕唯一被記錄下來的理由（2026-07-29 proposal.md）：不讓測試/benchmark/release/agent 品質工作把系統碟塞滿、拖慢整台電腦。那是舊工作站兩顆較慢硬碟的處境。

> **2026-08-21 補記（本決定發布當日追加）**：上一句只涵蓋**守門**的理由，不涵蓋 `.project-runtime` 這個 root 本身。Sean 建立這個 root 的第一意圖是 **repo 根目錄的可讀性**（把散落 root 的一次性資料夾與 cache 收進單一目錄），系統碟隔離是 2026-07-29 才附掛上去的第二意圖。該意圖當時沒被寫進任何記錄，導致本決定發布當天就有人照記錄推出「守門退役 ⇒ 這個 root 已無存在理由」的錯誤結論。第一意圖現已收進 [`GLOSSARY.md`](../../GLOSSARY.md) 的 `runtime root（.project-runtime）` 詞條；**本決定退役的只有第二意圖，root 本身與其 leaf 結構不受影響**。

Sean 2026-08-20 拍板退役：執行環境改變後，當初設這道護欄的前提不再成立。


現行執行環境只有系統碟可用；2026-08-07 豁免 change 當時的負載量測已確認 pytest 工作負載 CPU-bound、磁碟使用率遠低於飽和，系統碟拒絕原本要防的傷害在這台硬體上不會重現。這個量測基礎在 2026-08-21 退役時未變，不需要重新量測。

代價是實的：豁免生效的每次執行都在 stderr 印一行降級警告，依舊版 `docs/agents/quality-gates.md` 明文，看到這行代表該次綠燈**不構成「隔離仍然成立」的證據**。這台機器上的每一次 Full route 全綠都必須附帶這句但書——整個 repo 在此機器上永遠產不出一次乾淨的完工證據。

## Decision

**退役系統碟拒絕與其豁免機制，volume 無法確立的 integrity refusals 原封不動保留。**

具體退役範圍：

- `Get-ProjectRuntimeRootRejectionReason` 的系統碟比較分支（`$rootDrive -eq $systemDrive` 及其豁免呼叫），與服務它的 `$env:SystemDrive` 前置檢查（`$systemDrive` 未設就 throw——只被這個分支使用，隨它一起退）。
- `Test-ProjectSystemDriveWaiver` 函式與 `$script:SystemDriveWaiverAnnounced` 狀態。
- 環境變數 `MAILATTIC_ALLOW_SYSTEM_DRIVE_RUNTIME_ROOT` 與其 stderr 降級警告。
- `docs/agents/quality-gates.md` 的「單碟工作站的豁免」整段與相關敘述；`CLAUDE.md` 對應的硬性規則；`AGENTS.md` 要求每次執行前手動設豁免變數的操作規則。
- 對應測試：直接刪除純粹斷言系統碟拒絕/豁免行為的測試；三條名字掛著 `waiver` 但斷言的其實是存活的 integrity refusal（UNC、dangling junction 的 root 與 leaf）的測試，改寫成不依賴豁免環境變數的驅動方式，斷言內容不變。

**未受影響、一行都沒動**：

- `Get-ProjectRuntimeRootRejectionReason` 的 volume-無法確立分支——UNC 路徑、懸空 reparse point、解析失敗、磁碟機根格式不符。exit code 18 對這族仍然是唯一出口。
- `scripts/invoke-pytest.ps1:406-412` 獨立的 containment re-check（逃出 artifact root），與此次退役的函式無關。
- 七個 managed leaf 的建立、process-scoped TEMP/TMP/UV_CACHE_DIR 等重導向、cross-process pytest lock——三者都在拒絕檢查通過之後才發生，動機各自獨立於磁碟快慢。
- `Assert-CoverageStagingDirectory`（`invoke-pytest.ps1:378`）重用同一個守門函式，退役後 coverage 產物路徑同樣不再受系統碟保護——這是同一個硬體動機的延伸，接受它隨之退役，不是另一個決策。

## Restoration condition

**這是硬體處境觸發的退役，不是「這個護欄從一開始就不該存在」的結論。** 哪天這台工作站的處境變回「多顆磁碟、其中至少一顆明顯較慢」，系統碟拒絕與豁免機制要被**重新評估**，不是被當作已經被否定、永遠不需要再考慮。重新評估時的起點是 [D-20260807-1](D-20260807-1-waivable-system-drive-gate-on-single-disk.md) 與本決定各自的 Context 段——那裡記錄了原始理由與量測方法，不必重新推導一遍。

## Rejected alternatives

- **在 C: 上開 VHDX / Dev Drive**：磁碟區層面的隔離而非實體隔離，`#89` 已否決過一次；本決定的結論是這層隔離在目前硬體下不需要，不是需要換一種方式達成它。
- **把 exit 18 降級成 warning、或留一個 CI-only 後門**：半退役比不退役更糟——守門的殼還在，但沒人知道它不作數了。本決定要的是連同契約敘述一起消失的退役。
- **維持現狀**：每一次 Full route 全綠都要附帶「不構成隔離證據」的但書，這個代價是實的且持續發生，維持現狀不會讓它變好。

## Consequences

- 四條 pytest route（Quick / Full / FullSequential / Targeted）不再需要任何環境變數即可在系統碟上執行。
- `docs/decisions/D-20260807-1-waivable-system-drive-gate-on-single-disk.md` 的 frontmatter 加了 `superseded_by: D-20260821-1`；其 body（原始理由與量測方法）保留不動，供日後重新評估時參考。
- `docs/decisions.md` 由既有機制重生，不手動編輯。
- 反轉成本主要在 `scripts/project-tool-environment.ps1`（重新加回系統碟比較分支與豁免函式）、對應的三份測試檔、`docs/agents/quality-gates.md`、`CLAUDE.md`、`AGENTS.md` 五處；integrity refusals 完全不用動，因為它們從未被移除。

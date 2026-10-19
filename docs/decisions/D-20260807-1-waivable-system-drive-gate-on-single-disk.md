---
id: D-20260807-1
date: 2026-08-07
status: accepted
title: 系統碟守門改為操作者可明示豁免，邊界劃在「守門推導出什麼」而非路徑怎麼拼
superseded_by: D-20260821-1
supersede_note: 2026-08-20 換機後系統水平全面拉高，硬體動機不再成立；系統碟拒絕與其豁免機制（`MAILATTIC_ALLOW_SYSTEM_DRIVE_RUNTIME_ROOT`）整條退役，見 D-20260821-1。同批引入的「volume 無法確立」integrity refusals（UNC、dangling junction）動機不同，未受影響，仍在。
---

## Context

專案的工具環境 helper 把測試與品質 gate 的 I/O 全部關進一個 ignored 的 runtime root（TEMP / TMP / TMPDIR / UV_CACHE_DIR / XDG_CACHE_HOME / PRE_COMMIT_HOME 一律重導到它底下）。其中一段守門會把 runtime root 解析成真實磁碟區，只要落在系統碟就在建立任何目錄之前 exit 18。canonical pytest launcher 是唯一的直接呼叫者，release script 與 diff-coverage reporter 都經由它繼承同一行為。

這條拒絕**只有一個被記錄下來的理由**：不讓測試、benchmark、release 檢查或 agent 產生的品質工作把系統碟塞滿而拖慢整台電腦。那個理由寫於 repo 還住在獨立資料碟的年代，當時的設計文件也確實權衡過「checkout 搬到系統碟」這個情境，選擇 fail loud 而不支援它。

**這個前提在 2026-08-05 失效**。工作站換成單碟機型，系統磁碟區佔滿整顆碟、無未配置空間；唯一的另一個磁碟區是備份專用，明文排除作為高頻小檔 churn 的落點。換機後的負載量測顯示 pytest 工作負載是 CPU-bound、磁碟使用率遠低於飽和，也就是**這條拒絕原本要防的那個具體傷害，在這個硬體上不會重現**。

後果是全面的：四條 pytest route 全部 exit 18、release script 跑不動、這個 repo 對任何 change 都產不出完工證據——包括修這件事的 change 本身。

## Decision

**改為由操作者透過行程環境變數 `MAILATTIC_ALLOW_SYSTEM_DRIVE_RUNTIME_ROOT` 明示豁免，而不是偵測單碟機器。**

- **只認 trim 後恰好等於 `1`**。空字串、空白、`0`、`false` 一律不算同意，拒絕照舊生效。非空但無法辨識的值另外印一行點名該值，因為那通常代表操作者以為豁免開著、實際上沒有。
- **邊界劃在守門推導出什麼，不在路徑怎麼拼**：volume 被解析出來、而且就是系統碟 → 可豁免，不論是直接寫、extended-length prefix、`subst` 還是 junction 到達的。volume **根本無法確立**（dangling junction、任何 UNC）→ **永不豁免**——對系統碟的宣告說明不了一個從未被確定的位置。leaf 走同一條判準、各自受檢：豁免生效時 leaf 落在系統碟同樣放行（單碟機器上每個 leaf 本來就在系統碟），但 volume 無法確立的 leaf 照樣擋。
- **豁免生效時每次執行都在 stderr 印一行降級警告**（不是只印第一次、不寫 stdout、不改 exit code）。**看到那行就代表該次綠燈不構成「隔離仍然成立」的證據**，只代表測試本身通過。
- **沒有命令列旗標，也沒有可 commit 的設定檔對應物**。它只存在於行程環境變數，所以不會從 repo 被別台機器繼承。

## Rejected alternatives

- **自動偵測「只有一顆碟就放行」**：在這個硬體上這條規則不是比較難寫，而是**答案錯的**。第二個磁碟區確實存在（那個備份用磁碟區），自動規則因此會繼續拒絕，而唯一能滿足它的做法是把測試 churn 放到備份磁碟區上——對一個以小檔建立/刪除為主的工作負載來說，這比放在系統碟上實質更糟。要讓規則答對就得把磁碟區、實體磁碟、匯流排型別與可移除性關聯起來，那是一套脆弱、綁平台、難以決定性測試的機制，而它服務的是一個人類每台機器只做一次的判斷。
- **在同一顆碟上開一個 VHDX 掛成獨立磁碟機代號**：守門解析的是邏輯磁碟區，所以這樣做確實會過關——但它**買不到守門存在的目的**，因為一個以系統碟上的檔案為後端的虛擬磁碟提供不了任何實體 I/O 隔離。採用它等於把檢查變成形式：報告成功，而它宣稱的性質是假的。附帶還多一個常駐掛載元件，失效時的表現是開機後某次測試莫名其妙失敗。
- **一次性提示而非每次警告**：一週之內就與「完全沒提示」無法區分。豁免狀態會伴隨這台機器的整個生命週期，且它靜默削弱了其他工具都預設成立的一個性質，因此代價（每次一行雜訊）換到的是**沒有任何人能在讀到一次綠燈時不知道隔離已被豁免**。

## Consequences

- 本機跑任何 pytest route 或 `release.ps1` **前必須設 `MAILATTIC_ALLOW_SYSTEM_DRIVE_RUNTIME_ROOT=1`**，否則四條全 exit 18。
- 在任何存在可用非系統磁碟區的機器上，這條拒絕**原封不動照舊生效**——包含所有間接拼法。
- **哪天這台機器有了真正的非系統磁碟區，就把這個變數清掉**，守門會自動回到原本的 fail-loud。這是 rollback 的全部內容。
- 守門變窄而非消失這件事，靠一組**在豁免明確開啟的狀態下**做的斷言釘住（subst、junction、dangling junction、UNC 四種）：日後若有人把豁免擴大到涵蓋「volume 無法確立」那一類，這些測試會變紅而不是靜默地繼續通過。
- 「工作負載是 CPU-bound」是一個時間切片的量測。日後若磁碟時間真的成為瓶頸，正確的回應是配置一個真的磁碟區並清掉這個變數，本設計把那條路以零成本保留著。
- 契約正典：[`docs/agents/quality-gates.md`](../agents/quality-gates.md) 單碟豁免段、capability spec（`openspec/specs/agent-quality-gates/spec.md`，不隨本 repo 帶出）。完整設計與實作契約：`openspec/changes/archive/2026-08-07-waive-system-drive-gate-on-single-disk/design.md`。追蹤：GitHub issue `#89`。

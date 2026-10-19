# Trigger→Resolution hot

> **本檔每個 session 都會載入**（`CLAUDE.md` → `AGENTS.md` → 本檔的 `@import`；Codex 不處理 `@` import，靠 AGENTS.md 那段的觸發條件來讀）。每格一句。完整經過（成因、補救步驟、實測細節）住 [`trigger-resolution-detail.md`](trigger-resolution-detail.md) 同一個 `PC-NNN`，一句話不夠用時才讀。
> **寫入方**：session-park 的 promote 流程 —— `hit_count >= 2` 的候選過 dedup gate 後，完整四欄 append 到 detail 檔，本檔只加精簡的一列。

| Trigger 症狀 | 適用 | 做法 | 不適用 |
|---|---|---|---|
| equivalence / parity / golden 測試在實作改成委派上游後仍全綠 | A 已直接呼叫 B，或兩邊共用同一組序列化設定 | 先確認兩側仍是獨立來源；不是就改測我方擁有的介面形狀，對照物獨立推導 → PC-079 | — |
| 手動改 `claude_desktop_config.json`，重開 app 後改動消失 | app 自己也會寫的 config 檔 | 托盤 Quit → 確認無殘留 process → 才編輯 → 重開 → PC-074 | 症狀是「改了沒生效」而非「改了又變回去」 |
| 新守門的回歸測試是綠的，但拿掉守門不會變紅 | 守門／白名單／剖析器；多條檢查共用同一種失敗表現 | 實際註解掉守門跑一次；輸入只違反被測那條；斷言錯誤訊息屬於哪一條；證明計數項必要用配對比較 → PC-096 | 純資料轉換／計算類測試 |
| `pwsh -File script.ps1` 傳參後綁錯目標 | 以 `-File` 為入口、參數超過 2 個 | 加 `[CmdletBinding(PositionalBinding=$false)]`；腳本要自己分類原始參數時改成不宣告 param、直接讀 `$args` → PC-091 | 單一參數，或以 dot-source／module 使用 |
| worktree 內跑測試噴一整批 `ModuleNotFoundError` | 繞過 launcher 直接下 `uv run pytest` | 走 `scripts\invoke-pytest.ps1`，worktree 先 `uv sync`；不要補裝套件 → PC-077 | `uv run python -m pytest` 仍缺套件才是 venv 壞 |
| 長跑命令掛背景仍在中途被砍 | 預估超過 8 分鐘的命令 | 寫一支 runner `.ps1`，以 `Start-Process` 脫離工具管轄，啟動參數整段照 detail 檔 PC-118 抄；完成只認 exit 標記檔 → PC-118 | 8 分鐘內的命令用一般背景執行 |
| MCP 工具的邊界輸入拿到的錯誤訊息不是自己寫的那句 | 參數宣告成 union 或非純 `str` | 框架先解析、函式體沒執行：改測判定 helper，保留自己規則裡的對應 clause → PC-104 | 純 `str` 參數；in-process 直接呼叫函式 |
| commit 的 `--stat` 出現不屬於這次改動的檔 | 同 repo 有第二個 writer；用了 `git add -A`／`.`；或先 stage 再跑長 gate | 列明確路徑，commit 前 `git diff --cached --name-only`；長 gate 改走 feature branch → PC-088 | 單人單 session 的 repo |
| push 到非預期分支，或刪分支時 `unknown revision` | 同 repo 有第二個 session；規劃與動作隔了數次工具呼叫 | 落點以 `git commit` 輸出為準；push 前重查分支；「列 SHA → 驗祖先 → 刪」寫成同一次呼叫 → PC-133 | 單人單 session |
| `/closeout` gate 紅，訊息含 `VIRTUAL_ENV does not match` 或「Resolved project Python does not exist」 | 失敗的測試自己會再呼叫 `uv` | 用 launcher 對同一測試取證，綠即 runner 環境 artifact → PC-168 | 失敗測試本身不呼叫 `uv` |
| `git merge-base --is-ancestor <branch> origin/main` 對每支分支都非零 | 本 repo（squash-only）判斷分支併了沒 | 看 upstream 是否 `[gone]`；或三點 diff 取檔名、再雙點比那些檔的內容 → PC-167 | 內容相同不等於可以刪 |
| 守門類改動的跨模型審查每輪都答得出「還能怎麼繞過」 | 送審的是守門類改動，審查沒拿到威脅模型 | 第 1 輪就附 [D-20260908-1](../decisions/D-20260908-1-guards-defend-confused-deputy-not-deliberate-collision.md)、該票的 principal／deputy 劃線、已接受的殘留風險、不在審查範圍的設計 → PC-204 | 判準內側找到的問題照修 |
| 請 Sean 以 `!` 代跑的指令報 `command not found` 或語法錯誤 | 交給使用者在輸入框打 `! <cmd>` 的指令 | `!` 現在走 PowerShell（2026-10-13 Sean 確認）：`$env:VAR = 'x'; cmd`、雙引號包路徑；`!` 的輸出會標示實際的 shell，對不上就換語法 → PC-177 | 自己用工具跑的命令 |
| 正式 DB 寫入、寄信、`release.ps1`、`gh pr merge` 被 auto mode 擋下 | auto mode；動作屬 real-world transaction | 直接把完整 bash 指令交給 Sean 以 `!` 跑，或請他加 permission rule → PC-210 | 唯讀 probe、dry-run |
| 測試一次冒出一整批與改動無關的 error，或以 `PermissionError [WinError 32]` 收場 | 同一個工作目錄同時有兩個 pytest（常見：背景在跑完整測試，前景又跑一次） | 背景有測試在跑時前景只做唯讀的事；等它結束再跑，並行的那兩次結果都不算數 → PC-227 | 不同工作目錄各跑各的 |
| 證明「只讀正式信庫」時，旁邊多出或改了 `-wal`／`-shm` | WAL 模式資料庫，含 `mode=ro` 唯讀開啟 | 唯讀開啟也會建出這兩個檔：看主檔大小與修改時間，`-wal` 非空才算寫入，`-shm` 不當證據；不要去刪它們 → PC-169 | 非 WAL 模式 |
| 修解碼、清洗、切分類缺陷後，原本正確的輸入被改壞 | 新增修復路徑或丟內容的邏輯 | 新路徑掛在舊路徑失敗之後；丟內容要有「判斷不出來就保留」的出口；base 與分支跑同一批形狀再 diff → PC-230 | 純新增功能 |
| 比對樣式改嚴後，審查才抓到漏掉的形狀或長輸入變慢 | 收緊 regex／選擇器判定的票 | 量「差一點就符合」的長輸入；會中變不中的清單從語法表列，不從樣本列 → PC-231 | 放寬比對 |

---
name: CI Runner Feasibility Spike
description: GitHub hosted windows runner 上 canonical pytest launcher 的實測紀錄（issue #60）
type: evidence
---

# CI runner feasibility spike（issue #60）

一次性量測，回答一個問題：**在不改動任何現有腳本的前提下，`scripts/invoke-pytest.ps1` 能否在 GitHub hosted `windows-latest` runner 上跑完？**

執行環境為拋棄式 workflow `.github/workflows/spike-runner-drive.yml`（branch `spike/ci-runner-drive-probe`，量測後連同 branch 刪除，未 merge 進 main）。

- Run: `30680874057`，commit `7f4a5bd`
- 開始 2026-08-01T02:52:49Z，結束 02:56:08Z，wall clock **3 分 19 秒**（199 s）
- 計費：`/timing` 端點在 run 剛結束時回 `total_ms: 0`（尚未彙總）。以 wall clock 推算為 4 分鐘 × Windows 2 倍乘數 ≈ **8 個計費分鐘**

## 答案

**能。** 系統碟守門（exit 18）**不會**在這個環境觸發，也**不需要** VHD、`subst` 或任何腳本改動。

`-Route Quick` 完整跑完 pytest 並回 exit **1**（7 個測試失敗），而不是 18。

## 為什麼原本擔心的 exit 18 沒有發生

| 事實 | 實測值 |
|---|---|
| workspace | `D:\a\mailattic\mailattic` |
| `$env:SystemDrive` | `C:` |
| 守門要判的 runtime root | `D:\a\mailattic\mailattic\.project-runtime` |
| root drive | `D:\` |
| `RUNNER_TEMP` | `D:\a\_temp` |
| `RUNNER_TOOL_CACHE` | `C:\hostedtoolcache\windows` |
| ImageOS | `win25-vs2026`，ImageVersion `20260714.173.1` |
| OS | Microsoft Windows Server 2025 Datacenter 10.0.26100 |
| 行程權限 | Administrator = `True` |

runtime root 落在 `D:`，不等於 `$env:SystemDrive`（`C:`），`Get-ProjectRuntimeRootRejectionReason` 直接放行。

**上游公告的適用範圍被誤讀了。** [actions/runner-images#12416](https://github.com/actions/runner-images/issues/12416) 的措辭是「all GitHub-hosted runners in **public repositories** using the Windows Server 2025 image will no longer have a D:/ drive」——當時 repo 為非公開，D: 槽仍在。稽核當時把它當成可能適用是保守假設，實測推翻。

> ⚠️ **下一段已於 2026-08-21 失效**，見 [D-20260821-1](decisions/D-20260821-1-retire-system-drive-hardware-guard.md)：系統碟拒絕整條已退役，CI runner 沒有 D: 槽不再會讓守門擋下任何東西，下面那個 VHDX fallback 也因此沒有適用情境（本 repo 明文不採 VHDX 路線）。原文保留為當時的實測紀錄。

**這項結論有保存期限。** 它取決於 GitHub 的映像磁碟配置，不是契約。若日後非公開 repo 也移除 D:，症狀會是 CI 突然固定 exit 18 且 stderr 首行為 `[project-tool-environment] Unsafe runtime root, it resolves onto the system drive`。屆時的正解是在 runner 上掛一顆真實的第二 volume（VHDX + `diskpart`，`subst` 無效，守門解真實 volume），再傳 `-RuntimeRoot`，而**不是**在守門上開 CI 例外。spike 的 branch 已刪除，所以那條 fallback 的 `diskpart` script 記在這裡：

```
create vdisk file="%RUNNER_TEMP%\ci-runtime.vhdx" maximum=8192 type=expandable
select vdisk file="%RUNNER_TEMP%\ci-runtime.vhdx"
attach vdisk
create partition primary
format fs=ntfs quick label=cirt
assign letter=X
```

存成檔案後 `diskpart /s <file>`，再 `pwsh -NoProfile -File scripts/invoke-pytest.ps1 -Route Full -RuntimeRoot 'X:\project-runtime'`。runner 行程已是 Administrator（本次實測確認），`diskpart` 可直接執行。

## uv cache 沒有衝突

`astral-sh/setup-uv@v9.0.0` 自己偵測到 tracked 設定並讓路：

```
Found cache-dir in D:\a\...\uv.toml: .project-runtime/uv-cache
Using cache-dir from uv config file, not modifying UV_CACHE_DIR
```

`UV_CACHE_DIR` 在 job 內為空字串，`uv.toml` 的 `cache-dir = ".project-runtime/uv-cache"` 生效。**不需要顯式覆寫。** uv 版本 `0.12.1`。

## 工作區乾淨

全部 probe 跑完後 `git status --porcelain` 為 0 行。launcher 與測試沒有留下未忽略的殘留。

## 測試結果：7 failed / 1050 passed / 116 subtests，149.64 s

7 個失敗全部是**本機環境與 runner 環境的差異**，不是這個 commit 的缺陷。分成四類，鑑別力差異很大：

### 1. 缺少 `readpst.exe`（4 個測試，環境依賴）

`tests/test_bootstrap.py` ×3、`tests/test_rebuild.py` ×1，全部 `ParserUnavailableError`。

值得注意的是**測試本身傳了假路徑**：

```python
parser = ReadpstParser(readpst_path=Path("C:/tools/readpst.exe"), ...)
```

`C:/tools/readpst.exe` 在開發機上也不存在。它之所以在本機通過，是因為 `resolve_readpst_path` 往下 fall through 到 repo default `.tools/libpst/tool/bin/readpst.exe` —— 而 `.tools/` 是 gitignored 的 vendored binary。

**這幾個測試想驗的是 `-j` flag 的組法，卻默默依賴一顆不在版控裡的外部執行檔。** 本機永遠看不到這件事。

### 2. `.xlsx` MIME 型別（1 個測試，宿主 registry 依賴）

`tests/test_outbound_draft_attach.py::test_multiple_attachments_all_present_with_correct_mime_types`

```
- application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
+ application/octet-stream
```

Windows 上 `mimetypes` 會讀 registry 的副檔名→Content Type 對應。開發機裝了 Office 所以 `.xlsx` 解得出來；Server 2025 runner 沒有，退回 `application/octet-stream`。

**這代表 outbound 附件的 MIME 判定依賴收件人看不到的本機軟體安裝狀態。** 同一份草稿在不同機器上會產生不同的 `Content-Type`。

### 3. Python 3.13 patch 版之間的 stdlib 行為變動（1 個測試，**最值得追**）

`tests/test_parser_semicolon_recipients.py::test_raw_bytes_none_returns_prior_parse_without_raising`

該測試鎖定「沒有 raw bytes 可回退時，維持既有的截斷行為：只留第一個位址」。

| Python | `email.policy.default` 解析 `a@x.com; b@y.com; c@z.com` |
|---|---|
| 3.13.12（本機，實測） | `(Address(username='a', domain='x.com'),)` —— 1 筆 |
| 3.13.14（runner，實測） | 2 筆，第二筆是 `{'email': None, 'name': None}` |

`.python-version` 只寫 `3.13`，所以 uv 在 runner 上取到 3.13.14。

**這不是 CI 的問題，是真實的可攜性風險**：`fix-semicolon-recipient-truncation` 這條能力建立在一個未文件化的 stdlib 解析怪癖上，而那個怪癖在 patch 版之間變了。Sean 下次升 Python 時，同一段程式碼的 fallback 行為就會位移，且可能讓 `{'email': None}` 這種空收件人進入 ingest 路徑。**需要獨立追查，優先度高於 CI 本身。**

### 4. 鎖競爭測試的時序敏感（1 個測試，疑似 flaky）

`tests/test_invoke_pytest.py::test_second_concurrent_launch_exits_17_without_invoking_uv_or_mutating_output`

行程 B 預期回 17（鎖被佔），實測回 0、stdout/stderr 皆空。

⚠️ 以下是**單次觀測的初步推論，未經第二次執行驗證**：測試讓行程 A 的 stub uv `STUB_UV_SLEEP_SECONDS="3"`，並在啟動 B 之前先確認 `proc_a.poll() is None`。在 hosted runner 上 `pwsh` 冷啟動可能超過 3 秒，於是 A 在 B 真正搶鎖之前就已釋放，B 順利取得鎖並跑完 stub uv → exit 0。若成立，這是測試對機器速度的假設過緊，不是鎖本身的缺陷。**需要重跑 2–3 次確認是否穩定重現，才能下結論。**

## 對 CI 設計的影響

1. **`#62`（把 launcher 掛進 CI）的封鎖理由已解除** —— 但先決條件從「能不能跑」變成「先把上面四類環境依賴處理掉」。在那之前把 pytest 設成 required check，會得到一個永遠紅的閘門。
2. **`#61`（最小 CI）不受任何影響**，可照原設計進行。
3. Full route 的耗時可從這次的 Quick（149.64 s）估算：Quick 與 Full 只差 `test_mcp_server.py` 的 `slow` 節點與 `test_tag_based_release.py` 的 `release` 節點，預期落在 3–5 分鐘，單次 CI run 約 12–16 個計費分鐘。
4. **這次 spike 本身已經證明了 CI 的核心論點**：一次 3 分鐘的乾淨環境執行，找出四類本機執行**在定義上不可能發現**的宿主依賴。這是 CI 唯一無法被本機 gate 取代的價值，也是它值得做的理由。

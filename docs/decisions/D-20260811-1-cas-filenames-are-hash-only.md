---
id: D-20260811-1
date: 2026-08-11
status: accepted
title: CAS 檔名只用 SHA-256，不帶來源副檔名
---

## Context

附件 store 原本把實體檔命名為 `{sha256}{原始副檔名}`。這讓同一份 bytes 在不同 ingest 路徑取得不同檔名時，落成多個物理物件：有檔名時可能是 `.pdf` / `.docx`，PST TNEF 或 IMAP 缺 `filename` 時則落成 `.bin`。副檔名是某次投遞的 instance metadata，不是內容屬性，因此 `(sha256, suffix)` 不是內容定址。

2026-08-07 全掃 `attachments/` 的數萬個檔案（數十 GB）發現：

- 多組 hash 有多個 suffix；
- 多出大量重複實體檔，浪費數 GB；
- 其中絕大多數是 `.bin` 加一個真副檔名，`.bin` 側占絕大部分空間。

`verify_cas()` 以 `file_path.stem` 對 payload digest；兩個 suffix variant 的 stem 都相同且都驗得過，所以完整性檢查不會揭露這種重複。GitHub issue #90 於 2026-08-11 拍板採 Option A。

## Decision

**CAS 實體檔名只用 lower-case SHA-256 hex digest，不帶副檔名。** 路徑固定為：

```text
attachments/<digest[0:2]>/<digest[2:4]>/<digest>
```

`attachment_name` 與 `mime_type` 留在 `message_attachments`，負責 presentation semantics：

- export 優先保留 `attachment_name` 的 usable suffix；
- 檔名沒有 suffix 時由 `mime_type` 推斷；
- 兩者都推不出來時固定 `.bin`；
- `--ext` 同樣比對檔名 suffix 或 MIME metadata，不從 `storage_path` 猜。

因此 storage identity 與呈現檔名分離：bytes 決定 CAS path，row metadata 決定匯出副檔名。

## Migration transaction boundary

SQLite 與 NTFS 沒有共同的原子 commit primitive，遷移以這條不變量取代不存在的跨資源 transaction：

> 每一個非 NULL `message_attachments.storage_path`，在任意中斷點都必須指向存在且 payload digest 等於 row `content_hash` 的檔案。

confirmed migration 的順序固定為：

1. 取得 SQLite immediate write transaction，重新掃描並 fail-closed 驗證所有 row / path / filename digest / payload digest。
2. 保留全部 legacy path，先 materialize 並驗證 digest-only canonical path；同一 NTFS volume 優先 hard link，不支援時才 copy-to-temp + atomic replace。
3. 在同一 SQLite transaction 改完全部 `storage_path`，確認所有 canonical target 存在且 hash 正確後 commit。
4. commit 後才逐一刪 legacy suffix path。

commit 前失敗時 rows 仍指向未刪的 legacy files；commit 後 cleanup 失敗時 rows 已指向 canonical files，只會多留可安全重試的 redundant files。腳本預設 dry-run，必須同時顯式給 `--database`、`--attachment-root`，並加 `--confirm` 才寫入。

## Rejected alternatives

- **Option B：只清目前重複檔、繼續用 suffix-bearing key**：一樣必須改寫全部 `storage_path`，卻沒有修掉成因；未來仍會長回來，等於為同一資料做兩次不可逆遷移，因此否決。
- **Option C：接受重複檔的浪費**：目前比例雖不高，但浪費會隨新 ingest 無上限累積，而且 CAS identity 本身仍錯，因此否決。
- **先做 Option B 當暫時方案**：與 Option B 同樣造成重複遷移，明確禁止。
- **從 magic bytes 重新偵測輸出副檔名**：會新增第二套 MIME policy 與依賴；DB 已有 `attachment_name` / `mime_type`，不採。
- **先 rename/delete legacy，再更新 DB**：任何中斷都可能留下 dangling rows，違反 migration invariant，不採。

## Consequences

- 相同 bytes 不論原始檔名、suffix 或是否缺檔名，只會有一個 canonical physical file。
- `verify_cas()` 不必修改：digest-only filename 的 `stem` 仍等於 digest；若需要改 verifier 才能過，代表遷移形狀錯誤。
- `storage_path` 不再提供副檔名訊號。任何查詢或 export 若需要 extension，只能依賴 row metadata；metadata 全失時明確落 `.bin` 或無法命中 `--ext`。
- 遷移完整 preflight 會讀取並 hash 整個 attachment store，I/O 成本高，但這是證明 bytes 未被錯配的必要 gate。
- interrupted run 可能留下安全的多餘 hard link / copy；重跑同一 confirmed command 會完成 row switch 或 cleanup，成功後再次執行為 no-op。

## Production execution boundary

本 change 只產出腳本並在 `.project-runtime/pytest` 的合成 DB / attachment fixture 上驗證，**不得由 agent 對 repo 根的 canonical DB或正式 `attachments/` 執行**。

正式 R5 人工作業順序：

1. `bes db backup` 建一致性 DB snapshot，另確認 attachment store 有可復原副本；
2. 對 explicit canonical paths 跑 dry-run，人工核對每個 delete path、row/file reconciliation difference 都是 0；
3. maintenance window 內加 `--confirm`；
4. 重跑 dry-run，確認 rows-to-rewrite = 0、legacy delete = 0、multi-path hash groups = 0；
5. 跑 `bes db verify-cas`；
6. 隨機抽 20 筆，用 `bes attachments --copy-to` 實際取檔並核對 metadata-derived extension。

若要 rollback，必須一起使用遷移前 DB snapshot 與 attachment store 可復原副本；不可只還原舊 DB，讓 suffix-bearing rows 指向已刪除的路徑。

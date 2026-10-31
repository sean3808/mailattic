# DB schema

欄位設計記憶；行數與最新 cutoff 用 SQL 查（`SELECT MAX(sent_at) FROM messages`），不記在這裡。

| Table | 重點 |
|---|---|
| `messages` | 核心 `subject` / `sender_*` / `sent_at` / `received_at` / `body_text`；thread `message_id` / `in_reply_to` / `references_header`；traceability `pst_source` / `folder_path` / `logical_locator`；discovery audit `source_input_mode` / `source_requested_from`；dedup `dedup_key` / `dedup_basis`；檢索 `recipient_search_text` / `attachment_search_text`；`thread_key`（indexed，connected-component 最小 `db_id`，`bes thread` 靠它單一等值查詢；**derivable cache**，`scripts/backfill_thread_key.py` 可重建；Message-ID 身分一律經 `query.normalize_message_id`：先去外側空白、再去角括號，大小寫不動，原始 header 不改寫；舊 cache 對 scratch DB 重跑 backfill 即修復，正式 DB 只有 Sean 親跑）；`thread_bridge_target`（inferred bridge 的 target **stable logical id**：`message_id` 或 `dedup_key`，不是 raw `messages.id`）／`thread_bridge_source`（只允許 `subject_heuristic` 或 NULL） |
| `message_recipients` | `recipient_type` ∈ {to, cc, bcc}, `email`, `display_name` |
| `message_attachments` | 附件**檔名**索引；未做 OCR / 內文抽取 |
| `imap_sync_progress` | IMAP 增量同步水位（per account/folder/uidvalidity 的 last UID），與信件同一個 commit、隨 DB 備份/還原；`.imap_watermarks.json` 只是鏡像 |
| `message_fts` (FTS5) | 主旨 / 本文 / 收件人檢索欄 / 附件名 |
| `ingest_failures` | 解析失敗的 raw record，含 stage 標記供事後重 ingest。附件子失敗（`attachment-store` / `attachment-decode`）另帶 `attachment_name` / `attachment_size`（解析當下抽出的位元組數，供補回時核對），並用 **`resolved_at`** 表示「已解決」：`NULL` ＝ 仍開著（每次 `sync-imap` 開頭會用還在本機的原始信重試）；有時間戳 ＝ 已解決，有三種來源：(1) 已用原始信補回（`#255`）；(2) **已放棄**（`#256`）＝人工下 `bes db abandon-attachment-failure`：同一列 `resolved_at` 設為放棄當下、`abandon_reason` 記理由（「已放棄」就是 `abandon_reason IS NOT NULL`，系統從不自己寫 `abandon_reason`）；(3) **自癒（Self-healed，`#31`）**：訊息層級失敗（`error_stage` 非 `attachment-*`）其相同來源與位置（`pst_source`、`logical_locator` 及 `folder_path`）之訊息已在失敗記錄之後成功入庫，由 IMAP sync 等路徑的 sweep 自動設定 `resolved_at` 為當前時間戳（`abandon_reason` 保持 NULL；自癒非放棄）。`resolved_at` 有值讓所有既有的 `resolved_at IS NULL` 過濾自動略過它（不重試、不計數）。舊資料庫在 `initialize_database` 時原地補欄位，舊列的 `attachment_name` / `attachment_size` / `resolved_at` / `abandon_reason` 皆 `NULL`（沒有 `attachment_name` 的舊列不重試，但仍算未解決、可被放棄） |

**thread-bridge 只寫 `thread_bridge_*` 兩欄，真實 header 保持原樣**：`scripts/bridge_orphan_outbound.py` dry-run 先列 evidence，confirmed mode 才寫並重算 `thread_key`；清掉兩欄重跑 `backfill_thread_key.py` 即回到 header-only assignment。

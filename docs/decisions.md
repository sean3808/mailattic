# Decision Log

<!-- BEGIN GENERATED ADR-INDEX -->
| ID | Date | Title | Status |
| --- | --- | --- | --- |
| [D-20260525-1](decisions/D-20260525-1-no-wsl-write-to-mnt-drive.md) | 2026-05-25 | WSL 內禁止用 Linux 工具直寫 /mnt/<drive>，改走 robocopy 或 tar bundle | accepted |
| [D-20260527-1](decisions/D-20260527-1-global-bes-is-build-snapshot.md) | 2026-05-27 | 全域 bes 裝成 non-editable build 快照，與開發 working tree 解耦 | accepted |
| [D-20260528-1](decisions/D-20260528-1-bes-never-generates-content.md) | 2026-05-28 | bes 不接 LLM，outbound 只做機械步驟，內文一律由 caller 提供 | accepted |
| [D-20260528-2](decisions/D-20260528-2-outbound-visibility-via-bcc-self.md) | 2026-05-28 | bes 寄出的信靠 BCC 寄件人自己讓 Outlook 看得到，不走 IMAP APPEND *(partially superseded by `D-20260714-2`: POP3 前提已於 2026-07-14 失效（Outlook 改走 IMAP），BCC-self 機制仍在跑，待 APPEND-to-Sent 取代，見 GitHub issue #21)* | accepted |
| [D-20260602-1](decisions/D-20260602-1-native-windows-daily-sync.md) | 2026-06-02 | 每日 IMAP 同步走 Windows-native Python，不 spawn container 或 WSL shell | accepted |
| [D-20260602-2](decisions/D-20260602-2-skill-dev-canonical-in-repo.md) | 2026-06-02 | skill 的 dev-canonical 放本 repo，SSOT 只是下游 release copy | accepted |
| [D-20260602-3](decisions/D-20260602-3-archive-scrape-as-outbound-source.md) | 2026-06-02 | ~~用 archive-scrape 定期抓 server archive 當作 outbound 郵件來源~~ | ~~superseded~~ |
| [D-20260603-1](decisions/D-20260603-1-no-task-or-commitment-tracking.md) | 2026-06-03 | 不做承諾偵測 / 未結案 thread 追蹤，那是任務管理工具的職責 | accepted |
| [D-20260603-2](decisions/D-20260603-2-db-backup-is-staging-not-dr.md) | 2026-06-03 | bes db backup 產的是 staging 快照，不是 DR 終點 | accepted |
| [D-20260609-1](decisions/D-20260609-1-no-changelog-file.md) | 2026-06-09 | 不維護 CHANGELOG.md，變更史以 openspec archive 為正典 | accepted |
| [D-20260703-1](decisions/D-20260703-1-maildir-is-7day-scratch.md) | 2026-07-03 | .mailattic-work/maildir/ 是 7 天暫存跳板，不是 raw archive | accepted |
| [D-20260709-1](decisions/D-20260709-1-sync-fails-loud-on-ingest-failure.md) | 2026-07-09 | sync 對 message-level ingest 失敗 fail-loud，停在該 UID 不推 watermark | accepted |
| [D-20260714-1](decisions/D-20260714-1-imap-sent-is-sole-outbound-source.md) | 2026-07-14 | IMAP Sent folder 是 outbound 唯一常規來源，archive-scrape 降為手動備援 | accepted |
| [D-20260714-2](decisions/D-20260714-2-chinese-sent-folder-of-record.md) | 2026-07-14 | 接受中文 寄件備份 為 Sent folder of record，bes 自己處理 mUTF7 編解碼 | accepted |
| [D-20260721-1](decisions/D-20260721-1-revisit-mcp-for-cowork.md) | 2026-07-21 | 為 CoWork 重評 no-MCP 立場（限定唯讀 read-tier 例外；Spike A 已 GO） | accepted |
| [D-20260723-1](decisions/D-20260723-1-taipei-day-date-filter-semantics.md) | 2026-07-23 | Date filter bound 語意統一為 Asia/Taipei 整天（inclusive），不統一各介面的輸入語法 | accepted |
| [D-20260724-1](decisions/D-20260724-1-recipient-domain-boundary-match-option-a.md) | 2026-07-24 | 收件人網域比對走查詢層邊界函式（Option A），資料正規化（Option B）延後 | accepted |
| [D-20260728-1](decisions/D-20260728-1-mcp-server-tracks-working-tree.md) | 2026-07-28 | MCP server 暫不做發行版、跟隨 working tree；開發結案時改為 build 快照 | accepted |
| [D-20260730-1](decisions/D-20260730-1-mcp-capability-parity-and-v2-first-sequencing.md) | 2026-07-30 | MCP 能力終態與 skill 三 capability 對等；技術順序由重構暴露度決定，SDK v2 先行 | accepted |
| [D-20260731-1](decisions/D-20260731-1-mattpocock-skillset-adoption-gate.md) | 2026-07-31 | mattpocock skillset 不採用為主工作流；解除條件為「能承載流程強制的累積式能力契約」 | accepted |
| [D-20260807-1](decisions/D-20260807-1-waivable-system-drive-gate-on-single-disk.md) | 2026-08-07 | 系統碟守門改為操作者可明示豁免，邊界劃在「守門推導出什麼」而非路徑怎麼拼 *(partially superseded by `D-20260821-1`: 2026-08-20 換機後系統水平全面拉高，硬體動機不再成立；系統碟拒絕與其豁免機制（`MAILATTIC_ALLOW_SYSTEM_DRIVE_RUNTIME_ROOT`）整條退役，見 D-20260821-1。同批引入的「volume 無法確立」integrity refusals（UNC、dangling junction）動機不同，未受影響，仍在。)* | accepted |
| [D-20260811-1](decisions/D-20260811-1-cas-filenames-are-hash-only.md) | 2026-08-11 | CAS 檔名只用 SHA-256，不帶來源副檔名 | accepted |
| [D-20260812-1](decisions/D-20260812-1-keep-squash-only-merge.md) | 2026-08-12 | 維持 squash-only merge，理由不是歷史整潔而是多輪 PR 的 churn 量體 | accepted |
| [D-20260813-1](decisions/D-20260813-1-design-doc-prs-get-no-follow-up-disposition.md) | 2026-08-13 | 設計文件型 PR 不給「先併、開 follow-up 票之後修」這個處置 | accepted |
| [D-20260813-2](decisions/D-20260813-2-open-source-is-showcase-not-product.md) | 2026-08-13 | 開源的定位是展示成果，不是可用產品——它授權邊界工作，不授權跨平台支出 | accepted |
| [D-20260820-1](decisions/D-20260820-1-review-trigger-is-change-size-not-pr.md) | 2026-08-20 | 語意 review 的觸發條件是變更 size，不是走不走 PR | accepted |
| [D-20260820-2](decisions/D-20260820-2-excluded-ci-infrastructure-options.md) | 2026-08-20 | 三個 CI 基礎設施選項明文排除，「Free 方案」是所有 CI 決策的固定前提 | accepted |
| [D-20260820-3](decisions/D-20260820-3-migrate-to-contract-anchored-flowback.md) | 2026-08-20 | 退出 Spectra 鏈，遷移 Contract-Anchored Flow-Back | accepted |
| [D-20260820-4](decisions/D-20260820-4-skill-ssot-deploy-trigger-main-commit.md) | 2026-08-20 | skill→SSOT 自動部署的觸發改為「main commit + 內容不一致」 | accepted |
| [D-20260821-1](decisions/D-20260821-1-retire-system-drive-hardware-guard.md) | 2026-08-21 | 退役 runtime-root 守門的系統碟（硬體動機）拒絕與其豁免機制 | accepted |
| [D-20260821-2](decisions/D-20260821-2-runtime-root-override-does-not-move-pytest-cache.md) | 2026-08-21 | -RuntimeRoot 覆寫不搬 pytest cache —— 記為已知邊界而非修復 | accepted |
| [D-20260826-1](decisions/D-20260826-1-keyword-search-orders-by-relevance.md) | 2026-08-26 | 帶 keyword 的搜尋改用相關性排序，不再一律時間新到舊 | accepted |
| [D-20260827-1](decisions/D-20260827-1-maildir-import-reconciles-rather-than-predicts.md) | 2026-08-27 | maildir 匯入的漏信偵測用對帳，不用預測；讀信與對帳共用同一份走訪 | accepted |
| [D-20260903-1](decisions/D-20260903-1-recipient-rebuild-extraction-on-windows-outlook.md) | 2026-09-03 | 收件人 rebuild 的抽取走 Windows 上的 Outlook COM，stage 落 Windows 本機；issue #175 的 WSL/ext4 data plane 邊界改寫 | accepted |
| [D-20260907-1](decisions/D-20260907-1-com-timestamps-are-relabelled-at-the-read-site.md) | 2026-09-07 | Outlook COM 時間戳在讀取點 relabel 成台北，不改讀 MAPI 的 UTC 屬性 | accepted |
| [D-20260908-1](decisions/D-20260908-1-guards-defend-confused-deputy-not-deliberate-collision.md) | 2026-09-08 | 守門防的是 confused deputy，不防受益人刻意構造的碰撞；判準是「要讓它失效，需不需要 principal 違背自己的意圖」 | accepted |
| [D-20260914-1](decisions/D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md) | 2026-09-14 | 寄信授權在對話層、工具層以草稿指紋保證「寄出的＝看過的」；推翻「拒絕任何非互動送信旁路」 | accepted |
| [D-20260915-1](decisions/D-20260915-1-smtp-settings-file-resolved-by-bes-with-from-shown-at-review.md) | 2026-09-15 | 寄件設定檔路徑由 bes 解析（flag > 環境變數 > 內建預設），寄件帳號改在呈審時露出；推翻 #113「不得預設指向 .env.sean3808」 | accepted |
| [D-20260924-1](decisions/D-20260924-1-mcp-send-binds-reviewed-draft.md) | 2026-09-24 | MCP 寄信能力（mail_draft_reply + mail_send）採對話層指紋授權、草稿儲存物理隔離與顯式設定檔 | accepted |
| [D-20260930-1](decisions/D-20260930-1-missing-imap-keys-do-not-block-send.md) | 2026-09-30 | 寄件設定缺 IMAP 欄位不擋寄信；寄件備份遺漏由 BCC-self 兜底 | accepted |
| [D-20261010-1](decisions/D-20261010-1-showcase-ships-ahead-of-remoteization-in-a-fresh-repo.md) | 2026-10-10 | 展示提前、與遠端化脫鉤——在全新的公開 repo 以 mailattic 之名上線，舊歷史不帶 | accepted |
| [D-20261013-1](decisions/D-20261013-1-mcp-draft-from-follows-smtp-account.md) | 2026-10-13 | 擬稿的寄件人跟著寄件設定的帳號走，程式不再內建擁有者地址 | accepted |
| [D-20261018-1](decisions/D-20261018-1-internal-working-notes-stay-local.md) | 2026-10-18 | 內部工作筆記只留本機，不進版控、不隨公開版帶出 | accepted |
| [D-20261018-2](decisions/D-20261018-2-public-tree-scrub-criteria-and-accepted-residuals.md) | 2026-10-18 | 公開樹的去識別判準：先問誰會驗這個值，量級與作息資訊列為已接受殘留 | accepted |
<!-- END GENERATED ADR-INDEX -->

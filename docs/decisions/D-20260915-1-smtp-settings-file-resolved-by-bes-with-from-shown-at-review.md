---
id: D-20260915-1
date: 2026-09-15
status: accepted
title: 寄件設定檔路徑由 bes 解析（flag > 環境變數 > 內建預設），寄件帳號改在呈審時露出；推翻 #113「不得預設指向 .env.sean3808」
---

## Context

`#113`（2026-08-12）拿掉了 outbound 三命令 `--env-file` 的錯誤預設（當時指向 archive-scrape 憑證檔，走預設必炸），改成必填，並在「不做」清單寫明：**不得把預設改成指向 `.env.sean3808`**。理由是「送信用哪個帳號」不該變成看不見的預設，因為收件端看得到寄件人。

2026-09-15 `#195` 真寄驗收時，agent 找不到寄件設定檔路徑，卡了一輪。這個路徑沒有寫在任何正典位置，只記在另一個 skill 裡，mailattic skill 只說「先取得路徑」。每個呼叫方都得各自記住、各自抄一份。

當天查證：DB 路徑採用同樣的三層解析（`--database` > `MAILATTIC_DB` > absolute default），而 `MAILATTIC_DB` 在本機使用者層與系統層都**沒有設定**。DB 能用，靠的是內建預設。

## Decision

寄件設定檔路徑比照 DB 路徑，由 bes 依序解析（`outbound.resolve_smtp_env_file`）：

1. `--env-file` flag
2. `MAILATTIC_SMTP_ENV_FILE`（空字串或全空白視為 unset）
3. 內建 absolute default：repo 根的 `.env.sean3808`，固定路徑，呼叫端 cwd 不影響結果

解析出的檔案不存在時，在任何 DB 或網路 I/O 之前 exit 2，訊息列出三條出路。`MAIL_SERVER_ENV_PATH`（archive-scrape 憑證）仍然不作為 SMTP 設定來源，`#113` 這一半維持不變。

**寄件帳號在呈審時可見**：`draft-reply` 的 stdout 在 `Subject:` 之前印出 `From:`，`send-draft` / `send-batch` 的草稿摘要也印出 `From:`。值取自草稿 `.eml` 實際寫入的 From header，受指紋保護。

## 為什麼推翻 #113 的那一條

`#113` 要守的是「寄件帳號必須被看見」，這個目的保留，改變的是守在哪一步：

- 原本守在**打指令**：每次都要明寫 `--env-file`。但路徑本身不等於帳號，看到 `.env.sean3808` 這個檔名，並不等於確認了寄件人。
- 現在守在**呈審**：呈審是寄出前的必經步驟（契約 #2、skill capability C），`From:` 就在使用者說「寄」之前露出，而且印的是草稿真正要寄出的值。

這與 git（`user.email` 預設加上 commit 紀錄）、kubectl（current context）、郵件客戶端（撰寫視窗的寄件人欄）的常規一致：身分可以有預設值，但在不可逆動作之前讓人看得到。

(B) 是 (A) 的前提，兩者必須同時成立，不能只做內建預設。

## Rejected alternatives

- **只加環境變數、不加內建預設**：使用者得先學會設定使用者層環境變數並重開工具才用得到。單機、單一寄件帳號的情況下，這只是多一件會被忘記的隱形設定。
- **維持必填、只把路徑寫進 skill**：路徑是環境事實，寫進文件等於多一份會過期的抄本，而且每個呼叫方仍要各自抄一次。
- **具名帳號（profile）機制**：目前只有一個寄件帳號。出現第二個帳號時再開票。

## Consequences

- 單機單帳號的日常使用，不必再帶 `--env-file`。換機器或要改用別的帳號時，用 flag 或環境變數覆寫。
- 呈審清單多一欄 `From:`，skill capability C 同步更新。
- `#189`（MCP 寄信）原規劃「帳密檔路徑綁 bes-mcp 的 `env`」，應重用 `resolve_smtp_env_file` 與同一個環境變數名稱。
- 全域 `bes` 是 build 快照（D-20260527-1）：必須發行，其他專案才會生效。

---
id: D-20260709-1
date: 2026-07-09
status: accepted
title: sync 對 message-level ingest 失敗 fail-loud，停在該 UID 不推 watermark
---

## Context

GitHub issue #13：一批信寫進了 maildir，但 ingest 階段靜默失敗、`messages` 沒產生 row，watermark 卻照樣往前推。結果是**漏信**，而且沒有任何訊號 —— sync 每天回報成功。

這是最糟的失敗模式：資料遺失 + 系統宣稱健康。

## Decision

某 UID 的信件**本體**無法入庫（`parse-maildir` / `normalize-message` / `persist-message` 任一階段失敗、`messages` 沒有該 row）時，sync **故意停在該 UID**：

- watermark 不越過
- 失敗記進 `ingest_failures`
- run 回 exit 非零
- 下次 sync 從同一 UID 重試

**判準是「message row 有沒有 persist」，不是「有沒有任何 failure」。** 附件解碼 / 儲存的子失敗**不**觸發停止 —— 信件本體仍入庫、watermark 照推、子失敗另記 `ingest_failures`。

## Rejected alternatives

- **記 log 繼續跑**：就是 issue #13 的行為。漏信無聲。
- **任何 failure 都停**：附件解碼失敗（例如某個壞掉的 MIME part）會讓整個 folder 卡死，但信件本文其實已經好好入庫了。停損的對象錯了。

## Consequences

- **刻意取捨**：永久無法解析的 poison message 會持續卡住該 folder，直到人工介入（修根因或人工推進 watermark）。接受這個代價，因為替代方案是無聲漏信。
- 設計正典：`openspec/changes/archive/2026-07-09-harden-native-sync-ingest-accounting/`。

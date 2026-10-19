---
id: D-20260907-1
date: 2026-09-07
status: accepted
title: Outlook COM 時間戳在讀取點 relabel 成台北，不改讀 MAPI 的 UTC 屬性
---

## Context

Outlook COM 回傳讀取主機的本地牆上時鐘，`pywintypes` 卻替它蓋上 UTC 標籤，於是經 `OutlookComReader` 匯入的每封信 `sent_at` / `received_at` 都錯 8 小時。缺陷本體、量化證據與驗收在 GitHub issue `#183`；本檔只記路線選擇。

兩條修法都能得到正確的瞬間，差別在**依賴什麼**：

- **(A) 在讀取點 relabel** —— 把 COM 給的讀數重新標成 `datetime_bounds.TAIPEI`。依賴「抽取主機在台北」這個假設。
- **(B) 改讀 MAPI `PR_CLIENT_SUBMIT_TIME`**（`0x00390040`）—— 該屬性原生就是 UTC，不需要任何時區假設。現場探測實測它與正式 DB 的 readpst 值逐秒相符。

## Decision

Sean 於 2026-09-07 拍板走 **(A)**，(B) 留作後續強化另開票。

修正住 `outlook_pst._com_wall_clock_to_aware`，套用於 `SentOn` 與 `ReceivedTime` 兩個讀取點——全 repo 只有這兩個 COM 日期讀取。

**時區硬編為 `Asia/Taipei` 是刻意的**，不改成推導主機時區：`OutlookComReader` 按設計只能對「掛在執行中 Outlook profile 裡的 store」工作，本來就是單機工具；推導主機時區會把一個可 grep 的常數換成對 OS 地區設定的隱性依賴，設定漂移時沒有任何測試會紅。

## Rejected

**(B) 改讀 `PR_CLIENT_SUBMIT_TIME`**，儘管它在本輪被實測驗證過，且不需時區假設。三個理由：

1. `received_at` 的對應屬性 `PR_MESSAGE_DELIVERY_TIME`（`0x0E060040`）**未經驗證**。同一個 commit 裡放一個驗過的與一個沒驗過的改動，等於用前者的信心替後者背書。
2. 兩個屬性都**不保證存在**於草稿與本機自建項目（沒有 transport submit 事件）。所以 (B) 不取代 (A)，只會疊在 (A) 上面，再加一個「何時用哪個」的判斷。
3. (A) 當下就要用來解掉 `#175` 的 dry-run 阻塞。

**「naive 才補時區」的寫法**（例如沿用 `datetime_bounds.coerce_to_taipei_aware`）：值進來就是 aware 的，這種寫法是 no-op，而且**測試會照樣綠**。

## Consequences

- **翻案條件**：驗證 `PR_MESSAGE_DELIVERY_TIME` 在本 corpus 的可得性、並決定兩屬性缺席時的 fallback 形狀之後，(B) 應重新評估——屆時 (A) 降為 fallback 路徑而非被刪除。
- **單機假設沒有 eager 層的家**。它目前只寫在本檔與 `recipient-rebuild-plan.md`（本機工作檔）（`feat/175-recipient-rebuild-plan` 分支），`#175` 收尾後那份 plan 的歸宿未定。要在別台機器上跑抽取之前，先回來讀這條。
- **相鄰但不相同的決策**：[D-20260723-1](D-20260723-1-taipei-day-date-filter-semantics.md) 管的是**查詢端**如何解讀不帶 offset 的 timestamp（行為契約 #4）。本檔管的是**寫入端**如何從 COM 取得正確瞬間。兩者都收斂到台北，但改動其中一個不會自動修好另一個。
- 舊的全域 `bes` wheel 快照仍帶著這個缺陷；跨專案生效必須發行（[D-20260527-1](D-20260527-1-global-bes-is-build-snapshot.md)）。

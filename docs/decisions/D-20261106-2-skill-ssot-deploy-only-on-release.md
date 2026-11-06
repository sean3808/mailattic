---
id: D-20261106-2
date: 2026-11-06
status: accepted
title: skill→SSOT 部署只在發行時跑，拿掉 main commit 的 git hook
---

## Context

[D-20260820-4](D-20260820-4-skill-ssot-deploy-trigger-main-commit.md) 讓 main 上每次 commit 都經 post-commit／post-merge hook 把 `skills/mailattic/` 部署到 SSOT。它在 Consequences 預留了一條：若出現「skill 已進 main 但暫不想 release 給其他專案」的需求，就回到顯式 release 時點。

這個需求已經出現：

- **skill 跑在 bes 發行之前**。其他專案的 agent 用的是全域安裝、已發行的 `bes`，讀的卻是 main 上最新的 skill。2026-11-06 一個沒有包含在任何發行 tag 裡的 commit，它的 skill 說明照樣被部署出去。這次只是資料狀態的說明；如果是新的子命令或 flag，agent 就會照說明去叫一個已安裝的 bes 根本沒有的介面。
- `scripts/release.ps1` 已經在全域重裝之後跑 `deploy-skill-to-ssot.py --verify-required`（部署後逐檔核對），所以發行本身就是完整的部署時點，git hook 是第二條、而且比發行更早的路徑。

## Decision

從 `.pre-commit-config.yaml` 拿掉 `deploy-skill-to-ssot` hook，`default_install_hook_types` 也一併拿掉 `post-commit`、`post-merge`。skill 只在 `scripts/release.ps1` 發行時部署到 SSOT，這樣 SSOT 上的 skill 一定跟同一次發行的 `bes` 配對。

部署腳本本身的 gate（main、skill 目錄乾淨、以 tracked 清單為界）與「只 cp、不 commit SSOT」不變。

## Rejected alternatives

- **保留 hook，另加「是否已發行」判斷**：hook 要去判斷 HEAD 有沒有被發行 tag 涵蓋，等於在 hook 裡重做一次 release 的判準，兩處會分岔。
- **維持現狀**：skill 與已安裝的 bes 會持續錯開，錯開的時間長短取決於下一次發行何時來。

## Consequences

- skill 改動要等下一次發行才會到達其他專案。發行頻率是一天數次，延遲以小時計。
- 已經 clone 的工作樹裡，舊的 `.git/hooks/post-commit`／`post-merge` wrapper 仍在；設定裡沒有對應的 hook，所以這兩個 wrapper 什麼都不做。需要清乾淨時，重跑 `pre-commit install` 不會移除它們，要手動刪除。
- SSOT 那一側的 skill 副本仍是下游 release copy：在 SSOT 直接改 skill 內容，下一次發行就會被蓋掉。要改 skill 一律改本 repo。

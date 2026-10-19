---
id: D-20260602-2
date: 2026-06-02
status: accepted
title: skill 的 dev-canonical 放本 repo，SSOT 只是下游 release copy
---

## Context

`mailattic` skill 是本專案對外的使用介面正典（其他專案的 subagent 透過它用 bes，而非裸呼叫）。原本 skill 住在另一個 SSOT repo。

問題：CLI 改了 flag，skill 的呼叫說明在另一個 repo，兩邊沒辦法在同一個 change / 同一次 review 裡一起改。2026-06-02 `--full-body --json` 與 `contact-history` 上線後，skill 就漏更新了。

## Decision

skill 的 **dev-canonical 搬進本 repo** `skills/mailattic/`，受 Spectra 納管，跟 CLI 同 repo 可原子化同改。

SSOT repo 裡的 `skills/shared/mailattic/` 降為**下游 release copy**（junction 部署到 `~/.claude/skills/`），由 archive commit 的 post-commit hook（`scripts/deploy-skill-to-ssot.py`）自動從本 repo cp 過去。

隨之凍結**同步契約**：改了 `bes` 子命令或 flag（影響 sync / find / reply 使用方式的）→ 必須在同一個 change 內更新 repo skill。判斷句：「這個 change 改了 subagent 該怎麼呼叫 bes 嗎？」

## Rejected alternatives

- **skill 留 SSOT，靠人記得同步**：已經證實不行（上面那次漏更新）。
- **hook 直接 commit SSOT repo**：跨 repo 自動 commit 太侵入。hook 只 cp 不 commit，junction 服務的是 working tree，部署即刻生效；SSOT 的 git commit 留人工。

## Consequences

- SSOT working tree 比它自己的 HEAD 新，是**正常設計**，不是待修的 drift。不要用 `git checkout` / renormalize 去「修」它，那會回退已部署內容。（本機診斷筆記，未入版控）。
- hook 只在 archive commit 觸發（擋 WIP 外流）；cp 失敗 fail-loud。

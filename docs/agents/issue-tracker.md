# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues in `sean3808/mailattic`. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

## What an issue is in this repo

The issue is where intent lives, from first report through to done: what to do, why, and what counts as finished. Its full text, comments included, is the baseline that the later semantic review and `/spec-reconcile` judge the diff against — so read the comments, not just the body.

An issue is not the behavioral canon. What the system must do lives in `AGENTS.md > ## Behaviour contracts` plus the code. The Spectra change directories used before 2026-08-20 are retired ([D-20260820-3](../decisions/D-20260820-3-migrate-to-contract-anchored-flowback.md)) and not carried in this repo; new work does not open one.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either — resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies** — the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only — the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me` — the session's first write.
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.

## 留言的角色標記

在 issue 或 PR 留言的**第一行**放角色標記。`gh` 的作者欄對所有寫入方都顯示 `sean3808`（token owner，不是作者），讀的人分不出是誰寫的，就得花一輪回頭問。三個寫入方，整行照抄：

```text
> `Claude Code`
> `Codex implementer`
> Sean
```

三條必要條件（字串可以再議，這三條不行）：

1. **固定在第一行** —— 人與 agent 都能不讀內文就認出。
2. **三個寫入方各有唯一字串**，尤其 `Codex implementer` —— 它最容易被誤認成 Sean。
3. **不依賴 `gh` 的 author 欄**。那欄永遠是 token owner，靠它區分的方案都是假的。

**未標記的留言 = 歸屬不明、要問**：兩個 agent 寫入方一律必標，Sean 是自願標。2026-10-03 以前的留言另有 `/session-dispatch` 與 `/session-merge-queue` 兩個標記，屬已退役的雙 session 分工（`#207`）。

**「誰寫的」與「誰決定」分開記**：角色標記只答前者；決策歸屬寫在標題（`## 拍板：走 (A)（2026-08-11，Sean）`）。Codex 寫下 Sean 的裁決時，標記寫 `Codex implementer`、標題寫 Sean。既有 comment 保持原樣 —— 歷史歸屬只有當事 session 知道，事後補等於猜。

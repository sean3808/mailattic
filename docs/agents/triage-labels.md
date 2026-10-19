# Triage Labels

The label set below is the live, decided one — it is what `gh label list` actually returns for this repo. Use these strings verbatim; do not invent roles the tracker doesn't have.

| Label | Meaning |
| ----------------- | ---------------------------------------------- |
| `bug`             | Something is broken |
| `enhancement`     | New capability |
| `needs-triage`    | Nobody has looked at it yet — the landing state for a new issue |
| `needs-info`      | Looked at; what's missing is in **someone else's** hands (reporter, vendor, upstream) |
| `ready-for-human` | Looked at; what's missing is **your decision** |
| `ready-for-agent` | Fully specified, ready for an AFK agent |
| `roadmap`         | Context and route record, not a work item |
| `wontfix`         | Decided not to act — do not reopen |

Two axes, applied independently. `bug` / `enhancement` says **what kind of work** it is. The four readiness labels say **who can move it next** — and unlike the first axis they are ordered: an issue walks `needs-triage` → (`needs-info` | `ready-for-human`) → `ready-for-agent`. An issue carries at most one from each axis.

**The line between `needs-info` and `ready-for-human` is who holds the missing thing.** Outside this repo → `needs-info`, and the action is to go ask. Yours to decide → `ready-for-human`, and the action is to decide. Getting this backwards is how an issue ends up waiting on a human who is waiting on it.

`roadmap` is off both axes. It marks an issue that exists to preserve context, evidence, or route history rather than to be picked up — a superseded umbrella issue whose phasing canon has moved to an ADR, for example. A `roadmap` issue carries no readiness label: it is not waiting on anyone.

There is deliberately **no `blocked`** (removed 2026-08-09; it existed until then). Being blocked is not one state, it is three, and each has a different next action — collapsing them into one label tells the reader nothing about what to do:

- blocked by another issue here → **GitHub's native issue dependencies**, verified working on this repo (`gh api repos/sean3808/mailattic/issues/<n>/dependencies/blocked_by`, POST with `-F issue_id=<internal id>` — the internal `id`, not the issue number); `sub_issues` is available as well
- blocked on something outside the repo → `needs-info`
- blocked on a decision you owe → `ready-for-human`

> The migration found the label had already drifted from the mechanical state: of the four issues carrying `blocked`, two (`#78` / `#35`) had `blocked_by = 0` because every blocker had been closed. Nothing errored — the label just went on asserting a state nobody was maintaining. That drift is the argument for not having it.

If a skill asks for a role this table doesn't list, map it onto the closest label above rather than creating a new one. Adding a label is a tracker-wide decision, not a per-issue one.

## The readiness labels are dispatch triggers

Every readiness label names a concrete action, and that is the test for whether it earns its place — a label nobody acts on is decoration:

| Label | What it triggers |
|---|---|
| `needs-triage` | A triage pass picks it up |
| `needs-info` | Someone goes and asks; the issue then waits on the reply |
| `ready-for-human` | Someone asks Claude to summarize it so the decision can be made |
| `ready-for-agent` | A fresh worktree and session get opened for it |

Nothing else on this tracker moves a human. `bug` / `enhancement` stay as an auxiliary hint — apply them when they help, but they are never part of a readiness decision.

The agent applying a readiness label is almost always the one that just adjudicated the issue, and the agent picking it up later arrives in a clean worktree with **no conversation context** — the issue is its entire input. So the label alone is not the handoff; the comment that accompanies it is.

### Applying `ready-for-agent`

A false positive costs a whole session: the agent works until it hits a decision it cannot make, then stalls. Four gates, all four required:

1. No unresolved fork left in the issue — nothing reading "待拍板 / 二選一 / 需要決定" that a later comment hasn't closed.
2. A single branch / PR can finish it. Acceptance must not depend on accumulating evidence across future changes or on another session's observation.
3. Acceptance is checkable by a third party — a command, a test name, or a red-green cycle. `#58`'s "removing the guard must turn the test red" is the model.
4. `gh api repos/<owner>/<repo>/issues/<n> --jq .issue_dependencies_summary.blocked_by` returns 0.

**Gate 4 is checked when the label goes on, and nothing keeps checking it afterwards.** A readiness label is an assertion made at one moment; `blocked_by` keeps moving after that moment. Add a dependency to an issue that already carries `ready-for-agent` and the label goes on claiming the issue is dispatchable — nothing errors, nothing sweeps it. **So re-read `blocked_by` at dispatch time, not just at labelling time.** This is the same failure that retired `blocked`: a label asserting a mechanical state that no check maintains. Removing `blocked` removed one instance of it, not the shape — `ready-for-agent` carries the identical risk and is the one that actually costs a session when it is wrong.

The same comment that applies the label carries the dispatch brief — objective and scope, an explicit **不做** list, the acceptance check, and one line naming any part of the body that no longer holds. `#27` and `#58` are worked examples. This is a comment, not a template: what matters is that those four things are present, not that they sit under fixed headings. An issue filed already labelled has no such comment, and needs none — its body **is** the brief and answers to the same four gates (`#60` / `#61`).

Three things the brief is repeatedly better for having:

- **Every 不做 entry carries its reason**, and the reason may belong to another issue. `#61` declines a doc-index check because it would contaminate `#47`'s running pilot, and declines coverage because `#40`'s non-goals already ruled on it. A bare prohibition gets argued away by the next agent under pressure; a prohibition with a stake in it does not.
- **Name what will stop the agent, before it gets there.** `#62` names the exact test that rejects a hand-rolled `uv run pytest` inside a workflow. Cheaper than letting it be discovered.
- **Name the output path.** "Write it up in `docs/`" makes the agent invent a filename nobody will find again.

**The brief has a second reader.** The issue's body and every comment are the intent baseline for the semantic review that follows implementation (`/code-review`'s Spec axis, `/closeout`'s semantic-review cell, `/spec-reconcile`). The brief is therefore not only instructions to the implementer — it is also the standard the diff gets judged against.

That makes one test worth applying to **every 不做 entry**: could a third party settle this by reading only the diff? Gate 3 above already demands that of acceptance, but the 不做 list is where the two kinds of sentence actually mix, and nothing was holding it to the same bar. "Do not add a coverage flag" passes. "Wait until `#104` lands" and "do what Sean decided in the thread" do not — those govern the implementer's sequencing, and a reviewer handed them files findings against work no diff can contain.

Entries that fail the test still belong in the comment, just somewhere else in it — separated at whatever paragraph or heading level fits the issue. Any separation a reader can see is enough; this is still a comment, not a template.

`#61` is the tell that the mixing is not theoretical: its doc-index decline is at once a spec clause and a cross-issue process reason.

### Forbidding a workaround

When a gate is going to obstruct the work, the brief must forbid the bypass **and state what the gate is for** — the bypass is the agent's most natural move the moment it hits friction. `#60` does this: it forbids a CI-only escape hatch around the `exit 18` system-drive guard, and says why (「守門的 intent 是『工具 I/O 不落系統碟』，開後門等於把 gate 降級成建議」). A prohibition written as a pattern gets read as an enumeration and routed around; written as an intent it survives a case nobody listed.

### Watch periods are not acceptance

An observation window is not work, and bundling one into a `ready-for-agent` ticket makes "done" unreachable — the agent finishes the build and then stalls waiting out the window. Keep the acceptance to what a single PR closes, and state the closing condition separately, including the readiness label the issue flips to while it is being watched. `#61` carries the corrected shape; `#40` is the case that motivated the rule.

For the same reason, **an issue with open forks in it does not become actionable just because its last dependency closed.** `blocked_by` dropping to 0 answers one unblock condition, not all of them. Say so in the issue: `#62` records that closing `#60` is only one of its unblock conditions, the others being its own pending choices. Otherwise the flip happens mechanically and the dispatched agent lands on gate 1.

Better still, **where a constraint can be an edge, do not leave it as prose.** When `#60`'s spike surfaced host dependencies that must land before pytest can be gated, the right move was not a paragraph warning the next reader — it was filing `#64` / `#66` / `#67` and wiring them as blockers of `#62`, so `blocked_by` answers the question mechanically. Prose is the fallback for a blocker that has no issue, not the default.

A ticket that opens a watch period must also say, alongside the closing condition, **in what form the evidence is kept**, choosing one of: paste the real output into an issue comment; keep the artefact and give its path; or leave a re-runnable command. The rule is to state which one you chose, not to always keep artefacts — a pure observation (open a worktree, look, remove it) legitimately leaves nothing behind, and forcing a leftover just creates something to clean up. Without it, "observed, then cleaned up" is indistinguishable from "never done": `#103` had the `ls` output pasted in a comment and its worktree removed, and the check that found no worktree reopened it.

For the checker: to deny that something happened, read the comments and the timeline, not only `state` / `closedAt` / `labels`. Traces of one-off actions are cleaned up by default, so their absence from the filesystem or the issue fields is not a refutation (`#103` again).

### One issue, one work unit

An issue holding two work units cannot be dispatched or even named: a readiness label on it is ambiguous, and neither the human nor a later agent can refer to half of it by number — conversation falls back to branch or change names that mean nothing outside the session that coined them. When the work splits, usually because a proposal became two Spectra changes, split the issue with it.

Split into native `sub_issues` rather than filing two fresh tickets and closing the original. The parent keeps the research, flag verification, and rejected options that justify the shape, which is normally the most expensive content in the issue and the part a clean worktree has no other way to obtain. `#59` → `#73` / `#74` is the worked example.

The parent drops its readiness label — nobody picks it up — but it is **not** `roadmap`: `roadmap` means preserved context with no live work behind it (`#24`), while a parent with open children is still tracking work. Where the children are ordered, wire the order as `blocked_by` on the child rather than as prose in the parent, and move the parent's acceptance list down into the children — a list written for one unit fits neither half, and leaving it in the parent is what makes the split look optional.

### An unconfirmed finding still gets a ticket

"Needs replication" is a scope, not a reason to skip filing. `#60`'s spike produced four categories of failure; three were filed immediately and the fourth — a single unreplicated lock-race observation — was left in the closing comment of an issue that then got closed, which is exactly how work falls outside both queues. If it is worth writing down honestly, it is worth an issue whose first acceptance item is the replication itself (`#67`).

### Applying `ready-for-human`

The comment opens with the pending decision stated as a question plus its options, so the summary can start from that line instead of re-reading the whole body. Long-lived proposal issues are the case this exists for — `#35` carries four separate open decisions scattered through a 13 KB body. `#63` is the shape done right from the start: the body opens on the question, gives (A) / (B) with a recommendation, and keeps a second, non-decision finding (branch protection being unavailable on this plan) as a record rather than inflating it into a second question.

**Every decision Sean owes lives on an issue** (Sean, 2026-10-10). A decision that surfaces mid-session lands as a comment on the issue it belongs to, or as a new issue when none fits, and `session-continuity.md` lists the issue number as a pointer. A decision written only into the hand-off note is visible to one machine and gets re-copied at every park; the 2026-10-01 batch sat that way for nine days without being re-verified.

A `ready-for-human` issue still carries a **不做** list. It is read by agents too, and the thing most worth forbidding there is the agent acting on the decision itself — `#63` bars any agent from touching repository settings.

### When the body has been overtaken

Add one line at the top of the affected section pointing at the comment that overturned it. Do not rewrite or delete the original — the superseded reasoning is why the issue is worth keeping. `#24` and `#40` both do this with a `⚠️` banner; `#15` §1 is the case that motivated the rule.

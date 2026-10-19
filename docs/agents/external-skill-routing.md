# External Skill Routing

External skill packs (currently `mattpocock-skills`) assume repo conventions this project does
differently. **They are not forked.** The upstream copies stay unmodified; every deviation is
declared here instead.

They reach this repo through two independent surfaces, and a rule only holds where it is written:

| Surface | Loaded from | Routing entry point |
|---|---|---|
| Claude Code | `~/.claude/plugins/cache/mattpocock/` | `CLAUDE.md` (`@AGENTS.md`) → this file |
| Codex | `.agents/skills/` (managed by `npx skills`, gitignored; `skills-lock.json` is the reproducible source) | `AGENTS.md` → this file |

Never hand-edit either location — a plugin update or a `skills` reinstall overwrites it. That includes
`agents/openai.yaml` policy files inside `.agents/skills/<skill>/`: they live in an ignored, tool-managed
directory, so they are not a durable place to put a guard. To stop Codex reaching a skill at all,
uninstall it (`npx skills remove <skill>`) — the removal lands in `skills-lock.json`, which is tracked.

Human-only skills are marked twice, once per surface, because neither surface reads the other's marker:
`disable-model-invocation: true` in `SKILL.md` for Claude Code, and `policy.allow_implicit_invocation:
false` in `agents/openai.yaml` for Codex (Codex ignores unknown frontmatter keys). Upstream ships both
and they currently agree 1:1 — verified against the installed copies, no mismatches.

**So the risk here is drift, not absence.** Upstream has already had one: `writing-for-agents` was
made model-invoked and its `SKILL.md` updated, but its `openai.yaml` was moved without editing, so the
skill silently disappeared from Codex's available-skills list (mattpocock/skills#748). Nothing errors —
a skill just stops being offered on one surface. If a skill behaves differently under Claude Code and
Codex, compare those two files before assuming anything about this repo.

Still open upstream: Codex's own installer rejects `disable-model-invocation` as an unsupported
frontmatter key, so these skills fail its native validation even though `npx skills add` installs them
fine (mattpocock/skills#360).

## When a skill's instruction conflicts with this repo

| The skill says | Do this instead |
|---|---|
| Write the glossary to `GLOSSARY.md` | **No deviation since 2026-08-20** — repo root `GLOSSARY.md` is the one glossary (was `openspec/LANGUAGE.md` until the Spectra chain retired, [D-20260820-3](../decisions/D-20260820-3-migrate-to-contract-anchored-flowback.md)). See [domain.md](domain.md) for the ADR path, which still deviates (`docs/decisions/`, not `docs/adr/`) |
| Multiple contexts via `GLOSSARY-MAP.md` | Not applicable. This repo has one business language. Ignore that section rather than growing into it |
| Invoke pytest directly, with or without coverage instrumentation | `scripts\invoke-pytest.ps1` is the only entry point. See `AGENTS.md` and [quality-gates.md](quality-gates.md). A contract test patrols every tracked file for the raw command and coverage-flag literals, so do not spell them out here either |
| Point a prototype or scratch run at "a scratch DB" | Pass an explicit `--database` to a temp path. **Never let it resolve the default** — the multi-GB production DB sits at the repo root and *is* the default target |
| Save research / investigation notes "where the repo keeps such notes" | `.subagent-output/` (gitignored) |
| Run `/setup-matt-pocock-skills` first | Not needed to proceed: the three files it generates (`issue-tracker.md`, `triage-labels.md`, `domain.md`) are already here and hand-tuned, so read those and carry on. It is installed on both surfaces and human-only on both, so running it is Sean's call. When he does, it starts from upstream seed templates (e.g. `docs/adr/`, and its domain-docs section writes without asking) — merge its output into the hand-tuned files and keep this repo's deviations (`docs/decisions/`, the label set in `triage-labels.md`) |
| Write the PR body from the `pr` template (Summary / Evidence / Merge Danger) | Follow the `pr` skill for the body, and add a `## Squash message` section to it. `AGENTS.md > Pull Request 交付契約` requires that heading with the why and one `Closes #n` per issue — this repo is squash-only, so that section is the only part of the PR that survives into `main`'s history. Evidence comes from `scripts\invoke-pytest.ps1` runs, not a raw test command |
| `/implement-spec`: subagents explore, implement and merge, each implementer in its own worktree | A fresh worktree needs `uv sync` first. If the exploration step runs on the built-in Explore agent, put any repo rule it must follow in its prompt. The integration branch reaches `main` by squash merge like any other branch |
| Mutate an existing active change reported by `spectra list --json` from any worktree | Treat the list as common-repository inventory, not current-worktree inventory. Spectra 2.3.1 discovers active change directories and task counts from registered sibling worktrees through the common Git directory. Before every mutation targeting an existing active change, use `git worktree list --porcelain` to enumerate every registered worktree root and test each `<worktree-root>/openspec/changes/<change>` path, including the current root from `git rev-parse --show-toplevel`. Exactly one match is required. If that match is the current root, run the mutation there; if it is another root, switch there and rerun the command. Zero matches (parked or misspelled change) or multiple matches are ambiguous: stop without running the mutation. This owner gate does not apply to commands whose purpose is to create or restore the active directory, such as `spectra new` or `spectra unpark`. |

The owner check is required even when Spectra itself reports success. A disposable-clone probe on
2026-08-12 had only `openspec/changes/archive/` in the calling tree and the active change in a
registered sibling worktree. `spectra list --json` returned that sibling change with its exact task
count. Running `spectra archive <change> --yes` from the calling tree then returned exit 0 while it
deleted the active change, applied the delta to canonical specs, and created the archive **in the
sibling worktree**. The list therefore is not evidence that an existing active change belongs to the
current tree; the exactly-one-owner check above is the gate for mutations targeting such a change.

## Whole-spec implementation: which skill Sean invokes is the decision

Two skills implement a whole spec across worktrees, and the choice between them is not a judgment
call for the agent. `/implement-spec` means the work is done by Claude's own subagents;
`/herdr-dispatch` means it is handed to another model (agy, Codex). Whichever one Sean invokes
settles who executes — do not ask, and do not substitute one for the other.

Rules the skills get right and this repo keeps unchanged: `prototype`'s throwaway branch out of `main`
with a pointer from the issue, and `domain-modeling`'s three-condition gate for opening an ADR.

## Why a routing table and not a fork

A fork carries the whole rule set but has to be re-diffed against every upstream release. A routing
table costs nothing on upgrade, but only covers what someone remembered to write down.

That trade-off has already bitten this repo once: `docs/agents/domain.md` used to say only *"don't
mention `CONTEXT.md`, don't suggest creating one"*. Agents obediently did not create it — and also
never read `openspec/LANGUAGE.md`, because nothing pointed them there. No error, no signal.

So: **state the positive target, never just the prohibition**, and treat a routing line as unverified
until an agent that read it lands on the right file. Cheap check — give a fresh Explore agent this
file and nothing else, then ask where the glossary lives, how tests are run, and whether a prototype
may open the default database.

Lines whose failure would cause real damage do not stay prose-only. The test-entry rule is enforced
by a contract test; the production-DB rule is currently prose, and should grow a check the first time
anything actually reaches for the default path.

# mailattic

> The project canon shared by every coding agent: Codex reads this file directly, Claude Code loads it through `@AGENTS.md` in `CLAUDE.md`. It holds only facts that stay true across changes and sessions. In-flight progress and change-specific decisions live in GitHub issues (intent) and PRs (discussion).

## What this is

**A query backend for a historical mail corpus**: historical PSTs from a self-hosted mail server (`mailattic.test`) plus incremental IMAP sync → SQLite + FTS5 (years of mail, tens of thousands of messages). It answers for the stretch the server no longer holds, which a live IMAP proxy structurally cannot reach. Every consumer is a program interface: agents in other projects use `bes` / the skill, CoWork uses MCP, and Sean uses the same two.

- **Interfaces**: CLI `bes` (subcommands print JSON) + Python API. The cross-project hub is the **`mailattic` skill** (dev-canonical in this repo's `skills/`; the SSOT is the downstream release copy), not bare `bes` calls.
- **CoWork**: the sandbox cannot run a host CLI, so an MCP server reaches it through the Claude Desktop bridge (six read tools, three write tools) → [`docs/mcp-server-contract.md`](docs/mcp-server-contract.md). Whether the sandbox can see the DB and CAS directory depends on per-session mounts, so it is not an interface (`#28` §1 / §2.2); host subagents are unaffected.
- **Dual-track**: Outlook handles daily send and receive (the human interface); this DB handles long-term accumulation. The two coexist through the message-id dedup gate.
- **This repo is the public home** (`sean3808/mailattic`, [D-20261010-1](docs/decisions/D-20261010-1-showcase-ships-ahead-of-remoteization-in-a-fresh-repo.md)): daily development and issues happen here. The earlier private repo is frozen as an archive; its commit history and original threads were not carried over, so an unlinked `#NNN` in the docs is usually a private-era ticket that cannot be looked up here. Issues, PRs, and commits carry placeholders only: no real mail content, real domains, or real addresses (the pre-commit check blocks them). Debugging that needs real mail as evidence keeps that evidence on the local machine.
- **The human-facing front page** is [`README.md`](README.md) (English first; [`README.zh-TW.md`](README.zh-TW.md) is the Traditional Chinese edition, and the two change together). This file is the rules for agents.

**Out of scope**: enterprise mail server / eDiscovery / GUI client / push notifications / semantic vector search (FTS5 keyword only) / attachment OCR / task tracking (chasing commitments belongs to a task tool, [D-20260603-1](docs/decisions/D-20260603-1-no-task-or-commitment-tracking.md)) / a product for third parties to import their own mailboxes (open source here is a showcase; no cross-platform cost is paid for hypothetical users, [D-20260813-2](docs/decisions/D-20260813-2-open-source-is-showcase-not-product.md)). A request that lands on this list gets reported before any work starts.

## Common commands

```powershell
uv sync                                    # sync the venv (Python 3.13); first step in a fresh worktree
uv run bes <subcommand> [flags]            # CLI, runs the working tree

# The launcher is the only pytest entry (a contract test patrols raw calls outside it)
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Quick            # daily; not release evidence
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Full             # closeout / merge / release evidence
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route FullSequential   # when parallelism is suspect
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Targeted tests\test_query.py::test_x

uv run --locked --exact ruff check --fix   # pre-commit runs it too
uv run --locked --exact ruff format        # check only: add --check
uv run --locked --exact pyright            # scans only mcp_server.py / mcp_delivery.py (pyproject include)
```

Choosing a route, and the canonical deptry / pip-audit commands with their exit-code meanings → [`docs/agents/quality-gates.md`](docs/agents/quality-gates.md). **A non-zero gate is not a found defect by itself**: the gate may have failed to run. Read the output.

The production DB `mailattic.db` (repo root, several GB) is the **default target** of every entry point. Prototypes, experiments, and one-off scripts always pass an explicit `--database` pointing at a scratch path. Backup snapshots live in `backups/`.

## Architecture map (`src/mailattic/`)

| Area | Modules | Notes |
|---|---|---|
| Entry | `cli.py`, `__main__.py` | argparse dispatch for every subcommand; entry points `bes` / `bes-mcp` (`__main__.py` only forwards to `cli.main`) |
| Ingest | `discovery.py` → `parser.py` / `outlook_pst.py` → `ingest.py` → `storage.py` / `schema.py` | PST/`.eml` discovery → parsing (`outlook_pst.py` reads PSTs through Outlook, including the recipient-extraction ledger check) → dedup gate + normalisation → write; a single-message failure is recorded in `ingest_failures`. The actual shape of the three import paths is in `GLOSSARY.md` under "Import paths (匯入路徑)" |
| Query | `query.py`, `query_commands.py`, `contact_history.py`, `thread_key.py`, `cjk_segmentation.py`, `datetime_bounds.py` | FTS5 + structured fields; the CLI and the Python API share this layer. `query_commands.py` is the CLI query command family (search / attachments / show / thread / contact-history): it registers its own parsers and carries its own handlers and date-input conversion; `cli.py` only calls its `register_*` |
| IMAP | `imap_native.py`, `imap_runtime.py`, `imap_state.py`, `imap_sync.py`, `mutf7.py` | UID-based incremental sync (native imaplib) |
| Outbound | `outbound.py`, `outbound_commands.py` | draft/send/APPEND-to-Sent; the human authorisation gate → contract #2. `outbound_commands.py` is the CLI send command family (draft-reply / list-drafts / send-draft / send-batch): it registers its own parsers and carries its own handlers, send-settings-file errors, review printout, fingerprint pair syntax, and exit-code mapping; `cli.py` only calls its `register_outbound_commands` |
| MCP | `mcp_server.py`, `mcp_delivery.py`, `mcp_readonly.py`, `mcp_writer.py` | CoWork bridge: six read tools + three write tools (attachment export, draft reply, send) + the response-budget degradation ladder. Reads → contract #1 (bound at one point, `ReadOnlyMailReader` in `mcp_readonly.py`); writes go only through the one accessor in `mcp_writer.py`; send → contract #7. When changing output fields or degradation order, read `mcp_server.py` in this order: response-budget refusals (`_*_over_budget_error` / `_*_terminal_refusal`) → field whitelists (`shape_*` / `_shape_*`) → ladder composition (`_*_ladder_states`) → the tool handlers inside `create_server` (each calls `mcp_delivery.walk_ladder`) (#327). **Verify tool-surface changes with the probe loop** ([`docs/mcp-client-probe-loop.md`](docs/mcp-client-probe-loop.md)): headless `claude -p` against the working tree, no Claude Desktop restart (restart only for closeout acceptance; `codex exec` is explicitly excluded) |
| Attachments | `attachment_store.py`, `quarantine.py` | content-addressed store (SHA-256); quarantine: during cleanup, a scratch raw message whose attachment failures are unresolved moves here instead of being deleted (#256) |
| Shared | `models.py`, `identity.py` | `models.py`: cross-layer dataclasses and search-text normalisation; `identity.py`: which addresses count as "me" (`self_addresses` table, maintained by `bes identity`) |
| Maintenance | `db_maintenance.py`, `rebuild.py`, `archive_scrape.py` | backup / doctor / verify-cas; rebuild is the deep DR fallback |

## DB schema

Read [`docs/db-schema.md`](docs/db-schema.md) before writing SQL, changing the schema, or touching the thread bridge (column design per table, and which columns are rebuildable caches). Get row counts and the latest cutoff from SQL (`SELECT MAX(sent_at) FROM messages`).

## CLI and Python API

Subcommand list, flags, and the Python API (`search_messages` / `SearchFilters` / `get_message`) → [`docs/cli-reference.md`](docs/cli-reference.md); `uv run bes <cmd> --help` is authoritative.

Two semantics fail silently when misused, returning a plausible-looking wrong answer:

- **Dates → contract #4**: whole Taipei days, inclusive at both ends; an ISO-8601 timestamp given to `search` / `attachments` without an offset is read as Taipei time. Mail on a boundary day quietly gains or loses a day.
- **`--keyword` boolean operators are case-sensitive**: uppercase `AND` / `OR` / `NOT`; lowercase `and` is a literal term. CJK is segmented per character; part numbers with hyphens take a phrase quote (`'"PEN-100"'`).

## Hard rules

### Environment

- **Windows 11 / Python 3.13 / `uv` / `.venv/`**, run locally. Use `uv` for everything: pip, poetry, a bare `python`, and older Pythons are out. Source PSTs are read-only.
- **Every script is Python `.py`** (git hooks included): git hooks run as pre-commit framework local hooks (`entry: uv run python scripts/xxx.py`, `language: system`), and shell actions use stdlib (`subprocess` / `shutil` / `filecmp`).
- **`PYTHONIOENCODING=utf-8`**: required when running Python directly or using the Python API; the `bes` CLI handles it internally.
- **A WSL writer + a `/mnt/<drive>` destination → always robocopy or a tar bundle, whatever the file count**: on Windows, `robocopy "\\wsl$\Ubuntu-24.04\..." "C:\..." /E /Z /R:2 /W:2 /MT:8` (keep these conservative flags), or `tar -czf` on the WSL side first and then move it. `rsync` / `cp -a` / `tar -x` writing straight to `/mnt/c/...` once triggered a storage-controller reset and partial data corruption ([D-20260525-1](docs/decisions/D-20260525-1-no-wsl-write-to-mnt-drive.md)). Read [`docs/wsl-environment.md`](docs/wsl-environment.md) before any WSL operation (distro, mirror paths, uv location, docker mounts, the `run_sync.py` path trap).
- **Work returned from a non-Windows environment (Linux cloud agent / WSL / container) is always a draft**: the canonical launcher and `pyright` commands run only on Windows; elsewhere only `ruff check` / `ruff format --check` / `deptry src` / `pip-audit` run. Open a **draft PR**; it becomes ready for review only after a local Windows `-Route Full` passes. A raw pytest pass outside the launcher is not evidence (host-difference bugs do not reproduce on Linux). Before dispatching, ask: does what this ticket must verify touch pwsh / the launcher / the registry / `.tools/`? If so, dispatch to a Windows environment (mechanism → quality-gates.md, `#61`).

### Tests and one-off artefacts

- **pytest runs only through the four routes of `scripts\invoke-pytest.ps1`**, with no environment variables needed (the system-drive guard is retired, [D-20260821-1](docs/decisions/D-20260821-1-retire-system-drive-hardware-guard.md)). Exit 18 only means the volume could not be established (dangling junction, UNC).
- **One-off artefacts stay inside the project under `.project-runtime/`**: test fixtures, scratch DBs, and test output use `tmp_path` (the launcher points it at `.project-runtime/pytest`). When a production artefact's contract requires a location outside the working tree, tests still verify its content in `.project-runtime/pytest` through a test-only override, plus a refusal test proving the production entry rejects in-repo paths. Other tool output goes to an existing managed leaf first; when none fits, read the runtime contract in `scripts/project-tool-environment.ps1` before adding a new ignored path.

### git, review, and merge

- **main has several writers**: check `git status --short` before starting, and leave every pre-existing change unrelated to the task exactly as it is (unreverted, unoverwritten, unstaged). Stage by explicit path (`git add -A` / `git add .` sweep in other writers' files), confirm each file with `git diff --cached --name-only` before committing, trust the `[<branch> <sha>]` that `git commit` prints as the landing point, and re-check `git branch --show-current` before pushing. The offending session sees no symptom at all; only a later `--stat` reveals it (PC-088 / PC-133).
- **Semantic review is triggered by size, not by a PR** ([D-20260820-1](docs/decisions/D-20260820-1-review-trigger-is-change-size-not-pr.md)): M/L always get a semantic review, wherever they were produced and whether or not they go through a PR; S (config, docs, archive, chore, a known one-liner) goes straight to main; when the size is unclear, treat it as M. Open a PR only for an external hand-off or when a traceable thread is wanted. What is relaxed is the process wrapper, not the review. The three excluded CI options → [D-20260820-2](docs/decisions/D-20260820-2-excluded-ci-infrastructure-options.md).
- **Squash merge only** ([D-20260812-1](docs/decisions/D-20260812-1-keep-squash-only-merge.md); the squash message requirements are in `## Pull request delivery contract`): the global L-size `--no-ff` is impossible here, and `gh pr merge --merge` is rejected by GraphQL. This is a deliberate repo setting: keep it, and keep merges on GitHub rather than routing around it with a local merge. `--ff-only` is used only to sync local main to `origin/main`.

### Keeping docs in sync

- **A CLI surface change (`bes` subcommand / flag) → update `skills/mailattic/` in the same change** (`SKILL.md` + `references/capability-*.md`) and review them together. The test: does this change alter how a subagent should call bes?
- **Four tests are bound to this file**; check them before moving a section: `tests/test_architecture_map.py` (the modules in `## Architecture map` = the actual modules in `src/`), `tests/test_agent_pr_contract.py` (exactly one PR delivery contract JSON block in the whole repo, in this file's section), `tests/test_contract_gate_reminder.py` (the anchor tests listed in the `## Behaviour contracts` table = `ANCHOR_TESTS` in `.claude/hooks/contract_gate_reminder.py`), `tests/test_tag_based_release.py` (README + this file + [`docs/global-install-and-release.md`](docs/global-install-and-release.md) together make clear that push ≠ release ≠ skill archive, and the first two link to the third).
- **Renaming a file, moving a file, or changing a heading turns `tests/test_markdown_pointers.py` red** (`#116`): it scans every relative link and `#anchor` in tracked markdown and goes red when the target file or heading is missing. The fix is to update the links that point there, not the test. Existing dead anchors are frozen in `tests/fixtures/dead_markdown_pointers.txt` (it may only shrink: a new dead anchor goes red, and a repaired one must be removed from the list); when regenerating the list is genuinely needed, run `uv run python -m tests.test_markdown_pointers` and have a human look at the diff.
- **A report to an AI agent is facts + location**: `subject` / `sender` / `recipients` / `sent_at` / `pst_source` / `message_id` / `db_id`; add a body excerpt when needed (keyword ±N lines); for a contradiction, give each side's own `message_id`.
- **An APPEND warning is never a reason to resend → contract #3**: SMTP acceptance ≠ final delivery (it can still bounce) ≠ appearing in Sent (visible only when APPEND succeeds).

## Behaviour contracts (行為契約)

**This is the project's current behavioural canon** (replacing the retired Spectra capability specs). Each entry = one business-semantics sentence + one anchor test as a mechanical tripwire: breaking it turns something red instead of relying on someone's memory. Background and this repo's ruling: [D-20260820-3](docs/decisions/D-20260820-3-migrate-to-contract-anchored-flowback.md) / issue `#165`.

**Gate**: a change that touches a contract entry or its anchor test **must go through a PR + `/spec-reconcile`** (other changes follow the size rule of [D-20260820-1](docs/decisions/D-20260820-1-review-trigger-is-change-size-not-pr.md); direct commits are fine). Three hard constraints:

- Reconcile reports **NOT INTENDED + IMPLEMENTED** or **CONTRACT VIOLATED** → stop and wait for Sean's ruling; the agent does not decide whether it may pass.
- Reconcile is bound to **that run's final diff**: a commit landing after the report voids that reconcile, and it is rerun.
- A change to an anchor test file itself requires a cross-model semantic review: whoever loosens a tripwire cannot review their own loosening. `/closeout`'s `semantic-review` cell (`/codex-review`) carries it out, so the closeout level is Medium or higher.

**Protocol delivery (the orientation point)**: this section is the single canon for the gate. When a dispatch may touch a contract entry or anchor test and the executor does not load this file (built-in Explore / Plan, external models that do not read `AGENTS.md`), the dispatch carries this section's gate clauses and the iron rule below.

**Review dispatch iron rule (PC-043)**: the dispatch for `/spec-reconcile` and for the Spec axis of code review **must include the intent-precedence clause**: the full issue (comments included) is the intent baseline; documents shipped with the work under review, and comments written on the spot by the implementing session, are **claims under review** and never the baseline. The verbatim clause lives in the "Intent 基準" section of the `spec-reconcile` SKILL.md; copy the whole section into the dispatch rather than rewriting it.

| # | Contract | Anchor test |
|---|---|---|
| 1 | **MCP reads are read-only; the only write is the send record** — the CoWork MCP server reads only through the read-only path: data access goes through `mcp_readonly.ReadOnlyMailReader` (bound at one point with `read_only=True`), and reading the original message for a draft also uses a read-only connection; `mcp_server.py` must not import the data functions of query / contact_history / storage directly. The MCP server's only DB write is `mail_send` recording a sent message through the whitelisted send path (contract #7), the same as `bes send-draft` (`#189` ruling, 2026-09-25) | `tests/test_mcp_server.py::McpServerTests::test_server_module_carries_no_read_only_import_boundary_violation` (the 3 other AST guards in the same file belong to this contract) |
| 2 | **An outbound send = the draft that was reviewed** (CLI form, [D-20260914-1](docs/decisions/D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md)) — the gate lives inside the core function, and every send must satisfy at least one of: a **fingerprint matching the current draft bytes** (SHA-256), or an explicit-text authorisation where stdin and stdout are **both** real TTYs; with neither, the send is refused. A fingerprint mismatch is refused without writing the send-once marker; a batch is checked per message, and a mismatch refuses only that message; there is no non-interactive flag other than the fingerprint. Every send must carry send settings (`env`, used to APPEND into the sent-mail backup after sending): when the caller gives none, the send is refused before authorisation and SMTP, without writing a marker (`#114`) — otherwise the mail goes out, the sent-mail backup never shows it, and that warning line is exactly the resend temptation #3 exists to block. "Was the user actually asked" cannot be tested; it is the agent's conversational duty and outside this contract. MCP form: see #7 ([D-20260924-1](docs/decisions/D-20260924-1-mcp-send-binds-reviewed-draft.md)) | `tests/test_outbound_send.py::test_fingerprint_mismatch_refused_without_marker` (+ `test_non_tty_without_fingerprint_refused_listing_both_ways_out`, `test_batch_fingerprint_mismatch_refuses_only_that_draft`, `test_non_tty_stdin_refused`, `test_non_tty_stdout_refused`, `test_send_cli_has_no_authorization_bypass_beyond_fingerprint`; `tests/test_imap_append.py::test_send_draft_env_none_refused_before_smtp_and_marker`, `test_send_batch_env_none_refuses_whole_batch_before_smtp`, `test_submit_prepared_draft_env_none_refused_before_marker`) |
| 3 | **An APPEND failure never triggers a resend** — when the post-send APPEND into the sent-mail backup fails, only a stderr warning is printed and the exit code is unchanged: the mail has already gone out, and treating a visibility failure as a send failure would send duplicate mail to the outside world | `tests/test_imap_append.py::test_append_failure_does_not_fail_the_send` |
| 4 | **Whole-Taipei-day date semantics** — date filters are whole Taipei days, inclusive at both ends (`00:00:00+08:00` to `23:59:59+08:00`); a timestamp without an offset is read as Taipei time, not UTC (pins the current state of [D-20260723-1](docs/decisions/D-20260723-1-taipei-day-date-filter-semantics.md)) | `tests/test_date_bounds.py::WholeDayBoundTests::test_lower_bound_is_midnight_taipei` (+ `test_upper_bound_is_last_whole_second_taipei`) |
| 5 | **Sync fails loud** — when the message itself is not persisted, sync stops at that UID: the watermark does not move past it, `ingest_failures` records it, and the exit code is non-zero; **an attachment sub-failure does not trigger the stop** ([D-20260709-1](docs/decisions/D-20260709-1-sync-fails-loud-on-ingest-failure.md)) | `tests/test_imap_sync_ingest_accounting.py::test_message_stage_failure_blocks_watermark_and_retries_failed_uid` (+ `test_attachment_sub_failure_still_advances_watermark`) |
| 6 | **Rebuild stops when the extraction ledger does not balance** — in the import phase of `bes rebuild`, when a PST's recipient extraction is judged incomplete (`recipient_extraction_complete=false`, e.g. enumeration did not finish, or message or recipient counts disagree), that PST fails outright and no checkpoint is recorded: continuing with incomplete extraction would stage a DB with silently wrong recipients that no later phase can detect; **it must not be downgraded to a WARNING to let a rebuild finish**. This entry pins only rebuild's reaction to that judgement; how the judgement is computed (the extraction ledger check in `outlook_pst.py`) is outside the anchor's scope (landed in `#180`, promoted in `#185`) | `tests/test_rebuild.py::test_import_phase_fails_when_recipient_extraction_is_incomplete` |
| 7 | **Outbound conversational authorisation (MCP form, [D-20260924-1](docs/decisions/D-20260924-1-mcp-send-binds-reviewed-draft.md))** — CoWork can send only a draft produced by `mail_draft_reply` whose content fingerprint matches at send time; a CLI draft cannot be sent through MCP; only the three whitelisted callers can directly trigger the core send step. A fingerprint mismatch is refused without writing the send-once marker. "Was the user actually asked" cannot be tested; it is the agent's conversational duty and outside this contract | `tests/test_mcp_send.py::test_mcp_send_fingerprint_mismatch_refused_without_marker` (+ `test_mcp_send_refuses_cli_drafts`, `test_submit_prepared_draft_caller_whitelist`) |

## Delivery flow

**Contract-Anchored Flow-Back** ([D-20260820-3](docs/decisions/D-20260820-3-migrate-to-contract-anchored-flowback.md)): intent lives in the issue, the behavioural canon lives in `## Behaviour contracts` above, and reconciliation happens before merge. One round looks like this:

1. **Issue brief**: what, why, and what counts as done; clear is enough, with no spec document. The full issue (comments included) is the intent baseline for every later review.
2. **`/implement`**: built-in TDD and code review; implementation converges here.
3. **Touches a contract entry or anchor test → PR + `/spec-reconcile`**; everything else follows the size rule in Hard rules.
4. **Squash merge**.

The Spectra-era capability specs and change archive predate 2026-08-20 and were not carried into this repo ([D-20261010-1](docs/decisions/D-20261010-1-showcase-ships-ahead-of-remoteization-in-a-fresh-repo.md) point 5); Spectra commands have nothing to act on here. When a per-round reconcile is too heavy, the step down is to reconcile several rounds together; reconciliation itself stays.

## Pull request delivery contract

The machine-readable block below is the only body of this contract; its field names are the acceptance semantics. Other agent documents point here rather than restating or copying it.

```json
{
  "pr_delivery_contract": {
    "pull_request_body": {
      "squash_message": {
        "heading": "## Squash message",
        "must_explain_why": true,
        "per_issue": {
          "closing_reference": "Closes #n",
          "must_explain_why": true
        }
      }
    },
    "completion": {
      "pull_request_state": "ready_for_review"
    }
  }
}
```

This repo allows only squash merges, so the squash message is the design rationale that survives once the per-round commits disappear; ready for review means Sean can be asked to authorise the merge, and it triggers the CI that runs only on ready PRs.

## Role markers in issue and PR comments

Put a role marker on the **first line** of every issue or PR comment, copied whole: ``> `Claude Code` `` for Claude Code, ``> `Codex implementer` `` for Codex. An unmarked comment has unknown authorship; ask. Full rule → [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md#留言的角色標記).

## Deployment and release

Global install, DB path resolution, the daily schedule, the scratch maildir, the release flow, and the pre-push guard → [`docs/global-install-and-release.md`](docs/global-install-and-release.md); read it before releasing, touching the schedule, or chasing "the global `bes` behaves differently from the repo".

- **Tagging never rebuilds**: `release.ps1` builds first, and only then does the guard let the tag through.
- **The global `bes` is a build snapshot**: `uv run bes` in the repo runs the working tree, while other projects' subagents run the last released wheel; a cross-project change takes effect only after a release. The MCP server uses `uv run --project` and follows the working tree; restarting Claude Desktop picks it up.
- **When query results look wrong, first confirm which DB was hit**: `--database` flag > `MAILATTIC_DB` > the hard-coded absolute default pointing at the repo root.

## Where to look first

Every entry here is a canonical location; globbing around them usually finds a stale copy.

| Your question | Go to |
|---|---|
| Does any document cover X? | [`docs/INDEX.md`](docs/INDEX.md) |
| Why was this chosen? Can it change now? | [`docs/decisions.md`](docs/decisions.md) → `docs/decisions/D-YYYYMMDD-N-*.md` (not `docs/adr/`). Check before changing an existing design; say so explicitly when an approach contradicts an ADR |
| What *should* the system do? | `## Behaviour contracts` + the code |
| What changed recently? | PRs / `git log`. There is deliberately no `CHANGELOG.md` ([D-20260609-1](docs/decisions/D-20260609-1-no-changelog-file.md); the change archive that decision named as the change-history canon was not carried into this repo) |
| What exactly does this term mean? | [`GLOSSARY.md`](GLOSSARY.md), the one glossary in the repo |
| Writing or reviewing code/tests: defect shapes that already bit this repo (derived-value diffs, parse/write inverses, ratio-test fixtures, test-data de-identification) | [`CODING_STANDARDS.md`](CODING_STANDARDS.md) |
| Deployment state, watchpoints, pending externals | `docs/current-status.md` (a local working file outside version control, [D-20261018-1](docs/decisions/D-20261018-1-internal-working-notes-stay-local.md); a fresh clone lacks it) |
| Picking up in-flight work; items waiting on Sean's ruling | All in GitHub issues + PRs (waiting items are labelled `ready-for-human`); `session-continuity.md` (maintained by `/session-park`) only lists ticket numbers as pointers |
| Issue / label operations | [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md) (`gh`, `sean3808/mailattic`), [`docs/agents/triage-labels.md`](docs/agents/triage-labels.md) |
| An external skill's (mattpocock series) instructions conflict with this repo | [`docs/agents/external-skill-routing.md`](docs/agents/external-skill-routing.md); read it before acting on the skill's text. Skills are used as published, never forked |
| Lessons waiting for promotion | `.claude/promote-candidates.md` (a local working file outside version control; `/lesson-lookup` checks here first) |
| Local query CPU spikes | Not necessarily this project (once it was the local antivirus real-time scan slowing DB access); diagnostic notes stay local, outside version control |

`docs/decisions.md` and `docs/INDEX.md` are generated; edit their sources instead: a new decision is a new file in `docs/decisions/`, and a harness hook regenerates the indexes. To update them immediately, run `uv run python scripts/gen_doc_index.py --target all`.

## Trigger→Resolution hot

Pitfalls hit repeatedly (hit ≥ 2), in a four-column pattern (Trigger symptom / Scope / Resolution / Negative signal), live in `docs/agents/trigger-resolution.md` (the session-park promote flow appends there). Claude Code loads it through the import on the next line; an agent that does not process `@` imports reads that file before touching git concurrency, long-running commands, guard tests, MCP tool parameters, tests inside a worktree, or a failed closeout gate.

@docs/agents/trigger-resolution.md

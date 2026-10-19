# MCP client probe loop

This is the advisory LLM-client verification layer above the deterministic MCP tests. It answers one question those tests cannot: after reading only the registered descriptions and schemas, does a low-tier model choose the intended tool and argument types?

The loop never replaces pytest, CI, or release checks. A valid probe is evidence about client behavior, not a repository gate.

## Current participation

- **Client A: Claude Code** — eligible through `scripts/probe_supervisor.py`, which owns a bidirectional stream session and withholds the target until the canary passes.
- **Client B: codex-cli 0.146.0** — excluded because `codex exec` accepts its prompt at launch and runs the turn to completion. The current supervisor cannot inspect a canary result before deciding whether to send the target. Missing inventory is not an exclusion reason.
- The resulting round is single-client evidence and MUST be reported as `comparison unavailable`, never agreement or absence of divergence.

## Production-data safety boundary

One supervisor invocation carries exactly one trial and has three ordered stages.

1. **Production-data launch-isolation preflight, before process creation.** The supervisor validates the exact filled configuration, absolute `uv.exe`, current checkout, manifest-bound fixture, server environment, sanitized child environment, output roots, fixed client arguments, and unique empty cwd. Failure creates no client and sends no provider traffic.
2. **Canary confirmation.** The supervisor creates the client and sends only its generated canary turn. Claude Code emits one `system/init` after each user turn; the supervisor treats a pre-content init as metadata only, requires the canary turn to establish a non-empty session identifier, and requires the target-turn init to repeat it. Its tool inventory and pending-server status are not safety evidence. A correlated non-error probe result counts as an answer, and only an exact returned subject counts as provenance.
3. **Target question.** The supervisor sends the fixed question only after the canary matches. Missing, echoed-only, malformed, or error results stop before this stage.

The fixed client arguments supply no user/project/local setting sources, add a probe-only system prompt, request no file or shell built-in tools, explicitly allow only the five known read-only tools on the configured probe MCP source, and disable session persistence. Empty setting sources exclude settings-defined hooks while preserving the explicit strict MCP config. The prompt is behavioral guidance, not containment. The command deliberately omits `--bare` so Claude Code can use subscription OAuth and does not use a bypass permission mode; credential reads and other implementation-defined non-bare discovery outside those setting sources remain accepted residuals. `launch_isolation_enforced` records only the supervisor-owned production-data preflight. It does not claim that the prompt constrained the client or that client output attested flag compliance. One pre-content `system/init` per turn is metadata when its session identifier remains stable. `rate_limit_event`, `system/thinking_tokens`, and `tool_progress` naming an exact allowlisted probe-server tool are ignored as evidence-neutral telemetry; progress for any other tool, changed session identity, and every other unexpected record, including a hook lifecycle or permission-denied event, fail closed.

## Valid trial rule

A trial enters the round table only when one supervisor row has all of:

```json
{
  "status": "complete",
  "launch_isolation_enforced": true,
  "probe_server_answered": true,
  "canary_matched": true
}
```

The values are independent:

- a correlated non-error request echo produces answered=`true`, matched=`false`;
- a correlated error result produces answered=`false`, matched=`false`, even when its payload repeats the canary;
- a target-stage failure retains successful canary evidence but does not have status `complete` and is not a trial result;
- process exit code, server-status text, model prose, and another invocation's evidence do not repair a missing value.

## Running a round

Run from the checkout under test in PowerShell. Use a new absolute fixture directory outside every repository worktree for each round.

```powershell
$env:PYTHONIOENCODING = 'utf-8'
```

Keep this binding in the PowerShell session for every direct Python invocation below.

### 1. Confirm the client authentication source

```powershell
claude auth status
claude -p --model haiku --tools "" --no-session-persistence 'Reply with exactly AUTH_OK and nothing else.'
```

The supervisor uses the same non-bare print-mode authentication path. The smoke must exit zero before generating a round fixture. It verifies only that the installed client can reach the provider with Haiku; fixture binding and database provenance remain separate supervisor and canary evidence.

### 2. Generate a fresh fixture

```powershell
$RoundId = Get-Date -Format 'yyyyMMdd-HHmmss'
$FixtureDir = "C:\IT_Projects\mailattic-probe-$RoundId"
uv run --locked --exact python scripts\build_probe_fixture_db.py --output-directory $FixtureDir
New-Item -ItemType Directory -Path (Join-Path $FixtureDir 'probe-records') | Out-Null
New-Item -ItemType Directory -Force -Path '.dev\probe-trials' | Out-Null
```

The generator creates `probe-fixture.db`, `probe-fixture.canary`, and `probe-fixture.manifest.json`. Do not edit, rename, or add SQLite sidecars after generation.

### 3. Fill the client A template

Parse the template as JSON and assign all three machine-specific values. Assigning the complete database path avoids treating its embedded fixture-directory placeholder as a standalone JSON value.

```powershell
$RepoRoot = (Resolve-Path '.').Path
$UvExecutable = (Get-Command uv -ErrorAction Stop).Source
$Template = Get-Content -LiteralPath '.dev\mcp-dev.json.template' -Raw
$Config = $Template | ConvertFrom-Json
$Config.mcpServers.'mailattic-mail-probe'.command = $UvExecutable
$Config.mcpServers.'mailattic-mail-probe'.args[2] = $RepoRoot
$Config.mcpServers.'mailattic-mail-probe'.env.MAILATTIC_DB = (Join-Path $FixtureDir 'probe-fixture.db')
$Filled = Join-Path $FixtureDir 'mcp-dev.json'
$Config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Filled -Encoding utf8 -NoNewline
```

There is no separate configuration-check command. The supervisor validates this exact filled copy immediately before creating the client.

### 4. Run one invocation per question and trial

Copy the exact prompt text for one `Q##` from `.dev/mcp-probe-questions.md`. Give every raw record a unique filename. The outcome destination can collect multiple supervisor rows for the round.

```powershell
$QuestionId = 'Q01'
$Trial = 1
$TargetQuestion = '找一下信件主旨或內文有「探針測試」的郵件，告訴我寄件者和日期。'
$RawRecord = Join-Path $FixtureDir "probe-records\$QuestionId-t$Trial.jsonl"
$OutcomeRow = Join-Path $RepoRoot ".dev\probe-trials\client-a-$RoundId.jsonl"
$ClaudeExecutable = (Get-Command claude -ErrorAction Stop).Source

uv run --locked --exact python scripts\probe_supervisor.py `
    --client-executable $ClaudeExecutable `
    --probe-config $Filled `
    --probe-server mailattic-mail-probe `
    --canary-file (Join-Path $FixtureDir 'probe-fixture.canary') `
    --target-question $TargetQuestion `
    --raw-record $RawRecord `
    --outcome-row $OutcomeRow `
    --model haiku
```

Run every question at least twice. Do not retry an invalid invocation under the same raw filename; the supervisor reserves raw output exclusively so run identity cannot be overwritten.

## Record layers

Each invocation has three evidence layers.

1. **Raw JSONL** — verbatim client output under the external fixture's `probe-records/`. It contains the returned canary and stays untracked outside the working tree.
2. **Supervisor outcome JSONL** — emitted under `.dev/probe-trials/`. These records stay on the local machine and are not version-controlled (ignored by `.gitignore`) so that trial artifacts remain strictly local and do not re-enter the repository. It contains status, session identifier, the three evidence booleans, and failure stage/reason; it contains no canary value.
3. **Observation row** — authored from the matching raw record. It carries session identifier, question identifier, client, trial number, selected tools, argument types, call outcome, and observed over-size behavior. It contains no canary value.

An observation without a matching complete supervisor outcome is excluded. Probe records stay local and are never version-controlled; search tracked content for the generated canary and stop if it appears.

The supervisor makes bounded attempts to remove its validated empty client cwd after each invocation. A persistent Windows sharing violation is reported as a warning and may leave that empty directory for later safe cleanup; it does not replace or suppress the already-classified outcome row.

## Question-set reachability

The question set is fixed and append-only. Q03 and Q06 target fixture shapes guaranteed to exceed the assembled response budget. Q07 targets the distinct `mail_search` over-budget rejection. Measurement through the server's assembly path found the current full-body Q07 shape reachable at 573,426 characters against the 120,000-character budget; summaries-only measured 7,550 characters. A valid trial still records what the model actually called rather than claiming the designed behavior occurred.

## Round record

The round document contains one row per question and both client A trials, preserves disagreement as instability, and excludes invalid outcomes. For this round it states:

- client A: participating;
- client B: excluded because no staged canary-before-target transport exists for `codex exec` 0.146.0;
- comparison: unavailable.

Tool-description explanations remain hypotheses until a controlled description change removes the observed behavior.

## CoWork closeout

Reopening Claude Desktop or CoWork is a closeout-only acceptance check, not a per-round step. The headless loop supplies iterative evidence; closeout confirms the long-running application still loads the final working-tree server configuration.

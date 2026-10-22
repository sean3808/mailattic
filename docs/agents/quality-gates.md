# Agent quality gates

The mechanical checks an agent runs to substantiate a completion claim. Each
one answers a single question, runs in seconds, and reports through an exit
code plus output you have to read.

This document is the canonical list. Run the command exactly as written — the
flags are part of the gate, not decoration.

## Commit-time preconditions

Commit-time parity requires the tracked hooks to be installed with:

```bash
uv run --locked --exact pre-commit install
```

The runtime running a gate must be able to execute `uv`, access a usable
project environment and its configured caches, and obtain any distribution
needed by the locked environment. A missing operational prerequisite is an
operational failure, not a checker finding. The tracked configuration declares
`minimum_pre_commit_version: "4.5.1"` so an older runner fails before reading
the hook definitions. The same tracked invocation and configuration apply
whether a commit is prepared from an AI agent or a plain terminal.

## Layered pytest routes

Run every route from the repository root through the canonical launcher,
`scripts\invoke-pytest.ps1`. It is the single public interface for agent
feedback, closeout, and release — do not hand-assemble the underlying
locked, exact `uv run` pytest invocation anywhere else. The launcher
resolves the repository root regardless of the caller's working directory,
points TEMP/TMP/TMPDIR/UV_CACHE_DIR/XDG_CACHE_HOME/PRE_COMMIT_HOME at
isolated leaves below the ignored `.project-runtime/` root, removes any
inherited `PYTEST_ADDOPTS` (pytest appends it to its own command line, so it
would otherwise extend the exact argument vector below), serializes
concurrent runs behind one cross-process lock (a second concurrent launch
exits 17 without touching uv or pytest output), and rejects a runtime root
whose volume cannot be established with exit 18 before uv or pytest ever
starts. See `scripts\project-tool-environment.ps1` for the shared
implementation.

### targeted

Run the explicit path(s) or node ID(s) named by the current task for a
focused regression check:

```powershell
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Targeted tests\test_agent_quality_gates.py::test_pytest_discovery_and_marker_policy_is_explicit
```

Multiple targets may be given as bare trailing tokens, or as several values
after a single `-Target` flag -- both forms are equivalent:

```powershell
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Targeted tests\test_a.py::test_one tests\test_b.py
```

```powershell
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Targeted -Target tests\test_a.py::test_one tests\test_b.py
```

The launcher classifies its own raw argument list itself, before any
declarative PowerShell parameter binding runs, so `-RuntimeRoot` never
captures a bare target token by accident and no placeholder flag is needed
to make targeting work. Only `-Route`, `-Target` and `-RuntimeRoot` are
recognized: any other token starting with `-` is rejected with exit 2
instead of being forwarded to pytest, and a `-Route` / `-RuntimeRoot` value
may not start with `-` either, so no caller can smuggle an extra pytest
option (`--basetemp=C:\...` above all) past the isolation contract or make
one disappear into a value slot. A target may not start with `@` either:
pytest expands `@file` as a response file, which would carry the same
options in indirectly.

### quick

Run during ordinary implementation feedback and self-review. It excludes
every audited `slow` and `release` node (`-m "not slow and not release"`), so
it is not release evidence:

```powershell
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Quick
```

Because Quick skips `tests/test_report_diff_coverage.py` (marked `slow`, #12),
a change to `scripts/report-diff-coverage.ps1` runs
`-Route Targeted tests\test_report_diff_coverage.py` before closeout.

### full

Run for medium/high closeout and release. It is authoritative and keeps all
marked nodes:

```powershell
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route Full
```

### full-sequential

Use as the reproducible fallback and for parallel-equivalence proof:

```powershell
pwsh -NoProfile -File scripts\invoke-pytest.ps1 -Route FullSequential
```

### full-coverage

Emits the `full` vector byte-for-byte and appends only coverage
instrumentation and reporting. Do not invoke it by hand: it requires a staging
directory and run descriptor that `scripts\report-diff-coverage.ps1` creates.
Use that script instead — see [Diff coverage evidence](#diff-coverage-evidence).

It takes a run **ID**, never a path. The launcher resolves the coverage
artifact root itself and derives `<runtime root>/coverage/<run id>` beneath it.
A caller-supplied path could not be validated: from one path the launcher
cannot tell which ancestor is the approved root, nor whether the directory
belongs to this run, to another run, or to a forged sibling.

`full` accepts the same run ID, and only for the same reason: proving that
instrumentation did not change test semantics requires comparing both runs'
collected test identities, so each run writes its machine-readable report into
its own staging directory. Evidence that lives at a name shared between runs
can be replaced between the run that wrote it and the read that consumes it —
the launcher releases the project pytest lock when its pytest child exits,
which is before the reporting script has read anything, so invocation
serialization says nothing about that read. The two routes are validated
differently: `full-coverage` writes first among evidence producers and
requires a freshly created directory holding nothing outside an enumerated
whitelist — the run descriptor, plus optionally one or both of the launcher
log files (`launcher-stdout.log`, `launcher-stderr.log`) that the reporting
script's spawn-time stream redirection creates — while `full` writes second
into a directory that legitimately already holds the instrumented run's output.
Because `full` cannot demand a whitelist-clean directory, it demands the next
best thing — its own report destination must not already exist. Both rules say
the same thing from each writer's position: what this run will read must not
predate this run. Those checks run under the pytest lock immediately before
launch, so no other launcher invocation fits between the check and the run it
guards, and they refuse rather than proceed when a status cannot be determined
at all. It does not claim to stop a third party writing into the staging
directory; nothing here does. A plain `full` invocation supplies no run ID,
produces no report, and is unchanged.

Coverage lives inside the launcher rather than beside it because coverage is
precisely the case where the measured population must be the execution the
release gate measures. A bypass would not merely weaken the single-entry-point
contract — it would invalidate the numbers.

## Choosing a route

An agent uses targeted for current-task regression, quick for ordinary
implementation feedback, full for medium/high closeout, full-sequential
for fallback and equivalence proof, and full-coverage only through
`scripts\report-diff-coverage.ps1`. `quick` is not release evidence, and no
route reproduces a raw canonical pytest command by hand outside the
launcher. Release invokes the same launcher with `-Route Full`; there is no
hidden sixth pytest variant. A new worker count requires a new immutable
benchmark and decision under the #37 contract.

Every route creates the seven managed leaves below one ignored
`.project-runtime/` parent (`python-temp`, `pytest`, `ledger-collection`,
`uv-cache`, `xdg-cache`, `pre-commit`, `locks`). Python and subprocess
temporary files use `python-temp`, while pytest's configured
`--basetemp=.project-runtime/pytest` uses the distinct `pytest` leaf. The
nested marker-ledger collection uses `.project-runtime/ledger-collection`;
all leaves below the root are intentionally distinct paths.

The exit-18 rejection is a "guard that cannot see MUST NOT wave the caller
through" check: a runtime root whose volume cannot be established at all is
rejected before any directory is created or any child process is launched.
Every UNC path is rejected this way, since a UNC final path name carries no
drive letter to check. A dangling reparse point (a junction whose target does
not exist) is rejected too, rather than walked past to a safe-looking parent,
because its target can come into existence later on any volume. This covers
the default root too: a `.project-runtime` directory or leaf that is itself a
dangling junction no longer slips through.

What this rejection does **not** do: it never asks whether a resolved path is
tracked by Git. Under the default configuration nothing lands in tracked
territory, because the root sits beneath the ignored `.project-runtime/` parent
— but that is a property of the default, not something the launcher enforces,
and there are two known ways past it.

`-RuntimeRoot` is itself a caller-supplied path. It is checked for one thing only
— that its volume can be established at all — so an override naming a location
inside the repository is accepted, and every managed leaf, including the coverage
artifacts, follows it there. Separately, a reparse point planted at the runtime
root is not detected as such: the approved root and everything derived from it
resolve through that same link, so the containment comparison still matches.

Neither is reachable by accident: one requires passing the override deliberately,
the other requires write access inside the repository already. Both apply to
basetemp and every other managed leaf equally — they belong to the runtime-root
contract as a whole, not to any one route.

`-RuntimeRoot` moves pytest's own basetemp with it. The five canonical routes
pass no override and keep their exact argument vectors; under an explicit
override the launcher appends `--basetemp=<runtime root>/pytest`, because
that one path comes from the static `addopts` in `pyproject.toml` rather than
from an environment variable. The tracked `addopts` value is unchanged, so it
still governs any pytest run started outside the launcher.

Known boundary of `-RuntimeRoot`: the pytest lock lives on the `locks` leaf
below the runtime root, so two launches using *different* runtime roots take
different locks and can run at the same time. Do not run concurrent launches
with differing `-RuntimeRoot` values. No canonical route, the release script,
or any documented workflow passes the flag; only the test suite does.

## Diff coverage evidence

Answers one question at closeout: **which executable lines and branches that
this change added or modified were never exercised by a test?** It is not a
global percentage chase and it does not replace a spec's acceptance tests.

```powershell
pwsh -NoProfile -File scripts\report-diff-coverage.ps1 `
  -RepositoryRoot . `
  -ChangeName <spectra-change-name> `
  -R <full sha of local origin/main> `
  -B <full sha immediately before the change> `
  -T <full sha of the committed change> `
  -DeclaredCommitSetManifestPath <path>   # or -DeclaredCommitSet <one sha>
```

Spell that last argument in full — `-DeclaredCommit...` is an ambiguous prefix
of the two parameter names, and PowerShell rejects it rather than guessing.

**A change of more than one commit must use the manifest form.** Under
`pwsh -File`, `-DeclaredCommitSet` reliably carries a single SHA and no more:
several bare SHAs cannot bind to one array parameter, and a comma-joined string
arrives as one argument. That is not much of a loss — a manifest is the better
provenance story regardless, since it is reviewed on its own and the run records
its hash next to the set it supplied.

Report-only: **no `fail_under`, no `--fail-under`, and no percentage of any
kind changes the exit status.** A test failure, a tool failure, a missing or
malformed XML, a path mismatch, an unresolved commit, or snapshot drift is
still a non-success — those mean the measurement is invalid, which is a
different thing from the measurement being unflattering.

### Two numbers that are not the same number

The run produces two reports and they answer different questions.

`R...T` is **cumulative stack evidence** whenever local `main` sits above
`origin/main` — it spans every commit in that stack, not just this change.
Its percentage must never be quoted as this change's diff coverage.

`B...T` is the **change-attribution** report, and it is valid only when the
commit set actually reachable in `B...T` exactly equals a *declared* set you
supply separately. That separation is the whole point: if the expected set
were derived from `B...T` too, the comparison would always pass and a stacked
or unrelated commit would ride along unnoticed. The manifest keeps the
declared set, the computed set, and the hash of the declared set's source as
three distinct fields so a reviewer can see what was claimed, what was
measured, and where the claim came from.

Global coverage appears in the terminal output as **health context only**. It
is not a baseline and not a threshold.

### An empty report is not good news

`diff-cover` prints "No lines with coverage information in this diff" both
when the range genuinely changed no production code *and* when the coverage
data failed to line up with the paths Git reports. Those look identical and
mean opposite things — the second one means nothing was measured at all,
while reading as though everything was covered.

The script distinguishes them by independently checking whether the range
touches `src/` before interpreting the result, and an empty production diff is
reported as empty rather than as 100%. If you ever find yourself reading an
empty report by hand, apply the same test before believing it.

### Classifying what comes back

Every uncovered changed line or partial branch gets exactly one of:

- a test was added;
- a reviewed, reasonable exclusion — recorded with its rationale in the
  evidence ledger, never suppressed in configuration; or
- an explicit follow-up risk with a named owner.

Do not write low-value tests purely to cover defensive or unreachable
branches. Results are appended to
[`docs/agents/coverage-evidence.md`](coverage-evidence.md); no threshold
decision happens until that ledger holds three qualifying rows.

## The exit-code contract (shared by every gate here)

**Exit 0** means the gate passed cleanly.

**A non-zero exit means the gate did not pass.** It does *not* mean the gate
found a defect. The same code comes back when the gate could not run properly
at all:

- the lockfile is stale and `--locked` refused to rewrite it
- an external service was unreachable
- the configuration is malformed

**Read the output to tell which happened.** Nothing in the exit code
distinguishes them, and treating every non-zero exit as a finding turns an
operational failure into a phantom defect blamed on whatever change is in
flight.

## The gates

### Dependency contract

```bash
uv run --locked --exact deptry src
```

**Question:** do the source imports under `src/` agree with the dependency
declarations in `pyproject.toml`?

**Network:** not required. Fully local.

**Findings:** an import-site finding names the file, the line, the rule code
and the package. A declaration-side finding names `pyproject.toml` and the
package — a declaration nothing imports has no import site to point at.

**Scope is `src/` on purpose.** `scripts/`, `docker/` and `skills/` import the
first-party package, and without configured first-party roots a wider scan
buries the real findings under false `DEP001 mailattic` noise.
Widening the scope means configuring those roots first, not just dropping the
path argument.

**Calibration lives in `pyproject.toml`, not on the command line.**
`[tool.deptry]` maps `beautifulsoup4` to its `bs4` module and ignores `tzdata`
under `DEP002` alone — that one is a data-only distribution the stdlib
`zoneinfo` consumes on Windows, so no import statement can ever mention it.
Both entries carry their reason next to them. There is no blanket rule ignore,
and adding one would end the gate.

**This gate does not check version bounds.** A declaration with an open ceiling
passes exactly as well as a bounded one. If you need to confirm bounds, read
the specifier that reached package metadata:

```bash
uv run --no-sync python -c "import importlib.metadata as m; print(m.requires('mailattic'))"
```

### Ruff lint

The canonical lint commands use the Ruff resolved by `uv.lock`:

```bash
uv run --locked --exact ruff check .
uv run --locked --exact ruff check --fix --exit-non-zero-on-fix .
```

**Question:** does the repository pass the configured Ruff lint rules, and can
the fix form repair the findings it knows how to repair?

**Network:** not normally required after the locked project environment and
its caches are available. A missing distribution may cause `uv` to download
it; that is an operational prerequisite, not a Ruff finding. `--locked` keeps
the command from rewriting `uv.lock`, and `--exact` prevents extra installed
packages from changing the measured environment.

**Findings and mutation:** the first command checks the repository-wide `.`
without changing checked source. The second command may modify Python, PYI,
and Jupyter files, and uses `--exit-non-zero-on-fix` so a repair is still a
failed gate until the result is reviewed and staged. Both commands honor the
central exclusions in `ruff.toml`: `skills/` and
`scripts/gen_doc_index.py`. The pre-commit lint hook additionally uses
`--force-exclude` because pre-commit supplies explicit filenames.

The shared exit-code contract applies: exit 0 is a clean pass; non-zero means
the gate did not pass and the output must distinguish lint findings from an
unavailable `uv`, environment, cache, distribution, or malformed
configuration.

### Ruff format

The canonical formatting commands are:

```bash
uv run --locked --exact ruff format .
uv run --locked --exact ruff format --check .
```

**Question:** is the repository in the repository-owned Ruff format, and, when
requested, can Ruff apply that format consistently?

**Network:** not normally required once the locked environment and caches are
usable. `uv` may synchronize `.venv` and write its/Ruff's caches, and a missing
distribution may require a package download. These are operational side
effects; `--locked` does not permit a lockfile rewrite and `--exact` keeps the
runtime aligned with the lock.

**Findings and mutation:** `ruff format .` mutates the allowed Python, PYI,
and Jupyter files. `ruff format --check .` only checks the repository-wide
source and does not modify checked source or tracked repository files; it
returns non-zero when formatting drift exists. Both commands inherit the
central `ruff.toml` exclusions for `skills/` and
`scripts/gen_doc_index.py`.

Commit-time enforcement requires the tracked hooks to be installed with:

```bash
uv run --locked --exact pre-commit install
```

The Ruff lint and format hooks are local, run the locked project Ruff, use
`--force-exclude`, and are restricted to `stages: [pre-commit]`; Ruff does not
run as a mutating pre-push gate. The tracked configuration requires
`minimum_pre_commit_version: "4.5.1"`, so an older runner fails before
interpreting the hooks. The runtime must be able to execute `uv` with a usable
project environment, configured caches, and the required distributions.

The format hook is intentionally mutating. If pre-commit can restore a
partially staged file's unstaged stash cleanly, a format fix remains on disk,
the hook fails because it modified the file, and the contributor must review
and restage it. If stash restoration conflicts with the formatter's fix,
pre-commit reports the conflict, rolls the fix back, restores the original
staged and unstaged edits, and fails; reconcile the split before retrying.

### MCP boundary types

The canonical human-readable and machine-readable commands are:

```bash
uv run --locked --exact pyright --pythonpath .venv/Scripts/python.exe --warnings
uv run --locked --exact pyright --pythonpath .venv/Scripts/python.exe --warnings --outputjson
```

**Question:** do the two production MCP boundary modules satisfy the locked
Pyright checker with no errors or warnings?

**Scope:** only `src/mailattic/mcp_server.py` and
`src/mailattic/mcp_delivery.py`. The `[tool.pyright]` `include`
list in `pyproject.toml` is the single scope authority. All other production
modules under `src/` and the entire test tree remain outside this gate; a clean
result is not a repository-wide type-safety claim.

**Network:** not normally required after the locked environment and caches are
available. A stale lockfile, unavailable locked Pyright distribution, missing
`.venv/Scripts/python.exe`, malformed configuration, or checker startup failure
is an operational failure rather than a clean result or source diagnostic.

**Findings:** both commands retain `--warnings`, so every error or warning
returns non-zero. The human route prints diagnostics for review. JSON consumers
must check the process exit code and parse both `summary.errorCount` and
`summary.warningCount`; they must not interpret empty, missing, or unparsable
stdout from a non-zero process as zero diagnostics. A diagnostic supplies its
file, severity, message, start/end range, and rule when Pyright assigns one;
the summary also reports the number of analyzed files and elapsed time.

**Suppression and configuration policy:** `[tool.pyright]` contains only
`include`, `pythonVersion`, `pythonPlatform`, and `typeCheckingMode`. Do not add
`ignore`, `exclude`, diagnostic-rule overrides, shared virtual-environment
settings, inline or file-level ignore directives, or `Any` merely to clear a
finding. Do not add `pyrightconfig.json`: it has higher precedence and would
replace the reviewed TOML authority. Narrow dynamic values using their producer
or framework contract; every unavoidable cast needs an adjacent evidence
comment.

**Expansion rule:** keep this gate explicit and outside pre-commit. A reviewed
change may add a high-change or high-risk production boundary only after it
captures that boundary's locked-checker baseline, clears the diagnostics
without blanket suppression, and updates both `[tool.pyright].include` and this
document in the same change.

### Known vulnerabilities

```bash
uv run --locked --exact pip-audit --skip-editable
```

**Question:** does the locked dependency set contain packages with published
advisories?

**Network:** yes — this gate consults an online advisory service.

**Findings:** package, installed version, advisory identifier, and the fixing
version when the advisory data supplies one. A vulnerability with no released
fix is still reported; it just has no fixing version to name.

**`--skip-editable` is not optional.** The first-party package is installed
editable and is not published, so it cannot be audited either way. With the
flag it is skipped as `distribution marked as editable`; without it the same
skip is reported as an unresolvable PyPI lookup, which reads like a failure.
The skip does not affect the exit code in either direction.

**Never add `--strict`.** It promotes any skipped distribution into a failure,
which makes this gate fail permanently on the first-party package and teaches
the next reader that a non-zero exit here means nothing.

**A clean result does not prove a live query happened.** `pip-audit` reuses an
HTTP cache, so a run with no connectivity can complete against cached advisory
data and report clean. The pass is only as fresh as the cache.

**A newly failing audit on an unchanged commit is new information, not a
regression.** The external advisory dataset changes independently of this
repository. When this gate turns red without a code change, the finding is
about a dependency that was already there — do not attribute it to the change
under review.

**This gate is not a release precondition.** `scripts/release.ps1` stays
runnable while the advisory service is unreachable; a release must not become
impossible because someone else's host is down. Run this gate during dependency
updates and at closeout instead.

### Commit blocklist guard

```bash
uv run python scripts/check_commit_blocklist.py
```

**Question:** does the staged commit (file paths or added content lines) contain any real domain or address literal from the local commit blocklist?

**Network:** not required. Fully local.

**Configuration:** point the `MAILATTIC_COMMIT_BLOCKLIST` environment variable at a UTF-8 text file containing one blocked literal per line (blank lines and lines starting with `#` are ignored; matching is case-insensitive substring). The blocklist file must live outside the repository or be gitignored; there is no default path inside the repository.

**Pre-commit hook:** registered as `check-commit-blocklist` in `.pre-commit-config.yaml` (`stages: [pre-commit]`, `always_run: true`, `pass_filenames: false`). It runs automatically on every commit attempt. If `MAILATTIC_COMMIT_BLOCKLIST` is unset, points to a missing file, points to an unreadable file, or points to a file with zero usable lines, the hook fails closed and refuses the commit.

**What to do when it refuses:**

- **Configuration failure (unset / missing / unreadable / empty list):** set `MAILATTIC_COMMIT_BLOCKLIST` in your environment to the path of a readable UTF-8 blocklist file containing at least one usable entry.
- **Blocklist match in staged changes:** the hook outputs the file path, line number, and blocklist line number without printing the matched literal itself (preventing private identifiers from leaking into logs). Inspect the reported locations, replace the real domains or addresses with anonymized synthetic values, and restage before re-committing.

## Why the invocations look like that

**`--locked`** stops `uv run` from quietly updating `uv.lock` when it considers
it stale. Without it, a gate can repair the very state it is measuring and then
report the repaired state as clean. With it, a stale lock is a loud failure —
fix it with `uv lock` as a deliberate, separately reviewable act.

**`--exact`** makes the environment match the lock rather than merely contain
it. Without it the audited set can be a superset of the locked set, which is
not the set anyone asked about.

**`uv run` rather than an ephemeral tool runner** pins both the checker version
and what the checker can see. Both matter. During this capability's own
implementation, deptry 0.25.1 classified one missing `urllib3` declaration as
`DEP001` when run through an ephemeral runner and as `DEP003` when run inside
the project environment — the ephemeral environment has no `urllib3` installed
and cannot tell that `requests` supplies it. Same tool, same version, same
defect, two rule codes. An agent recording one and a reviewer reproducing the
other would not be comparing like with like.

## What these gates do and do not touch

The dependency and vulnerability checks do not modify tracked files. The Ruff
check forms do not modify checked source; the Ruff fix and format commands and
the commit-time Ruff hooks can modify allowed Python, PYI, and Jupyter files.
All gates may synchronize the project virtual environment to match the lock
and write to tool caches. `--locked` guarantees that synchronization never
rewrites `uv.lock` itself. The protected `skills/` and
`scripts/gen_doc_index.py` paths remain outside every mutating Ruff gate.

## What GitHub Actions adds, and what it does not

`.github/workflows/ci.yml` has two event classes. The `static` and `package`
jobs run on `windows-latest` for every non-draft pull request, every push to
`main`, and on manual dispatch. They exist for the one thing a local run cannot
establish: whether *this commit*, checked out clean with no local cache, no
`.venv`, and none of the gitignored vendored files, is complete and
installable. The weekly schedule does not repeat either job.

The `static` job syncs the locked environment and then runs the check forms of
the gates above — Ruff lint, Ruff format, the dependency contract, and the MCP
boundary types — in their canonical form, followed by a `git status
--porcelain` assertion that the working tree is still clean. That last step is
the one with no local equivalent: a gate that repaired a file without the
repair being committed shows up here and nowhere else.

The `package` job builds the wheel, installs *the built wheel* as a uv tool,
and starts both console scripts. A `[tool.hatch]` packages list that omits a
module fails here rather than during a release.

The `hosted-full` job is a supplemental environment-compatibility probe. It
runs only from the weekly schedule (`23 18 * * 0`, Monday 02:23 Asia/Taipei)
or manual dispatch, and calls the same canonical launcher with `-Route Full`.
A scheduled run executes only `hosted-full`; a manual dispatch intentionally
runs all three jobs for the selected ref. Pull requests and pushes never make
`hosted-full` eligible.

This cadence bounds automatic usage at four or five automatic Full runs per
month. Using issue #62's measured estimate of 12–16 Windows allowance minutes
per Full run, that is approximately 48–80 allowance minutes per month before
manual runs. Manual dispatch is reserved for changes whose risk specifically
concerns Python, PowerShell, the launcher, runtime isolation, or hosted Windows
behavior.

The two green signals have different meanings:

- Green `static` plus `package` means the recorded commit installs and starts
  on a clean Windows host.
- Green `hosted-full` means the recorded commit completed the canonical Full
  route on that GitHub-hosted Windows runner. It does not replace local Full
  evidence for merge readiness or release, and says nothing about a later
  commit.

Both test failures and operational failures leave `hosted-full` non-successful,
but they are not the same diagnosis. A launcher rejection, unavailable tool,
or GitHub failure to acquire a hosted runner is an operational failure, not a
test verdict or a defect attributed to the checked commit. Read the output
before classifying any red run.

Neither green signal means anything more than stated above, and in particular:

- **The advisory gate is not run.** Its dataset changes independently of this
  repository, so it would turn red without a code change.
- **Nothing about the production database is exercised.** That the globally
  installed `bes` works against the real database is proven only by
  `scripts/release.ps1` stages 3-4; see
  [`docs/global-install-and-release.md`](../global-install-and-release.md).

No status check is required for merge: this repository is a GitHub Free
repository, where branch protection and rulesets are not available. CI is
advisory signal that a human reads, not a gate that blocks.

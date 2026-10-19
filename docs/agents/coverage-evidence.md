# Coverage evidence ledger

Schema version: 1

The durable, canonical record of change-level diff-coverage observations. It
lives here rather than inside any archived change directory: an archive is
immutable history and must not become an append target.

How rows are produced: [`quality-gates.md > Diff coverage evidence`](quality-gates.md#diff-coverage-evidence).

## What this ledger is for

Coverage is **report-only** today. No threshold, no `fail_under`, no configured
exclusion. This ledger exists so that a future decision about whether any
threshold is justified can be made from real observations instead of intuition.

**Three later completed Spectra changes must each contribute a valid row before
that decision may be proposed.** The third qualifying row authorizes a separate,
reviewed Spectra proposal to *evaluate* the question under
#40 — it does
not automatically set a threshold, alter closeout status, or pre-authorize any
outcome.

## What must not enter this ledger

- **The tooling qualification run performed by #75.** It measured the toolchain,
  not a change, and it is explicitly not one of the three.
- **Any number from the earlier moving-worktree feasibility run** (the 87% / 84%
  figures). Production code and tests were being edited by another writer
  between execution and analysis, so those percentages and missing-line lists
  describe no single state of the repository.
- **A cumulative `R...T` percentage presented as one change's coverage.** If
  attribution could not be proven, the row records that it could not be proven.
- **A partially written run.** A run directory without its completion marker is
  not evidence.

## Row schema

Every field is required. A row missing any field is not a qualifying row.

| Field | Meaning |
|---|---|
| `change` | Spectra change name |
| `tracker` | GitHub issue for the change |
| `R` / `B` / `T` | Full commit SHAs: local `origin/main` ref at measurement start; reviewed commit immediately before the change; immutable committed target |
| `merge_base_RT` / `merge_base_BT` | Full SHAs |
| `declared_commits` | The independently reviewed commit allowlist |
| `computed_commits` | The set actually reachable in `B...T` |
| `declared_source_sha256` | Hash of where the declared set came from |
| `attribution` | `proven` or `not-proven`, with the reason when not proven |
| `snapshot_hashes` | Source, tests, config, and dependency hashes at start and end |
| `tool_versions` | Locked pytest-cov / coverage / diff-cover, plus Python and uv |
| `commands` | Exact commands run |
| `global_branch_coverage` | Health context only |
| `changed_lines` / `missing_lines` | Change-level result, with file and line detail |
| `partial_branches` | Parsed from the target-bound coverage XML, never inferred from a line percentage |
| `classification` | Per uncovered line or branch: `test-added`, `reviewed-exclusion` (with rationale), or `follow-up-risk` (with owner) |
| `tests_added` | Whether tests were added |
| `tests_found_defect` | Whether those tests found a real defect — the field that tells us whether this exercise is worth its cost |

## Qualifying rows

**2 of 3.**

Rows are appended by later completed changes. The #75 tooling qualification is
not one of the three.

<!-- Append one section per qualifying row below, newest last. -->

### Row 1 — fix-coverage-staging-log-whitelist

First true end-to-end pass of the pipeline (the run that proved #87 fixed).
The change contains no `src/` production code — the diff-coverage result is a
genuine empty production diff, reported as empty rather than as a percentage.

| Field | Value |
|---|---|
| `change` | fix-coverage-staging-log-whitelist |
| `tracker` | #87 |
| `R` | `4fb2410d9ed3e60741b93293ddf60f91a6739d27` (local `origin/main` at start) |
| `B` | `4fb2410d9ed3e60741b93293ddf60f91a6739d27` |
| `T` | `af8cf6718c1f874b10b27b6282f9edd17d448a13` |
| `merge_base_RT` / `merge_base_BT` | `4fb2410d9ed3e60741b93293ddf60f91a6739d27` (both) |
| `declared_commits` | `af8cf6718c1f874b10b27b6282f9edd17d448a13` (single commit, `-DeclaredCommitSet` parameter) |
| `computed_commits` | `af8cf6718c1f874b10b27b6282f9edd17d448a13` — equals declared set for both `B...T` and `R...T` |
| `declared_source_sha256` | `aadf48d72f882938aca4ae6e5aadf90d18456e276af838e51ebab3c1a9e2dff9` |
| `attribution` | proven (`attributionValidBT: true`; `R...T` label `single-change`) |
| `snapshot_hashes` | start HEAD = end HEAD = `T`, clean at both ends (script drift check passed); tool/config hash `e03132c70cc1`; per-artifact SHA-256 in the run manifest (`runId f88bc58ea9f10892422cb19aec373936`, `manifest.json` + `MANIFEST_COMPLETE` present) |
| `tool_versions` | pytest 9.0.3, pytest-cov 7.1.0, coverage 7.15.3, diff-cover 10.4.1, Python 3.13.12, uv 0.11.29, git 2.55.0.windows.3 (all `--locked --exact`) |
| `commands` | The four commands recorded in the run manifest: launcher `-Route FullCoverage -CoverageRunId f88bc58e…`, launcher `-Route Full -CoverageRunId f88bc58e…`, and the two `diff-cover` invocations against `refs/report-diff-coverage/f88bc58e…/{R,B}` |
| `global_branch_coverage` | 0.7982 (line 0.8905) — health context only |
| `changed_lines` / `missing_lines` | 0 / 0 — empty production diff (`B...T` touches `scripts/`, `tests/`, `docs/`, `openspec/` only; `measurableProductionPathsBT` empty) |
| `partial_branches` | none (`changedPartialBranchesBT: []`, parsed from the target-bound coverage XML) |
| `classification` | not applicable — no uncovered changed line or branch exists |
| `tests_added` | yes — 7 launcher regression tests (whitelist boundary, exact-name near-miss, lock-ordering) |
| `tests_found_defect` | yes — run red against the pre-change gate they reproduced #87's deterministic failure (8 failures), which is the defect this change fixes |

Instrumented and uninstrumented runs both: 1223 passed / 0 failed;
collection-identity and instrumentation-semantics checks matched.

### Row 2 — fix-thread-resolver-out-of-range-identifier

First row with a non-empty production diff: the shared thread-identifier
resolver's magnitude gate plus its CLI and MCP entry-point mappings.

| Field | Value |
|---|---|
| `change` | fix-thread-resolver-out-of-range-identifier |
| `tracker` | #57 |
| `R` | `1f488792bbcc519f8c0814ac542233c40bf2b2ec` (local `origin/main` at start) |
| `B` | `1f488792bbcc519f8c0814ac542233c40bf2b2ec` |
| `T` | `6d8a1329181e208b068c7023e0a3822bcb733227` |
| `merge_base_RT` / `merge_base_BT` | `1f488792bbcc519f8c0814ac542233c40bf2b2ec` (both) |
| `declared_commits` | `6d8a1329181e208b068c7023e0a3822bcb733227` (single commit, `-DeclaredCommitSet` parameter) |
| `computed_commits` | `6d8a1329181e208b068c7023e0a3822bcb733227` — equals declared set for both `B...T` and `R...T` |
| `declared_source_sha256` | `21ec14eb8989fff5504d621e0dba274c35172e06f1fc801b198c200556fb2012` |
| `attribution` | proven (`attributionValidBT: true`; `R...T` label `single-change`) |
| `snapshot_hashes` | start HEAD = end HEAD = `T`, clean at both ends; per-artifact SHA-256 in the run manifest (`runId 0da41405e5cf858eea89e9eb20d3b689`, `manifest.json` + `MANIFEST_COMPLETE` present) |
| `tool_versions` | pytest 9.0.3, pytest-cov 7.1.0, coverage 7.15.3, diff-cover 10.4.1, Python 3.13.12, uv 0.11.29, git 2.55.0.windows.3 (all `--locked --exact`; same locked set as Row 1) |
| `commands` | The four commands recorded in the run manifest: launcher `-Route FullCoverage -CoverageRunId 0da41405…`, launcher `-Route Full -CoverageRunId 0da41405…`, and the two `diff-cover` invocations against `refs/report-diff-coverage/0da41405…/{R,B}` |
| `global_branch_coverage` | 0.7996 (line 0.8912) — health context only |
| `changed_lines` / `missing_lines` | 45 / 0 — `B...T` production diff spans `src/mailattic/{query,cli,mcp_server}.py` (3 files); diff coverage 100% |
| `partial_branches` | none (`changedPartialBranchesBT: []`, parsed from the target-bound coverage XML) |
| `classification` | not applicable — no uncovered changed line or branch exists |
| `tests_added` | yes — 14 resolver gauntlet tests (red-first with per-case baseline probes), 4 MCP mapping tests, 3 CLI exit-code tests |
| `tests_found_defect` | yes — run red against the unfixed resolver they reproduced #57's unclassified `OverflowError` / `ValueError` crashes and the pre-gate `FileNotFoundError` ordering, which are the defects this change fixes |

Instrumented and uninstrumented runs both: 1247 passed / 0 failed;
collection-identity and instrumentation-semantics checks matched.

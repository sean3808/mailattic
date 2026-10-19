<#
.SYNOPSIS
  Canonical pytest launcher: one serialized entry point for every documented
  pytest route (targeted, quick, full, full-sequential, full-coverage).

.DESCRIPTION
  This is the single public interface agents, docs, and release orchestration
  must use to run pytest. It:

  1. Validates -Route/-Target before touching anything else (invalid
     combinations exit 2 before any environment mutation, lock acquisition,
     or child process launch).
  2. Resolves the repository root from this script's own location, so the
     launcher behaves the same regardless of the caller's working directory.
  3. Maps -Route to the exact accepted `uv run --locked --exact pytest`
     argument vector -- these MUST stay byte-for-byte identical to the
     documented contract, which is versioned, not frozen forever: version 2
     (#75's report-only diff coverage change) added one fixed --junitxml
     element to the Full and FullCoverage tails only, so both routes produce
     comparable (nodeid, outcome) evidence. Version 3 (#75 round 4) removes
     that element from Full's DEFAULT vector again: Full writes a junit
     report only when given -CoverageRunId, and then it lives in that run's
     own staging directory, never at a shared fixed path -- that report has
     exactly one reader (scripts/report-diff-coverage.ps1's comparison
     read), and a writer with no reader is exactly the shared-path shape
     this version exists to eliminate. A plain Full invocation with no
     -CoverageRunId is therefore identical to version 1 (before #75), not to
     version 2. Every other element of every route is unchanged. An
     explicit -RuntimeRoot also appends a basetemp argument pointing at that
     root's pytest leaf (see the -RuntimeRoot note below).
  4. Delegates to Invoke-InProjectToolEnvironment (scripts/project-tool-environment.ps1),
     which resolves/validates the runtime root, creates the seven managed
     runtime leaves, acquires the cross-process pytest lock, points
     TEMP/TMP/TMPDIR/UV_CACHE_DIR/XDG_CACHE_HOME/PRE_COMMIT_HOME at their
     leaves, launches uv, and restores the caller's CWD/environment
     afterward regardless of outcome.
  5. Propagates the child's exit code unchanged.

.PARAMETER Route
  One of Targeted, Quick, Full, FullSequential, FullCoverage.

  FullCoverage emits the Full vector byte-for-byte and appends only coverage
  instrumentation and reporting options. It exists so coverage measures the
  same execution the release gate measures; a bypass would not merely weaken
  the single-entry-point contract, it would invalidate the numbers.

.PARAMETER CoverageRunId
  Required with -Route FullCoverage; optional with -Route Full; rejected for
  every other route. A run ID, NOT a path. The launcher resolves the coverage
  artifact root itself from the repository-owned runtime-root configuration
  and derives the staging directory by joining this ID beneath it. A
  caller-supplied path could not be validated: from one path the launcher
  cannot tell which ancestor is the approved root, nor whether the directory
  belongs to this run, another run, or a forged sibling.

  FullCoverage and Full are the two routes that participate in one comparison
  run, so both accept this grant -- but they validate the derived staging
  directory differently. FullCoverage is the first writer: the directory must
  be freshly created and hold only an enumerated whitelist -- exactly one
  run-descriptor.json (required) plus, optionally, launcher-stdout.log and/or
  launcher-stderr.log, the two redirection files the calling entry point
  necessarily creates before this launcher process starts. Full runs second,
  into a directory that legitimately already holds FullCoverage's output, so
  it verifies the volume/reparse decision, path containment, and the run
  descriptor's identity, but does not require the directory to be otherwise
  restricted to that whitelist. A plain -Route Full invocation that supplies
  no -CoverageRunId writes NO junit report at all: that report exists solely
  for scripts/report-diff-coverage.ps1's own comparison read, which always
  supplies a run ID, so an unbound Full invocation has no reader to produce
  one for.

.PARAMETER Target
  One or more explicit paths or pytest node IDs. Required when -Route is
  Targeted; rejected for every other route. All three of these invocation
  shapes are supported: `-Target onePath`, `-Target pathA pathB` (one flag,
  several bare values), and bare trailing tokens with no -Target flag at
  all (`-Route Targeted pathA pathB`). An unrecognized token starting with
  `-` is rejected with exit 2 rather than forwarded to pytest.

.PARAMETER RuntimeRoot
  Optional absolute override for the project runtime root, for a future
  checkout whose repository volume is unsuitable. When given, pytest's own
  basetemp moves with it: the launcher appends an explicit
  `--basetemp=<runtime root>/pytest`, because that one path comes from the
  static repo-relative addopts rather than from an environment variable the
  helper can point elsewhere. See scripts/project-tool-environment.ps1 for
  the safety check this triggers.
  Known boundary: pytest's cache directory does NOT move with it. The tracked
  `cache_dir` in pyproject.toml is repo-relative and no override is appended
  for it, so an explicit -RuntimeRoot still leaves cache metadata under
  <repo>/.project-runtime/pytest-cache. Recorded rather than fixed, because
  the situation this override exists for -- a repository volume unsuitable
  for test I/O -- was retired along with the system-drive guard on 2026-08-21
  (docs/decisions/D-20260821-1-retire-system-drive-hardware-guard.md), and the
  cache is rebuildable state rather than evidence. Should that situation
  return: declare an eighth managed leaf `pytest-cache` in
  scripts/project-tool-environment.ps1 and append `-o cache_dir=<that leaf>`
  next to the basetemp argument, with the leaf-distinctness contract test
  extended to cover it.
  Always addressed by an explicit -RuntimeRoot flag; it never captures a
  positional/bare argument (see the parsing note below).

.EXAMPLE
  pwsh -NoProfile -File scripts/invoke-pytest.ps1 -Route Targeted tests/test_invoke_pytest.py

.EXAMPLE
  pwsh -NoProfile -File scripts/invoke-pytest.ps1 -Route Targeted tests/test_a.py::test_one tests/test_b.py

.EXAMPLE
  pwsh -NoProfile -File scripts/invoke-pytest.ps1 -Route Targeted -Target tests/test_a.py::test_one tests/test_b.py

.EXAMPLE
  pwsh -NoProfile -File scripts/invoke-pytest.ps1 -Route Quick

.EXAMPLE
  pwsh -NoProfile -File scripts/invoke-pytest.ps1 -Route Full

.EXAMPLE
  pwsh -NoProfile -File scripts/invoke-pytest.ps1 -Route FullSequential

.EXAMPLE
  # Callers do not invoke this route by hand: the staging directory and its run
  # descriptor must already exist. scripts/report-diff-coverage.ps1 creates both
  # and then calls this.
  pwsh -NoProfile -File scripts/invoke-pytest.ps1 -Route FullCoverage -CoverageRunId a1b2c3d4

.OUTPUTS
  Exits with the child pytest process's exit code on a normal run. Exits 2
  for an invalid -Route/-Target/-CoverageRunId combination (rejected before
  any tool launch). Exits 17 on pytest-lock contention and 18 for an unsafe
  (unestablishable-volume or reparse-escaping) runtime root or coverage
  artifact path, or -- for Full only -- when its own full-junit.xml
  destination's occupancy cannot be verified (a permission denial, a
  disconnected volume, or any other I/O error querying the entry; distinct
  from "already exists" below, both in exit code and in message). Exits 19
  covers several staging-directory conditions, and which apply depends on the
  route: the derived coverage staging directory is missing (Full and
  FullCoverage, both with -CoverageRunId); it holds an entry outside the
  enumerated whitelist of run-descriptor.json plus the two optional launcher
  log files (FullCoverage only -- Full's second-writer grant deliberately
  does not apply this whitelist rule, since it runs into a directory
  FullCoverage already populated); its descriptor's identity does not match
  the invocation (Full and FullCoverage); or, for Full only, its own
  full-junit.xml destination already exists (file, directory, or reparse
  point, including a dangling one) before pytest would write it. All of these
  are raised before uv or pytest ever starts, and -- for the staging-directory
  shape/descriptor checks -- while the project pytest lock is held,
  immediately before pytest launches.
#>
# This script declares NO param block on purpose: every argument arrives
# unparsed in $args and is classified manually below.
#
# Declaring -Route/-RuntimeRoot/-Target as ordinary named parameters was
# verified (throwaway probe script) to break targeting in two ways, even with
# PositionalBinding=$false and -Target marked ValueFromRemainingArguments: a
# bare trailing token intended for -Target gets bound to -RuntimeRoot instead
# whenever -RuntimeRoot has not been given a value yet, and an explicit
# `-Target a b` throws "A positional parameter cannot be found that accepts
# argument 'b'" because a parameter that already bound one value by name is
# not reconsidered for ValueFromRemainingArguments.
#
# Collecting into a single [Parameter(ValueFromRemainingArguments)] variable
# fixes those two, but makes this an advanced script, which silently adds the
# PowerShell common parameters: a token like `-p` is then rejected by the
# binder as an ambiguous prefix of -ProgressAction/-PipelineVariable before
# this script's body ever runs, so the unknown-option guard below could never
# see it. With no param block at all, every token -- including `-p` -- reaches
# that guard, which is what makes its rejection contract universal.
$RawArguments = $args

$ErrorActionPreference = 'Stop'

$validRoutes = @('Targeted', 'Quick', 'Full', 'FullSequential', 'FullCoverage')

$Route = $null
$RuntimeRoot = $null
$CoverageRunId = $null
$Target = @()
$TargetFlagSeen = $false

$index = 0
while ($index -lt $RawArguments.Count) {
    $current = $RawArguments[$index]
    if ($current -eq '-Route' -or $current -eq '-RuntimeRoot' -or $current -eq '-CoverageRunId') {
        $flag = $current
        $index++
        if ($index -ge $RawArguments.Count) {
            [Console]::Error.WriteLine("[invoke-pytest] $flag requires a value.")
            exit 2
        }
        $value = $RawArguments[$index]
        # A value slot must not silently swallow a dash-prefixed token: doing
        # so is the one remaining way an unknown option could disappear
        # without reaching the guard below (and, when a later occurrence of
        # the same flag overwrites this one, without failing at all).
        if ($value -like '-*') {
            [Console]::Error.WriteLine(
                "[invoke-pytest] $flag requires a value, but the next token is '$value'; values may not start with '-'."
            )
            exit 2
        }
        switch ($flag) {
            '-Route' { $Route = $value }
            '-RuntimeRoot' { $RuntimeRoot = $value }
            '-CoverageRunId' { $CoverageRunId = $value }
        }
        $index++
    } elseif ($current -eq '-Target') {
        # The flag token itself is only a marker; every following bare
        # token (until the next recognized flag) is collected below. Its
        # presence is recorded separately so `-Target` with no values still
        # counts as "-Target was given" for the route validation below.
        $TargetFlagSeen = $true
        $index++
    } elseif ($current -like '-*' -or $current -like '@*') {
        # An unrecognized dash-prefixed token must never be forwarded as a
        # pytest target: doing so would let a caller smuggle arbitrary pytest
        # options (e.g. --basetemp=C:\...) past the isolation contract this
        # launcher exists to enforce. `@` is rejected for the same reason:
        # pytest expands `@file` as a response file (verified against the
        # locked pytest 9.0.3), so its contents would reach pytest unchecked.
        [Console]::Error.WriteLine(
            "[invoke-pytest] Unknown option '$current'; only -Route, -Target, -RuntimeRoot, and -CoverageRunId are accepted, and a target may not start with '@'."
        )
        exit 2
    } else {
        $Target += $current
        $index++
    }
}

if (-not $Route) {
    [Console]::Error.WriteLine('[invoke-pytest] -Route is required and must be one of: ' + ($validRoutes -join ', '))
    exit 2
}
if ($validRoutes -notcontains $Route) {
    [Console]::Error.WriteLine("[invoke-pytest] -Route '$Route' is invalid; must be one of: " + ($validRoutes -join ', '))
    exit 2
}

# Parameter validation happens first and unconditionally, before this script
# dot-sources the environment helper, creates any runtime leaf, acquires the
# pytest lock, or launches any child process.
if ($Route -eq 'Targeted') {
    if (-not $Target -or $Target.Count -eq 0) {
        [Console]::Error.WriteLine(
            '[invoke-pytest] -Target is required and must name one or more explicit paths or node IDs when -Route Targeted is used.'
        )
        exit 2
    }
} elseif ($TargetFlagSeen -or ($Target -and $Target.Count -gt 0)) {
    # Both the flag and any collected value count: `-Route Quick -Target`
    # with the marker alone must be rejected too, or a caller could pass the
    # flag for a non-targeted route and have it silently ignored.
    [Console]::Error.WriteLine(
        "[invoke-pytest] -Target is only valid together with -Route Targeted (received -Route $Route)."
    )
    exit 2
}

if ($Route -eq 'FullCoverage' -or $Route -eq 'Full') {
    # Required for FullCoverage (the first writer into a run's staging
    # directory); optional for Full (the second, comparison-run writer --
    # see the -CoverageRunId parameter documentation above). A plain -Route
    # Full invocation with no -CoverageRunId is valid and keeps its previous
    # behavior unchanged.
    if ($Route -eq 'FullCoverage' -and -not $CoverageRunId) {
        [Console]::Error.WriteLine(
            '[invoke-pytest] -CoverageRunId is required when -Route FullCoverage is used.'
        )
        exit 2
    }
    if ($CoverageRunId) {
        # The run ID is joined into a filesystem path below, so its shape is
        # the first line of defence: no separators, no drive letters, no dot
        # segments. Rejecting everything outside this alphabet is cheaper
        # than trying to detect traversal after the fact.
        if ($CoverageRunId -notmatch '^[A-Za-z0-9][A-Za-z0-9-]*$') {
            [Console]::Error.WriteLine(
                "[invoke-pytest] -CoverageRunId '$CoverageRunId' is invalid; it must match ^[A-Za-z0-9][A-Za-z0-9-]*$."
            )
            exit 2
        }
    }
} elseif ($CoverageRunId) {
    [Console]::Error.WriteLine(
        "[invoke-pytest] -CoverageRunId is only valid together with -Route FullCoverage or -Route Full (received -Route $Route)."
    )
    exit 2
}

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'project-tool-environment.ps1')

function Get-CoverageArtifactRoot {
    <#
    .SYNOPSIS
      The one coverage artifact root, derived from the same repository-owned
      runtime-root configuration every other managed path uses.
    .DESCRIPTION
      Deliberately NOT a caller parameter. scripts/report-diff-coverage.ps1
      calls this same function, so the reporting script and the launcher can
      never disagree about where a run's staging directory lives. It is also
      not one of the seven managed leaves: coverage evidence outlives a single
      tool invocation, so it must not be swept with the per-run leaves.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$RepositoryRoot,

        [string]$RuntimeRoot
    )

    $root = Get-ProjectRuntimeRoot -RepositoryRoot $RepositoryRoot -RuntimeRoot $RuntimeRoot
    return (Join-Path $root 'coverage')
}

function Get-ProjectParentProcessId {
    <#
    .SYNOPSIS
      Resolve the actual OS parent process ID of $ProcessId, or $null when it
      cannot be determined.

    .DESCRIPTION
      Used to bind a coverage run descriptor's ownerProcessId to the process
      that actually invoked this launcher, not merely to any process that
      happens to still be alive under that recorded PID. A live-but-unrelated
      process reusing (or forging) a run ID's descriptor must not pass as an
      authorized invocation just because *some* process with that PID exists.
    #>
    param(
        [Parameter(Mandatory)]
        [int]$ProcessId
    )

    try {
        $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    } catch {
        return $null
    }
    if (-not $process -or -not $process.ParentProcessId) {
        return $null
    }
    return [int]$process.ParentProcessId
}

function Assert-CoverageStagingDirectory {
    <#
    .SYNOPSIS
      Fail closed on the derived staging directory, deciding on its resolved
      real path rather than its spelling. Returns the resolved path.

    .DESCRIPTION
      -RequireFreshDirectory selects between the two routes' distinct grants
      (design.md > "Bind collection evidence to the run that produced it"):
      FullCoverage is the first writer into a run's staging directory and
      passes this switch, so the directory must hold only an enumerated
      whitelist, closed by exact name: exactly one run-descriptor.json
      (required), plus optionally launcher-stdout.log and/or
      launcher-stderr.log -- the two redirection files the calling entry
      point (scripts/report-diff-coverage.ps1) necessarily creates before
      this launcher process even starts, so a sole-entry rule would refuse
      every coverage run. Full runs second, into a directory that
      legitimately already holds FullCoverage's output, so it omits the
      switch: the volume/reparse decision, path containment, and the
      descriptor's run/owner/lease identity are still verified, but the
      directory is not required to be otherwise restricted to that
      whitelist.

      This function performs the actual I/O (existence check, Win32 real-path
      resolution, directory listing, descriptor read) every time it is
      called. Callers that need the derived path merely for composing an
      argument string before validation is possible (e.g. before the pytest
      lock is held) MUST derive it with a plain Join-Path instead of calling
      this function early -- see the call site notes near this function's
      two callers below, both of which now run only inside the lock-held
      -PreLaunchValidation closure.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$ArtifactRoot,

        [Parameter(Mandatory)]
        [string]$RunId,

        [switch]$RequireFreshDirectory
    )

    foreach ($candidate in @($ArtifactRoot, (Join-Path $ArtifactRoot $RunId))) {
        $reason = Get-ProjectRuntimeRootRejectionReason -RuntimeRoot $candidate
        if ($reason) {
            [Console]::Error.WriteLine(
                "[invoke-pytest] Unsafe coverage artifact path, $($reason): $candidate"
            )
            exit 18
        }
    }

    $staging = Join-Path $ArtifactRoot $RunId
    if (-not (Test-Path -LiteralPath $staging -PathType Container)) {
        [Console]::Error.WriteLine(
            "[invoke-pytest] Coverage staging directory does not exist: $staging"
        )
        exit 19
    }

    # Resolve both sides and re-check containment: a reparse point inside the
    # artifact root can send an otherwise well-formed path anywhere, and the
    # spelling above would still look correct.
    $realRoot = Resolve-ProjectFinalPathName -Path $ArtifactRoot
    $realStaging = Resolve-ProjectFinalPathName -Path $staging
    if (-not $realRoot.Path -or -not $realStaging.Path) {
        [Console]::Error.WriteLine(
            "[invoke-pytest] Cannot resolve the coverage artifact path (Win32 error $($realRoot.ErrorCode)/$($realStaging.ErrorCode)): $staging"
        )
        exit 18
    }
    $expected = Join-Path $realRoot.Path $RunId
    if ($realStaging.Path -ne $expected) {
        [Console]::Error.WriteLine(
            "[invoke-pytest] Coverage staging directory escapes the artifact root: $($realStaging.Path) (expected $expected)"
        )
        exit 18
    }
    $realStaging = $realStaging.Path

    if ($RequireFreshDirectory) {
        # FullCoverage's whitelist rule: it is the first writer, but the
        # calling entry point (scripts/report-diff-coverage.ps1) necessarily
        # creates its own stdout/stderr redirection files before this
        # launcher process even starts, so a sole-entry rule would refuse
        # every spawn made with in-staging log redirection. The directory may
        # therefore hold, in addition to the required run descriptor, at most
        # one launcher-stdout.log and at most one launcher-stderr.log --
        # closed by exact name, never a pattern: a near-miss like
        # launcher-other.log or launcher-stdout.log.bak is still refused.
        $allowedLogNames = @('launcher-stdout.log', 'launcher-stderr.log')
        $entries = @(Get-ChildItem -LiteralPath $realStaging -Force)
        $descriptorEntries = @($entries | Where-Object { $_.Name -eq 'run-descriptor.json' })
        $unexpectedEntries = @($entries | Where-Object {
            $_.Name -ne 'run-descriptor.json' -and $allowedLogNames -notcontains $_.Name
        })
        if ($descriptorEntries.Count -ne 1 -or $unexpectedEntries.Count -gt 0) {
            [Console]::Error.WriteLine(
                "[invoke-pytest] Coverage staging directory must contain only the whitelisted entries -- run-descriptor.json (required), launcher-stdout.log (optional), launcher-stderr.log (optional): $realStaging"
            )
            exit 19
        }
        $descriptorPath = $descriptorEntries[0].FullName
    } else {
        # Full's grant: this directory legitimately already holds
        # FullCoverage's output, so only the descriptor's presence and
        # identity are checked here -- not exclusivity.
        $descriptorPath = Join-Path $realStaging 'run-descriptor.json'
        if (-not (Test-Path -LiteralPath $descriptorPath -PathType Leaf)) {
            [Console]::Error.WriteLine(
                "[invoke-pytest] Coverage staging directory is missing its run descriptor: $realStaging"
            )
            exit 19
        }
    }

    try {
        $descriptor = Get-Content -LiteralPath $descriptorPath -Raw | ConvertFrom-Json
    } catch {
        [Console]::Error.WriteLine(
            "[invoke-pytest] Unreadable coverage run descriptor: $descriptorPath"
        )
        exit 19
    }

    if ($descriptor.runId -ne $RunId -or $descriptor.leaseNamespace -ne $RunId) {
        [Console]::Error.WriteLine(
            "[invoke-pytest] Coverage run descriptor identity does not match the invocation: $realStaging"
        )
        exit 19
    }
    if (-not (Get-Process -Id $descriptor.ownerProcessId -ErrorAction SilentlyContinue)) {
        [Console]::Error.WriteLine(
            "[invoke-pytest] Coverage run descriptor names a dead owner process ($($descriptor.ownerProcessId)): $realStaging"
        )
        exit 19
    }

    # A live PID alone is not enough: descriptor.ownerProcessId must identify
    # the actual OS parent of THIS invocation (the process that spawned this
    # launcher), not merely some unrelated process that happens to be alive
    # under a reused or forged PID. Without this, two launchers racing to
    # validate the same run ID could both pass this check, letting a queued
    # invocation overwrite the first run's coverage files.
    $actualParentProcessId = Get-ProjectParentProcessId -ProcessId $PID
    if (-not $actualParentProcessId -or $descriptor.ownerProcessId -ne $actualParentProcessId) {
        [Console]::Error.WriteLine(
            "[invoke-pytest] Coverage run descriptor owner ($($descriptor.ownerProcessId)) does not match this invocation's actual parent process ($actualParentProcessId): $realStaging"
        )
        exit 19
    }

    return $realStaging
}

function Get-ProjectPathOccupancy {
    <#
    .SYNOPSIS
      Classify what is at $Path as 'Occupied' (a file, a directory, or a
      reparse point of any kind, including a dangling one whose target does
      not resolve), 'Absent', or 'Unverifiable'.

    .DESCRIPTION
      [IO.File]::GetAttributes reports the entry itself without following a
      reparse point (unlike Test-Path, which resolves through a symlink or
      junction to its target and would therefore miss a dangling one). This
      is the same idiom Get-ProjectRuntimeRootRejectionReason above already
      relies on for the identical reason.

      Only FileNotFoundException/DirectoryNotFoundException -- the .NET-typed
      equivalents of the ERROR_FILE_NOT_FOUND/ERROR_PATH_NOT_FOUND codes that
      same function's own not-found check keys on -- justify 'Absent': the
      entry genuinely does not exist (or an ancestor segment does not).
      Every other failure (a permission denial, a disconnected volume, any
      other I/O error) means this guard cannot see the entry, which is
      'Unverifiable', not 'Absent': a guard that cannot see must not wave
      the caller through.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    try {
        [void][IO.File]::GetAttributes($Path)
        return 'Occupied'
    } catch [System.IO.FileNotFoundException] {
        return 'Absent'
    } catch [System.IO.DirectoryNotFoundException] {
        return 'Absent'
    } catch {
        return 'Unverifiable'
    }
}

# FullCoverage always needs a location to write a junit XML report: the
# machine-readable (nodeid, outcome) population that
# scripts/report-diff-coverage.ps1 compares across the instrumented and
# uninstrumented runs for full collection/subtest identity (design.md >
# "Bind collection evidence to the run that produced it"). Full needs one
# ONLY when it is given a run ID -- that report has exactly one consumer,
# scripts/report-diff-coverage.ps1's own comparison read, and that consumer
# always supplies a run ID now. A junit report with no reader is exactly the
# shared-writable-path shape this route contract exists to eliminate, so a
# plain `Full` invocation writes no junit report at all.
#
# The staging-directory path below is derived with a plain Join-Path -- no
# I/O, no safety check -- purely so it can be woven into the pytest argument
# vector a few lines down. That derivation is a convenience for argument
# assembly ONLY; it is not, and must not be treated as, the validation the
# requirement demands. The actual checks (volume/reparse safety, the
# directory's existence, its whitelist shape or descriptor presence, the
# descriptor's identity, and -- for Full -- its own full-junit.xml
# occupancy) all run once, together, inside $coveragePreLaunchValidation
# below, which Invoke-InProjectToolEnvironment invokes after the project
# pytest lock is held and immediately before pytest launches (see the call
# near the bottom of this file). Running the derivation early and the
# validation lock-held means a directory that changes in the window between
# the two is still caught, because nothing here is trusted until the
# lock-held check re-establishes it.
$fullRouteStagingDirectory = $null
$fullRouteJunitXmlPath = $null
if ($Route -eq 'Full' -and $CoverageRunId) {
    # Full's second-writer grant (design.md > "Bind collection evidence to
    # the run that produced it"): still a run ID, never a caller-supplied
    # path, with the location derived the same way FullCoverage's is -- but
    # this directory legitimately already holds FullCoverage's output, so
    # the whitelist rule is not applied (see -RequireFreshDirectory on
    # Assert-CoverageStagingDirectory above).
    $fullRouteStagingDirectory = Join-Path `
        (Get-CoverageArtifactRoot -RepositoryRoot $repositoryRoot -RuntimeRoot $RuntimeRoot) `
        $CoverageRunId

    # Bound to this comparison run: nothing else is ever pointed at this
    # directory, so no unrelated later invocation can overwrite this file
    # between this route returning and report-diff-coverage.ps1's read of it.
    $fullRouteJunitXmlPath = Join-Path $fullRouteStagingDirectory 'full-junit.xml'
}

# One combined lock-held pre-launch validation for both coverage-participant
# routes. Built here (after both routes' staging paths are known) and passed
# to Invoke-InProjectToolEnvironment's -PreLaunchValidation near the bottom
# of this file, which invokes it after the project pytest lock is held and
# immediately before pytest launches -- never before. This is the single
# ordering guarantee the requirement demands: whichever route is active, the
# staging-directory shape/descriptor validation (and, for Full, its own
# full-junit.xml occupancy check) happens once, atomically with the launch it
# guards, so no other launcher invocation can interleave between the check
# and the run it protects.
$coveragePreLaunchValidation = $null
if ($Route -eq 'Full' -and $CoverageRunId) {
    $capturedArtifactRoot = Get-CoverageArtifactRoot -RepositoryRoot $repositoryRoot -RuntimeRoot $RuntimeRoot
    $capturedRunId = $CoverageRunId
    $capturedFullRouteJunitXmlPath = $fullRouteJunitXmlPath
    $coveragePreLaunchValidation = {
        # Full's own grant: volume/reparse safety, existence, and descriptor
        # identity, but NOT the whitelist rule -- this directory legitimately
        # already holds FullCoverage's output.
        [void](Assert-CoverageStagingDirectory `
            -ArtifactRoot $capturedArtifactRoot `
            -RunId $capturedRunId)

        # This destination must be a fresh write, not merely a writable one:
        # the evidence this route exists to produce (design.md > "Bind
        # collection evidence to the run that produced it") is only
        # trustworthy if it was actually produced by THIS invocation of
        # pytest. If pytest dies before writing here, a pre-existing
        # full-junit.xml -- file, directory, or reparse point -- would
        # otherwise be read afterward as this run's collection evidence.
        # Full deliberately does not apply the whitelist rule to the rest of
        # the staging directory (see -RequireFreshDirectory above), so this
        # is the one entry name it does still own exclusively, and it is
        # checked here rather than folded into Assert-CoverageStagingDirectory
        # because 'full-junit.xml' is this route's own output name, not a
        # property of the staging directory FullCoverage populated. Exit 19,
        # not 18: this is a staging-directory shape/identity failure like
        # every other check above it, not an unsafe-path failure
        # (volume/reparse-escape) like Assert-CoverageStagingDirectory's own
        # exit-18 checks. An Unverifiable classification (permission denial,
        # disconnected volume, any other I/O error querying the entry) is
        # instead exit 18, matching Assert-CoverageStagingDirectory's own
        # "cannot resolve" checks above: a guard that cannot see the
        # destination must not wave the caller through, and its message is
        # deliberately distinct from the "already exists" one so the two
        # failure modes are never confused.
        $occupancy = Get-ProjectPathOccupancy -Path $capturedFullRouteJunitXmlPath
        if ($occupancy -eq 'Occupied') {
            [Console]::Error.WriteLine(
                "[invoke-pytest] Full's JUnit destination already exists and must not be reused: $capturedFullRouteJunitXmlPath"
            )
            exit 19
        } elseif ($occupancy -eq 'Unverifiable') {
            [Console]::Error.WriteLine(
                "[invoke-pytest] Full's JUnit destination occupancy could not be verified: $capturedFullRouteJunitXmlPath"
            )
            exit 18
        }
    }.GetNewClosure()
} elseif ($Route -eq 'FullCoverage') {
    $capturedArtifactRoot = Get-CoverageArtifactRoot -RepositoryRoot $repositoryRoot -RuntimeRoot $RuntimeRoot
    $capturedRunId = $CoverageRunId
    $coveragePreLaunchValidation = {
        # FullCoverage's grant: volume/reparse safety, existence, the
        # whitelist shape check, and descriptor identity -- it is the first
        # writer into this run's staging directory.
        [void](Assert-CoverageStagingDirectory `
            -ArtifactRoot $capturedArtifactRoot `
            -RunId $capturedRunId `
            -RequireFreshDirectory)
    }.GetNewClosure()
}

# Exact accepted argument vectors -- version 3 (see #75's report-only diff
# coverage change, round 4): version 2 added a fixed --junitxml element to
# both the Full and FullCoverage tails so the two routes produced comparable
# collection/subtest identity evidence. Version 3 removes that element from
# Full's default vector entirely: Full's junit report exists ONLY when
# -CoverageRunId is supplied, and then it lives in that run's own staging
# directory, never at a shared fixed path. A plain `Full` invocation with no
# -CoverageRunId is therefore deliberately identical to version 1 (before
# #75), not to version 2 -- it carries no --junitxml element at all.
# FullCoverage's own tail is unaffected either way. #37 and the documented
# route contracts depend on every other element of every route staying
# byte-for-byte identical to the previously manual invocations.
$pytestArguments = switch ($Route) {
    'Targeted' { @('-q') + $Target }
    'Quick' { @('-n', '3', '--dist=load', '-q', '-m', 'not slow and not release', 'tests') }
    'Full' {
        if ($fullRouteJunitXmlPath) {
            @('-n', '3', '--dist=load', '-q', 'tests', "--junitxml=$fullRouteJunitXmlPath")
        } else {
            @('-n', '3', '--dist=load', '-q', 'tests')
        }
    }
    'FullSequential' { @('-n', '0', '-q', 'tests') }
    # Byte-for-byte the Full vector's non-junit elements; the coverage
    # options (including its OWN junit path, distinct from Full's) are
    # appended below so the inheritance stays visible here rather than being
    # retyped.
    'FullCoverage' { @('-n', '3', '--dist=load', '-q', 'tests') }
}

$coverageStagingDirectory = $null
if ($Route -eq 'FullCoverage') {
    # Plain Join-Path derivation only -- see the comment above
    # $coveragePreLaunchValidation's construction for why the actual
    # whitelist/descriptor validation is deferred to that lock-held closure
    # rather than performed here. No pre-existing-destination check is
    # needed for junit.xml/coverage.xml either, unlike Full's full-junit.xml
    # check: the whitelist check already enforced by that closure tolerates
    # nothing beyond run-descriptor.json and the two optional launcher log
    # files, so a pre-existing junit.xml or coverage.xml already fails that
    # check; adding a second check for the same fact would be redundant, not
    # additional coverage.
    $coverageStagingDirectory = Join-Path `
        (Get-CoverageArtifactRoot -RepositoryRoot $repositoryRoot -RuntimeRoot $RuntimeRoot) `
        $CoverageRunId

    $pytestArguments += @(
        '--cov=src'
        '--cov-branch'
        '--cov-report=term-missing'
        "--cov-report=xml:$(Join-Path $coverageStagingDirectory 'coverage.xml')"
        "--junitxml=$(Join-Path $coverageStagingDirectory 'junit.xml')"
    )
}

if ($RuntimeRoot) {
    # Every other managed path follows an environment variable the helper
    # sets, but pytest's basetemp comes from the static repo-relative addopts
    # in pyproject.toml. Without this argument an explicit -RuntimeRoot --
    # whose only reason to exist is a repository volume that is unsuitable --
    # would move six leaves and leave the largest writer of the seven behind,
    # silently. Appended only under the override, so the four canonical route
    # vectors above stay byte-for-byte identical and the tracked addopts
    # keeps governing any pytest run started outside this launcher.
    $overrideRoot = Get-ProjectRuntimeRoot -RepositoryRoot $repositoryRoot -RuntimeRoot $RuntimeRoot
    $overrideLeaves = Get-ProjectRuntimeLeaves -RuntimeRoot $overrideRoot
    $pytestArguments += "--basetemp=$($overrideLeaves['pytest'])"
}

# Byte-identical to every other route: pytest-cov and coverage.py are locked
# development dependencies, not an `--with` overlay. An overlay would put the
# child in an ephemeral environment whose VIRTUAL_ENV does not match the
# project's, and uv's warning about that mismatch lands in output other tools
# parse -- measured during qualification, where it broke two release-installer
# tests that pass under this route.
$uvArguments = @('run', '--locked', '--exact', 'pytest') + $pytestArguments

$previousCoverageFile = $env:COVERAGE_FILE
if ($coverageStagingDirectory) {
    # coverage.py writes its data file next to COVERAGE_FILE, and each xdist
    # worker writes its own suffixed sibling. Without this they would land in
    # the repository root and show up in git status.
    $env:COVERAGE_FILE = Join-Path $coverageStagingDirectory '.coverage'
}
try {
    $result = Invoke-InProjectToolEnvironment `
        -RepositoryRoot $repositoryRoot `
        -RuntimeRoot $RuntimeRoot `
        -ChildFilePath 'uv' `
        -ChildArgumentList $uvArguments `
        -PreLaunchValidation $coveragePreLaunchValidation
} finally {
    $env:COVERAGE_FILE = $previousCoverageFile
}

exit $result.ExitCode

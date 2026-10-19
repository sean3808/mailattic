<#
.SYNOPSIS
  Report-only diff-coverage entry point (#75). Resolves R/B/T, invokes the
  canonical pytest launcher's FullCoverage route exactly once, and produces
  cumulative (R...T) and change-attributed (B...T) diff-coverage evidence
  from that single coverage XML.

.DESCRIPTION
  This script performs Git resolution, coverage-XML parsing, diff-cover
  invocation, and artifact management ONLY. It never invokes pytest itself:
  the coverage XML is obtained solely by calling
  `scripts/invoke-pytest.ps1 -Route FullCoverage -CoverageRunId <id>` in the
  target repository, exactly once. See
  openspec/changes/establish-report-only-diff-coverage/design.md and
  .../specs/agent-quality-gates/spec.md for the full behavioral contract;
  this file implements it and does not restate it.

  No artifact-root parameter is accepted. The coverage artifact root is
  resolved the same way the launcher resolves it -- via
  Get-ProjectRuntimeRoot in scripts/project-tool-environment.ps1 -- so this
  script and the launcher can never disagree about where a run's staging
  directory lives.

  Coverage remains report-only: no fail-under is configured anywhere, and a
  coverage percentage never determines this script's exit code. The
  operational failures that DO produce a non-zero exit are documented next
  to each check below (dirty target repo, launcher failure, missing/
  malformed XML, path mismatch, unresolved Git identity, snapshot drift,
  instrumented/uninstrumented outcome disagreement).

.PARAMETER RepositoryRoot
  The target repository root. The canonical launcher
  (scripts/invoke-pytest.ps1) and its shared environment helper
  (scripts/project-tool-environment.ps1) MUST already exist under
  <RepositoryRoot>/scripts/ -- this script calls them, it does not
  reimplement their path-safety or pytest-invocation logic.

.PARAMETER ChangeName
  The Spectra change name this run's evidence is being produced for. Part of
  the run directory's identity key (see Decisions > "Make Windows artifacts
  reproducible and status-clean" in design.md).

.PARAMETER R
  Full commit SHA of the local `origin/main` ref at measurement start.

.PARAMETER B
  Full commit SHA of the reviewed boundary commit immediately before the
  Spectra change being assessed.

.PARAMETER T
  Full commit SHA of the immutable target commit containing the completed
  change. The target repository MUST be clean and checked out at this exact
  commit before this script is invoked.

.PARAMETER DeclaredCommitSet
  An explicit allowlist of full commit SHAs, supplied independently of B and
  T. Mutually exclusive with -DeclaredCommitSetManifestPath; exactly one of
  the two MUST be supplied. This set is never derived from `B...T` -- doing
  so would make the attribution equality check vacuous.

.PARAMETER DeclaredCommitSetManifestPath
  Path to an immutable, reviewed manifest file listing full commit SHAs, one
  per line (blank lines and lines starting with `#` are ignored). Mutually
  exclusive with -DeclaredCommitSet.

.OUTPUTS
  Writes a summary object to the pipeline on success, including the run's
  staging directory, manifest path, and both report labels/attribution
  results. Throws (and the process exits non-zero) on any operational
  failure; a low or zero coverage percentage alone is never a failure.
#>
# PositionalBinding=$false is load-bearing under `pwsh -File`, which is how
# every documented invocation reaches this script. With positional binding on,
# a second bare SHA after -DeclaredCommitSet binds to
# -DeclaredCommitSetManifestPath instead, and the run is then refused for
# supplying both forms at once -- while a comma-joined string arrives as ONE
# argv token and is refused for not being a 40-character SHA. Between them
# there was no way to declare more than one commit. ValueFromRemainingArguments
# lets the bare SHAs collect into the array they were always meant to be.
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory)]
    [string]$RepositoryRoot,

    [Parameter(Mandatory)]
    [string]$ChangeName,

    [Parameter(Mandatory)]
    [string]$R,

    [Parameter(Mandatory)]
    [string]$B,

    [Parameter(Mandatory)]
    [string]$T,

    # Under `pwsh -File` this reliably carries ONE SHA. Several bare SHAs cannot
    # be collected here: ValueFromRemainingArguments does not reconsider a
    # parameter that already bound a value by name (the same PowerShell
    # behavior documented at length in scripts/invoke-pytest.ps1), and a
    # comma-joined string arrives as a single argv token. Declare a multi-commit
    # change through -DeclaredCommitSetManifestPath, which is the better
    # provenance story anyway: the manifest is reviewed, and its hash is
    # recorded alongside the set it supplied.
    [string[]]$DeclaredCommitSet,

    [string]$DeclaredCommitSetManifestPath
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Get-Sha256HexOfString {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $hasher.ComputeHash($bytes)
    } finally {
        $hasher.Dispose()
    }
    return -join ($hashBytes | ForEach-Object { $_.ToString('x2') })
}

function Get-Sha256HexOfFile {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-GitCommand {
    <#
    .SYNOPSIS
      Run `git -C <RepositoryRoot> <Arguments>`, throwing with the captured
      output on a non-zero exit unless -AllowNonZeroExit is set.
    #>
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$AllowNonZeroExit
    )
    $output = & git -C $RepositoryRoot @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -and -not $AllowNonZeroExit) {
        throw "git $($Arguments -join ' ') failed with exit code ${exitCode}: $($output -join [Environment]::NewLine)"
    }
    return [pscustomobject]@{ Output = @($output); ExitCode = $exitCode }
}

function Test-SetsEqual {
    <#
    .SYNOPSIS
      Order-independent set equality for two string collections.

    .DESCRIPTION
      $null and an empty collection both mean "no items" here and must
      compare equal to each other -- neither is treated as a distinct third
      state. Piping through Where-Object (rather than a bare `@($First)`
      wrap) is load-bearing: a caller's variable that itself holds $null
      (e.g. a function-return result that collapsed across the function-call
      boundary because PowerShell unwraps a zero-item pipeline output to
      $null unless the CALL SITE is also wrapped in `@(...)`) makes a bare
      `@($First)` a ONE-element array containing that $null, not an empty
      array -- and Compare-Object then throws binding a null ReferenceObject.
      Piping through Where-Object sends genuinely zero items through for a
      null input, avoiding that collapse. Verified directly against this
      exact repro; see tests/test_report_diff_coverage.py.
    #>
    param([string[]]$First, [string[]]$Second)
    $a = @($First | Where-Object { $null -ne $_ })
    $b = @($Second | Where-Object { $null -ne $_ })
    if ($a.Count -ne $b.Count) { return $false }
    if ($a.Count -eq 0) { return $true }
    $diff = Compare-Object -ReferenceObject $a -DifferenceObject $b
    return (-not $diff)
}

function Get-MeasurableChangedPaths {
    <#
    .SYNOPSIS
      The subset of a range's changed src/ paths that can possibly appear in
      diff-cover's src_stats: those that still exist at the target commit.

    .DESCRIPTION
      `git diff --name-only` lists a path for EVERY change kind, including a
      pure deletion -- but diff-cover can never report a deleted file in
      src_stats, because the target side has no executable lines and the
      coverage XML (measured at T) carries no entry for it either. Comparing
      the FULL changed-path list against src_stats would therefore flag
      every single production-file deletion as a path mismatch, even when
      every other file in the same change is a perfectly ordinary,
      correctly-reported edit.

      The distinguishing test is deliberately "does the file exist at T",
      not "did diff-cover report it" -- the latter is exactly the signal the
      path-mismatch guard exists to verify independently, so using it here
      would reopen the original hole (a genuinely missing report would look
      identical to a legitimate deletion).
    #>
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$TargetCommit,
        [string[]]$ChangedPaths
    )
    $measurable = foreach ($path in @($ChangedPaths)) {
        $existsAtTarget = Invoke-GitCommand -RepositoryRoot $RepositoryRoot `
            -Arguments @('cat-file', '-e', "${TargetCommit}:${path}") -AllowNonZeroExit
        if ($existsAtTarget.ExitCode -eq 0) {
            $path
        }
    }
    return @($measurable)
}

function Get-CoverageArtifactRootForReport {
    <#
    .SYNOPSIS
      Resolve the coverage artifact root the same way the launcher does.

    .DESCRIPTION
      Deliberately does NOT dot-source scripts/invoke-pytest.ps1: that file
      has unconditional top-level argument-parsing logic which calls `exit`
      when -Route is absent, and dot-sourcing it here would run that logic
      in this process and terminate it. This function calls the same
      Get-ProjectRuntimeRoot function the launcher calls (from
      project-tool-environment.ps1, which this script also dot-sources), so
      the resolved root can never diverge from the launcher's own.
    #>
    param([Parameter(Mandatory)][string]$RepositoryRoot)
    $root = Get-ProjectRuntimeRoot -RepositoryRoot $RepositoryRoot
    return (Join-Path $root 'coverage')
}

function Get-PytestSummaryCounts {
    <#
    .SYNOPSIS
      Extract normalized pass/fail/skip/error counts from a captured pytest
      `-q` stdout log, ignoring the run's duration and worker-interleaving
      order -- neither is a meaningful identity signal under -n/--dist=load.
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$StdOut)
    $counts = [ordered]@{ passed = 0; failed = 0; error = 0; skipped = 0; xfailed = 0; xpassed = 0 }
    $lines = @($StdOut -split "`r?`n")
    $summaryLine = $lines | Where-Object { $_ -match '\d+ (passed|failed|error|skipped|xfailed|xpassed)' } | Select-Object -Last 1
    if (-not $summaryLine) {
        return $counts
    }
    foreach ($key in @($counts.Keys)) {
        if ($summaryLine -match "(\d+) $key") {
            $counts[$key] = [int]$Matches[1]
        }
    }
    return $counts
}

function Test-SummaryCountsEqual {
    param($First, $Second)
    foreach ($key in @($First.Keys)) {
        if ($First[$key] -ne $Second[$key]) { return $false }
    }
    return $true
}

function Get-JunitTestIdentities {
    <#
    .SYNOPSIS
      Parse a pytest junit XML report into a sorted, de-duplicated array of
      "<classname>::<name>|<outcome>" identity strings covering EVERY
      collected test -- passed, failed, error, and skipped (which is also
      where pytest's default junit-xml plugin records xfail/xpass) alike.

    .DESCRIPTION
      Terminal totals alone (Get-PytestSummaryCounts) cannot tell "test A
      failed in both runs" apart from "test A failed here, test B failed
      there" when the two runs happen to report the same failed count -- and
      a stdout-only signal (the previous approach here) could only ever
      recover FAILED/ERROR identity, never a same-count SKIP/xfail/pass
      swap. Both the Full and FullCoverage routes now always write a junit
      XML report (scripts/invoke-pytest.ps1, version 2 of the route
      contract), giving a genuinely machine-readable population to compare
      instead.
    #>
    param([Parameter(Mandatory)][string]$JunitXmlPath)
    if (-not (Test-Path -LiteralPath $JunitXmlPath -PathType Leaf)) {
        throw "Expected junit XML report was not produced: $JunitXmlPath"
    }
    try {
        [xml]$junitXml = Get-Content -LiteralPath $JunitXmlPath -Raw
    } catch {
        throw "Junit XML report is malformed: $JunitXmlPath -- $($_.Exception.Message)"
    }
    $identities = foreach ($testcase in @($junitXml.SelectNodes('//testcase'))) {
        if (-not $testcase) { continue }
        $outcome = 'passed'
        if ($testcase.SelectSingleNode('failure')) { $outcome = 'failed' }
        elseif ($testcase.SelectSingleNode('error')) { $outcome = 'error' }
        elseif ($testcase.SelectSingleNode('skipped')) { $outcome = 'skipped' }
        "$($testcase.classname)::$($testcase.name)|$outcome"
    }
    return @($identities | Sort-Object -Unique)
}

function Get-ChangedLineNumbersByFile {
    <#
    .SYNOPSIS
      Map git-relative file path -> set of NEW-side changed line numbers for
      a three-dot compare range, restricted to src/. Used to intersect
      against XML branch metadata for changed-partial-branch detail.
    #>
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$Range
    )
    $diffOutput = (Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('diff', '--unified=0', $Range, '--', 'src')).Output
    $result = @{}
    $currentFile = $null
    foreach ($line in $diffOutput) {
        if ($line -match '^\+\+\+ b/(.+)$') {
            $currentFile = $Matches[1]
            if (-not $result.ContainsKey($currentFile)) {
                $result[$currentFile] = New-Object 'System.Collections.Generic.HashSet[int]'
            }
            continue
        }
        if ($line -match '^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@') {
            if (-not $currentFile) { continue }
            $startLine = [int]$Matches[1]
            $lineCount = if ($Matches[2]) { [int]$Matches[2] } else { 1 }
            for ($i = 0; $i -lt $lineCount; $i++) {
                [void]$result[$currentFile].Add($startLine + $i)
            }
        }
    }
    return $result
}

function Get-PartialBranchLinesFromCoverageXml {
    <#
    .SYNOPSIS
      Parse coverage.py's Cobertura-style XML directly for per-line branch
      metadata. Never inferred from diff-cover's own line percentage --
      diff-cover is not asked for --branch-coverage, so its violation_lines
      never conflate "uncovered line" with "partially covered branch".
    #>
    param([Parameter(Mandatory)][xml]$CoverageXml)
    $result = @{}
    foreach ($package in @($CoverageXml.coverage.packages.package)) {
        foreach ($class in @($package.classes.class)) {
            if (-not $class) { continue }
            $filename = ($class.filename -replace '\\', '/')
            $gitPath = "src/$filename"
            if (-not $result.ContainsKey($gitPath)) { $result[$gitPath] = @{} }
            foreach ($lineNode in @($class.lines.line)) {
                if (-not $lineNode) { continue }
                if ($lineNode.branch -eq 'true' -and $lineNode.'condition-coverage') {
                    if ($lineNode.'condition-coverage' -match '^(\d+)%') {
                        $pct = [int]$Matches[1]
                        if ($pct -lt 100) {
                            $result[$gitPath][[int]$lineNode.number] = [pscustomobject]@{
                                conditionCoverage = $lineNode.'condition-coverage'
                                missingBranches   = $lineNode.'missing-branches'
                            }
                        }
                    }
                }
            }
        }
    }
    return $result
}

function Get-ChangedPartialBranches {
    param($ChangedLinesByFile, $PartialBranchesByFile)
    $combined = @()
    foreach ($file in $ChangedLinesByFile.Keys) {
        if (-not $PartialBranchesByFile.ContainsKey($file)) { continue }
        foreach ($lineNo in $ChangedLinesByFile[$file]) {
            if ($PartialBranchesByFile[$file].ContainsKey($lineNo)) {
                $detail = $PartialBranchesByFile[$file][$lineNo]
                $combined += [pscustomobject]@{
                    file              = $file
                    line              = $lineNo
                    conditionCoverage = $detail.conditionCoverage
                    missingBranches   = $detail.missingBranches
                }
            }
        }
    }
    return @($combined | Sort-Object file, line)
}

function Invoke-DiffCoverReport {
    <#
    .SYNOPSIS
      Run `uv run --locked --exact diff-cover` once against the one coverage
      XML, comparing HEAD (which preflight already verified equals T)
      against $CompareRef using three-dot notation. Never re-runs tests.
    #>
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$CoverageXmlPath,
        [Parameter(Mandatory)][string]$CompareRef,
        [Parameter(Mandatory)][string]$JsonReportPath,
        [Parameter(Mandatory)][string]$TextLogPath
    )
    Push-Location -LiteralPath $RepositoryRoot
    try {
        $arguments = @(
            'run', '--locked', '--exact', 'diff-cover',
            $CoverageXmlPath,
            "--compare-branch=$CompareRef",
            '--diff-range-notation=...',
            "--format=json:$JsonReportPath",
            '--show-uncovered'
        )
        $output = & uv @arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    ($output -join [Environment]::NewLine) | Set-Content -LiteralPath $TextLogPath -Encoding utf8NoBOM
    return [pscustomobject]@{ ExitCode = $exitCode; Output = @($output) }
}

function Assert-DiffCoverReportShape {
    <#
    .SYNOPSIS
      Validate a parsed diff-cover JSON report's required fields exist and
      are correctly typed, BEFORE any set/path comparison consumes them.

    .DESCRIPTION
      Syntactically valid JSON (ConvertFrom-Json parses without throwing) is
      not the same as a structurally valid report. diff-cover can exit 0
      while writing JSON whose `src_stats` field (or a numeric total field)
      is missing or explicitly null -- and every consumer downstream of the
      parse (the src_stats key enumeration, the numeric totals written into
      the manifest) would silently treat that the same as a legitimate empty
      report (`src_stats: {}`), especially when the production diff is
      ALSO genuinely empty: the path-mismatch guard is gated on "is the
      range's changed-path set empty", so a malformed report for a
      genuinely-empty range would sail straight through that gate with
      never having been asked whether its own shape is trustworthy. This
      check is unconditional -- it runs regardless of whether the diff is
      empty -- specifically to close that gap.

      A missing/null src_stats is a distinct failure from a legitimate
      `src_stats: {}`: the former means the report cannot be trusted at all;
      the latter is this script's normal "nothing to cover" shape, already
      accepted elsewhere (see the Where-Object comment at the src_stats key
      enumeration below).
    #>
    param(
        [Parameter(Mandatory)]
        $Report,
        [Parameter(Mandatory)][string]$RangeLabel,
        [Parameter(Mandatory)][string]$JsonReportPath
    )
    if ($null -eq $Report) {
        throw "diff-cover produced a malformed report for ${RangeLabel}: the JSON parsed to null: $JsonReportPath"
    }
    $reportPropertyNames = @($Report.PSObject.Properties.Name)
    foreach ($field in @('total_percent_covered', 'total_num_lines', 'total_num_violations')) {
        if ($reportPropertyNames -notcontains $field -or $null -eq $Report.$field) {
            throw "diff-cover produced a malformed report for ${RangeLabel}: field '$field' is missing or null: $JsonReportPath"
        }
        $fieldValue = $Report.$field
        $isNumeric = ($fieldValue -is [double]) -or ($fieldValue -is [int]) -or ($fieldValue -is [long]) -or ($fieldValue -is [decimal])
        if (-not $isNumeric) {
            throw "diff-cover produced a malformed report for ${RangeLabel}: field '$field' is not numeric ($($fieldValue.GetType().FullName)): $JsonReportPath"
        }
    }
    if ($reportPropertyNames -notcontains 'src_stats' -or $null -eq $Report.src_stats) {
        throw ("diff-cover produced a malformed report for ${RangeLabel}: field 'src_stats' is missing or null " +
            "(a legitimate empty report uses src_stats: {}, not a missing/null field): $JsonReportPath")
    }
    if ($Report.src_stats -isnot [System.Management.Automation.PSCustomObject]) {
        throw "diff-cover produced a malformed report for ${RangeLabel}: field 'src_stats' is not an object ($($Report.src_stats.GetType().FullName)): $JsonReportPath"
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

try {
    $RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path

    $launcherPath = Join-Path $RepositoryRoot 'scripts/invoke-pytest.ps1'
    $environmentHelperPath = Join-Path $RepositoryRoot 'scripts/project-tool-environment.ps1'
    if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
        throw "Canonical pytest launcher not found under the target repository: $launcherPath"
    }
    if (-not (Test-Path -LiteralPath $environmentHelperPath -PathType Leaf)) {
        throw "Shared environment helper not found under the target repository: $environmentHelperPath"
    }
    . $environmentHelperPath

    # -- Parameter validation ------------------------------------------------
    if ($DeclaredCommitSet -and $DeclaredCommitSetManifestPath) {
        throw 'Provide exactly one of -DeclaredCommitSet or -DeclaredCommitSetManifestPath, not both.'
    }
    if (-not $DeclaredCommitSet -and -not $DeclaredCommitSetManifestPath) {
        throw 'Provide exactly one of -DeclaredCommitSet or -DeclaredCommitSetManifestPath.'
    }

    foreach ($pair in @(
            [pscustomobject]@{ Name = 'R'; Value = $R }
            [pscustomobject]@{ Name = 'B'; Value = $B }
            [pscustomobject]@{ Name = 'T'; Value = $T }
        )) {
        if ($pair.Value -notmatch '^[0-9a-fA-F]{40}$') {
            throw "$($pair.Name) must be a full 40-character commit SHA, got: $($pair.Value)"
        }
    }
    $R = $R.ToLowerInvariant()
    $B = $B.ToLowerInvariant()
    $T = $T.ToLowerInvariant()

    foreach ($pair in @(
            [pscustomobject]@{ Name = 'R'; Value = $R }
            [pscustomobject]@{ Name = 'B'; Value = $B }
            [pscustomobject]@{ Name = 'T'; Value = $T }
        )) {
        $verify = Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('cat-file', '-e', "$($pair.Value)^{commit}") -AllowNonZeroExit
        if ($verify.ExitCode -ne 0) {
            throw "$($pair.Name) does not resolve to a commit in the target repository: $($pair.Value)"
        }
    }

    # -- Declared commit set (independent of B/T -- never derived from B...T)
    if ($DeclaredCommitSetManifestPath) {
        if (-not (Test-Path -LiteralPath $DeclaredCommitSetManifestPath -PathType Leaf)) {
            throw "Declared commit set manifest not found: $DeclaredCommitSetManifestPath"
        }
        $declaredCommitSetSourceSha256 = Get-Sha256HexOfFile -Path $DeclaredCommitSetManifestPath
        $manifestText = Get-Content -LiteralPath $DeclaredCommitSetManifestPath -Raw
        $rawDeclaredSet = @(
            ($manifestText -split "`r?`n") |
                ForEach-Object { $_.Trim() } |
                Where-Object { $_ -and -not $_.StartsWith('#') }
        )
        $declaredSetSource = "manifest:$DeclaredCommitSetManifestPath"
    } else {
        $rawDeclaredSet = @($DeclaredCommitSet | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        $declaredCommitSetSourceSha256 = Get-Sha256HexOfString ($rawDeclaredSet -join "`n")
        $declaredSetSource = 'parameter:DeclaredCommitSet'
    }
    if ($rawDeclaredSet.Count -eq 0) {
        throw 'The declared commit set is empty; a Spectra change with no commits cannot be attributed.'
    }
    foreach ($sha in $rawDeclaredSet) {
        if ($sha -notmatch '^[0-9a-fA-F]{40}$') {
            throw "Declared commit set entry is not a full 40-character commit SHA: $sha"
        }
    }
    $declaredCommitSet = @($rawDeclaredSet | ForEach-Object { $_.ToLowerInvariant() } | Sort-Object -Unique)

    # -- Preflight: target repository clean at T ------------------------------
    $headSha = ((Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('rev-parse', 'HEAD')).Output | Select-Object -First 1).Trim().ToLowerInvariant()
    if ($headSha -ne $T) {
        throw "Target repository HEAD ($headSha) does not match the immutable target T ($T). Checkout T before running this script."
    }
    $startStatus = @((Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('status', '--porcelain')).Output | Where-Object { $_ })
    if ($startStatus.Count -gt 0) {
        throw "Target repository is not clean at T; git status --porcelain reported $($startStatus.Count) entr$(if ($startStatus.Count -eq 1) {'y'} else {'ies'})."
    }
    $startSnapshot = [ordered]@{ head = $headSha; clean = $true }

    # -- Git identity resolution ----------------------------------------------
    $mergeBaseRT = ((Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('merge-base', $R, $T)).Output | Select-Object -First 1).Trim().ToLowerInvariant()
    $mergeBaseBT = ((Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('merge-base', $B, $T)).Output | Select-Object -First 1).Trim().ToLowerInvariant()

    $computedCommitSetBT = @((Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('rev-list', "$B..$T")).Output |
            ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ } | Sort-Object -Unique)
    $computedCommitSetRT = @((Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('rev-list', "$R..$T")).Output |
            ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ } | Sort-Object -Unique)

    $commitSetsEqualBT = Test-SetsEqual -First $declaredCommitSet -Second $computedCommitSetBT
    $commitSetsEqualRT = Test-SetsEqual -First $declaredCommitSet -Second $computedCommitSetRT
    $rtLabel = if ($commitSetsEqualRT) { 'single-change' } else { 'cumulative-stack-evidence' }

    $changedProductionPathsRT = @((Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('diff', '--name-only', "$R...$T", '--', 'src')).Output | Where-Object { $_ })
    $changedProductionPathsBT = @((Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('diff', '--name-only', "$B...$T", '--', 'src')).Output | Where-Object { $_ })
    $emptyProductionDiffBT = ($changedProductionPathsBT.Count -eq 0)
    $emptyProductionDiffRT = ($changedProductionPathsRT.Count -eq 0)

    # -- Tool/config identity for the run directory key -----------------------
    $uvLockPath = Join-Path $RepositoryRoot 'uv.lock'
    $lockRelevant = ''
    if (Test-Path -LiteralPath $uvLockPath -PathType Leaf) {
        $lockContent = Get-Content -LiteralPath $uvLockPath -Raw
        foreach ($pkg in @('pytest-cov', 'coverage', 'diff-cover')) {
            if ($lockContent -match "(?ms)name\s*=\s*`"$([regex]::Escape($pkg))`"\s*\r?\nversion\s*=\s*`"([^`"]+)`"") {
                $lockRelevant += "$pkg=$($Matches[1]);"
            }
        }
    }
    $toolConfigHash = (Get-Sha256HexOfString "branch=true;source=src;$lockRelevant").Substring(0, 12)

    # -- Run ID + staging directory (fail-if-exists) ---------------------------
    # The launcher (invoke-pytest.ps1) derives its staging directory as
    # Join-Path (artifact root) (CoverageRunId), so the run ID passed to the
    # launcher IS the physical directory name. The broader identity concept
    # required by design.md -- change name + R + B + T + tool/config hash +
    # a unique run ID -- is satisfied by hashing all of those inputs plus a
    # fresh GUID into the run ID itself: two runs that differ in any of
    # those fields, or that are retried concurrently with a fresh GUID, get
    # different run IDs and therefore different directories. The full
    # composite identity is retained separately in the manifest as
    # `runDirectoryKey` for auditability.
    $runIdSeed = "$ChangeName|$R|$B|$T|$toolConfigHash|$([guid]::NewGuid().ToString())"
    $runId = (Get-Sha256HexOfString $runIdSeed).Substring(0, 32)

    $artifactRoot = Get-CoverageArtifactRootForReport -RepositoryRoot $RepositoryRoot
    $stagingDir = Join-Path $artifactRoot $runId

    # Apply the SAME rejection check the launcher's Assert-CoverageStagingDirectory
    # applies (Get-ProjectRuntimeRootRejectionReason, from the environment
    # helper this script already dot-sources) BEFORE any I/O -- not after.
    # Without this, a default (or overridden) runtime root whose volume
    # cannot be established, or that resolves through an unsafe reparse
    # target, would still get a staging directory and a run-descriptor.json
    # written here, only to be refused later by the launcher's own -Route
    # FullCoverage call (exit 18) -- a run already known to be doomed still
    # left artifacts in a forbidden location. See AGENTS.md's runtime-output
    # rule for the constraint this satisfies.
    foreach ($candidate in @($artifactRoot, $stagingDir)) {
        $rejectionReason = Get-ProjectRuntimeRootRejectionReason -RuntimeRoot $candidate
        if ($rejectionReason) {
            throw "Unsafe coverage artifact path, ${rejectionReason}: $candidate"
        }
    }

    if (Test-Path -LiteralPath $stagingDir) {
        throw "Coverage run staging directory already exists (run ID collision): $stagingDir"
    }
    try {
        New-Item -ItemType Directory -Path $stagingDir -ErrorAction Stop | Out-Null
    } catch {
        throw "Failed to create coverage run staging directory fail-if-exists: $stagingDir -- $($_.Exception.Message)"
    }

    # The staging directory MUST contain only the launcher's enumerated
    # whitelist at the moment the launcher's FullCoverage route validates it:
    # this run descriptor (required), plus -- once this script spawns the
    # launcher below with its stdout/stderr redirected into this same
    # directory -- launcher-stdout.log and launcher-stderr.log (both
    # optional, exact names only). Write the descriptor now, before anything
    # else, and before the launcher call.
    $runDescriptor = [ordered]@{
        runId           = $runId
        ownerProcessId  = $PID
        leaseNamespace  = $runId
    }
    $runDescriptorPath = Join-Path $stagingDir 'run-descriptor.json'
    ($runDescriptor | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $runDescriptorPath -Encoding utf8NoBOM -NoNewline

    $tempRefPrefix = "refs/report-diff-coverage/$runId"
    $tempRefs = [ordered]@{
        R = "$tempRefPrefix/R"
        B = "$tempRefPrefix/B"
        T = "$tempRefPrefix/T"
    }

    $manifestPath = Join-Path $stagingDir 'manifest.json'
    $completionMarkerPath = Join-Path $stagingDir 'MANIFEST_COMPLETE'

    try {
        # Namespaced by run ID; not required for the git commands in this
        # script (which already resolve R/B/T as full SHAs), but pins the
        # comparison identities for the tools this script shells out to and
        # gives every temporary reference this run creates a single,
        # removable namespace. Leftovers from OTHER runs are never touched.
        foreach ($refName in $tempRefs.Values) {
            $shaForRef = switch -Regex ($refName) {
                '/R$' { $R }
                '/B$' { $B }
                '/T$' { $T }
            }
            Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('update-ref', $refName, $shaForRef) | Out-Null
        }

        # -- Invoke the launcher's FullCoverage route exactly once ------------
        # Written directly under the run's own (already-managed) staging
        # directory -- never via [System.IO.Path]::GetTempFileName() into the
        # system temp directory. A process killed between creating a system
        # temp file and a later copy-into-staging step would strand it
        # outside the artifact root and the completion-marker audit boundary
        # (AGENTS.md's "one-off tool output belongs under an existing managed
        # .project-runtime/ leaf" rule); writing the real destination path
        # directly removes that gap instead of relying on cleanup running.
        $launcherStdOutLog = Join-Path $stagingDir 'launcher-stdout.log'
        $launcherStdErrLog = Join-Path $stagingDir 'launcher-stderr.log'
        $coverageProcess = Start-Process -FilePath 'pwsh' `
            -ArgumentList @('-NoProfile', '-File', $launcherPath, '-Route', 'FullCoverage', '-CoverageRunId', $runId) `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $launcherStdOutLog `
            -RedirectStandardError $launcherStdErrLog
        $coverageExitCode = $coverageProcess.ExitCode
        $coverageStdOut = Get-Content -LiteralPath $launcherStdOutLog -Raw -ErrorAction SilentlyContinue
        $coverageStdErr = Get-Content -LiteralPath $launcherStdErrLog -Raw -ErrorAction SilentlyContinue
        if ($coverageExitCode -ne 0) {
            throw "scripts/invoke-pytest.ps1 -Route FullCoverage exited with code $coverageExitCode. See $launcherStdOutLog / $launcherStdErrLog."
        }

        $coverageXmlPath = Join-Path $stagingDir 'coverage.xml'
        if (-not (Test-Path -LiteralPath $coverageXmlPath -PathType Leaf)) {
            throw "Expected coverage XML was not produced by the launcher: $coverageXmlPath"
        }
        try {
            [xml]$coverageXml = Get-Content -LiteralPath $coverageXmlPath -Raw
        } catch {
            throw "Coverage XML is malformed: $coverageXmlPath -- $($_.Exception.Message)"
        }

        # -- Uninstrumented Full run for terminal-outcome identity check ------
        # Same immutable snapshot (still HEAD == T, verified again at the end
        # of this script). Passed THIS run's own ID -- the second enumerated
        # grant of -CoverageRunId (design.md > "Bind collection evidence to
        # the run that produced it") -- so its junit evidence is bound to
        # this run's own staging directory rather than a name shared with
        # any other run. This does not produce a second coverage XML and is
        # never treated as a second source of coverage data -- only its
        # pass/fail/skip/error counts and its junit-reported test population
        # are compared against the instrumented run above.
        # Same reasoning as the FullCoverage capture above: write directly
        # into the staging directory, never into system temp.
        $fullStdOutLog = Join-Path $stagingDir 'uninstrumented-full-stdout.log'
        $fullStdErrLog = Join-Path $stagingDir 'uninstrumented-full-stderr.log'
        $fullProcess = Start-Process -FilePath 'pwsh' `
            -ArgumentList @('-NoProfile', '-File', $launcherPath, '-Route', 'Full', '-CoverageRunId', $runId) `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $fullStdOutLog `
            -RedirectStandardError $fullStdErrLog
        $fullExitCode = $fullProcess.ExitCode
        $fullStdOut = Get-Content -LiteralPath $fullStdOutLog -Raw -ErrorAction SilentlyContinue
        if ($fullExitCode -ne 0) {
            throw "scripts/invoke-pytest.ps1 -Route Full (uninstrumented comparison run) exited with code $fullExitCode. See $fullStdOutLog / $fullStdErrLog."
        }

        # The Full route (version 3 of the route contract, see
        # scripts/invoke-pytest.ps1) writes its junit XML directly into THIS
        # run's own staging directory when given this run's ID -- the second
        # enumerated grant of -CoverageRunId (design.md > "Bind collection
        # evidence to the run that produced it"). No copy step, and no
        # shared fixed-name leaf, is involved: this script already owns the
        # staging directory, no other run can ever be pointed at it, and
        # nothing else can write here between the Full route returning and
        # this read.
        $uninstrumentedFullJunitPath = Join-Path $stagingDir 'full-junit.xml'
        if (-not (Test-Path -LiteralPath $uninstrumentedFullJunitPath -PathType Leaf)) {
            throw "Expected junit XML report was not produced by scripts/invoke-pytest.ps1 -Route Full: $uninstrumentedFullJunitPath"
        }
        $instrumentedJunitPath = Join-Path $stagingDir 'junit.xml'

        $instrumentedCounts = Get-PytestSummaryCounts -StdOut $coverageStdOut
        $uninstrumentedCounts = Get-PytestSummaryCounts -StdOut $fullStdOut
        $countsMatch = Test-SummaryCountsEqual -First $instrumentedCounts -Second $uninstrumentedCounts
        if (-not $countsMatch) {
            throw ("Instrumented (FullCoverage) and uninstrumented (Full) runs at commit $T disagree on terminal " +
                "outcome counts; coverage evidence is invalid. Instrumented=$($instrumentedCounts | ConvertTo-Json -Compress) " +
                "Uninstrumented=$($uninstrumentedCounts | ConvertTo-Json -Compress)")
        }

        # Totals matching is not enough: instrumentation could change WHICH
        # tests actually executed (a passed/skipped/xfailed/xpassed swap, not
        # only a failure swap) while leaving every terminal count unchanged.
        # Compare the full junit-reported (nodeid, outcome) identity sets --
        # see Get-JunitTestIdentities. @() at the CALL SITE (not just inside
        # the function) is load-bearing: a function whose entire output is
        # zero objects collapses to $null when captured by the caller, even
        # though the function's own `return @(...)` wrapped it as an array
        # internally -- wrapping the call itself is what survives that
        # collapse. See Test-SetsEqual's own docstring for the exact repro
        # this was verified against.
        $instrumentedTestIdentities = @(Get-JunitTestIdentities -JunitXmlPath $instrumentedJunitPath)
        $uninstrumentedTestIdentities = @(Get-JunitTestIdentities -JunitXmlPath $uninstrumentedFullJunitPath)
        $collectionIdentityMatch = Test-SetsEqual -First $instrumentedTestIdentities -Second $uninstrumentedTestIdentities
        if (-not $collectionIdentityMatch) {
            $identityDiff = @(Compare-Object -ReferenceObject $instrumentedTestIdentities -DifferenceObject $uninstrumentedTestIdentities)
            $onlyInInstrumented = @($identityDiff | Where-Object { $_.SideIndicator -eq '<=' } | ForEach-Object { $_.InputObject })
            $onlyInUninstrumented = @($identityDiff | Where-Object { $_.SideIndicator -eq '=>' } | ForEach-Object { $_.InputObject })
            throw ("Instrumented (FullCoverage) and uninstrumented (Full) runs at commit $T report identical terminal " +
                "outcome counts but disagree on WHICH tests executed and with what outcome (collection/subtest identity " +
                "mismatch); coverage evidence is invalid. Only in instrumented: " +
                "$(if ($onlyInInstrumented.Count) { $onlyInInstrumented -join ', ' } else { '(none)' }). " +
                "Only in uninstrumented: $(if ($onlyInUninstrumented.Count) { $onlyInUninstrumented -join ', ' } else { '(none)' }).")
        }
        $instrumentationSemanticsMatch = ($countsMatch -and $collectionIdentityMatch)

        # -- Two diff-cover reports from the one XML, no test rerun -----------
        $diffCoverJsonRT = Join-Path $stagingDir 'diffcover-R-T.json'
        $diffCoverLogRT = Join-Path $stagingDir 'diffcover-R-T.log'
        $diffCoverJsonBT = Join-Path $stagingDir 'diffcover-B-T.json'
        $diffCoverLogBT = Join-Path $stagingDir 'diffcover-B-T.log'

        $diffCoverRT = Invoke-DiffCoverReport -RepositoryRoot $RepositoryRoot -CoverageXmlPath $coverageXmlPath `
            -CompareRef $tempRefs.R -JsonReportPath $diffCoverJsonRT -TextLogPath $diffCoverLogRT
        $diffCoverBT = Invoke-DiffCoverReport -RepositoryRoot $RepositoryRoot -CoverageXmlPath $coverageXmlPath `
            -CompareRef $tempRefs.B -JsonReportPath $diffCoverJsonBT -TextLogPath $diffCoverLogBT

        if ($diffCoverRT.ExitCode -ne 0) {
            throw "diff-cover failed for the R...T range (exit $($diffCoverRT.ExitCode)). See $diffCoverLogRT."
        }
        if ($diffCoverBT.ExitCode -ne 0) {
            throw "diff-cover failed for the B...T range (exit $($diffCoverBT.ExitCode)). See $diffCoverLogBT."
        }
        if (-not (Test-Path -LiteralPath $diffCoverJsonRT -PathType Leaf)) {
            throw "diff-cover did not produce the expected JSON report: $diffCoverJsonRT"
        }
        if (-not (Test-Path -LiteralPath $diffCoverJsonBT -PathType Leaf)) {
            throw "diff-cover did not produce the expected JSON report: $diffCoverJsonBT"
        }

        $diffCoverReportRT = Get-Content -LiteralPath $diffCoverJsonRT -Raw | ConvertFrom-Json
        $diffCoverReportBT = Get-Content -LiteralPath $diffCoverJsonBT -Raw | ConvertFrom-Json

        # -- Validate report SHAPE before any set/path comparison consumes it -
        # Unconditional: runs whether or not the production diff is empty.
        # Syntactically valid JSON with a missing/null src_stats or numeric
        # field is a distinct failure from a legitimate empty report, and
        # must never be accepted just because the range itself is empty.
        Assert-DiffCoverReportShape -Report $diffCoverReportRT -RangeLabel 'R...T' -JsonReportPath $diffCoverJsonRT
        Assert-DiffCoverReportShape -Report $diffCoverReportBT -RangeLabel 'B...T' -JsonReportPath $diffCoverJsonBT

        # -- Distinguish "no production changes" from "path mismatch" --------
        # diff-cover's src_stats only lists files it found BOTH in the git
        # diff AND with measured coverage lines; it prints/reports nothing
        # distinguishable between "this range touches no src/ files" and "the
        # XML's paths do not line up with git's diff paths". The git-diff
        # name-only listing computed above is the independent signal that
        # tells the two apart.
        # The Where-Object is load-bearing, not defensive tidying. For an empty
        # `src_stats: {}` object, `.PSObject.Properties.Name` yields $null, and
        # @($null) is an array of ONE element -- so a naive .Count would be 1,
        # never 0, precisely for the input shape this guard exists to catch.
        # Verified in PowerShell 7; see tests/test_report_diff_coverage.py.
        #
        # A zero-count check alone only catches a TOTAL miss. When a range
        # touches several src/ files and diff-cover recognizes only some of
        # them, the count is non-zero and a count-only check would wrongly
        # accept it -- the missing file(s) would silently vanish from the
        # evidence. Compare the normalized SETS of changed-vs-reported paths
        # instead, so a partial miss is caught the same way a total one is.
        #
        # The expected side of that comparison is the MEASURABLE subset of
        # changed paths -- those that still exist at T -- not the full
        # changed-path list. A pure deletion can never appear in diff-cover's
        # src_stats (the target side has no lines to measure), so comparing
        # against the full list would flag every production-file deletion as
        # a path mismatch, even alongside other, perfectly ordinary edits in
        # the same change. See Get-MeasurableChangedPaths.
        $srcStatsKeysBT = @($diffCoverReportBT.src_stats.PSObject.Properties.Name | Where-Object { $_ })
        $measurableProductionPathsBT = @(Get-MeasurableChangedPaths -RepositoryRoot $RepositoryRoot -TargetCommit $T -ChangedPaths $changedProductionPathsBT)
        $changedProductionPathsBTNormalized = @($measurableProductionPathsBT | ForEach-Object { $_ -replace '\\', '/' } | Sort-Object -Unique)
        $srcStatsKeysBTNormalized = @($srcStatsKeysBT | ForEach-Object { $_ -replace '\\', '/' } | Sort-Object -Unique)
        $pathMismatchBT = $false
        if (-not $emptyProductionDiffBT -and -not (Test-SetsEqual -First $changedProductionPathsBTNormalized -Second $srcStatsKeysBTNormalized)) {
            $pathMismatchBT = $true
        }
        if ($pathMismatchBT) {
            $pathDiffBT = @(Compare-Object -ReferenceObject $changedProductionPathsBTNormalized -DifferenceObject $srcStatsKeysBTNormalized)
            $missingFromDiffCoverBT = @($pathDiffBT | Where-Object { $_.SideIndicator -eq '<=' } | ForEach-Object { $_.InputObject })
            $extraInDiffCoverBT = @($pathDiffBT | Where-Object { $_.SideIndicator -eq '=>' } | ForEach-Object { $_.InputObject })
            throw ("Path mismatch: git reports $($changedProductionPathsBT.Count) changed src/ file(s) for B...T " +
                "($($measurableProductionPathsBT.Count) of which exist at T and are measurable), " +
                "but diff-cover's reported file set differs from the measurable set. Missing from diff-cover: " +
                "$(if ($missingFromDiffCoverBT.Count) { $missingFromDiffCoverBT -join ', ' } else { '(none)' }). " +
                "Extra in diff-cover: $(if ($extraInDiffCoverBT.Count) { $extraInDiffCoverBT -join ', ' } else { '(none)' }). " +
                'This is the exact ambiguity this script exists to catch -- treat it as a tool/path defect, not as "nothing to cover".')
        }

        # Same null-collapse hazard, set-vs-count reasoning, and
        # measurable-vs-full-changed-set reasoning as the B...T case above;
        # RT is informational only (never throws).
        $srcStatsKeysRT = @($diffCoverReportRT.src_stats.PSObject.Properties.Name | Where-Object { $_ })
        $measurableProductionPathsRT = @(Get-MeasurableChangedPaths -RepositoryRoot $RepositoryRoot -TargetCommit $T -ChangedPaths $changedProductionPathsRT)
        $changedProductionPathsRTNormalized = @($measurableProductionPathsRT | ForEach-Object { $_ -replace '\\', '/' } | Sort-Object -Unique)
        $srcStatsKeysRTNormalized = @($srcStatsKeysRT | ForEach-Object { $_ -replace '\\', '/' } | Sort-Object -Unique)
        $pathMismatchRT = (-not $emptyProductionDiffRT -and -not (Test-SetsEqual -First $changedProductionPathsRTNormalized -Second $srcStatsKeysRTNormalized))

        # -- Changed partial-branch detail, parsed from the XML, never from
        #    diff-cover's line percentage -----------------------------------
        $partialBranchesByFile = Get-PartialBranchLinesFromCoverageXml -CoverageXml $coverageXml
        $changedLinesRT = Get-ChangedLineNumbersByFile -RepositoryRoot $RepositoryRoot -Range "$R...$T"
        $changedLinesBT = Get-ChangedLineNumbersByFile -RepositoryRoot $RepositoryRoot -Range "$B...$T"
        $changedPartialBranchesRT = Get-ChangedPartialBranches -ChangedLinesByFile $changedLinesRT -PartialBranchesByFile $partialBranchesByFile
        $changedPartialBranchesBT = Get-ChangedPartialBranches -ChangedLinesByFile $changedLinesBT -PartialBranchesByFile $partialBranchesByFile

        # -- Attribution: exact commit-set equality AND path match -----------
        $attributionValidBT = ($commitSetsEqualBT -and -not $pathMismatchBT)

        # -- Tool/version identity --------------------------------------------
        $uvVersion = ((& uv --version) -join ' ').Trim()
        $gitVersion = ((& git --version) -join ' ').Trim()

        # -- Hash every artifact written --------------------------------------
        # pytest-cov's raw data file(s) (COVERAGE_FILE and its per-xdist-worker
        # suffixed siblings, e.g. ".coverage.<host>.<pid>.<rand>") are retained
        # in the staging directory alongside the derived coverage.xml. Without
        # hashing them too, the manifest can verify the derived XML was not
        # mutated but has nothing to say about the underlying data it came
        # from -- enumerate and hash every retained ".coverage*" file as well.
        $coverageDataFiles = @(
            Get-ChildItem -LiteralPath $stagingDir -Filter '.coverage*' -File -ErrorAction SilentlyContinue |
                Sort-Object Name
        )
        $artifactHashes = [ordered]@{}
        foreach ($artifactPath in (@(
                    $runDescriptorPath, $coverageXmlPath,
                    $launcherStdOutLog, $launcherStdErrLog,
                    $fullStdOutLog, $fullStdErrLog,
                    $instrumentedJunitPath, $uninstrumentedFullJunitPath,
                    $diffCoverJsonRT, $diffCoverLogRT,
                    $diffCoverJsonBT, $diffCoverLogBT
                ) + @($coverageDataFiles | ForEach-Object { $_.FullName }))) {
            if (Test-Path -LiteralPath $artifactPath -PathType Leaf) {
                $artifactHashes[(Split-Path -Leaf $artifactPath)] = Get-Sha256HexOfFile -Path $artifactPath
            }
        }

        # -- Manifest ----------------------------------------------------------
        $manifest = [ordered]@{
            schemaVersion       = 1
            trackerIdentity     = '#75'
            changeName          = $ChangeName
            runId               = $runId
            runDirectoryKey     = [ordered]@{
                changeName     = $ChangeName
                r              = $R
                b              = $B
                t              = $T
                toolConfigHash = $toolConfigHash
                runId          = $runId
            }
            commits             = [ordered]@{
                r           = $R
                b           = $B
                t           = $T
                mergeBaseRT = $mergeBaseRT
                mergeBaseBT = $mergeBaseBT
            }
            declaredCommitSet          = $declaredCommitSet
            declaredCommitSetSource    = $declaredSetSource
            declaredCommitSetSourceSha256 = $declaredCommitSetSourceSha256
            computedCommitSetBT        = $computedCommitSetBT
            computedCommitSetRT        = $computedCommitSetRT
            commitSetsEqualBT          = $commitSetsEqualBT
            commitSetsEqualRT          = $commitSetsEqualRT
            attributionValidBT         = $attributionValidBT
            rtLabel                    = $rtLabel
            pathMismatchBT             = $pathMismatchBT
            pathMismatchRT             = $pathMismatchRT
            emptyProductionDiffBT      = $emptyProductionDiffBT
            emptyProductionDiffRT      = $emptyProductionDiffRT
            changedProductionPathsBT   = $changedProductionPathsBT
            changedProductionPathsRT   = $changedProductionPathsRT
            measurableProductionPathsBT = [array]@($measurableProductionPathsBT)
            measurableProductionPathsRT = [array]@($measurableProductionPathsRT)
            startSnapshot              = $startSnapshot
            instrumentedTestCounts     = $instrumentedCounts
            uninstrumentedTestCounts   = $uninstrumentedCounts
            instrumentedTestIdentityCount   = $instrumentedTestIdentities.Count
            uninstrumentedTestIdentityCount = $uninstrumentedTestIdentities.Count
            collectionIdentityMatch    = $collectionIdentityMatch
            instrumentationSemanticsMatch = $instrumentationSemanticsMatch
            launcherExitCode           = $coverageExitCode
            uninstrumentedFullExitCode = $fullExitCode
            globalCoverage             = [ordered]@{
                lineRate   = $coverageXml.coverage.'line-rate'
                branchRate = $coverageXml.coverage.'branch-rate'
            }
            diffCoverReportRT   = [ordered]@{
                totalPercentCovered = $diffCoverReportRT.total_percent_covered
                totalNumLines       = $diffCoverReportRT.total_num_lines
                totalNumViolations  = $diffCoverReportRT.total_num_violations
                srcStatsFileCount   = $srcStatsKeysRT.Count
                label               = $rtLabel
            }
            diffCoverReportBT   = [ordered]@{
                totalPercentCovered = $diffCoverReportBT.total_percent_covered
                totalNumLines       = $diffCoverReportBT.total_num_lines
                totalNumViolations  = $diffCoverReportBT.total_num_violations
                srcStatsFileCount   = $srcStatsKeysBT.Count
                attributionValid    = $attributionValidBT
            }
            # ConvertTo-Json renders a one-element collection as a bare object
            # and a multi-element one as an array. A manifest whose field type
            # depends on cardinality is a trap for every consumer, so these
            # stay arrays at every count, including zero and one.
            changedPartialBranchesRT = [array]@($changedPartialBranchesRT)
            changedPartialBranchesBT = [array]@($changedPartialBranchesBT)
            toolVersions        = [ordered]@{
                uv  = $uvVersion
                git = $gitVersion
            }
            commands            = @(
                "pwsh -NoProfile -File $launcherPath -Route FullCoverage -CoverageRunId $runId"
                "pwsh -NoProfile -File $launcherPath -Route Full -CoverageRunId $runId"
                "uv run --locked --exact diff-cover $coverageXmlPath --compare-branch=$($tempRefs.R) --diff-range-notation=... --format=json:$diffCoverJsonRT --show-uncovered"
                "uv run --locked --exact diff-cover $coverageXmlPath --compare-branch=$($tempRefs.B) --diff-range-notation=... --format=json:$diffCoverJsonBT --show-uncovered"
            )
            artifactSha256      = $artifactHashes
        }

        ($manifest | ConvertTo-Json -Depth 12) | Set-Content -LiteralPath $manifestPath -Encoding utf8NoBOM

        # -- End-of-run verification: no drift, nothing entered Git status ----
        $endHeadSha = ((Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('rev-parse', 'HEAD')).Output | Select-Object -First 1).Trim().ToLowerInvariant()
        $endStatus = @((Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('status', '--porcelain')).Output | Where-Object { $_ })
        if ($endHeadSha -ne $startSnapshot.head -or $endStatus.Count -gt 0) {
            throw ("Target repository drifted during the run (start HEAD=$($startSnapshot.head), end HEAD=$endHeadSha, " +
                "end status entries=$($endStatus.Count)). Coverage evidence is invalid.")
        }

        # Written only after the manifest is fully flushed -- a half-written
        # run is never mistaken for a complete one.
        Set-Content -LiteralPath $completionMarkerPath -Value $runId -Encoding utf8NoBOM

        [pscustomobject]@{
            RunId                 = $runId
            StagingDirectory      = $stagingDir
            ManifestPath          = $manifestPath
            RTLabel               = $rtLabel
            AttributionValidBT    = $attributionValidBT
            GlobalLineRate        = $coverageXml.coverage.'line-rate'
            GlobalBranchRate      = $coverageXml.coverage.'branch-rate'
            # An empty production diff must read as empty, never as 100%.
            # diff-cover reports 100 over zero lines, and a human scanning this
            # summary would take that for "every changed line is covered" when
            # nothing was measured at all. The manifest already records
            # emptyProductionDiff*; this is the human-facing half of the same
            # rule.
            DiffCoverPercentRT    = if ($emptyProductionDiffRT) {
                'empty (no production changes)'
            } else {
                $diffCoverReportRT.total_percent_covered
            }
            DiffCoverPercentBT    = if ($emptyProductionDiffBT) {
                'empty (no production changes)'
            } else {
                $diffCoverReportBT.total_percent_covered
            }
            ChangedPartialBranchesBTCount = @($changedPartialBranchesBT).Count
        }
    } finally {
        # Temporary refs are namespaced by run ID and removed on every
        # handleable exit. Another run's leftovers are never enumerated or
        # touched -- that is documented, deliberate behavior, not an
        # omission (see design.md > "Make Windows artifacts reproducible and
        # status-clean").
        foreach ($refName in $tempRefs.Values) {
            Invoke-GitCommand -RepositoryRoot $RepositoryRoot -Arguments @('update-ref', '-d', $refName) -AllowNonZeroExit | Out-Null
        }
    }
} catch {
    [Console]::Error.WriteLine("[report-diff-coverage] $($_.Exception.Message)")
    exit 1
}

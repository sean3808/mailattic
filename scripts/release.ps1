<#
.SYNOPSIS
  Release a verified global bes snapshot under an annotated date-based tag.

.DESCRIPTION
  This script is dot-sourceable so its validation and orchestration functions can
  be exercised in isolated test repositories. Direct execution invokes the full
  release workflow defined by Invoke-BesRelease.
#>

param(
    [string]$Tag,
    [string]$Remote = 'origin'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Invoke-GitCapture {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [Parameter(Mandatory)]
        [string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0)
    )

    Push-Location -LiteralPath $RepoRoot
    try {
        $output = @(& git @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($exitCode -notin $AllowedExitCodes) {
        $detail = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        throw "git $($Arguments -join ' ') failed with exit code $exitCode. $detail"
    }

    [pscustomobject]@{
        ExitCode = $exitCode
        Output = (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine).Trim()
    }
}

function Test-BesReleaseTag {
    param(
        [Parameter(Mandatory)]
        [string]$Tag
    )

    if ($Tag -notmatch '^bes-v(?<year>\d{4})\.(?<month>\d{2})\.(?<day>\d{2})\.(?<sequence>[1-9]\d*)$') {
        return $false
    }

    $dateText = "$($Matches.year).$($Matches.month).$($Matches.day)"
    $parsedDate = [datetime]::MinValue
    return [datetime]::TryParseExact(
        $dateText,
        'yyyy.MM.dd',
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::None,
        [ref]$parsedDate
    )
}

function Assert-ReleasePreconditions {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [Parameter(Mandatory)]
        [string]$Tag,
        [Parameter(Mandatory)]
        [string]$Remote
    )

    $resolvedRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
    $gitRootResult = Invoke-GitCapture -RepoRoot $resolvedRoot -Arguments @(
        'rev-parse', '--show-toplevel'
    )
    $gitRoot = (Resolve-Path -LiteralPath $gitRootResult.Output).Path
    if ($gitRoot -ne $resolvedRoot) {
        throw "RepoRoot must be the Git repository root: $gitRoot"
    }

    if (-not (Test-BesReleaseTag -Tag $Tag)) {
        throw "Invalid release tag '$Tag'. Expected bes-vYYYY.MM.DD.N with a valid date and positive sequence."
    }

    $branch = (Invoke-GitCapture -RepoRoot $resolvedRoot -Arguments @(
        'symbolic-ref', '--quiet', '--short', 'HEAD'
    )).Output
    if ($branch -ne 'main') {
        throw "Release must run on main; current branch is '$branch'."
    }

    $worktree = (Invoke-GitCapture -RepoRoot $resolvedRoot -Arguments @(
        'status', '--porcelain', '--untracked-files=all'
    )).Output
    if ($worktree) {
        throw "Release requires a clean worktree, including untracked files."
    }

    $remoteCheck = Invoke-GitCapture -RepoRoot $resolvedRoot -Arguments @(
        'remote', 'get-url', $Remote
    ) -AllowedExitCodes @(0, 2, 128)
    if ($remoteCheck.ExitCode -ne 0) {
        throw "Git remote '$Remote' is not configured or cannot be resolved."
    }

    $localTag = Invoke-GitCapture -RepoRoot $resolvedRoot -Arguments @(
        'show-ref', '--verify', '--quiet', "refs/tags/$Tag"
    ) -AllowedExitCodes @(0, 1)
    if ($localTag.ExitCode -eq 0) {
        throw "Release tag '$Tag' already exists locally."
    }

    $remoteTag = Invoke-GitCapture -RepoRoot $resolvedRoot -Arguments @(
        'ls-remote', '--exit-code', '--tags', $Remote, "refs/tags/$Tag"
    ) -AllowedExitCodes @(0, 2)
    if ($remoteTag.ExitCode -eq 0) {
        throw "Release tag '$Tag' already exists on remote '$Remote'."
    }

    return (Invoke-GitCapture -RepoRoot $resolvedRoot -Arguments @(
        'rev-parse', '--verify', 'HEAD^{commit}'
    )).Output
}

function Assert-ReleaseSnapshot {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [Parameter(Mandatory)]
        [string]$ExpectedCommit
    )

    $currentBranch = (Invoke-GitCapture -RepoRoot $RepoRoot -Arguments @(
        'symbolic-ref', '--quiet', '--short', 'HEAD'
    )).Output
    if ($currentBranch -ne 'main') {
        throw "Release branch changed away from main to '$currentBranch'."
    }

    $currentCommit = (Invoke-GitCapture -RepoRoot $RepoRoot -Arguments @(
        'rev-parse', '--verify', 'HEAD^{commit}'
    )).Output
    if ($currentCommit -ne $ExpectedCommit) {
        throw "Release HEAD changed from '$ExpectedCommit' to '$currentCommit'."
    }

    $worktree = (Invoke-GitCapture -RepoRoot $RepoRoot -Arguments @(
        'status', '--porcelain', '--untracked-files=all'
    )).Output
    if ($worktree) {
        throw 'Release worktree changed after initial validation.'
    }
}

function Get-MatchingPackageManifest {
    param(
        [Parameter(Mandatory)]
        [string]$SourceRoot,
        [Parameter(Mandatory)]
        [string]$InstalledRoot
    )

    $resolvedSource = (Resolve-Path -LiteralPath $SourceRoot).Path
    $resolvedInstalled = (Resolve-Path -LiteralPath $InstalledRoot).Path
    $entries = [System.Collections.Generic.List[string]]::new()

    $sourceFiles = @(
        Get-ChildItem -LiteralPath $resolvedSource -Recurse -File |
            Where-Object {
                $_.Extension -ne '.pyc' -and
                $_.FullName -notmatch '(^|[\\/])__pycache__([\\/]|$)'
            } |
            Sort-Object {
                [IO.Path]::GetRelativePath($resolvedSource, $_.FullName)
            }
    )

    if ($sourceFiles.Count -eq 0) {
        throw "Source package contains no releasable files: $resolvedSource"
    }

    foreach ($sourceFile in $sourceFiles) {
        $relativePath = [IO.Path]::GetRelativePath(
            $resolvedSource,
            $sourceFile.FullName
        )
        $installedFile = Join-Path $resolvedInstalled $relativePath
        if (-not (Test-Path -LiteralPath $installedFile -PathType Leaf)) {
            throw "Installed package is missing '$relativePath'."
        }

        $sourceHash = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $installedHash = (Get-FileHash -LiteralPath $installedFile -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($sourceHash -ne $installedHash) {
            throw "Installed package mismatch for '$relativePath'."
        }

        $portablePath = $relativePath.Replace('\', '/')
        $entries.Add(('{0}:{1}' -f $portablePath, $sourceHash))
    }

    $manifestText = $entries -join [char]10
    $manifestBytes = [Text.Encoding]::UTF8.GetBytes($manifestText)
    $manifestHash = [Security.Cryptography.SHA256]::HashData($manifestBytes)
    return [Convert]::ToHexString($manifestHash).ToLowerInvariant()
}

function Get-InstalledPackagePath {
    param(
        [string]$ToolPythonOverride
    )

    if ($ToolPythonOverride) {
        $toolPython = (Resolve-Path -LiteralPath $ToolPythonOverride).Path
    } else {
        $toolDirOutput = @(& uv tool dir 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "uv tool dir failed with exit code $LASTEXITCODE."
        }
        $toolRoot = ($toolDirOutput | ForEach-Object { $_.ToString() }) -join ''
        $toolRoot = $toolRoot.Trim()
        if (-not $toolRoot) {
            throw 'uv tool dir returned an empty path.'
        }
        $toolPython = Join-Path $toolRoot 'mailattic\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $toolPython -PathType Leaf)) {
            throw "The isolated uv tool Python was not found: $toolPython"
        }
    }

    $importCode = 'import mailattic, pathlib; print(pathlib.Path(mailattic.__file__).resolve().parent)'
    $packageOutput = @(& $toolPython -I -c $importCode 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $detail = ($packageOutput | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        throw "Unable to import mailattic with the uv tool Python. $detail"
    }
    $packagePath = ($packageOutput | ForEach-Object { $_.ToString() }) -join ''
    $packagePath = $packagePath.Trim()
    if (-not (Test-Path -LiteralPath $packagePath -PathType Container)) {
        throw "Installed package path does not exist: $packagePath"
    }
    return (Resolve-Path -LiteralPath $packagePath).Path
}

function Assert-InstalledRelease {
    param(
        [Parameter(Mandatory)]
        [string]$SourceRoot,
        [Parameter(Mandatory)]
        [string]$InstalledPackageRoot,
        [string]$BesExecutable = 'bes'
    )

    @(& $BesExecutable --help 2>&1) | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Global bes --help failed with exit code $LASTEXITCODE."
    }

    $manifestParameters = @{
        SourceRoot = $SourceRoot
        InstalledRoot = $InstalledPackageRoot
    }
    return Get-MatchingPackageManifest @manifestParameters
}

function Get-ReleaseEvidencePath {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot
    )

    $gitPath = (Invoke-GitCapture -RepoRoot $RepoRoot -Arguments @(
        'rev-parse', '--git-path', 'bes-release-state.json'
    )).Output
    if ([IO.Path]::IsPathRooted($gitPath)) {
        return [IO.Path]::GetFullPath($gitPath)
    }
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot $gitPath))
}

function Write-ReleaseEvidence {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [Parameter(Mandatory)]
        [string]$Tag,
        [Parameter(Mandatory)]
        [string]$Commit,
        [Parameter(Mandatory)]
        [string]$ManifestHash
    )

    $evidencePath = Get-ReleaseEvidencePath -RepoRoot $RepoRoot
    $temporaryPath = "$evidencePath.tmp"
    $evidence = [ordered]@{
        schema_version = 1
        tag = $Tag
        commit = $Commit
        verified_at = [DateTimeOffset]::UtcNow.ToString(
            'yyyy-MM-ddTHH:mm:ss.fffZ',
            [Globalization.CultureInfo]::InvariantCulture
        )
        source_manifest_sha256 = $ManifestHash
    }
    $json = $evidence | ConvertTo-Json -Compress

    try {
        [IO.File]::WriteAllText(
            $temporaryPath,
            $json,
            [Text.UTF8Encoding]::new($false)
        )
        [IO.File]::Move($temporaryPath, $evidencePath, $true)
    } finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }

    return $evidencePath
}

function Get-ReleaseEvidenceSnapshot {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot
    )

    $evidencePath = Get-ReleaseEvidencePath -RepoRoot $RepoRoot
    $existed = Test-Path -LiteralPath $evidencePath -PathType Leaf
    $bytes = if ($existed) {
        [IO.File]::ReadAllBytes($evidencePath)
    } else {
        $null
    }
    return [PSCustomObject]@{
        Path = $evidencePath
        Existed = $existed
        Bytes = $bytes
    }
}

function Restore-ReleaseEvidenceSnapshot {
    param(
        [Parameter(Mandatory)]
        [PSCustomObject]$Snapshot
    )

    $temporaryPath = "$($Snapshot.Path).restore.tmp"
    try {
        if ($Snapshot.Existed) {
            [IO.File]::WriteAllBytes($temporaryPath, $Snapshot.Bytes)
            [IO.File]::Move($temporaryPath, $Snapshot.Path, $true)
        } elseif (Test-Path -LiteralPath $Snapshot.Path -PathType Leaf) {
            Remove-Item -LiteralPath $Snapshot.Path -Force
        }
    } finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

function Write-ReleaseStage {
    param(
        [Parameter(Mandatory)]
        [int]$Number,
        [Parameter(Mandatory)]
        [string]$Message
    )

    Write-Host ("[release] {0}/9 {1}" -f $Number, $Message)
}

function Invoke-PytestLauncher {
    param(
        [Parameter(Mandatory)]
        [string]$LauncherPath,
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    & pwsh -NoProfile -File $LauncherPath @Arguments
    $launcherExitCode = $LASTEXITCODE
    if ($launcherExitCode -ne 0) {
        $argText = $Arguments -join ' '
        throw (
            "scripts\invoke-pytest.ps1 $argText (canonical pytest gate) " +
            "failed with exit code $launcherExitCode."
        )
    }
}

function Invoke-GhJson {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    Push-Location -LiteralPath $RepoRoot
    try {
        $global:LASTEXITCODE = 0
        $output = @(& gh @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        return $null
    }

    $text = (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine).Trim()
    if (-not $text) {
        return $null
    }

    try {
        return ($text | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Get-HostedFullRunStatus {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [string]$Commit
    )

    if (-not $Commit) {
        return [pscustomobject]@{
            State = 'not found'
            RunUrl = $null
        }
    }

    $repoInfo = Invoke-GhJson -RepoRoot $RepoRoot -Arguments @(
        'repo', 'view', '--json', 'nameWithOwner'
    )
    if (-not $repoInfo -or -not $repoInfo.nameWithOwner) {
        return [pscustomobject]@{
            State = 'not found'
            RunUrl = $null
        }
    }
    $repoName = $repoInfo.nameWithOwner

    $runsPages = Invoke-GhJson -RepoRoot $RepoRoot -Arguments @(
        'api', '--paginate', '--slurp',
        "repos/$repoName/actions/workflows/ci.yml/runs?head_sha=$Commit&per_page=100"
    )
    if (-not $runsPages) {
        return [pscustomobject]@{
            State = 'not found'
            RunUrl = $null
        }
    }

    $runs = @(
        foreach ($page in @($runsPages)) {
            if ($page.workflow_runs) {
                $page.workflow_runs
            }
        }
    ) | Sort-Object -Property id -Descending

    foreach ($run in $runs) {
        $runUrl = $run.html_url
        $jobPages = Invoke-GhJson -RepoRoot $RepoRoot -Arguments @(
            'api', '--paginate', '--slurp',
            "repos/$repoName/actions/runs/$($run.id)/jobs?per_page=100&filter=all"
        )

        $hostedFullJob = $null
        if ($jobPages) {
            $hostedFullJob = @(
                foreach ($jobPage in @($jobPages)) {
                    if ($jobPage.jobs) {
                        $jobPage.jobs | Where-Object { $_.name -eq 'hosted-full' }
                    }
                }
            ) | Select-Object -First 1
        }

        if ($hostedFullJob) {
            $jobStatus = $hostedFullJob.status
            $jobConclusion = $hostedFullJob.conclusion
            if ($jobStatus -eq 'completed') {
                if ($jobConclusion -eq 'success') {
                    return [pscustomobject]@{
                        State = 'success'
                        RunUrl = $runUrl
                    }
                }
                if ($jobConclusion -in @('failure', 'cancelled', 'timed_out', 'action_required')) {
                    return [pscustomobject]@{
                        State = $jobConclusion
                        RunUrl = $runUrl
                    }
                }
                continue
            }
            if ($jobStatus -eq 'in_progress') {
                return [pscustomobject]@{
                    State = 'in progress'
                    RunUrl = $runUrl
                }
            }
            return [pscustomobject]@{
                State = 'queued'
                RunUrl = $runUrl
            }
        }

        if ($run.status -eq 'in_progress') {
            return [pscustomobject]@{
                State = 'in progress'
                RunUrl = $runUrl
            }
        }
        if ($run.status -in @('queued', 'waiting', 'requested', 'pending')) {
            return [pscustomobject]@{
                State = 'queued'
                RunUrl = $runUrl
            }
        }
        if ($run.status -eq 'completed' -and $run.conclusion -in @('failure', 'cancelled', 'timed_out', 'action_required')) {
            return [pscustomobject]@{
                State = $run.conclusion
                RunUrl = $runUrl
            }
        }
    }

    return [pscustomobject]@{
        State = 'not found'
        RunUrl = $null
    }
}

function Invoke-ReleaseTests {
    <#
    .SYNOPSIS
      Verify HEAD via hosted-full (or fall back to local -Route Full) and
      always run the local-only recipient rebuild tests via -Route Targeted.

    .DESCRIPTION
      Release inspects GitHub Actions `ci.yml` runs for HEAD (`hosted-full`
      job). When `hosted-full` succeeded for HEAD, local `-Route Full` is
      skipped; when it is in progress, queued, or not found, release falls
      back to `-Route Full` via `scripts/invoke-pytest.ps1` (canonical full pytest gate).
      A failed or cancelled `hosted-full` stops the release immediately. In all passing cases, the two gitignored local-only
      recipient rebuild test files run through `-Route Targeted`.

      Any non-zero exit code from the launcher -- an ordinary pytest
      failure, lock contention (17), or a runtime root whose volume cannot
      be established (18) -- is surfaced as a thrown release-test-stage
      failure, which
      Invoke-BesRelease propagates before reinstall, evidence, tag creation,
      or push ever run.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [string]$Commit
    )

    $launcherPath = Join-Path $RepoRoot 'scripts\invoke-pytest.ps1'
    if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
        throw "Canonical pytest launcher was not found: $launcherPath"
    }

    $hostedFull = Get-HostedFullRunStatus -RepoRoot $RepoRoot -Commit $Commit
    switch ($hostedFull.State) {
        'success' {
            Write-Host "[release] Accepted hosted-full run: $($hostedFull.RunUrl); skipping local Full"
        }
        { $_ -in @('failure', 'cancelled', 'timed_out', 'action_required') } {
            throw "hosted-full $($hostedFull.State) for commit $Commit ($($hostedFull.RunUrl)); release stopped."
        }
        default {
            Write-Host "[release] hosted-full $($hostedFull.State); falling back to local Full"
            Invoke-PytestLauncher -LauncherPath $launcherPath -Arguments @('-Route', 'Full')
        }
    }

    Invoke-PytestLauncher -LauncherPath $launcherPath -Arguments @(
        '-Route', 'Targeted',
        'tests/test_recipient_rebuild_audit.py',
        'tests/test_recipient_rebuild_apply.py'
    )
}

function Invoke-GlobalReinstall {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot
    )

    $reinstallPath = [IO.Path]::GetFullPath(
        (Join-Path $RepoRoot 'scripts\reinstall-global.ps1')
    )
    if (-not (Test-Path -LiteralPath $reinstallPath -PathType Leaf)) {
        throw "Global reinstall script was not found: $reinstallPath"
    }

    & $reinstallPath
    if (-not $?) {
        throw 'reinstall-global.ps1 failed.'
    }
}

function Sync-ReleaseSkill {
    param([Parameter(Mandatory)][string]$RepoRoot)

    Push-Location -LiteralPath $RepoRoot
    $previousEncoding = $env:PYTHONIOENCODING
    try {
        $env:PYTHONIOENCODING = 'utf-8'
        & uv run --locked --exact python scripts/deploy-skill-to-ssot.py --verify-required
        if ($LASTEXITCODE -ne 0) {
            throw "skill SSOT sync/verification failed with exit code $LASTEXITCODE."
        }
    } finally {
        $env:PYTHONIOENCODING = $previousEncoding
        Pop-Location
    }
}

function New-ReleaseTag {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [Parameter(Mandatory)]
        [string]$Tag,
        [Parameter(Mandatory)]
        [string]$Commit
    )

    Invoke-GitCapture -RepoRoot $RepoRoot -Arguments @(
        'tag', '-a', $Tag, $Commit, '-m', "bes release $Tag"
    ) | Out-Null
}

function Invoke-AtomicReleasePush {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [Parameter(Mandatory)]
        [string]$Tag,
        [Parameter(Mandatory)]
        [string]$Remote
    )

    Invoke-GitCapture -RepoRoot $RepoRoot -Arguments @(
        'push', '--atomic', $Remote, 'main', "refs/tags/$Tag"
    ) | Out-Null
}

function New-GitHubRelease {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [Parameter(Mandatory)]
        [string]$Tag
    )

    Push-Location -LiteralPath $RepoRoot
    try {
        $global:LASTEXITCODE = 0
        $output = @(& gh release create $Tag --verify-tag --generate-notes 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        $detail = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        throw "gh release create $Tag --verify-tag --generate-notes failed with exit code $exitCode. $detail"
    }
}

function Invoke-BesRelease {
    param(
        [Parameter(Mandatory)]
        [string]$RepoRoot,
        [Parameter(Mandatory)]
        [string]$Tag,
        [string]$Remote = 'origin'
    )

    $resolvedRoot = (Resolve-Path -LiteralPath $RepoRoot).Path

    Write-ReleaseStage -Number 1 -Message 'Validate release inputs'
    $preconditionParameters = @{
        RepoRoot = $resolvedRoot
        Tag = $Tag
        Remote = $Remote
    }
    $releaseCommit = Assert-ReleasePreconditions @preconditionParameters

    Write-ReleaseStage -Number 2 -Message 'Run full test suite'
    Invoke-ReleaseTests -RepoRoot $resolvedRoot -Commit $releaseCommit

    Write-ReleaseStage -Number 3 -Message 'Reinstall global bes'
    Invoke-GlobalReinstall -RepoRoot $resolvedRoot

    Write-ReleaseStage -Number 4 -Message 'Verify installed snapshot'
    $installedPackage = Get-InstalledPackagePath
    $sourcePackage = Join-Path $resolvedRoot 'src\mailattic'
    $verificationParameters = @{
        SourceRoot = $sourcePackage
        InstalledPackageRoot = $installedPackage
    }
    $manifestHash = Assert-InstalledRelease @verificationParameters
    Assert-ReleaseSnapshot -RepoRoot $resolvedRoot -ExpectedCommit $releaseCommit

    Write-ReleaseStage -Number 5 -Message 'Sync and verify skill SSOT'
    try {
        Sync-ReleaseSkill -RepoRoot $resolvedRoot
        Assert-ReleaseSnapshot -RepoRoot $resolvedRoot -ExpectedCommit $releaseCommit
    } catch {
        throw (
            "Global BES is already installed and verified; skill SSOT stage failed. " +
            "No new release evidence/tag/push was produced; installation was not rolled back. " +
            "Fix the cause and retry scripts/release.ps1 -Tag $Tag -Remote $Remote. " +
            "Cause: $($_.Exception.Message)"
        )
    }

    Write-ReleaseStage -Number 6 -Message 'Write local release evidence'
    $evidenceSnapshot = Get-ReleaseEvidenceSnapshot -RepoRoot $resolvedRoot
    $evidenceParameters = @{
        RepoRoot = $resolvedRoot
        Tag = $Tag
        Commit = $releaseCommit
        ManifestHash = $manifestHash
    }
    $evidencePath = Write-ReleaseEvidence @evidenceParameters
    Write-Host "[release] Evidence: $evidencePath"

    Write-ReleaseStage -Number 7 -Message 'Create annotated release tag'
    try {
        New-ReleaseTag -RepoRoot $resolvedRoot -Tag $Tag -Commit $releaseCommit
    } catch {
        $tagError = $_.Exception.Message
        try {
            Restore-ReleaseEvidenceSnapshot -Snapshot $evidenceSnapshot
        } catch {
            throw (
                "Local installation was verified, but tag '$Tag' was not created and " +
                "release evidence could not be restored. Tag cause: $tagError. " +
                "Restore cause: $($_.Exception.Message)"
            )
        }
        throw (
            "Local installation was verified, but tag '$Tag' was not created. " +
            "Release evidence was restored. Cause: $tagError"
        )
    }

    Write-ReleaseStage -Number 8 -Message 'Atomically push main and release tag'
    try {
        $pushParameters = @{
            RepoRoot = $resolvedRoot
            Tag = $Tag
            Remote = $Remote
        }
        Invoke-AtomicReleasePush @pushParameters
    } catch {
        $retry = "git push --atomic $Remote main refs/tags/$Tag"
        throw (
            "Local deployment, evidence, and tag succeeded, but the remote release did not complete. " +
            "Retry with: $retry. Cause: $($_.Exception.Message)"
        )
    }

    Write-ReleaseStage -Number 9 -Message 'Create GitHub Release'
    try {
        New-GitHubRelease -RepoRoot $resolvedRoot -Tag $Tag
    } catch {
        $retry = "gh release create $Tag --verify-tag --generate-notes"
        throw (
            "Local deployment, evidence, tag, and remote push succeeded, but GitHub Release creation failed. " +
            "The tag was NOT rolled back. Retry with: $retry. Cause: $($_.Exception.Message)"
        )
    }

    Write-Host "[release] Complete: $Tag at $releaseCommit"
}

if ($MyInvocation.InvocationName -ne '.') {
    if (-not $Tag) {
        Write-Error "-Tag is required. Example: -Tag bes-v2026.07.18.1"
        exit 2
    }
    try {
        $repoRoot = Split-Path -Parent $PSScriptRoot
        Invoke-BesRelease -RepoRoot $repoRoot -Tag $Tag -Remote $Remote
    } catch {
        [Console]::Error.WriteLine("[release] ERROR: $($_.Exception.Message)")
        exit 1
    }
}

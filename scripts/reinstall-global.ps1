<#
.SYNOPSIS
  Rebuild the wheel and reinstall the global 'bes' command from the new wheel.

.DESCRIPTION
  Deterministic reinstall steps:
    1. Clean dist/ (removes stale artifacts).
    2. Run 'uv build' to produce exactly one wheel.
    3. Remove the existing global 'bes' tool (uv tool uninstall).
    4. Install the new wheel with 'uv tool install --force <wheel>' (non-editable,
       overwrite semantics).

  If a .venv/lib64 reparse-point lock (PC-017: Windows + WSL shared-venv condition)
  is encountered during the operation, the shared cleanup helper is applied to clear
  the lock — without deleting .venv or running uv sync.

  IMPORTANT: This script does NOT delete the repository .venv, does NOT run uv sync,
  and does NOT invoke rebuild-venv.ps1. Those are development-environment operations
  that are outside the scope of a global-tool reinstall.

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/reinstall-global.ps1
#>

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Load shared reparse-point cleanup helper (used only if lib64 lock is encountered).
. (Join-Path $PSScriptRoot 'venv-reparse-helper.ps1')

$repoRoot = Split-Path -Parent $PSScriptRoot
$distDir  = Join-Path $repoRoot 'dist'
$venvPath = Join-Path $repoRoot '.venv'

Write-Host "Repo root : $repoRoot"
Write-Host ""

# Step 1: clean dist/ to guarantee deterministic wheel selection.
if (Test-Path -LiteralPath $distDir) {
    Write-Host "Cleaning dist/ ..."
    Remove-Item -LiteralPath $distDir -Recurse -Force
    Write-Host "dist/ cleaned."
} else {
    Write-Host "dist/ does not exist; skipping clean."
}

# Step 2: build the wheel — apply reparse-point cleanup only if the build hits PC-017.
Write-Host ""
Write-Host "Building wheel ..."
Push-Location $repoRoot
try {
    & uv build 2>&1 | Tee-Object -Variable buildOutput | Write-Host
    $buildExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($buildExitCode -ne 0) {
    # Check whether the failure looks like a reparse-point / access-denied lock (PC-017).
    $outputText = $buildOutput -join "`n"
    $looksLikeReparseLock = ($outputText -match '存取被拒' -or $outputText -match 'os error 5' -or
                             $outputText -match 'lib64')

    if ($looksLikeReparseLock -and (Test-Path -LiteralPath $venvPath)) {
        Write-Host ""
        Write-Warning "uv build failed with reparse-point lock (PC-017). Applying shared cleanup helper ..."
        Remove-VenvReparsePoints -VenvPath $venvPath

        # Retry build after clearing the lock.
        Write-Host ""
        Write-Host "Retrying build after reparse-point cleanup ..."
        Push-Location $repoRoot
        try {
            & uv build
            if ($LASTEXITCODE -ne 0) {
                throw "uv build failed again (exit code $LASTEXITCODE) after reparse-point cleanup."
            }
        } finally {
            Pop-Location
        }
    } else {
        throw "uv build exited with code $buildExitCode"
    }
}

# Step 3: locate the wheel by exact path.
$wheels = @(Get-ChildItem -LiteralPath $distDir -Filter '*.whl' -File)
if ($wheels.Count -ne 1) {
    throw "Expected exactly 1 wheel in dist/, found $($wheels.Count). " +
          "Ensure dist/ was clean before building."
}
$wheelPath = $wheels[0].FullName
Write-Host "Wheel     : $wheelPath"

# Step 4: remove existing global 'bes' (if present) then install new wheel.
# Using uninstall + install rather than relying solely on --force, to ensure a clean
# state (e.g. removes any editable install metadata that --force alone might leave).
Write-Host ""
Write-Host "Removing existing global 'bes' (if installed) ..."
& uv tool uninstall mailattic 2>&1 | Write-Host
# uv tool uninstall exits non-0 if the tool is not installed — that is acceptable here.
# We do not throw; we proceed to install regardless.

Write-Host ""
Write-Host "Installing global 'bes' from new wheel (non-editable, --force) ..."
& uv tool install --force $wheelPath
if ($LASTEXITCODE -ne 0) {
    throw "uv tool install exited with code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Done. Global 'bes' reinstalled from fresh wheel. Repo .venv untouched."
Write-Host "Verify with: bes --help"

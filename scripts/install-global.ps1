<#
.SYNOPSIS
  Build a wheel and install the global 'bes' command from that distribution wheel.

.DESCRIPTION
  Produces a non-editable, working-tree-decoupled build snapshot of the global 'bes'
  tool via the following deterministic steps:
    1. Clean dist/ (removes any stale artifacts from previous builds).
    2. Run 'uv build' to produce exactly one wheel.
    3. Select that wheel by exact path (not a glob that could match stale artifacts).
    4. Install with 'uv tool install --force <wheel>' — overwrite semantics replace
       any existing editable or older global installation.

  After running this script, the global 'bes' reflects a fixed build snapshot.
  Subsequent changes to the repository working tree do NOT affect the global command
  until this script (or reinstall-global.ps1) is run again.

  NOTE: This script does NOT install in editable mode (-e).

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/install-global.ps1
#>

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$repoRoot = Split-Path -Parent $PSScriptRoot
$distDir  = Join-Path $repoRoot 'dist'

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

# Step 2: build the wheel.
Write-Host ""
Write-Host "Building wheel ..."
Push-Location $repoRoot
try {
    & uv build
    if ($LASTEXITCODE -ne 0) {
        throw "uv build exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

# Step 3: locate the wheel by exact path.
# uv build places exactly one .whl in dist/ after a clean build.
$wheels = @(Get-ChildItem -LiteralPath $distDir -Filter '*.whl' -File)
if ($wheels.Count -ne 1) {
    throw "Expected exactly 1 wheel in dist/, found $($wheels.Count). " +
          "Ensure dist/ was clean before building."
}
$wheelPath = $wheels[0].FullName
Write-Host "Wheel     : $wheelPath"

# Step 4: install with --force (overwrite any existing editable / older tool).
# NOT using -e / --editable — this must be a non-editable build snapshot.
Write-Host ""
Write-Host "Installing global 'bes' from wheel (non-editable, --force) ..."
& uv tool install --force $wheelPath
if ($LASTEXITCODE -ne 0) {
    throw "uv tool install exited with code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Done. Global 'bes' is now a fixed build snapshot decoupled from the working tree."
Write-Host "Verify with: bes --help"

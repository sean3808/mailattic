# Rebuild .venv when uv sync hits PC-017 (.venv/lib64 reparse-point file lock).
# Symptom: `uv sync` fails with: failed to remove file `...\.venv\lib64`: 存取被拒。 (os error 5)
# Root cause: WSL-created .venv leaves a Linux-style lib64 -> lib symlink as a Windows
# reparse point that uv (running from Windows) cannot delete via its normal cleanup.
# Fix: nuke .venv from Windows side (handles reparse points), then re-sync.

$ErrorActionPreference = 'Stop'

# Load shared reparse-point cleanup helper.
. (Join-Path $PSScriptRoot 'venv-reparse-helper.ps1')

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repoRoot '.venv'

Write-Host "Repo root: $repoRoot"
Write-Host "Target   : $venvPath"

if (Test-Path -LiteralPath $venvPath) {
    Write-Host "Removing existing .venv ..."

    # Step 1: remove reparse points first (lib64 -> lib symlink) via shared helper.
    Remove-VenvReparsePoints -VenvPath $venvPath

    # Step 2: remove the rest.
    Remove-Item -LiteralPath $venvPath -Recurse -Force
    Write-Host ".venv removed."
} else {
    Write-Host ".venv does not exist; nothing to remove."
}

# Step 3: re-sync via uv.
Write-Host ""
Write-Host "Running: uv sync"
Push-Location $repoRoot
try {
    & uv sync
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Done. Verify with: uv run bes --help"

# Shared helper: remove .venv/lib64 reparse-point locks before uv operations.
# PC-017: Windows + WSL shared-venv leaves lib64 as a Windows reparse point that
# uv (running from Windows) cannot delete via its normal cleanup path, causing:
#   "failed to remove file `...\.venv\lib64`: 存取被拒。 (os error 5)"
#
# Usage: dot-source this file, then call Remove-VenvReparsePoints with the .venv path.
#   . (Join-Path $PSScriptRoot 'venv-reparse-helper.ps1')
#   Remove-VenvReparsePoints -VenvPath (Join-Path $repoRoot '.venv')

function Remove-VenvReparsePoints {
    <#
    .SYNOPSIS
        Remove reparse-point entries (junctions / symlinks) inside a .venv directory.
    .DESCRIPTION
        Enumerates all reparse points under VenvPath recursively and deletes them using
        .NET IO APIs that handle reparse-point semantics correctly (no target traversal).
        This is a targeted cleanup — it does NOT delete the .venv itself or run uv sync.
    .PARAMETER VenvPath
        Full path to the .venv directory to clean. The directory must exist.
    .OUTPUTS
        None. Writes progress to host. Warnings on per-item failures (non-fatal).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$VenvPath
    )

    if (-not (Test-Path -LiteralPath $VenvPath)) {
        Write-Warning "Remove-VenvReparsePoints: VenvPath does not exist: $VenvPath"
        return
    }

    Write-Host "Scanning for reparse points in: $VenvPath"

    # -Attributes ReparsePoint lists junctions/symlinks without traversing into them.
    $reparseItems = Get-ChildItem -LiteralPath $VenvPath -Recurse -Force `
        -Attributes ReparsePoint -ErrorAction SilentlyContinue

    if (-not $reparseItems) {
        Write-Host "  No reparse points found."
        return
    }

    foreach ($item in $reparseItems) {
        Write-Host "  reparse: $($item.FullName)"
        try {
            if ($item.PSIsContainer) {
                # Directory reparse points (junctions / dir symlinks): Delete without
                # following into the link target (second arg $false = non-recursive).
                [System.IO.Directory]::Delete($item.FullName, $false)
            } else {
                [System.IO.File]::Delete($item.FullName)
            }
        } catch {
            Write-Warning "  failed to remove reparse point: $($item.FullName) — $($_.Exception.Message)"
        }
    }

    Write-Host "Reparse-point cleanup done."
}

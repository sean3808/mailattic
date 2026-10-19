<#
.SYNOPSIS
  Install / update the daily mbsync IMAP sync Windows Task Scheduler task.

.DESCRIPTION
  Reads scripts/mbsync-daily.xml (UTF-8 source-of-truth, version controlled),
  substitutes placeholders, converts to UTF-16 LE with BOM (Task Scheduler import requirement),
  and imports via schtasks /create /xml /F.

.PARAMETER TaskName
  Name of the Windows Scheduled Task. Default: "mailattic mbsync daily".

.PARAMETER TaskFolder
  Task Scheduler library folder the task is registered under. Default: "MyTasks".
  Operator-owned tasks live in a named folder so the Task Scheduler root stays readable;
  schtasks creates the folder on demand. Pass "" to register at the root.

.PARAMETER StartTime
  Daily trigger time in HH:mm (24h, local time). Default: "08:45".

.PARAMETER BesPath
  Full path to the decoupled global bes.exe (the scheduled task Command). When omitted,
  resolved at install time from `uv tool dir --bin`\bes.exe — the build-snapshot wheel
  installed via `uv tool`, decoupled from the repo development venv. The scheduled task calls
  this by absolute path (not bare `bes` on PATH) because the S4U scheduled context PATH may
  not include the uv tool bin dir. Fails loud if the resolved path does not exist (run
  scripts/install-global.ps1 first).

.PARAMETER DbPath
  Windows path to the canonical SQLite DB passed via --database. Default: repo-root mailattic.db.

.PARAMETER Uninstall
  Remove the task instead of installing.

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/install-task-scheduler.ps1

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/install-task-scheduler.ps1 -StartTime "09:00"

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/install-task-scheduler.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string]$TaskName   = "mailattic mbsync daily",
    [string]$TaskFolder = "MyTasks",
    [string]$StartTime  = "08:45",
    [string]$BesPath    = "",
    [string]$DbPath     = "C:\IT_Projects\mailattic\mailattic.db",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Fully-qualified task path. schtasks treats the folder as part of /tn and creates it
# on demand; the XML <URI> must carry the same value or the imported task lands at the
# root regardless of /tn.
$taskFolderNorm = $TaskFolder.Trim('\')
$taskUri = if ($taskFolderNorm) { "\$taskFolderNorm\$TaskName" } else { "\$TaskName" }
$taskPathForCmdlet = if ($taskFolderNorm) { "\$taskFolderNorm\" } else { "\" }

# Registering an S4U task requires elevation. Without this check schtasks fails with a
# bare "ERROR: Access is denied." that names neither the cause nor the fix.
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($currentIdentity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must run elevated — the task uses S4U logon, which schtasks refuses to register without Administrator rights. Re-run from an elevated PowerShell: pwsh -ExecutionPolicy Bypass -File $($MyInvocation.MyCommand.Path)"
}

if ($Uninstall) {
    Write-Host "Removing scheduled task: $taskUri" -ForegroundColor Yellow
    schtasks /delete /tn $taskUri /f
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /delete failed with exit code $LASTEXITCODE"
    }
    Write-Host "OK: task removed." -ForegroundColor Green
    return
}

$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$templatePath = Join-Path $scriptDir "mbsync-daily.xml"
if (-not (Test-Path $templatePath)) {
    throw "Template not found: $templatePath"
}

# Resolve the decoupled global bes.exe by absolute path. The scheduled task must NOT
# go through `uv run`/the repo development venv — the daily sync must survive branch
# switches and a half-rebuilt .venv (see harden-sync-scheduling design).
if (-not $BesPath) {
    $binDir = (& uv tool dir --bin).Trim()
    $BesPath = Join-Path $binDir "bes.exe"
}
if (-not (Test-Path $BesPath)) {
    throw "Global bes not found at: $BesPath. Install it first with: pwsh -ExecutionPolicy Bypass -File scripts/install-global.ps1"
}

# StartBoundary = tomorrow at StartTime (so first run is at least one day in the future)
$tomorrow = (Get-Date).AddDays(1).Date
$timeParts = $StartTime.Split(":")
if ($timeParts.Length -ne 2) {
    throw "Invalid -StartTime '$StartTime'; expected HH:mm (e.g. 08:45)"
}
$startBoundary = $tomorrow.AddHours([int]$timeParts[0]).AddMinutes([int]$timeParts[1])
$startBoundaryStr = $startBoundary.ToString("yyyy-MM-ddTHH:mm:ss")

Write-Host "Installing task '$taskUri':" -ForegroundColor Cyan
Write-Host "  StartBoundary : $startBoundaryStr (local)" -ForegroundColor Gray
Write-Host "  BesPath       : $BesPath" -ForegroundColor Gray
Write-Host "  DbPath        : $DbPath" -ForegroundColor Gray

$xmlContent = Get-Content -LiteralPath $templatePath -Raw -Encoding utf8

# Escape operator-supplied values before injecting into XML text — paths/names may
# contain & < > " (e.g. D:\A&B\...), which would otherwise produce invalid XML or
# alter the imported task definition.
function Esc([string]$v) { [System.Security.SecurityElement]::Escape($v) }

$xmlContent = $xmlContent.Replace("__TASK_URI__",       (Esc $taskUri))
$xmlContent = $xmlContent.Replace("__START_BOUNDARY__", $startBoundaryStr)
$xmlContent = $xmlContent.Replace("__BES_PATH__",       (Esc $BesPath))
$xmlContent = $xmlContent.Replace("__DB_PATH__",        (Esc $DbPath))

# Fail loud if any placeholder survived substitution (drift guard: XML template
# and this script must stay in lockstep).
$leftover = [regex]::Matches($xmlContent, "__[A-Z_]+__") | ForEach-Object { $_.Value } | Sort-Object -Unique
if ($leftover) {
    throw "Unsubstituted placeholder(s) remain in XML: $($leftover -join ', '). Template and install script are out of sync."
}

# Task Scheduler requires UTF-16 LE BOM + encoding="UTF-16" declaration.
# Template declaration is already encoding="UTF-16"; just write bytes in UTF-16 LE BOM.
$tempXml = Join-Path $env:TEMP "mbsync-daily-$([System.Guid]::NewGuid().ToString('N')).xml"
try {
    [System.IO.File]::WriteAllText($tempXml, $xmlContent, [System.Text.Encoding]::Unicode)

    schtasks /create /xml $tempXml /tn $taskUri /f
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /create failed with exit code $LASTEXITCODE"
    }
} finally {
    if (Test-Path $tempXml) { Remove-Item $tempXml -Force }
}

Write-Host ""
Write-Host "OK: task installed. Verify with:" -ForegroundColor Green
Write-Host "  Get-ScheduledTask -TaskPath '$taskPathForCmdlet' -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host "  Get-ScheduledTaskInfo -TaskPath '$taskPathForCmdlet' -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host ""
Write-Host "Manual trigger (one-shot test):" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskPath '$taskPathForCmdlet' -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host "  # then watch .mailattic-work/logs/mbsync-$(Get-Date -Format 'yyyy-MM-dd').log for new entry" -ForegroundColor Gray

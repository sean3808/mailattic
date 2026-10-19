<#
.SYNOPSIS
  Install / update the daily archive scraper Windows Task Scheduler task.

.DESCRIPTION
  Reads scripts/archive-scrape-daily.xml (UTF-8 source-of-truth, version controlled),
  substitutes placeholders, converts to UTF-16 LE with BOM (Task Scheduler import requirement),
  and imports via schtasks /create /xml /F.

  Scraper runs the decoupled global bes (`bes scrape-archive --once`) Windows-native by absolute
  path — no WSL shell, no /mnt/d, no `uv run`, so the scheduled run cannot create reparse-point
  artifacts in the Windows development venv (harden-sync-scheduling change). Scrape is pure-Python
  HTTP, so it needs no WSL. Credentials load from the mail server's credential file (host-resolvable, so no WSL
  path override). Default trigger time is 02:00 Taipei, well apart from the daily mbsync run, to
  avoid SQLite write contention (add-archive-scraper Decision 3).

.PARAMETER TaskName
  Name of the Windows Scheduled Task. Default: "MailatticArchiveScrape".

.PARAMETER StartTime
  Daily trigger time in HH:mm (24h, local time). Default: "02:00".

.PARAMETER BesPath
  Full path to the decoupled global bes.exe (the scheduled task Command). When omitted,
  resolved at install time from `uv tool dir --bin`\bes.exe — the build-snapshot wheel installed
  via `uv tool`, decoupled from the repo development venv. Called by absolute path (not bare `bes`
  on PATH) because the S4U scheduled context PATH may not include the uv tool bin dir. Fails loud
  if the resolved path does not exist (run scripts/install-global.ps1 first).

.PARAMETER Uninstall
  Remove the task instead of installing.

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/install-archive-scrape-task.ps1

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/install-archive-scrape-task.ps1 -StartTime "03:30"

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File scripts/install-archive-scrape-task.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string]$TaskName  = "MailatticArchiveScrape",
    [string]$StartTime = "02:00",
    [string]$BesPath   = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($Uninstall) {
    Write-Host "Removing scheduled task: $TaskName" -ForegroundColor Yellow
    schtasks /delete /tn $TaskName /f
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /delete failed with exit code $LASTEXITCODE"
    }
    Write-Host "OK: task removed." -ForegroundColor Green
    return
}

$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$templatePath = Join-Path $scriptDir "archive-scrape-daily.xml"
if (-not (Test-Path $templatePath)) {
    throw "Template not found: $templatePath"
}

# Resolve the decoupled global bes.exe by absolute path. The scheduled scrape must run
# Windows-native — no WSL, no /mnt/d, no `uv run` — so it cannot create reparse-point
# artifacts in the Windows development venv (harden-sync-scheduling design).
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
    throw "Invalid -StartTime '$StartTime'; expected HH:mm (e.g. 02:00)"
}
$startBoundary = $tomorrow.AddHours([int]$timeParts[0]).AddMinutes([int]$timeParts[1])
$startBoundaryStr = $startBoundary.ToString("yyyy-MM-ddTHH:mm:ss")

Write-Host "Installing task '$TaskName':" -ForegroundColor Cyan
Write-Host "  StartBoundary : $startBoundaryStr (local)" -ForegroundColor Gray
Write-Host "  BesPath       : $BesPath" -ForegroundColor Gray

$xmlContent = Get-Content -LiteralPath $templatePath -Raw -Encoding utf8

$xmlContent = $xmlContent.Replace("__TASK_NAME__",      $TaskName)
$xmlContent = $xmlContent.Replace("__START_BOUNDARY__", $startBoundaryStr)
$xmlContent = $xmlContent.Replace("__BES_PATH__",       $BesPath)

# Task Scheduler requires UTF-16 LE BOM + encoding="UTF-16" declaration.
# Template declaration is already encoding="UTF-16"; just write bytes in UTF-16 LE BOM.
$tempXml = Join-Path $env:TEMP "archive-scrape-daily-$([System.Guid]::NewGuid().ToString('N')).xml"
try {
    [System.IO.File]::WriteAllText($tempXml, $xmlContent, [System.Text.Encoding]::Unicode)

    schtasks /create /xml $tempXml /tn $TaskName /f
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /create failed with exit code $LASTEXITCODE"
    }
} finally {
    if (Test-Path $tempXml) { Remove-Item $tempXml -Force }
}

Write-Host ""
Write-Host "OK: task installed. Verify with:" -ForegroundColor Green
Write-Host "  Get-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host ""
Write-Host "Manual trigger (one-shot test):" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host "  # then watch .archive-scrape-logs/archive-scrape-$(Get-Date -Format 'yyyy-MM-dd').log for new entry" -ForegroundColor Gray

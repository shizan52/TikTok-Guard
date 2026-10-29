# Run ONE command line as LocalSystem (the account the bot's Windows service runs as) and print its output.
#   powershell -ExecutionPolicy Bypass -File deploy\as_system.ps1 -Command "venv\Scripts\python.exe run.py doctor"
#
# Why this exists: a Chromium profile's cookies are encrypted with the Windows user that wrote them (DPAPI).
# The service runs as LocalSystem, so everything that opens the bot's profiles - restoring them from the bundle, a manual
# `scan`, `doctor`, a test reply - must run as LocalSystem too. Running it as another user would make the browser fail to
# decrypt the cookies and then save a logged-out state over them. Needs an elevated shell (an admin SSH session is one).
param(
    [Parameter(Mandatory = $true)][string]$Command,
    [string]$WorkDir = (Resolve-Path "$PSScriptRoot\..").Path,
    [int]$TimeoutSec = 900
)
$ErrorActionPreference = "Stop"
$id   = "tgsys-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
$logs = Join-Path $WorkDir "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$cmd  = Join-Path $logs "$id.cmd"
$out  = Join-Path $logs "$id.out"
$code = Join-Path $logs "$id.code"
# a tiny batch file keeps quoting trivial: the command, its output captured, its exit code recorded
@"
@echo off
cd /d "$WorkDir"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
$Command > "$out" 2>&1
echo %errorlevel% > "$code"
"@ | Set-Content -Encoding ASCII $cmd

$action    = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$cmd`""
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds ($TimeoutSec + 60)) -Priority 7
try {
    Register-ScheduledTask -TaskName $id -Action $action -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $id
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while (-not (Test-Path $code) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
    if (-not (Test-Path $code)) {
        Stop-ScheduledTask -TaskName $id -ErrorAction SilentlyContinue
        Write-Host "TIMEOUT after $TimeoutSec s"
    }
    if (Test-Path $out) { Get-Content $out -Encoding UTF8 }
    $exit = if (Test-Path $code) { [int](Get-Content $code -Raw).Trim() } else { 124 }
    Write-Host "[exit $exit]"
}
finally {
    Unregister-ScheduledTask -TaskName $id -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $cmd, $out, $code -Force -ErrorAction SilentlyContinue
}
exit $exit

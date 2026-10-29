# Windows server setup (run from an elevated PowerShell, inside the extracted bundle folder - which becomes the app folder):
#   powershell -ExecutionPolicy Bypass -File deploy\install_windows.ps1
# 1st run creates the venv and .env and stops if secrets are missing; run it again once .env is filled in.
#
# Needs: Python 3.11+ and Brave. Changes nothing outside this folder except ONE Windows service, which follows the
# convention of a box that runs other services under NSSM: LocalSystem, automatic start, restart on failure.
param(
    [string]$ServiceName = "tiktok-guard",
    [string]$Python = "",                                   # default: python on PATH / py launcher
    [string]$Nssm = "",                                     # default: nssm on PATH, then C:\tools\nssm\nssm.exe
    [switch]$SkipRestore,
    [switch]$NoStart                                        # install everything, leave the service stopped (to test first)
)
$ErrorActionPreference = "Stop"
$App = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $App
$py = Join-Path $App "venv\Scripts\python.exe"

Write-Host "== 1/5 python environment (no browser download) =="
if (-not (Test-Path $py)) {
    if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $Python) { $Python = "py" }
    & $Python -m venv "$App\venv"
}
$env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1"
& $py -m pip install --quiet --disable-pip-version-check -r "$App\requirements.txt"

Write-Host "== 2/5 .env =="
$brave = @("C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
           "C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not (Test-Path "$App\.env")) {
    Copy-Item "$App\.env.template" "$App\.env"
    if ($brave) { (Get-Content "$App\.env") -replace '^BFP_PATCHED_CHROMIUM=.*', "BFP_PATCHED_CHROMIUM=$brave" | Set-Content -Encoding UTF8 "$App\.env" }
    icacls "$App\.env" /inheritance:r /grant:r "SYSTEM:(F)" "Administrators:(F)" | Out-Null
    Write-Host ">> EDIT $App\.env now: OPENROUTER_API_KEY and TELEGRAM_BOT_TOKEN, then run this script again."
    exit 0
}
$envText = Get-Content "$App\.env" -Raw
foreach ($k in "OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN") {
    if ($envText -notmatch "(?m)^$k=\s*[^#\s]") { throw "$k is empty in $App\.env" }
}

Write-Host "== 3/5 rebuild the profiles from the bundle (TikTok-only state) - as LocalSystem, like the service =="
$states = @(Get-ChildItem "$App\profiles\*\state.json" -ErrorAction SilentlyContinue)
if ($states.Count -gt 0 -and -not $SkipRestore) {
    & "$PSScriptRoot\as_system.ps1" -Command "`"$py`" run.py restore-profiles --bundle `"$App`"" -WorkDir $App
    if ($LASTEXITCODE -ne 0) { throw "restore-profiles failed - read the output above; the service was NOT installed" }
} else {
    Write-Host "nothing to restore ($($states.Count) state file(s))"
}
$template = Get-Content "$App\data\accounts.template.json" -Raw | ConvertFrom-Json
$master   = "$App\profiles\$($template.master)\User Data"
if (Test-Path $master) {
    $line = "BFP_USER_DATA_DIR=$master"
    $lines = Get-Content "$App\.env"
    if ($lines -match '^BFP_USER_DATA_DIR=') { $lines = $lines -replace '^BFP_USER_DATA_DIR=.*', $line } else { $lines += $line }
    $lines | Set-Content -Encoding UTF8 "$App\.env"
    Write-Host "set $line"
}

Write-Host "== 4/5 service '$ServiceName' (LocalSystem, below-normal priority, restart on failure) =="
if (-not $Nssm) {
    $Nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
    if (-not $Nssm -and (Test-Path "C:\tools\nssm\nssm.exe")) { $Nssm = "C:\tools\nssm\nssm.exe" }
}
if (-not $Nssm) { throw "nssm.exe not found - pass -Nssm <path>" }
New-Item -ItemType Directory -Force "$App\data\logs" | Out-Null
$ErrorActionPreference = "Continue"      # nssm writes ordinary messages to stderr; judge it by its exit code instead
function Invoke-Nssm {
    $out = (& $Nssm @args 2>&1 | Out-String) -replace "`0", ""       # nssm prints UTF-16
    if ($LASTEXITCODE -ne 0) { throw "nssm $($args -join ' ') failed: $out" }
    return $out.Trim()
}
if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    Invoke-Nssm install $ServiceName $py "run.py serve" | Out-Null
}
Invoke-Nssm set $ServiceName Application $py | Out-Null
Invoke-Nssm set $ServiceName AppParameters "run.py serve" | Out-Null
Invoke-Nssm set $ServiceName AppDirectory $App | Out-Null
Invoke-Nssm set $ServiceName DisplayName "TikTok comment guard (Telegram bot + monitor)" | Out-Null
Invoke-Nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
Invoke-Nssm set $ServiceName ObjectName LocalSystem | Out-Null
Invoke-Nssm set $ServiceName AppPriority BELOW_NORMAL_PRIORITY_CLASS | Out-Null      # never compete with other services on the box
Invoke-Nssm set $ServiceName AppEnvironmentExtra "PYTHONUTF8=1" "PYTHONIOENCODING=utf-8" "PYTHONUNBUFFERED=1" | Out-Null
Invoke-Nssm set $ServiceName AppStdout "$App\data\logs\service.out.log" | Out-Null
Invoke-Nssm set $ServiceName AppStderr "$App\data\logs\service.err.log" | Out-Null
Invoke-Nssm set $ServiceName AppRotateFiles 1 | Out-Null
Invoke-Nssm set $ServiceName AppRotateOnline 1 | Out-Null
Invoke-Nssm set $ServiceName AppRotateBytes 2000000 | Out-Null
Invoke-Nssm set $ServiceName AppExit Default Restart | Out-Null
Invoke-Nssm set $ServiceName AppRestartDelay 20000 | Out-Null                         # a crash loop must not hammer the box
Invoke-Nssm set $ServiceName AppThrottle 30000 | Out-Null
Invoke-Nssm set $ServiceName AppStopMethodConsole 25000 | Out-Null                    # Ctrl+C first: lets the bot close its browser
Write-Host ("service configured: priority=" + (Invoke-Nssm get $ServiceName AppPriority) + " account=" + (Invoke-Nssm get $ServiceName ObjectName))

if ($NoStart) {
    Write-Host "== 5/5 NOT started (-NoStart). Test with: deploy\as_system.ps1 -Command `"venv\Scripts\python.exe run.py doctor`"; start with: nssm start $ServiceName =="
    exit 0
}
Write-Host "== 5/5 start =="
Invoke-Nssm start $ServiceName | Out-Null
Start-Sleep -Seconds 5
Write-Host (Invoke-Nssm status $ServiceName)
Write-Host "logs: $App\data\logs\bot.log (+ service.out.log / service.err.log)   stop: nssm stop $ServiceName"

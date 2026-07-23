# run_dashboard_task.ps1 - Task Scheduler runner for the read-only dashboard.
#
# Registered as the PmbotDashboard task (at logon, 1-min delay).
#
# Serves on 127.0.0.1 only. `python -m pmbot.dashboard.app` would bind 0.0.0.0
# (app.py's __main__ block), which as an always-on service would expose the
# portfolio, P&L and leader wallets to every network this machine joins - so we
# invoke uvicorn directly instead of editing app.py. Change --host to 0.0.0.0
# here if you deliberately want LAN access.
$ErrorActionPreference = 'Continue'

# Repo root is the parent of scripts/ - keeps this working on any clone.
$repo   = Split-Path -Parent $PSScriptRoot
$py     = Join-Path $repo '.venv\Scripts\python.exe'
$logDir = Join-Path $repo 'logs'

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ('dashboard-{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))

Set-Location $repo
$env:PYTHONUNBUFFERED = '1'

# Reclaim stale instances FIRST, before any logging. Stopping the task kills
# this wrapper but can orphan the cmd.exe/python child it spawned, which keeps
# port 8090 bound. That orphan's cmd.exe also holds an append handle on $log,
# and Add-Content cannot share it - the write fails, ErrorAction=Continue eats
# the error, and the banner silently vanishes. Killing first releases both.
# We only reach here on a fresh start, so any surviving match is stale.
$stale = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*uvicorn*pmbot.dashboard.app*' })
foreach ($p in $stale) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
if ($stale.Count) { Start-Sleep -Milliseconds 500 }

Get-ChildItem $logDir -Filter 'dashboard-*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

Add-Content -Path $log -Encoding utf8 `
    -Value "=== pmbot dashboard START $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
if ($stale.Count) {
    Add-Content -Path $log -Encoding utf8 `
        -Value ("  reclaimed stale PID(s): {0}" -f ($stale.ProcessId -join ', '))
}

# cmd.exe owns the redirection - uvicorn logs to stderr, and PS 5.1 would wrap
# every line in a NativeCommandError record.
& cmd.exe /c "`"$py`" -m uvicorn pmbot.dashboard.app:app --host 127.0.0.1 --port 8090 >> `"$log`" 2>&1"
$code = $LASTEXITCODE

Add-Content -Path $log -Encoding utf8 `
    -Value "=== pmbot dashboard EXIT code=$code $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
exit $code

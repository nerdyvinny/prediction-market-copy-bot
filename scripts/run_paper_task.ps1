# run_paper_task.ps1 - Task Scheduler runner for the paper copy loop.
#
# Registered as the PmbotPaperLoop task (at logon, 1-min delay). Unlike a
# scanner this is a DAEMON: the task sets ExecutionTimeLimit to unlimited and
# MultipleInstances=IgnoreNew, because two instances would race each other on
# the same pmbot.db ledger. Stop the task before running `pmbot paper` by hand.
$ErrorActionPreference = 'Continue'

# Repo root is the parent of scripts/ - keeps this working on any clone.
$repo   = Split-Path -Parent $PSScriptRoot
$exe    = Join-Path $repo '.venv\Scripts\pmbot.exe'
$logDir = Join-Path $repo 'logs'

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ('paper-{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))

Set-Location $repo
$env:PYTHONUNBUFFERED = '1'

# Reclaim stale instances FIRST, before any logging. Stopping the task kills
# this wrapper but can orphan the cmd.exe/pmbot.exe child it spawned. The
# scheduler then reports no instance while the orphan still holds pmbot.db, so
# the next start would race a live loop - exactly what IgnoreNew is meant to
# prevent. The orphan's cmd.exe also holds an append handle on $log, which
# makes Add-Content fail silently (ErrorAction=Continue eats it), so the kill
# has to come first. We only reach here on a fresh start: any match is stale.
$stale = @(Get-CimInstance Win32_Process -Filter "Name='pmbot.exe' OR Name='cmd.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*pmbot.exe*paper*' })
foreach ($p in $stale) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
if ($stale.Count) { Start-Sleep -Milliseconds 500 }

# Keep a month of history; the loop prints a line every 10s.
Get-ChildItem $logDir -Filter 'paper-*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

Add-Content -Path $log -Encoding utf8 `
    -Value "=== pmbot paper START $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
if ($stale.Count) {
    Add-Content -Path $log -Encoding utf8 `
        -Value ("  reclaimed stale PID(s): {0}" -f ($stale.ProcessId -join ', '))
}

# cmd.exe does the redirection, not PowerShell: pmbot logs via
# logging.basicConfig (stderr), and PS 5.1 would wrap every one of those lines
# in a NativeCommandError ErrorRecord.
& cmd.exe /c "`"$exe`" paper >> `"$log`" 2>&1"
$code = $LASTEXITCODE

Add-Content -Path $log -Encoding utf8 `
    -Value "=== pmbot paper EXIT code=$code $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
exit $code

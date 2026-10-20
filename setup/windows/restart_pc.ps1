# Restarts the web app and Caddy that the "AI Director" startup task runs (e.g. after a git pull).
# Run in PowerShell **as Administrator** (the task's processes run under their own logon):
#   powershell -ExecutionPolicy Bypass -File setup\windows\restart_pc.ps1
# A render that is running is cancelled: check the app first.
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$task = "AI Director"

function Listeners { Get-NetTCPConnection -LocalPort 8700, 443 -State Listen -ErrorAction SilentlyContinue }

Write-Host "Stopping the app..."
Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
# Stopping the task ends its PowerShell, not always the app and Caddy it started: stop those too.
Listeners | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
$deadline = (Get-Date).AddSeconds(30)
while (((Listeners) -or (Get-ScheduledTask -TaskName $task).State -eq "Running") -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
}
if ((Listeners)) { throw "Ports 8700/443 are still in use; check Task Manager for python.exe / caddy.exe." }

Write-Host "Starting the app..."
Start-ScheduledTask -TaskName $task
$config = Get-Content (Join-Path $Root "remote\config.json") -Raw | ConvertFrom-Json
$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 2
    try {
        Invoke-RestMethod "http://127.0.0.1:8700/api/info" -Headers @{ "X-Token" = $config.token } -TimeoutSec 5 | Out-Null
        Write-Host "The app is running: $($config.public_url)" -ForegroundColor Green
        exit 0
    } catch { }
} while ((Get-Date) -lt $deadline)
throw "The app didn't answer within a minute. Check Task Scheduler > AI Director, and output\caddy.err.log."

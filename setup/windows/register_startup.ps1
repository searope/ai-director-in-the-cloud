# Runs start_pc.ps1 at every Windows startup (no login needed) and restarts it if it stops.
# Run in PowerShell **as Administrator**, from the repo folder:
#   powershell -ExecutionPolicy Bypass -File setup\windows\register_startup.ps1
# Remove it with:  Unregister-ScheduledTask -TaskName "AI Director" -Confirm:$false
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Root `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Root\setup\windows\start_pc.ps1`""
$trigger = New-ScheduledTaskTrigger -AtStartup
# S4U: runs under your account whether or not you're logged in, without storing your password.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "AI Director" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "AI Director"
Write-Host "Registered and started. It now runs at every startup. Logs: output\caddy.log; app status: Task Scheduler > AI Director."

# Starts Caddy (HTTPS on ports 80/443, log: output\caddy.log) and the web app on 127.0.0.1:8700.
# The app creates a Runpod GPU pod when a job needs one and removes it when idle (remote/gpu.py).
#   powershell -ExecutionPolicy Bypass -File setup\windows\start_pc.ps1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $Root
New-Item -ItemType Directory -Force output | Out-Null
# A log of each start and stop, since the startup task runs hidden (newest run at the end).
function Log($text) { Add-Content -Path "output\start_pc.log" -Value ("{0:yyyy-MM-dd HH:mm:ss} [pid {1}] {2}" -f (Get-Date), $PID, $text) }
trap { Log "error: $_"; exit 1 }
Log "starting (user $env:USERNAME)"

$caddy = Start-Process -FilePath "tools\caddy.exe" -ArgumentList "run", "--config", "tools\Caddyfile", "--adapter", "caddyfile" `
    -WorkingDirectory $Root -RedirectStandardOutput "output\caddy.log" -RedirectStandardError "output\caddy.err.log" `
    -WindowStyle Hidden -PassThru
Log "caddy pid $($caddy.Id); starting the app"
# In Windows PowerShell 5.1, "Stop" would turn the app's first stderr line into a script-ending error.
$ErrorActionPreference = "Continue"
try {
    & .venv\Scripts\python.exe remote\server.py --host 127.0.0.1 --port 8700 2>> output\server.err.log
    Log "the app stopped (exit code $LASTEXITCODE)"
} finally {
    Stop-Process -Id $caddy.Id -ErrorAction SilentlyContinue
}
# The app only stops if it crashed or was killed. Report a failure, so the startup task
# (register_startup.ps1: restart every minute on failure) starts it again.
exit 1

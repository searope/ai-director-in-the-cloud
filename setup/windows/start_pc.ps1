# Starts Caddy (HTTPS on ports 80/443, log: output\caddy.log) and the web app on 127.0.0.1:8700.
# The app creates a Runpod GPU pod when a job needs one and removes it when idle (remote/gpu.py).
#   powershell -ExecutionPolicy Bypass -File setup\windows\start_pc.ps1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $Root
New-Item -ItemType Directory -Force output | Out-Null

$caddy = Start-Process -FilePath "tools\caddy.exe" -ArgumentList "run", "--config", "tools\Caddyfile", "--adapter", "caddyfile" `
    -WorkingDirectory $Root -RedirectStandardOutput "output\caddy.log" -RedirectStandardError "output\caddy.err.log" `
    -WindowStyle Hidden -PassThru
try {
    & .venv\Scripts\python.exe remote\server.py --host 127.0.0.1 --port 8700
} finally {
    Stop-Process -Id $caddy.Id -ErrorAction SilentlyContinue
}

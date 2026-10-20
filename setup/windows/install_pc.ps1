# One-time setup of the web app on an always-on Windows PC (the GPU runs on Runpod, created on demand).
# Installs a Python virtual environment with the app's packages, downloads Caddy (HTTPS with automatic
# Let's Encrypt certificates for your domain) and allows it through Windows Firewall.
#
# Run in PowerShell **as Administrator**, from the repo folder:
#   powershell -ExecutionPolicy Bypass -File setup\windows\install_pc.ps1 -Domain searope.chickenkiller.com
# Your router must forward ports 80 and 443 to this PC. Details: docs/home-server.md
param([Parameter(Mandatory = $true)][string]$Domain)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $Root

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

Step "Python"
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
if (-not (Test-Path ".venv\Scripts\python.exe")) { py -3 -m venv .venv }
& .venv\Scripts\python.exe -m pip install --upgrade pip
# aiohttp: web server; pillow/numpy/av: video assembly; imageio-ffmpeg: sound mixing; qrcode: link QR code
& .venv\Scripts\python.exe -m pip install aiohttp pillow numpy av qrcode imageio-ffmpeg

Step "Caddy (HTTPS)"
New-Item -ItemType Directory -Force tools | Out-Null
if (-not (Test-Path "tools\caddy.exe")) {
    Invoke-WebRequest "https://caddyserver.com/api/download?os=windows&arch=amd64" -OutFile "tools\caddy.exe"
}
@"
$Domain {
	reverse_proxy 127.0.0.1:8700
}
"@ | Set-Content -Encoding ascii "tools\Caddyfile"

Step "Windows Firewall: allow Caddy on ports 80 and 443"
$rule = "AI Director (Caddy HTTPS)"
if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP -LocalPort 80, 443 `
        -Program "$Root\tools\caddy.exe" -Profile Any | Out-Null
}

Step "Done"
if (-not (Test-Path "remote\config.json")) {
    Write-Host "Next: create remote\config.json (see docs/home-server.md), then run setup\windows\start_pc.ps1"
} else {
    Write-Host "Start it with: powershell -ExecutionPolicy Bypass -File setup\windows\start_pc.ps1"
}

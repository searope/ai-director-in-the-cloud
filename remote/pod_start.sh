#!/usr/bin/env bash
# Start command of a GPU pod created by the web app on another machine (remote/gpu.py):
# ComfyUI on 127.0.0.1:8188 (private) and the token-protected gateway on port 8700.
cd "$(dirname "$0")/.."
mkdir -p output
./run_comfyui.sh > output/comfyui.log 2>&1 &
exec .venv/bin/python remote/gpu_gateway.py

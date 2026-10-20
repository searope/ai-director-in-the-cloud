#!/usr/bin/env bash
# Start command of a GPU pod created by the web app on another machine (remote/gpu.py):
# ComfyUI on 127.0.0.1:8188 (private) and the token-protected gateway on port 8700.
cd "$(dirname "$0")/.."
mkdir -p output
./run_comfyui.sh > output/comfyui.log 2>&1 &
# Fetch any default model that isn't on the volume yet (e.g. after a new default was added); files that are
# already there are skipped in a second. ComfyUI sees new files without a restart.
.venv/bin/python setup/download_models.py > output/download.log 2>&1 &
exec .venv/bin/python remote/gpu_gateway.py

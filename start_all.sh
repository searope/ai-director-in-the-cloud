#!/usr/bin/env bash
# Starts ComfyUI in the background (log: output/comfyui.log) and the phone remote in the foreground.
cd "$(dirname "$0")"
mkdir -p output
./run_comfyui.sh > output/comfyui.log 2>&1 &
COMFY_PID=$!
trap 'kill $COMFY_PID 2>/dev/null' EXIT
echo "Waiting for ComfyUI to start..."
until curl -s http://127.0.0.1:8188/system_stats > /dev/null; do sleep 3; done
./run_remote.sh "$@"

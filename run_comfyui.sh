#!/usr/bin/env bash
# Starts ComfyUI on 127.0.0.1:8188 (private; reach it through the web app or an SSH tunnel).
cd "$(dirname "$0")/ComfyUI"
exec ../.venv/bin/python main.py --enable-manager "$@"

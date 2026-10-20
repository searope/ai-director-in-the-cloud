#!/usr/bin/env bash
# Usage: ./animate.sh examples/storyboard.json   (ComfyUI must already be running)
exec "$(dirname "$0")/.venv/bin/python" "$(dirname "$0")/animate.py" "$@"

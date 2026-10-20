#!/usr/bin/env bash
# Phone remote: serves the mobile app on port 8700 and prints the link + QR code to open on the phone.
exec "$(dirname "$0")/.venv/bin/python" "$(dirname "$0")/remote/server.py" "$@"

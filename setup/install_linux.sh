#!/usr/bin/env bash
# Linux equivalent of install_windows.ps1 (e.g. if the laptop dual-boots).
# Usage: bash setup/install_linux.sh [--skip-models] [--with-gguf]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMFY="$ROOT/ComfyUI"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
SKIP_MODELS=0
WITH_GGUF=0
for arg in "$@"; do
  case "$arg" in
    --skip-models) SKIP_MODELS=1 ;;
    --with-gguf) WITH_GGUF=1 ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

step() { printf '\n==> %s\n' "$1"; }

step "Checking NVIDIA driver"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

step "Getting ComfyUI"
if [ -d "$COMFY/.git" ]; then git -C "$COMFY" pull --ff-only
else git clone https://github.com/comfyanonymous/ComfyUI.git "$COMFY"; fi

step "Creating virtual environment"
[ -x "$PY" ] || python3 -m venv "$VENV"
"$PY" -m pip install --upgrade pip wheel

step "Installing PyTorch (CUDA 13.0 build)"
"$PY" -m pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu130

step "Installing ComfyUI requirements"
"$PY" -m pip install -r "$COMFY/requirements.txt"
"$PY" -m pip install -r "$COMFY/manager_requirements.txt"
"$PY" -m pip install qrcode  # QR code for the phone remote

if [ "$WITH_GGUF" = 1 ]; then
  step "Installing ComfyUI-GGUF"
  [ -d "$COMFY/custom_nodes/ComfyUI-GGUF" ] || git clone https://github.com/city96/ComfyUI-GGUF.git "$COMFY/custom_nodes/ComfyUI-GGUF"
  "$PY" -m pip install -r "$COMFY/custom_nodes/ComfyUI-GGUF/requirements.txt"
fi

step "Verifying the GPU is visible to PyTorch"
"$PY" -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print(torch.__version__, torch.cuda.get_device_name(0), 'sm_%d%d' % torch.cuda.get_device_capability(0))"

if [ "$SKIP_MODELS" = 0 ]; then
  step "Downloading models (~50 GB, resumable)"
  "$PY" "$ROOT/setup/download_models.py"
fi

step "Done"
echo "Start everything:     ./start_all.sh   (then scan the QR code with your phone)"
echo "Command line:         ./animate.sh examples/storyboard.json"

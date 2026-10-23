#!/usr/bin/env bash
# Start command of a Community Cloud pod (remote/gpu.py). Those pods can't mount the network volume, so this sets
# ComfyUI up on the pod's own disk in a few minutes instead of setup/install_linux.sh's hour:
#  - it reuses the image's PyTorch (no 3 GB download) and installs ComfyUI's other packages with uv;
#  - the gateway starts first and downloads the models the job needs (MODEL_PROFILES), in parallel, while
#    ComfyUI installs; the app shows the step and the download progress from /gateway/health.
# The app's start command unpacks the pod-side code here first (from AI_DIRECTOR_CODE), so it needs no git access.
set -u
cd "$(dirname "$0")/.."
mkdir -p output
exec >> output/setup.log 2>&1
T0=$(date +%s)
stage() { echo "$1" > output/setup_stage.txt; echo "$(date +%T) +$(( $(date +%s) - T0 ))s $1"; }

PY=""
for p in python3 python; do
  if command -v "$p" >/dev/null && "$p" -c "import torch" 2>/dev/null; then PY="$(command -v "$p")"; break; fi
done
[ -n "$PY" ] || PY="$(command -v python3)"
stage "installing Python packages"
"$PY" -m pip install -q --break-system-packages uv 2>/dev/null || "$PY" -m pip install -q uv
UV=("$PY" -m uv pip install -q --system --break-system-packages --python "$PY")
mkdir -p .venv/bin && ln -sf "$PY" .venv/bin/python  # run_comfyui.sh and the scripts use .venv/bin/python
"${UV[@]}" aiohttp &
[ -d ComfyUI/.git ] || git clone -q --depth 1 https://github.com/comfyanonymous/ComfyUI.git ComfyUI &
wait

stage "installing ComfyUI"
.venv/bin/python remote/gpu_gateway.py > output/gateway.log 2>&1 &
GATEWAY=$!
TORCH="$("$PY" -c 'import torch; print(torch.__version__)' 2>/dev/null)"
grep -viE '^\s*(torch|torchvision|torchaudio)\s*([<>=!~;].*)?$' ComfyUI/requirements.txt > output/comfy_requirements.txt
"${UV[@]}" -r output/comfy_requirements.txt -r ComfyUI/manager_requirements.txt
if ! "$PY" -c "import torchaudio" 2>/dev/null; then  # H3's and LTX's sound need it; keep the image's torch
  "${UV[@]}" torchaudio "torch==$TORCH" --index-url https://download.pytorch.org/whl/cu130
fi
"$PY" -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print(torch.__version__, torch.cuda.get_device_name(0))"

stage "starting ComfyUI"
./run_comfyui.sh > output/comfyui.log 2>&1 &
until "$PY" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8188/system_stats', timeout=5)" 2>/dev/null; do
  sleep 2
done
echo "$(date +%T) +$(( $(date +%s) - T0 ))s ComfyUI is up"
rm -f output/setup_stage.txt
wait "$GATEWAY"

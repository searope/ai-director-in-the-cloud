# Home server: the app on your PC, the GPU on Runpod only when there's work

```
phone / any browser ──HTTPS──▶ Windows PC (always on): Caddy :443 ──▶ web app 127.0.0.1:8700
                                   │  queue, library, sound mixing, ElevenLabs
                                   │  creates a pod when a job needs the GPU, removes it after 10 idle min
                                   ▼
             Runpod RTX 5090 pod: gateway :8700 (token, Runpod HTTPS proxy) ──▶ ComfyUI 127.0.0.1:8188
                                   models + ComfyUI on the network volume (/workspace)
```

- **You pay for the GPU only around jobs**, at the normal pod rate. The first job of a session waits
  2-5 minutes while the pod starts. Later jobs start at once, until the GPU has been idle for 10
  minutes. You can also start or stop it by hand in the app; it shows this month's GPU spend.
- **Fixed costs:** the network volume, about $0.07/GB a month ($10-14 for 150-200 GB). The PC
  costs nothing extra.
- **Safety nets:**
  - The app removes a pod after 4 hours if nothing is running.
  - If the PC goes offline, the pod stops itself after 30 minutes without requests. The app
    deletes it when it's back.
  - Set a spending limit or low-balance alert in the Runpod console as well.
- **Security:**
  - The app is protected by its access token, and Caddy serves it over HTTPS with a free Let's
    Encrypt certificate.
  - ComfyUI is never exposed. The pod's gateway accepts only requests that carry a secret token,
    which the app creates for each pod.

## 1. One-time: the network volume (ComfyUI + models)

The app creates GPU pods by itself, but the volume they mount has to be prepared once.

1. Runpod console → **Storage** → create a **network volume** of about 200 GB in a data center that
   has RTX 5090s on Secure Cloud. Note its **ID** and **data center** (e.g. `US-KS-2`).
2. Deploy a temporary RTX 5090 pod on that volume (mounted at `/workspace`) with a PyTorch Linux
   image. Note the **image name** (e.g. `runpod/pytorch:…`); the app must use the same image later,
   because the Python environment on the volume is built for it.
3. In its web terminal:
   ```bash
   cd /workspace && git clone https://github.com/searope/ai-director-in-the-cloud.git && cd ai-director-in-the-cloud
   export HF_TOKEN=hf_...          # Hugging Face read token
   bash setup/install_linux.sh     # ComfyUI + PyTorch + default models (~1 h)
   ```
4. Terminate the temporary pod. The volume keeps everything.

To update the pod-side code later (e.g. the gateway), run `git pull` in that folder from any pod on the
volume. Claude can also do steps 1-4 through the Runpod plugin.

## 2. Runpod API key for the app

Runpod console → **Settings → API Keys** → create a key the app can use to create and delete pods
(Read/Write). It's stored only on your PC, in `remote/config.json`.

## 3. Install on the PC

Ports 80 and 443 must be forwarded to the PC (already done), and `searope.chickenkiller.com` must
point at your home IP (FreeDNS; keep its updater running if your IP changes).

In PowerShell **as Administrator**:
```powershell
cd $HOME
git clone https://github.com/searope/ai-director-in-the-cloud.git
cd ai-director-in-the-cloud
powershell -ExecutionPolicy Bypass -File setup\windows\install_pc.ps1 -Domain searope.chickenkiller.com
```

Then create `remote\config.json`. This file is never committed, so keys are safe in it:
```json
{
  "public_url": "https://searope.chickenkiller.com",
  "elevenlabs_api_key": "sk_...",
  "gpu": {
    "api_key": "rpa_...",
    "volume_id": "your-volume-id",
    "datacenter": "US-KS-2",
    "image": "runpod/pytorch:<the tag from step 1.2>"
  }
}
```
Optional `gpu` settings, shown with their defaults:

| Setting | Default | Meaning |
|---|---|---|
| `gpu_types` | `["NVIDIA GeForce RTX 5090"]` | GPUs to try, in order |
| `idle_minutes` | `10` | Idle minutes before the pod is removed |
| `max_hours` | `4` | Safety limit on a pod's lifetime |
| `cloud` | `"SECURE"` | Cloud tier |
| `disk_gb` | `30` | Container disk |
| `min_cuda` | `"13.0"` | Minimum host CUDA version |
| `pod_safety_minutes` | `30` | The pod stops itself after this long without requests |

`elevenlabs_api_key` is optional (the paid Sound feature). It now lives on the PC because sound is
mixed there. The Runpod secret isn't needed for this setup.

## 4. Start it

Test it in the foreground first; the window shows the link with your access token:
```powershell
powershell -ExecutionPolicy Bypass -File setup\windows\start_pc.ps1
```
Open `https://searope.chickenkiller.com/?t=<token>` on your phone. The token is also in
`remote\config.json`. The first visit can take a few seconds while Caddy gets the certificate. The
GPU card shows "GPU off: starts by itself when you render".

Then make it start with Windows, without needing a login (as Administrator):
```powershell
powershell -ExecutionPolicy Bypass -File setup\windows\register_startup.ps1
```

## Troubleshooting

- **No HTTPS / certificate errors:** check `output\caddy.err.log`. Usually ports 80/443 aren't
  forwarded to this PC, or the domain doesn't point at your current IP.
- **"GPU didn't start":**
  - The error text says why; often no RTX 5090 is free in that data center right now. Add
    fallbacks to `gpu_types`, using the exact names from Runpod's GPU types list (e.g.
    `"NVIDIA GeForce RTX 4090"`, slower), or try again later.
  - The app always removes a pod that failed to start.
- **Check for leftover pods** now and then in the Runpod console. The app removes its own pods, and
  `output\gpu_sessions.jsonl` logs every pod with its time and hourly rate.

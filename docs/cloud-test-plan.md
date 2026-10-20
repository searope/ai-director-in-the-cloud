# Test plan: rented cloud GPU

**Goal:** find out, for a small budget (~$20–40), whether rendering on a rented GPU is worth it:
speed, quality, cost per finished clip, and how much hassle each session is.

**Device-independent:** the rented machine runs **everything**: ComfyUI, the models, the
keyframe maker and the web app. You open the web app from **any device with a browser** (phone,
tablet, any computer, at home or away). Nothing has to be installed or kept running on your own
devices.

```
any device (browser) ──HTTPS──▶ rented GPU: web app (port 8700, token-protected)
                                              └──▶ ComfyUI (127.0.0.1:8188, never exposed)
```

- The web app is reachable through the provider's **HTTPS proxy** (RunPod:
  `https://<pod-id>-8700.proxy.runpod.net`) and protected by the app's access token.
- **ComfyUI stays private** (it listens on 127.0.0.1 only; it has no password).
- Jobs, results and keyframes are saved on the persistent volume, so they survive between sessions.

What's compared: a rented **RTX 5090 (32 GB)** as the main candidate, optionally a short **H100
(80 GB)** run, and any **local machine you already have** (e.g. the RTX 5050 laptop) as a free
baseline if you want one.

---

## Phase 0: preparation (any device, free, ~30 min)

| # | Step | Done when |
|---|---|---|
| 0.1 | **Accounts**: RunPod (primary, because its persistent network volumes keep the ~100 GB of models between sessions) and Hugging Face (create a Read token; accept the licenses of FLUX.2 Klein 9B / FLUX 3 Klein if you want them for comparison: 9B is non-commercial, FLUX 3 Klein only under $5M revenue). | Logged in, token saved in a password manager |
| 0.2 | **Spending guard**: prepay only **$25** of RunPod credit for now. | Credit = $25 |
| 0.3 | **Benchmark set**: 3 keyframe pairs, same aspect ratio: **A** photos (person, small pose change), **B** a drawn/anime pair, **C** two characters. One motion prompt per pair, written down. Keep them in cloud storage or on your phone, so you can upload them from any device. | 6 images + 3 prompts |
| 0.4 | *(Optional baseline)* If you have a local GPU machine, run tests T3 and T4 below on it and note the times. | Baseline numbers, or skipped |

No SSH keys or terminal on your own device are required: the setup runs in RunPod's **web
terminal**.

---

## Phase 1: first cloud session (~1–1.5 h, ≈ $3–5)

| # | Step | Notes |
|---|---|---|
| 1.1 | Create a **network volume**, ~200 GB (~150 GB is enough without the optional models), in a region that offers RTX 5090s on **Secure Cloud** (network volumes don't work with Community Cloud). | Billed monthly even when no GPU runs. Delete it after the test if you don't continue |
| 1.2 | Deploy an **RTX 5090 pod** with a PyTorch / CUDA 13 Linux template, the volume mounted at `/workspace`, and **HTTP port 8700 exposed**. Do **not** expose 8188. Optional, for the paid ElevenLabs sound: add the environment variable `ELEVENLABS_API_KEY` = `{{ RUNPOD_SECRET_elevenlabs_api_key }}` (after saving the key as the Runpod secret `elevenlabs_api_key`). | 8700 is the web app (token-protected); 8188 is ComfyUI (must stay private) |
| 1.3 | Open the pod's **web terminal** and run: `cd /workspace && git clone https://github.com/searope/ai-director-in-the-cloud.git && cd ai-director-in-the-cloud` | The repo lives on the persistent volume. The repo is private, so `git clone` asks for credentials: use your GitHub username and a [fine-grained token](https://github.com/settings/personal-access-tokens) with read access to this repo only |
| 1.4 | `export HF_TOKEN=hf_...` then `bash setup/install_linux.sh` | Installs ComfyUI + PyTorch and downloads the default models. Note how long it takes |
| 1.5 | Optional comparison models: `.venv/bin/python setup/download_models.py --profile klein9b --profile flux3-klein` | Not for commercial work (see the README's license table). Skip `minimax-h3`: its license doesn't cover the US |
| 1.6 | Start everything inside `tmux` (so it keeps running if the web terminal closes): `tmux new -s anim` then `./start_all.sh` | It prints an **`https://…proxy.runpod.net/?t=…`** link and a QR code |
| 1.7 | Open that link on your **phone**, then on **another device**. The status pill should show the 5090 and ~32 GB. | Same link works everywhere; bookmark it / "Add to Home Screen" |
| 1.8 | Render one short test (Preview, 2 s) from the phone. Close the browser, reopen the link from another device, and check the job is there. | Confirms jobs live on the server, not the device |
| 1.9 | **Stop the pod** (not just close the tab). | GPU billing stops; the volume keeps models, jobs and the token. If you forget, the app stops it after 30 idle minutes (see the safety checklist) |

**Next sessions** take ~2 minutes: start the pod with the same volume → web terminal →
`cd /workspace/ai-director-in-the-cloud && tmux new -s anim ./start_all.sh` → open your bookmarked link. The
pod ID, and so the link, can change when you redeploy a pod; the access token stays the same.

**Exit check:** a render started from the phone finished on the 5090, the result opened on a second
device, and you know the setup time and cost.

---

## Phase 2: benchmark (~2–3 h on a 5090, ≈ $3–8)

Use the **same keyframes, prompts and seed** (`seed: 42`) everywhere, so only the hardware differs.
Run each test twice: the first run is "cold" (models loading), the second "warm" (normal speed).

| ID | Test | Settings |
|---|---|---|
| T1 | Keyframe image, Klein 4B | Keyframes tab, 16:9, same prompt ×3 |
| T2 | Keyframe image, Klein 9B and FLUX 3 Klein | same prompt |
| T3 | Wan 2.2 fast, Preview, 2 s | pair A |
| T4 | Wan 2.2 fast, Standard (~480p), 5 s | pairs A, B, C |
| T5 | Wan 2.2 **quality**, ~720p, 5 s | pair A (web terminal: `./animate.sh --keyframes … --quality --width 1280 --height 720`) |
| T6 | ~~MiniMax H3 fast, 5 s, with sound~~ | skipped: H3's license excludes the US |
| T7 | ~~MiniMax H3 quality (20 steps), 5 s~~ | skipped, same reason |
| T8 | Full storyboard, 3 keyframes, anime style with restyle | `examples/storyboard.json` |

**Record for each run** (in a shared spreadsheet, so you can fill it in from any device):

| ID | Machine | Cold time | Warm time | Peak VRAM | Failed? | Quality 1–5 | Notes |
|---|---|---|---|---|---|---|---|
| T4-A | 5090 | | | | | | |
| T4-A | local (optional) | | | | | | |

The job card shows render time. For peak VRAM, run `nvidia-smi` in the web terminal while a job
renders. Quality, judged by eye: **5** = usable as is, **3** = needs a retake, **1** = broken
(morphing, melted faces, flicker). Check on a phone *and* a large screen.

**Optional (≈ $3):** repeat T4 and T5 on an **H100** for one hour, using the same volume if it's
in the same region, to see whether the more expensive GPU is worth it.

---

## Phase 3: what renting unlocks (~2–4 h, ≈ $5–10)

After Phase 2. Each item needs a small code addition first (Claude):

| # | Test | Question it answers |
|---|---|---|
| 3.1 | **LTX-2.5 first/last-frame** engine on pairs A–C | Is the newest open keyframe model better than Wan 2.2? (Free commercial use under $10M revenue) |
| 3.2 | **Multi-keyframe clips** (3–4 keyframes in one clip, with an engine whose license allows it, e.g. LTX-2.5) | Smoother than joining 2-keyframe clips? |
| 3.3 | **480p/720p → 1080p upscale** (SeedVR2) of the best clips | Is "render low, upscale keepers" good enough? |
| 3.4 | **Cast / multi-reference keyframes** for pair C | Do both characters stay recognizable without training? |

---

## Phase 4: LoRA training trial (≈ 3–5 h, ≈ $5–10), optional

Do this only if 3.4 shows characters drifting. Prepare 20–30 varied images of the main character,
train **one** character LoRA on the 5090 (Claude adds the training workflow), then re-run the keyframe
and video tests with and without it.

---

## Phase 5: decision

**Cost per finished clip** = warm minutes per clip × takes per keeper ÷ 60 × $ per hour.

| If… | Then |
|---|---|
| Cloud quality and speed are good and you'd render a few hours a month | **Cloud on demand**: start a pod per session, open the bookmarked link from any device |
| You also have a capable local machine and its results are acceptable | Use it for quick work, and the cloud for heavy renders (both use the same web app) |
| You'd rent > ~100 h a month for many months | Re-run the buy-vs-rent maths with current prices |
| Starting sessions felt like too much work | Claude adds a one-command start and auto-stop-when-idle before deciding |

**Budget recap:** Phase 1 ≈ $3–5 · Phase 2 ≈ $3–8 (+$3 H100) · Phase 3 ≈ $5–10 · Phase 4 ≈ $5–10,
plus the network volume for the month. **Total ≈ $20–40.**

---

## Safety checklist (every session)

- Expose **only port 8700** (the web app, token-protected). Never expose ComfyUI's 8188.
- Treat the link with `?t=…` like a password. If it leaks, run `./run_remote.sh --new-token` once
  (old links stop working) and restart.
- **Stop the pod** when done, and check the RunPod dashboard shows nothing running. As a safety
  net, the web app stops the pod by itself after **30 minutes with nothing rendering and no
  action in the app** (open tabs that only watch don't count). In the last 10 minutes the app shows
  "Stops in N min"; tap it to keep the pod running. Change the time with
  `./start_all.sh --idle-stop 60`, or turn it off with `--idle-stop 0`. The startup message says
  whether auto-stop is on.
- Keep the HF token as an environment variable in the session; never commit it.
- Keyframes and prompts are processed on the provider's machine, so don't upload anything you
  wouldn't put on a third-party server.
- **After the test:** delete the network volume if you're not continuing (it bills monthly).

## Known gaps to close before or during the test (Claude)

- **One-command start:** a RunPod start script (or template) so a new session doesn't need any
  typing in the terminal.
- **Proxy limits:** RunPod's HTTPS proxy has request time limits. Uploads and polling are short
  requests, so this should be fine, but very large uploads (many full-resolution photos) should be
  checked in Phase 1.

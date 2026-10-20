# AI Director (cloud)

An AI video **director** that runs on a **rented GPU** and is used from **any browser** (phone,
tablet, computer):

```
brief → script & storyboard → consistent keyframes (cast) → shots (open-weight models on the GPU) → assembled film
```

Inspired by the "director" workflow of hosted studios such as OpenArt, but self-hosted: open models
on a GPU you rent by the hour (roughly 10–50× cheaper per take than closed-model APIs, with full
control over styles, LoRAs and seeds).

**Policy:** spending is limited to a **rented cloud GPU** (open-weight models only) and an **AI
subscription** (Claude or similar) for the story and scripting work. No pay-per-clip generation
APIs or credits. **Commercial use must be allowed**, so the defaults use only Apache 2.0 models
(see [Model licenses](#model-licenses) and [CLAUDE.md](CLAUDE.md)).

> **Status: early.** What works today is the engine inherited from
> [searope/32gb-rtx-5050-8gb](https://github.com/searope/32gb-rtx-5050-8gb): keyframe maker,
> keyframe → video with Wan 2.2 (MiniMax H3 optional), styles, and the browser app. The director features
> below are the roadmap.

## Architecture

```
any device (browser) ──HTTPS──▶ rented GPU: web app (port 8700, token-protected)
                                              └──▶ ComfyUI (127.0.0.1:8188, never exposed)
```

Everything (models, jobs, keyframes, projects) lives on the cloud machine's persistent volume, so
any device sees the same state.

## What works now

| Piece | Where |
|---|---|
| Keyframe maker: text → keyframe 1, edits → next keyframes (FLUX.2 Klein 4B default; Klein 9B, FLUX 3 Klein optional) | web app, Keyframes tab |
| Keyframes → video: Wan 2.2 14B first/last frame (default); MiniMax H3 with sound (optional, see licenses) | web app, Create tab; `animate.py` |
| Styles: presets, custom text styles, keyframe redraw, Wan style LoRAs | `styles.json` |
| Studio web app (dark, OpenArt-style layout): Home, Create image (text → image, edit image), Create video (keyframes from the library, motion per gap), Library of all images and videos; job queue; works on phone and desktop; prints the public link on RunPod. The previous single-page app is at `/classic` | `remote/server.py`, `remote/static/` |
| Model downloader with profiles | `setup/download_models.py`, `setup/models.json` |

## Roadmap (director workflow)

1. **Projects + shot list + cuts:** a storyboard of shots (keyframe, prompt, duration, engine per
   shot), each rendered separately, assembled with hard cuts; regenerate, reorder or retime single
   shots without re-rendering the rest.
2. **Cast:** named characters with reference images, tagged in shots, used as multi-reference input
   when making keyframes. Later: one combined multi-character LoRA.
3. **Story planner:** brief → script → shot list, written by the AI subscription (e.g. Claude Code
   editing the project's storyboard file) or a local model on the GPU; editable before rendering.
4. **More engines (open weights, on the GPU, commercial use allowed):** LTX-2.5 first/last frame,
   a Wan 2.2 "max quality" preset (20 steps, 720p, upscaling).
5. **Finishing:** 1080p upscaling of chosen shots, voice-over and music tracks, continuity check.
6. **Cloud ops:** one-command session start, cost tracking per project. (Idle auto-stop is done:
   the app stops a RunPod pod after 30 minutes with nothing rendering and no action in the app;
   `--idle-stop MIN` changes it, `0` turns it off.)

## Model licenses

Summaries as of Oct 2026, from secondary sources: check the LICENSE on each official model card
before commercial use. Details per download profile are in `setup/models.json` (`license`).

| Model | License | Commercial use |
|---|---|---|
| Wan 2.2 14B (+ umt5, Wan VAE) | Apache 2.0 | ✅ free (default video engine) |
| FLUX.2 Klein 4B (+ Qwen3 4B) | Apache 2.0 | ✅ free (default image model) |
| FILM interpolation | Apache 2.0 | ✅ free |
| Lightx2v Wan 2.2 4-step LoRAs (fast mode) | not yet verified | ❓ quality mode doesn't use them |
| FLUX 3 Klein | FLUX Kommunity License | ⚠️ free only under $5M annual revenue |
| FLUX.2 Klein 9B | FLUX non-commercial | ❌ needs a paid Black Forest Labs license |
| MiniMax H3 (+ turbo LoRA) | MiniMax H3 Community License | ❌ excludes the US, EU, UK and South Korea |

## Getting started

Follow **[docs/cloud-test-plan.md](docs/cloud-test-plan.md)**: a budget-capped (~$20–40) plan that
sets this up on a RunPod RTX 5090 and benchmarks it. Short version, on the pod:

```bash
cd /workspace && git clone https://github.com/searope/ai-director-in-the-cloud.git && cd ai-director-in-the-cloud
export HF_TOKEN=hf_...            # Hugging Face read token
bash setup/install_linux.sh       # ComfyUI + PyTorch + default models
tmux new -s anim ./start_all.sh   # prints https://<pod-id>-8700.proxy.runpod.net/?t=... and a QR code
```

Expose **only port 8700**. Stop the pod when you're done; if you forget, the app stops it after 30
minutes with nothing rendering and no action in the app (`./start_all.sh --idle-stop 60` to change,
`0` to turn off). On other hosts, set `IDLE_STOP_COMMAND` to the command that stops the machine.

## Origin

Forked from [searope/32gb-rtx-5050-8gb](https://github.com/searope/32gb-rtx-5050-8gb) (the
laptop setup for an RTX 5050 8 GB) at commit `3546bfc`. The two projects now evolve separately.

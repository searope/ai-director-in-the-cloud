# AI Director (cloud)

An AI video **director** that runs on a **rented GPU** and is used from **any browser** (phone,
tablet, computer):

```
brief → script & storyboard → consistent keyframes (cast) → shots (open models, optional closed APIs) → assembled film
```

Inspired by the "director" workflow of hosted studios such as OpenArt, but self-hosted: open models
on a GPU you rent by the hour (roughly 10–50× cheaper per take than closed-model APIs, with full
control over styles, LoRAs and seeds), with closed models such as Kling as an optional, paid engine
for hero shots.

> **Status: early.** What works today is the engine inherited from
> [searope/32gb-rtx-5050-8gb](https://github.com/searope/32gb-rtx-5050-8gb): keyframe maker,
> keyframe → video with Wan 2.2 / MiniMax H3, styles, and the browser app. The director features
> below are the roadmap.

## Architecture

```
any device (browser) ──HTTPS──▶ rented GPU: web app (port 8700, token-protected)
                                              └──▶ ComfyUI (127.0.0.1:8188, never exposed)
                                              └──▶ (optional) closed-model APIs via ComfyUI partner nodes
```

Everything (models, jobs, keyframes, projects) lives on the cloud machine's persistent volume, so
any device sees the same state.

## What works now

| Piece | Where |
|---|---|
| Keyframe maker: text → keyframe 1, edits → next keyframes (FLUX.2 Klein 4B/9B, FLUX 3 Klein) | web app, Keyframes tab |
| Keyframes → video: Wan 2.2 14B first/last frame; MiniMax H3 with sound | web app, Create tab; `animate.py` |
| Styles: presets, custom text styles, keyframe redraw, Wan style LoRAs | `styles.json` |
| Browser app with job queue, works from any device; prints the public link on RunPod | `remote/server.py` |
| Model downloader with profiles | `setup/download_models.py`, `setup/models.json` |

## Roadmap (director workflow)

1. **Projects + shot list + cuts:** a storyboard of shots (keyframe, prompt, duration, engine per
   shot), each rendered separately, assembled with hard cuts; regenerate, reorder or retime single
   shots without re-rendering the rest.
2. **Cast:** named characters with reference images, tagged in shots, used as multi-reference input
   when making keyframes. Later: one combined multi-character LoRA.
3. **Story planner:** brief → script → shot list with a language model (local via ComfyUI, or the
   Claude API), editable before rendering.
4. **More engines:** LTX-2.5 first/last frame, H3 multi-keyframe, Kling start/end frame (paid, via
   ComfyUI partner nodes).
5. **Finishing:** 1080p upscaling of chosen shots, voice-over and music tracks, continuity check.
6. **Cloud ops:** one-command session start, idle auto-stop, cost tracking per project.

## Getting started

Follow **[docs/cloud-test-plan.md](docs/cloud-test-plan.md)**: a budget-capped (~$20–40) plan that
sets this up on a RunPod RTX 5090 and benchmarks it. Short version, on the pod:

```bash
cd /workspace && git clone https://github.com/searope/ai-director-in-the-cloud.git && cd ai-director-in-the-cloud
export HF_TOKEN=hf_...            # Hugging Face read token
bash setup/install_linux.sh       # ComfyUI + PyTorch + default models
tmux new -s anim ./start_all.sh   # prints https://<pod-id>-8700.proxy.runpod.net/?t=... and a QR code
```

Expose **only port 8700**. Stop the pod when you're done.

## Origin

Forked from [searope/32gb-rtx-5050-8gb](https://github.com/searope/32gb-rtx-5050-8gb) (the
laptop setup for an RTX 5050 8 GB) at commit `3546bfc`. The two projects now evolve separately.

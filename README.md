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

Recommended: the app on an always-on home PC, the GPU on Runpod only while there's work
([docs/home-server.md](docs/home-server.md)):

```
any device (browser) ──HTTPS──▶ home PC: web app (queue, library, sound), Caddy for HTTPS
                                   └─ creates a GPU pod when a job needs it, removes it after 10 idle min
                                        ▼
                                 Runpod pod: token-protected gateway ──▶ ComfyUI (never exposed)
                                             models on the network volume
```

Alternative (the original setup): everything on the rented GPU pod, with ComfyUI on 127.0.0.1 and the
web app on port 8700. The app is then reachable only while the pod runs; it stops the pod after 30 idle
minutes.

## What works now

| Piece | Where |
|---|---|
| Keyframe maker: text → keyframe 1, edits → next keyframes (FLUX.2 Klein 4B default; Klein 9B, FLUX 3 Klein optional) | web app, Keyframes tab |
| Keyframes → video: Wan 2.2 14B first/last frame (default); MiniMax H3 with sound (optional, see licenses) | web app, Create tab; `animate.py` |
| Styles: presets, custom text styles, keyframe redraw, Wan style LoRAs | `styles.json` |
| Studio web app (dark, OpenArt-style layout): Home, Create image (text → image, edit image), Create video (keyframes from the library, motion per gap), Library of all images and videos; job queue; works on phone and desktop; prints the public link on RunPod. The previous single-page app is at `/classic` | `remote/server.py`, `remote/static/` |
| Sound for finished videos (**opt-in, paid ElevenLabs credits**): voice-over, multi-voice dialogue, sound effects and music as layers with their own start time and volume, mixed into a copy of the video; dubbing of the speech layers into other languages. Off unless `ELEVENLABS_API_KEY` is set | Sound button on a video; `remote/soundtrack.py` |
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
5. **Finishing:** 1080p upscaling of chosen shots, continuity check, an open-weight (free) voice
   and music option on the GPU next to ElevenLabs, character voices tied to the Cast.
6. **Cloud ops:** cost tracking per project. (Done: GPU pods created on demand and removed when idle,
   with the month's GPU spend shown in the app; idle auto-stop when everything runs on a pod.)

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
| ElevenLabs (sound, opt-in API, not a model) | ElevenLabs terms; paid per use | ⚠️ needs a paid ElevenLabs plan (free-plan dubs are watermarked); check the terms for music |

## Getting started

**Home PC + GPU on demand:** follow [docs/home-server.md](docs/home-server.md).

**Everything on a pod:** follow **[docs/cloud-test-plan.md](docs/cloud-test-plan.md)**: a budget-capped (~$20–40) plan that
sets this up on a RunPod RTX 5090 and benchmarks it. Short version, on the pod:

```bash
cd /workspace && git clone https://github.com/searope/ai-director-in-the-cloud.git && cd ai-director-in-the-cloud
export HF_TOKEN=hf_...            # Hugging Face read token
bash setup/install_linux.sh       # ComfyUI + PyTorch + default models
tmux new -s anim ./start_all.sh   # prints https://<pod-id>-8700.proxy.runpod.net/?t=... and a QR code
```

**Optional, paid: ElevenLabs sound.** Set `ELEVENLABS_API_KEY` before `./start_all.sh` (or as a
RunPod secret / pod environment variable, or `"elevenlabs_api_key"` in `remote/config.json`, which
is not committed). Then a finished video's **Sound** button adds voice-over, dialogue, effects and
music layers, and dubs. Every generation uses your ElevenLabs credits. `ELEVENLABS_API_URL` picks
a data-residency host (e.g. `https://api.eu.residency.elevenlabs.io`).

Expose **only port 8700**. Stop the pod when you're done; if you forget, the app stops it after 30
minutes with nothing rendering and no action in the app (`./start_all.sh --idle-stop 60` to change,
`0` to turn off). On other hosts, set `IDLE_STOP_COMMAND` to the command that stops the machine.

## Origin

Forked from [searope/32gb-rtx-5050-8gb](https://github.com/searope/32gb-rtx-5050-8gb) (the
laptop setup for an RTX 5050 8 GB) at commit `3546bfc`. The two projects now evolve separately.

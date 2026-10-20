# Project policy

**Compute and AI spending is limited to two things:**

1. **A rented cloud GPU** (pay per hour, e.g. RunPod or Vast.ai) running **free, open-weight
   models** locally on that machine: video, image, upscaling, LoRA training, and local language
   models. "Free" means free to download and run; each model still has its own license (see
   **Model licenses** below).
2. **An AI subscription** (Claude, or a similar flat-rate assistant subscription) for the
   language-model work: story planning, scripts, shot lists, continuity review, and development.

**Not allowed without the owner's explicit approval:**

- Pay-per-use generation APIs or credits: ComfyUI partner/API nodes (Kling, Veo, Seedance,
  MiniMax H3 Max, Runway, Vidu…), fal.ai, Replicate, OpenArt credits and similar.
- Metered LLM API keys that bill per token, when the subscription can do the job.

**Approved exception (owner, Oct 2026): ElevenLabs sound.** The app's Sound editor (voice-over,
dialogue, sound effects, music, dubbing; `remote/soundtrack.py`) calls the ElevenLabs API, which
bills the owner's ElevenLabs credits per use. It must stay opt-in: off unless `ELEVENLABS_API_KEY`
is set, labelled as paid in the app and docs, and never required by any other feature. Commercial
use needs a paid ElevenLabs plan; **the owner has one** (free-plan dubs are watermarked; unverified
secondary sources say free-plan output is non-commercial and needs attribution). Check ElevenLabs'
terms before using generated music commercially.

**Approved exception (owner, Oct 2026): the Anthropic API for the Director.** The Director's script
writer (`remote/director.py`) can use the owner's Anthropic API credit (pay per token) as well as
the subscription (headless Claude Code). It must stay opt-in: offered only when `anthropic_api_key`
is set, labelled as paid, confirmed before each request, and never the only way to write a script.
The writer is pluggable (any OpenAI-compatible endpoint or CLI, see `docs/director.md`) because other
people running the app may use other models; other paid providers still need the owner's approval
for the owner's own setup.

**Runpod pods:** the web app runs on the owner's always-on Windows PC (`docs/home-server.md`) and
creates the GPU pod itself when a job needs it, removing it after 10 idle minutes (`remote/gpu.py`).
GPU time should be paid only around jobs: don't leave pods running. When Claude creates a pod by hand
(e.g. the one-time network-volume setup), it confirms GPU type, cloud and expected hourly cost with the
owner first, since a pod bills from the moment it starts, and terminates it when done. Expose only HTTP
port 8700. If the app itself runs on a pod (the older setup), add
`ELEVENLABS_API_KEY={{ RUNPOD_SECRET_elevenlabs_api_key }}` (the owner's Runpod secret).

**Consequences for the code:**

- Every engine must run on the rented GPU from downloadable weights (e.g. Wan 2.2, LTX-2.5,
  FLUX.2 Klein 4B). Do not add `comfy_api_nodes` / partner nodes, or code paths that need an
  `api_key_comfy_org`.
- Prefer quality settings that use more GPU time (more steps, higher resolution, upscaling) over
  paid services. GPU time is the budget.
- Language-model features (story planner, continuity check) should work through the subscription
  tool (e.g. Claude Code writing and editing project files) or a local model on the GPU, not a
  metered API by default.

## Model licenses

**Commercial use of the output must be allowed**: free (preferred), or under a paid license at a
reasonable price that the owner has approved. The owner is in the **USA**, and rented pods usually
run in US/EU datacenters, so a license must cover both.

- **Commercial-safe default set:** **MiniMax H3** (the default video engine, under the owner's
  license below), **Wan 2.2 14B** (+ umt5 text encoder, Wan VAE; Apache 2.0),
  **FLUX.2 Klein 4B** (+ Qwen3 4B text encoder; Apache 2.0), **FILM** interpolation (Apache 2.0).
  Defaults in code (engine, image model, download profiles) must come only from this set, except
  FLUX 3 Klein below.
- **FLUX 3 Klein: the default image model (owner's choice, Oct 2026).** FLUX Kommunity License: free
  commercial use only under $5M annual revenue (above that, a paid Black Forest Labs license). The owner
  accepted that cap. FLUX.2 Klein 4B stays the fallback when FLUX 3 isn't installed, and docs must tell
  anyone else running the app about the cap.
- **MiniMax H3: licensed to the owner (Oct 2026).** The owner holds a written license from MiniMax
  covering all use, including commercial, with no requirement to show "MiniMax H3" anywhere. It is
  the owner's license: the public community license still excludes the US, EU, UK and South Korea,
  so docs must tell anyone else running the app that they need their own license (or use Wan 2.2).
- **Opt-in only, clearly labelled** in the app and docs:
  - FLUX.2 Klein 9B: non-commercial (commercial use needs a paid Black Forest Labs license).
- **Unverified:** the lightx2v speed LoRAs: Wan 2.2 4-step, and MiniMax H3 turbo 8-step v1.0 and
  4-step v1.3 (lightx2v/Minimax-h3-Turbo has no license on its repo; other uploaders call it Apache
  2.0). They power the fast modes; check before commercial use. Quality modes work without them.
- **Planned:** LTX-2.x is free for commercial use under $10M annual revenue (LoRAs trained on it
  carry the same terms); acceptable, but label it.
- Every model added to `setup/models.json` needs a `license` note, and every engine or image model
  in `animate.py` needs an entry in `LICENSES`. The owner chose (Oct 2026) not to show video engine
  license text in Create video, nor image model license text in Create image (Oct 2026): both are hover
  tooltips on their menus. These notes summarize secondary
  sources (Oct 2026); verify the LICENSE file on the official model card before relying on them.

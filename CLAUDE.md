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

- **Commercial-safe default set** (Apache 2.0): **Wan 2.2 14B** (+ umt5 text encoder, Wan VAE),
  **FLUX.2 Klein 4B** (+ Qwen3 4B text encoder), **FILM** interpolation. Defaults in code
  (engine, image model, download profiles) must come only from this set.
- **Opt-in only, clearly labelled** in the app and docs:
  - FLUX.2 Klein 9B: non-commercial (commercial use needs a paid Black Forest Labs license).
  - FLUX 3 Klein: FLUX Kommunity License, free commercial use only under $5M annual revenue.
  - MiniMax H3 (+ its turbo LoRA): its community license excludes the US, EU, UK and South Korea,
    so it is **not usable by the owner** without a separate license from MiniMax. Kept in the code
    for completeness; don't make it a default or build new features on it.
- **Unverified:** Lightx2v Wan 2.2 4-step speed LoRAs. Check the model card before commercial use;
  Wan's quality mode works without them.
- **Planned:** LTX-2.x is free for commercial use under $10M annual revenue (LoRAs trained on it
  carry the same terms); acceptable, but label it.
- Every model added to `setup/models.json` needs a `license` note, and every engine or image model
  in `animate.py` needs an entry in `LICENSES` (shown in the app). These notes summarize secondary
  sources (Oct 2026); verify the LICENSE file on the official model card before relying on them.

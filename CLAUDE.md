# Project policy

**Compute and AI spending is limited to two things:**

1. **A rented cloud GPU** (pay per hour, e.g. RunPod or Vast.ai) running **free, open-weight
   models** locally on that machine: video, image, upscaling, LoRA training, and local language
   models. "Free" means free to download and run; each model still has its own license. Check it
   before commercial use (e.g. FLUX.2 Klein 9B is non-commercial; FLUX.2 Klein 4B is Apache 2.0).
2. **An AI subscription** (Claude, or a similar flat-rate assistant subscription) for the
   language-model work: story planning, scripts, shot lists, continuity review, and development.

**Not allowed without the owner's explicit approval:**

- Pay-per-use generation APIs or credits: ComfyUI partner/API nodes (Kling, Veo, Seedance,
  MiniMax H3 Max, Runway, Vidu…), fal.ai, Replicate, OpenArt credits and similar.
- Metered LLM API keys that bill per token, when the subscription can do the job.

**Consequences for the code:**

- Every engine must run on the rented GPU from downloadable weights (e.g. Wan 2.2, MiniMax H3,
  LTX-2.5, FLUX). Do not add `comfy_api_nodes` / partner nodes, or code paths that need an
  `api_key_comfy_org`.
- Prefer quality settings that use more GPU time (more steps, higher resolution, upscaling) over
  paid services. GPU time is the budget.
- Language-model features (story planner, continuity check) should work through the subscription
  tool (e.g. Claude Code writing and editing project files) or a local model on the GPU, not a
  metered API by default.

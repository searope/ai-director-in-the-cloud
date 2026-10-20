# Director: from a brief to a film

**Director** in the app turns a short brief into a film:

1. **Brief:** title, idea, visual style, length, aspect ratio, cast (your **Characters**) and notes.
2. **Script:** an AI model writes the shot list. You can edit any shot by hand, or type a change
   request ("make it night", "add a close-up of the captain") and revise. Shots that don't change
   keep their keyframes and renders.
3. **Continuity check (optional):** the same AI model reviews the shot list for outfits, props, time
   of day, screen direction and prompts that won't work, and leaves notes on the shots.
   **Fix these with the AI** sends the notes back as a change request.
4. **Keyframes:** for each shot, a start frame (text to image, with the shot's characters' reference
   images) and an end frame (an edit of the start frame). To use your own photo, choose
   **Use a library image as the start frame**, then **Make end keyframe**.
5. **Shots:** each shot is a 2-5 s Wan 2.2 clip between its two keyframes, rendered on the GPU.
6. **Film:** the clips joined in order with hard cuts. The film appears in the Library like any
   video; its **Sound** button lists the script's narration with each line's start time, so a
   voice-over layer takes one tap per line (ElevenLabs, paid, opt-in).

Projects are saved on the PC in `output/director/`.

## Choosing the AI model

The **AI model** menu on each film picks who writes and revises its script. You can switch at any
time; a different model can revise a script another one wrote. All models get the same
instructions and must return the same JSON shot list, so nothing else changes.

With no setup, the app offers:

- **Claude (subscription):** Claude Code, if it's installed and signed in on the PC. The app runs
  it headless (`claude -p`, no tools), so it counts against your Claude subscription's usage
  limits, with no per-use bill.
- **Claude API (paid):** shown when `"anthropic_api_key"` is in `remote/config.json`. Billed per
  token by Anthropic; the app asks before each request. Uses Claude Opus 5.5 by default.

## Adding or changing models

List the models you want under `"writers"` in `remote/config.json`, then restart the app. The
list replaces the defaults above. Each entry needs an `id` (any short name), a `type` and a
`label`; add `"paid": true` to anything billed per use, so the app labels it and asks first.

```json
"writers": [
  {"id": "claude-code", "type": "claude-code", "label": "Claude (subscription)"},
  {"id": "claude-api", "type": "anthropic", "label": "Claude API", "model": "claude-opus-5-5",
   "api_key": "sk-ant-...", "paid": true},
  {"id": "openai", "type": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1",
   "model": "gpt-...", "api_key": "sk-...", "paid": true},
  {"id": "gemini", "type": "openai", "label": "Gemini",
   "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-...",
   "api_key": "...", "paid": true},
  {"id": "local", "type": "openai", "label": "Local model", "base_url": "http://127.0.0.1:8080/v1",
   "model": "qwen3"},
  {"id": "gemini-cli", "type": "command", "label": "Gemini CLI", "command": "gemini"}
]
```

| `type` | What it runs | Settings |
|---|---|---|
| `claude-code` | Claude Code (`claude -p`) on its signed-in subscription | `model` (optional, e.g. `opus`), `path` (if `claude` isn't on PATH), `timeout` (s) |
| `anthropic` | The Anthropic API with JSON output | `api_key` (or `anthropic_api_key` at the top level), `model`, `effort` (`low`-`max`, default `high`) |
| `openai` | Any OpenAI-compatible chat endpoint: OpenAI, Gemini, OpenRouter, Groq, or a local server (llama.cpp, vLLM, Ollama, LM Studio) | `base_url`, `model`, `api_key` (if needed) |
| `command` | Any command-line tool that reads the prompt on stdin and prints the answer, e.g. another vendor's CLI on its own subscription | `command`, `timeout` (s) |

The model names above are placeholders; use the ones your provider lists. An OpenAI-compatible
endpoint should support JSON-schema output (`response_format`); most current ones do. A
`command` tool only gets the schema in its prompt, so pick one that follows instructions well.

**Project policy:** pay-per-use services need the owner's approval (`CLAUDE.md`). The Claude
API is approved as an opt-in; other paid providers are for people running their own copy of the
app with their own accounts.

**Local model on the GPU pod:** a model served on the pod (e.g. llama.cpp's `llama-server` on
port 8080) is free apart from GPU time. It isn't set up by the installer yet: the pod only
exposes the gateway's port 8700.

## Tips

- Keep each shot's **end change** small (one pose or camera change): Wan animates only 2-5 s.
- Tag at most 3 characters per shot. Their main reference image goes to the image model.
- **Fast renders** (4 steps) are good for a first pass; turn it off for the final render.
- "Make all keyframes" and "Render all shots" start the GPU when needed; it stops itself after 10
  idle minutes.

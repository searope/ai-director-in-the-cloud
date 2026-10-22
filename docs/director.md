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
5. **Shots:** each shot is a 2-5 s clip between its two keyframes, rendered on the GPU with the film's
   **Video model**: MiniMax H3 with generated sound (default) or Wan 2.2 (silent).
   **Check clip** has the AI model look at the rendered clip (see below) and suggest a better prompt.
6. **Film:** the clips joined in order with hard cuts. The film appears in the Library like any
   video; its **Sound** button lists the script's narration with each line's start time, so a
   voice-over layer takes one tap per line (ElevenLabs, paid, opt-in).

**Storyboard tab (optional):** a production sheet for the film, in the layout of a film storyboard board.
**Write storyboard** has the AI model (one more request, paid if the model is) plan, from the shot list:
the colour palette and the world's "fingerprint"; reference sheets for the recurring characters,
vehicles, props and creatures (front, side and back views); the main sets and a top-down plan with each
cut's camera setup; every shot's lens, camera move, framing and purpose; and lighting/mood references,
mood keywords and cinematography notes. **Draw pictures** draws the references, sets, plan and lighting
pictures on the GPU with the film's image model (a cast member's sheet uses their reference image);
↻ redraws one. The storyboard panels are the shots' start keyframes. **Print / PDF** prints the sheet
(landscape) or saves it as a PDF. Rewriting keeps every picture whose prompt didn't change.

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

## Redrawn keyframes: checked, then approved

When a render redraws its keyframes in a style (Create video, "Redraw keyframes in this style
first"), two optional steps run before the video, both on by default:

- **Check the redrawn keyframes:** the AI model compares them with the originals (the same people,
  hair, face and clothes in every keyframe; each keeping its own pose and layout; the style applied
  evenly; no borders or extra objects). If it finds problems, the redraw runs again with its advice,
  up to twice, and the best attempt is kept. The result shows on the render card.
- **Stop so I can approve them:** the render then waits with status "your OK", showing the original
  and redrawn keyframes (tap one to see it full size). **Render with these** renders the video from
  exactly these keyframes; **Redraw again** redraws them, with an optional note (e.g. "keep his eyes
  looking up"). While a render waits, the GPU counts as idle, so the pod may stop; approving starts
  one again.

## Checking a rendered clip

**Check clip** (on any finished render in Create video, and on Director shots) shows the clip to the
AI model as one contact sheet: the start and end keyframes, then 12 frames spread through the clip
with their times. The model reports what a viewer would find wrong (faces or hands that morph,
objects that appear, vanish or duplicate, smearing, flicker, motion that ignores the prompt, drift
away from the keyframes), gives a verdict, writes an improved motion prompt, and says when the real
cause is a keyframe (for example an END keyframe with an object the START doesn't have).

- **Render again with this prompt** renders again from your original keyframes with the same
  settings, so a style redraw runs again too (a bad redraw is often what the check found). Tick
  **Keep the redrawn keyframes** to reuse the redrawn ones and change only the prompt.
- In the Director, **Use this prompt and render again** saves it as the shot's motion and renders.
- Keyframe advice is shown as a note; act on it with Remake keyframes or by editing the end change.

The model needs to see images: Claude Code reads the sheet with its Read tool (only that tool,
only in the sheet's folder), the Claude API and OpenAI-compatible endpoints get it in the request,
and a `command` writer gets the file's path. One check is one request (~20-30 s with Claude Code).

## Places: the same room in every shot

A character keeps its face because the image model sees its reference picture; a room needs the same.
In **Characters**, **New place** makes a place: its first image is the **master picture**, a wide view of
the whole room with nobody in it, in your film's aspect ratio; its **layout** says where things are ("bed
against the back wall, nightstand with the lantern on its LEFT, one round window above the headboard"); an
optional **floor plan** (draw it in the plan editor: bed, nightstand, window, door, camera...) shows the room
from above. Add the place to a film's cast: the AI writer lists it in every shot that happens there and
describes the room by its layout, and the image model gets the master picture, the plan and the layout with
every keyframe in that place (up to 3 characters and one place per picture).

With **Draw shots from the place's picture** on (the default), a shot's start keyframe is drawn as an *edit*
of the master picture, so the furniture can't move: the nightstand stays on the same side and the window keeps
its shape. Turn it off for shots whose camera angle is very different from the master's.

## Render all: one shot at a time, each one checked

**Render all** renders the shots one by one. After each render, the project's AI model checks the clip
(below) before the next one starts, and Render all stops at the first shot that needs another go: no GPU time
goes to the rest. It also stops on a shot without keyframes or with a warning. With a writer that is paid per
use, or none set up, it stops after every shot so you can look at it yourself; press Render all again to go on.
Fix a stopped shot (the check suggests a better motion prompt and what to change in the keyframes), render it
again, and Render all carries on from the next one.

Each shot card warns before you render:
- the start and end keyframes are almost the same picture: the clip will barely move (put the action or a
  camera move into the end change);
- the motion or style names how the picture is made (fingers, brushes, glass): video models show those things
  (real fingers smearing the paint). Describe only what the camera sees.

The clip check compares the frames with the shot: every point of its action and end, the characters'
expressions, the timing (one long breath, not a quick in-and-out), whether the clip really moves, anything
showing how it was made, and, for a shot in a place, the room against the place's picture. It also measures how
far the first and last frames are from the keyframes.

## Lantern reels

Format **Lantern reel** (the series' look is filled in when the brief leaves it open: the paint-on-glass style, and any character whose name starts with "Lantern" in the cast) makes a vertical 9:16 short of 20-30 seconds for a series about everyday stress,
being well and mental health. The AI writes the script in four beats (a stressful moment, a reframe while
the quote appears, one simple practice to try, a calm close) with a small glowing lantern in every reel, and
also writes the **words on screen** (each from one shot until another; "glow" lines are lantern amber) and the
post caption. Heavy topics get a line with the US 988 crisis number in the caption. You can edit the words
and caption under the shot list. **Make film** then lays the words over the picture in the Lantern look
(`remote/reel.py`): serif lines at the lower third, the lantern mark in the corner and a closing card with
the glowing lantern. The words-free cut is kept, so the reel can be redone with other words.

Any finished video can become a reel too: open it in the Library and choose **Quote reel** (times in
seconds). A reel is a new video in the library, so Sound (music) and Download work on it; add music after
the words, since Edit reel starts again from the original. The logo is `remote/static/lantern.png`.

## Sound

MiniMax H3 (and LTX) make each clip's soundtrack from a sound line. Each shot has one, written by the AI with
the script: the concrete sounds you'd hear ("a blanket rustling, slow breathing, a clock ticking far away"). A
shot without one gets "quiet room tone and the soft natural sounds of the scene". Never write "no music": the
model has no negative prompt and the word itself brings music in; without any sound line it adds music too.
Describe music only when you want it, or add it later with Sound. Make film levels the joined sound to one
loudness.

## Tips

- Keep each shot's **end change** small (one pose or camera change): a shot is only 2-5 s.
- Tag at most 3 characters per shot. Their main reference image goes to the image model.
- **Fast renders** (4 steps) are good for a first pass; turn it off for the final render.
- "Make all keyframes" and "Render all shots" start the GPU when needed; it stops itself after 10
  idle minutes.

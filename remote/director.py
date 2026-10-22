"""Director: a brief becomes a script and shot list, then keyframes, shots and one film.

A language model writes the shot list (any "writer" below: Claude Code on the owner's
subscription, the Anthropic API, any OpenAI-compatible endpoint, or any command-line tool).
Every writer gets the same instructions and returns JSON that matches SCRIPT_SCHEMA, so the
rest of the pipeline doesn't depend on which model wrote it. Each shot then becomes a start
keyframe (text to image, with the shot's characters as references), an end keyframe (an edit of
the start), and a 2-5 s clip between the two (MiniMax H3 with sound by default, or Wan 2.2).
"Make film" joins the clips in order, keeping their sound.

Projects are output/director/<id>.json. Writers are configured in remote/config.json:

  "writers": [
    {"id": "claude-code", "type": "claude-code", "label": "Claude (subscription)"},
    {"id": "claude-api", "type": "anthropic", "label": "Claude API", "model": "claude-opus-5-5", "paid": true},
    {"id": "local", "type": "openai", "label": "Local model", "base_url": "http://127.0.0.1:8080/v1", "model": "qwen3"},
    {"id": "gemini-cli", "type": "command", "label": "Gemini CLI", "command": "gemini -p"}
  ]

Without a "writers" list: Claude Code if it is installed, plus the Anthropic API when
"anthropic_api_key" is set (paid per token; opt-in, approved by the owner in Oct 2026).
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

import reel

SECONDS = (2, 3, 4, 5)
LENGTH_FOR_SECONDS = {2: 33, 3: 49, 4: 65, 5: 81}  # Wan 2.2 frames at 16 fps
MAX_SHOTS = 40
VIDEO_ENGINES = ("minimax-h3", "wan22", "ltx23")  # the first is the default for new films
TEXT_FIELDS = {"scene": 200, "action": 600, "start_frame": 1500, "end_change": 600, "motion": 600, "narration": 600}

SCRIPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "logline", "shots"],
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "shots": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["scene", "action", "start_frame", "end_change", "motion", "seconds", "characters", "narration"],
            "properties": {
                "scene": {"type": "string"},
                "action": {"type": "string"},
                "start_frame": {"type": "string"},
                "end_change": {"type": "string"},
                "motion": {"type": "string"},
                "seconds": {"type": "integer", "enum": list(SECONDS)},
                "characters": {"type": "array", "items": {"type": "string"}},
                "narration": {"type": "string"},
            },
        }},
    },
}

SYSTEM_PROMPT = """You write shot lists for short AI-animated films.

The film is made shot by shot. For each shot, an image model draws the first frame from
`start_frame`, then edits it into the last frame with `end_change`, and a video model
(MiniMax H3 or Wan 2.2) animates the 2-5 seconds between those two frames following `motion`. Shots are
joined with hard cuts.

For each shot, in order:
- scene: a short heading: place and time of day.
- action: what happens, in one or two sentences, for the human reader.
- start_frame: a complete image prompt for the first frame: who is in it (characters by
  name), the setting, composition and camera framing, lighting, and the film's visual style.
  The image model sees only this text and the characters' reference images, so never refer to
  other shots ("same as before").
- end_change: one edit that turns the first frame into the last frame: a single pose,
  expression, position or camera change, e.g. "she turns toward the camera and smiles".
  Keep it small enough to happen in a few seconds.
- motion: the movement and camera for the video model, e.g. "she turns her head toward the
  camera, hair moving in the wind, slow push-in".
- seconds: 2 to 5.
- characters: the cast members visible in the shot, at most 3, spelled exactly as in the
  cast list. Use [] when no cast member is visible.
- narration: voice-over or dialogue heard during the shot, or "".

Keep the visual style, the characters' looks and the world consistent from shot to shot.
Make the seconds add up to roughly the requested length. Prefer several short, simple shots
to one long complicated move. Give the film a title and a one-sentence logline."""

# A Lantern reel (brief format "reel"): the script also carries the words shown on screen and the post caption.
REEL_SCHEMA = json.loads(json.dumps(SCRIPT_SCHEMA))
REEL_SCHEMA["required"].append("reel")
REEL_SCHEMA["properties"]["reel"] = {
    "type": "object", "additionalProperties": False, "required": ["parts", "caption"],
    "properties": {
        "parts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "accent", "shot", "until_shot"],
            "properties": {"text": {"type": "string"}, "accent": {"type": "boolean"},
                           "shot": {"type": "integer"}, "until_shot": {"type": "integer"}}}},
        "caption": {"type": "string"},
    },
}

# The series' look, filled in when a project becomes a Lantern reel with no style of its own (styles.json "paint-on-glass").
REEL_STYLE = ("paint-on-glass animation, slow-drying oil paint smeared by fingers on backlit glass, soft morphing edges, "
              "luminous glowing colour, visible finger strokes; night blue and warm lantern amber; the painting fills the "
              "frame edge to edge, no paper border")
REEL_AVOID = "border, frame, vignette, paper edge"  # Wan's negative prompt for reel shots

REEL_PROMPT = """

This film is a Lantern reel: a vertical 9:16 short of 20-30 seconds in a series about handling everyday
stress, being well and mental health. Its words appear on screen over the picture. Build it in four beats:
1. Moment (1-2 shots): a relatable stressful moment (a phone that won't stop buzzing, a late night of
   overthinking, a crowded commute). No words on screen yet.
2. Reframe (1-2 shots): the quote appears while the scene changes: the tension eases, the light warms.
3. Try this (1-2 shots): one simple, well-established practice, shown and named: slow breathing (in for 4,
   out for 6), 5-4-3-2-1 grounding, naming the feeling, unclenching jaw and shoulders, a short walk
   outside, writing the worry down. The picture can move with it (a slow rise and fall for a breath).
4. Close (1 shot): a calm picture with the lantern. No words.

The series' mark: a small glowing paper lantern is in every reel. Its light follows the feeling: dim or
flickering in the moment, steadier in the reframe, warm and bright at the close. Keep night blue and warm
amber in every frame. Make the people and creatures original; never imitate characters from other films. When the cast has the
series character (a name starting with "Lantern"), it is the reel's main character: calm, with a neutral,
slightly smiling face, except for the strain of the stressful moment.

`reel.parts` are the words on screen: 2-6 short parts, lowercase, at most 9 words each, gentle and honest
(it's fine to say something is hard; no toxic positivity, no medical claims or promises). Set accent: true
on the one or two key phrases. The practice goes in parts too, e.g. "breathe in… 2… 3… 4" then
"and out… slowly". `shot` is the shot (1-based) where a part appears and `until_shot` the last shot it
stays on; parts meant to be read together share until_shot. Nothing on screen in the last shot.
`reel.caption` is the post caption: one to three short sentences that open up the idea, the practice in one
line, then 3-5 hashtags. When the topic is heavy (burnout, grief, panic, loneliness, hopelessness), add a
line on its own: "If you're struggling, you're not alone. In the US, call or text 988." Never diagnose.
`narration` is usually "": the words are on screen.

Before you answer, check every point and fix the script if one fails:
- the lantern is visible in the first shot and in the last shot, and its light changes with the feeling;
- one practice from the list above is named in the words on screen and shown in the try-this shots;
- every part is lowercase, at most 9 words, and the last shot has no words;
- the caption has the practice on a line of its own;
- every start_frame names the night-blue and lantern-amber palette and the visual style, and says the
  picture fills the frame edge to edge with no border or vignette."""


REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "notes"],
    "properties": {
        "summary": {"type": "string"},
        "notes": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["shot", "note"],
            "properties": {"shot": {"type": "integer"}, "note": {"type": "string"}},
        }},
    },
}

REVIEW_PROMPT = """You check shot lists for short AI-animated films before they are rendered.

Each shot's first frame is drawn from `start_frame` alone (plus the reference images of its
`characters`), its last frame is an edit of the first one by `end_change`, and a video model
animates 2-5 seconds between them. Look for real problems only:
- continuity between shots: a character's looks or outfit, props, time of day, weather,
  location, screen direction;
- a start_frame that refers to other shots ("same as before") or leaves out what the image model
  needs (who, where, framing, light, style), or contradicts the characters' descriptions;
- an end_change or motion too big to happen in the shot's seconds;
- a character named in start_frame but missing from `characters`, or the other way round;
- the story not making sense in this order.

Each note names one problem and how to fix it, e.g. "Mara wears a red coat here but a yellow
one in shot 2: make it yellow." Never restate or summarise a shot: a note that doesn't name a
problem is wrong. Most good shot lists have few notes or none; return an empty list when
nothing needs fixing. The summary is one sentence on the shot list's overall state, e.g.
"Consistent, except Mara's coat in shot 3." """


CAMERA_MOVES = ["STATIC", "PAN", "TILT", "DOLLY", "TRACK", "HANDHELD", "STEADICAM", "CRANE", "DRONE", "ZOOM", "ORBIT"]
FRAMINGS = ["EXTREME WIDE", "WIDE", "MEDIUM WIDE", "MEDIUM", "MEDIUM CLOSE-UP", "CLOSE-UP", "EXTREME CLOSE-UP", "OVER THE SHOULDER", "POV"]
_PROMPTED = {"type": "object", "additionalProperties": False, "required": ["name", "prompt"],
             "properties": {"name": {"type": "string"}, "prompt": {"type": "string"}}}

BOARD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["palette", "fingerprint", "references", "environments", "camera_plan", "shots", "lighting", "mood",
                 "cinematography"],
    "properties": {
        "palette": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "hex"],
            "properties": {"name": {"type": "string"}, "hex": {"type": "string"}}}},
        "fingerprint": {"type": "string"},
        "references": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "kind", "prompt"],
            "properties": {"name": {"type": "string"}, "kind": {"type": "string", "enum": ["character", "vehicle", "prop", "creature"]},
                           "prompt": {"type": "string"}}}},
        "environments": {"type": "array", "items": _PROMPTED},
        "camera_plan": {"type": "object", "additionalProperties": False, "required": ["prompt", "cuts"],
                        "properties": {"prompt": {"type": "string"}, "cuts": {"type": "array", "items": {
                            "type": "object", "additionalProperties": False, "required": ["shot", "setup"],
                            "properties": {"shot": {"type": "integer"}, "setup": {"type": "string"}}}}}},
        "shots": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["shot", "lens_mm", "camera", "framing", "note"],
            "properties": {"shot": {"type": "integer"}, "lens_mm": {"type": "integer"},
                           "camera": {"type": "string", "enum": CAMERA_MOVES}, "framing": {"type": "string", "enum": FRAMINGS},
                           "note": {"type": "string"}}}},
        "lighting": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "note", "prompt"],
            "properties": {"name": {"type": "string"}, "note": {"type": "string"}, "prompt": {"type": "string"}}}},
        "mood": {"type": "array", "items": {"type": "string"}},
        "cinematography": {"type": "array", "items": {"type": "string"}},
    },
}

BOARD_PROMPT = """You are the director of photography preparing the production board (storyboard sheet) for a
short AI-animated film, from its brief and shot list. An image model (FLUX.2) draws every picture on the board
from your prompts alone, so each prompt must be complete and self-contained and include the film's visual style.

- palette: 4 to 6 key colours of the film, each with a short evocative name and its hex code (#rrggbb).
- fingerprint: one line naming the 2-4 visual elements that make the film's world recognisable.
- references: the recurring characters, vehicles, props and creatures (at most 6, most important first). Each
  prompt asks for a reference sheet on a plain light grey studio background with even light: for a character,
  full-body front, side and back views standing side by side plus a close-up of the face, the same outfit in
  every view; for a vehicle or prop, front, side and three-quarter views plus one detail close-up; for a
  creature, side and front views plus a close-up. Describe the subject's look precisely (build, face, hair,
  clothes, colours, materials) so it matches the shot list. Name cast members exactly as in the cast list.
- environments: the 1 to 3 main locations, each a wide establishing picture of the empty set, in the film's style
  and light.
- camera_plan: prompt for a top-down aerial map of the main location showing the paths the action takes
  (a clean illustrated plan, like a location scout's map, no text), and for each shot its camera setup in a few
  words, e.g. "low tracking car alongside, left of the trail".
- shots: one entry per shot in order (shot numbers from 1): a lens in mm (14-200; wide lenses for space and
  speed, long lenses for faces and compression), the camera move, the framing, and one sentence on the shot's
  purpose in the story and what the camera does, e.g. "the chase opens at full speed, establishing the line
  through the forest".
- lighting: 3 or 4 lighting/mood references: a short name, a one-line note, and a prompt for an atmospheric
  picture (a texture or detail of the world in that light, no main characters needed).
- mood: 4 to 6 mood keywords of two or three words, e.g. "sunlit danger".
- cinematography: 4 to 6 short rules for the whole film (lens family, contrast, depth of field, camera
  behaviour, screen direction)."""


# ---------------------------------------------------------------- writers

class WriterError(RuntimeError):
    pass


def find_claude(path=None):
    """Claude Code's real executable. On Windows npm installs a claude.cmd shim; calling the .exe
    behind it directly avoids cmd.exe mangling the JSON arguments."""
    found = path or shutil.which("claude")
    if not found:
        return None
    if found.lower().endswith((".cmd", ".ps1")) or os.path.splitext(found)[1] == "":
        exe = os.path.join(os.path.dirname(found), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
        if os.path.exists(exe):
            return exe
    return found if os.path.exists(found) else None


def _run(cmd, stdin, timeout, shell=False, cwd=None):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              shell=shell, cwd=cwd or tempfile.gettempdir(), creationflags=flags)
    except subprocess.TimeoutExpired:
        raise WriterError(f"no answer after {timeout // 60} minutes") from None
    except OSError as e:
        raise WriterError(f"could not start the writer: {e}") from None


def _json_from_text(text):
    """The first JSON object in a model's text reply (some CLIs wrap it in prose or code fences)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise WriterError("the reply contained no JSON: " + text.strip()[:300])
    try:
        return json.loads(text[start:end + 1])
    except ValueError as e:
        raise WriterError(f"the reply was not valid JSON ({e})") from None


def _image_note(image):
    """For writers that read files themselves: point them at the image."""
    return f"\n\nThe image to look at is the file {image}: read it first." if image else ""


def _image_b64(image):
    with open(image, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()


def _probe(video_path):
    """Seconds, frame rate and whether there is a sound track."""
    import av
    with av.open(video_path) as container:
        v = container.streams.video[0]
        seconds = float(v.duration * v.time_base) if v.duration and v.time_base else float(container.duration or 0) / 1e6
        return {"seconds": seconds, "fps": float(v.average_rate or 16), "audio": bool(container.streams.audio)}


class ClaudeCodeWriter:
    """Headless Claude Code (`claude -p`) on the owner's subscription, with structured output. No tools, except
    Read (limited to the image's folder) when there is an image to look at."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.exe = find_claude(cfg.get("path"))

    @property
    def ready(self):
        return bool(self.exe)

    def write(self, system, user, schema, image=None):
        if not self.exe:
            raise WriterError("Claude Code isn't installed on this computer (or set \"path\" for this writer)")
        folder = os.path.dirname(image) if image else None
        tools = ["--tools", "Read", "--allowedTools", "Read", "--add-dir", folder] if image else ["--tools", ""]
        cmd = [self.exe, "-p", "--output-format", "json", *tools, "--strict-mcp-config",
               "--no-session-persistence", "--json-schema", json.dumps(schema), "--system-prompt", system]
        if self.cfg.get("model"):
            cmd += ["--model", self.cfg["model"]]
        proc = _run(cmd, user + _image_note(image), self.cfg.get("timeout", 900), cwd=folder)
        try:
            out = json.loads(proc.stdout)
        except ValueError:
            raise WriterError((proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()[-600:]) from None
        if out.get("is_error") or out.get("subtype") != "success":
            raise WriterError(str(out.get("result") or out.get("subtype") or "Claude Code failed")[-600:])
        return out.get("structured_output") or _json_from_text(out.get("result") or "")


class AnthropicWriter:
    """The Anthropic API (paid per token), with JSON output constrained to the schema."""

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def ready(self):
        return bool(self.cfg.get("api_key"))

    def write(self, system, user, schema, image=None):
        try:
            import anthropic
        except ImportError:
            raise WriterError("the anthropic package is missing: pip install anthropic") from None
        client = anthropic.Anthropic(api_key=self.cfg["api_key"], timeout=self.cfg.get("timeout", 600))
        try:
            response = client.beta.messages.create(
                model=self.cfg.get("model") or "claude-opus-5-5",
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": ([{"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": _image_b64(image)}}] if image else [])
                    + [{"type": "text", "text": user}]}],
                output_config={"effort": self.cfg.get("effort", "high"),
                               "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default"},  # a declined request is retried on a fallback model
            )
        except anthropic.AuthenticationError:
            raise WriterError("the Anthropic API key was refused") from None
        except anthropic.RateLimitError:
            raise WriterError("Anthropic API rate limit: try again in a minute") from None
        except anthropic.APIStatusError as e:
            raise WriterError(f"Anthropic API error {e.status_code}: {e.message}") from None
        except anthropic.APIConnectionError:
            raise WriterError("could not reach the Anthropic API") from None
        if response.stop_reason == "refusal":
            raise WriterError("the model declined this request")
        if response.stop_reason == "max_tokens":
            raise WriterError("the reply was too long and got cut off")
        return _json_from_text(next((b.text for b in response.content if b.type == "text"), ""))


class OpenAIWriter:
    """Any OpenAI-compatible chat endpoint: OpenAI, Gemini's compatible endpoint, OpenRouter, or a local
    model served by llama.cpp, vLLM or Ollama."""

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def ready(self):
        return bool(self.cfg.get("base_url") and self.cfg.get("model"))

    def write(self, system, user, schema, image=None):
        content = ([{"type": "image_url", "image_url": {"url": "data:image/png;base64," + _image_b64(image)}}] if image else []) \
            + [{"type": "text", "text": user}]
        body = {"model": self.cfg["model"],
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "script", "schema": schema, "strict": True}}}
        headers = {"Content-Type": "application/json"}
        if self.cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {self.cfg['api_key']}"
        req = urllib.request.Request(self.cfg["base_url"].rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.get("timeout", 600)) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise WriterError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise WriterError(f"could not reach {self.cfg['base_url']}: {e}") from None
        try:
            return _json_from_text(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise WriterError("unexpected reply: " + json.dumps(data)[:300]) from None


class CommandWriter:
    """Any command-line tool that reads a prompt on stdin and prints its answer (e.g. a vendor's CLI on
    its own subscription). The schema goes into the prompt, since there is no structured-output flag."""

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def ready(self):
        return bool(self.cfg.get("command"))

    def write(self, system, user, schema, image=None):
        prompt = (f"{system}\n\n{user}{_image_note(image)}\n\nReply with only one JSON object that matches this JSON Schema, "
                  f"with no other text:\n{json.dumps(schema)}")
        proc = _run(self.cfg["command"], prompt, self.cfg.get("timeout", 900), shell=True)
        if proc.returncode:
            raise WriterError((proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()[-600:])
        return _json_from_text(proc.stdout)


WRITER_TYPES = {"claude-code": ClaudeCodeWriter, "anthropic": AnthropicWriter, "openai": OpenAIWriter, "command": CommandWriter}


def load_writers(config):
    """Writers from config["writers"], or the defaults: Claude Code, plus the Anthropic API if a key is set."""
    entries = config.get("writers")
    if entries is None:
        entries = [{"id": "claude-code", "type": "claude-code", "label": "Claude (subscription)"}]
        key = os.environ.get("ANTHROPIC_API_KEY") or config.get("anthropic_api_key")
        if key:
            entries.append({"id": "claude-api", "type": "anthropic", "label": "Claude API", "api_key": key, "paid": True})
    writers = {}
    for entry in entries:
        if entry.get("type") not in WRITER_TYPES or not entry.get("id"):
            print("Ignoring a writer with no id or an unknown type:", entry.get("id"), entry.get("type"))
            continue
        cfg = dict(entry)
        if cfg["type"] == "anthropic" and not cfg.get("api_key"):
            cfg["api_key"] = os.environ.get("ANTHROPIC_API_KEY") or config.get("anthropic_api_key")
        writer = WRITER_TYPES[cfg["type"]](cfg)
        writer.id, writer.label, writer.paid = cfg["id"], cfg.get("label") or cfg["id"], bool(cfg.get("paid"))
        writer.model = cfg.get("model") or ("claude-opus-5-5" if cfg["type"] == "anthropic" else "")
        writers[writer.id] = writer
    return writers


# ---------------------------------------------------------------- projects

def board_items(board):
    """The board's pictures (references, environments, camera plan, lighting), each a dict with key, prompt, image."""
    if not board:
        return []
    return [*board.get("references", []), *board.get("environments", []),
            *([board["camera_plan"]] if board.get("camera_plan") else []), *board.get("lighting", [])]


def _clean_text(value, limit):
    return str(value or "").strip()[:limit]


class Director:
    def __init__(self, root, writers, maker, characters, queue_job, jobs, add_film):
        """queue_job(image_paths, raw_settings) -> job; jobs: the server's job store (for status and files);
        add_film(make_video, keyframe_paths, info) -> job: records a finished film in the library."""
        self.dir = os.path.join(root, "output", "director")
        self.writers, self.maker, self.characters = writers, maker, characters
        self.queue_job, self.jobs, self.add_film = queue_job, jobs, add_film
        self.lock = threading.RLock()
        self.running = {}  # project id -> number of AI requests, keyframe pairs and films in progress
        os.makedirs(self.dir, exist_ok=True)
        for name in os.listdir(self.dir):  # work interrupted by a restart
            if name.endswith(".json"):
                project = self._load(name[:-5])
                if project and self._reset_interrupted(project):
                    self._save(project)

    # ---- storage

    def path(self, project_id):
        if not project_id or len(project_id) != 12 or not all(c in "0123456789abcdef" for c in project_id):
            raise ValueError("bad project id")
        return os.path.join(self.dir, project_id + ".json")

    def _load(self, project_id):
        try:
            with open(self.path(project_id), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save(self, project):
        project["updated"] = time.time()
        tmp = self.path(project["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(project, f, indent=1)
        os.replace(tmp, self.path(project["id"]))

    @staticmethod
    def _reset_interrupted(project):
        changed = False
        if project.get("ai", {}).get("status") == "working":
            project["ai"].update(status="failed", error="The server restarted while the script was being written.")
            changed = True
        if project.get("film", {}).get("status") == "working":
            project["film"].update(status="failed", error="The server restarted while the film was being made.")
            changed = True
        for item in board_items(project.get("board")):
            if item.get("img_status") == "working":
                item.update(img_status="failed", img_error="The server restarted while the picture was being drawn.")
                changed = True
        for shot in project.get("shots", []):
            if shot.get("kf_status") == "working":
                shot.update(kf_status="failed", kf_error="The server restarted while the keyframes were being made.")
                changed = True
        return changed

    def update(self, project_id, fn):
        """Load, change and save a project under the lock; returns the saved project."""
        with self.lock:
            project = self._load(project_id)
            if not project:
                raise KeyError("no such project")
            fn(project)
            self._save(project)
            return project

    def get(self, project_id):
        """The project with each shot's render status filled in from the job queue."""
        project = self._load(project_id)
        if not project:
            return None
        for shot in project["shots"]:
            job = self.jobs.jobs.get(shot.get("job_id") or "")
            shot["render"] = ({"status": job["status"], "progress": job.get("progress"), "error": job.get("error"),
                               "review": job.get("review")} if job else None)
            shot["characters"] = [c for c in shot["characters"] if self.characters.get(c)]
        project["cast"] = [c for c in project["cast"] if self.characters.get(c)]
        film = project.get("film", {})
        if film.get("status") == "done":
            if self.film_path(project):  # the job itself, for its player, Sound and Download
                film["job"] = self.jobs.public(self.jobs.jobs[film["job_id"]])
            else:
                project["film"] = {"status": "idle"}  # its video was deleted from the library
        return project

    def list(self):
        items = []
        for name in os.listdir(self.dir):
            if name.endswith(".json"):
                p = self._load(name[:-5])
                if p:
                    items.append({"id": p["id"], "title": p["title"], "updated": p["updated"], "shots": len(p["shots"]),
                                  "logline": p.get("logline", ""), "cover": next((s["start_image"] for s in p["shots"]
                                                                                  if self.maker.exists(s.get("start_image"))), None)})
        return sorted(items, key=lambda p: p["updated"], reverse=True)

    def busy(self):
        return any(self.running.values())

    def _task_started(self, project_id):
        with self.lock:
            self.running[project_id] = self.running.get(project_id, 0) + 1

    def _task_done(self, project_id):
        with self.lock:
            self.running[project_id] = max(0, self.running.get(project_id, 0) - 1)

    def _update_quietly(self, project_id, fn):
        """update() from a background task: the project or shot may have been deleted meanwhile."""
        try:
            self.update(project_id, fn)
        except KeyError:
            pass

    # ---- editing

    def _clean_brief(self, raw):
        raw = raw or {}
        seconds = raw.get("seconds")
        aspect = raw.get("aspect") if raw.get("aspect") in ("16:9", "9:16", "1:1", "4:3", "3:4") else "16:9"
        fmt = "reel" if raw.get("format") == "reel" else "film"
        if fmt == "reel":
            aspect = "9:16"
        return {"format": fmt, "idea": _clean_text(raw.get("idea"), 4000), "style": _clean_text(raw.get("style"), 300),
                "seconds": max(4, min(300, int(seconds))) if str(seconds or "").isdigit() else 30, "aspect": aspect,
                "notes": _clean_text(raw.get("notes"), 2000)}

    def _clean_cast(self, ids):
        return [i for i in dict.fromkeys(str(i) for i in (ids or [])) if self.characters.get(i)][:12]

    def _clean_shot(self, raw, old=None):
        shot = dict(old or {"id": uuid.uuid4().hex[:8], "start_image": None, "end_image": None, "job_id": None,
                            "kf_status": "idle", "kf_error": None})
        for field, limit in TEXT_FIELDS.items():
            if field in raw:
                shot[field] = _clean_text(raw[field], limit)
            shot.setdefault(field, "")
        if "seconds" in raw:
            shot["seconds"] = int(raw["seconds"]) if str(raw["seconds"]).isdigit() and int(raw["seconds"]) in SECONDS else 5
        shot.setdefault("seconds", 5)
        if "characters" in raw:
            shot["characters"] = [i for i in dict.fromkeys(str(c) for c in raw["characters"] or []) if self.characters.get(i)][:3]
        shot.setdefault("characters", [])
        for key in ("start_image", "end_image"):  # a library image chosen as a keyframe (e.g. the user's own photo)
            if key in raw and raw[key] != shot.get(key):
                if raw[key] and not self.maker.exists(raw[key]):
                    raise ValueError("that image is no longer in the library")
                shot[key] = raw[key] or None
                shot.update(job_id=None, kf_status="done" if shot.get("start_image") and shot.get("end_image") else "idle", kf_error=None)
                if key == "start_image":
                    shot["end_image"] = raw.get("end_image") or None  # the old end frame was made from the old start
        if old:
            changed = {f for f in ("start_frame", "end_change", "characters", "motion", "seconds") if shot.get(f) != old.get(f)}
            if changed & {"start_frame", "end_change", "characters"}:  # the keyframes no longer match
                shot.update(start_image=None, end_image=None, job_id=None, kf_status="idle", kf_error=None)
            elif changed:  # the keyframes still fit; only the clip needs rendering again
                shot["job_id"] = None
        return shot

    def create(self, raw):
        title = _clean_text(raw.get("title"), 120) or "Untitled film"
        writer = raw.get("writer") if raw.get("writer") in self.writers else next(iter(self.writers), None)
        project = {"id": uuid.uuid4().hex[:12], "title": title, "created": time.time(), "logline": "",
                   "brief": self._clean_brief(raw.get("brief")), "cast": self._clean_cast(raw.get("cast")),
                   "writer": writer, "image_model": None, "fast": True, "shots": [],
                   "engine": raw.get("engine") if raw.get("engine") in VIDEO_ENGINES else VIDEO_ENGINES[0],
                   "ai": {"status": "idle"}, "film": {"status": "idle"}}
        with self.lock:
            self._save(project)
        return project

    def edit(self, project_id, raw):
        def change(p):
            if "title" in raw:
                p["title"] = _clean_text(raw["title"], 120) or p["title"]
            if "brief" in raw:
                was_reel = p["brief"].get("format") == "reel"
                p["brief"] = self._clean_brief(raw["brief"])
                if p["brief"]["format"] == "reel" and not was_reel:
                    self._reel_defaults(p)
            if "cast" in raw:
                p["cast"] = self._clean_cast(raw["cast"])
            if raw.get("writer") in self.writers:
                p["writer"] = raw["writer"]
            if "fast" in raw:
                p["fast"] = bool(raw["fast"])
            if raw.get("engine") in VIDEO_ENGINES and raw["engine"] != p.get("engine"):
                p["engine"] = raw["engine"]
            if isinstance(raw.get("reel"), dict):  # the words on screen and the caption, edited by hand
                p["reel"] = self._clean_reel(raw["reel"], len(p["shots"]))
            if "shots" in raw:  # the full list in its new order: known ids are edited, new ones added, missing ones removed
                old = {s["id"]: s for s in p["shots"]}
                p["shots"] = [self._clean_shot(s, old.get(s.get("id"))) for s in raw["shots"][:MAX_SHOTS]]
        return self.update(project_id, change)

    def _reel_defaults(self, project):
        """A project switched to Lantern reel gets the series' look when its brief leaves it open: the
        paint-on-glass style, and the series character (any character named "Lantern ...") in the cast."""
        if not project["brief"]["style"]:
            project["brief"]["style"] = REEL_STYLE
        if not project["cast"]:
            project["cast"] = [c["id"] for c in self.characters.list() if c["name"].lower().startswith("lantern")][:2]

    @staticmethod
    def _clean_reel(raw, shots, quiet_close=False):
        """quiet_close (the AI's words): nothing on screen in the last shot, the reel's calm close."""
        last = shots - 1 if quiet_close and shots > 1 else shots
        parts = []
        for part in (raw.get("parts") or [])[:reel.MAX_PARTS]:
            if not isinstance(part, dict) or not str(part.get("text") or "").strip():
                continue
            shot = max(1, min(last or 1, int(part.get("shot") or 1) if str(part.get("shot") or "").isdigit() else 1))
            until = int(part.get("until_shot") or shot) if str(part.get("until_shot") or "").isdigit() else shot
            parts.append({"text": _clean_text(part["text"], 160), "accent": bool(part.get("accent")),
                          "shot": shot, "until_shot": max(shot, min(last or 1, until))})
        return {"parts": parts, "caption": _clean_text(raw.get("caption"), 2200)}

    def delete(self, project_id):
        if self.running.get(project_id):
            raise ValueError("wait for the running step to finish")
        path = self.path(project_id)
        if os.path.exists(path):
            os.remove(path)

    # ---- writing the script

    def _cast_text(self, project):
        lines = []
        for char_id in project["cast"]:
            c = self.characters.get(char_id)
            if c:
                lines.append(f"- {c['name']}" + (f": {c['description']}" if c["description"] else ""))
        return "\n".join(lines) or "(no cast: invent the people or creatures the story needs, described in each prompt)"

    def _current_script(self, project):
        extra = {"reel": project["reel"]} if project["brief"].get("format") == "reel" and project.get("reel") else {}
        return extra | {"title": project["title"], "logline": project.get("logline", ""), "shots": [
            {k: s[k] for k in ("scene", "action", "start_frame", "end_change", "motion", "seconds", "narration")}
            | {"characters": [c["name"] for c in map(self.characters.get, s["characters"]) if c]}
            for s in project["shots"]]}

    def _prompt(self, project, instruction, task="write"):
        b = project["brief"]
        text = ("Format: Lantern reel. Follow the Lantern reel rules: the lantern in every reel, one practice to try, "
                "the words on screen and the caption.\n" if b.get("format") == "reel" else "")
        text += (f"Title: {project['title']}\nIdea: {b['idea'] or '(none: invent one)'}\n"
                f"Visual style: {b['style'] or '(choose one that suits the idea)'}\nTarget length: about {b['seconds']} seconds\n"
                f"Aspect ratio: {b['aspect']}\n" + (f"Notes: {b['notes']}\n" if b["notes"] else "")
                + f"\nCast (reference images are passed to the image model for these characters):\n{self._cast_text(project)}\n")
        if task == "board":
            return text + (f"\nShot list:\n{json.dumps(self._current_script(project), indent=1)}\n\n"
                           "Prepare the production board." + (f" {instruction}" if instruction else ""))
        if task == "check":
            return text + (f"\nShot list to check:\n{json.dumps(self._current_script(project), indent=1)}\n\n"
                           "List only the problems that need fixing, each with its fix.")
        if instruction and project["shots"]:
            text += (f"\nCurrent shot list:\n{json.dumps(self._current_script(project), indent=1)}\n\nRevise it: {instruction}\n"
                     "Return the whole revised shot list. Keep shots you don't need to change exactly as they are.")
        else:
            text += "\nWrite the shot list." + (f" {instruction}" if instruction else "")
        return text

    def write(self, project_id, instruction="", writer_id=None, task="write"):
        """Start the writer in the background; the project's "ai" status shows progress.
        writer_id switches the project to another writer first (any model can revise any script).
        task "check" asks for continuity notes on the current shot list instead of a new one."""
        instruction = _clean_text(instruction, 2000)
        project = self._load(project_id)
        if not project:
            raise KeyError("no such project")
        if writer_id:
            if writer_id not in self.writers:
                raise ValueError("unknown AI writer")
            project = self.update(project_id, lambda p: p.update(writer=writer_id))
        writer = self.writers.get(project.get("writer"))
        if not writer:
            raise ValueError("no AI writer is set up: see docs/director.md")
        if not writer.ready:
            raise ValueError(f"{writer.label} isn't set up on this computer")
        if task in ("check", "board") and not project["shots"]:
            raise ValueError("write the script first")
        def claim(p):  # under the lock, on the saved copy: two quick taps can't start two writers
            if p.get("ai", {}).get("status") == "working":
                raise ValueError("the AI model is already working on this film")
            p["ai"] = {"status": "working", "task": task, "writer": writer.id, "started": time.time(), "instruction": instruction}
        self.update(project_id, claim)
        self._task_started(project_id)
        threading.Thread(target=self._write, args=(project_id, writer, instruction, task), daemon=True).start()

    def _write(self, project_id, writer, instruction, task="write"):
        try:
            prompt = self._prompt(self._load(project_id), instruction, task)
            if task == "check":
                review = writer.write(REVIEW_PROMPT, prompt, REVIEW_SCHEMA)
                self.update(project_id, lambda p: self._apply_review(p, review, writer))
            elif task == "board":
                board = writer.write(BOARD_PROMPT, prompt, BOARD_SCHEMA)
                self.update(project_id, lambda p: self._apply_board(p, board, writer))
            else:
                is_reel = self._load(project_id)["brief"].get("format") == "reel"
                script = writer.write(SYSTEM_PROMPT + REEL_PROMPT if is_reel else SYSTEM_PROMPT, prompt,
                                      REEL_SCHEMA if is_reel else SCRIPT_SCHEMA)
                self.update(project_id, lambda p: self._apply_script(p, script, writer))
        except Exception as e:  # report any failure in the app
            msg = str(e) if isinstance(e, WriterError) else f"{type(e).__name__}: {e}"
            self._update_quietly(project_id, lambda p: p["ai"].update(status="failed", error=msg[-800:], finished=time.time()))
        finally:
            self._task_done(project_id)

    def _apply_script(self, project, script, writer):
        if not isinstance(script, dict) or not isinstance(script.get("shots"), list) or not script["shots"]:
            raise WriterError("the reply had no shots")
        by_name = {}
        for char_id in project["cast"]:
            c = self.characters.get(char_id)
            if c:
                by_name[c["name"].strip().lower()] = char_id
        old = list(project["shots"])
        shots = []
        for raw in script["shots"][:MAX_SHOTS]:
            if not isinstance(raw, dict):
                continue
            raw = dict(raw, characters=[by_name[n.strip().lower()] for n in raw.get("characters") or []
                                        if isinstance(n, str) and n.strip().lower() in by_name])
            # an unchanged shot keeps its keyframes and clip
            same = next((s for s in old if all(s.get(f) == _clean_text(raw.get(f), TEXT_FIELDS[f]) for f in ("start_frame", "end_change"))
                         and s.get("characters") == raw["characters"][:3]), None)
            if same:
                old.remove(same)
            shots.append(self._clean_shot(raw, same))
        project["shots"] = shots
        project["title"] = _clean_text(script.get("title"), 120) or project["title"]
        project["logline"] = _clean_text(script.get("logline"), 400)
        if isinstance(script.get("reel"), dict):
            project["reel"] = self._clean_reel(script["reel"], len(shots), quiet_close=True)
        project["ai"].update(status="done", error=None, finished=time.time(), writer=writer.id)
        project["review"] = None  # notes were about the old shot list

    def _apply_review(self, project, review, writer):
        if not isinstance(review, dict) or not isinstance(review.get("notes"), list):
            raise WriterError("the reply had no notes")
        notes = []
        for n in review["notes"][:100]:
            if isinstance(n, dict) and isinstance(n.get("shot"), int) and 1 <= n["shot"] <= len(project["shots"]):
                notes.append({"shot_id": project["shots"][n["shot"] - 1]["id"], "note": _clean_text(n.get("note"), 600)})
        project["review"] = {"summary": _clean_text(review.get("summary"), 600), "notes": notes,
                             "writer": writer.id, "finished": time.time()}
        project["ai"].update(status="done", error=None, finished=time.time(), writer=writer.id)

    def _apply_board(self, project, board, writer):
        """Store the board; a picture whose prompt didn't change keeps its image."""
        if not isinstance(board, dict) or not isinstance(board.get("shots"), list):
            raise WriterError("the reply had no board")
        old = {i["key"]: i for i in board_items(project.get("board"))}

        def item(key, name, prompt, **extra):
            prompt = _clean_text(prompt, 1500)
            was = old.get(key)
            keep = was if was and was.get("prompt") == prompt else {}
            return {"key": key, "name": _clean_text(name, 120), "prompt": prompt, "image": keep.get("image"),
                    "img_status": keep.get("img_status", "idle") if keep.get("img_status") != "working" else "idle",
                    "img_error": None, **extra}

        lists = lambda k: [x for x in board.get(k) or [] if isinstance(x, dict)]  # noqa: E731
        plan = board.get("camera_plan") if isinstance(board.get("camera_plan"), dict) else {}
        ids = [s["id"] for s in project["shots"]]
        project["board"] = {
            "palette": [{"name": _clean_text(c.get("name"), 40), "hex": c["hex"]} for c in lists("palette")[:6]
                        if isinstance(c.get("hex"), str) and len(c["hex"]) == 7 and c["hex"].startswith("#")
                        and all(ch in "0123456789abcdefABCDEF" for ch in c["hex"][1:])],
            "fingerprint": _clean_text(board.get("fingerprint"), 300),
            "references": [item(f"ref{i}", r.get("name"), r.get("prompt"),
                                kind=r.get("kind") if r.get("kind") in ("character", "vehicle", "prop", "creature") else "prop")
                           for i, r in enumerate(lists("references")[:6])],
            "environments": [item(f"env{i}", e.get("name"), e.get("prompt")) for i, e in enumerate(lists("environments")[:3])],
            "camera_plan": item("plan", "Top-down plan", plan.get("prompt"),
                                cuts=[{"shot_id": ids[c["shot"] - 1], "setup": _clean_text(c.get("setup"), 200)}
                                      for c in plan.get("cuts") or [] if isinstance(c, dict) and isinstance(c.get("shot"), int)
                                      and 1 <= c["shot"] <= len(ids)]),
            "shots": [{"shot_id": ids[x["shot"] - 1], "lens_mm": max(8, min(800, x["lens_mm"])) if isinstance(x.get("lens_mm"), int) else 35,
                       "camera": x.get("camera") if x.get("camera") in CAMERA_MOVES else "STATIC",
                       "framing": x.get("framing") if x.get("framing") in FRAMINGS else "MEDIUM",
                       "note": _clean_text(x.get("note"), 400)}
                      for x in lists("shots") if isinstance(x.get("shot"), int) and 1 <= x["shot"] <= len(ids)],
            "lighting": [item(f"light{i}", li.get("name"), li.get("prompt"), note=_clean_text(li.get("note"), 200))
                         for i, li in enumerate(lists("lighting")[:4])],
            "mood": [_clean_text(m, 40) for m in board.get("mood") or [] if isinstance(m, str)][:6],
            "cinematography": [_clean_text(m, 160) for m in board.get("cinematography") or [] if isinstance(m, str)][:6],
            "writer": writer.id, "finished": time.time(),
        }
        project["ai"].update(status="done", error=None, finished=time.time(), writer=writer.id)

    def draw_board(self, project_id, keys=None):
        """Draw the board's pictures on the GPU, one after another: the ones not drawn yet, or `keys` again."""
        project = self._load(project_id)
        if not project:
            raise KeyError("no such project")
        items = board_items(project.get("board"))
        if not items:
            raise ValueError("write the storyboard first")
        todo = [i["key"] for i in items if i["prompt"] and (i["key"] in keys if keys else i.get("img_status") != "done" or not i.get("image"))]
        todo = [k for k in todo if next(i for i in items if i["key"] == k).get("img_status") != "working"]
        if not todo:
            raise ValueError("all the pictures are drawn: choose one to redraw")

        def mark(p):
            for i in board_items(p.get("board")):
                if i["key"] in todo:
                    i.update(img_status="working", img_error=None)
        self.update(project_id, mark)
        self._task_started(project_id)
        threading.Thread(target=self._draw_board, args=(project_id, todo), daemon=True).start()

    def _draw_board(self, project_id, keys):
        try:
            for key in keys:
                project = self._load(project_id)
                item = next((i for i in board_items((project or {}).get("board")) if i["key"] == key), None)
                if not item:
                    continue
                # a cast member's reference sheet uses their reference image, so the sheet matches the keyframes
                cast = [c for c in map(self.characters.get, project["cast"]) if c and c["refs"]
                        and c["name"].strip().lower() == item["name"].strip().lower()] if key.startswith("ref") else []
                try:  # (the maker names the cast member's reference image in the prompt)
                    image = self._wait_task(self.maker.start(item["prompt"], "16:9", None, None, project.get("image_model"),
                                                             self.characters.cast([cast[0]["id"]]) if cast else ()))
                    change = {"image": image, "img_status": "done", "img_error": None}
                except Exception as e:  # keep drawing the others
                    change = {"img_status": "failed", "img_error": str(e)[-400:]}

                def save(p, key=key, change=change):
                    for i in board_items(p.get("board")):
                        if i["key"] == key and i.get("prompt") == item["prompt"]:  # not rewritten meanwhile
                            i.update(change)
                self._update_quietly(project_id, save)
        finally:
            self._task_done(project_id)

    # ---- keyframes, clips and the film

    def _shot(self, project, shot_id):
        shot = next((s for s in project["shots"] if s["id"] == shot_id), None)
        if not shot:
            raise KeyError("no such shot")
        return shot

    def _wait_task(self, task):
        while task["status"] == "running":
            time.sleep(2)
            task = self.maker.get(task["id"]) or {"status": "failed", "error": "the image task disappeared"}
        if task["status"] != "done":
            raise RuntimeError(task.get("error") or "the image failed")
        return task["image_id"]

    def make_keyframes(self, project_id, shot_id, keep_start=False):
        """Make the shot's start and end keyframes; keep_start makes only the end one from the current start
        (e.g. a photo the user chose)."""
        project = self._load(project_id)
        if not project:
            raise KeyError("no such project")
        shot = self._shot(project, shot_id)
        keep_start = keep_start and self.maker.exists(shot.get("start_image") or "")
        if not shot["end_change"] or not (keep_start or shot["start_frame"]):
            raise ValueError("write the start frame and the end change first")
        if shot.get("kf_status") == "working":
            raise ValueError("these keyframes are already being made")
        cast = self.characters.cast([c for c in shot["characters"] if self.characters.get(c)])  # skip deleted ones

        def mark(p):
            s = self._shot(p, shot_id)
            s.update(kf_status="working", kf_error=None, end_image=None, job_id=None)
            if not keep_start:
                s["start_image"] = None
        self.update(project_id, mark)
        self._task_started(project_id)
        threading.Thread(target=self._keyframes, args=(project_id, shot_id, project, shot, cast, keep_start), daemon=True).start()

    def _keyframes(self, project_id, shot_id, project, shot, cast, keep_start=False):
        try:
            model, aspect = project.get("image_model"), project["brief"]["aspect"]
            if keep_start:
                start = shot["start_image"]
            else:
                start = self._wait_task(self.maker.start(shot["start_frame"], aspect, None, None, model, cast))
                self.update(project_id, lambda p: self._shot(p, shot_id).update(start_image=start))
            end = self._wait_task(self.maker.start(shot["end_change"], aspect, start, None, model, cast))
            self.update(project_id, lambda p: self._shot(p, shot_id).update(end_image=end, kf_status="done"))
        except Exception as e:
            self._update_quietly(project_id, lambda p: self._shot(p, shot_id).update(kf_status="failed", kf_error=str(e)[-600:]))
        finally:
            self._task_done(project_id)

    def render(self, project_id, shot_id):
        project = self._load(project_id)
        if not project:
            raise KeyError("no such project")
        shot = self._shot(project, shot_id)
        paths = [self.maker.path(shot.get("start_image") or ""), self.maker.path(shot.get("end_image") or "")] \
            if shot.get("start_image") and shot.get("end_image") else None
        if not paths or not all(os.path.exists(p) for p in paths):
            raise ValueError("make the keyframes first")
        engine = project.get("engine", "wan22")  # films made before H3 became the default rendered with Wan
        job = self.queue_job(paths, {"prompts": [shot["motion"] or "smooth natural motion"], "engine": engine,
                                     "image_model": project.get("image_model") or self.maker.default_model(), "style": "photoreal",
                                     # the film's style keeps the clip in it (and tells Check clip what to judge)
                                     "custom_style": project["brief"].get("style", ""),
                                     "restyle": False, "fast": project.get("fast", True), "audio": True,
                                     "length": LENGTH_FOR_SECONDS[shot["seconds"]], "size": "standard",
                                     "smooth": engine == "wan22",  # H3 and LTX already render 24-25 fps
                                     "negative_extra": REEL_AVOID if project["brief"].get("format") == "reel" else ""})
        self.update(project_id, lambda p: self._shot(p, shot_id).update(job_id=job["id"]))
        return job

    def film_path(self, project):
        """The latest film's video (a job in the library), or None."""
        job_id = (project or {}).get("film", {}).get("job_id")
        path = os.path.join(self.jobs.dir(job_id), "result.mp4") if job_id and job_id in self.jobs.jobs else None
        return path if path and os.path.exists(path) else None

    def make_film(self, project_id):
        project = self.get(project_id)
        if not project:
            raise KeyError("no such project")
        clips, keyframes, narration, start, bounds = [], [], [], 0.0, []
        for i, shot in enumerate(project["shots"]):
            if not shot["render"] or shot["render"]["status"] != "done":
                raise ValueError(f"shot {i + 1} isn't rendered yet")
            clips.append(os.path.join(self.jobs.dir(shot["job_id"]), "result.mp4"))
            if self.maker.exists(shot.get("start_image") or ""):
                keyframes.append(self.maker.path(shot["start_image"]))
            if shot["narration"]:
                narration.append({"start": round(start, 2), "text": shot["narration"], "shot": i + 1})
            bounds.append((start, start + _probe(clips[-1])["seconds"]))
            start = bounds[-1][1]
        if not clips:
            raise ValueError("there are no shots")
        if project.get("film", {}).get("status") == "working":
            raise ValueError("the film is already being made")
        info = {"title": project["title"], "fast": project.get("fast", True), "narration": narration,
                "prompts": [s["action"] or s["motion"] for s in project["shots"]],
                "engine": "minimax-h3" if any(_probe(c)["audio"] for c in clips) else "wan22"}
        words = self.reel_times(project.get("reel", {}).get("parts", []), bounds) if project["brief"].get("format") == "reel" else None
        if words is not None and not words:
            raise ValueError("the reel has no words on screen: write them in Words on screen")
        self.update(project_id, lambda p: p.update(film={"status": "working", "started": time.time()}))
        self._task_started(project_id)
        threading.Thread(target=self._film, args=(project_id, clips, keyframes, info, words), daemon=True).start()

    @staticmethod
    def reel_times(parts, bounds):
        """Seconds for each part on screen: it appears a moment into its shot (a little later for each earlier
        part in the same shot) and stays until the end of its until_shot."""
        timed, per_shot = [], {}
        for part in parts:
            first, last = min(part["shot"], len(bounds)) - 1, min(part["until_shot"], len(bounds)) - 1
            k = per_shot[first] = per_shot.get(first, -1) + 1
            start = bounds[first][0] + 0.6 + 1.4 * k
            end = bounds[max(first, last)][1] - 0.15
            if end - start >= 0.8:
                timed.append({"text": part["text"], "accent": part["accent"], "start": round(start, 2), "end": round(end, 2)})
        return timed

    @staticmethod
    def _join(clips, out_path):
        """Join the clips with hard cuts, re-encoding so mixed sizes, frame rates and engines fit together.
        Sound is kept (H3 clips have it); a silent clip gets silence of its length."""
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        info = [_probe(c) for c in clips]
        fps = max(round(i["fps"]) for i in info)  # 24 for H3 only, 32 for smoothed Wan
        inputs = sum((["-i", c] for c in clips), [])
        parts = [f"[{n}:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,fps={fps}[v{n}];" for n in range(len(clips))]
        extra = len(clips)  # index of the next generated input
        with_audio = any(m["audio"] for m in info)
        if with_audio:
            for n, meta in enumerate(info):
                if meta["audio"]:
                    parts.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo[a{n}];")
                else:  # silence as long as the clip
                    inputs += ["-f", "lavfi", "-t", f"{meta['seconds']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
                    parts.append(f"[{extra}:a]anull[a{n}];")
                    extra += 1
        joined = "".join(f"[v{n}]" + (f"[a{n}]" if with_audio else "") for n in range(len(clips)))
        maps = ["-map", "[out]"] + (["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"] if with_audio else [])
        outs = "[out][aout]" if with_audio else "[out]"
        cmd = [ffmpeg, "-y", "-loglevel", "error"] + inputs + [
            "-filter_complex", "".join(parts) + f"{joined}concat=n={len(clips)}:v=1:a={int(with_audio)}{outs}",
            *maps, "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
        proc = _run(cmd, None, 1800)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip()[-600:] or "ffmpeg failed")

    def _film(self, project_id, clips, keyframes, info, words=None):
        def make(path):
            if words is None:
                return self._join(clips, path)
            clean = os.path.join(os.path.dirname(path), "clean.mp4")  # the cut without words, for editing them later
            self._join(clips, clean)
            reel.compose(clean, path, words)
        try:
            job = self.add_film(make, keyframes, info)
            if words is not None:
                project = self._load(project_id) or {}
                self.jobs.update(job, reel={"source": job["id"], "parts": words, "logo": True, "end_card": True,
                                            "caption": project.get("reel", {}).get("caption", "")})
            self.update(project_id, lambda p: p.update(film={"status": "done", "finished": time.time(),
                                                              "shots": len(clips), "job_id": job["id"]}))
        except Exception as e:
            self._update_quietly(project_id, lambda p: p.update(film={"status": "failed", "error": str(e)[-600:]}))
        finally:
            self._task_done(project_id)

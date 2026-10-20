"""Director: a brief becomes a script and shot list, then keyframes, shots and one film.

A language model writes the shot list (any "writer" below: Claude Code on the owner's
subscription, the Anthropic API, any OpenAI-compatible endpoint, or any command-line tool).
Every writer gets the same instructions and returns JSON that matches SCRIPT_SCHEMA, so the
rest of the pipeline doesn't depend on which model wrote it. Each shot then becomes a start
keyframe (text to image, with the shot's characters as references), an end keyframe (an edit of
the start), and a 2-5 s Wan 2.2 clip between the two. "Make film" joins the clips in order.

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

SECONDS = (2, 3, 4, 5)
LENGTH_FOR_SECONDS = {2: 33, 3: 49, 4: 65, 5: 81}  # Wan 2.2 frames at 16 fps
MAX_SHOTS = 40
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
(Wan 2.2) animates the 2-5 seconds between those two frames following `motion`. Shots are
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
        return {"idea": _clean_text(raw.get("idea"), 4000), "style": _clean_text(raw.get("style"), 300),
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
                   "writer": writer, "image_model": "klein4b", "fast": True, "shots": [],
                   "ai": {"status": "idle"}, "film": {"status": "idle"}}
        with self.lock:
            self._save(project)
        return project

    def edit(self, project_id, raw):
        def change(p):
            if "title" in raw:
                p["title"] = _clean_text(raw["title"], 120) or p["title"]
            if "brief" in raw:
                p["brief"] = self._clean_brief(raw["brief"])
            if "cast" in raw:
                p["cast"] = self._clean_cast(raw["cast"])
            if raw.get("writer") in self.writers:
                p["writer"] = raw["writer"]
            if "fast" in raw:
                p["fast"] = bool(raw["fast"])
            if "shots" in raw:  # the full list in its new order: known ids are edited, new ones added, missing ones removed
                old = {s["id"]: s for s in p["shots"]}
                p["shots"] = [self._clean_shot(s, old.get(s.get("id"))) for s in raw["shots"][:MAX_SHOTS]]
        return self.update(project_id, change)

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
        return {"title": project["title"], "logline": project.get("logline", ""), "shots": [
            {k: s[k] for k in ("scene", "action", "start_frame", "end_change", "motion", "seconds", "narration")}
            | {"characters": [c["name"] for c in map(self.characters.get, s["characters"]) if c]}
            for s in project["shots"]]}

    def _prompt(self, project, instruction, task="write"):
        b = project["brief"]
        text = (f"Title: {project['title']}\nIdea: {b['idea'] or '(none: invent one)'}\n"
                f"Visual style: {b['style'] or '(choose one that suits the idea)'}\nTarget length: about {b['seconds']} seconds\n"
                f"Aspect ratio: {b['aspect']}\n" + (f"Notes: {b['notes']}\n" if b["notes"] else "")
                + f"\nCast (reference images are passed to the image model for these characters):\n{self._cast_text(project)}\n")
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
        if task == "check" and not project["shots"]:
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
            else:
                script = writer.write(SYSTEM_PROMPT, prompt, SCRIPT_SCHEMA)
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
            model, aspect = project.get("image_model", "klein4b"), project["brief"]["aspect"]
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
        job = self.queue_job(paths, {"prompts": [shot["motion"] or "smooth natural motion"], "engine": "wan22",
                                     "image_model": project.get("image_model", "klein4b"), "style": "photoreal",
                                     "restyle": False, "fast": project.get("fast", True),
                                     "length": LENGTH_FOR_SECONDS[shot["seconds"]], "size": "standard", "smooth": True})
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
        clips, keyframes, narration, start = [], [], [], 0.0
        for i, shot in enumerate(project["shots"]):
            if not shot["render"] or shot["render"]["status"] != "done":
                raise ValueError(f"shot {i + 1} isn't rendered yet")
            clips.append(os.path.join(self.jobs.dir(shot["job_id"]), "result.mp4"))
            if self.maker.exists(shot.get("start_image") or ""):
                keyframes.append(self.maker.path(shot["start_image"]))
            if shot["narration"]:
                narration.append({"start": round(start, 2), "text": shot["narration"], "shot": i + 1})
            start += (LENGTH_FOR_SECONDS[shot["seconds"]] - 1) / 16  # a Wan clip lasts (frames - 1) / 16 s
        if not clips:
            raise ValueError("there are no shots")
        if project.get("film", {}).get("status") == "working":
            raise ValueError("the film is already being made")
        info = {"title": project["title"], "fast": project.get("fast", True), "narration": narration,
                "prompts": [s["action"] or s["motion"] for s in project["shots"]]}
        self.update(project_id, lambda p: p.update(film={"status": "working", "started": time.time()}))
        self._task_started(project_id)
        threading.Thread(target=self._film, args=(project_id, clips, keyframes, info), daemon=True).start()

    @staticmethod
    def _join(clips, out_path):
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        # Re-encode while joining: the clips are all Wan output, but this also copes with mixed sizes.
        streams = "".join(f"[{i}:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,fps=32[v{i}];" for i in range(len(clips)))
        joined = "".join(f"[v{i}]" for i in range(len(clips)))
        cmd = [ffmpeg, "-y", "-loglevel", "error"] + sum((["-i", c] for c in clips), []) + [
            "-filter_complex", f"{streams}{joined}concat=n={len(clips)}:v=1:a=0[out]",
            "-map", "[out]", "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
        proc = _run(cmd, None, 1800)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip()[-600:] or "ffmpeg failed")

    def _film(self, project_id, clips, keyframes, info):
        try:
            job = self.add_film(lambda path: self._join(clips, path), keyframes, info)
            self.update(project_id, lambda p: p.update(film={"status": "done", "finished": time.time(),
                                                              "shots": len(clips), "job_id": job["id"]}))
        except Exception as e:
            self._update_quietly(project_id, lambda p: p.update(film={"status": "failed", "error": str(e)[-600:]}))
        finally:
            self._task_done(project_id)

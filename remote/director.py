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


def _run(cmd, stdin, timeout, shell=False):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                              shell=shell, cwd=tempfile.gettempdir(), creationflags=flags)
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


class ClaudeCodeWriter:
    """Headless Claude Code (`claude -p`) on the owner's subscription: no tools, structured output."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.exe = find_claude(cfg.get("path"))

    @property
    def ready(self):
        return bool(self.exe)

    def write(self, system, user, schema):
        if not self.exe:
            raise WriterError("Claude Code isn't installed on this computer (or set \"path\" for this writer)")
        cmd = [self.exe, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
               "--no-session-persistence", "--json-schema", json.dumps(schema), "--system-prompt", system]
        if self.cfg.get("model"):
            cmd += ["--model", self.cfg["model"]]
        proc = _run(cmd, user, self.cfg.get("timeout", 900))
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

    def write(self, system, user, schema):
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
                messages=[{"role": "user", "content": user}],
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
            raise WriterError("the script was too long for one reply; ask for fewer shots")
        return _json_from_text(next((b.text for b in response.content if b.type == "text"), ""))


class OpenAIWriter:
    """Any OpenAI-compatible chat endpoint: OpenAI, Gemini's compatible endpoint, OpenRouter, or a local
    model served by llama.cpp, vLLM or Ollama."""

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def ready(self):
        return bool(self.cfg.get("base_url") and self.cfg.get("model"))

    def write(self, system, user, schema):
        body = {"model": self.cfg["model"],
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
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

    def write(self, system, user, schema):
        prompt = (f"{system}\n\n{user}\n\nReply with only one JSON object that matches this JSON Schema, "
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
    def __init__(self, root, writers, maker, characters, queue_job, jobs):
        """queue_job(image_paths, raw_settings) -> job; jobs: the server's job store (for status and files)."""
        self.dir = os.path.join(root, "output", "director")
        self.writers, self.maker, self.characters = writers, maker, characters
        self.queue_job, self.jobs = queue_job, jobs
        self.lock = threading.RLock()
        self.busy_ids = set()  # projects with an AI request, keyframes or a film in progress
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
            shot["render"] = ({"status": job["status"], "progress": job.get("progress"), "error": job.get("error")}
                              if job else None)
        project["cast"] = [c for c in project["cast"] if self.characters.get(c)]
        return project

    def list(self):
        items = []
        for name in os.listdir(self.dir):
            if name.endswith(".json"):
                p = self._load(name[:-5])
                if p:
                    items.append({"id": p["id"], "title": p["title"], "updated": p["updated"], "shots": len(p["shots"]),
                                  "logline": p.get("logline", ""), "cover": next((s.get("start_image") for s in p["shots"]
                                                                                  if s.get("start_image")), None)})
        return sorted(items, key=lambda p: p["updated"], reverse=True)

    def busy(self):
        return bool(self.busy_ids)

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
        if project_id in self.busy_ids:
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

    def _prompt(self, project, instruction):
        b = project["brief"]
        text = (f"Title: {project['title']}\nIdea: {b['idea'] or '(none: invent one)'}\n"
                f"Visual style: {b['style'] or '(choose one that suits the idea)'}\nTarget length: about {b['seconds']} seconds\n"
                f"Aspect ratio: {b['aspect']}\n" + (f"Notes: {b['notes']}\n" if b["notes"] else "")
                + f"\nCast (reference images are passed to the image model for these characters):\n{self._cast_text(project)}\n")
        if instruction and project["shots"]:
            current = {"title": project["title"], "logline": project.get("logline", ""), "shots": [
                {k: s[k] for k in ("scene", "action", "start_frame", "end_change", "motion", "seconds", "narration")}
                | {"characters": [c["name"] for c in map(self.characters.get, s["characters"]) if c]}
                for s in project["shots"]]}
            text += (f"\nCurrent shot list:\n{json.dumps(current, indent=1)}\n\nRevise it: {instruction}\n"
                     "Return the whole revised shot list. Keep shots you don't need to change exactly as they are.")
        else:
            text += "\nWrite the shot list." + (f" {instruction}" if instruction else "")
        return text

    def write(self, project_id, instruction="", writer_id=None):
        """Start the writer in the background; the project's "ai" status shows progress.
        writer_id switches the project to another writer first (any model can revise any script)."""
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
        with self.lock:
            if project_id in self.busy_ids and project.get("ai", {}).get("status") == "working":
                raise ValueError("the script is already being written")
            self.busy_ids.add(project_id)
        self.update(project_id, lambda p: p.update(ai={"status": "working", "writer": writer.id, "started": time.time(),
                                                       "instruction": instruction}))
        threading.Thread(target=self._write, args=(project_id, writer, instruction), daemon=True).start()

    def _write(self, project_id, writer, instruction):
        try:
            prompt = self._prompt(self._load(project_id), instruction)
            script = writer.write(SYSTEM_PROMPT, prompt, SCRIPT_SCHEMA)
            self.update(project_id, lambda p: self._apply_script(p, script, writer))
        except Exception as e:  # report any failure in the app
            msg = str(e) if isinstance(e, WriterError) else f"{type(e).__name__}: {e}"
            self.update(project_id, lambda p: p["ai"].update(status="failed", error=msg[-800:], finished=time.time()))
        finally:
            self.busy_ids.discard(project_id)

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

    def make_keyframes(self, project_id, shot_id):
        project = self._load(project_id)
        if not project:
            raise KeyError("no such project")
        shot = self._shot(project, shot_id)
        if not shot["start_frame"] or not shot["end_change"]:
            raise ValueError("write the start frame and the end change first")
        if shot.get("kf_status") == "working":
            raise ValueError("these keyframes are already being made")
        cast = self.characters.cast(shot["characters"])

        def mark(p):
            self._shot(p, shot_id).update(kf_status="working", kf_error=None, start_image=None, end_image=None, job_id=None)
        self.update(project_id, mark)
        self.busy_ids.add(project_id)
        threading.Thread(target=self._keyframes, args=(project_id, shot_id, project, shot, cast), daemon=True).start()

    def _keyframes(self, project_id, shot_id, project, shot, cast):
        try:
            model, aspect = project.get("image_model", "klein4b"), project["brief"]["aspect"]
            start = self._wait_task(self.maker.start(shot["start_frame"], aspect, None, None, model, cast))
            self.update(project_id, lambda p: self._shot(p, shot_id).update(start_image=start))
            end = self._wait_task(self.maker.start(shot["end_change"], aspect, start, None, model, cast))
            self.update(project_id, lambda p: self._shot(p, shot_id).update(end_image=end, kf_status="done"))
        except Exception as e:
            self.update(project_id, lambda p: self._shot(p, shot_id).update(kf_status="failed", kf_error=str(e)[-600:]))
        finally:
            if not any(s.get("kf_status") == "working" for s in (self._load(project_id) or {}).get("shots", [])):
                self.busy_ids.discard(project_id)

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

    def film_path(self, project_id):
        return os.path.join(self.dir, project_id + ".mp4")

    def make_film(self, project_id):
        project = self.get(project_id)
        if not project:
            raise KeyError("no such project")
        clips = []
        for i, shot in enumerate(project["shots"]):
            if not shot["render"] or shot["render"]["status"] != "done":
                raise ValueError(f"shot {i + 1} isn't rendered yet")
            clips.append(os.path.join(self.jobs.dir(shot["job_id"]), "result.mp4"))
        if not clips:
            raise ValueError("there are no shots")
        if project.get("film", {}).get("status") == "working":
            raise ValueError("the film is already being made")
        self.update(project_id, lambda p: p.update(film={"status": "working", "started": time.time()}))
        self.busy_ids.add(project_id)
        threading.Thread(target=self._film, args=(project_id, clips), daemon=True).start()

    def _film(self, project_id, clips):
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            tmp = self.film_path(project_id) + ".tmp.mp4"
            # Re-encode while joining: the clips are all Wan output, but this also copes with mixed sizes.
            streams = "".join(f"[{i}:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,fps=32[v{i}];" for i in range(len(clips)))
            joined = "".join(f"[v{i}]" for i in range(len(clips)))
            cmd = [ffmpeg, "-y", "-loglevel", "error"] + sum((["-i", c] for c in clips), []) + [
                "-filter_complex", f"{streams}{joined}concat=n={len(clips)}:v=1:a=0[out]",
                "-map", "[out]", "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp]
            proc = _run(cmd, None, 1800)
            if proc.returncode:
                raise RuntimeError(proc.stderr.strip()[-600:] or "ffmpeg failed")
            os.replace(tmp, self.film_path(project_id))
            self.update(project_id, lambda p: p.update(film={"status": "done", "finished": time.time(), "shots": len(clips)}))
        except Exception as e:
            self.update(project_id, lambda p: p.update(film={"status": "failed", "error": str(e)[-600:]}))
        finally:
            self.busy_ids.discard(project_id)

"""Phone/browser remote for the keyframe animator (local machine or rented cloud GPU).

Serves a mobile web app (remote/static) on the local network. From the phone
you can upload keyframes (camera roll or camera), choose a style, write motion
prompts, queue renders, watch progress, cancel, and play/download the results.
Jobs run one at a time through animate.run() against the local ComfyUI.

Usage (with ComfyUI already running):
    python remote/server.py            # prints the link + QR code to open on the phone
    python remote/server.py --new-token

Every API call needs the access token from the printed link, so other people on
the same Wi-Fi can't use the laptop. For access away from home use Tailscale
(see README) rather than port forwarding.
"""

import argparse
import asyncio
import copy
import hmac
import io
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import animate  # noqa: E402
import clipreview  # noqa: E402
import director as directors  # noqa: E402
import gpu as gpus  # noqa: E402
import soundtrack  # noqa: E402

STATIC = os.path.join(HERE, "static")
CONFIG = os.path.join(HERE, "config.json")
JOBS_DIR = os.path.join(ROOT, "output", "remote_jobs")
KEYFRAMES_DIR = os.path.join(ROOT, "output", "keyframe_maker")
CHARACTERS_DIR = os.path.join(ROOT, "output", "characters")
MODELS_DIR = os.path.join(ROOT, "ComfyUI", "models")  # when ComfyUI runs on this machine
MAX_KEYFRAMES = 12
MAX_CHARACTER_REFS = 4  # reference images per character; the first one goes to the image model
MAX_CAST = 3            # characters per keyframe: each adds a reference image to the model's input
MAX_SIDE = 2048  # phone photos are downscaled to this before rendering

SIZES = {"preview": 512 * 288, "standard": 832 * 480}
LENGTHS = {33, 49, 65, 81}


# ---------------------------------------------------------------- job store

class Jobs:
    def __init__(self, gpu):
        self.gpu = gpu
        self.keyframe_checker = lambda job: None  # set in main(): job -> check(originals, redrawn) or None
        self.lock = threading.Lock()
        self.jobs = {}
        self.pending = queue.Queue()
        os.makedirs(JOBS_DIR, exist_ok=True)
        for job_id in sorted(os.listdir(JOBS_DIR)):
            path = os.path.join(JOBS_DIR, job_id, "job.json")
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                job = json.load(f)
            if job["status"] == "running":
                job.update(status="failed", error="Server restarted while this job was running.")
            if (job.get("review") or {}).get("status") == "working":
                job["review"] = {"status": "failed", "error": "The server restarted during the check."}
            self.jobs[job_id] = job
            if job["status"] == "queued":
                self.pending.put(job_id)
        threading.Thread(target=self._worker, daemon=True).start()

    def dir(self, job_id):
        return os.path.join(JOBS_DIR, job_id)

    def save(self, job):
        tmp = os.path.join(self.dir(job["id"]), "job.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in job.items() if k != "cancel"}, f, indent=1)
        os.replace(tmp, os.path.join(self.dir(job["id"]), "job.json"))

    def update(self, job, **fields):
        with self.lock:
            job.update(fields)
            self.save(job)

    def public(self, job):
        with self.lock:
            data = copy.deepcopy({k: v for k, v in job.items() if k != "cancel"})
        data["queue_position"] = self.position(job["id"])
        if data["status"] == "running" and not data["styled"]:  # redrawn keyframes show as soon as they exist
            folder = os.path.join(self.dir(job["id"]), "result_keyframes")
            if os.path.isdir(folder):  # NN.png only: the restyle's temporary files are named otherwise
                data["styled"] = sorted(n for n in os.listdir(folder) if len(n) == 6 and n[:2].isdigit() and n.endswith(".png"))
        return data

    def position(self, job_id):
        queued = sorted((j for j in self.jobs.values() if j["status"] == "queued"), key=lambda j: j["created"])
        ids = [j["id"] for j in queued]
        return ids.index(job_id) + 1 if job_id in ids else None

    def add(self, job):
        with self.lock:
            self.jobs[job["id"]] = job
            self.save(job)
        self.pending.put(job["id"])

    def busy(self):
        with self.lock:
            return any(j["status"] in ("queued", "running") for j in self.jobs.values())

    def delete(self, job_id):
        with self.lock:
            self.jobs.pop(job_id, None)
        shutil.rmtree(self.dir(job_id), ignore_errors=True)

    def _worker(self):
        while True:
            job_id = self.pending.get()
            job = self.jobs.get(job_id)
            if not job or job["status"] != "queued":
                continue
            self._run(job)

    def _run(self, job):
        out_dir = self.dir(job["id"])
        s = job["settings"]
        overrides = {
            "server": None,  # set once the GPU is up
            "keyframes": [os.path.join(out_dir, "inputs", name) for name in job["inputs"]],
            "prompts": s["prompts"],
            "engine": s.get("engine", "minimax-h3"),
            "image_model": s.get("image_model", "klein4b"),
            "audio": s.get("audio", True),
            "sound": s.get("sound") or None,
            "style": s.get("style"),
            "custom_style": s.get("custom_style") or None,
            "restyle": s.get("restyle"),
            "fast": s.get("fast", True),
            "turbo_steps": s.get("turbo_steps", 8),
            "keyframe_check": self.keyframe_checker(job) if s.get("check_keyframes") else None,
            "length": s.get("length", 81),
            "interpolate": s.get("interpolate", 2),
            "area": SIZES.get(s.get("size"), SIZES["standard"]),
            "loop": s.get("loop", False),
            "seed": s.get("seed"),
            "output": os.path.join(out_dir, "result.mp4"),
            "restyle_note": s.get("restyle_note") or None,
        }
        # With "approve_keyframes", a restyling render runs in two steps: redraw (and check) the keyframes, stop
        # with status "approve" for the user to look at them, then render the video from them once approved.
        style = animate.resolve_style({"style": s.get("style"), "custom_style": s.get("custom_style")})
        restyles = s.get("restyle") if s.get("restyle") is not None else bool(style.get("restyle_prompt"))
        if restyles and s.get("approve_keyframes"):
            if job.get("approved") and job.get("styled"):
                overrides["keyframes"] = [os.path.join(out_dir, "result_keyframes", n) for n in job["styled"]]
                overrides["restyle"] = False
            else:
                overrides["stop_after_restyle"] = True

        def report(text, final=False):
            with self.lock:
                job["progress"] = text
                if final:
                    job["log"] = (job.get("log") or [])[-30:] + [text]
                    self.save(job)

        self.update(job, status="running", started=time.time(), progress="starting")
        keep_awake(True)
        try:
            overrides["server"] = self.gpu.ensure(report, lambda: job.get("cancel", False))
            cfg = animate.make_config(overrides, out_dir)
            step = animate.run(cfg, report, lambda: job.get("cancel", False))
            styled_dir = os.path.join(out_dir, "result_keyframes")
            styled = sorted(n for n in os.listdir(styled_dir) if len(n) == 6 and n.endswith(".png")) if os.path.isdir(styled_dir) else []
            if step == "restyled":
                self.update(job, status="approve", progress="waiting for your OK on the redrawn keyframes", styled=styled)
            else:
                self.update(job, status="done", finished=time.time(), progress="done", styled=styled)
        except animate.Cancelled:
            self.update(job, status="cancelled", finished=time.time(), progress="cancelled")
        except Exception as e:  # report any failure to the phone
            self.update(job, status="failed", finished=time.time(), error=str(e)[-2000:])
        finally:
            keep_awake(False)


class KeyframeMaker:
    """Single images for the Keyframes tab: text -> keyframe 1, then edits of the previous keyframe.

    Each request is a short task polled by the phone. Tasks go to the front of
    ComfyUI's queue, so they only wait for a video segment that is already rendering.
    """

    def __init__(self, gpu):
        self.gpu = gpu
        self.tasks = {}
        self.lock = threading.Lock()
        os.makedirs(KEYFRAMES_DIR, exist_ok=True)

    @staticmethod
    def path(image_id):
        if not image_id or not all(c in "0123456789abcdef" for c in image_id) or len(image_id) != 16:
            raise ValueError("bad image id")
        return os.path.join(KEYFRAMES_DIR, image_id + ".png")

    def exists(self, image_id):
        """Is this library image still there? (False for a malformed id too.)"""
        try:
            return os.path.exists(self.path(image_id))
        except ValueError:
            return False

    def save_meta(self, image_id, **meta):
        """Sidecar JSON next to the PNG, so every device's library shows the prompt, model and seed."""
        meta = {"id": image_id, "created": time.time(), **meta}
        try:
            from PIL import Image
            with Image.open(self.path(image_id)) as im:
                meta["width"], meta["height"] = im.size
        except Exception:
            pass
        with open(self.path(image_id)[:-4] + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)

    def list(self, limit=300):
        items = []
        for name in os.listdir(KEYFRAMES_DIR):
            if not name.endswith(".png"):
                continue
            image_id, path = name[:-4], os.path.join(KEYFRAMES_DIR, name)
            meta = {"id": image_id, "created": os.path.getmtime(path), "kind": "image"}
            try:
                with open(path[:-4] + ".json", encoding="utf-8") as f:
                    meta.update(json.load(f))
            except (OSError, ValueError):
                pass  # images made before metadata existed
            items.append(meta)
        items.sort(key=lambda m: m["created"], reverse=True)
        return items[:limit]

    def delete(self, image_id):
        path = self.path(image_id)
        for p in (path, path[:-4] + ".json"):
            if os.path.exists(p):
                os.remove(p)

    def start(self, prompt, aspect, source_id=None, seed=None, model="klein4b", cast=(), style=None):
        """cast: characters (from Characters.get) whose first reference image goes to the model.
        style: a style id, to redraw source_id in that style (prompt is then the style's restyle prompt)."""
        if aspect not in animate.KEYFRAME_SIZES:
            raise ValueError("bad aspect")
        if model not in animate.IMAGE_MODELS:
            raise ValueError("unknown image model")
        source = self.path(source_id) if source_id else None
        if source and not os.path.exists(source):
            raise ValueError("previous keyframe not found on the laptop; start over")
        task_id = uuid.uuid4().hex[:12]
        task = {"id": task_id, "status": "running", "progress": "waiting for the GPU", "started": time.time()}
        with self.lock:
            self.tasks[task_id] = task
        threading.Thread(target=self._run, args=(task, prompt, aspect, source, seed, model, list(cast), style), daemon=True).start()
        return task

    @staticmethod
    def cast_prompt(prompt, cast, first_image):
        """Name each character's reference image, so the model knows who is who."""
        if not cast:
            return prompt
        who = "; ".join(f"{c['name']} is the character in image {first_image + i}"
                        + (f" ({c['description']})" if c["description"] else "") for i, c in enumerate(cast))
        return (f"{prompt.strip().rstrip('.')}. {who}. Keep each character's face, hair, body and outfit "
                "exactly as in their reference image.")

    def _run(self, task, prompt, aspect, source, seed, model, cast, style=None):
        image_id = uuid.uuid4().hex[:16]

        def report(text, final=False):
            task["progress"] = text

        try:
            server = self.gpu.ensure(report)
            refs = [self.path(c["refs"][0]) for c in cast]
            used = animate.make_keyframe(server, self.cast_prompt(prompt, cast, 2 if source else 1), self.path(image_id),
                                         seed, aspect, source, report, model, refs, keep_look=not style)
            self.save_meta(image_id, kind="style" if style else "edit" if source else "text", prompt=prompt, model=model,
                           style=style,
                           aspect=None if source else aspect, seed=used,
                           source=os.path.basename(source)[:-4] if source else None,
                           characters=[{"id": c["id"], "name": c["name"]} for c in cast])
            task.update(status="done", image_id=image_id, seed=used)
        except Exception as e:
            task.update(status="failed", error=str(e)[-1500:])

    def get(self, task_id):
        return self.tasks.get(task_id)

    def busy(self):
        return any(t["status"] == "running" for t in list(self.tasks.values()))


class Characters:
    """Named characters for consistent keyframes: output/characters/<id>.json with a name, a short
    description and library image ids. The first image is the one passed to the image model."""

    def __init__(self, maker):
        self.maker = maker
        self.lock = threading.Lock()
        os.makedirs(CHARACTERS_DIR, exist_ok=True)

    @staticmethod
    def path(char_id):
        if not char_id or len(char_id) != 12 or not all(c in "0123456789abcdef" for c in char_id):
            raise ValueError("bad character id")
        return os.path.join(CHARACTERS_DIR, char_id + ".json")

    def get(self, char_id):
        """The character, with images deleted from the library left out; None if there is none."""
        try:
            with open(self.path(char_id), encoding="utf-8") as f:
                char = json.load(f)
        except (OSError, ValueError):
            return None
        char["refs"] = [r for r in char.get("refs", []) if self.maker.exists(r)]
        return char

    def list(self):
        items = [self.get(name[:-5]) for name in os.listdir(CHARACTERS_DIR) if name.endswith(".json")]
        return sorted((c for c in items if c), key=lambda c: c["name"].lower())

    def save(self, raw, char_id=None):
        name = str(raw.get("name") or "").strip()[:60]
        if not name:
            raise ValueError("give the character a name")
        refs = list(dict.fromkeys(str(r) for r in (raw.get("refs") or [])))
        if not refs:
            raise ValueError("add at least one reference image")
        if len(refs) > MAX_CHARACTER_REFS:
            raise ValueError(f"up to {MAX_CHARACTER_REFS} reference images per character")
        if not all(self.maker.exists(r) for r in refs):
            raise ValueError("a reference image is no longer in the library")
        with self.lock:
            old = self.get(char_id) if char_id else None
            if char_id and not old:
                raise KeyError("no such character")
            char = {"id": char_id or uuid.uuid4().hex[:12], "name": name,
                    "description": str(raw.get("description") or "").strip()[:400], "refs": refs,
                    "created": old["created"] if old else time.time(), "updated": time.time()}
            tmp = self.path(char["id"]) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(char, f, indent=1)
            os.replace(tmp, self.path(char["id"]))
        return char

    def delete(self, char_id):
        path = self.path(char_id)
        if os.path.exists(path):
            os.remove(path)

    def cast(self, ids):
        """Characters for one keyframe, in the order chosen."""
        ids = list(dict.fromkeys(str(i) for i in (ids or [])))
        if len(ids) > MAX_CAST:
            raise ValueError(f"up to {MAX_CAST} characters per image")
        cast = []
        for char_id in ids:
            char = self.get(char_id)
            if not char:
                raise ValueError("a chosen character no longer exists")
            if not char["refs"]:
                raise ValueError(f"{char['name']} has no reference image left in the library")
            cast.append(char)
        return cast


class IdleStop:
    """Stops the rented GPU after a while with no work and no user action, so a forgotten pod stops billing.

    Work = a queued/running video job, a running image task, or audio being generated or dubbed. A user action = any API call that
    changes something (POST/DELETE), including "Keep running" in the app. Open tabs only poll with
    GET, so they don't keep the pod alive by themselves.
    """

    def __init__(self, minutes, jobs, maker, sound):
        self.seconds = minutes * 60
        self.jobs, self.maker, self.sound = jobs, maker, sound
        self.last = time.time()
        self.pod_id, self.api_key = os.environ.get("RUNPOD_POD_ID"), os.environ.get("RUNPOD_API_KEY")
        self.command = os.environ.get("IDLE_STOP_COMMAND")  # other hosts, e.g. "vastai stop instance 123"
        self.enabled = bool(self.seconds and (self.command or (self.pod_id and self.api_key)))
        if self.enabled:
            threading.Thread(target=self._watch, daemon=True).start()

    def touch(self):
        self.last = time.time()

    def busy(self):
        return self.jobs.busy() or self.maker.busy() or self.sound.busy()

    def seconds_left(self):
        if not self.enabled:
            return None
        if self.busy():
            self.touch()
        return max(0, self.seconds - (time.time() - self.last))

    def _stop(self):
        if self.command:
            subprocess.run(self.command, shell=True, check=True, timeout=120)
            return
        req = urllib.request.Request(f"https://api.runpod.io/v2/pods/{self.pod_id}/action", method="POST",
                                     data=json.dumps({"action": "stop"}).encode(),
                                     headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60).close()

    def _watch(self):
        while True:
            time.sleep(30)
            if self.seconds_left() > 0:
                continue
            print(time.strftime("%H:%M:%S"), f"Idle for {self.seconds // 60} min: stopping the pod.", flush=True)
            try:
                self._stop()
            except Exception as e:  # keep serving and try again after another idle period
                print("Could not stop the pod:", e, flush=True)
            self.touch()  # don't repeat the request while the pod shuts down


def keep_awake(on):
    """Stop Windows from sleeping mid-render (e.g. when the lid timer kicks in)."""
    if os.name != "nt":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


# ---------------------------------------------------------------- input handling

def save_keyframe(data, dest):
    """Normalise a phone photo: apply EXIF rotation, drop alpha, cap the size, save as PNG."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        im.save(dest, "PNG")


def clean_settings(raw, n_keyframes, styles):
    loop = bool(raw.get("loop"))
    n_segments = n_keyframes - (0 if loop else 1)
    prompts = [str(p).strip() for p in (raw.get("prompts") or [])][:n_segments]
    prompts += [""] * (n_segments - len(prompts))
    last = "smooth natural motion, consistent subject"
    for i, p in enumerate(prompts):  # an empty box repeats the previous segment's prompt
        prompts[i] = last = p or last

    custom = str(raw.get("custom_style") or "").strip()[:300]
    style = raw.get("style") or "photoreal"
    if not custom and style not in styles:
        raise ValueError(f"unknown style {style}")
    engine = raw.get("engine") or "minimax-h3"
    if engine not in animate.ENGINES:
        raise ValueError(f"unknown engine {engine}")
    image_model = raw.get("image_model") or "klein4b"
    if image_model not in animate.IMAGE_MODELS:
        raise ValueError(f"unknown image model {image_model}")
    restyle = raw.get("restyle")
    seed = raw.get("seed")
    return {
        "prompts": prompts,
        "engine": engine,
        "image_model": image_model,
        "audio": bool(raw.get("audio", True)),
        "sound": str(raw.get("sound") or "").strip()[:500],
        "style": style,
        "custom_style": custom,
        "restyle": None if restyle is None else bool(restyle),
        "fast": bool(raw.get("fast", True)),
        "turbo_steps": 4 if str(raw.get("turbo_steps")) == "4" else 8,
        "check_keyframes": bool(raw.get("check_keyframes")),
        "approve_keyframes": bool(raw.get("approve_keyframes")),  # stop for the user's OK after the redraw
        "writer": str(raw.get("writer") or "")[:40],  # the AI model that checks redrawn keyframes
        "length": int(raw.get("length", 81)) if int(raw.get("length", 81)) in LENGTHS else 81,
        "interpolate": 2 if raw.get("smooth", True) else 1,
        "size": raw.get("size") if raw.get("size") in SIZES else "standard",
        "loop": loop,
        "seed": int(seed) if str(seed or "").strip().isdigit() else None,
    }


def queue_job(jobs, files, raw, extra=None):
    """Queue a video render: files are 2-12 keyframes (image bytes or paths), raw the settings from the app,
    extra more fields for the job record (set before it's queued: the worker may start on it at once)."""
    if not 2 <= len(files) <= MAX_KEYFRAMES:
        raise ValueError(f"send 2 to {MAX_KEYFRAMES} keyframes")
    settings = clean_settings(raw, len(files), animate.load_styles())
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    in_dir = os.path.join(jobs.dir(job_id), "inputs")
    os.makedirs(in_dir)
    names = []
    try:
        for i, data in enumerate(files):
            name = f"{i + 1:02d}.png"
            save_keyframe(data if isinstance(data, bytes) else _read_bytes(data), os.path.join(in_dir, name))
            names.append(name)
    except Exception as e:
        shutil.rmtree(jobs.dir(job_id), ignore_errors=True)
        raise ValueError(f"could not read image {len(names) + 1}: {e}") from None
    job = {"id": job_id, "created": time.time(), "status": "queued", "progress": "queued",
           "settings": settings, "inputs": names, "styled": [], "log": [], **(extra or {})}
    jobs.add(job)
    return job


def keyframe_checker(jobs, writers):
    """job -> a check for animate.run's keyframe redraw, using the job's chosen AI model (or the first one that is
    set up); each result is recorded in job["keyframe_checks"] for the app."""
    def for_job(job):
        writer = writers.get(job["settings"].get("writer")) or next((w for w in writers.values() if w.ready), None)
        if not writer or not writer.ready:
            return None
        s = job["settings"]
        style = animate.resolve_style({"style": s.get("style"), "custom_style": s.get("custom_style")})

        def check(originals, redrawn):
            result = clipreview.check_keyframes(writer, originals, redrawn,
                                                {"label": style.get("label", "?"), "description": style.get("video_prompt", "")})
            result = {**result, "writer": writer.id, "time": time.time()}
            jobs.update(job, keyframe_checks=(job.get("keyframe_checks") or []) + [result])
            return result
        return check
    return for_job


def job_keyframes(jobs, job):
    """The keyframes a render actually used: the redrawn ones when it restyled them, else its inputs."""
    names, folder = (job["styled"], "result_keyframes") if job.get("styled") else (job["inputs"], "inputs")
    return [os.path.join(jobs.dir(job["id"]), folder, n) for n in names]


def review_job(jobs, job, writer):
    """Have a writer review a finished clip in the background; the result goes to job["review"]."""
    if job["status"] != "done" or job.get("kind") == "film":
        raise ValueError("only a finished clip can be checked")
    if (job.get("review") or {}).get("status") == "working":
        raise ValueError("this clip is already being checked")
    jobs.update(job, review={"status": "working", "writer": writer.id, "started": time.time()})

    def run():
        keys = job_keyframes(jobs, job)
        settings = job["settings"]
        originals = [os.path.join(jobs.dir(job["id"]), "inputs", n) for n in job["inputs"]] if job.get("styled") else []
        try:
            style = animate.resolve_style({"style": settings.get("style"), "custom_style": settings.get("custom_style")})
            result = clipreview.review(writer, os.path.join(jobs.dir(job["id"]), "result.mp4"), [keys[0], keys[-1]],
                                       " / then: ".join(settings["prompts"]),
                                       {"label": style.get("label", "?"), "description": style.get("video_prompt", "")},
                                       [originals[0], originals[-1]] if originals else ())
            jobs.update(job, review={**result, "status": "done", "writer": writer.id, "finished": time.time()})
        except Exception as e:  # report any failure in the app
            jobs.update(job, review={"status": "failed", "writer": writer.id, "error": str(e)[-800:]})
    threading.Thread(target=run, daemon=True).start()


def rerender_job(jobs, job, prompts, keep_styled=False):
    """Render a job again with new motion prompts, from its original keyframes and with its settings, so a
    restyle runs again too (a bad restyle is often what the clip check found). keep_styled reuses the
    restyled keyframes instead and skips the restyle, so only the prompt changes."""
    if job.get("kind") == "film":
        raise ValueError("a film can't be rendered again; render its shots in the Director")
    prompts = [str(p).strip()[:1500] for p in prompts][:len(job["settings"]["prompts"])]
    if not prompts or not all(prompts):
        raise ValueError("write the motion prompt")
    prompts += job["settings"]["prompts"][len(prompts):]
    reuse = keep_styled and bool(job.get("styled"))
    keyframes = job_keyframes(jobs, job) if reuse else [os.path.join(jobs.dir(job["id"]), "inputs", n) for n in job["inputs"]]
    settings = dict(job["settings"], prompts=prompts, seed=None, restyle=False if reuse else job["settings"].get("restyle"))
    return queue_job(jobs, keyframes, {"smooth": settings.get("interpolate", 2) > 1, "length": settings["length"],
                                       "size": settings["size"], "fast": settings["fast"], **settings},
                     extra={"retry_of": job["id"]})


def add_film_job(jobs, make_video, keyframes, info):
    """Record a finished film as a done job, so it shows in the library with Sound and Download.
    make_video(path) writes the video; keyframes are image paths (shown as the job's inputs);
    info: title, prompts (one line per shot) and narration ([{start, text}])."""
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    in_dir = os.path.join(jobs.dir(job_id), "inputs")
    os.makedirs(in_dir)
    try:
        names = []
        for i, path in enumerate(keyframes):
            names.append(f"{i + 1:02d}.png")
            shutil.copyfile(path, os.path.join(in_dir, names[-1]))
        make_video(os.path.join(jobs.dir(job_id), "result.mp4"))
    except Exception:
        shutil.rmtree(jobs.dir(job_id), ignore_errors=True)
        raise
    now = time.time()
    job = {"id": job_id, "kind": "film", "title": info["title"], "narration": info.get("narration", []),
           "created": now, "started": now, "finished": now, "status": "done", "progress": "done",
           "settings": {"prompts": info["prompts"], "engine": info.get("engine", "wan22"), "style": "photoreal", "custom_style": "",
                        "fast": info.get("fast", True), "loop": False, "seed": None},
           "inputs": names, "styled": [], "log": []}
    jobs.add(job)  # the worker skips jobs that aren't queued
    return job


# ---------------------------------------------------------------- web app

def make_app(jobs, token, maker, idle, sound, gpu, characters, director):
    @web.middleware
    async def auth(request, handler):
        if request.path.startswith("/api/"):
            given = request.headers.get("X-Token") or request.query.get("t", "")
            if not hmac.compare_digest(given.encode(), token.encode()):
                return web.json_response({"error": "bad or missing token"}, status=401)
            if request.method != "GET":
                idle.touch()
                gpu.touch()
        response = await handler(request)
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # pick up app updates after a git pull
        return response

    def get_job(request):
        job = jobs.jobs.get(request.match_info["id"])
        if not job:
            raise web.HTTPNotFound()
        return job

    async def index(request):
        return web.FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})

    async def classic(request):
        return web.FileResponse(os.path.join(STATIC, "classic.html"), headers={"Cache-Control": "no-cache"})

    async def info(request):
        styles = animate.load_styles()
        have = model_checker(gpu)
        turbo4 = animate.DEFAULTS["models"]["h3_lora_4step"]
        h3_missing = [f"{sub}/{animate.DEFAULTS['models'][key]}"
                      for key, sub in (("h3_unet", "diffusion_models"), ("h3_text_encoder", "text_encoders"),
                                       ("h3_vae", "vae"), ("h3_audio_vae", "vae"), ("h3_lora", "loras"))
                      if not have(sub, animate.DEFAULTS["models"][key])]
        comfy = {"online": False}
        gpu_state = gpu.status()
        try:
            if gpu.remote and gpu_state["state"] != "ready":
                raise ConnectionError("GPU pod not running")
            stats = await asyncio.to_thread(_fetch_json, gpu.url() + "/system_stats", gpu.headers)
            dev = (stats.get("devices") or [{}])[0]
            comfy = {"online": True, "gpu": dev.get("name", ""), "vram_total": dev.get("vram_total"),
                     "vram_free": dev.get("vram_free"), "ram_total": stats.get("system", {}).get("ram_total"),
                     "ram_free": stats.get("system", {}).get("ram_free")}
        except Exception:
            pass
        left = idle.seconds_left()
        return web.json_response({
            "styles": [{"id": k, "label": v.get("label", k), "restyle": bool(v.get("restyle_prompt")), "group": v.get("group", "Other"),
                        "look": v.get("video_prompt", ""),  # searched on the Styles page
                        # a pre-rendered example (static/styles/<id>.jpg), made with setup/style_examples.py
                        "example": f"/static/styles/{k}.jpg" if os.path.exists(os.path.join(STATIC, "styles", f"{k}.jpg")) else None}
                       for k, v in styles.items()],
            "comfy": comfy,
            "image_models": [{"id": k, "label": v["label"], "license": v["license"],
                              "missing": animate.missing_image_model_files(MODELS_DIR, k, have)}
                             for k, v in animate.IMAGE_MODELS.items()],
            "engines": [{"id": "minimax-h3", "label": "MiniMax H3 + sound", "license": animate.LICENSES["minimax-h3"],
                         "ready": not h3_missing, "missing": h3_missing,
                         "turbo4": have("loras", turbo4), "turbo_license": animate.LICENSES["h3-turbo"]},
                        {"id": "wan22", "label": "Wan 2.2 (silent)", "license": animate.LICENSES["wan22"], "ready": True}],
            "queued": sum(1 for j in jobs.jobs.values() if j["status"] == "queued"),
            "running": any(j["status"] == "running" for j in jobs.jobs.values()),
            "elevenlabs": sound.info(),
            "writers": [{"id": w.id, "label": w.label, "model": w.model, "paid": w.paid, "ready": w.ready}
                        for w in director.writers.values()],
            "idle_stop": None if left is None else {"minutes": idle.seconds // 60, "seconds_left": round(left)},
            "gpu": gpu_state,
        })

    async def keepalive(request):
        return web.json_response({"ok": True})  # the middleware already reset the idle timers

    async def gpu_start(request):
        gpu.start_now()
        return web.json_response(gpu.status())

    async def gpu_stop(request):
        if jobs.busy() or maker.busy():
            return web.json_response({"error": "something is still rendering; cancel it first"}, status=409)
        gpu.stop_now()
        return web.json_response({"ok": True})

    async def list_jobs(request):
        items = sorted(jobs.jobs.values(), key=lambda j: j["created"], reverse=True)
        return web.json_response([jobs.public(j) for j in items[:50]])

    async def create_job(request):
        reader = await request.multipart()
        files, raw = [], {}
        async for part in reader:
            if part.name == "keyframes":
                files.append(await part.read(decode=False))
            elif part.name == "keyframe_id":  # an image already in the library, in upload order
                try:
                    path = maker.path((await part.text()).strip())
                except ValueError:
                    return web.json_response({"error": "bad keyframe id"}, status=400)
                if not os.path.exists(path):
                    return web.json_response({"error": "a chosen keyframe is no longer in the library"}, status=400)
                files.append(await asyncio.to_thread(_read_bytes, path))
            elif part.name == "settings":
                raw = json.loads(await part.text())
        try:
            job = await asyncio.to_thread(queue_job, jobs, files, raw)
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(jobs.public(job))

    async def job_detail(request):
        return web.json_response(jobs.public(get_job(request)))

    async def cancel_job(request):
        job = get_job(request)
        if job["status"] in ("queued", "approve"):
            jobs.update(job, status="cancelled", progress="cancelled")
        elif job["status"] == "running":
            job["cancel"] = True
        return web.json_response(jobs.public(job))

    async def approve_keyframes(request):
        """A render waiting for the user's OK on its redrawn keyframes: render the video, or redraw them again."""
        job, raw = get_job(request), await request.json()
        if job["status"] != "approve":
            return web.json_response({"error": "this render isn't waiting for an OK"}, status=400)
        if raw.get("action") == "redraw":
            shutil.rmtree(os.path.join(jobs.dir(job["id"]), "result_keyframes"), ignore_errors=True)
            note = str(raw.get("note") or "").strip()[:600]
            jobs.update(job, status="queued", progress="queued", styled=[], keyframe_checks=[], approved=False,
                        settings=dict(job["settings"], restyle_note=note))
        else:
            jobs.update(job, status="queued", progress="queued", approved=True)
        jobs.pending.put(job["id"])
        return web.json_response(jobs.public(job))

    async def check_clip(request):
        job, raw = get_job(request), await request.json()
        writer = director.writers.get(raw.get("writer"))
        if not writer or not writer.ready:
            return web.json_response({"error": "choose an AI model that is set up (see docs/director.md)"}, status=400)
        try:
            review_job(jobs, job, writer)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(jobs.public(job))

    async def rerender(request):
        job, raw = get_job(request), await request.json()
        try:
            new = await asyncio.to_thread(rerender_job, jobs, job, raw.get("prompts") or [], bool(raw.get("keep_styled")))
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(jobs.public(new))

    async def delete_job(request):
        job = get_job(request)
        if job["status"] == "running":
            return web.json_response({"error": "cancel it first"}, status=409)
        jobs.delete(job["id"])
        return web.json_response({"ok": True})

    async def video(request):
        """The render; ?version=mix for it with its soundtrack, ?version=<language> for a dubbed mix."""
        job = get_job(request)
        version = request.query.get("version", "original")
        if version != "original" and version != "mix" and version not in soundtrack.LANGUAGES:
            raise web.HTTPNotFound()
        path = sound.video_path(job, version)
        if not os.path.exists(path):
            raise web.HTTPNotFound()
        headers = {}
        if request.query.get("download"):
            suffix = "" if version == "original" else "-sound" if version == "mix" else f"-{version}"
            headers["Content-Disposition"] = f'attachment; filename="animation-{job["id"]}{suffix}.mp4"'
        return web.FileResponse(path, headers=headers)

    # ---- soundtrack (ElevenLabs, opt-in)

    async def sound_call(fn, *args):
        try:
            return web.json_response(await asyncio.to_thread(fn, *args))
        except (ValueError, TypeError, KeyError) as e:
            return web.json_response({"error": str(e).strip("'")}, status=400)
        except RuntimeError as e:  # ElevenLabs or ffmpeg
            return web.json_response({"error": str(e)}, status=502)

    async def voices(request):
        if not sound.enabled:
            return web.json_response({"error": "ElevenLabs is off: set ELEVENLABS_API_KEY on the GPU machine"}, status=400)
        return await sound_call(sound.voices)

    async def add_track(request):
        return await sound_call(sound.add, get_job(request), await request.json())

    async def update_track(request):
        job = get_job(request)
        return await sound_call(lambda raw: sound.update(job, request.match_info["track"], raw) or {"ok": True},
                                await request.json())

    async def delete_track(request):
        job = get_job(request)
        return await sound_call(lambda: sound.remove(job, request.match_info["track"]) or {"ok": True})

    async def add_dub(request):
        return await sound_call(sound.dub, get_job(request), (await request.json()).get("language"))

    async def delete_dub(request):
        job = get_job(request)
        return await sound_call(lambda: sound.remove_dub(job, request.match_info["lang"]) or {"ok": True})

    async def audio_file(request):
        path = sound.file(get_job(request), request.match_info["name"])
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def image(request):
        job = get_job(request)
        kind, name = request.match_info["kind"], os.path.basename(request.match_info["name"])
        folder = {"input": "inputs", "styled": "result_keyframes"}.get(kind)
        path = os.path.join(jobs.dir(job["id"]), folder or "", name)
        if not folder or not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def keyframe_generate(request):
        data = await request.json()
        prompt = str(data.get("prompt") or "").strip()[:1500]
        if not prompt:
            return web.json_response({"error": "describe the keyframe"}, status=400)
        seed = data.get("seed")
        model = data.get("model") or "klein4b"
        if model in animate.IMAGE_MODELS and animate.missing_image_model_files(MODELS_DIR, model, model_checker(gpu)):
            return web.json_response({"error": f"{animate.IMAGE_MODELS[model]['label']} isn't installed on the GPU. "
                                               "Choose another model, or download it (README, Model licenses)."}, status=400)
        try:
            task = maker.start(prompt, data.get("aspect", "16:9"), data.get("source") or None,
                               int(seed) if str(seed or "").isdigit() else None, data.get("model") or "klein4b",
                               characters.cast(data.get("characters")))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(task)

    async def try_styles(request):
        """Redraw one library image in several styles, to compare them: one image task per style."""
        data = await request.json()
        styles = animate.load_styles()
        chosen = [k for k in dict.fromkeys(data.get("styles") or []) if styles.get(k, {}).get("restyle_prompt")]
        if not chosen:
            return web.json_response({"error": "choose at least one style that redraws images"}, status=400)
        model = data.get("model") or "klein4b"
        if model in animate.IMAGE_MODELS and animate.missing_image_model_files(MODELS_DIR, model, model_checker(gpu)):
            return web.json_response({"error": f"{animate.IMAGE_MODELS[model]['label']} isn't installed on the GPU"}, status=400)
        seed = data.get("seed")
        try:
            tasks = [{"style": k, "task": maker.start(styles[k]["restyle_prompt"], "16:9", data.get("image_id"),
                                                      int(seed) if str(seed or "").isdigit() else None, model, style=k)}
                     for k in chosen]
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(tasks)

    async def keyframe_task(request):
        task = maker.get(request.match_info["id"])
        if not task:
            raise web.HTTPNotFound()
        return web.json_response(task)

    async def keyframe_upload(request):
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "image":
            return web.json_response({"error": "send an image"}, status=400)
        data = await part.read(decode=False)
        image_id = uuid.uuid4().hex[:16]
        try:
            await asyncio.to_thread(save_keyframe, data, maker.path(image_id))
            maker.save_meta(image_id, kind="upload", prompt="", name=os.path.basename(part.filename or "")[:120])
        except Exception as e:
            return web.json_response({"error": f"could not read the image: {e}"}, status=400)
        return web.json_response({"image_id": image_id})

    async def list_images(request):
        return web.json_response(await asyncio.to_thread(maker.list))

    async def delete_image(request):
        try:
            maker.delete(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    # ---- characters

    async def list_characters(request):
        return web.json_response(await asyncio.to_thread(characters.list))

    async def save_character(request):
        try:
            char = await asyncio.to_thread(characters.save, await request.json(), request.match_info.get("id"))
        except (ValueError, TypeError, AttributeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except KeyError:
            raise web.HTTPNotFound()
        return web.json_response(char)

    async def delete_character(request):
        try:
            characters.delete(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    # ---- director

    async def director_call(fn, *args):
        try:
            result = await asyncio.to_thread(fn, *args)
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except KeyError:
            raise web.HTTPNotFound()
        return web.json_response(result if result is not None else {"ok": True})

    def project_id(request):
        try:
            director.path(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        return request.match_info["id"]

    async def list_projects(request):
        return await director_call(director.list)

    async def create_project(request):
        return await director_call(director.create, await request.json())

    def found(project):
        if project is None:
            raise KeyError("no such project")
        return project

    async def get_project(request):
        pid = project_id(request)
        return await director_call(lambda: found(director.get(pid)))

    async def edit_project(request):
        pid = project_id(request)
        raw = await request.json()
        return await director_call(lambda: director.edit(pid, raw) and director.get(pid))

    async def delete_project(request):
        return await director_call(director.delete, project_id(request))

    async def write_script(request):
        pid, raw = project_id(request), await request.json()
        task = raw.get("task") if raw.get("task") in ("check", "board") else "write"
        return await director_call(lambda: director.write(pid, raw.get("instruction") or "", raw.get("writer"), task)
                                   or director.get(pid))

    async def draw_board(request):
        pid, raw = project_id(request), await request.json()
        keys = [str(k) for k in raw.get("keys") or []][:20] or None
        return await director_call(lambda: director.draw_board(pid, keys) or director.get(pid))

    async def shot_keyframes(request):
        pid, sid = project_id(request), request.match_info["shot"]
        keep_start = request.query.get("keep_start") == "1"
        return await director_call(lambda: director.make_keyframes(pid, sid, keep_start) or director.get(pid))

    async def shot_render(request):
        pid, sid = project_id(request), request.match_info["shot"]
        return await director_call(lambda: director.render(pid, sid) and director.get(pid))

    async def make_film(request):
        pid = project_id(request)
        return await director_call(lambda: director.make_film(pid) or director.get(pid))

    async def film(request):
        path = director.film_path(director.get(project_id(request)))
        if not path:
            raise web.HTTPNotFound()
        headers = {"Content-Disposition": 'attachment; filename="film.mp4"'} if request.query.get("download") else {}
        return web.FileResponse(path, headers=headers)

    async def keyframe_image(request):
        try:
            path = maker.path(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        if not os.path.exists(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    app = web.Application(middlewares=[auth], client_max_size=300 * 1024 * 1024)
    app.add_routes([
        web.get("/", index),
        web.get("/classic", classic),
        web.get("/api/info", info),
        web.post("/api/keepalive", keepalive),
        web.post("/api/gpu/start", gpu_start),
        web.post("/api/gpu/stop", gpu_stop),
        web.get("/api/jobs", list_jobs),
        web.post("/api/jobs", create_job),
        web.get("/api/jobs/{id}", job_detail),
        web.post("/api/jobs/{id}/cancel", cancel_job),
        web.delete("/api/jobs/{id}", delete_job),
        web.post("/api/jobs/{id}/review", check_clip),
        web.post("/api/jobs/{id}/approve", approve_keyframes),
        web.post("/api/jobs/{id}/rerender", rerender),
        web.get("/api/jobs/{id}/video", video),
        web.get("/api/voices", voices),
        web.post("/api/jobs/{id}/audio", add_track),
        web.patch("/api/jobs/{id}/audio/{track}", update_track),
        web.delete("/api/jobs/{id}/audio/{track}", delete_track),
        web.get("/api/jobs/{id}/audio/{name}", audio_file),
        web.post("/api/jobs/{id}/dubs", add_dub),
        web.delete("/api/jobs/{id}/dubs/{lang}", delete_dub),
        web.get("/api/jobs/{id}/image/{kind}/{name}", image),
        web.post("/api/keyframes/generate", keyframe_generate),
        web.post("/api/styles/try", try_styles),
        web.get("/api/keyframes/tasks/{id}", keyframe_task),
        web.post("/api/keyframes/upload", keyframe_upload),
        web.get("/api/keyframes/image/{id}", keyframe_image),
        web.get("/api/images", list_images),
        web.delete("/api/images/{id}", delete_image),
        web.get("/api/characters", list_characters),
        web.post("/api/characters", save_character),
        web.put("/api/characters/{id}", save_character),
        web.delete("/api/characters/{id}", delete_character),
        web.get("/api/director", list_projects),
        web.post("/api/director", create_project),
        web.get("/api/director/{id}", get_project),
        web.put("/api/director/{id}", edit_project),
        web.delete("/api/director/{id}", delete_project),
        web.post("/api/director/{id}/write", write_script),
        web.post("/api/director/{id}/board/draw", draw_board),
        web.post("/api/director/{id}/shots/{shot}/keyframes", shot_keyframes),
        web.post("/api/director/{id}/shots/{shot}/render", shot_render),
        web.post("/api/director/{id}/film", make_film),
        web.get("/api/director/{id}/film", film),
        web.static("/static", STATIC),
    ])
    return app


def model_checker(gpu):
    """have(subfolder, filename): is this model file on the GPU? A remote pod reports its files once it has
    run; until then every model counts as present."""
    remote_models = gpu.models()

    def have(sub, name):
        if gpu.remote:
            return remote_models is None or name in remote_models.get(sub, [])
        return os.path.exists(os.path.join(MODELS_DIR, sub, name))
    return have


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _fetch_json(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=10) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------- startup

def make_gpu(config, comfy_url, busy):
    """ComfyUI on this machine, or (with a "gpu" section in config.json) on a Runpod pod made on demand."""
    cfg = dict(config.get("gpu") or {})
    if not cfg:
        return gpus.LocalGPU(comfy_url)
    cfg["api_key"] = os.environ.get("RUNPOD_API_KEY") or cfg.get("api_key")
    os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
    return gpus.RunpodGPU(cfg, os.path.join(ROOT, "output", "gpu_pod.json"), busy,
                          os.path.join(ROOT, "output", "gpu_sessions.jsonl"))


def load_config():
    if not os.path.exists(CONFIG):
        return {}
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def load_token(regenerate):
    cfg = load_config()
    if regenerate or not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(18)
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
    return cfg["token"]


def local_addresses():
    addrs = set()
    try:  # the address of the interface used for outbound traffic (no packets are sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    addrs.discard("127.0.0.1")
    # Tailscale addresses (100.64.0.0/10) first: they also work away from home.
    return sorted(addrs, key=lambda a: (not a.startswith("100."), a))


def default_public_url(port):
    """Public HTTPS address when running on a cloud GPU host, or None on a local machine."""
    if os.environ.get("PUBLIC_URL"):
        return os.environ["PUBLIC_URL"].rstrip("/")
    if os.environ.get("RUNPOD_POD_ID"):  # RunPod's HTTPS proxy for an exposed HTTP port
        return f"https://{os.environ['RUNPOD_POD_ID']}-{port}.proxy.runpod.net"
    return None


def print_links(port, token, public_url=None):
    links = [f"http://{a}:{port}/?t={token}" for a in local_addresses()] or [f"http://127.0.0.1:{port}/?t={token}"]
    if public_url:
        links.insert(0, f"{public_url}/?t={token}")
        print(f"\nOpen this on any device (phone, tablet, computer):\n  {links[0]}")
        print("Local network addresses (only reachable from inside the same network):")
        links_to_print = links[1:]
    else:
        print("\nOpen this on your phone or any device on the same Wi-Fi (or Tailscale):")
        links_to_print = links
    for link in links_to_print:
        print("  " + link)
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(links[0])
        qr.print_ascii(invert=True)
    except ImportError:
        print("(pip install qrcode to show a scannable QR code here)")
    print("Keep this window open. Ctrl+C to stop.\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0", help="interface to listen on (default: all)")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--comfy", default="http://127.0.0.1:8188", help="ComfyUI address")
    ap.add_argument("--new-token", action="store_true", help="invalidate old phone links")
    ap.add_argument("--public-url", help="public HTTPS address of this machine, e.g. a cloud GPU's proxy URL "
                                         "(auto-detected on RunPod; or set PUBLIC_URL)")
    ap.add_argument("--idle-stop", type=int, default=int(os.environ.get("IDLE_STOP_MINUTES", 30)), metavar="MIN",
                    help="on RunPod (or with IDLE_STOP_COMMAND set), stop the pod after MIN minutes with no work "
                         "and no user action; 0 turns it off (default: 30, or IDLE_STOP_MINUTES)")
    args = ap.parse_args()

    token = load_token(args.new_token)
    config = load_config()
    holder = {}
    gpu = make_gpu(config, args.comfy, lambda: bool(holder) and (holder["jobs"].busy() or holder["maker"].busy()))
    animate.Comfy.headers = gpu.headers
    jobs = Jobs(gpu)
    maker = KeyframeMaker(gpu)
    holder.update(jobs=jobs, maker=maker)
    sound = soundtrack.Soundtrack(jobs, os.environ.get("ELEVENLABS_API_KEY") or config.get("elevenlabs_api_key"))
    idle = IdleStop(args.idle_stop, jobs, maker, sound)
    print_links(args.port, token, args.public_url or config.get("public_url") or default_public_url(args.port))
    if gpu.remote:
        print(f"GPU: a Runpod pod is created when a job needs it and removed after {gpu.cfg['idle_minutes']} idle min.\n")
    if idle.enabled:
        print(f"Auto-stop: the pod stops after {args.idle_stop} min with no work and no user action "
              "(--idle-stop 0 turns this off).\n")
    elif os.environ.get("RUNPOD_POD_ID") and args.idle_stop:
        print("Auto-stop is OFF: RUNPOD_API_KEY is not set in this pod, so it can't stop itself. "
              "Remember to stop the pod when you're done.\n")
    print("ElevenLabs sound (paid per use): " + ("on.\n" if sound.enabled else
          "off. Set ELEVENLABS_API_KEY to turn it on.\n"))
    characters = Characters(maker)
    writers = directors.load_writers(config)
    jobs.keyframe_checker = keyframe_checker(jobs, writers)
    director = directors.Director(ROOT, writers, maker, characters, lambda files, raw: queue_job(jobs, files, raw), jobs,
                                  lambda make_video, keyframes, info: add_film_job(jobs, make_video, keyframes, info))
    print("Director AI: " + (", ".join(f"{w.label}{' (paid)' if w.paid else ''}{'' if w.ready else ' [not set up]'}"
                                         for w in writers.values()) or "none set up (see docs/director.md)") + "\n")
    web.run_app(make_app(jobs, token, maker, idle, sound, gpu, characters, director),
                host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()

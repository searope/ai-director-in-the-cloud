"""Soundtracks for finished videos: ElevenLabs audio layers mixed in with ffmpeg.

A finished video can get layers: voice-over, dialogue (a voice per speaker), sound effects and
music, each with its own start time and volume. They are mixed into a copy of the video (the render
itself never changes), so one layer can be redone without touching the others. Dubbing translates
the speech layers (voice-over and dialogue) into another language; effects and music carry over
into every language version.

Policy (see CLAUDE.md): ElevenLabs bills per use, so this is an owner-approved, opt-in exception.
It is off unless ELEVENLABS_API_KEY (or "elevenlabs_api_key" in remote/config.json) is set.
Commercial use of the audio needs a paid ElevenLabs plan.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

API = os.environ.get("ELEVENLABS_API_URL", "https://api.elevenlabs.io")  # or a data-residency host
LICENSE = "paid per use (your ElevenLabs credits); commercial use needs a paid ElevenLabs plan"
KINDS = {"voice": "Voice-over", "dialogue": "Dialogue", "sfx": "Sound effect", "music": "Music"}
SPEECH = ("voice", "dialogue")
# Dubbing targets offered in the app (BCP-47); ElevenLabs supports more.
LANGUAGES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
             "pt": "Portuguese", "pl": "Polish", "nl": "Dutch", "tr": "Turkish", "ru": "Russian",
             "uk": "Ukrainian", "ar": "Arabic", "hi": "Hindi", "ja": "Japanese", "ko": "Korean", "zh": "Chinese"}
MP3 = "mp3_44100_128"


# ---------------------------------------------------------------- ffmpeg

def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError("ffmpeg not found: run  .venv/bin/pip install imageio-ffmpeg") from None


def probe(path):
    """(duration in seconds or None, has an audio stream) from ffmpeg's banner."""
    err = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None
    return duration, bool(re.search(r"Stream #.*Audio:", err))


def mix(out, layers, duration, video=None, video_volume=1.0, video_has_audio=False):
    """Mix audio layers [(path, start_s, volume)] over `video` (or into a WAV when video is None)."""
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    graph, labels = [], []
    if video:
        cmd += ["-i", video]
        if video_has_audio and video_volume > 0:
            graph.append(f"[0:a]aresample=48000,volume={video_volume:.2f}[v0]")
            labels.append("[v0]")
    first = 1 if video else 0
    for i, (path, start, volume) in enumerate(layers, first):
        cmd += ["-i", path]
        graph.append(f"[{i}:a]aresample=48000,volume={volume:.2f},adelay={int(start * 1000)}:all=1[a{i}]")
        labels.append(f"[a{i}]")
    tmp = out + ".tmp" + os.path.splitext(out)[1]
    graph.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,apad[out]")
    cmd += ["-filter_complex", ";".join(graph)]
    cmd += ["-map", "0:v", "-map", "[out]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"] if video else ["-map", "[out]", "-ac", "2"]
    cmd += ["-t", f"{duration:.3f}"] + (["-movflags", "+faststart"] if video else []) + [tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("ffmpeg: " + (r.stderr.strip()[-600:] or "mix failed"))
    os.replace(tmp, out)


# ---------------------------------------------------------------- ElevenLabs API

class ElevenLabs:
    def __init__(self, key):
        self.key = key

    def call(self, method, path, body=None, form=None, binary=False, timeout=300):
        headers, data = {"xi-api-key": self.key}, None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        elif form is not None:
            data, headers["Content-Type"] = multipart(form)
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError("ElevenLabs: " + error_text(e)) from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"ElevenLabs unreachable: {e.reason}") from None
        return raw if binary else json.loads(raw)

    def voices(self):
        data = self.call("GET", "/v2/voices?page_size=100", timeout=30)
        return [{"id": v["voice_id"], "name": v.get("name") or v["voice_id"], "category": v.get("category"),
                 "labels": ", ".join(x for x in (v.get("labels") or {}).values() if x), "preview": v.get("preview_url")}
                for v in data.get("voices", [])]

    def speech(self, text, voice_id):
        return self.call("POST", f"/v1/text-to-speech/{voice_id}?output_format={MP3}",
                         {"text": text, "model_id": "eleven_multilingual_v2"}, binary=True)

    def dialogue(self, lines):
        return self.call("POST", f"/v1/text-to-dialogue?output_format={MP3}",
                         {"inputs": [{"text": l["text"], "voice_id": l["voice_id"]} for l in lines], "model_id": "eleven_v3"},
                         binary=True)

    def sound_effect(self, text, duration, loop):
        return self.call("POST", f"/v1/sound-generation?output_format={MP3}",
                         {"text": text, "duration_seconds": duration, "loop": loop}, binary=True)

    def music(self, prompt, seconds, instrumental):
        return self.call("POST", f"/v1/music?output_format={MP3}",
                         {"prompt": prompt, "music_length_ms": int(seconds * 1000), "force_instrumental": instrumental},
                         binary=True, timeout=600)

    def dub(self, path, language, report, cancelled):
        """Upload `path`, dub it into `language` and return the dubbed audio (FLAC bytes)."""
        with open(path, "rb") as f:
            project = self.call("POST", "/v1/dubbing/project", form={
                "target_language": language, "reference": "ai-director",
                "file": (os.path.basename(path), f.read(), "audio/wav")})
        pid, lid = project["project_id"], project["language_ids"][0]  # target_language created the target
        while True:
            target = self.call("GET", f"/v1/dubbing/project/{pid}/language/{lid}", timeout=60)
            if target["status"] == "completed":
                break
            if target["status"] == "failed":
                raise RuntimeError("ElevenLabs dubbing failed: " + ((target.get("error") or {}).get("error") or "unknown error"))
            report("transcribing" if target["status"] == "queued" else "dubbing")
            time.sleep(5)
            if cancelled():
                raise RuntimeError("removed")
        with urllib.request.urlopen(target["outputs"]["lossless_audio"], timeout=300) as resp:  # signed URL
            return resp.read()


def error_text(e):
    try:
        detail = json.loads(e.read()).get("detail")
    except Exception:
        return f"HTTP {e.code}"
    if isinstance(detail, dict):
        return detail.get("message") or detail.get("status") or json.dumps(detail)
    if isinstance(detail, list):
        return "; ".join(str(d.get("msg", d)) for d in detail)
    return str(detail or f"HTTP {e.code}")


def multipart(fields):
    boundary = uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        if isinstance(value, tuple):
            filename, data, ctype = value
            out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                       f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
        else:
            out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- tracks per video job

class Soundtrack:
    """Audio layers stored in job["audio"]; files live in <job dir>/audio/."""

    def __init__(self, jobs, key):
        self.jobs = jobs
        self.api = ElevenLabs(key) if key else None
        self.mix_lock = threading.Lock()
        self._voices = (0, [])
        for job in jobs.jobs.values():  # work that was in flight when the server stopped
            a = job.get("audio") or {}
            stuck = [t for t in a.get("tracks", []) if t["status"] == "generating"] + \
                    [d for d in a.get("dubs", {}).values() if d["status"] == "working"]
            for item in stuck:
                item.update(status="failed", error="Server restarted while this was in progress.")
            if stuck:
                jobs.save(job)

    @property
    def enabled(self):
        return self.api is not None

    def info(self):
        return {"enabled": self.enabled, "license": LICENSE, "kinds": KINDS, "languages": LANGUAGES}

    def voices(self):
        stamp, cached = self._voices
        if time.time() - stamp > 600:
            self._voices = (time.time(), self.api.voices())
        return self._voices[1]

    def busy(self):
        with self.jobs.lock:
            for job in self.jobs.jobs.values():
                a = job.get("audio") or {}
                if any(t["status"] == "generating" for t in a.get("tracks", [])) or \
                   any(d["status"] == "working" for d in a.get("dubs", {}).values()):
                    return True
        return False

    # ---- paths and state

    def dir(self, job):
        path = os.path.join(self.jobs.dir(job["id"]), "audio")
        os.makedirs(path, exist_ok=True)
        return path

    def video_path(self, job, version="original"):
        """The render itself ("original"), its mix ("mix") or a dubbed mix (a language code)."""
        if version == "original":
            return os.path.join(self.jobs.dir(job["id"]), "result.mp4")
        name = "mix.mp4" if version == "mix" else f"mix_{version}.mp4"
        return os.path.join(self.jobs.dir(job["id"]), "audio", name)

    def file(self, job, name):
        path = os.path.join(self.jobs.dir(job["id"]), "audio", os.path.basename(name))
        return path if os.path.isfile(path) else None

    def _state(self, job):
        return job.setdefault("audio", {"tracks": [], "dubs": {}, "video_volume": 1.0, "mixed": None})

    def _change(self, job, fn):
        with self.jobs.lock:
            result = fn(self._state(job))
            self.jobs.save(job)
        return result

    def _track(self, job, track_id):
        for t in self._state(job)["tracks"]:
            if t["id"] == track_id:
                return t
        raise KeyError("track not found")

    def duration(self, job):
        if not job.get("duration"):
            job["duration"] = probe(self.video_path(job))[0] or 0
        return job["duration"]

    # ---- layers

    def add(self, job, raw):
        if not self.enabled:
            raise ValueError("ElevenLabs is off: add \"elevenlabs_api_key\" to remote/config.json on the computer that runs the app, then restart it")
        if job["status"] != "done":
            raise ValueError("add sound once the video has finished rendering")
        kind = raw.get("kind")
        if kind not in KINDS:
            raise ValueError("unknown track kind")
        length = self.duration(job)
        params = clean_params(kind, raw, length)
        track = {"id": uuid.uuid4().hex[:10], "kind": kind, "status": "generating", "created": time.time(),
                 "start": clamp(raw.get("start", 0), 0, max(length - 0.1, 0)), "volume": clamp(raw.get("volume", 1), 0, 2),
                 "params": params, "label": track_label(kind, params)}
        self._change(job, lambda a: a["tracks"].append(track))
        threading.Thread(target=self._generate, args=(job, track), daemon=True).start()
        return track

    def _generate(self, job, track):
        p = track["params"]
        try:
            if track["kind"] == "voice":
                data = self.api.speech(p["text"], p["voice_id"])
            elif track["kind"] == "dialogue":
                data = self.api.dialogue(p["lines"])
            elif track["kind"] == "sfx":
                data = self.api.sound_effect(p["text"], p["duration"], p["loop"])
            else:
                data = self.api.music(p["prompt"], p["seconds"], p["instrumental"])
            path = os.path.join(self.dir(job), track["id"] + ".mp3")
            with open(path, "wb") as f:
                f.write(data)
            length = probe(path)[0]
            self._change(job, lambda a: track.update(status="done", file=track["id"] + ".mp3", length=length))
            self.remix(job)
        except Exception as e:  # shown on the track in the app
            self._change(job, lambda a: track.update(status="failed", error=str(e)[-800:]))

    def update(self, job, track_id, raw):
        length = self.duration(job)

        def apply(a):
            if track_id == "video":
                a["video_volume"] = clamp(raw.get("volume", a["video_volume"]), 0, 2)
                return
            t = self._track(job, track_id)
            if "start" in raw:
                t["start"] = clamp(raw["start"], 0, max(length - 0.1, 0))
            if "volume" in raw:
                t["volume"] = clamp(raw["volume"], 0, 2)
        self._change(job, apply)
        self.remix(job)

    def remove(self, job, track_id):
        def drop(a):
            t = self._track(job, track_id)
            a["tracks"].remove(t)
            return t
        t = self._change(job, drop)
        if t.get("file"):
            path = self.file(job, t["file"])
            if path:
                os.remove(path)
        self.remix(job)

    # ---- dubbing

    def dub(self, job, language):
        if not self.enabled:
            raise ValueError("ElevenLabs is off: add \"elevenlabs_api_key\" to remote/config.json on the computer that runs the app, then restart it")
        if language not in LANGUAGES:
            raise ValueError("unsupported language")
        if not self._speech_layers(job):
            raise ValueError("add a voice-over or dialogue track first; dubbing translates those")
        dub = {"status": "working", "progress": "uploading", "created": time.time(), "speech": self._speech_signature(job)}
        self._change(job, lambda a: a["dubs"].update({language: dub}))
        threading.Thread(target=self._dub, args=(job, language, dub), daemon=True).start()
        return dub

    def _dub(self, job, language, dub):
        folder = self.dir(job)
        speech = os.path.join(folder, f"speech_{language}.wav")
        try:
            mix(speech, self._speech_layers(job), self.duration(job))

            def report(text):
                with self.jobs.lock:
                    dub["progress"] = text
            data = self.api.dub(speech, language, report, lambda: self._state(job)["dubs"].get(language) is not dub)
            with open(os.path.join(folder, f"dub_{language}.flac"), "wb") as f:
                f.write(data)
            self._change(job, lambda a: dub.update(status="done", progress="done", file=f"dub_{language}.flac"))
            self.remix(job)
        except Exception as e:
            if self._state(job)["dubs"].get(language) is dub:
                self._change(job, lambda a: dub.update(status="failed", error=str(e)[-800:]))
        finally:
            if os.path.exists(speech):
                os.remove(speech)

    def remove_dub(self, job, language):
        dub = self._change(job, lambda a: a["dubs"].pop(language, None))
        for name in (f"dub_{language}.flac", f"mix_{language}.mp4"):
            path = self.file(job, name)
            if path:
                os.remove(path)
        return dub

    def _speech_layers(self, job):
        return [(os.path.join(self.dir(job), t["file"]), t["start"], t["volume"])
                for t in self._state(job)["tracks"] if t["kind"] in SPEECH and t["status"] == "done"]

    def _speech_signature(self, job):
        return sorted([t["id"], t["start"]] for t in self._state(job)["tracks"] if t["kind"] in SPEECH and t["status"] == "done")

    # ---- mixing

    def remix(self, job):
        """Rebuild the mixed video and every dubbed version after a layer changed."""
        with self.mix_lock:
            a = self._state(job)
            folder, video, length = self.dir(job), self.video_path(job), self.duration(job)
            has_audio = probe(video)[1]
            done = [t for t in a["tracks"] if t["status"] == "done"]
            others = [(os.path.join(folder, t["file"]), t["start"], t["volume"]) for t in done if t["kind"] not in SPEECH]
            speech = [(os.path.join(folder, t["file"]), t["start"], t["volume"]) for t in done if t["kind"] in SPEECH]
            if done:
                mix(self.video_path(job, "mix"), others + speech, length, video, a["video_volume"], has_audio)
            elif os.path.exists(self.video_path(job, "mix")):
                os.remove(self.video_path(job, "mix"))
            signature = self._speech_signature(job)
            for language, dub in list(a["dubs"].items()):
                if dub["status"] == "done":
                    layers = others + [(os.path.join(folder, dub["file"]), 0, 1.0)]
                    mix(self.video_path(job, language), layers, length, video, a["video_volume"], has_audio)
                    dub["stale"] = dub["speech"] != signature
            self._change(job, lambda s: s.update(mixed=time.time() if done else None))


def clamp(value, lo, hi):
    return round(min(max(float(value), lo), hi), 2)


def text_field(raw, name, limit, what):
    text = str(raw.get(name) or "").strip()
    if not text:
        raise ValueError(f"write the {what}")
    if len(text) > limit:
        raise ValueError(f"the {what} is too long (max {limit} characters)")
    return text


def clean_params(kind, raw, video_length):
    if kind == "voice":
        return {"text": text_field(raw, "text", 5000, "voice-over text"), "voice_id": voice_id(raw.get("voice_id"))}
    if kind == "dialogue":
        lines = [{"voice_id": voice_id(l.get("voice_id")), "text": str(l.get("text") or "").strip(),
                  "speaker": str(l.get("speaker") or "").strip()[:40]}
                 for l in raw.get("lines") or [] if str(l.get("text") or "").strip()]
        if not lines:
            raise ValueError("write at least one line of dialogue")
        if len(lines) > 40 or sum(len(l["text"]) for l in lines) > 5000:
            raise ValueError("the dialogue is too long (max 40 lines, 5000 characters)")
        if len({l["voice_id"] for l in lines}) > 10:
            raise ValueError("dialogue can use at most 10 different voices")
        return {"lines": lines}
    if kind == "sfx":
        duration = raw.get("duration")
        return {"text": text_field(raw, "text", 1000, "sound description"),
                "duration": None if duration in (None, "") else clamp(duration, 0.5, 30), "loop": bool(raw.get("loop"))}
    seconds = raw.get("seconds") or max(3, video_length)
    return {"prompt": text_field(raw, "prompt", 2000, "music description"), "seconds": clamp(seconds, 3, 300),
            "instrumental": bool(raw.get("instrumental", True))}


def voice_id(value):
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", value):
        raise ValueError("choose a voice")
    return value


def track_label(kind, p):
    text = {"voice": lambda: p["text"], "dialogue": lambda: " / ".join(l["text"] for l in p["lines"]),
            "sfx": lambda: p["text"], "music": lambda: p["prompt"]}[kind]()
    return text if len(text) <= 80 else text[:77] + "…"

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
import hmac
import io
import json
import os
import queue
import secrets
import shutil
import socket
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

STATIC = os.path.join(HERE, "static")
CONFIG = os.path.join(HERE, "config.json")
JOBS_DIR = os.path.join(ROOT, "output", "remote_jobs")
KEYFRAMES_DIR = os.path.join(ROOT, "output", "keyframe_maker")
MAX_KEYFRAMES = 12
MAX_SIDE = 2048  # phone photos are downscaled to this before rendering

SIZES = {"preview": 512 * 288, "standard": 832 * 480}
LENGTHS = {33, 49, 65, 81}


# ---------------------------------------------------------------- job store

class Jobs:
    def __init__(self, comfy_url):
        self.comfy_url = comfy_url
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
            data = {k: v for k, v in job.items() if k != "cancel"}
        data["queue_position"] = self.position(job["id"])
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
            "server": self.comfy_url,
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
            "length": s.get("length", 81),
            "interpolate": s.get("interpolate", 2),
            "area": SIZES.get(s.get("size"), SIZES["standard"]),
            "loop": s.get("loop", False),
            "seed": s.get("seed"),
            "output": os.path.join(out_dir, "result.mp4"),
        }

        def report(text, final=False):
            with self.lock:
                job["progress"] = text
                if final:
                    job["log"] = (job.get("log") or [])[-30:] + [text]
                    self.save(job)

        self.update(job, status="running", started=time.time(), progress="starting")
        keep_awake(True)
        try:
            cfg = animate.make_config(overrides, out_dir)
            animate.run(cfg, report, lambda: job.get("cancel", False))
            styled_dir = os.path.join(out_dir, "result_keyframes")
            styled = sorted(os.listdir(styled_dir)) if os.path.isdir(styled_dir) else []
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

    def __init__(self, comfy_url):
        self.comfy_url = comfy_url
        self.tasks = {}
        self.lock = threading.Lock()
        os.makedirs(KEYFRAMES_DIR, exist_ok=True)

    @staticmethod
    def path(image_id):
        if not image_id or not all(c in "0123456789abcdef" for c in image_id) or len(image_id) != 16:
            raise ValueError("bad image id")
        return os.path.join(KEYFRAMES_DIR, image_id + ".png")

    def start(self, prompt, aspect, source_id=None, seed=None, model="klein4b"):
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
        threading.Thread(target=self._run, args=(task, prompt, aspect, source, seed, model), daemon=True).start()
        return task

    def _run(self, task, prompt, aspect, source, seed, model):
        image_id = uuid.uuid4().hex[:16]

        def report(text, final=False):
            task["progress"] = text

        try:
            used = animate.make_keyframe(self.comfy_url, prompt, self.path(image_id), seed, aspect, source, report, model)
            task.update(status="done", image_id=image_id, seed=used)
        except Exception as e:
            task.update(status="failed", error=str(e)[-1500:])

    def get(self, task_id):
        return self.tasks.get(task_id)


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
        "length": int(raw.get("length", 81)) if int(raw.get("length", 81)) in LENGTHS else 81,
        "interpolate": 2 if raw.get("smooth", True) else 1,
        "size": raw.get("size") if raw.get("size") in SIZES else "standard",
        "loop": loop,
        "seed": int(seed) if str(seed or "").strip().isdigit() else None,
    }


# ---------------------------------------------------------------- web app

def make_app(jobs, token, maker):
    @web.middleware
    async def auth(request, handler):
        if request.path.startswith("/api/"):
            given = request.headers.get("X-Token") or request.query.get("t", "")
            if not hmac.compare_digest(given.encode(), token.encode()):
                return web.json_response({"error": "bad or missing token"}, status=401)
        return await handler(request)

    def get_job(request):
        job = jobs.jobs.get(request.match_info["id"])
        if not job:
            raise web.HTTPNotFound()
        return job

    async def index(request):
        return web.FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})

    async def info(request):
        styles = animate.load_styles()
        models_dir = os.path.join(ROOT, "ComfyUI", "models")
        h3_missing = [f"{sub}/{animate.DEFAULTS['models'][key]}"
                      for key, sub in (("h3_unet", "diffusion_models"), ("h3_text_encoder", "text_encoders"),
                                       ("h3_vae", "vae"), ("h3_audio_vae", "vae"), ("h3_lora", "loras"))
                      if not os.path.exists(os.path.join(models_dir, sub, animate.DEFAULTS["models"][key]))]
        comfy = {"online": False}
        try:
            stats = await asyncio.to_thread(_fetch_json, jobs.comfy_url + "/system_stats")
            dev = (stats.get("devices") or [{}])[0]
            comfy = {"online": True, "gpu": dev.get("name", ""), "vram_total": dev.get("vram_total"),
                     "vram_free": dev.get("vram_free"), "ram_total": stats.get("system", {}).get("ram_total"),
                     "ram_free": stats.get("system", {}).get("ram_free")}
        except Exception:
            pass
        return web.json_response({
            "styles": [{"id": k, "label": v.get("label", k), "restyle": bool(v.get("restyle_prompt"))} for k, v in styles.items()],
            "comfy": comfy,
            "image_models": [{"id": k, "label": v["label"], "missing": animate.missing_image_model_files(models_dir, k)}
                             for k, v in animate.IMAGE_MODELS.items()],
            "engines": [{"id": "minimax-h3", "label": "MiniMax H3 + sound", "ready": not h3_missing,
                         "missing": h3_missing},
                        {"id": "wan22", "label": "Wan 2.2", "ready": True}],
            "queued": sum(1 for j in jobs.jobs.values() if j["status"] == "queued"),
            "running": any(j["status"] == "running" for j in jobs.jobs.values()),
        })

    async def list_jobs(request):
        items = sorted(jobs.jobs.values(), key=lambda j: j["created"], reverse=True)
        return web.json_response([jobs.public(j) for j in items[:50]])

    async def create_job(request):
        reader = await request.multipart()
        files, raw = [], {}
        async for part in reader:
            if part.name == "keyframes":
                files.append(await part.read(decode=False))
            elif part.name == "settings":
                raw = json.loads(await part.text())
        if not 2 <= len(files) <= MAX_KEYFRAMES:
            return web.json_response({"error": f"send 2 to {MAX_KEYFRAMES} keyframes"}, status=400)
        try:
            settings = clean_settings(raw, len(files), animate.load_styles())
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)

        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        in_dir = os.path.join(jobs.dir(job_id), "inputs")
        os.makedirs(in_dir)
        names = []
        try:
            for i, data in enumerate(files):
                name = f"{i + 1:02d}.png"
                await asyncio.to_thread(save_keyframe, data, os.path.join(in_dir, name))
                names.append(name)
        except Exception as e:
            shutil.rmtree(jobs.dir(job_id), ignore_errors=True)
            return web.json_response({"error": f"could not read image {len(names) + 1}: {e}"}, status=400)

        job = {"id": job_id, "created": time.time(), "status": "queued", "progress": "queued",
               "settings": settings, "inputs": names, "styled": [], "log": []}
        jobs.add(job)
        return web.json_response(jobs.public(job))

    async def job_detail(request):
        return web.json_response(jobs.public(get_job(request)))

    async def cancel_job(request):
        job = get_job(request)
        if job["status"] == "queued":
            jobs.update(job, status="cancelled", progress="cancelled")
        elif job["status"] == "running":
            job["cancel"] = True
        return web.json_response(jobs.public(job))

    async def delete_job(request):
        job = get_job(request)
        if job["status"] == "running":
            return web.json_response({"error": "cancel it first"}, status=409)
        jobs.delete(job["id"])
        return web.json_response({"ok": True})

    async def video(request):
        job = get_job(request)
        path = os.path.join(jobs.dir(job["id"]), "result.mp4")
        if not os.path.exists(path):
            raise web.HTTPNotFound()
        headers = {}
        if request.query.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="animation-{job["id"]}.mp4"'
        return web.FileResponse(path, headers=headers)

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
        try:
            task = maker.start(prompt, data.get("aspect", "16:9"), data.get("source") or None,
                               int(seed) if str(seed or "").isdigit() else None, data.get("model") or "klein4b")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(task)

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
        except Exception as e:
            return web.json_response({"error": f"could not read the image: {e}"}, status=400)
        return web.json_response({"image_id": image_id})

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
        web.get("/api/info", info),
        web.get("/api/jobs", list_jobs),
        web.post("/api/jobs", create_job),
        web.get("/api/jobs/{id}", job_detail),
        web.post("/api/jobs/{id}/cancel", cancel_job),
        web.delete("/api/jobs/{id}", delete_job),
        web.get("/api/jobs/{id}/video", video),
        web.get("/api/jobs/{id}/image/{kind}/{name}", image),
        web.post("/api/keyframes/generate", keyframe_generate),
        web.get("/api/keyframes/tasks/{id}", keyframe_task),
        web.post("/api/keyframes/upload", keyframe_upload),
        web.get("/api/keyframes/image/{id}", keyframe_image),
        web.static("/static", STATIC),
    ])
    return app


def _fetch_json(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------- startup

def load_token(regenerate):
    cfg = {}
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
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
    args = ap.parse_args()

    token = load_token(args.new_token)
    jobs = Jobs(args.comfy)
    print_links(args.port, token, args.public_url or default_public_url(args.port))
    web.run_app(make_app(jobs, token, KeyframeMaker(args.comfy)), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()

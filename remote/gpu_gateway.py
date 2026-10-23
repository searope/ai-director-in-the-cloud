"""Token-protected door to ComfyUI on a Runpod pod, for a web app running on another machine.

ComfyUI has no password, so it only listens on 127.0.0.1:8188. This gateway listens on port 8700
(exposed through Runpod's HTTPS proxy), checks the X-Gateway-Token header against GATEWAY_TOKEN,
and passes requests through to ComfyUI.

Safety net: if nothing but health checks has come through for GATEWAY_IDLE_MINUTES (default 30)
and ComfyUI's queue is empty, the pod stops itself (in case the machine that manages it went away).

On a Community Cloud pod (no network volume, see remote/pod_bootstrap.sh) the gateway also downloads the
models: the profiles in MODEL_PROFILES when it starts, more when the app asks (POST /gateway/fetch), and
reports the progress and the setup step in /gateway/health.

Started by remote/pod_start.sh; see remote/gpu.py for the other side.
"""

import asyncio
import hmac
import json
import os
import sys
import threading
import time
import urllib.request

from aiohttp import ClientSession, ClientTimeout, web

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(os.path.dirname(HERE), "ComfyUI", "models")
SETUP_STAGE = os.path.join(os.path.dirname(HERE), "output", "setup_stage.txt")  # written by pod_bootstrap.sh
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "setup"))
COMFY = "http://127.0.0.1:8188"
MODEL_DIRS = ("diffusion_models", "text_encoders", "vae", "loras", "unet", "clip", "checkpoints", "upscale_models")
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
       "proxy-authenticate", "host", "content-length", "x-gateway-token"}


VOLUME = "/workspace" if os.path.isdir("/workspace") else os.path.dirname(HERE)  # the network volume's mount
_used = {"bytes": None, "at": 0.0, "busy": False}


def volume_used():
    """Bytes stored on the network volume. Its free space can't be read (the network filesystem reports the whole
    cluster), so the files are added up, in the background and at most every 10 minutes."""
    def walk():
        total = 0
        for d, _, files in os.walk(VOLUME):
            for f in files:
                try:
                    total += os.lstat(os.path.join(d, f)).st_size
                except OSError:
                    pass
        _used.update(bytes=total, at=time.time(), busy=False)
    if not _used["busy"] and time.time() - _used["at"] > 600:
        _used["busy"] = True
        threading.Thread(target=walk, daemon=True).start()
    return _used["bytes"]


def list_models():
    found = {}
    for sub in MODEL_DIRS:
        base = os.path.join(MODELS, sub)
        found[sub] = sorted(os.path.relpath(os.path.join(d, f), base).replace(os.sep, "/")
                            for d, _, files in os.walk(base, followlinks=True) for f in files) if os.path.isdir(base) else []
    return found


class Models:
    """Model downloads run by the gateway (Community pods); None of it is used on a pod with the network volume."""

    def __init__(self):
        self.downloader, self.lock = None, threading.Lock()

    def fetch(self, names):
        import download_models
        profiles = download_models.load_profiles()
        names = [n for n in names if n in profiles]
        with self.lock:
            if not self.downloader:
                os.makedirs(MODELS, exist_ok=True)
                self.downloader = download_models.Downloader(MODELS)
        threading.Thread(target=self.downloader.add, args=(download_models.files_of(profiles, names),), daemon=True).start()
        return names

    def status(self):
        return self.downloader.status() if self.downloader else None

    def busy(self):
        return bool(self.downloader and self.downloader.status()["busy"])


def setup_stage():
    try:
        with open(SETUP_STAGE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def make_app(token, activity, models):
    session = {}

    @web.middleware
    async def auth(request, handler):
        given = request.headers.get("X-Gateway-Token", "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            return web.json_response({"error": "bad or missing gateway token"}, status=401)
        if not request.path.startswith("/gateway/"):
            activity["last"] = time.time()
        return await handler(request)

    async def client():
        if "c" not in session:
            session["c"] = ClientSession(timeout=ClientTimeout(total=600))
        return session["c"]

    async def health(request):
        try:
            async with (await client()).get(COMFY + "/system_stats", timeout=ClientTimeout(total=5)) as r:
                comfy = r.status == 200
        except Exception:
            comfy = False
        used = volume_used()  # the app knows the volume's size; None until the first count is done
        disk = {"used": used} if used is not None else None
        return web.json_response({"comfy": comfy, "models": await asyncio.to_thread(list_models) if comfy else None,
                                  "disk": disk, "setup": setup_stage(), "downloads": models.status()})

    async def fetch(request):
        """Download model profiles (setup/models.json) the app needs: {"profiles": [...]}."""
        names = (await request.json()).get("profiles") or []
        return web.json_response({"profiles": await asyncio.to_thread(models.fetch, names)})

    async def proxy(request):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        try:
            async with (await client()).request(request.method, COMFY + request.path_qs, headers=headers,
                                                data=await request.read()) as r:
                out = web.StreamResponse(status=r.status, headers={k: v for k, v in r.headers.items() if k.lower() not in HOP})
                await out.prepare(request)
                async for chunk in r.content.iter_chunked(1 << 16):
                    await out.write(chunk)
                await out.write_eof()
                return out
        except OSError as e:
            return web.json_response({"error": f"ComfyUI is not reachable: {e}"}, status=502)

    async def logs(request):
        """The last lines of ComfyUI's and the model download's logs, to see why a pod is slow or failing."""
        out = {}
        for name in ("comfyui", "download", "setup"):
            try:
                with open(os.path.join(os.path.dirname(HERE), "output", f"{name}.log"), encoding="utf-8", errors="replace") as f:
                    out[name] = f.read()[-6000:]
            except OSError as e:
                out[name] = str(e)
        return web.json_response(out)

    app = web.Application(middlewares=[auth], client_max_size=200 * 1024 ** 2)
    app.router.add_get("/gateway/health", health)
    app.router.add_get("/gateway/logs", logs)
    app.router.add_post("/gateway/fetch", fetch)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


def comfy_idle():
    try:
        with urllib.request.urlopen(COMFY + "/queue", timeout=10) as r:
            q = json.loads(r.read())
        return not q.get("queue_running") and not q.get("queue_pending")
    except Exception:
        return True  # ComfyUI is down: nothing is running


def stop_pod():
    pod, key = os.environ.get("RUNPOD_POD_ID"), os.environ.get("RUNPOD_API_KEY")
    if not (pod and key):
        print("Safety net: can't stop the pod (RUNPOD_POD_ID / RUNPOD_API_KEY not set).", flush=True)
        return
    req = urllib.request.Request(f"https://api.runpod.io/v2/pods/{pod}/action", method="POST",
                                 data=json.dumps({"action": "stop"}).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=60).close()


def watchdog(activity, minutes, models):
    while True:
        time.sleep(60)
        if time.time() - activity["last"] < minutes * 60 or not comfy_idle() or models.busy():
            continue
        print(time.strftime("%H:%M:%S"), f"No requests for {minutes} min: stopping the pod.", flush=True)
        try:
            stop_pod()
        except Exception as e:
            print("Could not stop the pod:", e, flush=True)
        activity["last"] = time.time()  # don't repeat while it shuts down (or retry after another period)


def main():
    token = os.environ.get("GATEWAY_TOKEN")
    if not token or len(token) < 16:
        raise SystemExit("GATEWAY_TOKEN (16+ characters) must be set; the web app passes it when it creates the pod.")
    minutes = int(os.environ.get("GATEWAY_IDLE_MINUTES", 30))
    activity, models = {"last": time.time()}, Models()
    if minutes:
        threading.Thread(target=watchdog, args=(activity, minutes, models), daemon=True).start()
    if os.environ.get("MODEL_PROFILES"):  # a pod without the network volume: fetch what the first job needs
        print("Downloading model profiles:", ", ".join(models.fetch(os.environ["MODEL_PROFILES"].split())), flush=True)
    print(f"Gateway on port 8700 -> ComfyUI; safety stop after {minutes} idle min.", flush=True)
    web.run_app(make_app(token, activity, models), host="0.0.0.0", port=8700, print=None)


if __name__ == "__main__":
    main()

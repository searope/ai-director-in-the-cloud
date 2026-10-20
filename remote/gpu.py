"""Where ComfyUI runs: on this machine, or on a Runpod GPU pod that exists only while there is work.

LocalGPU: ComfyUI on this machine (or a fixed address), always assumed to be there.

RunpodGPU: the web app runs on an always-on machine (e.g. a home PC) and creates a Runpod pod
when a job needs the GPU. The pod mounts the network volume that holds ComfyUI and the models,
runs remote/pod_start.sh, and serves ComfyUI only through remote/gpu_gateway.py (token-protected)
on Runpod's HTTPS proxy. After `idle_minutes` with no work the pod is terminated, so GPU time is
paid only around jobs. Terminating (not stopping) means the next session can take any free GPU in
the volume's data center; nothing is lost, since everything lives on the network volume.

Config: the "gpu" section of remote/config.json (see docs/home-server.md).
"""

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

API = "https://api.runpod.io/v2"
GATEWAY_PORT = 8700
# Runpod's proxy (Cloudflare) rejects Python's default "Python-urllib" User-Agent with error 1010.
USER_AGENT = "ai-director/1.0"


class LocalGPU:
    remote = False
    headers = {}

    def __init__(self, url):
        self._url = url.rstrip("/")

    def url(self):
        return self._url

    def ensure(self, report=lambda text: None, cancelled=lambda: False):
        return self._url

    def touch(self):
        pass

    def status(self):
        return {"mode": "local"}

    def models(self):
        return None  # check the local ComfyUI/models folder instead


class RunpodGPU:
    remote = True

    def __init__(self, cfg, state_path, busy, log_path=None):
        """cfg: the "gpu" config section. busy(): True while jobs or image tasks are waiting or running."""
        self.cfg = {"gpu_types": ["NVIDIA GeForce RTX 5090"], "cloud": "SECURE", "idle_minutes": 10,
                    "max_hours": 4, "disk_gb": 30, "boot_timeout_minutes": 20, "min_cuda": "13.0", **cfg}
        missing = [k for k in ("api_key", "volume_id", "datacenter", "image") if not self.cfg.get(k)]
        if missing:
            raise ValueError("gpu config is missing: " + ", ".join(missing))
        if not self.cfg.get("gateway_token"):
            self.cfg["gateway_token"] = secrets.token_urlsafe(24)
        self.headers = {"X-Gateway-Token": self.cfg["gateway_token"], "User-Agent": USER_AGENT}
        self.busy = busy
        self.state_path, self.log_path = state_path, log_path
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.state, self.error, self.pod, self.since = "off", None, None, time.time()
        self.last_activity, self.cached_models = time.time(), None
        self._load()
        threading.Thread(target=self._watch, daemon=True).start()

    # ---- public

    def url(self):
        return f"https://{self.pod['id']}-{GATEWAY_PORT}.proxy.runpod.net" if self.pod else None

    def touch(self):
        self.last_activity = time.time()

    def ensure(self, report=lambda text: None, cancelled=lambda: False):
        """Return the ComfyUI address, creating the pod first if needed (blocks until it is ready)."""
        self.touch()
        self.start_now()
        while not self.ready.wait(3):
            if self.state == "failed":
                raise RuntimeError("Could not start the GPU pod: " + (self.error or "unknown error"))
            if self.state == "off":  # it was shutting down when this job arrived
                self.start_now()
            if cancelled():
                raise RuntimeError("cancelled while the GPU was starting")
            report(f"starting the GPU ({fmt(time.time() - self.since)}, usually 2-5 min)")
            self.touch()
        return self.url()

    def start_now(self):
        self.touch()
        with self.lock:
            if self.state in ("off", "failed"):
                self.error = None
                self._set("starting")
                threading.Thread(target=self._start, daemon=True).start()

    def stop_now(self):
        threading.Thread(target=self._terminate, args=("stopped from the app",), daemon=True).start()

    def status(self):
        left = None
        if self.state == "ready" and not self.busy():
            left = max(0, self.cfg["idle_minutes"] * 60 - (time.time() - self.last_activity))
        return {"mode": "runpod", "state": self.state, "error": self.error, "since": self.since,
                "gpu": (self.pod or {}).get("gpu_id"), "cost_per_hour": (self.pod or {}).get("cost"),
                "idle_minutes": self.cfg["idle_minutes"], "seconds_left": None if left is None else round(left),
                "month_cost": round(self.month_cost(), 2)}

    def models(self):
        return self.cached_models

    # ---- Runpod API

    def api(self, method, path, body=None, timeout=60):
        req = urllib.request.Request(API + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.cfg['api_key']}", "Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read())
                detail = detail.get("detail") or detail.get("title") or detail
            except Exception:
                detail = f"HTTP {e.code}"
            raise ApiError(e.code, f"Runpod API {method} {path}: {detail}") from None

    def _create(self):
        c = self.cfg
        root = c.get("project_dir", "/workspace/ai-director-in-the-cloud")
        body = {
            "name": "ai-director-gpu",
            "image": c["image"],
            # Update the pod-side code on the volume from GitHub first (a second or two; a failure keeps the old code).
            "cmd": ["bash", "-c", f"(/start.sh >/dev/null 2>&1 &) ; (cd {root} && timeout 60 git pull --ff-only -q) ; "
                                  f"exec bash {root}/remote/pod_start.sh"],
            "disk": c["disk_gb"],
            "ports": [f"{GATEWAY_PORT}/http"],
            "env": {"GATEWAY_TOKEN": c["gateway_token"], "GATEWAY_IDLE_MINUTES": str(c.get("pod_safety_minutes", 30)),
                    **c.get("env", {})},
            "cloud": c["cloud"],
            "dataCenterIds": [c["datacenter"]],
            "mounts": {"network": [{"volumeId": c["volume_id"], "path": "/workspace"}]},
        }
        if c.get("registry"):
            body["registry"] = c["registry"]
        errors = []
        for gpu_id in c["gpu_types"]:  # in order of preference; 400 also means "none free right now"
            try:
                gpu = {"id": gpu_id, "count": 1, "minCudaVersion": c["min_cuda"]}  # the venv has CUDA 13 PyTorch
                pod = self.api("POST", "/pods", {**body, "gpu": gpu})
                return {"id": pod["id"], "gpu_id": gpu_id, "cost": pod.get("cost"), "created": time.time()}
            except ApiError as e:
                if e.code not in (400, 409, 422, 503):
                    raise
                errors.append(f"{gpu_id}: {e}")
        raise RuntimeError("no GPU available right now. " + " | ".join(errors))

    # ---- lifecycle

    def _start(self, reattach=False):
        try:
            if not self.pod:
                self.pod = self._create()
                self._save()
            deadline = time.time() + self.cfg["boot_timeout_minutes"] * 60
            while time.time() < deadline:
                if self._gateway_ok():
                    self._set("ready")
                    self.ready.set()
                    self.touch()
                    return
                info = self._pod_info()
                if info is None or info.get("status") in ("TERMINATED", "ERROR"):
                    raise RuntimeError(f"the pod {'disappeared' if info is None else 'failed to start'}")
                if info.get("cost") is not None:
                    self.pod["cost"] = info["cost"]
                time.sleep(5)
            raise RuntimeError(f"ComfyUI didn't come up within {self.cfg['boot_timeout_minutes']} minutes")
        except Exception as e:
            self._terminate("failed to start")
            if reattach:  # the pod from before a restart of this app is gone: nothing to report
                return
            self.error = str(e)[-600:]
            self._set("failed")

    def _gateway_ok(self):
        try:
            req = urllib.request.Request(self.url() + "/gateway/health", headers=self.headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            if data.get("comfy"):
                self.cached_models = data.get("models")
                return True
        except Exception:
            pass
        return False

    def _pod_info(self):
        try:
            return self.api("GET", f"/pods/{self.pod['id']}")
        except ApiError as e:
            if e.code == 404:
                return None
            raise

    def _terminate(self, reason):
        with self.lock:
            pod = self.pod
            if not pod:
                return
            self.ready.clear()
            self._set("stopping")
        try:
            self.api("DELETE", f"/pods/{pod['id']}")
        except ApiError as e:
            if e.code != 404:
                self.error = f"Could not remove the pod ({e}). Check the Runpod console!"
                self._set("failed")
                return
        except Exception as e:  # network trouble: keep the pod id and try again on the next check
            self.error = f"Could not reach Runpod to remove the pod ({e}); retrying."
            self._set("ready" if self.ready.is_set() else "failed")
            return
        self._log("stop", reason)  # every pod is billed from creation, even one that never became ready
        self.pod = None
        self._save()
        self._set("off")

    def _watch(self):
        """Terminate after idle_minutes without work, after max_hours, or if Runpod stopped the pod."""
        while True:
            time.sleep(20)
            try:
                if self.busy():
                    self.touch()
                if self.state == "ready":
                    if not self._gateway_ok():
                        info = self._pod_info()
                        if info is None or info.get("status") in ("EXITED", "TERMINATED", "ERROR"):
                            self._terminate("the pod stopped by itself")
                            continue
                    idle = time.time() - self.last_activity
                    age = time.time() - self.pod.get("created", time.time())
                    if idle > self.cfg["idle_minutes"] * 60:
                        self._terminate(f"idle for {self.cfg['idle_minutes']} min")
                    elif age > self.cfg["max_hours"] * 3600 and not self.busy():
                        self._terminate(f"safety limit of {self.cfg['max_hours']} h")
                elif self.state in ("off", "failed") and self.pod:  # left over (e.g. a failed removal)
                    self._terminate("cleanup")
            except Exception as e:
                self.error = str(e)[-300:]

    def _set(self, state):
        self.state, self.since = state, time.time()

    # ---- persistence and cost log

    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        if saved.get("pod"):
            self.pod = saved["pod"]
            if saved.get("gateway_token"):  # the pod was created with this token
                self.cfg["gateway_token"] = saved["gateway_token"]
                self.headers = {"X-Gateway-Token": saved["gateway_token"]}
            self.state = "starting"
            self.ready.clear()
            threading.Thread(target=self._start, args=(True,), daemon=True).start()  # reattach or clean up

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pod": self.pod, "gateway_token": self.cfg["gateway_token"]}, f, indent=1)
        os.replace(tmp, self.state_path)

    def _log(self, event, reason=None):
        if not self.log_path or not self.pod:
            return
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "event": event, "pod": self.pod["id"], "gpu": self.pod.get("gpu_id"),
                                "cost_per_hour": self.pod.get("cost"), "created": self.pod.get("created"),
                                "reason": reason}) + "\n")

    def month_cost(self):
        """GPU spend this calendar month from the session log (pod creation to removal x hourly rate)."""
        if not self.log_path or not os.path.exists(self.log_path):
            return 0.0
        start = time.mktime(time.localtime()[:2] + (1, 0, 0, 0, 0, 0, -1))
        total = 0.0
        with open(self.log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e["event"] == "stop" and e.get("created") and e.get("cost_per_hour") and e["t"] >= start:
                    total += (e["t"] - max(e["created"], start)) / 3600 * e["cost_per_hour"]
        if self.pod and self.pod.get("cost") and self.state in ("ready", "starting", "stopping"):
            total += (time.time() - max(self.pod.get("created", time.time()), start)) / 3600 * self.pod["cost"]
        return total


class ApiError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fmt(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"

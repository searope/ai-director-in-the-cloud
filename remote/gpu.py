"""Where ComfyUI runs: on this machine, or on a Runpod GPU pod that exists only while there is work.

LocalGPU: ComfyUI on this machine (or a fixed address), always assumed to be there.

RunpodGPU: the web app runs on an always-on machine (e.g. a home PC) and creates a Runpod pod
when a job needs the GPU, in one of two ways ("cloud" in the config):
- Secure Cloud: the pod mounts the network volume that holds ComfyUI and the models and runs
  remote/pod_start.sh. Nothing is lost when it's removed, since everything lives on the volume.
- Community Cloud (cheaper, third-party hosts): network volumes can't attach there, so the pod starts
  empty. Its start command carries the pod-side code (pod_code()), remote/pod_bootstrap.sh installs
  ComfyUI in a few minutes, and the gateway downloads only the models the job needs.
  "COMMUNITY_FIRST" tries Community GPUs that fit the models, cost under max_price and have a fast
  download line, cheapest per job first, and falls back to the Secure pod with the volume.
Either way ComfyUI is served only through remote/gpu_gateway.py (token-protected) on Runpod's HTTPS
proxy. After the cloud's idle time with no work the pod is terminated, so GPU time is paid only
around jobs.

Config: the "gpu" section of remote/config.json (see docs/home-server.md).
"""

import base64
import io
import json
import os
import secrets
import tarfile
import threading
import time
import urllib.error
import urllib.request

API = "https://api.runpod.io/v2"
GRAPHQL = "https://api.runpod.io/graphql"  # Community pods: only this API filters hosts by download speed
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY_PORT = 8700
# Runpod's proxy (Cloudflare) rejects Python's default "Python-urllib" User-Agent with error 1010.
USER_AGENT = "ai-director/1.0"


# The pod-side code a Community pod gets in its start command (it has no volume and no copy of the repo).
POD_FILES = ["remote/gpu_gateway.py", "remote/pod_bootstrap.sh", "setup/download_models.py", "setup/models.json",
             "run_comfyui.sh"]
# Model profiles (setup/models.json) each video engine and image model needs.
ENGINE_PROFILES = {"minimax-h3": ["minimax-h3"], "wan22": ["wan22-flf", "interp"], "ltx23": ["ltx23"]}
IMAGE_PROFILES = {"flux3klein": ["flux3-klein"], "klein4b": ["restyle"], "klein9b": ["klein9b"]}
# Speed relative to an RTX 5090 on the video engines, to rank Community GPUs by cost per job (hourly price /
# speed). Rough estimates from the cards' compute and memory bandwidth (Oct 2026), not measurements; set
# "gpu_speed" in the config to correct them. 0 = never use (no bfloat16).
GPU_SPEED = {
    "NVIDIA GeForce RTX 5090": 1.0, "NVIDIA RTX PRO 6000 Blackwell Server Edition": 1.1,
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition": 1.1, "NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition": 0.95,
    "NVIDIA RTX PRO 5000 Blackwell": 0.7, "NVIDIA RTX PRO 4500 Blackwell": 0.55, "NVIDIA GeForce RTX 4090": 0.6,
    "NVIDIA H100 80GB HBM3": 1.3, "NVIDIA H100 NVL": 1.1, "NVIDIA H100 PCIe": 1.0, "NVIDIA A100-SXM4-80GB": 0.65,
    "NVIDIA A100 80GB PCIe": 0.6, "NVIDIA A100-SXM4-40GB": 0.6, "NVIDIA L40S": 0.65, "NVIDIA L40": 0.55,
    "NVIDIA RTX 6000 Ada Generation": 0.6, "NVIDIA RTX 5000 Ada Generation": 0.45, "NVIDIA RTX A6000": 0.3,
    "NVIDIA A40": 0.3, "Tesla V100-SXM2-32GB": 0, "Tesla V100-PCIE-32GB": 0,
}
CUDA_VERSIONS = ["12.8", "12.9", "13.0", "13.1", "13.2", "13.3", "13.4", "13.5"]


def profiles_for(engine=None, image_model=None, restyle=False):
    """The model profiles a job needs: its video engine's, and its image model's when it draws pictures."""
    out = list(ENGINE_PROFILES.get(engine, [])) if engine else []
    if image_model and (restyle or not engine):
        out += [p for p in IMAGE_PROFILES.get(image_model, []) if p not in out]
    return out


def rank_community(offers, max_price=1.0, min_vram_gb=32, speed=None, only=None):
    """Community GPU types to try, cheapest per job first: [(gpu_id, hourly price)].
    offers: [{"id", "memoryInGb", "price", "stock"}] from Runpod (price and stock already for hosts with the download
    speed and CUDA we need). Keeps types in stock with enough VRAM for the models and an hourly price under
    max_price; only: an explicit list of allowed types, which then also sets the order."""
    speed = {**GPU_SPEED, **(speed or {})}
    fit = [o for o in offers if o.get("stock") and o.get("price") and o["price"] < max_price
           and (o.get("memoryInGb") or 0) >= min_vram_gb and speed.get(o["id"], 0.3) > 0
           and (only is None or o["id"] in only)]
    if only is not None:
        fit.sort(key=lambda o: only.index(o["id"]))
    else:
        fit.sort(key=lambda o: (o["price"] / speed.get(o["id"], 0.3), o["price"]))
    return [(o["id"], o["price"]) for o in fit]


def pod_code(root=ROOT):
    """POD_FILES as a base64 .tar.gz for a Community pod's start command (about 15 KB)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in POD_FILES:
            with open(os.path.join(root, rel), "rb") as f:
                data = f.read().replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(rel)
            info.size, info.mode, info.mtime = len(data), 0o755 if rel.endswith(".sh") else 0o644, int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def model_files(names):
    """(dir, file name) of the model profiles' files, from setup/models.json."""
    with open(os.path.join(ROOT, "setup", "models.json"), encoding="utf-8") as f:
        profiles = json.load(f)["profiles"]
    return [(item["dir"], item["name"]) for n in names if n in profiles for item in profiles[n]["files"]]


class LocalGPU:
    remote = False
    headers = {}

    def __init__(self, url):
        self._url = url.rstrip("/")

    def url(self):
        return self._url

    def ensure(self, report=lambda text: None, cancelled=lambda: False, need=None):
        return self._url

    def touch(self):
        pass

    def status(self):
        return {"mode": "local"}

    def lost(self):
        return False

    def replace(self):
        pass

    def models(self):
        return None  # check the local ComfyUI/models folder instead


class RunpodGPU:
    remote = True

    def __init__(self, cfg, state_path, busy, log_path=None):
        """cfg: the "gpu" config section. busy(): True while jobs or image tasks are waiting or running."""
        self.cfg = {"gpu_types": ["NVIDIA GeForce RTX 5090"], "cloud": "SECURE", "idle_minutes": 10,
                    "max_hours": 4, "disk_gb": 30, "boot_timeout_minutes": 20, "min_cuda": "13.0",
                    # Community Cloud (see the module docstring and docs/home-server.md)
                    "max_price": 1.0, "min_download_mbps": 1000, "min_vram_gb": 32, "community_disk_gb": 200,
                    "community_idle_minutes": 30, "community_boot_timeout_minutes": 30,
                    "community_models": ["minimax-h3", "flux3-klein"], **cfg}
        self.cfg["cloud"] = self.cfg["cloud"].upper()
        if self.cfg["cloud"] not in ("SECURE", "COMMUNITY_FIRST", "COMMUNITY"):
            raise ValueError('gpu "cloud" must be "SECURE", "COMMUNITY_FIRST" or "COMMUNITY"')
        needed = ("api_key", "image") if self.cfg["cloud"] == "COMMUNITY" else ("api_key", "volume_id", "datacenter", "image")
        missing = [k for k in needed if not self.cfg.get(k)]
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
        self.last_activity, self.cached_models, self.health = time.time(), None, {}
        self.need = []  # model profiles the next pod should download first (Community)
        # the network volume's last storage reading, kept on disk so the app shows it while the GPU is off
        self.disk_path = os.path.join(os.path.dirname(state_path), "gpu_disk.json")
        try:
            with open(self.disk_path, encoding="utf-8") as f:
                self.cached_disk = json.load(f)
        except (OSError, ValueError):
            self.cached_disk = None
        self._load()
        threading.Thread(target=self._watch, daemon=True).start()

    # ---- public

    def url(self):
        return f"https://{self.pod['id']}-{GATEWAY_PORT}.proxy.runpod.net" if self.pod else None

    def touch(self):
        self.last_activity = time.time()

    def ensure(self, report=lambda text: None, cancelled=lambda: False, need=None):
        """Return the ComfyUI address, creating the pod first if needed (blocks until it is ready).
        need: model profiles the job uses; a Community pod downloads them first and this waits for them."""
        self.touch()
        self.start_now(need)
        while not self.ready.wait(3):
            if self.state == "failed":
                raise RuntimeError("Could not start the GPU pod: " + (self.error or "unknown error"))
            if self.state == "off":  # it was shutting down when this job arrived
                self.start_now(need)
            if cancelled():
                raise RuntimeError("cancelled while the GPU was starting")
            report(f"starting the GPU ({fmt(time.time() - self.since)}, {self.progress() or self._usual()})")
            self.touch()
        if need and self.cloud() == "COMMUNITY":
            self._wait_models(need, report, cancelled)
        return self.url()

    def _usual(self):
        return "usually 5-10 min on Community Cloud" if self.cfg["cloud"] != "SECURE" else "usually 2-5 min"

    def _wait_models(self, need, report, cancelled):
        """Ask the pod's gateway to download these profiles (it skips files it has) and wait until they're there."""
        files = model_files(need)
        asked = False
        while True:
            have = self.cached_models or {}
            missing = [f for d, f in files if f not in have.get(d, [])]
            if not missing:
                return
            if not asked:
                self._gateway("POST", "/gateway/fetch", {"profiles": need})
                asked = True
            failed = [f for f in (self.health.get("downloads") or {}).get("failed", []) if f in missing]
            if failed:
                raise RuntimeError("could not download " + ", ".join(failed) + " (see the pod's download log)")
            if cancelled():
                raise RuntimeError("cancelled while the models were downloading")
            if not self.ready.is_set():
                raise RuntimeError("the GPU pod went away while the models were downloading")
            report(self.progress() or f"waiting for {len(missing)} model file(s)")
            self.touch()
            time.sleep(4)
            self._gateway_ok()

    def _gateway(self, method, path, body=None):
        req = urllib.request.Request(self.url() + path, method=method, headers={**self.headers, "Content-Type": "application/json"},
                                     data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")

    def cloud(self):
        return (self.pod or {}).get("cloud", "SECURE")

    def idle_minutes(self):
        return self.cfg["community_idle_minutes"] if self.cloud() == "COMMUNITY" else self.cfg["idle_minutes"]

    def progress(self):
        """What a starting pod is doing, e.g. "installing ComfyUI" or "downloading models 23 / 51 GB"."""
        h = self.health or {}
        d = h.get("downloads") or {}
        if d.get("busy") and d.get("total"):
            speed = f" at {d['speed'] * 8 / 1e6:.0f} Mbps" if d.get("speed") else ""
            return f"downloading models {d['done'] / 1e9:.0f} / {d['total'] / 1e9:.0f} GB{speed}"
        if h.get("setup"):
            return h["setup"]
        if self.state == "starting" and self.pod and not h:
            return "waiting for the pod to boot"
        return None

    def start_now(self, need=None):
        self.touch()
        if need:
            self.need = list(need)
        with self.lock:
            if self.state in ("off", "failed"):
                self.error = None
                self._set("starting")
                threading.Thread(target=self._start, daemon=True).start()

    def stop_now(self):
        threading.Thread(target=self._terminate, args=("stopped from the app",), daemon=True).start()

    def lost(self):
        """After a failed render: has the pod crashed or gone away? (Not answering for half a minute counts.)"""
        if not self.pod:
            return True
        for _ in range(3):
            if self._gateway_ok():
                return False
            time.sleep(10)
        try:
            info = self._pod_info()
        except Exception:
            return True
        return info is None or info.get("status") != "RUNNING" or not self._gateway_ok()

    def replace(self):
        """Remove a lost pod, so the next ensure() starts a new one."""
        self._terminate("lost during a render")

    def status(self):
        left = None
        if self.state == "ready" and not self.busy() and not self._downloading():
            left = max(0, self.idle_minutes() * 60 - (time.time() - self.last_activity))
        return {"mode": "runpod", "state": self.state, "error": self.error, "since": self.since,
                "gpu": (self.pod or {}).get("gpu_id"), "cost_per_hour": (self.pod or {}).get("cost"),
                "cloud": self.cloud() if self.pod else None, "cloud_setting": self.cfg["cloud"],
                "progress": self.progress() if self.pod else None,
                "idle_minutes": self.idle_minutes(), "seconds_left": None if left is None else round(left),
                "month_cost": round(self.month_cost(), 2), "disk": self.cached_disk}

    def _downloading(self):
        return bool((self.health.get("downloads") or {}).get("busy"))

    def models(self):
        return self.cached_models

    def volume_size(self):
        """The network volume's size in bytes (from Runpod), or None."""
        if not getattr(self, "_volume_size", None):
            try:
                req = urllib.request.Request(f"https://rest.runpod.io/v1/networkvolumes/{self.cfg['volume_id']}",  # REST API
                                             headers={"Authorization": f"Bearer {self.cfg['api_key']}", "User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._volume_size = int(json.loads(resp.read())["size"]) * 10**9
            except Exception:
                return None
        return self._volume_size

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

    def gql(self, query, variables=None, timeout=60):
        req = urllib.request.Request(GRAPHQL, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                     headers={"Authorization": f"Bearer {self.cfg['api_key']}", "Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, f"Runpod GraphQL: HTTP {e.code} {e.read()[:300]!r}") from None
        if data.get("errors"):
            raise ApiError(400, "Runpod GraphQL: " + "; ".join(str(x.get("message")) for x in data["errors"]))
        return data["data"]

    def _cuda_ok(self):
        floor = tuple(int(x) for x in self.cfg["min_cuda"].split("."))
        return [v for v in CUDA_VERSIONS if tuple(int(x) for x in v.split(".")) >= floor]

    def community_offers(self):
        """Community GPU types with their lowest price and stock on hosts with min_download_mbps and our CUDA."""
        c = self.cfg
        data = self.gql("query($p: GpuLowestPriceInput) { gpuTypes { id memoryInGb communityCloud "
                        "lowestPrice(input: $p) { uninterruptablePrice stockStatus } } }",
                        {"p": {"gpuCount": 1, "secureCloud": False, "minDownload": c["min_download_mbps"],
                               "allowedCudaVersions": self._cuda_ok()}})
        return [{"id": g["id"], "memoryInGb": g["memoryInGb"], "price": (g.get("lowestPrice") or {}).get("uninterruptablePrice"),
                 "stock": (g.get("lowestPrice") or {}).get("stockStatus")}
                for g in data["gpuTypes"] if g.get("communityCloud")]

    def _create(self):
        errors = []
        if self.cfg["cloud"] in ("COMMUNITY_FIRST", "COMMUNITY"):
            try:
                pod = self._create_community(errors)
                if pod:
                    return pod
            except Exception as e:
                errors.append(f"Community: {e}")
            if self.cfg["cloud"] == "COMMUNITY":
                raise RuntimeError("no Community GPU available right now. " + " | ".join(errors))
        return self._create_secure(errors)

    def community_start(self):
        """Start command and environment of a Community pod (the pod-side code travels in AI_DIRECTOR_CODE)."""
        c = self.cfg
        profiles = list(dict.fromkeys(self.need + c["community_models"]))
        root = "/workspace/ai-director-in-the-cloud"
        env = {"GATEWAY_TOKEN": c["gateway_token"], "GATEWAY_IDLE_MINUTES": str(c.get("pod_safety_minutes", 30)),
               "MODEL_PROFILES": " ".join(profiles), "AI_DIRECTOR_CODE": pod_code(), **c.get("env", {})}
        args = (f"bash -c '(/start.sh >/dev/null 2>&1 &) ; mkdir -p {root} && cd {root} && "
                f"echo \"$AI_DIRECTOR_CODE\" | base64 -d | tar xz && exec bash remote/pod_bootstrap.sh'")
        return args, env, profiles

    def _create_community(self, errors):
        c = self.cfg
        ranked = rank_community(self.community_offers(), c["max_price"], c["min_vram_gb"], c.get("gpu_speed"),
                                c.get("community_gpu_types"))
        if not ranked:
            errors.append(f"Community: no GPU with {c['min_vram_gb']}+ GB under ${c['max_price']}/h on a "
                          f"{c['min_download_mbps']} Mbps host is free")
            return None
        args, env, profiles = self.community_start()
        for gpu_id, price in ranked:
            inp = {"cloudType": "COMMUNITY", "gpuCount": 1, "gpuTypeId": gpu_id, "name": "ai-director-gpu",
                   "imageName": c.get("community_image") or c["image"], "dockerArgs": args, "ports": f"{GATEWAY_PORT}/http",
                   "containerDiskInGb": c["community_disk_gb"], "volumeInGb": 0, "minDownload": c["min_download_mbps"],
                   "allowedCudaVersions": self._cuda_ok(), "env": [{"key": k, "value": v} for k, v in env.items()]}
            try:
                pod = self.gql("mutation($i: PodFindAndDeployOnDemandInput) { podFindAndDeployOnDemand(input: $i) "
                               "{ id costPerHr } }", {"i": inp})["podFindAndDeployOnDemand"]
            except ApiError as e:  # usually "no longer any instances available": try the next type
                errors.append(f"{gpu_id} (Community): {e}")
                continue
            if not pod or not pod.get("id"):
                errors.append(f"{gpu_id} (Community): no pod returned")
                continue
            return {"id": pod["id"], "gpu_id": gpu_id, "cost": pod.get("costPerHr") or price, "created": time.time(),
                    "cloud": "COMMUNITY", "profiles": profiles}
        return None

    def _create_secure(self, errors):
        c = self.cfg
        if not (c.get("volume_id") and c.get("datacenter")):
            raise RuntimeError("no GPU available right now, and there is no network volume to fall back to. "
                               + " | ".join(errors))
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
                    # optional model profiles (setup/models.json) the pod downloads after the defaults
                    "EXTRA_MODEL_PROFILES": " ".join(c.get("extra_models", [])), **c.get("env", {})},
            "cloud": "SECURE",
            "dataCenterIds": [c["datacenter"]],
            "mounts": {"network": [{"volumeId": c["volume_id"], "path": "/workspace"}]},
        }
        if c.get("registry"):
            body["registry"] = c["registry"]
        for gpu_id in c["gpu_types"]:  # in order of preference; 400 also means "none free right now"
            try:
                gpu = {"id": gpu_id, "count": 1, "minCudaVersion": c["min_cuda"]}  # the venv has CUDA 13 PyTorch
                pod = self.api("POST", "/pods", {**body, "gpu": gpu})
                return {"id": pod["id"], "gpu_id": gpu_id, "cost": pod.get("cost"), "created": time.time(), "cloud": "SECURE"}
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
            minutes = self.cfg["community_boot_timeout_minutes" if self.cloud() == "COMMUNITY" else "boot_timeout_minutes"]
            deadline = time.time() + minutes * 60
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
                if self.pod.get("cloud") is None and info.get("cloud"):  # a pod saved by an older version
                    self.pod["cloud"] = info["cloud"]
                time.sleep(5)
            raise RuntimeError(f"ComfyUI didn't come up within {minutes} minutes ({self.progress() or 'no word from the pod'})")
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
            self.health = data
            if data.get("comfy"):
                self.cached_models = data.get("models")
                used = (data.get("disk") or {}).get("used")
                # the storage reading is the network volume's; a Community pod has its own disk instead
                if self.cloud() == "SECURE" and used is not None and used != (self.cached_disk or {}).get("used"):
                    size = self.volume_size()
                    self.cached_disk = {"used": used, "total": size, "free": size - used if size else None,
                                        "seen": time.time()}  # bytes on the network volume, and when
                    try:
                        with open(self.disk_path, "w", encoding="utf-8") as f:
                            json.dump(self.cached_disk, f)
                    except OSError:
                        pass
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
        self.pod, self.health, self.cached_models = None, {}, None
        self._save()
        self._set("off")

    def _watch(self):
        """Terminate after idle_minutes without work, after max_hours, or if Runpod stopped the pod."""
        while True:
            time.sleep(20)
            try:
                if self.busy() or self._downloading():
                    self.touch()
                if self.state == "ready":
                    if not self._gateway_ok():
                        info = self._pod_info()
                        if info is None or info.get("status") in ("EXITED", "TERMINATED", "ERROR"):
                            self._terminate("the pod stopped by itself")
                            continue
                    idle = time.time() - self.last_activity
                    age = time.time() - self.pod.get("created", time.time())
                    if idle > self.idle_minutes() * 60:
                        self._terminate(f"idle for {self.idle_minutes()} min")
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
                self.headers["X-Gateway-Token"] = saved["gateway_token"]  # keeps the User-Agent the proxy needs
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
                                "cloud": self.cloud(),
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

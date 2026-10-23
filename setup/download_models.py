"""Download the model files listed in models.json into ComfyUI/models.

Usage:
    python setup/download_models.py                  # default: minimax-h3 + wan22-flf + interp + restyle + flux3-klein
    python setup/download_models.py --profile interp
    python setup/download_models.py --list

Files download in parallel, each in ranged chunks over several connections (--connections, default 16), which
fills a 1 Gbps line. Downloads resume if interrupted (partial files end in .part, their finished chunks are listed
in .part.done). Set HF_TOKEN in the environment if Hugging Face asks you to log in.

The pod's gateway (remote/gpu_gateway.py) imports Downloader to fetch what a job needs and report its progress.
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "setup", "models.json")
DEFAULT_PROFILES = ["minimax-h3", "wan22-flf", "interp", "restyle", "flux3-klein"]
CHUNK = 128 * 1024 * 1024  # bytes per ranged request
BLOCK = 4 * 1024 * 1024
USER_AGENT = "keyframe-animator-setup"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def load_profiles():
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)["profiles"]


def files_of(profiles, names):
    """(dir, name, url) of every file in these profiles, in order, without duplicates."""
    seen, out = set(), []
    for name in names:
        for item in profiles[name]["files"]:
            key = (item["dir"], item["name"])
            if key not in seen:
                seen.add(key)
                out.append(item)
    return out


class _File:
    def __init__(self, item, dest):
        self.item, self.dest, self.part = item, dest, dest + ".part"
        self.url, self.size, self.chunks, self.done_chunks = None, 0, 0, set()
        self.done_bytes, self.error, self.lock = 0, None, threading.Lock()


class Downloader:
    """Downloads model files in parallel: a queue of ranged chunks shared by `connections` threads, files in the
    order they were added (so what a job needs first finishes first)."""

    def __init__(self, models_dir, connections=16, token=None):
        self.models_dir, self.token = models_dir, token if token is not None else os.environ.get("HF_TOKEN")
        self.chunks = queue.Queue()
        self.files, self.failed = {}, []
        self.lock = threading.Lock()
        self.started, self.base = None, 0  # for the speed: bytes done when the current run started
        for _ in range(connections):
            threading.Thread(target=self._worker, daemon=True).start()

    # ---- public

    def add(self, items):
        """Queue these model entries (dicts with dir, name, url); files already present are skipped."""
        for item in items:
            dest = os.path.join(self.models_dir, item["dir"], item["name"])
            with self.lock:
                if dest in self.files and not self.files[dest].error:
                    continue
                if os.path.exists(dest):
                    continue
                f = self.files[dest] = _File(item, dest)
                self.failed = [n for n in self.failed if n != item["name"]]
                if self.started is None or self.active() == 0:
                    self.started, self.base = time.time(), self._done()
            try:
                self._prepare(f)
            except Exception as e:
                self._fail(f, e)
                continue
            for i in range(f.chunks):
                if i not in f.done_chunks:
                    self.chunks.put((f, i))
            if f.chunks == len(f.done_chunks):
                self._finish(f)

    def status(self):
        with self.lock:
            files = list(self.files.values())
        total = sum(f.size for f in files)
        done = sum(f.done_bytes for f in files)
        active = [f for f in files if not f.error and not os.path.exists(f.dest)]
        elapsed = time.time() - (self.started or time.time())
        return {"total": total, "done": done, "files": len(files), "left": len(active),
                "speed": round((done - self.base) / elapsed) if active and elapsed > 1 else None,
                "failed": list(self.failed), "busy": bool(active)}

    def active(self):
        return sum(1 for f in self.files.values() if not f.error and not os.path.exists(f.dest))

    def wait(self, report=None, every=2.0):
        while True:
            s = self.status()
            if report:
                report(s)
            if not s["busy"]:
                return s
            time.sleep(every)

    # ---- internals

    def _done(self):
        return sum(f.done_bytes for f in self.files.values())

    def _headers(self, url, extra=None):
        h = {"User-Agent": USER_AGENT, **(extra or {})}
        if self.token and urllib.parse.urlparse(url).hostname in ("huggingface.co", "hf.co"):
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _prepare(self, f):
        """Find the file's size and its download address (after Hugging Face's redirect), and pick up a .part."""
        os.makedirs(os.path.dirname(f.dest), exist_ok=True)
        req = urllib.request.Request(f.item["url"], headers=self._headers(f.item["url"], {"Range": "bytes=0-0"}))
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):  # gated repository: needs a login and an accepted license
                page = f.item["url"].split("/resolve/")[0]
                hint = ("your HF_TOKEN was refused: check it's a valid Read token and that you accepted the license"
                        if self.token else "set HF_TOKEN to a Hugging Face Read token (huggingface.co/settings/tokens)")
                raise IOError(f"HTTP {e.code}: this model requires accepting its license at {page} "
                              f"while logged in; then {hint} and re-run") from None
            raise
        with resp:
            rng = resp.headers.get("Content-Range", "")
            f.size = int(rng.rsplit("/", 1)[1]) if resp.status == 206 and "/" in rng else int(resp.headers.get("Content-Length", 0))
            f.url = resp.geturl()
        if not f.size:
            raise IOError("the server didn't say how big the file is")
        f.chunks = (f.size + CHUNK - 1) // CHUNK
        sidecar = f.part + ".done"
        if os.path.exists(f.part) and os.path.exists(sidecar):
            try:
                with open(sidecar, encoding="utf-8") as s:
                    f.done_chunks = set(json.load(s))
            except (OSError, ValueError):
                f.done_chunks = set()
        elif os.path.exists(f.part):  # a .part from the old one-connection download: its start is complete
            have = os.path.getsize(f.part)
            f.done_chunks = {i for i in range(f.chunks) if (i + 1) * CHUNK <= have or (i == f.chunks - 1 and have == f.size)}
        if not os.path.exists(f.part):
            open(f.part, "wb").close()
        if os.path.getsize(f.part) != f.size:
            with open(f.part, "r+b") as p:
                p.truncate(f.size)
        f.done_bytes = sum(self._chunk_len(f, i) for i in f.done_chunks)

    @staticmethod
    def _chunk_len(f, i):
        return min(CHUNK, f.size - i * CHUNK)

    def _worker(self):
        while True:
            f, i = self.chunks.get()
            if f.error:
                continue
            for attempt in range(5):
                try:
                    self._get_chunk(f, i)
                    break
                except Exception as e:
                    if attempt == 4:
                        self._fail(f, e)
                    elif isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                        try:  # the signed download address expired: ask Hugging Face for a new one
                            self._resolve(f)
                        except Exception:
                            pass
                    time.sleep(2 + 3 * attempt)
            else:
                continue
            with f.lock:
                f.done_chunks.add(i)
                complete = len(f.done_chunks) == f.chunks
                try:
                    with open(f.part + ".done", "w", encoding="utf-8") as s:
                        json.dump(sorted(f.done_chunks), s)
                except OSError:
                    pass
            if complete:
                self._finish(f)

    def _resolve(self, f):
        req = urllib.request.Request(f.item["url"], headers=self._headers(f.item["url"], {"Range": "bytes=0-0"}))
        with urllib.request.urlopen(req, timeout=60) as resp:
            f.url = resp.geturl()

    def _get_chunk(self, f, i):
        start, length = i * CHUNK, self._chunk_len(f, i)
        req = urllib.request.Request(f.url, headers=self._headers(f.url, {"Range": f"bytes={start}-{start + length - 1}"}))
        got = 0
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(f.part, "r+b") as out:
                if resp.status != 206 and length != f.size:
                    raise IOError("the server ignored the byte range")
                out.seek(start)
                while got < length:
                    block = resp.read(min(BLOCK, length - got))
                    if not block:
                        break
                    out.write(block)
                    got += len(block)
                    with f.lock:
                        f.done_bytes += len(block)
        except BaseException:
            with f.lock:
                f.done_bytes -= got  # this chunk starts over
            raise
        if got != length:
            with f.lock:
                f.done_bytes -= got
            raise IOError(f"chunk {i} ended early ({got} of {length} bytes)")

    def _finish(self, f):
        try:
            os.replace(f.part, f.dest)
            os.remove(f.part + ".done")
        except FileNotFoundError:
            pass

    def _fail(self, f, e):
        f.error = str(e)
        with self.lock:
            if f.item["name"] not in self.failed:
                self.failed.append(f.item["name"])
        print(f"\n  FAILED  {f.item['name']}: {e}", flush=True)


def main():
    profiles = load_profiles()

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", action="append", choices=sorted(profiles), help="profile to download (repeatable)")
    ap.add_argument("--comfy", default=os.path.join(ROOT, "ComfyUI"), help="ComfyUI folder (default: ./ComfyUI)")
    ap.add_argument("--connections", type=int, default=16, help="parallel connections (default 16)")
    ap.add_argument("--list", action="store_true", help="list profiles and exit")
    args = ap.parse_args()

    if args.list:
        for name, p in profiles.items():
            print(f"{name:12} {p['description']}\n{'':12} License: {p.get('license', 'unknown')}")
        return

    models_dir = os.path.join(args.comfy, "models")
    if not os.path.isdir(models_dir):
        sys.exit(f"ComfyUI models folder not found: {models_dir}\nRun the install script first, or pass --comfy.")

    names = args.profile or DEFAULT_PROFILES
    for name in names:
        print(f"[{name}] {profiles[name]['description']}\n  License: {profiles[name].get('license', 'unknown')}")
    d = Downloader(models_dir, args.connections)
    d.add(files_of(profiles, names))

    def report(s):
        if s["total"]:
            speed = f" @ {human(s['speed'])}/s" if s["speed"] else ""
            sys.stdout.write(f"\r  {human(s['done'])} / {human(s['total'])}, {s['left']} file(s) left{speed}   ")
            sys.stdout.flush()
    s = d.wait(report)
    sys.stdout.write("\n")
    if s["failed"]:
        sys.exit(f"\n{len(s['failed'])} file(s) failed: {', '.join(s['failed'])}. Re-run to resume.")
    print("All models present.")


if __name__ == "__main__":
    main()

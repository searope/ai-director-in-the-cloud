"""Download the model files listed in models.json into ComfyUI/models.

Usage:
    python setup/download_models.py                  # default: wan22-flf + interp + restyle
    python setup/download_models.py --profile interp
    python setup/download_models.py --list

Downloads resume if interrupted (partial files end in .part).
Set HF_TOKEN in the environment if Hugging Face asks you to log in.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "setup", "models.json")
DEFAULT_PROFILES = ["wan22-flf", "interp", "restyle"]
CHUNK = 8 * 1024 * 1024


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def download(url, dest):
    if os.path.exists(dest):
        print(f"  ok      {os.path.basename(dest)} (already present)")
        return
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": "keyframe-animator-setup"}
    if have:
        headers["Range"] = f"bytes={have}-"
    token = os.environ.get("HF_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:  # range not satisfiable: .part is already complete
            os.replace(part, dest)
            return
        if e.code in (401, 403):  # gated repository: needs a login and an accepted license
            page = url.split("/resolve/")[0]
            hint = ("your HF_TOKEN was refused: check it's a valid Read token and that you accepted the license"
                    if token else "set HF_TOKEN to a Hugging Face Read token (huggingface.co/settings/tokens)")
            raise IOError(f"HTTP {e.code}: this model requires accepting its license at {page} "
                          f"while logged in; then {hint} and re-run") from None
        raise

    if have and resp.status != 206:  # server ignored Range, start over
        have = 0
    total = int(resp.headers.get("Content-Length", 0)) + have
    mode = "ab" if have else "wb"
    done, start, last = have, time.time(), 0.0
    with open(part, mode) as f:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last > 1:
                speed = (done - have) / max(now - start, 1e-6)
                pct = f"{100 * done / total:5.1f}%" if total else ""
                sys.stdout.write(f"\r  get     {os.path.basename(dest)} {pct} {human(done)} @ {human(speed)}/s   ")
                sys.stdout.flush()
                last = now
    sys.stdout.write("\n")
    if total and done != total:
        raise IOError(f"incomplete download ({done} of {total} bytes); re-run to resume")
    os.replace(part, dest)


def main():
    with open(MANIFEST, encoding="utf-8") as f:
        profiles = json.load(f)["profiles"]

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", action="append", choices=sorted(profiles), help="profile to download (repeatable)")
    ap.add_argument("--comfy", default=os.path.join(ROOT, "ComfyUI"), help="ComfyUI folder (default: ./ComfyUI)")
    ap.add_argument("--list", action="store_true", help="list profiles and exit")
    args = ap.parse_args()

    if args.list:
        for name, p in profiles.items():
            print(f"{name:12} {p['description']}\n{'':12} License: {p.get('license', 'unknown')}")
        return

    models_dir = os.path.join(args.comfy, "models")
    if not os.path.isdir(models_dir):
        sys.exit(f"ComfyUI models folder not found: {models_dir}\nRun the install script first, or pass --comfy.")

    failed = []
    for name in args.profile or DEFAULT_PROFILES:
        print(f"[{name}] {profiles[name]['description']}\n  License: {profiles[name].get('license', 'unknown')}")
        for item in profiles[name]["files"]:
            target_dir = os.path.join(models_dir, item["dir"])
            os.makedirs(target_dir, exist_ok=True)
            try:
                download(item["url"], os.path.join(target_dir, item["name"]))
            except Exception as e:  # keep going, report at the end
                print(f"\n  FAILED  {item['name']}: {e}")
                failed.append(item["name"])

    if failed:
        sys.exit(f"\n{len(failed)} file(s) failed: {', '.join(failed)}. Re-run to resume.")
    print("\nAll models present.")


if __name__ == "__main__":
    main()

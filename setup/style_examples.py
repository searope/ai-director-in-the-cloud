"""Make the example pictures for each style (remote/static/styles/<id>-<scene>.jpg), shown when choosing a style.

A few base pictures in different settings (urban, portrait, nature, interior) are generated with FLUX.2 Klein 4B
(text to image) and kept as _base-<scene>.jpg; each is then redrawn in every style that has a restyle_prompt.
"photoreal" uses the base pictures themselves. Scene 1 is the style's card picture. Needs a running ComfyUI with
the default models.

    python setup/style_examples.py --pod                    # the app's running GPU pod (output/gpu_pod.json)
    python setup/style_examples.py --server http://127.0.0.1:8188
    python setup/style_examples.py --pod --only cyberpunk steampunk   # just these (e.g. after adding styles)
    python setup/style_examples.py --pod --scenes 2 3                  # just these scenes
"""

import argparse
import io
import json
import os
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import animate  # noqa: E402

OUT = os.path.join(ROOT, "remote", "static", "styles")
SCENES = {  # remote/server.py STYLE_SCENES must match the count
    1: ("A young woman in a yellow raincoat walks a small brown dog across a city street at dusk, a red bicycle "
        "leaning on a lamp post, shop windows glowing behind her, full body, natural photo."),  # urban
    2: ("Close-up portrait of an old fisherman with a short grey beard and a knitted blue cap, weathered face, "
        "looking into the camera, a harbour softly blurred behind him, natural photo."),  # portrait
    3: ("A wide mountain valley at sunrise: a winding river, a small wooden cabin with smoke from the chimney, "
        "a pine forest, snowy peaks and a few clouds, natural landscape photo."),  # nature
    4: ("Two children laughing at a kitchen table, one pouring milk into a glass, a ginger cat sitting on the "
        "counter, a bowl of fruit and potted plants, warm light from the window, natural photo."),  # interior
}
SEED = 20261020
WIDTH = 384  # example size: small enough for the app to load quickly


def save_jpg(png_bytes, path):
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    im = im.resize((WIDTH, round(WIDTH * im.height / im.width)), Image.LANCZOS)
    im.save(path, "JPEG", quality=82, optimize=True)


def keep_app_awake(url, token):
    """These requests go to ComfyUI directly, which the app doesn't see as work: tell it every minute
    that the GPU is in use, so it doesn't remove the pod after its idle minutes."""
    def ping():
        while True:
            try:
                req = urllib.request.Request(url + "/api/keepalive", data=b"{}", method="POST",
                                             headers={"X-Token": token, "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=20).close()
            except OSError as e:
                print("keepalive failed:", e)
            time.sleep(60)
    threading.Thread(target=ping, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", help="ComfyUI address")
    ap.add_argument("--pod", action="store_true", help="use the app's running Runpod pod")
    ap.add_argument("--only", nargs="*", help="style ids to (re)make; default: all")
    ap.add_argument("--scenes", nargs="*", type=int, default=list(SCENES), help="scene numbers; default: all")
    ap.add_argument("--app-dir", default=ROOT, help="the running app's folder (output/gpu_pod.json, remote/config.json)")
    ap.add_argument("--app-url", default="http://127.0.0.1:8700", help="the running app, kept from removing the pod")
    args = ap.parse_args()
    if args.pod:
        state = json.load(open(os.path.join(args.app_dir, "output", "gpu_pod.json")))
        if not state.get("pod"):
            raise SystemExit("No pod is running: start the GPU in the app first.")
        keep_app_awake(args.app_url, json.load(open(os.path.join(args.app_dir, "remote", "config.json")))["token"])
        animate.Comfy.headers = {"X-Gateway-Token": state["gateway_token"], "User-Agent": "ai-director/1.0"}
        server = f"https://{state['pod']['id']}-8700.proxy.runpod.net"
    else:
        server = args.server or "http://127.0.0.1:8188"
    comfy = animate.Comfy(server)
    comfy.ping()
    os.makedirs(OUT, exist_ok=True)
    cfg = {"image_model": "klein4b"}
    quiet = lambda *a, **k: None  # noqa: E731

    styles = animate.load_default_styles()
    todo = [k for k in (args.only or styles) if styles.get(k, {}).get("restyle_prompt")]
    for n in args.scenes:
        base_jpg = os.path.join(OUT, f"_base-{n}.jpg")
        if not os.path.exists(base_jpg):
            width, height = animate.KEYFRAME_SIZES["16:9"]
            graph = animate.build_text_to_image_graph(cfg, SCENES[n], SEED, width, height, f"style_examples/base{n}")
            out = comfy.wait(comfy.queue(graph), f"base picture {n}", quiet, lambda: False)
            from PIL import Image
            Image.open(io.BytesIO(comfy.fetch(out["save"]["images"][0]))).convert("RGB").save(base_jpg, "JPEG", quality=92)
        with open(base_jpg, "rb") as f:
            save_jpg(f.read(), os.path.join(OUT, f"photoreal-{n}.jpg"))
        base = comfy.upload_image(base_jpg, f"style_examples_base{n}.jpg")
        for k in todo:
            t = time.time()
            graph = animate.build_restyle_graph(cfg, base, styles[k]["restyle_prompt"], SEED, f"style_examples/{k}")
            out = comfy.wait(comfy.queue(graph), k, quiet, lambda: False)
            save_jpg(comfy.fetch(out["save"]["images"][0]), os.path.join(OUT, f"{k}-{n}.jpg"))
            print(f"scene {n} {k}: {time.time() - t:.0f}s")
    print(f"Examples in {OUT}")


if __name__ == "__main__":
    main()

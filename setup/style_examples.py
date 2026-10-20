"""Make the example image for each style (remote/static/styles/<id>.jpg), shown when choosing a style.

One base picture is generated with FLUX.2 Klein 4B (text to image), then redrawn in every style that has
a restyle_prompt; "photoreal" uses the base picture itself. Needs a running ComfyUI with the default models.

    python setup/style_examples.py --pod                    # the app's running GPU pod (output/gpu_pod.json)
    python setup/style_examples.py --server http://127.0.0.1:8188
    python setup/style_examples.py --pod --only cyberpunk steampunk   # just these (e.g. after adding styles)
"""

import argparse
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import animate  # noqa: E402

OUT = os.path.join(ROOT, "remote", "static", "styles")
BASE_PROMPT = ("A young woman in a yellow raincoat walks a small brown dog across a city street at dusk, a red bicycle "
               "leaning on a lamp post, shop windows glowing behind her, full body, natural photo.")
SEED = 20261020
WIDTH = 384  # example size: small enough for the app to load quickly


def save_jpg(png_bytes, path):
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    im = im.resize((WIDTH, round(WIDTH * im.height / im.width)), Image.LANCZOS)
    im.save(path, "JPEG", quality=82, optimize=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", help="ComfyUI address")
    ap.add_argument("--pod", action="store_true", help="use the app's running Runpod pod")
    ap.add_argument("--only", nargs="*", help="style ids to (re)make; default: all")
    args = ap.parse_args()
    if args.pod:
        state = json.load(open(os.path.join(ROOT, "output", "gpu_pod.json")))
        if not state.get("pod"):
            raise SystemExit("No pod is running: start the GPU in the app first.")
        animate.Comfy.headers = {"X-Gateway-Token": state["gateway_token"], "User-Agent": "ai-director/1.0"}
        server = f"https://{state['pod']['id']}-8700.proxy.runpod.net"
    else:
        server = args.server or "http://127.0.0.1:8188"
    comfy = animate.Comfy(server)
    comfy.ping()
    os.makedirs(OUT, exist_ok=True)
    cfg = {"image_model": "klein4b"}
    quiet = lambda *a, **k: None  # noqa: E731

    base_png = os.path.join(OUT, "_base.png")
    if not os.path.exists(base_png):
        width, height = animate.KEYFRAME_SIZES["16:9"]
        graph = animate.build_text_to_image_graph(cfg, BASE_PROMPT, SEED, width, height, "style_examples/base")
        out = comfy.wait(comfy.queue(graph), "base picture", quiet, lambda: False)
        with open(base_png, "wb") as f:
            f.write(comfy.fetch(out["save"]["images"][0]))
    with open(base_png, "rb") as f:
        save_jpg(f.read(), os.path.join(OUT, "photoreal.jpg"))
    base = comfy.upload_image(base_png, "style_examples_base.png")

    styles = animate.load_styles()
    todo = [k for k in (args.only or styles) if styles.get(k, {}).get("restyle_prompt")]
    for k in todo:
        t = time.time()
        graph = animate.build_restyle_graph(cfg, base, styles[k]["restyle_prompt"], SEED, f"style_examples/{k}")
        out = comfy.wait(comfy.queue(graph), k, quiet, lambda: False)
        save_jpg(comfy.fetch(out["save"]["images"][0]), os.path.join(OUT, f"{k}.jpg"))
        print(f"{k}: {time.time() - t:.0f}s")
    print(f"Examples in {OUT}")


if __name__ == "__main__":
    main()

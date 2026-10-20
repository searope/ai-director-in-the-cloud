"""AI review of the style examples: is the whole picture in the style, and is everything still there?

For each style, a sheet with every scene's base picture (BEFORE) next to its redraw (AFTER) goes to an AI
writer from remote/director.py (by default Claude Code on the subscription). Its findings go to
styles_review.json, which the app shows in the style's details as known issues ("faces stay photographic"),
so the user knows similar objects in their keyframes may not take the style well.

    python setup/style_review.py                       # every style with examples
    python setup/style_review.py --only knitted voxel  # just these
    python setup/style_review.py --jobs 3              # three reviews at a time
"""

import argparse
import concurrent.futures
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "remote"))
import animate  # noqa: E402
import director  # noqa: E402

DIR = os.path.join(ROOT, "remote", "static", "styles")
OUT = os.path.join(ROOT, "styles_review.json")
SCENES = ["city street", "close-up face", "landscape", "indoor scene"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["coverage", "strength", "unstyled", "replaced", "summary"],
    "properties": {
        "coverage": {"type": "integer", "minimum": 0, "maximum": 100},
        "strength": {"type": "string", "enum": ["strong", "clear", "weak", "none"]},
        "unstyled": {"type": "array", "items": {"type": "string"}},
        "replaced": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

PROMPT = """You check how well an image-editing model redrew pictures in a requested visual style.

The sheet has one row per scene: the original photo (BEFORE, left) and the model's redraw (AFTER, right).
The redraw must (1) put the WHOLE picture in the style (people, faces, hands, animals, objects, buildings,
ground, sky), and (2) keep the content: the same people, animals, objects and buildings in the same places,
nothing important removed, added or swapped for something else. Small simplifications that belong to the
style (fewer details in pixel art, flattened shapes in cubism) are fine.

- coverage: your estimate of the share of the picture area, over all scenes, that clearly shows the style
  (0-100). A picture that is still a photo with a colour tint has low coverage for a medium style
  (knitted, pixel art, watercolour); for a colour grade or lighting style (film looks), the grade covering
  everything counts as full coverage.
- strength: how unmistakably the style shows where it is applied.
- unstyled: kinds of things that stay in the original photographic look, as short generic phrases (2-5 words)
  a user can relate to their own pictures, e.g. "faces", "skin", "background buildings", "the sky", "metal objects",
  "small people in the distance". Empty if none.
- replaced: important content that was removed, added or replaced (short phrases). Colour and lighting
  changes that are part of the style are not replacements. E.g. "the shop windows became a painted
  sky", "the dog is missing in the landscape scene". Empty if none.
- summary: one sentence for the user, e.g. "Strong knitted look on people and animals; buildings and the
  street stay photographic." """


def make_sheet(style_id, path):
    from PIL import Image, ImageDraw, ImageFont
    w, h, label = 512, 288, 26
    rows = [(n, os.path.join(DIR, f"_base-{n}.jpg"), os.path.join(DIR, f"{style_id}-{n}.jpg")) for n in range(1, 5)]
    rows = [r for r in rows if os.path.exists(r[1]) and os.path.exists(r[2])]
    if not rows:
        return False
    sheet = Image.new("RGB", (2 * w + 24, len(rows) * (h + label + 8) + 8), (18, 18, 22))
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    for i, (n, before, after) in enumerate(rows):
        y = 8 + i * (h + label + 8)
        for j, (path_, name) in enumerate(((before, "BEFORE"), (after, "AFTER"))):
            x = 8 + j * (w + 8)
            d.text((x + 2, y + 3), f"{name} · {SCENES[n - 1]}", fill=(250, 204, 21) if j else (235, 235, 240), font=font)
            sheet.paste(Image.open(path_).convert("RGB").resize((w, h)), (x, y + label))
    sheet.save(path, "PNG")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="style ids")
    ap.add_argument("--writer", default="claude-code", help="writer id from remote/config.json")
    ap.add_argument("--jobs", type=int, default=2, help="reviews at a time")
    ap.add_argument("--app-dir", default=ROOT, help="folder with remote/config.json (the writers)")
    args = ap.parse_args()
    config = json.load(open(os.path.join(args.app_dir, "remote", "config.json"), encoding="utf-8"))
    writer = director.load_writers(config).get(args.writer)
    if not writer or not writer.ready:
        raise SystemExit(f"writer {args.writer!r} isn't set up")
    if writer.paid:
        raise SystemExit(f"{writer.label} is paid per use: choose a subscription writer for this many reviews")
    styles = animate.load_default_styles()
    todo = [k for k in (args.only or styles) if styles.get(k, {}).get("restyle_prompt")]
    results = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    lock = threading.Lock()

    def review(k):
        with tempfile.TemporaryDirectory(prefix="stylereview_") as tmp:
            sheet = os.path.join(tmp, "sheet.png")
            if not make_sheet(k, sheet):
                return k, None
            s = styles[k]
            user = (f"Requested style: {s['label']}: {s['video_prompt']}\n"
                    f"The redraw instruction was: {s['restyle_prompt']}\nReview the sheet.")
            r = writer.write(PROMPT, user, SCHEMA, image=sheet)
        r = {"coverage": int(r.get("coverage", 0)), "strength": r.get("strength", ""), "unstyled": r.get("unstyled", [])[:8],
             "replaced": r.get("replaced", [])[:8], "summary": str(r.get("summary", ""))[:300],
             "prompt": s["restyle_prompt"], "checked": time.strftime("%Y-%m-%d")}
        with lock:
            results[k] = r
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=1, ensure_ascii=False)
        return k, r

    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(review, k) for k in todo]):
            try:
                k, r = fut.result()
            except Exception as e:
                print("failed:", e, flush=True)
                continue
            if r:
                print(f"{k:24s} {r['coverage']:3d}% {r['strength']:7s} unstyled: {', '.join(r['unstyled']) or '-'}"
                      f" | replaced: {', '.join(r['replaced']) or '-'}", flush=True)
    print(f"Reviews in {OUT}")


if __name__ == "__main__":
    main()

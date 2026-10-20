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

# The main features of each test scene: every one must be redrawn in the style, and none removed or replaced.
FEATURES = {
    1: ["the woman in the yellow raincoat", "the woman's face", "the brown dog", "the red bicycle", "the lamp post",
        "the shop windows", "the passers-by", "the pavement and street"],
    2: ["the old man's face", "his eyes", "the grey beard", "the blue knitted cap", "the harbour background"],
    3: ["the river", "the wooden cabin", "the chimney smoke", "the pine forest", "the snowy peaks", "the sky and clouds"],
    4: ["the two children", "the children's faces", "the milk being poured", "the ginger cat", "the fruit bowl",
        "the potted plants", "the window"],
}
STATUSES = ["styled", "partly", "unstyled", "missing", "replaced"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["features", "strength", "summary"],
    "properties": {
        "features": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["scene", "feature", "status", "note"],
            "properties": {"scene": {"type": "integer"}, "feature": {"type": "string"},
                           "status": {"type": "string", "enum": STATUSES}, "note": {"type": "string"}}}},
        "strength": {"type": "string", "enum": ["strong", "clear", "weak", "none"]},
        "summary": {"type": "string"},
    },
}

PROMPT = """You check how well an image-editing model redrew test pictures in a requested visual style.

The sheet has one row per scene: the original photo (BEFORE, left) and the model's redraw (AFTER, right).
The message lists each scene's main features. Judge EVERY listed feature in its scene's AFTER picture,
comparing with BEFORE:
- styled: clearly drawn in the requested style (its medium, texture, line, palette or light);
- partly: some of the style, but it still looks noticeably like the original photo;
- unstyled: still looks like the original photo (at most a colour tint), not in the style;
- missing: removed or no longer recognisable;
- replaced: turned into something else, or moved or rearranged substantially.
Small simplifications that belong to the style (fewer details in pixel art, flattened shapes in cubism) are
fine and count as styled. Colour and lighting changes that are part of the style are not replacements. For a
colour-grade or lighting style (film looks), a feature counts as styled when the grade or light covers it.
note: a few words on what you see, especially for anything not "styled", e.g. "skin stays photographic".

strength: how unmistakably the style shows where it is applied.
summary: one sentence for the user, e.g. "Strong knitted look on people and animals; buildings and the
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
            scenes = [n for n in FEATURES if os.path.exists(os.path.join(DIR, f"{k}-{n}.jpg"))]
            rows = "\n".join(f"Row {i + 1}, {SCENES[n - 1]} (scene {n}): " + "; ".join(FEATURES[n]) for i, n in enumerate(scenes))
            user = (f"Requested style: {s['label']}: {s['video_prompt']}\n"
                    f"The redraw instruction was: {s['restyle_prompt']}\n\nMain features to judge, by scene:\n{rows}\n"
                    "Use the scene numbers given here. Review the sheet.")
            r = writer.write(PROMPT, user, SCHEMA, image=sheet)
        feats = [f for f in r.get("features") or [] if isinstance(f, dict) and f.get("status") in STATUSES]
        score = {"styled": 1, "partly": 0.5}
        coverage = round(100 * sum(score.get(f["status"], 0) for f in feats) / max(1, len(feats)))
        generic = lambda f: f["feature"].replace("the ", "", 1)  # noqa: E731
        unstyled = list(dict.fromkeys(generic(f) for f in feats if f["status"] in ("unstyled", "partly")))
        replaced = [f"{generic(f)} {f['status']} ({SCENES[f['scene'] - 1] if 1 <= f.get('scene', 0) <= 4 else '?'})"
                    + (f": {f['note']}" if f.get("note") else "") for f in feats if f["status"] in ("missing", "replaced")]
        r = {"coverage": coverage, "strength": r.get("strength", ""), "unstyled": unstyled[:10], "replaced": replaced[:10],
             "passed": bool(feats) and all(f["status"] == "styled" for f in feats),
             "features": feats, "summary": str(r.get("summary", ""))[:300],
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
                print(f"{k:24s} {'PASS' if r['passed'] else 'fail'} {r['coverage']:3d}% {r['strength']:7s}"
                      f" unstyled: {', '.join(r['unstyled']) or '-'} | missing/replaced: {'; '.join(r['replaced']) or '-'}", flush=True)
    done = {k: results[k] for k in todo if k in results}
    failed = sorted((v["coverage"], k) for k, v in done.items() if not v.get("passed"))
    print(f"\n{len(done) - len(failed)} of {len(done)} styles pass (every main feature present and styled).")
    for cov, k in failed:
        print(f"  {k:24s} {cov:3d}%  unstyled: {', '.join(done[k]['unstyled']) or '-'}"
              f" | missing/replaced: {'; '.join(done[k]['replaced']) or '-'}")
    print(f"Reviews in {OUT}")


if __name__ == "__main__":
    main()

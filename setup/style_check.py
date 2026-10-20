"""Check style examples against their base pictures: is the whole picture styled, and is everything still there?

Each example (remote/static/styles/<style>-<scene>.jpg) is compared with its base picture (_base-<scene>.jpg)
on a 4x4 grid of tiles:
- styled: how much each tile's colour and fine texture changed. A tile that barely changed means the style
  was applied to only part of the picture ("partial").
- kept: how well each tile's shapes (blurred edges) still line up with the base. A tile whose shapes no longer
  match means something was removed, replaced or moved ("replaced").

    python setup/style_check.py                     # every example; report sorted by problems
    python setup/style_check.py --only knitted voxel --sheet out.jpg   # before/after sheet as well
"""

import argparse
import glob
import json
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "remote", "static", "styles")
GRID = 4
SIZE = (384, 216)
STYLED_MIN = 0.10   # tile change below this counts as unstyled
KEPT_MIN = 0.30     # tile shape correlation below this counts as replaced


def _load(path):
    return Image.open(path).convert("RGB").resize(SIZE, Image.LANCZOS)


def _edges(im):
    g = im.convert("L").filter(ImageFilter.GaussianBlur(2)).filter(ImageFilter.FIND_EDGES)
    return np.asarray(g.filter(ImageFilter.GaussianBlur(5)), np.float32)  # blurred: small shifts don't count


def _texture(im):
    a = np.asarray(im.convert("L"), np.float32) / 255
    lap = np.abs(4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:])
    return np.pad(lap, 1)


def _tile_stats(lab, tex, edges):
    """What a tile looks like, independent of exactly where things are: colour and brightness histograms,
    texture energy and edge density."""
    ab = np.histogram2d(lab[..., 1].ravel(), lab[..., 2].ravel(), bins=8, range=[[0, 1], [0, 1]])[0].ravel()
    lum = np.histogram(lab[..., 0], bins=8, range=(0, 1))[0]
    return {"ab": ab / ab.sum(), "lum": lum / lum.sum(), "tex": float(tex.mean()) + 1e-3,
            "edge": float((edges > 0.08).mean())}


def compare(base_path, styled_path):
    b, s = _load(base_path), _load(styled_path)
    bl = np.asarray(b.convert("LAB"), np.float32) / 255
    sl = np.asarray(s.convert("LAB"), np.float32) / 255
    bt, st = _texture(b), _texture(s)
    be, se = _edges(b), _edges(s)
    h, w = SIZE[1] // GRID, SIZE[0] // GRID
    change, kept = [], []
    for r in range(GRID):
        for c in range(GRID):
            t = (slice(r * h, (r + 1) * h), slice(c * w, (c + 1) * w))
            x, y = _tile_stats(bl[t], bt[t], bt[t]), _tile_stats(sl[t], st[t], st[t])
            change.append(float(np.abs(x["ab"] - y["ab"]).sum() / 2 + np.abs(x["lum"] - y["lum"]).sum() / 2
                                + abs(np.log(y["tex"] / x["tex"])) * 0.5 + abs(y["edge"] - x["edge"]) * 2))
            e1, e2 = be[t].ravel(), se[t].ravel()
            kept.append(1.0 if e1.std() < 2 or e2.std() < 2 else float(np.corrcoef(e1, e2)[0, 1]))
    change, kept = np.array(change), np.array(kept)
    return {"styled": round(float(change.mean()), 3), "unstyled_tiles": int((change < STYLED_MIN).sum()),
            "kept": round(float(np.median(kept)), 3), "replaced_tiles": int((kept < KEPT_MIN).sum()),
            "tiles_change": change.round(3).tolist(), "tiles_kept": kept.round(2).tolist()}


def verdict(m):
    problems = []
    if m["unstyled_tiles"] >= 4:
        problems.append(f"partial ({m['unstyled_tiles']}/16 tiles unstyled)")
    if m["styled"] < STYLED_MIN:
        problems.append("barely styled")
    if m["replaced_tiles"] >= 3:
        problems.append(f"replaced ({m['replaced_tiles']}/16 tiles changed shape)")
    return problems


def sheet(rows, out):
    """rows: (label, base_path, styled_path, metrics); before and after side by side, problems in red."""
    w, h, cols = 320, 180, 2
    font = ImageFont.truetype("arial.ttf", 14) if os.name == "nt" else ImageFont.load_default()
    n = (len(rows) + cols - 1) // cols
    im = Image.new("RGB", (cols * (2 * w + 12), n * (h + 22)), (20, 20, 24))
    d = ImageDraw.Draw(im)
    for i, (label, bp, sp, m) in enumerate(rows):
        x, y = (i % cols) * (2 * w + 12), (i // cols) * (h + 22)
        im.paste(_load(bp).resize((w, h)), (x, y + 22))
        im.paste(_load(sp).resize((w, h)), (x + w, y + 22))
        probs = verdict(m)
        d.text((x + 3, y + 3), f"{label}  styled {m['styled']:.2f} kept {m['kept']:.2f}  " + "; ".join(probs),
               fill=(248, 113, 113) if probs else (250, 204, 21), font=font)
    im.save(out, quality=85)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="style ids")
    ap.add_argument("--dir", default=DIR, help="folder with the examples and _base-<scene>.jpg")
    ap.add_argument("--sheet", help="write a before/after sheet (JPG) of the checked examples")
    ap.add_argument("--json", help="write the metrics as JSON")
    args = ap.parse_args()
    results, rows = {}, []
    for path in sorted(glob.glob(os.path.join(args.dir, "*-[0-9].jpg"))):
        name = os.path.basename(path)[:-4]
        style, scene = name.rsplit("-", 1)
        if style.startswith("_") or style == "photoreal" or (args.only and style not in args.only):
            continue
        base = os.path.join(args.dir, f"_base-{scene}.jpg")
        if not os.path.exists(base):
            continue
        m = compare(base, path)
        results[name] = m
        rows.append((name, base, path, m))
    bad = sorted(((verdict(m), k, m) for k, m in results.items() if verdict(m)), key=lambda t: -len(t[0]))
    print(f"{len(results)} examples checked, {len(bad)} with problems")
    for probs, k, m in bad:
        print(f"  {k:28s} styled {m['styled']:.2f}  kept {m['kept']:.2f}  {'; '.join(probs)}")
    if args.sheet:
        sheet(rows, args.sheet)
    if args.json:
        json.dump(results, open(args.json, "w"), indent=1)


if __name__ == "__main__":
    main()

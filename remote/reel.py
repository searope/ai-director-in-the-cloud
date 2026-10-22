"""Lantern quote reels: a finished video as a 1080x1920 reel with its quote over it.

The Lantern look: the picture fills the vertical frame (a landscape video sits on night blue instead),
the quote appears in parts in a serif at the lower third, the accent parts in lantern amber, each
fading in and out on its own times. A small lantern mark stays in a corner, and the reel ends on the
lantern glowing on night blue. Text is drawn with Pillow and laid over with ffmpeg, so any ffmpeg build
works (no drawtext/freetype needed).

parts: [{"text": "the moment you stop", "start": 1.0, "end": 6.5, "accent": False}, ...] in seconds.
A part that starts while earlier ones are still showing goes under them, so nothing jumps.
"""

import os
import re
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from soundtrack import ffmpeg_exe, probe

W, H, FPS = 1080, 1920, 30
NIGHT = (15, 24, 44)          # the night-blue background
TEXT = (238, 241, 248)        # moonlight white
AMBER = (250, 199, 117)       # lantern light, for the accent parts
GLASS = ((255, 236, 190), (239, 159, 39))  # the lantern's glow, centre to edge
FRAME = (243, 230, 200)       # the lantern's frame
TEXT_PAD = 40                 # room around a part's lines for its shadow and glow
FADE = 0.6                    # seconds for each part to fade in and out
END_SECONDS = 2.2             # the closing lantern card
MAX_PARTS = 8

FONTS = [r"C:\Windows\Fonts\georgia.ttf", r"C:\Windows\Fonts\cambria.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
         "/System/Library/Fonts/Supplemental/Georgia.ttf"]


def _font(size):
    for path in FONTS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def clean_parts(raw, duration=None):
    """Validated parts, sorted by start; times are clamped to the video's length."""
    parts = []
    for p in (raw or [])[:MAX_PARTS]:
        text = " ".join(str(p.get("text") or "").split())[:160]
        try:
            start, end = max(0.0, float(p.get("start", 0))), float(p.get("end", 0))
        except (TypeError, ValueError):
            raise ValueError("each line needs a start and an end time in seconds") from None
        if duration:
            start, end = min(start, duration), min(end, duration)
        if text and end - start >= 0.5:
            parts.append({"text": text, "start": round(start, 2), "end": round(end, 2), "accent": bool(p.get("accent"))})
    return sorted(parts, key=lambda p: p["start"])


# ---------------------------------------------------------------- pictures

def lantern_mark(size, glow=True):
    """The Lantern logo: a small glowing lantern (handle, cap, glass with a flame, base), RGBA, size x size.
    With glow, the lantern takes the middle of the picture and a warm halo the rest."""
    if glow:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        hd, u = ImageDraw.Draw(halo), size / 100
        hd.ellipse([16 * u, 18 * u, 84 * u, 86 * u], fill=AMBER + (150,))        # wide, soft
        hd.ellipse([30 * u, 32 * u, 70 * u, 72 * u], fill=(255, 226, 160, 230))  # brighter near the glass
        img.alpha_composite(halo.filter(ImageFilter.GaussianBlur(9 * u)))
        inner = round(size * 0.66)
        img.alpha_composite(lantern_mark(inner, glow=False), ((size - inner) // 2, (size - inner) // 2))
        return img
    s = size / 100  # drawn on a 100-unit grid
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(1, round(3 * s))
    d.arc([40 * s, 6 * s, 60 * s, 26 * s], 180, 360, fill=FRAME, width=w)         # handle
    d.polygon([(36 * s, 22 * s), (64 * s, 22 * s), (70 * s, 32 * s), (30 * s, 32 * s)], fill=FRAME)  # cap
    glass = [33 * s, 32 * s, 67 * s, 78 * s]
    for i in range(12, 0, -1):  # the glass: light warmest at the flame
        t = i / 12
        c = tuple(round(GLASS[0][k] * (1 - t) + GLASS[1][k] * t) for k in range(3))
        inset = (1 - t) * 14 * s
        d.rounded_rectangle([glass[0] + inset, glass[1] + inset, glass[2] - inset, glass[3] - inset], radius=8 * s, fill=c)
    d.rounded_rectangle(glass, radius=8 * s, outline=FRAME, width=w)
    d.ellipse([44.5 * s, 52 * s, 55.5 * s, 66 * s], fill=(255, 250, 235))          # flame: a round base...
    d.polygon([(44.8 * s, 57 * s), (55.2 * s, 57 * s), (50 * s, 43 * s)], fill=(255, 250, 235))  # ...and a tip
    d.polygon([(30 * s, 78 * s), (70 * s, 78 * s), (64 * s, 88 * s), (36 * s, 88 * s)], fill=FRAME)  # base
    return img


def _wrap(text, font, width, draw):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    return lines + ([line] if line else [])


def text_image(text, accent, size=66):
    """One part as a cropped RGBA image: centred serif lines with a soft shadow (and a warm glow when accented)."""
    font = _font(size)
    probe_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = _wrap(text, font, W - 200, probe_draw)
    step, pad = round(size * 1.32), TEXT_PAD
    img = Image.new("RGBA", (W, step * len(lines) + pad * 2), (0, 0, 0, 0))
    colour = AMBER if accent else TEXT

    def draw_lines(layer, fill):
        d = ImageDraw.Draw(layer)
        for i, line in enumerate(lines):
            x = (W - d.textlength(line, font=font)) / 2
            d.text((x, pad + i * step), line, font=font, fill=fill)
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_lines(shadow, (5, 8, 18, 200))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(9)))
    if accent:
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw_lines(glow, AMBER + (120,))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(14)))
    draw_lines(img, colour + (255,))
    return img


def _shade():
    """A dark wash over the lower part of a full-frame picture so the text reads on anything."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top = int(H * 0.48)
    for y in range(top, H):
        d.line([(0, y), (W, y)], fill=NIGHT + (round(200 * ((y - top) / (H - top)) ** 1.4),))
    return img


def layout(parts):
    """y position of each part: the first at the lower third, later ones under the parts still showing."""
    top, gap, ys, heights = int(H * 0.64), 14 - 2 * TEXT_PAD, [], []  # the padding overlaps
    for i, p in enumerate(parts):
        img_h = text_image(p["text"], p["accent"]).height
        showing = [j for j in range(i) if parts[j]["start"] <= p["start"] < parts[j]["end"]]
        y = max([ys[j] + heights[j] + gap for j in showing], default=top)
        ys.append(min(y, H - img_h - 120))
        heights.append(img_h)
    return ys


# ---------------------------------------------------------------- the reel

def compose(src, out, parts, logo=True, end_card=True):
    """Write the reel of video `src` to `out` (mp4). Returns the reel's length in seconds."""
    duration, has_audio = probe(src)
    if not duration:
        raise ValueError("couldn't read the video's length")
    parts = clean_parts(parts, duration)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", src]
        # a portrait picture covers the whole frame; a landscape one sits a little above centre on night blue
        fit = "increase,crop={W}:{H}" if _is_portrait(src) else "decrease"
        graph = [
            f"color=c=0x{bytes(NIGHT).hex()}:s={W}x{H}:r={FPS}:d={duration:.3f}[bg]",
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio={fit.format(W=W, H=H)},setsar=1,fps={FPS}[pic]",
            "[bg][pic]overlay=x=(W-w)/2:y='if(gte(h,H*0.8),(H-h)/2,(H-h)/2-H*0.12)':shortest=1[v0]",
        ]
        n, last = 1, "v0"

        def add_image(img, name):
            nonlocal n
            path = os.path.join(tmp, name)
            img.save(path)
            cmd.extend(["-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.3f}", "-i", path])
            n += 1
            return n - 1

        k = add_image(_shade(), "shade.png")
        graph.append(f"[{last}][{k}:v]overlay=0:0[v{k}]")
        last = f"v{k}"
        for i, (p, y) in enumerate(zip(parts, layout(parts))):
            k = add_image(text_image(p["text"], p["accent"]), f"part{i}.png")
            fade_out = max(p["start"] + FADE, p["end"] - FADE)
            graph.append(f"[{k}:v]format=rgba,fade=t=in:st={p['start']:.2f}:d={FADE}:alpha=1,"
                         f"fade=t=out:st={fade_out:.2f}:d={FADE}:alpha=1[t{k}]")
            graph.append(f"[{last}][t{k}]overlay=0:{y}:format=auto[v{k}]")
            last = f"v{k}"
        if logo:
            k = add_image(lantern_mark(150), "mark.png")
            graph.append(f"[{k}:v]format=rgba,colorchannelmixer=aa=0.85[m{k}]")
            graph.append(f"[{last}][m{k}]overlay=W-w-28:36:format=auto[v{k}]")
            last = f"v{k}"
        graph.append(f"[{last}]format=yuv420p[main]")
        if end_card:
            card = Image.new("RGBA", (W, H), NIGHT + (255,))
            mark = lantern_mark(620)
            card.alpha_composite(mark, ((W - 620) // 2, (H - 620) // 2 - 80))
            path = os.path.join(tmp, "card.png")
            card.convert("RGB").save(path)
            cmd.extend(["-loop", "1", "-framerate", str(FPS), "-t", f"{END_SECONDS}", "-i", path])
            k, n = n, n + 1
            graph.append(f"[{k}:v]format=yuv420p,fade=t=in:st=0:d=0.8,fade=t=out:st={END_SECONDS - 0.5}:d=0.5[card]")
        audio = []
        if has_audio:
            graph.append(f"[0:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{duration:.3f},"
                         f"afade=t=out:st={max(0, duration - 0.8):.2f}:d=0.8[a0]")
            audio = ["[a0]"]
            if end_card:
                cmd.extend(["-f", "lavfi", "-t", f"{END_SECONDS}", "-i", "anullsrc=r=48000:cl=stereo"])
                graph.append(f"[{n}:a]anull[a1]")
                audio.append("[a1]")
        if end_card:
            graph.append(f"[main]{audio[0] if audio else ''}[card]{audio[1] if audio else ''}"
                         f"concat=n=2:v=1:a={1 if audio else 0}[out]" + ("[aout]" if audio else ""))
        else:
            graph.append("[main]null[out]")
            if audio:
                graph.append("[a0]anull[aout]")
        cmd += ["-filter_complex", ";".join(graph), "-map", "[out]"]
        cmd += (["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"] if audio else [])
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode:
            raise RuntimeError("ffmpeg: " + (r.stderr.strip()[-600:] or "the reel failed"))
    return duration + (END_SECONDS if end_card else 0)


def _is_portrait(src):
    """True when the video is taller than about 4:5, so it can fill the 9:16 frame without losing much."""
    err = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", src], capture_output=True, text=True).stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    return bool(m) and int(m[1]) / int(m[2]) <= 0.8

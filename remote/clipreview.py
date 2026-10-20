"""Clip review: an AI model looks at a rendered clip for visual problems and suggests a better prompt.

The clip is shown to the model as one contact sheet: the intended start and end keyframes on top,
then frames sampled evenly through the clip, each labelled with its time. Any writer from
director.py can review it: Claude Code reads the sheet with its Read tool (no other tools),
the Anthropic API and OpenAI-compatible endpoints get the image in the request, and a command
writer gets the file's path in its prompt.
"""

import os
import tempfile

SHEET_FRAMES = 12   # frames sampled from the clip
CELL_W = 360        # width of one frame on the sheet, px

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "issues", "style_rating", "style_notes", "motion_prompt", "keyframe_advice"],
    "properties": {
        "verdict": {"type": "string", "enum": ["good", "minor", "redo"]},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["when", "what", "severity"],
            "properties": {
                "when": {"type": "string"},
                "what": {"type": "string"},
                "severity": {"type": "string", "enum": ["minor", "major"]},
            },
        }},
        "style_rating": {"type": "string", "enum": ["strong", "good", "weak", "lost"]},
        "style_notes": {"type": "string"},
        "motion_prompt": {"type": "string"},
        "keyframe_advice": {"type": "string"},
    },
}

SYSTEM_PROMPT = """You review short AI-generated video clips for visual problems and write a better prompt.

How the clip was made: a video model (MiniMax H3 or Wan 2.2, image-to-video with first and last frame)
animated between a START keyframe and an END keyframe, following a short motion prompt.
You see one contact sheet: the two keyframes on top, then frames sampled evenly through the
clip, each labelled with its time in seconds.

Compare START and END first: what changes between them (pose, objects, camera angle, height, distance)?
A subject seen from a different angle can look different (e.g. hair falling differently) without
actually being different; don't call that a keyframe mismatch.

Look for what a viewer would find wrong or weird:
- faces, hands, fingers or bodies that morph, melt, duplicate or change identity between frames;
- objects that appear, vanish, merge or change shape; text or logos that warp;
- textures or backgrounds that boil, smear or flicker; sudden jumps between frames;
- motion that doesn't match the prompt, or nothing happening; the clip drifting away from the
  keyframes (it should start like START and end like END);
- physics that look wrong (things floating, passing through each other).
Only report what you can actually see in the frames. Small frame-to-frame differences that a
viewer wouldn't notice are fine.

Then write `motion_prompt`: a better motion prompt for the same two keyframes that avoids the
problems: describe one clear, simple action, what stays still, and the camera (e.g. "static
camera"). The clip must end exactly on END, so the camera has to get from START's viewpoint to END's:
if they differ in angle, height or distance, describe that camera move explicitly and early (e.g. "the
camera slowly rises and moves closer, ending looking down at him from above"). Never ask for a static
camera or "no reframing" when the keyframes' framing differs: the model then morphs the subject (hair,
face, objects) to reach END instead of moving the camera. Name the things that must keep their shape. Keep it under about 120 words. Video models follow plain
physical descriptions better than abstract ones; describing how something moves works better
than listing what must not happen.

If the real cause is in the keyframes (START and END too different for one smooth move, an
object missing from or added in one of them, a face or eye colour that differs, a bad face in a
keyframe), say what to change in `keyframe_advice`; otherwise leave it "". The app always needs
both keyframes, so the options are: remake END as a small edit of START (say exactly what the
edit should change and keep), use START as both keyframes (a clip that returns to its start), or
split the move into two shots with a middle keyframe.

Judge the visual style too (the requested style is given in the message):
- do the keyframes really look like that style, or only partly (e.g. a photo with a filter)?
- when ORIGINAL keyframes are shown, the START and END keyframes were redrawn from them in the style:
  each redrawn keyframe should keep its original's pose, layout and objects, and both should share
  the same style;
- does the clip keep the style from start to end, or drift toward a photo or 3D look, or flicker
  between looks?
style_rating: "strong" (unmistakably that style throughout), "good" (clearly the style, small lapses),
"weak" (only partly, or inconsistent), or "lost" (the clip leaves the style). style_notes: one or two
sentences on what works and what doesn't. If the style is weak or lost, put style words into
motion_prompt (e.g. "watercolor painting, visible paper texture, soft bleeding pigments, the whole
clip stays a painting") and say in keyframe_advice if the redraw itself is the problem.

verdict: "good" (nothing a viewer would notice), "minor" (small flaws, usable), or "redo".
summary: one or two sentences."""


def _frames(video_path, count):
    """`count` frames spread evenly through the clip, with their times in seconds."""
    import av
    with av.open(video_path) as container:
        stream = container.streams.video[0]
        frames = [f.to_image() for f in container.decode(stream)]
        fps = float(stream.average_rate or 16)
    if not frames:
        raise ValueError("the clip has no frames")
    count = min(count, len(frames))
    picks = [round(i * (len(frames) - 1) / max(1, count - 1)) for i in range(count)]
    return [(frames[i], i / fps) for i in picks]


def make_sheet(video_path, keyframe_paths, out_path, count=SHEET_FRAMES, original_paths=()):
    """Write the contact sheet (PNG) and return its path. original_paths: the keyframes before a style
    redraw, shown next to the redrawn START and END."""
    from PIL import Image, ImageDraw, ImageFont
    frames = _frames(video_path, count)
    w = CELL_W
    h = round(w * frames[0][0].height / frames[0][0].width)
    cols = 4
    rows = (len(frames) + cols - 1) // cols
    label_h, gap = 26, 8
    keys = [Image.open(p).convert("RGB") for p in keyframe_paths[:2]]
    keys += [Image.open(p).convert("RGB") for p in list(original_paths)[:2]]
    top = (label_h + h + gap) if keys else 0
    sheet = Image.new("RGB", (cols * w + (cols + 1) * gap, top + rows * (label_h + h + gap) + gap), (18, 18, 22))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()

    def cell(img, x, y, label, color=(235, 235, 240)):
        draw.text((x + 2, y + 3), label, fill=color, font=font)
        sheet.paste(img.resize((w, h)), (x, y + label_h))

    for i, (img, label) in enumerate(zip(keys, ("START keyframe", "END keyframe", "ORIGINAL of START", "ORIGINAL of END"))):
        cell(img, gap + i * (w + gap), gap, label, (250, 204, 21))
    for i, (img, t) in enumerate(frames):
        r, c = divmod(i, cols)
        cell(img, gap + c * (w + gap), top + gap + r * (label_h + h + gap), f"{t:.2f} s")
    sheet.save(out_path, "PNG")
    return out_path


def user_prompt(motion_prompt, seconds, style=None, redrawn=False):
    text = f"The motion prompt used was: \"{motion_prompt}\"\nThe clip is {seconds:.1f} seconds long.\n"
    if style:
        text += f"Requested style: {style['label']}: {style['description']}\n"
        text += ("The START and END keyframes were redrawn in this style from the ORIGINAL keyframes shown next to them.\n"
                 if redrawn else "The keyframes were not redrawn: they were used as they are.\n")
    return text + "\nReview the clip on the contact sheet and write the improved motion prompt."


def duration(video_path):
    import av
    with av.open(video_path) as container:
        stream = container.streams.video[0]
        if stream.duration and stream.time_base:
            return float(stream.duration * stream.time_base)
        return float(container.duration or 0) / 1_000_000


def review(writer, video_path, keyframe_paths, motion_prompt, style=None, original_paths=()):
    """Ask a writer (from director.load_writers) to review a clip. Returns the parsed review.
    style: {"label", "description"} of the requested style; original_paths: the keyframes before a redraw."""
    with tempfile.TemporaryDirectory(prefix="clipreview_") as tmp:
        sheet = make_sheet(video_path, keyframe_paths, os.path.join(tmp, "sheet.png"), original_paths=original_paths)
        prompt = user_prompt(motion_prompt, duration(video_path), style, bool(original_paths))
        return writer.write(SYSTEM_PROMPT, prompt, REVIEW_SCHEMA, image=sheet)


# ---------------------------------------------------------------- checking redrawn keyframes before a render

KEYFRAME_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok", "score", "problems", "advice"],
    "properties": {
        "ok": {"type": "boolean"},
        "score": {"type": "integer", "enum": list(range(1, 11))},
        "problems": {"type": "array", "items": {"type": "string"}},
        "advice": {"type": "string"},
    },
}

KEYFRAME_PROMPT = """You check keyframes that were redrawn in a visual style before a video is rendered from them.

The sheet shows each ORIGINAL keyframe (top row) above its REDRAWN version (bottom row), in order.
A video model will animate from redrawn keyframe 1 to 2 (and so on), so anything that differs between
the redrawn keyframes but not between the originals will turn into a morph in the video.

Check:
- the same people look the same in every redrawn keyframe: hairstyle, hair length and colour, face,
  skin, clothes and their colours and logos. Compare the redrawn keyframes with each other, and each
  with its original: a different camera angle in the originals is fine, a redesigned person is not;
- each redrawn keyframe keeps its original's pose, composition, camera framing and objects (nothing
  added, removed or duplicated, e.g. a second copy of an object);
- the requested style is applied, and equally, to every redrawn keyframe;
- no artifacts: borders or white strips at the edges, cut-off parts, smeared or melted areas.

ok: true only if the keyframes can be animated without visible morphing of people or objects.
score: 1-10 for how well they meet all of the above (10 = perfect).
problems: short, concrete descriptions, e.g. "keyframe 2: hair is long and golden, keyframe 1: short
and ash-blond". Empty when ok.
advice: one or two sentences to add to the redraw instruction to fix the problems, naming the exact
look to keep, e.g. "Draw the boy with short, messy, light-blond hair and the same round face in both
pictures." Empty when ok."""


def keyframe_sheet(original_paths, redrawn_paths, out_path, cell_w=300):
    from PIL import Image, ImageDraw, ImageFont
    rows = [("ORIGINAL", original_paths), ("REDRAWN", redrawn_paths)]
    ims = [[Image.open(p).convert("RGB") for p in paths] for _, paths in rows]
    h = round(cell_w * ims[0][0].height / ims[0][0].width)
    label_h, gap, n = 26, 8, max(len(r) for r in ims)
    sheet = Image.new("RGB", (n * (cell_w + gap) + gap, 2 * (label_h + h + gap) + gap), (18, 18, 22))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    for r, ((label, _), row) in enumerate(zip(rows, ims)):
        y = gap + r * (label_h + h + gap)
        for i, im in enumerate(row):
            x = gap + i * (cell_w + gap)
            draw.text((x + 2, y + 3), f"{label} {i + 1}", fill=(250, 204, 21) if r else (235, 235, 240), font=font)
            sheet.paste(im.resize((cell_w, h)), (x, y + label_h))
    sheet.save(out_path, "PNG")
    return out_path


def check_keyframes(writer, original_paths, redrawn_paths, style=None):
    """Ask a writer whether redrawn keyframes are consistent enough to animate. Returns ok/score/problems/advice."""
    with tempfile.TemporaryDirectory(prefix="kfcheck_") as tmp:
        sheet = keyframe_sheet(original_paths, redrawn_paths, os.path.join(tmp, "keyframes.png"))
        prompt = (f"Requested style: {style['label']}: {style['description']}\n" if style else "") + \
            f"There are {len(redrawn_paths)} keyframes. Check the redrawn keyframes."
        return writer.write(KEYFRAME_PROMPT, prompt, KEYFRAME_SCHEMA, image=sheet)

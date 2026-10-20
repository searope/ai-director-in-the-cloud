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
    "required": ["verdict", "summary", "issues", "motion_prompt", "keyframe_advice"],
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
        "motion_prompt": {"type": "string"},
        "keyframe_advice": {"type": "string"},
    },
}

SYSTEM_PROMPT = """You review short AI-generated video clips for visual problems and write a better prompt.

How the clip was made: a video model (Wan 2.2, image-to-video with first and last frame)
animated between a START keyframe and an END keyframe, following a short motion prompt.
You see one contact sheet: the two keyframes on top, then frames sampled evenly through the
clip, each labelled with its time in seconds.

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
camera"). Name the things that must keep their shape. Keep it under about 120 words. Wan follows plain
physical descriptions better than abstract ones; describing how something moves works better
than listing what must not happen.

If the real cause is in the keyframes (START and END too different for one smooth move, an
object missing from or added in one of them, a face or eye colour that differs, a bad face in a
keyframe), say what to change in `keyframe_advice`; otherwise leave it "". The app always needs
both keyframes, so the options are: remake END as a small edit of START (say exactly what the
edit should change and keep), use START as both keyframes (a clip that returns to its start), or
split the move into two shots with a middle keyframe.

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


def make_sheet(video_path, keyframe_paths, out_path, count=SHEET_FRAMES):
    """Write the contact sheet (PNG) and return its path."""
    from PIL import Image, ImageDraw, ImageFont
    frames = _frames(video_path, count)
    w = CELL_W
    h = round(w * frames[0][0].height / frames[0][0].width)
    cols = 4
    rows = (len(frames) + cols - 1) // cols
    label_h, gap = 26, 8
    keys = [Image.open(p).convert("RGB") for p in keyframe_paths[:2]]
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

    for i, (img, label) in enumerate(zip(keys, ("START keyframe", "END keyframe"))):
        cell(img, gap + i * (w + gap), gap, label, (250, 204, 21))
    for i, (img, t) in enumerate(frames):
        r, c = divmod(i, cols)
        cell(img, gap + c * (w + gap), top + gap + r * (label_h + h + gap), f"{t:.2f} s")
    sheet.save(out_path, "PNG")
    return out_path


def user_prompt(motion_prompt, seconds):
    return (f"The motion prompt used was: \"{motion_prompt}\"\nThe clip is {seconds:.1f} seconds long.\n\n"
            "Review the clip on the contact sheet and write the improved motion prompt.")


def duration(video_path):
    import av
    with av.open(video_path) as container:
        stream = container.streams.video[0]
        if stream.duration and stream.time_base:
            return float(stream.duration * stream.time_base)
        return float(container.duration or 0) / 1_000_000


def review(writer, video_path, keyframe_paths, motion_prompt):
    """Ask a writer (from director.load_writers) to review a clip. Returns the parsed review."""
    with tempfile.TemporaryDirectory(prefix="clipreview_") as tmp:
        sheet = make_sheet(video_path, keyframe_paths, os.path.join(tmp, "sheet.png"))
        return writer.write(SYSTEM_PROMPT, user_prompt(motion_prompt, duration(video_path)), REVIEW_SCHEMA, image=sheet)

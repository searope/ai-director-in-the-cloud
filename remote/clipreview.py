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
- physics that look wrong (things floating, passing through each other);
- bodies moving wrongly against the set (a major issue): a person or animal sliding or gliding across a
  bed, floor or seat without moving their limbs (e.g. lying in bed and drifting toward the camera),
  floating, sinking into furniture, or the room moving with them. Compare each body with the fixed things
  around it (pillow, headboard, floor edge) frame by frame. A camera move moves everything together; only
  a real action should move a body relative to the set. This usually comes from a push-in the video model
  turned into the subject moving: say in motion_prompt that the character stays in place and only the
  camera moves.
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

When the message describes the SHOT (a Director film), also check it against that description:
- go through the shot's action and end point by point: is each thing it should show visible and
  recognisable in the frames (e.g. "the memory painted on the ceiling" must read as that memory, not an
  abstract swirl; "the lantern dims" must visibly dim)? Each missing story point is a major issue;
- faces and expressions: does each character's expression fit what the shot says, in every frame (a
  "calm, slight smile" turning into a sad, downturned mouth or worried brows is an issue)? Name the time;
- timing: does the action take the time it should (one long in-breath, not a quick in-and-out)?
- the clip must really move as the shot needs: a nearly frozen clip when the action asks for movement is
  a major issue (often START and END are almost the same picture: say so in keyframe_advice);
- anything showing how the picture is made rather than the scene: a painter's hands or fingers, brushes,
  panes of glass, camera gear. These are major issues; they come from making-of words in the prompts;
- when a SET picture (the place's reference) is on the sheet, the place must match it: the same
  furniture on the same sides, the same windows and doors in the same places, the same walls and colours.
  A nightstand that switches sides or a window that changes shape is a major continuity issue.
The message also gives measured differences between pictures (0 = identical, mean of the pixel
differences): first frame vs START and last frame vs END over about 15 mean the clip drifts away from its
keyframes; START vs END under about 8 means the keyframes are almost the same, so the clip barely moves.

verdict: "good" (nothing a viewer would notice), "minor" (small flaws, usable), or "redo" (any major
issue, including a missing story point, a wrong expression in a key moment, or a broken place).
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


def make_sheet(video_path, keyframe_paths, out_path, count=SHEET_FRAMES, original_paths=(), set_ref=None):
    """Write the contact sheet (PNG) and return its path. original_paths: the keyframes before a style
    redraw, shown next to the redrawn START and END; set_ref: the place's reference picture."""
    from PIL import Image, ImageDraw, ImageFont
    frames = _frames(video_path, count)
    w = CELL_W
    h = round(w * frames[0][0].height / frames[0][0].width)
    label_h, gap = 26, 8
    keys = [Image.open(p).convert("RGB") for p in keyframe_paths[:2]]
    keys += [Image.open(p).convert("RGB") for p in list(original_paths)[:2]]
    labels = ["START keyframe", "END keyframe"] + ["ORIGINAL of START", "ORIGINAL of END"][:len(list(original_paths)[:2])]
    if set_ref:
        keys.append(Image.open(set_ref).convert("RGB"))
        labels.append("SET (the place)")
    cols = max(4, len(keys))  # the top row holds every reference picture
    rows = (len(frames) + cols - 1) // cols
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

    for i, (img, label) in enumerate(zip(keys, labels)):
        cell(img, gap + i * (w + gap), gap, label, (250, 204, 21))
    for i, (img, t) in enumerate(frames):
        r, c = divmod(i, cols)
        cell(img, gap + c * (w + gap), top + gap + r * (label_h + h + gap), f"{t:.2f} s")
    sheet.save(out_path, "PNG")
    return out_path


def differences(video_path, keyframe_paths):
    """Mean pixel differences: first frame vs START, last frame vs END, START vs END (0 = identical)."""
    from PIL import ImageChops, ImageStat
    from PIL import Image
    frames = _frames(video_path, 2)
    start, end = (Image.open(p).convert("RGB") for p in keyframe_paths[:2])

    def diff(a, b):
        a = a.convert("RGB").resize((256, round(256 * a.height / a.width)))
        return round(sum(ImageStat.Stat(ImageChops.difference(a, b.resize(a.size))).mean) / 3, 1)
    return {"first_vs_start": diff(frames[0][0], start), "last_vs_end": diff(frames[-1][0], end), "start_vs_end": diff(start, end)}


def shot_text(shot, measured):
    text = ""
    if shot:
        text += (f"\nThe SHOT (what the clip should show): {shot.get('action', '')}\n"
                 f"It should end with: {shot.get('end', '')}\n")
        if shot.get("characters"):
            text += f"Characters: {shot['characters']}\n"
        place = shot.get("place")
        if place:
            text += f"The place: {place.get('name', '')}" + (f"; its layout: {place['layout']}" if place.get("layout") else "") + \
                (" (its picture is the SET cell on the sheet)" if place.get("image") else "") + "\n"
    if measured:
        text += (f"Measured differences: first frame vs START {measured['first_vs_start']}, last frame vs END "
                 f"{measured['last_vs_end']}, START vs END {measured['start_vs_end']}.\n")
    return text


def user_prompt(motion_prompt, seconds, style=None, redrawn=False, shot=None, measured=None):
    text = f"The motion prompt used was: \"{motion_prompt}\"\nThe clip is {seconds:.1f} seconds long.\n"
    text += shot_text(shot, measured)
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


def review(writer, video_path, keyframe_paths, motion_prompt, style=None, original_paths=(), shot=None, set_ref=None):
    """Ask a writer (from director.load_writers) to review a clip. Returns the parsed review, with the measured
    differences added. style: {"label", "description"} of the requested style; original_paths: the keyframes
    before a redraw; shot: a Director shot's {action, end, characters, place}; set_ref: the place's picture."""
    measured = differences(video_path, keyframe_paths)
    with tempfile.TemporaryDirectory(prefix="clipreview_") as tmp:
        sheet = make_sheet(video_path, keyframe_paths, os.path.join(tmp, "sheet.png"), original_paths=original_paths,
                           set_ref=set_ref)
        prompt = user_prompt(motion_prompt, duration(video_path), style, bool(original_paths), shot, measured)
        return {**writer.write(SYSTEM_PROMPT, prompt, REVIEW_SCHEMA, image=sheet), "measured": measured}


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

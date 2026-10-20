"""Turn a sequence of image keyframes into one animation.

Each pair of consecutive keyframes (K1->K2, K2->K3, ...) becomes a Wan 2.2
first/last-frame clip rendered by a running ComfyUI server. The clips share
their boundary frames, so they are joined seamlessly into a single MP4.

Two engines: Wan 2.2 14B (default, 16 fps, silent) and MiniMax H3 (24 fps,
generates matching sound; heavier, optional download). Check LICENSES before
commercial use: MiniMax H3's license excludes the US, EU, UK and South Korea.

A style preset (styles.json) adds style wording and optional Wan LoRAs to the
video prompt, and can first redraw the keyframes in that style with FLUX.2
Klein (e.g. turn photos into anime, claymation, watercolor...).

Usage:
    python animate.py examples/storyboard.json
    python animate.py --keyframes my_frames/ --prompt "the camera slowly orbits the character"
    python animate.py --keyframes my_frames/ --style anime
    python animate.py --keyframes my_frames/ --custom-style "paper cut-out stop motion"
    python animate.py --keyframes my_frames/ --sound "birds, soft wind"     # MiniMax H3 (default) with sound
    python animate.py --keyframes my_frames/ --engine wan22                 # Wan 2.2, silent
    python animate.py --list-styles

ComfyUI must be running (run_comfyui.bat / run_comfyui.sh). Needs only the
standard library plus PyAV, numpy and Pillow, which ComfyUI already installs.
"""

import argparse
import io
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
STYLES_FILE = os.path.join(ROOT, "styles.json")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

# Wan's default negative prompt (from the official ComfyUI template).
DEFAULT_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，"
    "JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，"
    "手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)

ENGINES = ("wan22", "minimax-h3")

# Short license notes shown in the app (details in setup/models.json; verify on each model card).
LICENSES = {
    "wan22": "Apache 2.0, commercial OK",
    "minimax-h3": "commercial OK under the owner's written MiniMax license (Oct 2026)",
    "h3-turbo": "lightx2v turbo LoRA: license not verified (no license on the repo)",
    "klein4b": "Apache 2.0, commercial OK",
    "klein9b": "non-commercial",
    "flux3klein": "commercial only under $5M revenue",
}

# Image models for keyframe restyling and the keyframe maker (text -> image, image edits).
IMAGE_MODELS = {
    "klein4b": {"label": "FLUX.2 Klein 4B (fast)", "family": "flux2", "license": LICENSES["klein4b"],
                "unet": "flux-2-klein-4b-fp8.safetensors", "text_encoder": "qwen_3_4b.safetensors",
                "vae": "flux2-vae.safetensors"},
    "klein9b": {"label": "FLUX.2 Klein 9B (sharper)", "family": "flux2", "license": LICENSES["klein9b"],
                "unet": "flux-2-klein-9b-fp8.safetensors", "text_encoder": "qwen_3_8b_fp8mixed.safetensors",
                "vae": "full_encoder_small_decoder.safetensors"},
    "flux3klein": {"label": "FLUX 3 Klein (newest)", "family": "flux3", "license": LICENSES["flux3klein"],
                   "unet": "flux3-image-klein-int8-convrot.safetensors", "text_encoder": "qwen3vl_4b_int8_convrot.safetensors",
                   "vae": "flux2-vae.safetensors"},
}


def image_model(cfg):
    name = cfg.get("image_model") or "klein4b"
    if name not in IMAGE_MODELS:
        raise ValueError(f"image_model must be one of: {', '.join(IMAGE_MODELS)}")
    return IMAGE_MODELS[name]


def missing_image_model_files(models_dir, name, exists=None):
    """Model files of IMAGE_MODELS[name] not present under ComfyUI/models (or per exists(folder, file))."""
    m = IMAGE_MODELS[name]
    files = [("diffusion_models", m["unet"]), ("text_encoders", m["text_encoder"]), ("vae", m["vae"])]
    exists = exists or (lambda d, f: os.path.exists(os.path.join(models_dir, d, f)))
    return [f"{d}/{f}" for d, f in files if not exists(d, f)]

DEFAULTS = {
    "server": "http://127.0.0.1:8188",
    "engine": "minimax-h3",   # "minimax-h3" (default, with sound) or "wan22" (silent; see LICENSES)
    "width": "auto",          # "auto" = match the first keyframe's aspect ratio
    "height": "auto",
    "area": 832 * 480,        # pixel budget for "auto" sizing; fits 8 GB VRAM
    "length": 81,             # frames per segment at 16 fps (4n+1); 81 = ~5 s. H3 uses the same duration at 24 fps
    "fps": 16,                # Wan's frame rate (H3 always renders 24 fps)
    "interpolate": None,      # FILM frame multiplier (1 = off); None = 2 for Wan (-> 32 fps), 1 for H3
    "turbo_steps": 8,         # H3 fast mode: 8 (turbo LoRA v1.0) or 4 (4-step turbo v1.3, about twice as fast)
    "audio": True,            # H3 only: keep the generated soundtrack
    "sound": None,            # H3 only: describe the sound, e.g. "rain on a tin roof, distant thunder"
    "fast": True,             # 4-step Lightx2v LoRAs; False = 20-step full quality (~5x slower)
    "seed": None,             # None = random
    "loop": False,            # add a last segment from the final keyframe back to the first
    "style": "photoreal",     # preset name from styles.json, or a style dict
    "custom_style": None,     # free-text style description; overrides "style"
    "restyle": None,          # redraw keyframes in the style first; None = only if the style has a restyle_prompt
    "image_model": "klein4b", # model for restyling and the keyframe maker, see IMAGE_MODELS
    "negative": DEFAULT_NEGATIVE,
    "crf": 18,
    "output": "output/animation.mp4",
    "models": {
        "high": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
        "low": "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
        "lora_high": "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
        "lora_low": "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors",
        "text_encoder": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "vae": "wan_2.1_vae.safetensors",
        "interp": "film_net_fp16.safetensors",
        "h3_unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "h3_text_encoder": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "h3_vae": "minimax_h3_video_vae_int8_convrot.safetensors",
        "h3_audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
        "h3_lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        "h3_lora_4step": "minimax_h3_fl2v_turbo_4step_v1.3_768p_comfyui_bf16.safetensors",
    },
}


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- ComfyUI API

class Comfy:
    headers = {}  # extra headers for every request, e.g. the token of a remote GPU gateway (remote/gpu.py)

    def __init__(self, server):
        self.server = server.rstrip("/")
        self.client_id = uuid.uuid4().hex

    def _request(self, path, data=None, headers=None):
        req = urllib.request.Request(self.server + path, data=data, headers={**self.headers, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"ComfyUI {path} returned {e.code}:\n{body}") from None

    def ping(self):
        try:
            self._request("/system_stats")
        except (urllib.error.URLError, ConnectionError):
            raise RuntimeError(f"Cannot reach ComfyUI at {self.server}. Start it first with run_comfyui.bat (or .sh).")

    def upload_image(self, path, name=None):
        boundary = uuid.uuid4().hex
        name = name or os.path.basename(path)
        with open(path, "rb") as f:
            payload = f.read()
        parts = []
        for key, value in (("overwrite", "true"), ("type", "input"), ("subfolder", "keyframes")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode() + payload + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        resp = json.loads(self._request(
            "/upload/image", b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}))
        return f"{resp['subfolder']}/{resp['name']}" if resp.get("subfolder") else resp["name"]

    def queue(self, graph, front=False):
        payload = {"prompt": graph, "client_id": self.client_id}
        if front:  # run before anything already waiting in ComfyUI's queue
            payload["front"] = True
        body = json.dumps(payload).encode()
        resp = json.loads(self._request("/prompt", body, {"Content-Type": "application/json"}))
        if resp.get("node_errors"):
            raise RuntimeError(f"ComfyUI rejected the workflow: {json.dumps(resp['node_errors'], indent=2)}")
        return resp["prompt_id"]

    def cancel(self, prompt_id):
        body = json.dumps({"delete": [prompt_id]}).encode()
        self._request("/queue", body, {"Content-Type": "application/json"})
        self._request("/interrupt", json.dumps({"prompt_id": prompt_id}).encode(), {"Content-Type": "application/json"})

    def wait(self, prompt_id, label, report, should_cancel):
        start = time.time()
        while True:
            if should_cancel():
                self.cancel(prompt_id)
                raise Cancelled()
            hist = json.loads(self._request(f"/history/{prompt_id}"))
            if prompt_id in hist:
                entry = hist[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                    detail = msgs[-1][1].get("exception_message", "") if msgs else ""
                    raise RuntimeError(f"{label} failed in ComfyUI: {detail.strip()}")
                if status.get("completed", True):
                    report(f"{label}: done in {time.time() - start:.0f}s", final=True)
                    return entry["outputs"]
            report(f"{label}: rendering... {time.time() - start:.0f}s")
            time.sleep(2)

    def fetch(self, image):
        query = urllib.parse.urlencode(
            {"filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")})
        return self._request(f"/view?{query}")


# ---------------------------------------------------------------- workflows

def build_segment_graph(cfg, style, start_name, end_name, prompt, seed, width, height, prefix):
    """API-format graph equivalent to ComfyUI's 'Wan 2.2 14B First-Last Frame to Video' template."""
    m = cfg["models"]
    fast = cfg["fast"]
    steps, split, cfg_scale, shift = (4, 2, 1.0, 5.0) if fast else (20, 10, 3.5, 8.0)
    negative = ", ".join(x for x in (cfg["negative"], style.get("negative")) if x)

    g = {
        "unet_high": {"class_type": "UNETLoader", "inputs": {"unet_name": m["high"], "weight_dtype": "default"}},
        "unet_low": {"class_type": "UNETLoader", "inputs": {"unet_name": m["low"], "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["text_encoder"], "type": "wan", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["clip", 0]}},
        "start": {"class_type": "LoadImage", "inputs": {"image": start_name}},
        "end": {"class_type": "LoadImage", "inputs": {"image": end_name}},
        "flf": {"class_type": "WanFirstLastFrameToVideo", "inputs": {
            "positive": ["pos", 0], "negative": ["neg", 0], "vae": ["vae", 0],
            "width": width, "height": height, "length": cfg["length"], "batch_size": 1,
            "start_image": ["start", 0], "end_image": ["end", 0]}},
    }
    high, low = ["unet_high", 0], ["unet_low", 0]

    loras = list(style.get("loras") or [])
    if fast:
        loras.insert(0, {"high": m["lora_high"], "low": m["lora_low"], "strength": 1.0})
    for i, lora in enumerate(loras):
        strength = float(lora.get("strength", 1.0))
        for which, name in (("high", lora.get("high") or lora.get("file")), ("low", lora.get("low") or lora.get("file"))):
            node = f"lora_{which}_{i}"
            src = high if which == "high" else low
            g[node] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": src, "lora_name": name, "strength_model": strength}}
            if which == "high":
                high = [node, 0]
            else:
                low = [node, 0]

    g["shift_high"] = {"class_type": "ModelSamplingSD3", "inputs": {"model": high, "shift": shift}}
    g["shift_low"] = {"class_type": "ModelSamplingSD3", "inputs": {"model": low, "shift": shift}}

    common = {"steps": steps, "cfg": cfg_scale, "sampler_name": "euler", "scheduler": "simple",
              "positive": ["flf", 0], "negative": ["flf", 1]}
    g["sample_high"] = {"class_type": "KSamplerAdvanced", "inputs": dict(common,
        model=["shift_high", 0], add_noise="enable", noise_seed=seed, latent_image=["flf", 2],
        start_at_step=0, end_at_step=split, return_with_leftover_noise="enable")}
    g["sample_low"] = {"class_type": "KSamplerAdvanced", "inputs": dict(common,
        model=["shift_low", 0], add_noise="disable", noise_seed=seed, latent_image=["sample_high", 0],
        start_at_step=split, end_at_step=10000, return_with_leftover_noise="disable")}
    g["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sample_low", 0], "vae": ["vae", 0]}}

    frames = ["decode", 0]
    if cfg["interpolate"] > 1:
        g["interp_model"] = {"class_type": "FrameInterpolationModelLoader", "inputs": {"model_name": m["interp"]}}
        g["interp"] = {"class_type": "FrameInterpolate", "inputs": {
            "interp_model": ["interp_model", 0], "images": frames, "multiplier": cfg["interpolate"]}}
        frames = ["interp", 0]
    g["save"] = {"class_type": "SaveImage", "inputs": {"images": frames, "filename_prefix": prefix}}
    return g


def h3_length(wan_length):
    """Same duration as a Wan segment of wan_length frames (16 fps), on H3's 24 fps 17k+5 frame grid."""
    frames = max(5, round((wan_length - 1) / 16 * 24))
    return frames + (5 - frames % 17) % 17


def build_h3_segment_graph(cfg, start_name, end_name, prompt, seed, width, height, prefix):
    """API-format graph equivalent to ComfyUI's 'MiniMax H3: Image to Video' template (first+last frame)."""
    m = cfg["models"]
    fast = cfg["fast"]
    g = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": m["h3_unet"], "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["h3_text_encoder"], "type": "minimax", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae"]}},
        "start": {"class_type": "LoadImage", "inputs": {"image": start_name}},
        "end": {"class_type": "LoadImage", "inputs": {"image": end_name}},
        "i2v": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["clip", 0], "vae": ["vae", 0], "prompt": prompt, "width": width, "height": height,
            "length": h3_length(cfg["length"]), "first_frame": ["start", 0], "last_frame": ["end", 0]}},
    }
    model = ["unet", 0]
    steps = 20
    if fast:  # a lightx2v turbo LoRA: 8 steps, or 4 with the 4-step version
        steps = 4 if cfg.get("turbo_steps") == 4 else 8
        lora = m["h3_lora_4step"] if steps == 4 else m["h3_lora"]
        g["lora"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": model, "lora_name": lora, "strength_model": 1.0}}
        model = ["lora", 0]
    g.update({
        "guider": {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": ["i2v", 0]}},
        "sigmas": {"class_type": "BasicScheduler", "inputs": {"model": model, "scheduler": "simple", "steps": steps, "denoise": 1.0}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
            "sigmas": ["sigmas", 0], "latent_image": ["i2v", 1]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
    })
    frames = ["decode", 0]
    if cfg["interpolate"] > 1:
        g["interp_model"] = {"class_type": "FrameInterpolationModelLoader", "inputs": {"model_name": m["interp"]}}
        g["interp"] = {"class_type": "FrameInterpolate", "inputs": {
            "interp_model": ["interp_model", 0], "images": frames, "multiplier": cfg["interpolate"]}}
        frames = ["interp", 0]
    g["save"] = {"class_type": "SaveImage", "inputs": {"images": frames, "filename_prefix": prefix}}
    if cfg["audio"]:
        g["audio_vae"] = {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_audio_vae"]}}
        g["decode_audio"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["sample", 0], "vae": ["audio_vae", 0]}}
        g["save_audio"] = {"class_type": "SaveAudio", "inputs": {"audio": ["decode_audio", 0], "filename_prefix": prefix + "_audio"}}
    return g


def _image_loaders(m, text):
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["text_encoder"], "type": m["family"], "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "text": {"class_type": "CLIPTextEncode", "inputs": {"text": text, "clip": ["clip", 0]}},
    }


def _flux3_sample(g, positive, negative, latent, seed, prefix):
    """FLUX 3 Klein distilled sampling, as in ComfyUI's 'FLUX 3 Image Klein Distilled' templates (16 steps, cfg 1)."""
    g.update({
        "sample": {"class_type": "KSampler", "inputs": {
            "model": ["unet", 0], "seed": seed, "steps": 16, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
            "positive": positive, "negative": negative, "latent_image": latent, "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": prefix}},
    })
    return g


def build_restyle_graph(cfg, image_name, instruction, seed, prefix, reference_name=None, megapixels=1.0,
                        references=(), size=None):
    """Image edit with the configured image model, as in ComfyUI's Klein 'Image Edit' templates.

    reference_name: an already-restyled keyframe passed as a second reference image so
    every keyframe comes out in the same look. references: more reference images after it
    (e.g. character portraits). Without image_name, a new image of size (width, height) is made
    from the references alone. megapixels counts 1024x1024 pixels.
    """
    m = image_model(cfg)
    g = _image_loaders(m, instruction)
    g["zero"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["text", 0]}}
    refs = (([("img", image_name)] if image_name else []) + ([("ref", reference_name)] if reference_name else [])
            + [(f"ref{i + 2}", name) for i, name in enumerate(references)])
    if not image_name and not size:
        raise ValueError("an image to edit or an output size is needed")
    if size:
        g["latent"] = {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": size[0], "height": size[1], "batch_size": 1}}

    if m["family"] == "flux3":
        refs_in = {}
        for i, (key, name) in enumerate(refs):
            g[f"{key}_load"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            refs_in[f"images.image{i}"] = [f"{key}_load", 0]
        g["refs"] = {"class_type": "ReferenceMultiLatent", "inputs": dict(
            refs_in, positive=["text", 0], negative=["zero", 0], vae=["vae", 0],
            resolution="resize", **{"resolution.megapixels": megapixels})}
        return _flux3_sample(g, ["refs", 0], ["refs", 1], ["latent", 0] if size else ["refs", 2], seed, prefix)

    pos, neg = ["text", 0], ["zero", 0]
    for key, name in refs:
        g[f"{key}_load"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g[f"{key}_scale"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": [f"{key}_load", 0], "upscale_method": "nearest-exact", "megapixels": megapixels, "resolution_steps": 1}}
        g[f"{key}_enc"] = {"class_type": "VAEEncode", "inputs": {"pixels": [f"{key}_scale", 0], "vae": ["vae", 0]}}
        g[f"{key}_pos"] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": pos, "latent": [f"{key}_enc", 0]}}
        g[f"{key}_neg"] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": neg, "latent": [f"{key}_enc", 0]}}
        pos, neg = [f"{key}_pos", 0], [f"{key}_neg", 0]

    if size:
        width, height = size
    else:  # the output keeps the edited image's size
        width, height = ["size", 0], ["size", 1]
        g["size"] = {"class_type": "GetImageSize", "inputs": {"image": ["img_scale", 0]}}
        g["latent"] = {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}}
    g.update({
        "sigmas": {"class_type": "Flux2Scheduler", "inputs": {"steps": 4, "width": width, "height": height}},
        "guider": {"class_type": "CFGGuider", "inputs": {"model": ["unet", 0], "positive": pos, "negative": neg, "cfg": 1.0}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
            "sigmas": ["sigmas", 0], "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": prefix}},
    })
    return g


def build_text_to_image_graph(cfg, prompt, seed, width, height, prefix):
    """Text-to-image with the configured image model, as in ComfyUI's Klein 'Text to Image' templates."""
    m = image_model(cfg)
    g = _image_loaders(m, prompt)
    latent = {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}}
    if m["family"] == "flux3":
        g["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["clip", 0]}}
        g["latent"] = latent
        return _flux3_sample(g, ["text", 0], ["neg", 0], ["latent", 0], seed, prefix)
    return dict(g, **{
        "zero": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["text", 0]}},
        "latent": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "sigmas": {"class_type": "Flux2Scheduler", "inputs": {"steps": 4, "width": width, "height": height}},
        "guider": {"class_type": "CFGGuider", "inputs": {"model": ["unet", 0], "positive": ["text", 0], "negative": ["zero", 0], "cfg": 1.0}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
            "sigmas": ["sigmas", 0], "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": prefix}},
    })


PAIR_HEIGHT = 1024  # each photo's height in a side-by-side restyle
PAIR_GAP = 48       # white gap between the two photos


def split_pair(out, left_w, right_w):
    """Cut a side-by-side restyle back into its two pictures. The model may paint the white gap a little to
    one side, so find the actual gap (a run of near-white columns near where it was) instead of cutting at
    the input's position, trim leftover white from the inner edges, and scale each half to its expected size."""
    import numpy as np
    from PIL import Image
    canvas_w = left_w + PAIR_GAP + right_w
    scale = out.width / canvas_w
    white = (np.asarray(out.convert("L")) > 225).mean(axis=0) > 0.85  # columns that are mostly white
    lo, hi = int((left_w - 0.1 * canvas_w) * scale), int((left_w + PAIR_GAP + 0.1 * canvas_w) * scale)
    runs, start = [], None
    for x in range(max(0, lo), min(out.width, hi)):
        if white[x] and start is None:
            start = x
        elif not white[x] and start is not None:
            runs.append((start, x)); start = None
    if start is not None:
        runs.append((start, min(out.width, hi)))
    runs = [r for r in runs if r[1] - r[0] >= 0.4 * PAIR_GAP * scale]  # not a white object in a picture
    center = (left_w + PAIR_GAP / 2) * scale
    if runs:  # the white run closest to where the gap should be
        gap_start, gap_end = min(runs, key=lambda r: abs((r[0] + r[1]) / 2 - center))
    else:
        gap_start, gap_end = round(left_w * scale), round((left_w + PAIR_GAP) * scale)
    a, b = 0, gap_start   # left picture: [a, b)
    c, d = gap_end, out.width
    limit = int(0.15 * out.width)  # trim at most this much leftover white per inner edge
    while b - a > 1 and white[b - 1] and gap_start - b < limit:
        b -= 1
    while d - c > 1 and white[c] and c - gap_end < limit:
        c += 1
    size = lambda w: (max(16, round(w * scale)), out.height)
    return [out.crop((a, 0, b, out.height)).resize(size(left_w), Image.LANCZOS),
            out.crop((c, 0, d, out.height)).resize(size(right_w), Image.LANCZOS)]


def restyle_keyframes(comfy, cfg, instruction, paths, out_dir, seed, prefix, report, should_cancel):
    """Redraw keyframes in a style so they share it, while each keeps its own pose and layout.

    Keyframe 1 is redrawn side by side with each later keyframe, as one picture in one pass, then cut
    apart: one pass gives both halves the same style, and the shared first keyframe keeps all pairs alike.
    (Passing a redrawn keyframe as a separate style reference made the model copy its whole picture.)
    Returns the paths of the redrawn keyframes in out_dir, in order.
    """
    from PIL import Image
    pair_instruction = (instruction + " The image is two separate pictures side by side with a white gap between "
                        "them: redraw both in exactly the same style, keep each picture's own pose, composition and "
                        "framing, and keep the white gap. Both pictures show the same people: draw each person with "
                        "exactly the same hairstyle, hair length, hair colour, face and clothes in both pictures, as "
                        "they look in the photos; only the pose and the camera angle differ.")
    photos = [Image.open(p).convert("RGB") for p in paths]
    fit = [im.resize((max(16, round(im.width * PAIR_HEIGHT / im.height)), PAIR_HEIGHT)) for im in photos]
    outputs = []
    for i in range(1, len(paths)) if len(paths) > 1 else [0]:
        left, right = fit[0], fit[i]
        canvas = Image.new("RGB", (left.width + PAIR_GAP + right.width, PAIR_HEIGHT), (255, 255, 255))
        canvas.paste(left, (0, 0))
        canvas.paste(right, (left.width + PAIR_GAP, 0))
        pair_path = os.path.join(out_dir, f"_pair_{i:02d}.png")
        canvas.save(pair_path)
        name = comfy.upload_image(pair_path, f"{os.path.basename(out_dir)}_pair_{i:02d}.png")
        megapixels = min(2.0, canvas.width * canvas.height / (1024 * 1024))
        graph = build_restyle_graph(cfg, name, pair_instruction, seed, f"{prefix}_{i:02d}", megapixels=megapixels)
        result = comfy.wait(comfy.queue(graph), f"restyle keyframes 1+{i + 1}/{len(paths)}", report, should_cancel)
        with open(pair_path, "wb") as f:
            f.write(comfy.fetch(result["save"]["images"][0]))
        halves = split_pair(Image.open(pair_path).convert("RGB"), left.width, right.width)
        os.remove(pair_path)
        if i <= 1:  # keyframe 1 comes from the first pair; later pairs only supply their right half
            outputs.append(halves[0])
        if len(paths) > 1:
            outputs.append(halves[1])
    saved = []
    for n, im in enumerate(outputs):
        path = os.path.join(out_dir, f"{n + 1:02d}.png")
        im.save(path)
        saved.append(path)
    return saved


# ---------------------------------------------------------------- keyframe maker

KEYFRAME_SIZES = {"16:9": (1024, 576), "9:16": (576, 1024), "1:1": (1024, 1024), "4:3": (1024, 768), "3:4": (768, 1024)}
EDIT_SUFFIX = (" Keep the same characters, art style, lighting, colors, background and camera framing; "
               "change nothing else.")


def make_keyframe(server, prompt, out_path, seed=None, aspect="16:9", source=None,
                  report=lambda text, final=False: None, model="klein4b", references=()):
    """Create one keyframe image with an IMAGE_MODELS model and save it to out_path.

    Without source: text-to-image at the given aspect. With source (an image path):
    edit that image according to prompt, keeping its size, so a chain of edits
    stays consistent. references: more image paths passed to the model after the source
    (e.g. character portraits, which the prompt then names as image 2, 3...; image 1, 2...
    without a source). Returns the seed used.
    """
    from PIL import Image

    cfg = {"image_model": model}
    image_model(cfg)  # validate
    seed = seed if seed is not None else random.randint(0, 2**32 - 1)
    comfy = Comfy(server)
    comfy.ping()
    prefix = f"keyframe_maker/{uuid.uuid4().hex[:8]}"
    refs = [comfy.upload_image(path, f"kfm_{uuid.uuid4().hex[:8]}.png") for path in references]
    if source:
        with Image.open(source) as im:
            megapixels = min(1.0, im.width * im.height / (1024 * 1024))
        name = comfy.upload_image(source, f"kfm_{uuid.uuid4().hex[:8]}.png")
        graph = build_restyle_graph(cfg, name, prompt.strip().rstrip(".") + "." + EDIT_SUFFIX, seed, prefix,
                                    megapixels=megapixels, references=refs)
    elif refs:
        graph = build_restyle_graph(cfg, None, prompt, seed, prefix, references=refs, size=KEYFRAME_SIZES[aspect])
    else:
        width, height = KEYFRAME_SIZES[aspect]
        graph = build_text_to_image_graph(cfg, prompt, seed, width, height, prefix)
    outputs = comfy.wait(comfy.queue(graph, front=True), "keyframe", report, lambda: False)
    with open(out_path, "wb") as f:
        f.write(comfy.fetch(outputs["save"]["images"][0]))
    return seed


# ---------------------------------------------------------------- styles & config

def load_styles():
    with open(STYLES_FILE, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def custom_style(description):
    return {
        "label": description,
        "video_prompt": description,
        "negative": "",
        "restyle_prompt": (f"Redraw this image in the following style: {description}. "
                           "Keep the exact composition, pose, framing and characters."),
        "loras": [],
    }


def resolve_style(cfg):
    if cfg.get("custom_style"):
        return custom_style(cfg["custom_style"])
    style = cfg.get("style") or "photoreal"
    if isinstance(style, dict):
        return style
    styles = load_styles()
    if style not in styles:
        raise ValueError(f"Unknown style '{style}'. Available: {', '.join(styles)}")
    return styles[style]


def make_config(overrides, base_dir=None):
    """Merge overrides onto DEFAULTS and validate. Relative keyframe paths resolve against base_dir."""
    cfg = json.loads(json.dumps(DEFAULTS))
    overrides = dict(overrides)
    cfg["models"].update(overrides.pop("models", {}) or {})
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    base_dir = base_dir or os.getcwd()

    keys = cfg.get("keyframes") or []
    if len(keys) < 2:
        raise ValueError("Need at least two keyframes.")
    cfg["keyframes"] = [p if os.path.isabs(p) else os.path.join(base_dir, p) for p in keys]
    if not os.path.isabs(cfg["output"]):
        cfg["output"] = os.path.join(ROOT, cfg["output"])
    if cfg["length"] % 4 != 1:
        raise ValueError("length must be 4n+1 (e.g. 33, 49, 65, 81).")
    if cfg["engine"] not in ENGINES:
        raise ValueError(f"engine must be one of: {', '.join(ENGINES)}")
    image_model(cfg)  # validate image_model
    if cfg["interpolate"] is None:
        cfg["interpolate"] = 2 if cfg["engine"] == "wan22" else 1
    if cfg["turbo_steps"] not in (4, 8):
        raise ValueError("turbo_steps must be 4 or 8.")

    n_segments = len(cfg["keyframes"]) - (0 if cfg["loop"] else 1)
    prompts = cfg.get("prompts") or ["smooth natural motion, consistent subject"]
    if isinstance(prompts, str):
        prompts = [prompts]
    if len(prompts) == 1:
        prompts = prompts * n_segments
    if len(prompts) != n_segments:
        raise ValueError(f"{n_segments} segments but {len(prompts)} prompts; give one prompt per segment or a single prompt.")
    cfg["prompts"] = prompts

    cfg["style_def"] = resolve_style(cfg)
    if cfg["restyle"] is None:
        cfg["restyle"] = bool(cfg["style_def"].get("restyle_prompt"))
    if cfg["restyle"] and not cfg["style_def"].get("restyle_prompt"):
        raise ValueError("This style has no restyle_prompt, so keyframes can't be restyled with it.")
    return cfg


# ---------------------------------------------------------------- helpers

def auto_size(path, area, multiple=16):
    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
    scale = (area / (w * h)) ** 0.5
    return (max(multiple, round(w * scale / multiple) * multiple),
            max(multiple, round(h * scale / multiple) * multiple))


def decode_audio(data):
    """Decode an audio file (bytes) to (float32 array [channels, samples] in stereo, sample_rate)."""
    import av
    import numpy as np

    chunks, rate = [], None
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.audio[0]
        rate = stream.rate
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=rate)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray())
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray())
    return np.concatenate(chunks, axis=1).astype(np.float32), rate


def write_video(frames, path, fps, crf, audio=None, audio_rate=None):
    """Encode PIL frames to H.264 MP4; audio is an optional float32 [2, samples] array, fitted to the video length."""
    import av
    import numpy as np

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    w, h = frames[0].size
    with av.open(path, "w") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width, stream.height = w - w % 2, h - h % 2
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": str(crf)}
        astream = None
        if audio is not None:
            astream = container.add_stream("aac", rate=audio_rate)
            astream.layout = "stereo"
        for im in frames:
            arr = np.asarray(im.convert("RGB"))[: stream.height, : stream.width]
            for packet in stream.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)

        if astream is not None:
            total = round(len(frames) / fps * audio_rate)
            audio = audio[:, :total]
            if audio.shape[1] < total:
                audio = np.pad(audio, ((0, 0), (0, total - audio.shape[1])))
            audio = np.ascontiguousarray(np.clip(audio, -1, 1))
            for pos in range(0, total, 1024):
                frame = av.AudioFrame.from_ndarray(audio[:, pos:pos + 1024], format="fltp", layout="stereo")
                frame.sample_rate = audio_rate
                frame.pts = pos
                for packet in astream.encode(frame):
                    container.mux(packet)
            for packet in astream.encode():
                container.mux(packet)


def console_report(text, final=False):
    sys.stdout.write(f"\r  {text}{' ' * 10}" + ("\n" if final else ""))
    sys.stdout.flush()


# ---------------------------------------------------------------- pipeline

def run(cfg, report=console_report, should_cancel=lambda: False):
    """Render the animation described by cfg (from make_config). Returns the output path."""
    from PIL import Image

    comfy = Comfy(cfg["server"])
    comfy.ping()
    style = cfg["style_def"]
    h3 = cfg["engine"] == "minimax-h3"

    keys = cfg["keyframes"]
    width, height = cfg["width"], cfg["height"]
    if width == "auto" or height == "auto":
        width, height = auto_size(keys[0], cfg["area"], 32 if h3 else 16)
    elif h3:
        width, height = width // 32 * 32, height // 32 * 32
    seed = cfg["seed"] if cfg["seed"] is not None else random.randint(0, 2**32 - 1)
    base_fps = 24 if h3 else cfg["fps"]
    out_fps = base_fps * max(1, cfg["interpolate"])
    if h3 and style.get("loras"):
        report("note: this style's LoRAs are for Wan 2.2 and are skipped with MiniMax H3", final=True)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    report(f"engine {cfg['engine']}, {len(keys)} keyframes, style '{style.get('label', '?')}'{' (restyling keyframes)' if cfg['restyle'] else ''}, "
           f"{width}x{height}, {h3_length(cfg['length']) if h3 else cfg['length']} frames/segment, {'fast' if cfg['fast'] else 'quality'} mode, seed {seed}",
           final=True)

    uploaded = {p: comfy.upload_image(p, f"{run_id}_{i:02d}{os.path.splitext(p)[1]}") for i, p in enumerate(keys)}

    if cfg["restyle"]:
        styled_dir = os.path.splitext(cfg["output"])[0] + "_keyframes"
        os.makedirs(styled_dir, exist_ok=True)
        # Optional check (cfg["keyframe_check"](originals, redrawn) -> {"ok", "score", "problems", "advice"}):
        # redraw again with the advice and a new seed while it finds problems, and keep the best attempt.
        base = f"{style['restyle_prompt']} {cfg.get('restyle_note') or ''}".strip()  # restyle_note: the user's request
        check, instruction, best = cfg.get("keyframe_check"), base, None
        tries = 1 + (int(cfg.get("keyframe_retries", 2)) if check else 0)
        for attempt in range(tries):
            styled = restyle_keyframes(comfy, cfg, instruction, keys, styled_dir, seed + 1000 * attempt,
                                       f"keyframe_anim/{run_id}/styled{attempt}", report, should_cancel)
            if not check:
                break
            report(f"checking the redrawn keyframes (try {attempt + 1}/{tries})")
            try:
                result = check(keys, styled)
            except Exception as e:  # a failed check doesn't stop the render
                report(f"keyframe check skipped: {e}", final=True)
                break
            report(f"keyframe check {attempt + 1}: " + ("OK" if result.get("ok") else
                   f"score {result.get('score')}/10, " + "; ".join(result.get("problems") or [])[:400]), final=True)
            if best is None or result.get("score", 0) > best[0]:
                best = (result.get("score", 0), [open(p, "rb").read() for p in styled])
            if result.get("ok"):
                break
            if should_cancel():
                raise Cancelled()
            instruction = f"{base} {result.get('advice', '')}".strip()
        if best is not None:  # put the best attempt's keyframes back (a later try may have been worse)
            for p, data in zip(styled, best[1]):
                with open(p, "wb") as f:
                    f.write(data)
        for i, (p, local) in enumerate(zip(keys, styled)):
            uploaded[p] = comfy.upload_image(local, f"{run_id}_styled_{i:02d}.png")
        report(f"restyled keyframes saved in {styled_dir}", final=True)
        if cfg.get("stop_after_restyle"):  # the caller shows them for approval before the video is rendered
            return "restyled"

    pairs = list(zip(keys, keys[1:] + ([keys[0]] if cfg["loop"] else [])))
    all_frames, audio_parts, audio_rate = [], [], None
    for i, ((a, b), prompt) in enumerate(zip(pairs, cfg["prompts"])):
        full_prompt = ", ".join(x for x in (prompt, style.get("video_prompt")) if x)
        label = f"segment {i + 1}/{len(pairs)}"
        prefix = f"keyframe_anim/{run_id}/seg{i + 1:02d}"
        if h3:
            if cfg["audio"] and cfg.get("sound"):
                full_prompt += f"\nAudio: {cfg['sound']}"
            graph = build_h3_segment_graph(cfg, uploaded[a], uploaded[b], full_prompt, seed + i, width, height, prefix)
        else:
            graph = build_segment_graph(cfg, style, uploaded[a], uploaded[b], full_prompt, seed + i, width, height, prefix)
        outputs = comfy.wait(comfy.queue(graph), label, report, should_cancel)
        images = sorted(outputs["save"]["images"], key=lambda im: im["filename"])
        # Many downloads at once: through a remote GPU's proxy each frame takes ~1.5 s on its own
        # (161 frames: ~4 min one by one, ~9 s sixteen at a time).
        report(f"{label}: downloading {len(images)} frames")
        with ThreadPoolExecutor(16) as pool:
            frames = list(pool.map(lambda im: Image.open(io.BytesIO(comfy.fetch(im))).convert("RGB"), images))
        all_frames.extend(frames if i == 0 else frames[1:])  # segment i starts on segment i-1's last frame
        if h3 and cfg["audio"]:
            samples, audio_rate = decode_audio(comfy.fetch(outputs["save_audio"]["audio"][0]))
            if i > 0:  # drop the audio under the dropped shared frame
                samples = samples[:, round(audio_rate / base_fps):]
            audio_parts.append(samples)

    report("encoding video")
    audio = None
    if audio_parts:
        import numpy as np
        audio = np.concatenate(audio_parts, axis=1)
    write_video(all_frames, cfg["output"], out_fps, cfg["crf"], audio, audio_rate)
    report(f"wrote {cfg['output']} ({len(all_frames)} frames, {len(all_frames) / out_fps:.1f}s at {out_fps} fps)", final=True)
    return cfg["output"]


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("storyboard", nargs="?", help="storyboard JSON (see examples/storyboard.json)")
    ap.add_argument("--keyframes", help="folder of keyframe images, used in filename order")
    ap.add_argument("--prompt", help="one motion prompt used for every segment")
    ap.add_argument("--style", help="style preset from styles.json (default photoreal)")
    ap.add_argument("--custom-style", help="describe any style in words, e.g. 'paper cut-out stop motion'")
    ap.add_argument("--restyle", dest="restyle", action="store_true", default=None, help="redraw keyframes in the style first")
    ap.add_argument("--no-restyle", dest="restyle", action="store_false", help="keep keyframes as they are")
    ap.add_argument("--list-styles", action="store_true")
    ap.add_argument("--engine", choices=ENGINES, help="video model (default minimax-h3)")
    ap.add_argument("--image-model", choices=list(IMAGE_MODELS), help="model for --restyle (default klein4b)")
    ap.add_argument("--sound", help="MiniMax H3: describe the soundtrack")
    ap.add_argument("--turbo-steps", type=int, choices=(4, 8), help="MiniMax H3 fast mode: 8 (default) or 4 steps")
    ap.add_argument("--no-audio", dest="audio", action="store_false", default=None, help="MiniMax H3: drop the soundtrack")
    ap.add_argument("--server")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--length", type=int, help="frames per segment, 4n+1 (default 81)")
    ap.add_argument("--fps", type=int, help="Wan frame rate before interpolation (default 16)")
    ap.add_argument("--interpolate", type=int, help="FILM multiplier, 1 = off (default 2 for Wan, 1 for H3)")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--loop", action="store_true", default=None, help="animate back from the last keyframe to the first")
    ap.add_argument("--quality", action="store_true", help="20-step sampling without speed LoRAs (much slower)")
    ap.add_argument("--output", help="output .mp4 path (default output/animation.mp4)")
    args = ap.parse_args()

    if args.list_styles:
        for name, s in load_styles().items():
            print(f"{name:20} {s.get('label', '')}{'  [restyles keyframes]' if s.get('restyle_prompt') else ''}")
        return
    if not args.storyboard and not args.keyframes:
        ap.error("give a storyboard JSON or --keyframes FOLDER")

    overrides, base = {}, os.getcwd()
    if args.storyboard:
        with open(args.storyboard, encoding="utf-8") as f:
            overrides = json.load(f)
        base = os.path.dirname(os.path.abspath(args.storyboard))
    if args.keyframes:
        folder = os.path.abspath(args.keyframes)
        overrides["keyframes"] = sorted(os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(IMAGE_EXTS))
    if args.prompt:
        overrides["prompts"] = [args.prompt]
    if args.quality:
        overrides["fast"] = False
    for key in ("server", "width", "height", "length", "fps", "interpolate", "seed", "loop", "output",
                "style", "custom_style", "restyle", "engine", "sound", "audio", "image_model", "turbo_steps"):
        if getattr(args, key) is not None:
            overrides[key] = getattr(args, key)

    try:
        cfg = make_config(overrides, base)
        run(cfg)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"\nError: {e}")


if __name__ == "__main__":
    main()

"""Game-ready tools for studio assets: import models, split images into parts, icons, sprites,
Meshy rigging and animation, and engine-ready ZIP exports.

Everything is saved next to the asset, in the same ``<asset-name>/<kind>/`` folders the studio uses:
``3d`` (models, also dropped ones), ``views`` (turnaround images), ``rig`` (rigged character),
``animations`` (animation clips), ``parts`` (cut-outs), ``icons`` and ``sprites``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from forge.asset_studio import ASSET_KINDS, _ws_rel, output_dir, read_asset, update_asset
from forge.blueprint_contract import serialize_frontmatter, split_frontmatter
from forge.config import load_config
from forge.overview import IMAGE_SUFFIXES, MODEL_SUFFIXES, resolve_rel, slugify

MESHY_BASE_URL = "https://api.meshy.ai"
MAX_MODEL_BYTES = 200 * 1024 * 1024
MODEL_SIDECAR_SUFFIXES = {".bin", ".mtl", ".png", ".jpg", ".jpeg", ".webp", ".tga", ".bmp"}
ICON_SIZES = (32, 64, 128, 256, 512)
ENGINES = ("godot", "unity", "unreal")
# Meshy credits are about one US cent each (Make 3D: 30 credits ≈ $0.30).
RIG_CREDITS = 5
RIG_MAX_TRIANGLES = 380_000
ANIMATION_CREDITS = 3


def _unique(path: Path) -> Path:
    candidate, index = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        index += 1
    return candidate


def _clean_stem(name: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem).strip("-")[:60] or fallback


def _asset_image(ws_dir: Path, rel: str) -> Path:
    path = resolve_rel(ws_dir, rel)
    if path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
        raise FileNotFoundError(f"No image '{rel}'.")
    return path


# ---------- dropped models ----------

def save_model(ws_dir: Path, asset_rel: str, filename: str, data: bytes) -> str:
    """Store a 3D file (GLB/glTF/OBJ/FBX) in the asset's 3D outputs, like a generated model."""
    asset_path, _, _ = read_asset(ws_dir, asset_rel)
    suffix = Path(filename).suffix.lower()
    if suffix not in MODEL_SUFFIXES:
        raise ValueError(f"'{filename}' is not a 3D model (GLB, glTF, OBJ or FBX).")
    if len(data) > MAX_MODEL_BYTES:
        raise ValueError("Model is larger than 200 MB.")
    folder = output_dir(asset_path) / "3d"
    folder.mkdir(parents=True, exist_ok=True)
    target = _unique(folder / f"{_clean_stem(filename, 'model')}{suffix}")
    target.write_bytes(data)
    return _ws_rel(ws_dir, target)


def save_model_files(ws_dir: Path, asset_rel: str, files: list[tuple[str, bytes]]) -> list[str]:
    """Store dropped models with the files they point at (glTF buffers, OBJ materials, textures). Those keep their
    exact names, since the model refers to them by name, and are left out of the returned list of models."""
    if not any(Path(name).suffix.lower() in MODEL_SUFFIXES for name, _ in files):
        raise ValueError("Drop at least one 3D model (GLB, glTF, OBJ or FBX).")
    asset_path, _, _ = read_asset(ws_dir, asset_rel)
    folder = output_dir(asset_path) / "3d"
    saved = []
    for filename, data in files:
        name = re.split(r"[\\/]", filename)[-1]
        if Path(name).suffix.lower() in MODEL_SUFFIXES:
            saved.append(save_model(ws_dir, asset_rel, name, data))
        elif Path(name).suffix.lower() in MODEL_SIDECAR_SUFFIXES:
            if len(data) > MAX_MODEL_BYTES:
                raise ValueError(f"'{name}' is larger than 200 MB.")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
    return saved


def save_thumb(ws_dir: Path, model_rel: str, png: bytes) -> str:
    """Save a preview picture for a model (rendered in the browser) as ``<model>_thumb.png``."""
    model = resolve_rel(ws_dir, model_rel)
    if model.suffix.lower() not in MODEL_SUFFIXES or not model.is_file():
        raise FileNotFoundError(f"No model '{model_rel}'.")
    Image.open(io.BytesIO(png)).verify()
    thumb = model.with_name(f"{model.stem}_thumb.png")
    thumb.write_bytes(png)
    return _ws_rel(ws_dir, thumb)


# ---------- split an image into parts ----------

def _foreground_mask(image: Image.Image) -> Image.Image:
    """White where there is something drawn: alpha if the image has it, else distance from the corner colour."""
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    if alpha.getextrema()[0] < 250:
        return alpha.point(lambda a: 255 if a > 24 else 0)
    rgb = rgba.convert("RGB")
    w, h = rgb.size
    corners = [rgb.getpixel((0, 0)), rgb.getpixel((w - 1, 0)), rgb.getpixel((0, h - 1)), rgb.getpixel((w - 1, h - 1))]
    bg = tuple(sorted(c[i] for c in corners)[1] for i in range(3))
    red, green, blue = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg)).split()
    return ImageChops.add(ImageChops.add(red, green), blue).point(lambda d: 255 if d > 60 else 0)


def _runs(profile: list[int], min_gap: int, min_size: int) -> list[tuple[int, int]]:
    runs, start, gap = [], None, 0
    for i, value in enumerate(profile + [0] * (min_gap + 1)):
        if value:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > min_gap:
                end = i - gap + 1
                if end - start >= min_size:
                    runs.append((start, end))
                start, gap = None, 0
    return runs


def detect_parts(ws_dir: Path, image_rel: str) -> list[dict[str, float]]:
    """Guess one box per separate figure/object, left to right, as fractions of the image (0..1)."""
    image = Image.open(_asset_image(ws_dir, image_rel))
    scale = min(1.0, 512 / max(image.size))
    small = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
    mask = _foreground_mask(small)
    w, h = mask.size
    pixels = mask.load()
    cols = [sum(1 for y in range(h) if pixels[x, y]) > max(1, h // 200) for x in range(w)]
    boxes = []
    for x0, x1 in _runs([int(c) for c in cols], max(2, w // 80), max(4, w // 40)):
        rows = [int(any(pixels[x, y] for x in range(x0, x1))) for y in range(h)]
        spans = _runs(rows, max(2, h // 60), max(4, h // 40))
        if not spans:
            continue
        y0, y1 = spans[0][0], spans[-1][1]
        pad_x, pad_y = (x1 - x0) * 0.04, (y1 - y0) * 0.04
        boxes.append({
            "x": max(0.0, (x0 - pad_x) / w), "y": max(0.0, (y0 - pad_y) / h),
            "w": min(1.0, (x1 - x0 + 2 * pad_x) / w), "h": min(1.0, (y1 - y0 + 2 * pad_y) / h),
        })
    return boxes


def detect_sheet_items(ws_dir: Path, image_rel: str) -> list[dict[str, float]]:
    """One box per object on a sheet (a prop sheet), row by row. Objects are the separate blobs on the background;
    bits close together (a pile of bones, rubble round a pillar) count as one."""
    image = Image.open(_asset_image(ws_dir, image_rel))
    scale = min(1.0, 320 / max(image.size))
    mask = _foreground_mask(image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale)))))
    mask = mask.filter(ImageFilter.MaxFilter(7))  # join an object's loose bits
    w, h = mask.size
    pixels = mask.load()
    seen = bytearray(w * h)
    boxes = []
    for start in range(w * h):
        if seen[start] or not pixels[start % w, start // w]:
            continue
        seen[start] = 1
        stack, x0, y0, x1, y1, size = [start], w, h, 0, 0, 0
        while stack:
            i = stack.pop()
            x, y = i % w, i // w
            x0, y0, x1, y1, size = min(x0, x), min(y0, y), max(x1, x), max(y1, y), size + 1
            for n in (i - 1 if x else -1, i + 1 if x < w - 1 else -1, i - w, i + w):
                if 0 <= n < w * h and not seen[n] and pixels[n % w, n // w]:
                    seen[n] = 1
                    stack.append(n)
        if size > w * h * 0.002:
            boxes.append([x0, y0, x1 + 1, y1 + 1])
    # The dilation grew every box by 3 px: give that back as padding, and read the boxes in rows, top to bottom.
    out = [{"x": max(0.0, b[0] / w), "y": max(0.0, b[1] / h), "w": min(1.0, (b[2] - b[0]) / w), "h": min(1.0, (b[3] - b[1]) / h)} for b in boxes]
    rows: list[list[dict[str, float]]] = []
    for box in sorted(out, key=lambda b: b["y"] + b["h"] / 2):
        mid = box["y"] + box["h"] / 2
        row = next((r for r in rows if abs(mid - (r[0]["y"] + r[0]["h"] / 2)) < max(r[0]["h"], box["h"]) / 2), None)
        (row.append(box) if row else rows.append([box]))
    return [b for r in rows for b in sorted(r, key=lambda b: b["x"])][:24]


def _knock_out_background(crop: Image.Image) -> Image.Image:
    """A cut-out from a flat-background sheet: the background around the object becomes transparent, holes inside it stay."""
    rgba = crop.convert("RGBA")
    if rgba.getchannel("A").getextrema()[0] < 250:
        return rgba  # already cut out
    mask = _foreground_mask(rgba).convert("L")
    # Only background that touches the edge goes: light parts inside the object are not holes.
    w, h = mask.size
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if mask.getpixel(corner) == 0:
            ImageDraw.floodfill(mask, corner, 128)
    rgba.putalpha(mask.point(lambda v: 0 if v == 128 else 255))
    bbox = rgba.getchannel("A").getbbox()
    return rgba.crop(bbox) if bbox else rgba


BODY_PARTS = ("head", "torso", "left arm", "right arm", "left leg", "right leg")
BODY_PROMPT = """Look at this game character concept image and find its body parts for cutting it into pieces.
Return ONLY JSON: {"parts": [{"name": "head", "x": 0.41, "y": 0.02, "w": 0.18, "h": 0.17}, ...]}
x, y = top-left corner, w, h = size, all as fractions of the image width/height (0..1).
Include head, torso, left arm, right arm, left leg, right leg ("left"/"right" from the character's own point
of view, so the character's left arm is on the image's right side when it faces the viewer). Arms include
hands, legs include feet. Add separate held items or accessories (sword, shield, cape, hat…) as extra parts
with short names. Boxes may overlap a little; make each box tight but complete. If there are several
characters, use the largest one."""


def _clamp_box(box: dict[str, Any]) -> dict[str, Any] | None:
    try:
        x, y, w, h = (min(1.0, max(0.0, float(box[k]))) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    w, h = min(w, 1 - x), min(h, 1 - y)
    if w < 0.02 or h < 0.02:
        return None
    return {"x": x, "y": y, "w": w, "h": h, "name": str(box.get("name") or "").strip()[:60]}


def body_template(ws_dir: Path, image_rel: str) -> list[dict[str, Any]]:
    """Proportion-based guess for a standing figure (T- or A-pose): head, torso, arms and legs."""
    image = Image.open(_asset_image(ws_dir, image_rel))
    scale = min(1.0, 512 / max(image.size))
    mask = _foreground_mask(image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale)))))
    W, H = mask.size
    x0, y0, x1, y1 = mask.getbbox() or (0, 0, W, H)
    h = y1 - y0
    pixels = mask.load()

    def span(row: int) -> tuple[int, int]:
        xs = [x for x in range(x0, x1) if pixels[x, min(H - 1, row)]]
        return (min(xs), max(xs)) if xs else (x0, x1)

    # The hips are the narrowest solid row of the body; their width is close to the torso's.
    hip_l, hip_r = span(y0 + int(h * 0.5))
    mid = (hip_l + hip_r) / 2
    torso_w = max(hip_r - hip_l, (x1 - x0) * 0.22)
    head_w = torso_w * 0.7
    tl, tr = mid - torso_w / 2, mid + torso_w / 2
    raw = [
        ("head", mid - head_w / 2, y0, head_w, h * 0.17),
        ("torso", tl, y0 + h * 0.15, torso_w, h * 0.38),
        ("right arm", x0, y0 + h * 0.14, max(tl - x0, (x1 - x0) * 0.08), h * 0.42),  # character's right = image left
        ("left arm", tr, y0 + h * 0.14, max(x1 - tr, (x1 - x0) * 0.08), h * 0.42),
        ("right leg", tl - torso_w * 0.05, y0 + h * 0.5, torso_w * 0.55, h * 0.5),
        ("left leg", mid - torso_w * 0.0, y0 + h * 0.5, torso_w * 0.55, h * 0.5),
    ]
    boxes = [_clamp_box({"name": n, "x": x / W, "y": y / H, "w": w / W, "h": hh / H}) for n, x, y, w, hh in raw]
    return [b for b in boxes if b]


async def guess_body_parts(ws_dir: Path, image_rel: str, model_call: Any) -> dict[str, Any]:
    """Ask Glob's model where the head, torso, arms and legs are; fall back to body proportions."""
    path = _asset_image(ws_dir, image_rel)
    image = Image.open(path).convert("RGBA")
    image.thumbnail((768, 768))
    flat = Image.new("RGB", image.size, (255, 255, 255))
    flat.paste(image, mask=image.getchannel("A"))
    buffer = io.BytesIO()
    flat.save(buffer, "PNG")
    picture = [("image/png", base64.b64encode(buffer.getvalue()).decode("ascii"))]
    note = ""
    for _attempt in range(2):  # local models sometimes time out on the first image of a session
        try:
            result = await model_call(prompt=BODY_PROMPT, system_prompt="You locate parts in images and answer with JSON only.",
                                      images_b64=picture, effort="low")
            text = str(result.get("text") or "")
            match = re.search(r"\{.*\}", text, re.S)
            parts = json.loads(match.group(0)).get("parts") if match else None
            boxes = [b for b in (_clamp_box(p) for p in parts or []) if b and b["name"]]
            if len(boxes) >= 3:
                return {"boxes": boxes[:16], "source": "glob", "model": result.get("model") or ""}
            note = "Glob's answer had no usable boxes"
        except Exception as exc:  # no model configured, no vision support, timeout, bad JSON…
            reason = str(exc).strip() or type(exc).__name__
            if "timeout" in reason.lower() or "Timeout" in type(exc).__name__:
                reason = "the model took too long to answer"
            note = f"Glob could not look at the image ({reason[:160]})"
    return {"boxes": body_template(ws_dir, image_rel), "source": "template", "note": note}


async def name_sheet_items(ws_dir: Path, image_rel: str, boxes: list[dict[str, Any]], model_call: Any) -> dict[str, Any]:
    """Ask Glob's model what each numbered box on a sheet shows ("wooden barrel"), for the props' names."""
    image = Image.open(_asset_image(ws_dir, image_rel)).convert("RGB")
    image.thumbnail((1024, 1024))
    draw = ImageDraw.Draw(image)
    for i, box in enumerate(boxes, start=1):
        x, y = float(box["x"]) * image.width, float(box["y"]) * image.height
        draw.rectangle((x, y, x + float(box["w"]) * image.width, y + float(box["h"]) * image.height), outline=(255, 0, 90), width=3)
        draw.rectangle((x, y, x + 26, y + 20), fill=(255, 0, 90))
        draw.text((x + 7, y + 4), str(i), fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    prompt = (f"This prop sheet has {len(boxes)} numbered pink boxes. Name the object in each box in two or three plain words "
              'for a game asset list (e.g. "wooden barrel", "iron torch"). Answer ONLY JSON: {"names": ["...", ...]} '
              f"with exactly {len(boxes)} names, box 1 first.")
    try:
        result = await model_call(prompt=prompt, system_prompt="You name objects in images and answer with JSON only.",
                                  images_b64=[("image/png", base64.b64encode(buffer.getvalue()).decode("ascii"))], effort="low")
        match = re.search(r"\{.*\}", str(result.get("text") or ""), re.S)
        names = [str(n).strip()[:60] for n in (json.loads(match.group(0)).get("names") or [])] if match else []
        if len(names) == len(boxes) and all(names):
            return {"names": names, "source": "glob"}
        note = "Glob's answer didn't name every box"
    except Exception as exc:  # no chat model, no vision, timeout, bad JSON: the user names them
        note = f"Glob could not look at the sheet ({(str(exc).strip() or type(exc).__name__)[:160]})"
    return {"names": [f"Prop {i}" for i in range(1, len(boxes) + 1)], "source": "numbers", "note": note}


def is_trim_sheet(name: str) -> bool:
    """A level's trim sheet, as named when it was made. Its pieces go with the level's pack, not its props."""
    return "-trim-" in name


def split_image(ws_dir: Path, asset_rel: str, image_rel: str, boxes: list[dict[str, Any]], as_assets: bool = False) -> list[str]:
    """Cut boxes out of an image. Parts go to ``<asset>/parts/``; with ``as_assets`` every part becomes
    its own asset next to this one, with the cut-out as its winner image.

    A level kit's prop sheet becomes props in a folder named after the level, each cut out of the sheet's background."""
    asset_path, meta, _ = read_asset(ws_dir, asset_rel)
    image = Image.open(_asset_image(ws_dir, image_rel))
    level_sheet = meta.get("kind") == "level"
    trim_sheet = level_sheet and is_trim_sheet(Path(image_rel).name)
    if level_sheet and as_assets:
        props_dir = asset_path.parent / f"{asset_path.stem}-pack" / "trim" if trim_sheet else asset_path.parent / f"{asset_path.stem}-props"
        props_dir.mkdir(parents=True, exist_ok=True)
    if not boxes:
        raise ValueError("Draw at least one box.")
    created = []
    for index, box in enumerate(boxes[:24], start=1):
        x, y = float(box.get("x", 0)), float(box.get("y", 0))
        bw, bh = float(box.get("w", 0)), float(box.get("h", 0))
        left, top = max(0, round(x * image.width)), max(0, round(y * image.height))
        right, bottom = min(image.width, round((x + bw) * image.width)), min(image.height, round((y + bh) * image.height))
        if right - left < 4 or bottom - top < 4:
            continue
        crop = image.crop((left, top, right, bottom))
        if level_sheet:
            crop = _knock_out_background(crop)
        name = " ".join(word[:1].upper() + word[1:] for word in (str(box.get("name") or "").strip() or f"{asset_path.stem} part {index}").split())
        slug = slugify(name) or f"part-{index}"
        if as_assets:
            new_md = _unique((props_dir if level_sheet else asset_path.parent) / f"{slug}.md")
            folder = output_dir(new_md) / "candidates"
            folder.mkdir(parents=True, exist_ok=True)
            picture = folder / f"{new_md.stem}.png"
            crop.save(picture)
            kind = meta.get("kind") if meta.get("kind") in ASSET_KINDS and not level_sheet else "prop"
            fields = {"kind": kind, "prompt": name, "winner": _ws_rel(ws_dir, picture), "split_from": asset_rel}
            new_md.write_text(serialize_frontmatter(fields, f"# {name}\n\nCut from {asset_path.stem}.\n"), encoding="utf-8")
            created.append(_ws_rel(ws_dir, new_md))
        else:
            folder = output_dir(asset_path) / "parts"
            folder.mkdir(parents=True, exist_ok=True)
            target = _unique(folder / f"{slug}.png")
            crop.save(target)
            created.append(_ws_rel(ws_dir, target))
    if not created:
        raise ValueError("The boxes were too small to cut.")
    if level_sheet and as_assets:
        update_asset(ws_dir, asset_rel, {"trim_cut" if trim_sheet else "props_cut": len(created)})  # that part of the pack is done
    return created


# ---------- icons and sprites ----------

def make_icons(ws_dir: Path, asset_rel: str, image_rel: str, sizes: list[int] | None = None, padding: float = 0.08) -> list[str]:
    """Square, transparent inventory icons: trimmed to the subject, padded and scaled to each size."""
    asset_path, _, _ = read_asset(ws_dir, asset_rel)
    image = Image.open(_asset_image(ws_dir, image_rel)).convert("RGBA")
    if image.getchannel("A").getextrema()[0] >= 250:
        image = _knock_out_background(image)  # a flat background goes, so icons sit on any UI; light parts of the subject stay
    bbox = _foreground_mask(image).getbbox() or (0, 0, image.width, image.height)
    subject = image.crop(bbox)
    side = int(max(subject.size) * (1 + 2 * max(0.0, min(padding, 0.4))))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(subject, ((side - subject.width) // 2, (side - subject.height) // 2), subject)
    folder = output_dir(asset_path) / "icons"
    folder.mkdir(parents=True, exist_ok=True)
    created = []
    for size in sorted({int(s) for s in (sizes or [64, 128, 256]) if int(s) in ICON_SIZES}):
        target = folder / f"{asset_path.stem}-icon-{size}.png"
        canvas.resize((size, size), Image.LANCZOS).save(target)
        created.append(_ws_rel(ws_dir, target))
    if not created:
        raise ValueError(f"Pick icon sizes from {', '.join(map(str, ICON_SIZES))}.")
    return created


def save_sprite(ws_dir: Path, asset_rel: str, name: str, png: bytes, meta: dict[str, Any] | None = None) -> str:
    """Store a sprite sheet rendered in the browser, with a JSON file describing its frames."""
    asset_path, _, _ = read_asset(ws_dir, asset_rel)
    Image.open(io.BytesIO(png)).verify()
    folder = output_dir(asset_path) / "sprites"
    folder.mkdir(parents=True, exist_ok=True)
    target = _unique(folder / f"{_clean_stem(name, 'sprites')}.png")
    target.write_bytes(png)
    if meta:
        target.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return _ws_rel(ws_dir, target)


# ---------- Meshy rigging and animation ----------

def _meshy_key() -> str:
    key = load_config().get_key("meshy")
    if not key:
        raise ValueError("Add a Meshy API key in Settings to rig and animate.")
    return key


async def _meshy_task(client: httpx.AsyncClient, path: str, payload: dict[str, Any], key: str, timeout_s: float = 900) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {key}"}
    resp = await client.post(f"{MESHY_BASE_URL}{path}", headers=headers, json=payload)
    if resp.status_code == 402:
        raise ValueError("Not enough Meshy credits.")
    if resp.status_code >= 400:
        raise ValueError(f"Meshy rejected the request ({resp.status_code}): {resp.text[:240]}")
    task_id = resp.json()["result"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        status = await client.get(f"{MESHY_BASE_URL}{path}/{task_id}", headers=headers)
        status.raise_for_status()
        data = status.json()
        if data.get("status") == "SUCCEEDED":
            return data
        if data.get("status") in ("FAILED", "CANCELED"):
            message = (data.get("task_error") or {}).get("message") or data.get("status")
            raise ValueError(f"Meshy task failed: {message}")
    raise ValueError("Meshy took too long; try again later.")


async def _download(client: httpx.AsyncClient, url: str, target: Path) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(resp.content)
    return target.name


def glb_triangles(path: Path) -> int | None:
    """Triangle count from a GLB's JSON chunk (indexed and plain triangle primitives), or None if unreadable."""
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
            if header[:4] != b"glTF":
                return None
            length = int.from_bytes(header[12:16], "little")
            gltf = json.loads(handle.read(length))
    except (OSError, ValueError):
        return None
    accessors = gltf.get("accessors") or []
    total = 0
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            if prim.get("mode", 4) != 4:
                continue
            index = prim.get("indices")
            source = index if index is not None else (prim.get("attributes") or {}).get("POSITION")
            if source is not None and source < len(accessors):
                total += int(accessors[source].get("count") or 0) // 3
    return total


async def rig_model(ws_dir: Path, asset_rel: str, model_rel: str, height_m: float = 1.7) -> list[str]:
    """Auto-rig a humanoid GLB with Meshy. Saves the rigged GLB/FBX in ``rig/`` and the free walk/run
    clips in ``animations/``; remembers the rig task in the asset header for later animations."""
    asset_path, meta, body = read_asset(ws_dir, asset_rel)
    model = resolve_rel(ws_dir, model_rel)
    if model.suffix.lower() != ".glb" or not model.is_file():
        raise ValueError("Rigging needs a GLB model.")
    if model.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("The model is over 100 MB; remesh it to fewer polygons first.")
    triangles = glb_triangles(model)
    if triangles and triangles > RIG_MAX_TRIANGLES:
        raise ValueError(f"This model has {triangles:,} triangles; Meshy can rig up to {RIG_MAX_TRIANGLES:,}. "
                         "Run Make 3D again with \"Remesh to a target polycount\" on (30k is plenty for a character).")
    key = _meshy_key()
    data_uri = "data:model/gltf-binary;base64," + base64.b64encode(model.read_bytes()).decode()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    created: list[str] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
        task = await _meshy_task(client, "/openapi/v1/rigging", {"model_url": data_uri, "height_meters": max(0.1, float(height_m))}, key)
        result = task.get("result") or {}
        rig_dir = output_dir(asset_path) / "rig"
        stem = f"{asset_path.stem}-rigged-{stamp}"
        for fmt in ("glb", "fbx"):
            if url := result.get(f"rigged_character_{fmt}_url"):
                await _download(client, url, rig_dir / f"{stem}.{fmt}")
                created.append(_ws_rel(ws_dir, rig_dir / f"{stem}.{fmt}"))
        thumb = model.with_name(f"{model.stem}_thumb.png")
        if thumb.is_file():
            (rig_dir / f"{stem}_thumb.png").write_bytes(thumb.read_bytes())
        anim_dir = output_dir(asset_path) / "animations"
        for clip in ("walking", "running"):
            for fmt in ("glb", "fbx"):
                if url := (result.get("basic_animations") or {}).get(f"{clip}_{fmt}_url"):
                    await _download(client, url, anim_dir / f"{asset_path.stem}-{clip}-{stamp}.{fmt}")
                    if fmt == "glb":
                        created.append(_ws_rel(ws_dir, anim_dir / f"{asset_path.stem}-{clip}-{stamp}.glb"))
    meta = {**meta, "rig_task": task.get("id") or "", "rig_model": created[0] if created else ""}
    asset_path.write_text(serialize_frontmatter(meta, body), encoding="utf-8")
    return created


async def animation_library(search: str = "") -> list[dict[str, Any]]:
    """Meshy's preset animations (free to list), normalised to id / name / category / preview."""
    key = _meshy_key()
    params = {"search": search} if search else {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{MESHY_BASE_URL}/openapi/v1/animations/library", headers={"Authorization": f"Bearer {key}"}, params=params)
        if resp.status_code >= 400:
            raise ValueError(f"Could not load Meshy's animation list ({resp.status_code}).")
        data = resp.json()
    items = data.get("result") if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = items.get("items") or items.get("data") or []
    out = []
    for item in items or []:
        action_id = item.get("action_id", item.get("id"))
        if action_id is None:
            continue
        out.append({
            "id": int(action_id),
            "name": item.get("name") or item.get("action_name") or f"Action {action_id}",
            "category": item.get("category") or "",
            "sub_category": item.get("sub_category") or "",
            "preview": item.get("preview_url") or item.get("gif_url") or item.get("thumbnail_url") or item.get("video_url") or "",
        })
    return out


async def animate(ws_dir: Path, asset_rel: str, action_ids: list[int], names: list[str] | None = None, fps: int = 30) -> list[str]:
    """Apply 1-10 Meshy preset animations to the asset's rig; one GLB/FBX with a clip per action."""
    asset_path, meta, _ = read_asset(ws_dir, asset_rel)
    rig_task = str(meta.get("rig_task") or "")
    if not rig_task:
        raise ValueError("Rig the 3D model first.")
    ids = list(dict.fromkeys(int(a) for a in action_ids))[:10]
    if not ids:
        raise ValueError("Pick at least one animation.")
    key = _meshy_key()
    payload: dict[str, Any] = {"rig_task_id": rig_task}
    payload.update({"action_id": ids[0]} if len(ids) == 1 else {"action_ids": ids})
    if fps in (24, 25, 60):
        payload["post_process"] = {"operation_type": "change_fps", "fps": fps}
    label = slugify("-".join((names or [])[:3])) or f"anim-{'-'.join(map(str, ids[:3]))}"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    created: list[str] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        task = await _meshy_task(client, "/openapi/v1/animations", payload, key)
        result = task.get("result") or {}
        folder = output_dir(asset_path) / "animations"
        stem = f"{asset_path.stem}-{label}-{stamp}"
        for field, suffix in (("animation_glb_url", "glb"), ("processed_animation_fps_fbx_url", "fbx"), ("animation_fbx_url", "fbx")):
            target = folder / f"{stem}.{suffix}"
            if (url := result.get(field)) and not target.exists():
                await _download(client, url, target)
                created.append(_ws_rel(ws_dir, target))
    return created


# ---------- engine export ----------

ENGINE_ROOT = {"godot": "assets", "unity": "Assets/Art", "unreal": "Content/Art"}
ENGINE_MODELS = {"godot": (".glb", ".gltf", ".obj"), "unity": (".fbx", ".glb", ".obj"), "unreal": (".fbx", ".glb", ".obj")}
EXPORT_KINDS = ("3d", "rig", "animations", "textures", "icons", "sprites", "parts", "views")


def _asset_files(ws_dir: Path, asset_path: Path, meta: dict[str, Any], engine: str) -> list[tuple[Path, str]]:
    """(file, path inside the asset's export folder) for one asset."""
    files: list[tuple[Path, str]] = []
    winner = str(meta.get("winner") or "")
    if winner:
        picture = resolve_rel(ws_dir, winner)
        if picture.is_file():
            files.append((picture, f"concept{picture.suffix.lower()}"))
    base = output_dir(asset_path)
    for kind in EXPORT_KINDS:
        folder = base / kind
        if not folder.is_dir():
            continue
        for file in sorted(folder.iterdir()):
            suffix = file.suffix.lower()
            if not file.is_file() or file.stem.endswith("_thumb"):
                continue
            if suffix in MODEL_SUFFIXES and suffix not in ENGINE_MODELS[engine]:
                continue
            if suffix in MODEL_SUFFIXES or suffix in IMAGE_SUFFIXES or suffix in MODEL_SIDECAR_SUFFIXES or suffix == ".json":
                sub = {"3d": "models", "rig": "models"}.get(kind, kind)
                files.append((file, f"{sub}/{file.name}"))
    return files


def export_zip(ws_dir: Path, rel: str, engine: str) -> tuple[bytes, str]:
    """ZIP of one asset or every asset in a folder, laid out for the engine's project folder."""
    if engine not in ENGINES:
        raise ValueError(f"Engine must be one of: {', '.join(ENGINES)}.")
    target = resolve_rel(ws_dir, rel) if rel else ws_dir.resolve()
    if target.is_file():
        assets = [target]
        # A level takes its whole asset pack along.
        if split_frontmatter(target.read_text(encoding="utf-8", errors="ignore"))[0].get("kind") == "level":
            for folder in (target.parent / f"{target.stem}-props", target.parent / f"{target.stem}-pack"):
                if folder.is_dir():
                    assets += sorted(folder.rglob("*.md"))
    elif target.is_dir():
        assets = sorted(p for p in target.rglob("*.md") if "pillars" not in p.parts and ".trash" not in p.parts)
    else:
        raise FileNotFoundError(f"Nothing at '{rel}'.")
    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for md in assets:
            try:
                asset_path, meta, _ = read_asset(ws_dir, _ws_rel(ws_dir, md))
            except (FileNotFoundError, ValueError):
                continue
            if not meta.get("kind"):
                continue
            folder_rel = Path(_ws_rel(ws_dir, asset_path)).with_suffix("")
            clean = "/".join(re.sub(r"^\d+[-_ ]+", "", part) for part in folder_rel.parts)
            for file, inner in _asset_files(ws_dir, asset_path, meta, engine):
                archive.write(file, f"{ENGINE_ROOT[engine]}/{clean}/{inner}")
                count += 1
        if not count:
            raise ValueError("No generated files to export yet.")
        archive.writestr("README.txt", (
            f"Exported from Forge for {engine.title()} on {time.strftime('%Y-%m-%d')}.\n"
            f"Copy the '{ENGINE_ROOT[engine].split('/')[0]}' folder into your project root.\n"
            "models/ holds meshes (rigged ones end in -rigged), animations/ the clips, icons/ and sprites/ 2D game art,\n"
            "concept.png the chosen concept image.\n"
        ))
    name = slugify(Path(rel).stem if rel else "project") or "export"
    return buffer.getvalue(), f"{name}-{engine}.zip"

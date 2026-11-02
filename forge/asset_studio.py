"""Asset studio: generate images and 3D for any Markdown asset, whatever the folder structure.

Conventions (no folder names are assumed):
  * An asset is any ``.md`` whose header has a ``kind:`` (character, item, ui, …). Its ``prompt:``
    is the subject description. ``winner:`` records the chosen image.
  * Pillars are reference images that keep the art style consistent. Images in ``pillars/`` at the
    workspace root are the project bible and go with every generation; ``pillars/`` inside any folder
    applies to every asset below that folder. Pillars stack down the asset's folder path.
  * Text files (``.md``/``.txt``) in a ``pillars/`` folder are the style bible: text that is put in
    front of every prompt below that folder (T-pose, polygon budget, palette…), inherited the same way.
  * A folder README header may set ``style:`` (a text rule that is inherited the same way).
  * Outputs are saved next to the asset in ``<asset-name>/{candidates,variants,edits,3d}/`` and in the
    project vault at ``vault/<asset path>/<kind>/`` with a JSON sidecar recording exactly what was sent.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import time
from pathlib import Path
from typing import Any

from forge.blueprint_contract import serialize_frontmatter, split_frontmatter
from forge.config import load_config
from forge.models import AssetType
from forge.overview import IMAGE_SUFFIXES, MODEL_SUFFIXES, FOLDER_PAGES, TRASH_DIR, move_to_trash, resolve_rel
from forge.prompt_sanitize import finalize_generation_prompt
from forge.providers.base import ProviderCapability
from forge.providers.elevenlabs_image import resolve_image_provider

PILLAR_DIR = "pillars"
REF_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_REFERENCES = 8
OUTPUT_KINDS = ("candidates", "variants", "edits", "views", "3d", "rig", "animations", "textures", "parts", "icons", "sprites", "audio")
# Imported engine files the browser can't preview (Godot .res clips, .blend, .anim) are still listed.
LISTED_FILE_SUFFIXES = {".res", ".tres", ".anim", ".blend", ".dae", ".tga", ".exr", ".psd"}
ACTIONS = ("candidates", "variant", "edit", "views", "make_3d", "rig", "animate")
ACTION_FOLDER = {"candidates": "candidates", "variant": "variants", "edit": "edits", "views": "views", "make_3d": "3d", "rig": "rig", "animate": "animations"}
# Turnaround views generated from the winner; "{view}" is filled in per image.
TURNAROUND_VIEWS = ("left side view (profile)", "back view", "three-quarter front view")
TEXT_PILLAR_SUFFIXES = {".md", ".txt"}
EXTRA_MODEL_SUFFIXES = {".usdz", ".stl"}
# Choices offered on the Make 3D sheet; anything else sent in a request falls back to the config default.
MESH_CHOICES: dict[str, list[Any]] = {
    "ai_model": ["meshy-7.1", "meshy-6", "meshy-6-lite", "meshy-t2.5", "meshy-t2"],
    "model_type": ["standard", "smart-topology"],
    "topology": ["triangle", "quad"],
    "texture_resolution": ["2k", "4k"],
    "pose_mode": ["", "a-pose", "t-pose"],
    "origin_at": ["bottom", "center"],
    "target_formats": ["glb", "obj", "fbx", "usdz", "stl"],
}
POLYCOUNT_PRESETS = (1500, 3000, 6000, 12000, 30000, 100000)
# The fixed set of asset kinds; the UI offers these as tags so nobody has to type them.
ASSET_KINDS = ("character", "creature", "item", "weapon", "prop", "environment", "vehicle", "ui", "vfx", "sfx", "voice", "music")
# The framing each kind usually wants when the asset has no `aspect:` of its own (else the Settings default).
KIND_ASPECT = {"environment": "16:9", "vfx": "16:9", "ui": "3:4"}
# Kinds that are a whole picture: their background is part of the image, so it is never cut out.
SCENE_KINDS = ("environment",)
# How strongly the pillars steer an asset (`pillars:` in its header): off, light, normal (default) or strong.
PILLAR_STRENGTHS = ("off", "light", "normal", "strong")
# Sound assets: their "candidates" are audio files in <asset>/audio/, the prompt describes the sound (or, for a voice,
# is the line to speak) and the winner is the chosen take.
AUDIO_KINDS = ("sfx", "voice", "music")
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".flac"}
AUDIO_MODELS = (
    # Signed-in services come first: when the user signed in, that is the account they mean to use.
    {"id": "svc:elevenlabs/sfx", "label": "ElevenLabs · sound effects (signed in)", "kinds": ["sfx"], "service": "elevenlabs"},
    {"id": "svc:elevenlabs/tts", "label": "ElevenLabs · voices (signed in)", "kinds": ["voice"], "service": "elevenlabs"},
    {"id": "svc:elevenlabs/music", "label": "ElevenLabs · music (signed in)", "kinds": ["music"], "service": "elevenlabs"},
    {"id": "elevenlabs_audio:sfx", "label": "ElevenLabs · sound effects", "kinds": ["sfx"], "key": "elevenlabs"},
    {"id": "stability_audio:sfx", "label": "Stable Audio · sound effects", "kinds": ["sfx"], "key": "stability"},
    {"id": "elevenlabs_audio:tts", "label": "ElevenLabs · voices", "kinds": ["voice"], "key": "elevenlabs"},
    {"id": "stability_audio:music", "label": "Stable Audio · music & loops", "kinds": ["music"], "key": "stability"},
)
AUDIO_CAPABILITY = {"sfx": ProviderCapability.SOUND_EFFECT, "tts": ProviderCapability.TEXT_TO_SPEECH, "music": ProviderCapability.TEXT_TO_MUSIC}

PILLAR_RULE = (
    "Use the attached reference images only for art style: rendering, brushwork, material handling, lighting and "
    "camera angle. They show other characters, so do not copy their gender, faces, bodies, proportions, pose, "
    "clothing, props, emblems, logos or markings. Create the new subject exactly as described at the top; the description decides who it "
    "is, and the style bible never overrides it."
)
PILLAR_RULE_LIGHT = (
    "Loosely follow the colour palette and rendering of the attached reference images. Copy nothing else from them: "
    "no subjects, shapes, emblems, logos, markings or props. Create the new subject exactly as described at the top."
)
PILLAR_RULE_STRONG = PILLAR_RULE + " Match their art style closely: same rendering, materials, outline and lighting."
VARIANT_RULE = (
    "Create a variant of the first attached image. Keep the same subject, silhouette, proportions, pose, "
    "camera angle and art style; change only what is described above."
)
VIEWS_RULE = (
    "Draw the exact same subject as the first attached image from a different angle: {view}. Keep the identity, "
    "outfit, colours, proportions, pose and art style identical; full body, centred, plain background."
)
EDIT_RULE = "Edit the first attached image. Change only what is described above and keep everything else identical."


# ---------- reading assets ----------

def _ws_rel(ws_dir: Path, path: Path) -> str:
    return path.resolve().relative_to(ws_dir.resolve()).as_posix()


def read_asset(ws_dir: Path, rel: str) -> tuple[Path, dict[str, Any], str]:
    path = resolve_rel(ws_dir, rel)
    if not path.is_file() or path.suffix.lower() != ".md":
        raise FileNotFoundError(f"No asset document '{rel}'.")
    meta, body, _ = split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
    return path, meta, body


def is_asset(meta: dict[str, Any]) -> bool:
    return bool(str(meta.get("kind") or "").strip())


def output_dir(asset_path: Path) -> Path:
    return asset_path.parent / asset_path.stem


def _folder_meta(directory: Path) -> dict[str, Any]:
    for name in FOLDER_PAGES + ("GAME.md",):
        page = directory / name
        if page.is_file():
            meta, _, _ = split_frontmatter(page.read_text(encoding="utf-8", errors="ignore"))
            return meta
    return {}


def _chain(ws_dir: Path, asset_path: Path) -> list[Path]:
    """Workspace root down to the asset's own folder."""
    root = ws_dir.resolve()
    folders = [root]
    for part in asset_path.resolve().parent.relative_to(root).parts:
        folders.append(folders[-1] / part)
    return folders


def _label(ws_dir: Path, folder: Path) -> str:
    rel = _ws_rel(ws_dir, folder)
    return "Project bible" if rel in ("", ".") else rel


def collect_pillars(ws_dir: Path, asset_path: Path) -> list[dict[str, str]]:
    pillars: list[dict[str, str]] = []
    for folder in _chain(ws_dir, asset_path):
        pillar_dir = folder / PILLAR_DIR
        if not pillar_dir.is_dir():
            continue
        for image in sorted(pillar_dir.iterdir(), key=lambda p: p.name.lower()):
            if image.is_file() and image.suffix.lower() in REF_IMAGE_SUFFIXES:
                pillars.append({"path": _ws_rel(ws_dir, image), "source": _label(ws_dir, folder)})
    return pillars


def read_text_pillar(path: Path) -> str:
    _, body, _ = split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
    lines = body.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def collect_text_pillars(ws_dir: Path, asset_path: Path) -> list[dict[str, str]]:
    """Style-bible text from every `pillars/` folder on the asset's path, project root first."""
    texts: list[dict[str, str]] = []
    for folder in _chain(ws_dir, asset_path):
        pillar_dir = folder / PILLAR_DIR
        if not pillar_dir.is_dir():
            continue
        for doc in sorted(pillar_dir.iterdir(), key=lambda p: p.name.lower()):
            if doc.is_file() and doc.suffix.lower() in TEXT_PILLAR_SUFFIXES:
                text = read_text_pillar(doc)
                if text:
                    texts.append({"path": _ws_rel(ws_dir, doc), "source": _label(ws_dir, folder), "text": text})
    return texts


def pillar_targets(ws_dir: Path, rel: str) -> list[dict[str, str]]:
    """Folders an image from this asset can become a pillar for: the project bible down to the asset's folder."""
    root = ws_dir.resolve()
    return [{"path": "" if f == root else _ws_rel(ws_dir, f), "label": _label(ws_dir, f)} for f in _chain(ws_dir, resolve_rel(ws_dir, rel))]


def make_pillar(ws_dir: Path, image_rel: str, folder_rel: str) -> str:
    """Copy an image into `<folder>/pillars/` so it is sent with every generation below that folder."""
    image = resolve_rel(ws_dir, image_rel)
    if not image.is_file() or image.suffix.lower() not in REF_IMAGE_SUFFIXES:
        raise FileNotFoundError(f"No image '{image_rel}'.")
    folder = resolve_rel(ws_dir, folder_rel) if folder_rel else ws_dir.resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"No folder '{folder_rel}'.")
    target_dir = folder / PILLAR_DIR
    target_dir.mkdir(exist_ok=True)
    target = target_dir / image.name
    index = 2
    while target.exists():
        target = target_dir / f"{image.stem}-{index}{image.suffix}"
        index += 1
    shutil.copy2(image, target)
    return _ws_rel(ws_dir, target)


def default_image_model(cfg: Any) -> str:
    """The Settings default image model, or, when that one has no key, the first model that can run.

    A user with only a Meshy key (or sign-in) must not get every first generation sent to Gemini without a key."""
    chosen = cfg.default_image_model
    if chosen.startswith("svc:") or cfg.demo_mode:
        return chosen
    models = list_image_models()
    if any(m["id"] == chosen and m["available"] for m in models):
        return chosen
    return next((m["id"] for m in models if m["available"]), chosen)


def list_image_models() -> list[dict[str, Any]]:
    """Image models the studio can send to, with price per image and whether a key is set."""
    from forge.providers.elevenlabs_image import IMAGE_MODELS, prefixed_model_id
    from forge.providers.gemini_image import MODEL_PRICING_USD

    from forge import meshy_login

    cfg = load_config()
    # How each provider is reached, shown in the model menu: a pasted key, the user's sign-in, or nothing yet.
    via = {name: "API key" if cfg.get_key(name, logins=False) else "" for name in ("gemini", "meshy", "elevenlabs")}
    if not via["meshy"] and meshy_login.signed_in():
        via["meshy"] = "signed in"
    models = []
    for model_id, price in MODEL_PRICING_USD.items():
        if model_id == "gemini-2.5-flash-image":
            continue
        meshy = model_id.startswith("meshy:")
        name = model_id.split(":", 1)[-1].removeprefix("gemini-").replace("-", " ").title().replace("Gpt", "GPT")
        name = re.sub(r"(\d) (\d)", r"\1.\2", name)
        models.append({"id": model_id, "label": f"{'Meshy' if meshy else 'Gemini'} · {name}", "price": price, "available": bool(via[provider := "meshy" if meshy else "gemini"]), "via": via[provider]})
    for model_id, spec in IMAGE_MODELS.items():
        models.append({"id": prefixed_model_id(model_id), "label": spec.label, "price": spec.usd, "available": bool(via["elevenlabs"]), "via": via["elevenlabs"]})
    return models


def collect_style_rules(ws_dir: Path, asset_path: Path, meta: dict[str, Any]) -> list[dict[str, str]]:
    rules = []
    for folder in _chain(ws_dir, asset_path):
        style = str(_folder_meta(folder).get("style") or "").strip()
        if style:
            rules.append({"source": _label(ws_dir, folder), "text": style})
    own = str(meta.get("style") or "").strip()
    if own:
        rules.append({"source": "This asset", "text": own})
    return rules


def list_outputs(ws_dir: Path, asset_path: Path) -> dict[str, list[dict[str, Any]]]:
    base = output_dir(asset_path)
    outputs: dict[str, list[dict[str, Any]]] = {kind: [] for kind in OUTPUT_KINDS}
    for kind in OUTPUT_KINDS:
        folder = base / kind
        if not folder.is_dir():
            continue
        files = sorted((p for p in folder.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        models: dict[str, dict[str, Any]] = {}
        for file in files:
            suffix = file.suffix.lower()
            if file.stem.endswith("_thumb"):
                continue
            if suffix in AUDIO_SUFFIXES:
                outputs[kind].append({"path": _ws_rel(ws_dir, file), "type": "audio"})
            elif suffix in IMAGE_SUFFIXES:
                if kind in ("3d", "rig", "animations"):
                    continue  # textures a model file points at, kept next to it
                outputs[kind].append({"path": _ws_rel(ws_dir, file), "type": "image"})
            elif suffix in LISTED_FILE_SUFFIXES:
                outputs[kind].append({"path": _ws_rel(ws_dir, file), "type": "file"})
            elif suffix in MODEL_SUFFIXES or suffix in EXTRA_MODEL_SUFFIXES:
                # One card per model: the GLB (or first file), with every other export format alongside.
                entry = models.get(file.stem)
                if entry is None:
                    entry = models[file.stem] = {"path": _ws_rel(ws_dir, file), "type": "model", "formats": {}}
                    thumb = file.with_name(f"{file.stem}_thumb.png")
                    if thumb.is_file():
                        entry["thumb"] = _ws_rel(ws_dir, thumb)
                    outputs[kind].append(entry)
                entry["formats"][suffix.lstrip(".")] = _ws_rel(ws_dir, file)
                if suffix == ".glb":
                    entry["path"] = _ws_rel(ws_dir, file)
    return outputs


def describe_asset(ws_dir: Path, rel: str) -> dict[str, Any]:
    path, meta, body = read_asset(ws_dir, rel)
    return {
        "path": _ws_rel(ws_dir, path),
        "is_asset": is_asset(meta),
        "kind": meta.get("kind") or "",
        "prompt": str(meta.get("prompt") or ""),
        "winner": meta.get("winner") or "",
        "meta": meta,
        "body": body,
        "pillars": collect_pillars(ws_dir, path),
        "text_pillars": collect_text_pillars(ws_dir, path),
        "style_rules": collect_style_rules(ws_dir, path, meta),
        "outputs": list_outputs(ws_dir, path),
    }


def update_asset(ws_dir: Path, rel: str, fields: dict[str, Any]) -> dict[str, Any]:
    path, meta, body = read_asset(ws_dir, rel)
    if fields.get("kind") and fields["kind"] not in ASSET_KINDS:
        raise ValueError(f"Kind must be one of: {', '.join(ASSET_KINDS)}.")
    winner = fields.get("winner")
    if winner and not resolve_rel(ws_dir, str(winner)).is_file():
        raise FileNotFoundError(f"No image '{winner}'.")
    for key, value in fields.items():
        if value is None or value == "":
            meta.pop(key, None)
        else:
            meta[key] = value
    path.write_text(serialize_frontmatter(meta, body), encoding="utf-8")
    if winner and meta.get("cover"):
        set_project_cover(ws_dir, str(winner))
    return describe_asset(ws_dir, rel)


# ---------- key art ----------
# A project's key art is an ordinary asset with `cover: true`: its winner is copied to the workspace's
# cover image, which the overview and the projects page show. Picking another winner updates the cover.

KEY_ART_FILE = "key-art.md"


def set_project_cover(ws_dir: Path, image_rel: str) -> str:
    image = resolve_rel(ws_dir, image_rel)
    if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
        raise FileNotFoundError(f"No image '{image_rel}'.")
    root = ws_dir.resolve()
    for old in root.iterdir():
        if old.is_file() and old.stem.lower() == "cover" and old.suffix.lower() in IMAGE_SUFFIXES:
            old.unlink()
    target = root / f"cover{image.suffix.lower()}"
    shutil.copy2(image, target)
    return target.name


def key_art_asset(ws_dir: Path, title: str, pitch: str) -> str:
    """The project's key-art asset, made on first use with a prompt from the project's pitch."""
    root = ws_dir.resolve()
    for doc in sorted(root.rglob("*.md")):
        if TRASH_DIR in doc.parts:
            continue
        try:
            meta, _, _ = split_frontmatter(doc.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("cover") and meta.get("kind"):
            return _ws_rel(ws_dir, doc)
    prompt = (f"Key art for the game \"{title}\". {pitch.strip()} A wide cinematic cover image: the hero in the "
              "middle of their world at its most exciting moment, dramatic lighting, strong silhouette, no text, no logo.")
    meta = {"kind": "environment", "prompt": re.sub(r"\s+", " ", prompt).strip(), "aspect": "16:9", "cover": True}
    body = f"# Key art\n\nThe cover of {title}: shown on the project page and the projects overview. Pick another winner to change it.\n"
    (root / KEY_ART_FILE).write_text(serialize_frontmatter(meta, body), encoding="utf-8")
    return KEY_ART_FILE


# ---------- batch & history ----------

def assets_under(ws_dir: Path, folder: str = "") -> list[str]:
    """Every asset document in a folder and below it, in the order the overview shows them."""
    from forge.overview import _visible

    root = ws_dir.resolve()
    base = resolve_rel(ws_dir, folder) if folder else root
    found = []
    for doc in sorted(base.rglob("*.md")):
        parts = doc.relative_to(root).parts
        if PILLAR_DIR in parts or not all(_visible(Path(part)) for part in parts):
            continue
        try:
            meta, _, _ = split_frontmatter(doc.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if is_asset(meta):
            found.append(_ws_rel(ws_dir, doc))
    return found


def missing_assets(ws_dir: Path, folder: str = "") -> list[str]:
    """Assets with nothing to pick from yet: no winner and no candidates or takes."""
    missing = []
    for rel in assets_under(ws_dir, folder):
        asset = describe_asset(ws_dir, rel)
        made = asset["outputs"]["audio"] if asset["kind"] in AUDIO_KINDS else asset["outputs"]["candidates"]
        described = asset["prompt"].strip() or re.sub(r"^#.*$", "", asset["body"], flags=re.M).strip()  # a title alone says too little
        if not asset["winner"] and not made and described:
            missing.append(rel)
    return missing


def output_history(ws_dir: Path, rel: str) -> dict[str, dict[str, Any]]:
    """What each output of an asset was made from (prompt, model, cost, time), read from the vault records."""
    asset_path = resolve_rel(ws_dir, rel)
    records = ws_dir.parent / "vault" / Path(_ws_rel(ws_dir, asset_path)).with_suffix("")
    history: dict[str, dict[str, Any]] = {}
    if not records.is_dir():
        return history
    for record in records.glob("*/*.json"):
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("file"):
            history[data["file"]] = {k: data.get(k) for k in ("action", "prompt", "asset_prompt", "model", "provider", "cost_usd", "created_at")}
    return history


def remove_output(ws_dir: Path, rel: str, output: str) -> dict[str, Any]:
    """Move one output of an asset to `.trash/`: an image, or a model with all its formats and thumbnail."""
    path, meta, body = read_asset(ws_dir, rel)
    target = resolve_rel(ws_dir, output)
    base = output_dir(path).resolve()
    if target.parent.parent != base or target.parent.name not in OUTPUT_KINDS or not target.is_file():
        raise FileNotFoundError(f"'{output}' is not an output of this asset.")
    if target.suffix.lower() in MODEL_SUFFIXES or target.suffix.lower() in EXTRA_MODEL_SUFFIXES:
        siblings = [p for p in target.parent.iterdir() if p.is_file() and p.stem in (target.stem, f"{target.stem}_thumb")
                    and (p.suffix.lower() in MODEL_SUFFIXES or p.suffix.lower() in EXTRA_MODEL_SUFFIXES or p.stem.endswith("_thumb"))]
    else:
        siblings = [target]
    removed = []
    for file in siblings:
        original = _ws_rel(ws_dir, file)
        moved = move_to_trash(ws_dir, file)
        if moved:
            removed.append({"from": original, "trash": moved})
    was_winner = bool(meta.get("winner")) and resolve_rel(ws_dir, str(meta["winner"])) == target
    if was_winner:
        meta.pop("winner", None)
        path.write_text(serialize_frontmatter(meta, body), encoding="utf-8")
    result = describe_asset(ws_dir, rel)
    result["removed"] = removed
    result["was_winner"] = was_winner
    return result


def restore_outputs(ws_dir: Path, moves: list[dict[str, Any]]) -> list[str]:
    """Undo `remove_output`: move files from `.trash/` back where they were, never over an existing file."""
    trash = (ws_dir / TRASH_DIR).resolve()
    restored = []
    for move in moves:
        source = resolve_rel(ws_dir, str(move.get("trash") or ""))
        target = resolve_rel(ws_dir, str(move.get("from") or ""))
        if source.parent != trash or not source.is_file() or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        restored.append(_ws_rel(ws_dir, target))
    return restored


# ---------- building the request ----------

def mesh_settings(cfg: Any, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make 3D settings: the config defaults, with valid values from the send sheet on top."""
    o = dict(overrides or {})

    def pick(key: str, default: Any) -> Any:
        value = o.get(key, default)
        return value if value in MESH_CHOICES[key] else default

    try:
        polycount = int(o.get("target_polycount") or cfg.mesh_target_polycount)
    except (TypeError, ValueError):
        polycount = cfg.mesh_target_polycount
    formats = [f for f in (o.get("target_formats") or cfg.mesh_target_formats) if f in MESH_CHOICES["target_formats"]]
    ai_model = pick("ai_model", cfg.default_3d_model if cfg.default_3d_model in MESH_CHOICES["ai_model"] else "meshy-7.1")
    model_type = "smart-topology" if ai_model in ("meshy-t2", "meshy-t2.5") else pick("model_type", cfg.mesh_model_type)
    texture = bool(o.get("should_texture", cfg.mesh_should_texture))
    return {
        "ai_model": ai_model,
        "model_type": model_type,
        "target_polycount": max(100, min(300000, polycount)),
        "should_remesh": bool(o.get("should_remesh", cfg.mesh_should_remesh)),
        "topology": pick("topology", cfg.mesh_topology),
        "should_texture": texture,
        "enable_pbr": texture and bool(o.get("enable_pbr", cfg.mesh_enable_pbr)),
        "texture_resolution": pick("texture_resolution", cfg.mesh_texture_resolution),
        "pose_mode": pick("pose_mode", cfg.mesh_pose_mode),
        "origin_at": pick("origin_at", cfg.mesh_origin_at),
        "target_formats": ["glb"] + [f for f in dict.fromkeys(formats) if f != "glb"],  # GLB is always made
    }


def default_source(ws_dir: Path, asset: dict[str, Any]) -> str:
    if asset["winner"]:
        return asset["winner"]
    for kind in ("candidates", "variants", "edits"):
        images = [o for o in asset["outputs"][kind] if o["type"] == "image"]
        if images:
            return images[0]["path"]
    return ""


def build_request(ws_dir: Path, rel: str, action: str, *, source: str = "", instruction: str = "", model: str = "") -> dict[str, Any]:
    """Everything that will be sent for an action, so the UI can show it before anything runs."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown action '{action}'.")
    asset = describe_asset(ws_dir, rel)
    cfg = load_config()
    if asset["kind"] in AUDIO_KINDS:
        return _audio_request(asset, action, model)
    if action in ("rig", "animate"):
        return _rig_request(ws_dir, asset, action, source)
    source = source or (default_source(ws_dir, asset) if action != "candidates" else "")
    if action != "candidates" and not source:
        raise ValueError("Generate candidates and pick a winner first.")
    if source:
        resolved = resolve_rel(ws_dir, source)
        if not resolved.is_file():
            raise FileNotFoundError(f"No image '{source}'.")

    if action == "make_3d":
        return {
            "action": action,
            "asset": asset["path"],
            "prompt": "",
            "parts": [],
            "references": [{"path": source, "source": "Winner image", "role": "source"}]
            + [{"path": view["path"], "source": "Turnaround view", "role": "view"} for view in asset["outputs"]["views"] if view["type"] == "image"][:3],
            "model": cfg.default_3d_service_model or f"{cfg.default_3d_provider}:{cfg.default_3d_model}",
            "settings": mesh_settings(cfg),
            "choices": {**MESH_CHOICES, "polycount_presets": list(POLYCOUNT_PRESETS)},
            "count": 1,
        }

    if action == "views":
        subject = VIEWS_RULE
        parts = [{"source": "Turnaround", "text": subject, "role": "rule"}]
        return {
            "action": action,
            "asset": asset["path"],
            "prompt": compose_prompt([subject], cfg),
            "parts": parts,
            "bible": [],
            "use_bible": False,
            "references": [{"path": source, "source": "Source image", "role": "source"}],
            "model": model or default_image_model(cfg),
            "settings": {"views": ", ".join(TURNAROUND_VIEWS[:2])},
            "count": 2,
        }
    subject = instruction.strip() if action in ("variant", "edit") else asset["prompt"].strip()
    if not subject and action == "candidates":
        subject = asset["body"].strip()[:1200]
    # The style bible is kept apart: it is switched on or off per run and added in front at send time.
    parts = [{"source": "Instruction" if action != "candidates" else "Asset prompt", "text": subject, "role": "subject"}]
    parts += [{"source": f"Style · {rule['source']}", "text": rule["text"], "role": "style"} for rule in asset["style_rules"]]
    references: list[dict[str, str]] = []
    if source:
        references.append({"path": source, "source": "Source image", "role": "source"})
    strength = pillar_strength(asset["meta"])
    if action == "candidates" and strength != "off":
        # Pillars only steer new images; a variant or edit works from its source image alone.
        pillars = asset["pillars"][-2:] if strength == "light" else asset["pillars"]  # the closest folder's come last
        references += [{**pillar, "role": "pillar"} for pillar in pillars]
    rule = {"candidates": {"light": PILLAR_RULE_LIGHT, "strong": PILLAR_RULE_STRONG}.get(strength, PILLAR_RULE), "variant": VARIANT_RULE, "edit": EDIT_RULE}[action]
    if action != "candidates" or any(ref["role"] == "pillar" for ref in references):
        parts.append({"source": "Reference rule", "text": rule, "role": "rule"})
    aspect = asset_aspect(asset, cfg) if action == "candidates" else cfg.image_aspect_ratio
    prompt = compose_prompt([part["text"] for part in parts], cfg, aspect)
    return {
        "action": action,
        "asset": asset["path"],
        "prompt": prompt,
        "parts": parts,
        "bible": asset["text_pillars"],
        "use_bible": True,
        "references": references[:MAX_REFERENCES],
        "model": model or default_image_model(cfg),
        "settings": {"aspect_ratio": aspect, "transparent_background": cutout_background(asset, cfg)},
        "pillar_strength": strength,
        "count": 2 if action == "candidates" else 1,
    }


def asset_aspect(asset: dict[str, Any], cfg: Any) -> str:
    """The asset's own framing, else the usual one for its kind, else the Settings default."""
    return str(asset["meta"].get("aspect") or KIND_ASPECT.get(asset["kind"]) or cfg.image_aspect_ratio)


def cutout_background(asset: dict[str, Any], cfg: Any) -> bool:
    """Whether new images are cut out: the Settings default, except for scenes and the key art."""
    if asset["kind"] in SCENE_KINDS or asset["meta"].get("cover"):
        return False
    return bool(cfg.image_remove_bg)


def pillar_strength(meta: dict[str, Any]) -> str:
    value = str(meta.get("pillars") or "normal").lower()
    return value if value in PILLAR_STRENGTHS else "normal"


def list_audio_models() -> list[dict[str, Any]]:
    from forge import services

    cfg = load_config()
    signed_in = set(services.connected())
    # Demo mode never spends: a signed-in service has no demo of its own, so it only counts outside demo mode.
    return [{**m, "available": m["service"] in signed_in and not cfg.demo_mode if "service" in m else bool(cfg.get_key(m["key"]))}
            for m in AUDIO_MODELS]


def _audio_request(asset: dict[str, Any], action: str, model: str) -> dict[str, Any]:
    kind = asset["kind"]
    if action != "candidates":
        raise ValueError("Sound assets are made with Generate; variants, edits and 3D are for images.")
    text = asset["prompt"].strip() or asset["body"].strip()[:600]
    choices = [m for m in list_audio_models() if kind in m["kinds"]]
    picked = (next((m for m in choices if m["id"] == model), None) or next((m for m in choices if m["available"]), None)
              or next((m for m in choices if "service" not in m), choices[0]))
    settings: dict[str, Any] = {"duration": 0, "loop": False, "prompt_influence": 0.3} if kind == "sfx" else {}
    if kind == "voice":
        settings = {"voice_id": str(asset["meta"].get("voice") or ""), "model_id": "eleven_multilingual_v2"}
    if kind == "music":
        settings = {"duration": 30, "loop": True}
    return {
        "action": action,
        "asset": asset["path"],
        "audio": True,
        "kind": kind,
        "prompt": text,
        "parts": [{"source": "Line to speak" if kind == "voice" else "Asset prompt", "text": text, "role": "subject"}],
        "references": [],
        "model": picked["id"],
        "models": choices,
        "settings": settings,
        "count": 2 if kind == "sfx" else 1,
    }


def save_audio(ws_dir: Path, rel: str, filename: str, data: bytes) -> str:
    """An audio file dropped on a sound asset becomes a take in <asset>/audio/ (and the winner if there is none)."""
    asset_path, meta, _ = read_asset(ws_dir, rel)
    suffix = Path(filename).suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ValueError("Drop an MP3, WAV, OGG or FLAC file.")
    folder = output_dir(asset_path) / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(filename).stem).strip("-") or "sound"
    target = folder / f"{stem}{suffix}"
    n = 2
    while target.exists():
        target, n = folder / f"{stem}-{n}{suffix}", n + 1
    target.write_bytes(data)
    rel_target = _ws_rel(ws_dir, target)
    if not meta.get("winner"):
        update_asset(ws_dir, rel, {"winner": rel_target})
    return rel_target


def _rig_request(ws_dir: Path, asset: dict[str, Any], action: str, source: str) -> dict[str, Any]:
    from forge import game_tools

    models = [o["path"] for o in asset["outputs"]["3d"] if o["path"].lower().endswith(".glb")]
    if action == "rig":
        model = source if source.lower().endswith(".glb") else (models[0] if models else "")
        if not model:
            raise ValueError("Make or drop a GLB model first, then rig it.")
        refs = [{"path": model, "source": "3D model", "role": "source"}]
        thumb = next((o.get("thumb") for o in asset["outputs"]["3d"] if o["path"] == model), "")
        return {
            "action": action, "asset": asset["path"], "prompt": "", "parts": [], "references": refs, "thumb": thumb or "",
            "model": "meshy:rigging", "settings": {"height_meters": 1.7}, "count": 1,
            "credits": game_tools.RIG_CREDITS, "cost_usd": game_tools.RIG_CREDITS / 100,
        }
    if not asset["meta"].get("rig_task"):
        raise ValueError("Rig the 3D model first, then add animations.")
    return {
        "action": action, "asset": asset["path"], "prompt": "", "parts": [], "references": [],
        "model": "meshy:animation", "settings": {"fps": 30}, "count": 1, "action_ids": [], "names": [],
        "credits_each": game_tools.ANIMATION_CREDITS, "cost_usd": 0.0, "rig_model": asset["meta"].get("rig_model") or "",
    }


def prompt_with_bible(ws_dir: Path, request: dict[str, Any]) -> str:
    """The prompt as sent: subject first, then the style bible (read fresh from disk), then the reference rule.

    The bible is left out when it is switched off.
    """
    prompt = str(request.get("prompt") or "").strip()
    if request.get("use_bible", True) is False:
        return prompt
    asset_path = resolve_rel(ws_dir, str(request.get("asset") or ""))
    if "Style bible:" in prompt:
        return prompt  # the send screen already put it in the final prompt
    texts = [t["text"].strip() for t in collect_text_pillars(ws_dir, asset_path)]
    if not texts or not prompt:
        return prompt
    bible = "Style bible:\n" + "\n\n".join(texts)
    for rule in (PILLAR_RULE_STRONG, PILLAR_RULE, PILLAR_RULE_LIGHT, VARIANT_RULE, EDIT_RULE):
        at = prompt.find(rule)
        if at >= 0:
            return "\n\n".join(part for part in (prompt[:at].strip(), bible, prompt[at:].strip()) if part)
    return f"{prompt}\n\n{bible}"


def compose_prompt(texts: list[str], cfg: Any | None = None, aspect: str = "") -> str:
    cfg = cfg or load_config()
    body = "\n\n".join(text.strip() for text in texts if text and text.strip())
    return finalize_generation_prompt(body, pose_mode=cfg.image_pose_mode, aspect_ratio=aspect or cfg.image_aspect_ratio)


# ---------- running ----------

def _link_or_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def _record(actions: Any, project: str, ws_dir: Path, asset_rel: str, action: str, stored: Any, request: dict[str, Any], name: str) -> str:
    """Copy one generated file next to the asset and into the vault; return its workspace path."""
    store = actions.get_store(project)
    asset_path = resolve_rel(ws_dir, asset_rel)
    src = store.resolve_safe_path(stored.file_path)
    kind_folder = "audio" if request.get("audio") else ACTION_FOLDER[action]
    folder = output_dir(asset_path) / kind_folder
    target = folder / f"{name}{src.suffix.lower()}"
    _link_or_copy(src, target)
    if stored.type == AssetType.MESH and stored.thumbnail_path:
        thumb = store.resolve_safe_path(stored.thumbnail_path)
        if thumb.is_file() and thumb != src:
            _link_or_copy(thumb, folder / f"{name}_thumb.png")
    extras: list[Path] = []
    if stored.type == AssetType.MESH:
        for fmt, rel in (stored.extra_files or {}).items():
            extra = store.resolve_safe_path(rel)
            if extra.is_file() and extra.suffix.lower() == f".{fmt}":
                _link_or_copy(extra, folder / f"{name}{extra.suffix.lower()}")
                extras.append(extra)

    vault_dir = store.project_dir / "vault" / Path(asset_rel).with_suffix("") / kind_folder
    _link_or_copy(src, vault_dir / target.name)
    for extra in extras:
        _link_or_copy(extra, vault_dir / f"{name}{extra.suffix.lower()}")
    sidecar = {
        "asset": asset_rel,
        "action": action,
        "file": _ws_rel(ws_dir, target),
        "store_asset_id": stored.id,
        "prompt": request.get("prompt", ""),
        "asset_prompt": request.get("asset_prompt", ""),
        "references": [ref["path"] for ref in request.get("references", [])],
        "model": stored.model,
        "provider": stored.provider,
        "cost_usd": stored.cost_usd,
        "settings": request.get("settings") or {},
        "formats": sorted({src.suffix.lower().lstrip(".")} | {e.suffix.lower().lstrip(".") for e in extras}),
        "created_at": time.time(),
    }
    (vault_dir / f"{target.stem}.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return sidecar["file"]


def _store_id_for(store: Any, ws_dir: Path, asset_rel: str, file_rel: str) -> str | None:
    """Find the vault sidecar of a generated file so new outputs keep their lineage."""
    file_path = Path(file_rel)
    kind = file_path.parent.name
    sidecar = store.project_dir / "vault" / Path(asset_rel).with_suffix("") / kind / f"{file_path.stem}.json"
    try:
        return json.loads(sidecar.read_text(encoding="utf-8")).get("store_asset_id")
    except (OSError, ValueError):
        return None


async def run_action(actions: Any, project: str, request: dict[str, Any]) -> list[str]:
    """Run a request (as built by ``build_request`` and possibly edited by the user)."""
    action = request.get("action")
    if action not in ACTIONS:
        raise ValueError(f"Unknown action '{action}'.")
    ws_dir = actions.get_workspace(project).ws_dir
    asset_rel = str(request.get("asset") or "")
    asset_path, meta, _ = read_asset(ws_dir, asset_rel)
    store = actions.get_store(project)
    executor = actions.get_executor(project)
    bible = store.get_bible()
    cfg = load_config()
    refs = [resolve_rel(ws_dir, ref["path"]) for ref in request.get("references") or [] if ref.get("path")]
    refs = [ref for ref in refs if ref.is_file()][:MAX_REFERENCES]
    # Sound effects come in pools (five footsteps), so audio may ask for a few more takes.
    count = max(1, min(int(request.get("count") or 1), 6 if request.get("audio") else 4))
    # Each output remembers the asset prompt it came from, so an older prompt can be brought back.
    request = {**request, "asset_prompt": str(meta.get("prompt") or "")}
    title = str(meta.get("title") or asset_path.stem.replace("-", " ").replace("_", " ").title())
    sources = [ref["path"] for ref in request.get("references") or [] if ref.get("role") == "source"]
    parent_ids = [pid for src in sources[:1] if (pid := _store_id_for(store, ws_dir, asset_rel, src))]
    tracker = [0.0]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    created: list[str] = []

    if action in ("rig", "animate"):
        from forge import game_tools

        if action == "rig":
            model = next((ref["path"] for ref in request.get("references") or [] if ref.get("role") == "source"), "")
            return await game_tools.rig_model(ws_dir, asset_rel, model, float((request.get("settings") or {}).get("height_meters") or 1.7))
        return await game_tools.animate(ws_dir, asset_rel, list(request.get("action_ids") or []), list(request.get("names") or []), int((request.get("settings") or {}).get("fps") or 30))

    if request.get("audio"):
        model_id = str(request.get("model") or "")
        if model_id.startswith("svc:"):
            provider_id, mode = "service", model_id.rsplit("/", 1)[-1]
        else:
            provider_id, _, mode = model_id.partition(":")
        capability = AUDIO_CAPABILITY.get(mode)
        if not provider_id or capability is None:
            raise ValueError(f"Unknown sound model '{model_id}'.")
        picked = next((m for m in list_audio_models() if m["id"] == model_id), None)
        if picked and not picked["available"] and not cfg.demo_mode:
            # Without a key the provider falls back to demo mode and saves a silent file: say so instead.
            how = f"sign in to {picked['service'].title()}" if "service" in picked else f"add a {picked['key'].title()} key"
            raise ValueError(f"{picked['label']} isn't set up: {how} in Settings → Generators, or pick another generator.")
        text = str(request.get("prompt") or "").strip()
        if not text:
            raise ValueError("Describe the sound first (for a voice: the line to speak).")
        settings = {k: v for k, v in (request.get("settings") or {}).items() if v not in ("", None)}
        if not settings.get("duration"):
            settings.pop("duration", None)  # let the provider pick a fitting length
        if provider_id == "service":
            settings["model"] = model_id
        elif mode == "tts" and not settings.get("voice_id"):
            from forge.providers.elevenlabs_audio import list_voices

            settings["voice_id"] = next((v["id"] for v in await list_voices()), "")  # any voice beats an error
        for index in range(count):
            stored = await executor._generate_single_asset(
                provider_id=provider_id,
                capability=capability,
                name=f"{title} (sound {index + 1})",
                asset_type=AssetType.AUDIO,
                verb="generate_audio",
                recipe_id="audio",
                prompt_raw=text,
                prompt_full=text,
                parent_ids=[],
                reference_files=[],
                params=dict(settings),
                tags=["audio", str(request.get("kind") or ""), f"asset:{asset_rel}"],
                force_uncached=True,
                bible=bible,
                run_spent_tracker=tracker,
            )
            created.append(_record(actions, project, ws_dir, asset_rel, action, stored, request, f"{asset_path.stem}-{stamp}-{index + 1}"))
        if created and not meta.get("winner"):
            update_asset(ws_dir, asset_rel, {"winner": created[0]})
        return created

    if action == "make_3d":
        if not refs:
            raise ValueError("Make 3D needs a source image.")
        service_model = str(request.get("model") or "")
        stored = await executor._generate_single_asset(
            # A signed-in service's 3D model when one was picked, else the 3D provider from Settings.
            provider_id="service" if service_model.startswith("svc:") else cfg.default_3d_provider or "meshy",
            # Several pictures (winner + turnaround views) give Meshy the back and sides too.
            capability=ProviderCapability.MULTI_IMAGE_TO_3D if len(refs) > 1 else ProviderCapability.IMAGE_TO_3D,
            name=f"{title} (3D)",
            asset_type=AssetType.MESH,
            verb="make_3d",
            recipe_id="make_3d",
            prompt_raw=title,
            prompt_full=title,
            parent_ids=parent_ids,
            reference_files=refs[:4],
            params={"model": service_model} if service_model.startswith("svc:") else mesh_settings(cfg, request.get("settings")),
            tags=["3d", f"asset:{asset_rel}"],
            force_uncached=True,
            bible=bible,
            run_spent_tracker=tracker,
        )
        created.append(_record(actions, project, ws_dir, asset_rel, action, stored, request, f"{asset_path.stem}-3d-{stamp}"))
        return created

    if not str(request.get("prompt") or "").strip():
        raise ValueError("The prompt is empty.")
    prompt = prompt_with_bible(ws_dir, request)
    request = {**request, "prompt": prompt}
    # The sheet's own aspect ratio and background win over the Settings defaults.
    sheet = request.get("settings") or {}
    params = {"use_reference_image": bool(refs), "aspect_ratio": str(sheet.get("aspect_ratio") or cfg.image_aspect_ratio),
              "transparent": bool(sheet.get("transparent_background", cfg.image_remove_bg))}
    if request.get("model"):
        params["model"] = str(request["model"])
    provider_id = resolve_image_provider("gemini", params)
    template = prompt
    for index in range(count):
        if action == "views":
            prompt = template.replace("{view}", TURNAROUND_VIEWS[index % len(TURNAROUND_VIEWS)])
            request = {**request, "prompt": prompt}
        stored = await executor._generate_single_asset(
            provider_id=provider_id,
            capability=ProviderCapability.IMAGE_EDIT if refs else ProviderCapability.TEXT_TO_IMAGE,
            name=f"{title} ({action} {index + 1})",
            asset_type=AssetType.IMAGE,
            verb="generate" if action == "candidates" else ("variant" if action in ("variant", "views") else "edit_image"),
            recipe_id=action,
            prompt_raw=prompt,
            prompt_full=prompt,
            parent_ids=parent_ids,
            reference_files=refs,
            params=dict(params),
            tags=[action, f"asset:{asset_rel}"],
            force_uncached=True,
            bible=bible,
            run_spent_tracker=tracker,
        )
        created.append(_record(actions, project, ws_dir, asset_rel, action, stored, request, f"{asset_path.stem}-{stamp}-{index + 1}"))
    remember: dict[str, Any] = {}
    if action == "candidates" and params["aspect_ratio"] != str(meta.get("aspect") or ""):
        remember["aspect"] = params["aspect_ratio"]  # the next run opens with the framing picked this time
    if created and meta.get("cover") and not meta.get("winner"):
        remember["winner"] = created[0]  # key art goes straight onto the project card
    if remember:
        update_asset(ws_dir, asset_rel, remember)
    return created

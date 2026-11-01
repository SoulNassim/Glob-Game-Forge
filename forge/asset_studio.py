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
from forge.overview import IMAGE_SUFFIXES, MODEL_SUFFIXES, FOLDER_PAGES, resolve_rel
from forge.prompt_sanitize import finalize_generation_prompt
from forge.providers.base import ProviderCapability
from forge.providers.elevenlabs_image import resolve_image_provider

PILLAR_DIR = "pillars"
REF_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_REFERENCES = 8
OUTPUT_KINDS = ("candidates", "variants", "edits", "views", "3d", "rig", "animations", "parts", "icons", "sprites")
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
ASSET_KINDS = ("character", "creature", "item", "weapon", "prop", "environment", "vehicle", "ui", "vfx")

PILLAR_RULE = (
    "Use the attached reference images only for art style: rendering, brushwork, material handling, lighting and "
    "camera angle. They show other characters, so do not copy their gender, faces, bodies, proportions, pose, "
    "clothing or props. Create the new subject exactly as described at the top; the description decides who it "
    "is, and the style bible never overrides it."
)
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


def list_image_models() -> list[dict[str, Any]]:
    """Image models the studio can send to, with price per image and whether a key is set."""
    from forge.providers.elevenlabs_image import IMAGE_MODELS, prefixed_model_id
    from forge.providers.gemini_image import MODEL_PRICING_USD

    cfg = load_config()
    has = {name: bool(cfg.get_key(name)) for name in ("gemini", "meshy", "elevenlabs")}
    models = []
    for model_id, price in MODEL_PRICING_USD.items():
        if model_id == "gemini-2.5-flash-image":
            continue
        meshy = model_id.startswith("meshy:")
        name = model_id.split(":", 1)[-1].removeprefix("gemini-").replace("-", " ").title().replace("Gpt", "GPT")
        name = re.sub(r"(\d) (\d)", r"\1.\2", name)
        models.append({"id": model_id, "label": f"{'Meshy' if meshy else 'Gemini'} · {name}", "price": price, "available": has["meshy" if meshy else "gemini"]})
    for model_id, spec in IMAGE_MODELS.items():
        models.append({"id": prefixed_model_id(model_id), "label": spec.label, "price": spec.usd, "available": has["elevenlabs"]})
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
            if suffix in IMAGE_SUFFIXES:
                outputs[kind].append({"path": _ws_rel(ws_dir, file), "type": "image"})
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
    return describe_asset(ws_dir, rel)


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
            "model": f"{cfg.default_3d_provider}:{cfg.default_3d_model}",
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
            "model": model or cfg.default_image_model,
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
    if action == "candidates":
        # Pillars only steer new images; a variant or edit works from its source image alone.
        references += [{**pillar, "role": "pillar"} for pillar in asset["pillars"]]
    rule = {"candidates": PILLAR_RULE, "variant": VARIANT_RULE, "edit": EDIT_RULE}[action]
    if action != "candidates" or any(ref["role"] == "pillar" for ref in references):
        parts.append({"source": "Reference rule", "text": rule, "role": "rule"})
    prompt = compose_prompt([part["text"] for part in parts], cfg)
    return {
        "action": action,
        "asset": asset["path"],
        "prompt": prompt,
        "parts": parts,
        "bible": asset["text_pillars"],
        "use_bible": True,
        "references": references[:MAX_REFERENCES],
        "model": model or cfg.default_image_model,
        "settings": {"aspect_ratio": cfg.image_aspect_ratio, "transparent_background": cfg.image_remove_bg},
        "count": 2 if action == "candidates" else 1,
    }


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
    for rule in (PILLAR_RULE, VARIANT_RULE, EDIT_RULE):
        at = prompt.find(rule)
        if at >= 0:
            return "\n\n".join(part for part in (prompt[:at].strip(), bible, prompt[at:].strip()) if part)
    return f"{prompt}\n\n{bible}"


def compose_prompt(texts: list[str], cfg: Any | None = None) -> str:
    cfg = cfg or load_config()
    body = "\n\n".join(text.strip() for text in texts if text and text.strip())
    return finalize_generation_prompt(body, pose_mode=cfg.image_pose_mode, aspect_ratio=cfg.image_aspect_ratio)


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
    folder = output_dir(asset_path) / ACTION_FOLDER[action]
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

    vault_dir = store.project_dir / "vault" / Path(asset_rel).with_suffix("") / ACTION_FOLDER[action]
    _link_or_copy(src, vault_dir / target.name)
    for extra in extras:
        _link_or_copy(extra, vault_dir / f"{name}{extra.suffix.lower()}")
    sidecar = {
        "asset": asset_rel,
        "action": action,
        "file": _ws_rel(ws_dir, target),
        "store_asset_id": stored.id,
        "prompt": request.get("prompt", ""),
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
    count = max(1, min(int(request.get("count") or 1), 4))
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

    if action == "make_3d":
        if not refs:
            raise ValueError("Make 3D needs a source image.")
        stored = await executor._generate_single_asset(
            provider_id=cfg.default_3d_provider or "meshy",
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
            params=mesh_settings(cfg, request.get("settings")),
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
    params = {"use_reference_image": bool(refs), "aspect_ratio": cfg.image_aspect_ratio, "transparent": cfg.image_remove_bg}
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
    return created

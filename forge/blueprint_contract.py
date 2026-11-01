"""YAML-backed contracts for universal Forge workspace blueprints."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml


_FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(?P<header>.*?)\r?\n---\s*(?:\r?\n|$)", re.DOTALL)
_ARCHETYPES = {"character", "prop", "weapon_item", "vehicle", "environment", "ui_element", "custom"}
_STATUSES = {"concept", "2d_locked", "3d_ready", "engine_integrated"}


def split_frontmatter(content: str) -> tuple[dict[str, Any], str, bool]:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content, False
    try:
        loaded = yaml.safe_load(match.group("header")) or {}
    except yaml.YAMLError:
        return {}, content, False
    if not isinstance(loaded, dict):
        return {}, content, False
    return loaded, content[match.end():].lstrip("\r\n"), True


def serialize_frontmatter(metadata: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True, default_flow_style=False).rstrip()
    return f"---\n{header}\n---\n\n{body.strip()}\n"


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def game_metadata_from_bible(bible: Any) -> dict[str, Any]:
    dimension = getattr(bible, "project_dimension", "3d") or "3d"
    engine = getattr(bible, "target_engine", "custom")
    if engine == "generic":
        engine = "custom"
    scale_benchmarks = getattr(bible, "scale_benchmarks", None) or [
        {
            "name": "Standard Human / Player",
            "height_m": round(float(bible.character_height_units) * float(bible.unit_scale_meters), 3),
            "height_px": 128,
        }
    ]
    tokens = getattr(bible, "global_style_tokens", None) or {}
    return {
        "display_name": bible.display_name or bible.project_name,
        "project_dimension": dimension,
        "camera_perspective": bible.camera_perspective,
        "target_engine": engine,
        "background_rule": bible.background_rule,
        "polycount_budget": bible.polycount_budget,
        "texture_resolution": bible.texture_resolution,
        "unit_scale_meters": bible.unit_scale_meters,
        "scale_benchmarks": scale_benchmarks,
        "global_style_tokens": {
            "art_style": tokens.get("art_style", bible.art_style),
            "lighting_rules": tokens.get("lighting_rules", ""),
            "master_palette": tokens.get("master_palette", list(bible.palette)),
        },
    }


def game_frontmatter_updates(metadata: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    display_name = metadata.get("display_name", metadata.get("project_name"))
    if isinstance(display_name, str) and display_name.strip():
        updates["display_name"] = display_name.strip()
    texture_resolution = metadata.get("texture_resolution")
    if isinstance(texture_resolution, str) and texture_resolution.strip():
        updates["texture_resolution"] = texture_resolution.strip()
    background_rule = metadata.get("background_rule")
    if background_rule in {"transparent", "solid_neutral", "none"}:
        updates["background_rule"] = background_rule
    dimension = metadata.get("project_dimension")
    if dimension in {"2d", "3d", "hybrid"}:
        updates["project_dimension"] = dimension
    engine = metadata.get("target_engine")
    if engine in {"godot", "unity", "unreal", "custom", "generic"}:
        updates["target_engine"] = "generic" if engine == "custom" else engine
    if isinstance(metadata.get("camera_perspective"), str):
        updates["camera_perspective"] = metadata["camera_perspective"]
    benchmarks = metadata.get("scale_benchmarks")
    if isinstance(benchmarks, list):
        updates["scale_benchmarks"] = benchmarks
        player = next((entry for entry in benchmarks if isinstance(entry, dict) and entry.get("height_m")), None)
        if player:
            updates["character_height_units"] = float(player["height_m"])
            updates["unit_scale_meters"] = 1.0
    tokens = metadata.get("global_style_tokens")
    if isinstance(tokens, dict):
        updates["global_style_tokens"] = tokens
        if isinstance(tokens.get("art_style"), str) and tokens["art_style"]:
            updates["art_style"] = tokens["art_style"]
        palette = tokens.get("master_palette")
        if isinstance(palette, list) and palette:
            updates["palette"] = palette
    return updates


def inherited_blueprint_defaults(workspace_dir: Path, asset_relpath: str) -> dict[str, Any]:
    workspace_dir = workspace_dir.resolve()
    relative = Path(asset_relpath.replace("\\", "/"))
    if relative.parts and relative.parts[0] == "workspace":
        relative = Path(*relative.parts[1:])
    asset_path = (workspace_dir / relative).resolve()
    if not asset_path.is_relative_to(workspace_dir):
        raise ValueError("Invalid workspace blueprint path.")

    game_path = workspace_dir / "GAME.md"
    game_metadata = split_frontmatter(game_path.read_text(encoding="utf-8"))[0] if game_path.exists() else {}
    folder_paths = [workspace_dir]
    current = asset_path.parent
    while current != workspace_dir and current.is_relative_to(workspace_dir):
        folder_paths.append(current)
        current = current.parent
    inherited: dict[str, Any] = {"project_metadata": game_metadata}
    for folder in reversed(folder_paths):
        template_path = folder / "_folder.md"
        if template_path.exists():
            template, _, _ = split_frontmatter(template_path.read_text(encoding="utf-8"))
            inherited = deep_merge(inherited, template.get("archetype_template", template))
    return inherited


def normalize_asset_frontmatter(
    metadata: dict[str, Any], inherited: dict[str, Any] | None = None
) -> dict[str, Any]:
    inherited = inherited or {}
    template = inherited.get("archetype_template", inherited)
    if not isinstance(template, dict):
        template = {}
    normalized = deep_merge(template, metadata)
    archetype = normalized.get("archetype", "custom")
    normalized["archetype"] = archetype if archetype in _ARCHETYPES else "custom"
    status = normalized.get("status", "concept")
    normalized["status"] = status if status in _STATUSES else "concept"
    normalized.setdefault("id", "")
    normalized.setdefault("name", "Untitled Asset")
    normalized.setdefault("engine_bindings", {})
    normalized.setdefault("technical_specs", {})
    normalized.setdefault("palette_swatches", [])
    normalized.setdefault("design_pillars", [])
    normalized.setdefault("variants", [])
    normalized.setdefault("modular_parts", [])
    normalized.setdefault("agent_requests", [])
    normalized["inherited"] = inherited
    return normalized


def legacy_asset_metadata(body: str, asset_relpath: str) -> dict[str, Any]:
    """Recover the contract's identity fields from pre-frontmatter blueprints."""
    heading = re.search(r"(?m)^#\s+(.+?)(?:\s+—\s+Design Blueprint)?\s*$", body)
    status_match = re.search(r"(?mi)^\s*-\s+\*\*Status\*\*:\s*`?([^\s`]+)`?", body)
    target_match = re.search(r"(?mi)^\s*-\s+\*\*(?:In-game destination|Target path)\*\*:\s*`([^`]+)`", body)
    legacy_status = (status_match.group(1).lower() if status_match else "concept")
    status = {
        "planned": "concept",
        "approved": "engine_integrated",
        "winner_picked": "2d_locked",
        "candidates_ready": "concept",
    }.get(legacy_status, legacy_status if legacy_status in _STATUSES else "concept")
    metadata: dict[str, Any] = {
        "id": Path(asset_relpath).stem,
        "status": status,
    }
    if heading:
        metadata["name"] = heading.group(1).strip()
    if target_match:
        target = target_match.group(1).strip()
        binding_key = "scene_path"
        if target.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            binding_key = "sprite_path"
        elif target.lower().endswith((".glb", ".gltf", ".obj")):
            binding_key = "mesh_path"
        metadata["engine_bindings"] = {binding_key: target}
    return metadata


def pipeline_item_frontmatter(item: Any, bible: Any) -> dict[str, Any]:
    category = str(item.category).lower()
    if category in {"character", "characters"}:
        archetype = "character"
    elif category in {"environment", "environments"}:
        archetype = "environment"
    elif category in {"ui", "ui_element"}:
        archetype = "ui_element"
    elif category in {"vehicle", "vehicles"}:
        archetype = "vehicle"
    elif category in {"item", "items", "prop", "weapon_item"}:
        archetype = "weapon_item" if any(term in f"{item.title} {item.category}".lower() for term in ("weapon", "sword", "blade", "gun")) else "prop"
    else:
        archetype = "custom"

    if item.stage == "approved":
        status = "engine_integrated"
    elif item.final_asset_ids:
        status = "3d_ready"
    elif item.winner_asset_id:
        status = "2d_locked"
    else:
        status = "concept"
    target = item.target_path or ""
    binding_key = "scene_path"
    if target.lower().endswith((".png", ".jpg", ".webp")):
        binding_key = "sprite_path"
    elif target.lower().endswith((".glb", ".gltf", ".obj")):
        binding_key = "mesh_path"
    palette = list(getattr(bible, "palette", []) or [])
    pillars = [
        {"pillar_name": "Gameplay Function", "value": item.goal},
        {"pillar_name": "Gameplay Identity", "value": item.gameplay_uniqueness},
        {"pillar_name": "World Context", "value": item.personality},
        {"pillar_name": "Silhouette Rules", "value": item.visual_signature},
        {"pillar_name": "Material Breakdown", "value": ", ".join(item.material_tags)},
    ]
    return {
        "id": item.slug,
        "name": item.title,
        "archetype": archetype,
        "status": status,
        "engine_bindings": {"target_engine": getattr(bible, "target_engine", "custom"), "primary_path": target, binding_key: target},
        "technical_specs": {
            "poly_budget_tris": (item.mesh_params or {}).get("target_polycount", getattr(bible, "polycount_budget", None)),
            "actual_tris": None,
            "dimensions": {},
            "shared_base_asset": "",
        },
        "palette_swatches": [
            {"name": f"Palette {index + 1}", "hex": color, "material_surface": "Unassigned"}
            for index, color in enumerate(palette)
        ],
        "design_pillars": pillars,
        "variants": [{
            "id": "base",
            "name": "Base Version",
            "purpose": "Primary gameplay asset",
            "prompt_delta": "",
            "2d_candidates": list(item.candidate_asset_ids),
            "winner_2d": item.winner_asset_id,
            "orthographic_sheet": "",
            "3d_models": list(item.final_asset_ids),
            "active_output": item.final_asset_ids[-1] if item.final_asset_ids else item.winner_asset_id,
        }],
        "modular_parts": [],
        "agent_requests": [],
    }


def merge_generated_asset_frontmatter(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(existing)
    for key in ("id", "name", "archetype", "status"):
        merged[key] = generated[key]
    for key in ("engine_bindings", "technical_specs"):
        merged[key] = deep_merge(merged.get(key, {}), generated[key])
    for key in ("palette_swatches", "design_pillars", "modular_parts", "agent_requests"):
        if key not in merged:
            merged[key] = copy.deepcopy(generated[key])
    current_variants = merged.get("variants")
    if not isinstance(current_variants, list) or not current_variants:
        merged["variants"] = copy.deepcopy(generated["variants"])
    else:
        base = generated["variants"][0]
        index = next((i for i, variant in enumerate(current_variants) if isinstance(variant, dict) and variant.get("id") == "base"), None)
        if index is None:
            merged["variants"] = [base, *current_variants]
        else:
            merged["variants"][index] = deep_merge(current_variants[index], base)
    return merged

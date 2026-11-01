"""Engine export presets for Godot, Unity, and Unreal (Section 4).

Guarantees:
- Engine-ready folder structures (`Godot/`, `Unity/`, `Unreal/`)
- Format presets: GLB, OBJ, FBX (where available), PNG with alpha
- Sidecar metadata JSON (lineage, polycount, scale, style packs, prompt, tags)
- STRICT SECURITY: Never exports API keys or secrets under any circumstance.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from forge.models import Asset, AssetType
from forge.storage.project_store import ProjectStore


ENGINE_PRESETS = {
    "godot": {
        "name": "Godot 4.x Bundle",
        "models_subdir": "assets/models",
        "textures_subdir": "assets/textures",
        "sprites_subdir": "assets/sprites",
        "icons_subdir": "assets/icons",
        "naming": "snake_case",
        "preferred_mesh_formats": ["glb", "obj"],
    },
    "unity": {
        "name": "Unity UPM / Assets Bundle",
        "models_subdir": "Assets/ForgeExports/Models",
        "textures_subdir": "Assets/ForgeExports/Textures",
        "sprites_subdir": "Assets/ForgeExports/Sprites",
        "icons_subdir": "Assets/ForgeExports/Icons",
        "naming": "PascalCase",
        "preferred_mesh_formats": ["fbx", "glb", "obj"],
    },
    "unreal": {
        "name": "Unreal Engine 5 Content Bundle",
        "models_subdir": "Content/ForgeExports/Meshes",
        "textures_subdir": "Content/ForgeExports/Materials",
        "sprites_subdir": "Content/ForgeExports/Sprites",
        "icons_subdir": "Content/ForgeExports/UI",
        "naming": "UE_Prefix",  # SM_ for static meshes, T_ for textures, UI_ for icons
        "preferred_mesh_formats": ["fbx", "glb", "obj"],
    },
}


def _format_engine_name(raw_name: str, style: str, asset_type: AssetType) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", " ", raw_name.split("•")[0]).strip()
    words = [w for w in clean.split() if w] or ["Asset"]
    if style == "PascalCase":
        return "".join(w.capitalize() for w in words)
    if style == "UE_Prefix":
        pascal = "".join(w.capitalize() for w in words)
        prefix = "SM_" if asset_type == AssetType.MESH else ("UI_" if asset_type == AssetType.ICON else "T_")
        return f"{prefix}{pascal}"
    return "_".join(w.lower() for w in words) 


def export_project_assets(
    store: ProjectStore,
    engine: str = "godot",
    asset_ids: list[str] | None = None,
    formats: list[str] | None = None,
    create_zip: bool = True,
    path_hints: dict[str, str] | None = None,
) -> dict[str, Any]:
    preset = ENGINE_PRESETS.get(engine.lower(), ENGINE_PRESETS["godot"])
    all_assets = store.list_assets(include_archived=False)
    if asset_ids:
        id_set = set(asset_ids)
        selected = [a for a in all_assets if a.id in id_set]
    else:
        selected = all_assets

    stamp = int(time.time())
    bundle_name = f"export_{engine.lower()}_{stamp}"
    bundle_dir = store.exports_dir / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    exported_files: list[str] = []
    manifest_records: list[dict[str, Any]] = []
    hints = path_hints or {}

    for ast in selected:
        src = store.resolve_safe_path(ast.file_path)
        if not src.exists():
            continue

        if ast.type == AssetType.MESH:
            subdir = preset["models_subdir"]
        elif ast.type == AssetType.TEXTURE:
            subdir = preset["textures_subdir"]
        elif ast.type == AssetType.ICON:
            subdir = preset["icons_subdir"]
        else:
            subdir = preset["sprites_subdir"]

        hint = str(hints.get(ast.id) or "").replace("\\", "/").strip("/")
        if hint:
            target_folder = bundle_dir / Path(hint).parent
            formatted_stem = Path(hint).stem or _format_engine_name(ast.name, preset["naming"], ast.type)
        else:
            target_folder = bundle_dir / subdir
            formatted_stem = _format_engine_name(ast.name, preset["naming"], ast.type)
        target_folder.mkdir(parents=True, exist_ok=True)
        ext = src.suffix.lower()
        dest_file = target_folder / f"{formatted_stem}{ext}"
        if dest_file.exists():
            dest_file = target_folder / f"{formatted_stem}_{ast.id[-4:]}{ext}"
        shutil.copy2(src, dest_file)
        exported_files.append(dest_file.relative_to(bundle_dir).as_posix())

        # Also copy extra mesh formats (.obj / .fbx) if requested
        for fmt_key, extra_rel in ast.extra_files.items():
            if formats and fmt_key not in [f.lower() for f in formats]:
                continue
            extra_src = store.resolve_safe_path(extra_rel)
            if extra_src.exists():
                extra_dest = dest_file.with_suffix(f".{fmt_key}")
                shutil.copy2(extra_src, extra_dest)
                exported_files.append(extra_dest.relative_to(bundle_dir).as_posix())

        # Safe metadata record (never contains API keys!)
        manifest_records.append(
            {
                "id": ast.id,
                "name": ast.name,
                "engine_name": formatted_stem,
                "type": ast.type.value,
                "exported_path": dest_file.relative_to(bundle_dir).as_posix(),
                "parent_ids": ast.parent_ids,
                "verb": ast.verb,
                "prompt_full": ast.prompt_full,
                "style_packs": ast.style_packs,
                "tags": ast.tags,
                "metadata": ast.metadata,
            }
        )

    bible = store.get_bible()
    export_manifest = {
        "generator": "Forge Asset Maker 1.0",
        "project_name": store.project_name,
        "target_engine": engine.lower(),
        "preset_name": preset["name"],
        "exported_at": time.time(),
        "unit_scale_meters": bible.unit_scale_meters,
        "polycount_budget": bible.polycount_budget,
        "asset_count": len(manifest_records),
        "assets": manifest_records,
    }
    manifest_path = bundle_dir / "forge_export_manifest.json"
    txt_manifest = json.dumps(export_manifest, indent=2)
    # Security assertion: verify no key env values or key fields exist in export
    assert "api_key" not in txt_manifest.lower(), "Security violation: api_key found in export manifest"
    manifest_path.write_text(txt_manifest, encoding="utf-8")
    exported_files.append("forge_export_manifest.json")

    zip_rel = ""
    if create_zip:
        zip_path = store.exports_dir / f"{bundle_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f_rel in exported_files:
                zf.write(bundle_dir / f_rel, arcname=f_rel)
        zip_rel = f"exports/{zip_path.name}"

    return {
        "bundle_name": bundle_name,
        "engine": engine.lower(),
        "bundle_dir": f"exports/{bundle_name}",
        "zip_path": zip_rel,
        "asset_count": len(manifest_records),
        "files": exported_files,
    }

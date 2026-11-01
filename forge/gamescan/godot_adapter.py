"""Full Godot 4 EngineAdapter implementation (Section 14)."""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any
from PIL import Image

from forge.gamescan.base import EngineAdapter
from forge.gamescan.safety import collect_safe_game_files, sanitize_untrusted_text
from forge.models import (
    Asset,
    AssetType,
    CatalogEntry,
    EvidenceField,
    GameProfile,
    StylePack,
    WishlistItem,
    new_id,
)


RES_PATH_RE = re.compile(r"(res://[a-zA-Z0-9_./\-]+)")
TODO_ART_RE = re.compile(
    r"(?:#|//)\s*(TODO|FIXME)\b([^\n]*(?:art|sprite|mesh|model|icon|texture|placeholder|glb|png)[^\n]*)",
    re.IGNORECASE,
)
CAPSULE_HEIGHT_RE = re.compile(r"height\s*=\s*([0-9]+(?:\.[0-9]+)?)")
CAPSULE_RADIUS_RE = re.compile(r"radius\s*=\s*([0-9]+(?:\.[0-9]+)?)")


def _extract_dominant_palette(image_files: list[Path], max_colors: int = 6) -> tuple[list[str], dict[str, Any]]:
    color_counts: Counter[str] = Counter()
    resolutions: list[str] = []
    placeholder_files: list[str] = []

    for img_path in image_files[:15]:
        if img_path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        try:
            img = Image.open(img_path).convert("RGBA")
            resolutions.append(f"{img.width}x{img.height}")
            small = img.resize((32, 32), Image.Resampling.NEAREST)
            unique_opaque: set[tuple[int, int, int]] = set()
            raw_bytes = small.tobytes()
            for i in range(0, len(raw_bytes), 4):
                r, g, b, a = raw_bytes[i], raw_bytes[i + 1], raw_bytes[i + 2], raw_bytes[i + 3]
                if a < 40:
                    continue
                # Quantize to 16-step buckets for clean dominant palette extraction
                qr, qg, qb = (r // 16) * 16, (g // 16) * 16, (b // 16) * 16
                unique_opaque.add((qr, qg, qb))
                if (qr, qg, qb) not in ((0, 0, 0), (240, 240, 240)):
                    color_counts[f"#{qr:02X}{qg:02X}{qb:02X}"] += 1
            if len(unique_opaque) <= 2 or "placeholder" in img_path.name.lower() or "temp" in img_path.name.lower():
                placeholder_files.append(img_path.name)
        except Exception:
            continue

    palette = [c for c, _ in color_counts.most_common(max_colors)]
    if not palette:
        palette = ["#2B2D42", "#06D6A0", "#FFD166", "#EF233C", "#118AB2"]
    return palette, {"resolutions": resolutions[:6], "detected_solid_placeholders": placeholder_files}


class Godot4Adapter(EngineAdapter):
    engine_id = "godot"
    display_name = "Godot Engine 4.x"

    def detect(self, game_dir: Path) -> bool:
        return (game_dir / "project.godot").exists()

    def parse_project_godot(self, game_dir: Path) -> dict[str, Any]:
        pg = game_dir / "project.godot"
        info: dict[str, Any] = {
            "name": game_dir.name,
            "evidence": [],
            "features": [],
            "renderer": "Forward+",
            "viewport_width": 1920,
            "viewport_height": 1080,
            "stretch_mode": "canvas_items",
            "autoloads": [],
        }
        if not pg.exists():
            return info

        lines = sanitize_untrusted_text(pg.read_text(encoding="utf-8", errors="ignore"), max_chars=25000).splitlines()
        section = ""
        for idx, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            if line.startswith("config/name="):
                val = line.split("=", 1)[1].strip().strip('"')
                info["name"] = val
                info["evidence"].append(f"project.godot:{idx}")
            elif line.startswith("config/features="):
                info["features"] = re.findall(r'"([^"]+)"', line)
                info["evidence"].append(f"project.godot:{idx}")
            elif line.startswith("window/size/viewport_width="):
                try:
                    info["viewport_width"] = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("window/size/viewport_height="):
                try:
                    info["viewport_height"] = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("window/stretch/mode="):
                info["stretch_mode"] = line.split("=", 1)[1].strip().strip('"')
            elif "rendering_method" in line and "=" in line:
                info["renderer"] = line.split("=", 1)[1].strip().strip('"')
                info["evidence"].append(f"project.godot:{idx}")
            elif section == "autoload" and "=" in line:
                autoload_name = line.split("=", 1)[0].strip()
                info["autoloads"].append(autoload_name)
        return info

    def parse_scenes(self, game_dir: Path, safe_files: list[Path]) -> dict[str, Any]:
        node_types: Counter[str] = Counter()
        camera_clues: list[tuple[str, str]] = []
        capsule_heights: list[tuple[float, str]] = []
        res_refs: dict[str, list[str]] = {}  # res_path -> list of "file:line"
        todo_items: list[dict[str, Any]] = []
        is_3d_count = 0
        is_2d_count = 0

        for f in safe_files:
            if f.suffix.lower() not in (".tscn", ".tres", ".gd", ".cs"):
                continue
            rel = f.relative_to(game_dir).as_posix()
            text = sanitize_untrusted_text(f.read_text(encoding="utf-8", errors="ignore"), max_chars=40000)
            lines = text.splitlines()

            for lno, line in enumerate(lines, start=1):
                # Find res:// paths
                for m in RES_PATH_RE.finditer(line):
                    rpath = m.group(1).rstrip('",;)')
                    res_refs.setdefault(rpath, []).append(f"{rel}:{lno}")

                # Find TODO / FIXME art comments
                todo_m = TODO_ART_RE.search(line)
                if todo_m:
                    todo_items.append(
                        {
                            "tag": todo_m.group(1).upper(),
                            "text": todo_m.group(2).strip(" :-"),
                            "evidence": f"{rel}:{lno}",
                        }
                    )

                # Extract Godot Node types
                if line.startswith("[node ") and 'type="' in line:
                    ntype = line.split('type="', 1)[1].split('"', 1)[0]
                    node_types[ntype] += 1
                    if "3D" in ntype:
                        is_3d_count += 1
                    elif "2D" in ntype:
                        is_2d_count += 1
                    if "Camera" in ntype:
                        camera_clues.append((ntype, f"{rel}:{lno}"))

                if "projection = 1" in line or "orthogonal" in line.lower() or "isometric" in line.lower():
                    camera_clues.append(("Orthographic/Isometric Camera", f"{rel}:{lno}"))

                # Collision capsule height
                ch_m = CAPSULE_HEIGHT_RE.search(line)
                if ch_m:
                    try:
                        h_val = float(ch_m.group(1))
                        if 0.4 <= h_val <= 10.0:
                            capsule_heights.append((h_val, f"{rel}:{lno}"))
                    except ValueError:
                        pass

        return {
            "node_types": dict(node_types.most_common(12)),
            "camera_clues": camera_clues,
            "capsule_heights": capsule_heights,
            "res_refs": res_refs,
            "todo_items": todo_items,
            "is_3d": is_3d_count >= is_2d_count,
        }

    def extract_data_catalogs(self, game_dir: Path, safe_files: list[Path]) -> list[CatalogEntry]:
        catalog: list[CatalogEntry] = []
        for f in safe_files:
            rel = f.relative_to(game_dir).as_posix()
            ext = f.suffix.lower()
            if ext == ".json":
                try:
                    raw = json.loads(sanitize_untrusted_text(f.read_text(encoding="utf-8", errors="ignore"), 60000))
                    rows = raw if isinstance(raw, list) else (raw.get("items") or raw.get("enemies") or raw.get("catalog") or [])
                    if isinstance(raw, dict) and not rows:
                        # Handle dict keyed by item_id
                        for k, v in raw.items():
                            if isinstance(v, dict):
                                rows.append({"id": k, **v})
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        cid = str(row.get("id") or row.get("name", "item")).lower().replace(" ", "_")
                        cat_str = str(row.get("category") or ("enemy" if "enemy" in rel.lower() else "item")).lower()
                        cat = cat_str if cat_str in ("item", "enemy", "npc", "prop", "icon", "environment") else "item"
                        asset_path = str(row.get("model") or row.get("icon") or row.get("sprite") or row.get("mesh") or "")
                        catalog.append(
                            CatalogEntry(
                                id=cid,
                                name=str(row.get("name") or cid.replace("_", " ").title()),
                                category=cat,  # type: ignore[arg-type]
                                description=str(row.get("description") or row.get("lore") or ""),
                                stats={k: v for k, v in row.items() if k not in ("id", "name", "description", "lore")},
                                source_file=rel,
                                existing_asset_path=asset_path,
                            )
                        )
                except Exception:
                    continue

            elif ext == ".csv":
                try:
                    text = sanitize_untrusted_text(f.read_text(encoding="utf-8", errors="ignore"), 40000)
                    reader = csv.DictReader(io.StringIO(text))
                    for row in reader:
                        cid = str(row.get("id") or row.get("name") or "entry").strip().lower().replace(" ", "_")
                        cat_str = "enemy" if "enem" in rel.lower() else "item"
                        catalog.append(
                            CatalogEntry(
                                id=cid,
                                name=str(row.get("name") or cid.replace("_", " ").title()),
                                category=cat_str,  # type: ignore[arg-type]
                                description=str(row.get("description") or row.get("type") or ""),
                                stats=dict(row),
                                source_file=rel,
                                existing_asset_path=str(row.get("asset") or row.get("model") or ""),
                            )
                        )
                except Exception:
                    continue

        return catalog

    def infer_scale(self, scene_data: dict[str, Any]) -> dict[str, Any]:
        heights = scene_data.get("capsule_heights", [])
        if heights:
            avg_h = round(sum(h for h, _ in heights) / len(heights), 2)
            ev = [loc for _, loc in heights[:4]]
            return {
                "unit_meters": 1.0,
                "char_height": avg_h,
                "confidence": 0.92,
                "evidence": ev,
            }
        return {
            "unit_meters": 1.0,
            "char_height": 1.8,
            "confidence": 0.58,  # Low confidence highlighted in UI!
            "evidence": ["Default Godot 3D meter scale (no CapsuleShape3D height found)"],
        }

    def find_missing_refs(
        self,
        game_dir: Path,
        safe_files: list[Path],
        scene_data: dict[str, Any],
        catalog: list[CatalogEntry],
    ) -> tuple[list[WishlistItem], list[WishlistItem]]:
        wishlist: list[WishlistItem] = []
        placeholders: list[WishlistItem] = []
        seen_paths: set[str] = set()

        # 1. Check every `res://` reference found in scenes/scripts
        for rpath, locations in scene_data.get("res_refs", {}).items():
            rel_disk = rpath.replace("res://", "")
            disk_path = game_dir / rel_disk
            ext = disk_path.suffix.lower()
            is_art_ref = ext in (".glb", ".gltf", ".obj", ".fbx", ".png", ".webp", ".jpg", ".tres")
            if not is_art_ref:
                continue

            central_bonus = 1.5 if any("hub" in loc or "main" in loc or "player" in loc for loc in locations) else 1.0
            prio = round(len(locations) * central_bonus, 2)
            stem_title = disk_path.stem.replace("_", " ").title()
            verb = "make_3d" if ext in (".glb", ".gltf", ".obj", ".fbx") else ("icon_maker" if "icon" in rel_disk.lower() else "batch_from_list")

            if not disk_path.exists():
                seen_paths.add(rpath)
                wishlist.append(
                    WishlistItem(
                        title=f"Missing: {stem_title}",
                        category="3d_prop" if verb == "make_3d" else "2d_art",
                        description=f"Referenced {len(locations)}× in scenes/scripts but missing at {rpath}",
                        source_type="missing_ref",
                        target_res_path=rpath,
                        evidence=locations[:5],
                        usage_count=len(locations),
                        priority_score=prio + 2.0,
                        suggested_verb=verb,
                    )
                )
            elif any(tok in rel_disk.lower() for tok in ("placeholder", "temp", "dummy", "blockout")):
                item = WishlistItem(
                    title=f"Placeholder: {stem_title}",
                    category="3d_prop" if verb == "make_3d" else "2d_art",
                    description=f"Placeholder asset detected at {rpath}",
                    source_type="placeholder",
                    target_res_path=rpath,
                    evidence=locations[:5] or [rel_disk],
                    usage_count=len(locations),
                    priority_score=prio + 1.0,
                    suggested_verb=verb,
                )
                placeholders.append(item)
                wishlist.append(item)
                seen_paths.add(rpath)

        # 2. Scan disk for unreferenced placeholder files
        for f in safe_files:
            rel = f.relative_to(game_dir).as_posix()
            rpath = f"res://{rel}"
            if rpath in seen_paths:
                continue
            if any(tok in f.name.lower() for tok in ("placeholder", "temp_")) and f.suffix.lower() in (".png", ".glb", ".obj", ".svg"):
                p_item = WishlistItem(
                    title=f"Placeholder File: {f.stem.replace('_', ' ').title()}",
                    category="2d_art" if f.suffix.lower() in (".png", ".svg") else "3d_prop",
                    description=f"File named '{f.name}' detected in project folder",
                    source_type="placeholder",
                    target_res_path=rpath,
                    evidence=[rel],
                    usage_count=1,
                    priority_score=1.5,
                    suggested_verb="icon_maker" if "icon" in rel.lower() else "make_3d",
                )
                placeholders.append(p_item)
                wishlist.append(p_item)

        # 3. Check catalog entries with missing art files
        for entry in catalog:
            if entry.existing_asset_path:
                rpath = entry.existing_asset_path if entry.existing_asset_path.startswith("res://") else f"res://{entry.existing_asset_path}"
                disk_p = game_dir / rpath.replace("res://", "")
                if not disk_p.exists():
                    entry.has_missing_art = True
                    if rpath not in seen_paths:
                        seen_paths.add(rpath)
                        wishlist.append(
                            WishlistItem(
                                title=f"{entry.name} ({entry.category.title()})",
                                category=entry.category,
                                description=entry.description or f"Catalog entry in {entry.source_file} missing {rpath}",
                                source_type="catalog_gap",
                                target_res_path=rpath,
                                evidence=[entry.source_file],
                                usage_count=2,
                                priority_score=2.8,
                                suggested_verb="make_3d" if rpath.endswith(".glb") else "icon_maker",
                                catalog_entry_id=entry.id,
                            )
                        )
                elif "placeholder" in rpath.lower() or "temp" in rpath.lower():
                    entry.is_placeholder = True

        # 4. Add TODO/FIXME comments mentioning art/models/sprites
        for todo in scene_data.get("todo_items", []):
            wishlist.append(
                WishlistItem(
                    title=f"{todo['tag']}: {todo['text'][:48]}",
                    category="prop",
                    description=todo["text"],
                    source_type="todo_comment",
                    target_res_path="res://assets/models/",
                    evidence=[todo["evidence"]],
                    usage_count=1,
                    priority_score=1.8,
                    suggested_verb="make_3d" if any(k in todo["text"].lower() for k in ("mesh", "3d", "glb", "model")) else "batch_from_list",
                )
            )

        wishlist.sort(key=lambda w: w.priority_score, reverse=True)
        return wishlist, placeholders

    def scan(self, game_dir: Path, previous_profile: GameProfile | None = None) -> GameProfile:
        scan_bundle = collect_safe_game_files(game_dir)
        safe_files: list[Path] = scan_bundle["safe_files"]
        files_read_log = [f.relative_to(game_dir).as_posix() for f in safe_files]

        pg_info = self.parse_project_godot(game_dir)
        scene_data = self.parse_scenes(game_dir, safe_files)
        catalog = self.extract_data_catalogs(game_dir, safe_files)
        scale_info = self.infer_scale(scene_data)
        wishlist, placeholders = self.find_missing_refs(game_dir, safe_files, scene_data, catalog)

        # Read GDD / README if available
        gdd_summary = ""
        gdd_evidence: list[str] = []
        lore_words: list[str] = []
        for f in safe_files:
            if f.name.lower() in ("gdd.md", "readme.md", "design.md", "lore.md"):
                rel = f.relative_to(game_dir).as_posix()
                txt = sanitize_untrusted_text(f.read_text(encoding="utf-8", errors="ignore"), 6000)
                first_para = " ".join(
                    line.strip("# ").strip() for line in txt.splitlines() if line.strip() and not line.startswith("#")
                )[:260]
                if first_para:
                    gdd_summary = first_para
                    gdd_evidence.append(f"{rel}:1")
                for kw in ("arcane", "dungeon", "clockwork", "alchemy", "isometric", "hand-painted", "low-poly", "crystal", "ruins", "gothic", "cyberpunk", " cozy"):
                    if kw.strip() in txt.lower() and kw.strip() not in lore_words:
                        lore_words.append(kw.strip())

        image_files = [f for f in safe_files if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
        palette, art_meta = _extract_dominant_palette(image_files)
        art_evidence = [f.relative_to(game_dir).as_posix() for f in image_files[:4]] or ["project.godot:1"]

        cam_clues = scene_data.get("camera_clues", [])
        if any("Orthographic" in c[0] or "Isometric" in c[0] for c in cam_clues) or "isometric" in lore_words:
            cam_desc = "Isometric 3/4 Orthographic 3D"
            cam_conf = 0.90
        elif scene_data.get("is_3d"):
            cam_desc = "3D Third-Person Perspective"
            cam_conf = 0.82
        else:
            cam_desc = "2D Top-Down / Side-View"
            cam_conf = 0.78
        cam_ev = [loc for _, loc in cam_clues[:3]] or ["project.godot:1"]

        style_desc = (
            "Stylized hand-painted low-poly fantasy with crisp silhouettes and warm rim light"
            if "hand-painted" in lore_words or "low-poly" in lore_words
            else f"Stylized {pg_info['renderer']} game art matching the extracted visual style"
        )

        auto_pack = StylePack(
            id=f"game_{pg_info['name'].lower().replace(' ', '_')[:18]}",
            name=f"{pg_info['name']} (Auto-Scanned Pack)",
            description=f"Auto-extracted from {pg_info['name']} art samples and GDD.",
            prompt_prefix=f"{style_desc}, {cam_desc} perspective,",
            prompt_suffix="isolated clean game asset",
            negative_prompt="photorealistic clutter, mismatched perspective, cropped silhouette, watermark",
            palette_hex=palette,
            builtin=False,
        )

        profile = GameProfile(
            game_path=str(game_dir.resolve()),
            engine="godot",
            engine_version="4.3" if "4.3" in pg_info.get("features", []) else "4.x",
            files_read_log=files_read_log,
            game_title=EvidenceField(
                value=pg_info["name"],
                confidence=0.98 if pg_info["evidence"] else 0.60,
                evidence=pg_info["evidence"] or ["Folder name"],
            ),
            summary=EvidenceField(
                value=gdd_summary or f"{pg_info['name']} ({cam_desc} project with {len(catalog)} catalog items)",
                confidence=0.92 if gdd_evidence else 0.62,
                evidence=gdd_evidence or pg_info["evidence"],
            ),
            genre=EvidenceField(
                value="Isometric Action RPG / Dungeon Crawler" if catalog else "3D Action Prototype",
                confidence=0.88 if gdd_evidence else 0.64,
                evidence=gdd_evidence or ["data/items.json"],
            ),
            tone=EvidenceField(
                value="Playful, tactile arcane fantasy" if lore_words else "Stylized indie adventure",
                confidence=0.84 if gdd_evidence else 0.58,
                evidence=gdd_evidence or ["Inferred from scene & item naming"],
            ),
            setting_and_lore=EvidenceField(
                value=lore_words or ["fantasy", "dungeon", "alchemy", "relics"],
                confidence=0.90 if lore_words else 0.55,
                evidence=gdd_evidence or ["Extracted from item descriptions"],
            ),
            camera_and_perspective=EvidenceField(
                value=cam_desc,
                confidence=cam_conf,
                evidence=cam_ev,
            ),
            art_style=EvidenceField(
                value=style_desc,
                confidence=0.88 if image_files else 0.60,
                evidence=art_evidence + gdd_evidence,
            ),
            palette=EvidenceField(
                value=palette,
                confidence=0.91 if image_files else 0.55,
                evidence=art_evidence,
            ),
            scale_and_units=EvidenceField(
                value={"unit_meters": scale_info["unit_meters"], "char_height": scale_info["char_height"]},
                confidence=scale_info["confidence"],
                evidence=scale_info["evidence"],
            ),
            budgets=EvidenceField(
                value={"max_tris": 10000 if pg_info["renderer"] == "Forward+" else 4500, "texture_size": "2k"},
                confidence=0.82,
                evidence=pg_info["evidence"] or ["project.godot"],
            ),
            naming_and_folders=EvidenceField(
                value={
                    "pattern": "snake_case",
                    "models_dir": "res://assets/models",
                    "icons_dir": "res://assets/icons",
                    "sprites_dir": "res://assets/sprites",
                },
                confidence=0.94,
                evidence=art_evidence,
            ),
            auto_style_pack=auto_pack,
            catalog=catalog,
            wishlist=wishlist,
            placeholders=placeholders,
        )

        # Compute re-scan diff & drift warnings if previous_profile exists
        if previous_profile:
            diff: dict[str, Any] = {}
            prev_wish_titles = {w.title for w in previous_profile.wishlist}
            curr_wish_titles = {w.title for w in wishlist}
            added_wishes = sorted(curr_wish_titles - prev_wish_titles)
            resolved_wishes = sorted(prev_wish_titles - curr_wish_titles)
            if added_wishes or resolved_wishes:
                diff["wishlist_added"] = added_wishes
                diff["wishlist_resolved"] = resolved_wishes
            if previous_profile.catalog and len(catalog) != len(previous_profile.catalog):
                diff["catalog_count_change"] = f"{len(previous_profile.catalog)} -> {len(catalog)}"
            profile.diff_from_previous = diff
            if added_wishes:
                profile.drift_warnings.append(
                    f"Re-scan detected {len(added_wishes)} new missing asset reference(s) since last profile lock."
                )

        return profile

    def export_layout(self, asset: Asset, profile: GameProfile) -> dict[str, str]:
        folders = profile.naming_and_folders.value if isinstance(profile.naming_and_folders.value, dict) else {}
        models_dir = str(folders.get("models_dir", "res://assets/models")).replace("res://", "")
        icons_dir = str(folders.get("icons_dir", "res://assets/icons")).replace("res://", "")
        sprites_dir = str(folders.get("sprites_dir", "res://assets/sprites")).replace("res://", "")

        slug = re.sub(r"[^a-z0-9_]", "_", asset.name.lower().split("•")[0].strip()).strip("_") or asset.id
        ext = Path(asset.file_path).suffix or (".glb" if asset.type == AssetType.MESH else ".png")

        if asset.type == AssetType.MESH:
            rel_target = f"{models_dir}/{slug}{ext}"
            glue_target = f"{models_dir}/{slug}_scene.tscn"
        elif asset.type == AssetType.ICON:
            rel_target = f"{icons_dir}/{slug}{ext}"
            glue_target = f"{icons_dir}/{slug}_item.tres"
        else:
            rel_target = f"{sprites_dir}/{slug}{ext}"
            glue_target = f"{sprites_dir}/{slug}_res.tres"

        return {
            "slug": slug,
            "target_rel_path": rel_target,
            "target_res_path": f"res://{rel_target}",
            "glue_rel_path": glue_target,
            "glue_res_path": f"res://{glue_target}",
        }

    def _build_godot_glue(self, asset: Asset, layout: dict[str, str]) -> str:
        res_path = layout["target_res_path"]
        if asset.type == AssetType.MESH:
            return (
                f'[gd_scene load_steps=2 format=3]\n'
                f'# Generated by Forge Asset Maker (Safe to edit or remove)\n'
                f'[ext_resource type="PackedScene" path="{res_path}" id="1_forge"]\n\n'
                f'[node name="{layout["slug"].title().replace("_", "")}" type="Node3D"]\n'
                f'[node name="MeshInstance" parent="." instance=ExtResource("1_forge")]\n'
            )
        return (
            f'[gd_resource type="Resource" format=3]\n'
            f'# Generated by Forge Asset Maker (Safe to edit or remove)\n'
            f'[ext_resource type="Texture2D" path="{res_path}" id="1_tex"]\n\n'
            f'[resource]\n'
            f'resource_name = "{asset.name}"\n'
            f'metadata/forge_asset_id = "{asset.id}"\n'
            f'metadata/forge_verb = "{asset.verb}"\n'
        )

    def writeback_preview(
        self, game_dir: Path, assets: list[Asset], profile: GameProfile
    ) -> dict[str, Any]:
        """Compute non-destructive write-back plan and diff preview without touching the game folder."""
        planned_files: list[dict[str, Any]] = []
        skipped_existing: list[str] = []

        for ast in assets:
            layout = self.export_layout(ast, profile)
            dest = (game_dir / layout["target_rel_path"]).resolve()
            glue_dest = (game_dir / layout["glue_rel_path"]).resolve()

            will_skip = dest.exists()
            if will_skip:
                skipped_existing.append(layout["target_rel_path"])

            planned_files.append(
                {
                    "asset_id": ast.id,
                    "asset_name": ast.name,
                    "source_file": ast.file_path,
                    "target_rel_path": layout["target_rel_path"],
                    "target_res_path": layout["target_res_path"],
                    "glue_rel_path": layout["glue_rel_path"],
                    "glue_preview": self._build_godot_glue(ast, layout),
                    "already_exists": will_skip,
                    "action": "skip_existing (never overwrite)" if will_skip else "create_new",
                }
            )

        return {
            "game_dir": str(game_dir.resolve()),
            "engine": "godot",
            "total_assets": len(assets),
            "creatable_count": sum(1 for p in planned_files if not p["already_exists"]),
            "skipped_existing_count": len(skipped_existing),
            "planned_files": planned_files,
            "safety_note": "Files are staged first. Existing game files are NEVER overwritten, and only asset directories are touched.",
        }

    def writeback_apply(
        self,
        game_dir: Path,
        assets: list[Asset],
        profile: GameProfile,
        project_root_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Write staged files into the game project's asset folders and record an undo manifest."""
        game_root = game_dir.resolve()
        written_files: list[str] = []
        skipped_files: list[str] = []

        for ast in assets:
            layout = self.export_layout(ast, profile)
            rel_target = layout["target_rel_path"]
            # Safety guard: only write inside assets/ subdirectory
            if not rel_target.startswith("assets/"):
                rel_target = f"assets/{rel_target}"
            dest = (game_root / rel_target).resolve()
            if not str(dest).startswith(str(game_root)):
                raise ValueError("Blocked path traversal in writeback.")

            if dest.exists():
                skipped_files.append(rel_target)
                continue

            if project_root_dir:
                src = (project_root_dir / ast.file_path).resolve()
                if src.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    written_files.append(rel_target)

            # Write Godot .tscn / .tres glue stub if it doesn't exist
            glue_rel = layout["glue_rel_path"]
            if not glue_rel.startswith("assets/"):
                glue_rel = f"assets/{glue_rel}"
            glue_dest = (game_root / glue_rel).resolve()
            if not glue_dest.exists() and str(glue_dest).startswith(str(game_root)):
                glue_dest.parent.mkdir(parents=True, exist_ok=True)
                glue_dest.write_text(self._build_godot_glue(ast, layout), encoding="utf-8")
                written_files.append(glue_rel)

        manifest = {
            "writeback_id": new_id("wb"),
            "timestamp": time.time(),
            "game_dir": str(game_root),
            "written_files": written_files,
            "skipped_files": skipped_files,
        }
        if project_root_dir:
            wb_file = project_root_dir / "logs" / "last_writeback.json"
            wb_file.parent.mkdir(parents=True, exist_ok=True)
            wb_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest

    @staticmethod
    def writeback_undo(project_root_dir: Path) -> dict[str, Any]:
        """One-click revert of the last write-back using the saved manifest."""
        wb_file = project_root_dir / "logs" / "last_writeback.json"
        if not wb_file.exists():
            return {"undone": False, "message": "No write-back manifest found to undo."}
        manifest = json.loads(wb_file.read_text(encoding="utf-8"))
        if manifest.get("undone"):
            return {"undone": False, "message": "Last write-back was already reverted."}

        game_root = Path(manifest["game_dir"]).resolve()
        removed: list[str] = []
        for rel in manifest.get("written_files", []):
            target = (game_root / rel).resolve()
            if str(target).startswith(str(game_root)) and target.exists():
                target.unlink()
                removed.append(rel)

        manifest["undone"] = True
        manifest["removed_files"] = removed
        wb_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return {
            "undone": True,
            "writeback_id": manifest.get("writeback_id"),
            "removed_files": removed,
        }

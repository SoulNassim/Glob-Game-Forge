"""Unified Game World Bible, Per-Folder .md Asset Architecture, Slime Agent Brain & Game Importer.

Organizes each project (`projects/<name>/workspace/`) into a clean, logical file tree:
  workspace/
    GAME.md                       # Global World Bible & Category Style Rules
    ASSET_REQUIREMENTS.md         # Master checklist for external coding agents
    characters/                   # Individual .md file per character (Goal, Description, Prompt, Outputs)
    items/                        # Individual .md file per weapon/prop/item
    cards/                        # Individual .md file per card illustration
    environments/                 # Individual .md file per board/tile/environment
    ui/                           # Individual .md file per UI icon/frame
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from PIL import Image
from pydantic import BaseModel, Field

from forge.gamescan.safety import collect_safe_game_files, sanitize_untrusted_text
from forge.gamescan.stubs import detect_engine_adapter
from forge.blueprint_contract import (
    game_metadata_from_bible,
    merge_generated_asset_frontmatter,
    pipeline_item_frontmatter,
    serialize_frontmatter,
    split_frontmatter,
)
from forge.models import ActivityLogEntry, Asset, AssetStatus, AssetType, ProjectBible, new_id
from forge.providers.auth_manager import call_vscode_copilot_model
from forge.storage.project_store import ProjectStore


CATEGORY_TO_FOLDER = {
    "character": "characters",
    "item": "items",
    "card_art": "cards",
    "environment": "environments",
    "ui": "ui",
}

FOLDER_TO_CATEGORY = {v: k for k, v in CATEGORY_TO_FOLDER.items()}
CATEGORY_FOLDERS = tuple(FOLDER_TO_CATEGORY)

_FOLDER_SEGMENT_MAX = 64
_FOLDER_PATH_MAX = 240
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _normalize_folder_segment(value: str) -> str:
    """Return a safe, portable folder segment or raise for unsafe input."""
    raw = str(value).strip()
    if not raw:
        raise ValueError("Folder names cannot be empty.")
    if len(raw) > _FOLDER_SEGMENT_MAX:
        raise ValueError(f"Folder names must be {_FOLDER_SEGMENT_MAX} characters or fewer.")
    if raw in {".", ".."} or "/" in raw or "\\" in raw or ":" in raw:
        raise ValueError("Folder names cannot contain path separators or traversal segments.")
    if any(ord(char) < 32 for char in raw):
        raise ValueError("Folder names cannot contain control characters.")
    if any(char in '<>:"|?*' for char in raw):
        raise ValueError("Folder names contain an unsupported character.")
    if raw.endswith((" ", ".")):
        raise ValueError("Folder names cannot end with a space or period.")
    reserved = raw.rstrip(" .").split(".", 1)[0].upper()
    if reserved in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Folder name '{raw}' is reserved on Windows.")

    normalized = re.sub(r"[\s-]+", "_", raw.lower())
    normalized = re.sub(r"[^a-z0-9_]", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        raise ValueError("Folder names must contain at least one letter or number.")
    return normalized[:_FOLDER_SEGMENT_MAX].rstrip("_")


def normalize_nested_folder_path(folder_path: str, *, allow_empty: bool = True) -> str:
    """Normalize a nested path below a category without allowing traversal."""
    raw = str(folder_path or "").strip().replace("\\", "/")
    if not raw:
        if allow_empty:
            return ""
        raise ValueError("A nested folder path is required.")
    if raw.startswith("/") or ":" in raw or len(raw) > _FOLDER_PATH_MAX:
        raise ValueError("Folder paths must be relative to a category.")
    parts = raw.split("/")
    if any(not part.strip() or part.strip() in {".", ".."} for part in parts):
        raise ValueError("Folder paths cannot contain empty or traversal segments.")
    return "/".join(_normalize_folder_segment(part) for part in parts)


def normalize_category_folder_path(folder_path: str, *, allow_root: bool = True) -> str:
    """Normalize a category-relative workspace path such as ``characters/dogs``."""
    raw = str(folder_path or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ":" in raw or len(raw) > _FOLDER_PATH_MAX:
        raise ValueError("A category folder path is required.")
    parts = raw.split("/")
    if any(not part.strip() or part.strip() in {".", ".."} for part in parts):
        raise ValueError("Folder paths cannot contain empty or traversal segments.")

    root = _normalize_folder_segment(parts[0])
    if root in CATEGORY_TO_FOLDER:
        root = CATEGORY_TO_FOLDER[root]
    if root not in FOLDER_TO_CATEGORY:
        raise ValueError(
            f"Unknown category folder '{parts[0]}'. Expected one of: {', '.join(CATEGORY_FOLDERS)}."
        )
    if len(parts) == 1 and not allow_root:
        raise ValueError("A nested folder below a category is required.")
    nested = normalize_nested_folder_path("/".join(parts[1:]), allow_empty=True)
    return "/".join(part for part in (root, nested) if part)

DEFAULT_CATEGORY_PROMPTS: dict[str, dict[str, str]] = {
    "character": {
        "label": "Character / Figurine",
        "folder": "characters",
        "prompt_rule": "Full-body dark fantasy tabletop figurine on a carved circular stone pedestal base, readable chunky proportions, dramatic top-down dungeon rim lighting, clean isolated background, no cropped limbs.",
        "default_workflow": "make_3d_figurine",
    },
    "item": {
        "label": "Item / Weapon / Prop",
        "folder": "items",
        "prompt_rule": "Standalone game prop or item with a high-contrast silhouette, centered isometric 3/4 view, clean transparent background, materials matching the project's art style.",
        "default_workflow": "make_3d_prop",
    },
    "card_art": {
        "label": "Card / Flat Art",
        "folder": "cards",
        "prompt_rule": "Illustrated card art with dramatic painterly lighting, dynamic focal subject framed for vertical card portrait, rich atmospheric background matching the project's art style.",
        "default_workflow": "approve_2d",
    },
    "ui": {
        "label": "UI Frame / Orb / Icon",
        "folder": "ui",
        "prompt_rule": "Game UI element or icon in the project's art style, crisp orthographic front view, clean alpha channel.",
        "default_workflow": "approve_2d",
    },
    "environment": {
        "label": "Board / Environment Tile",
        "folder": "environments",
        "prompt_rule": "Dark candlelit stone dungeon tactical chessboard environment, cracked obsidian and limestone tiles with glowing orange rune inlays, warm torchlight bounce.",
        "default_workflow": "make_3d_prop",
    },
}

DEFAULT_MATERIAL_TAGS: dict[str, str] = {
    "Weathered Iron": "hammered dark iron with battle scratches and oiled steel highlights",
    "Mossy Castle Stone": "cracked ancient fortress masonry overgrown with damp dungeon moss and glowing magma seams",
    "Crimson Royal": "deep burgundy velvet drapery with tarnished silver filigree and dark plate armor",
    "Amber Runes": "incandescent carved orange-gold runes pulsing with arcane heat",
    "Aged Parchment & Wood": "candlelit medieval war-table oak, tarnished brass coins, and ink-stained parchment",
}

WORKFLOW_PRESETS: dict[str, dict[str, Any]] = {
    "make_3d_figurine": {
        "id": "make_3d_figurine",
        "label": "Make 3D Figurine (GLB)",
        "description": "Convert winning 2D image into a 3D mesh on a tabletop pedestal (Meshy 7.1, 12k tris, PBR textures).",
        "params": {"ai_model": "meshy-7.1", "target_polycount": 12000, "should_texture": True, "enable_pbr": True},
    },
    "make_3d_prop": {
        "id": "make_3d_prop",
        "label": "Make 3D Prop / Weapon (GLB)",
        "description": "Convert winning 2D image into a game-ready low/mid-poly 3D prop (smart-topology, 6k tris).",
        "params": {"ai_model": "meshy-t2.5", "model_type": "smart-topology", "target_polycount": 6000, "should_texture": True},
    },
    "split_body_parts": {
        "id": "split_body_parts",
        "label": "Split Character into Rig Parts",
        "description": "Isolate Head, Torso, Arms, Legs, and Weapon into separate clean assets for modular rigging or 3D.",
        "parts": ["Head & Helmet", "Armored Torso", "Left & Right Arms", "Greaves & Boots", "Primary Weapon / Shield"],
    },
    "slice_lineup_sheet": {
        "id": "slice_lineup_sheet",
        "label": "Slice Concept Lineup into 4 Assets",
        "description": "Split a horizontal multi-character concept sheet (e.g. Pawn, Knight, Rook, Queen) into 4 standalone assets.",
        "slices": ["Pawn Footman", "Armored Knight Destrier", "Rook Castle Golem", "Crimson Queen"],
    },
    "make_variants": {
        "id": "make_variants",
        "label": "Make Material / Faction Variants",
        "description": "Generate sibling variations (Iron, Mossy Stone, Crimson, Gold) with locked silhouette.",
    },
    "approve_2d": {
        "id": "approve_2d",
        "label": "Approve 2D Asset Directly",
        "description": "Finalize this 2D illustration/card/icon and mark it ready for the coding agent.",
    },
}


class PipelineItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("item"))
    slug: str
    title: str
    category: str = "character"  # character, item, card_art, ui, environment
    folder: str = ""  # characters, items, cards, environments, ui
    subfolder: str = ""  # e.g. pawns, knights, rooks, royals, bosses, weapons, spells
    order_index: int = 0
    goal: str = "Core gameplay asset with unmistakable silhouette at tabletop camera distance."
    core_idea: str = ""
    gameplay_uniqueness: str = ""
    personality: str = ""
    visual_signature: str = ""
    material_tags: list[str] = Field(default_factory=lambda: ["Weathered Iron"])
    description: str = ""
    target_path: str = "res://assets/models/item.glb"
    candidate_count: int = 1
    prepared_prompt: str = ""
    stage: str = "planned"  # "planned" -> "candidates_ready" -> "winner_picked" -> "approved"
    candidate_asset_ids: list[str] = Field(default_factory=list)
    winner_asset_id: str = ""
    workflow_preset: str = "make_3d_figurine"
    image_params: dict[str, Any] = Field(
        default_factory=lambda: {
            "model": "gemini-nano-banana-2.1",
            "aspect_ratio": "1:1",
            "pose_mode": "",
            "remove_background": True,
        }
    )
    mesh_params: dict[str, Any] = Field(
        default_factory=lambda: {
            "ai_model": "meshy-7.1",
            "model_type": "standard",
            "geometry_resolution": "standard",
            "target_polycount": 12000,
            "topology": "triangle",
            "should_remesh": True,
            "should_texture": True,
            "enable_pbr": True,
            "texture_resolution": "2k",
            "pose_mode": "",
            "origin_at": "bottom",
        }
    )
    final_asset_ids: list[str] = Field(default_factory=list)
    attached_asset_ids: list[str] = Field(default_factory=list)
    sha256: str = ""
    fulfills_request_id: str = ""

    def ensure_folder(self) -> str:
        raw_folder = str(self.folder or "").strip()
        if raw_folder:
            try:
                normalized = normalize_category_folder_path(raw_folder)
            except ValueError:
                normalized = ""
            if normalized:
                folder_parts = normalized.split("/")
                self.folder = folder_parts[0]
                folder_subfolder = "/".join(folder_parts[1:])
                if folder_subfolder:
                    existing_subfolder = str(self.subfolder or "").strip()
                    self.subfolder = "/".join(
                        part for part in (folder_subfolder, existing_subfolder) if part
                    )

        if self.folder not in FOLDER_TO_CATEGORY:
            self.folder = CATEGORY_TO_FOLDER.get(self.category, "characters")
        try:
            self.subfolder = normalize_nested_folder_path(self.subfolder, allow_empty=True)
        except ValueError:
            self.subfolder = ""
        if not self.subfolder:
            self.subfolder = infer_logical_subfolder(self.title, self.category, self.description)
        return self.folder

    @property
    def full_folder_path(self) -> str:
        base = self.ensure_folder()
        return f"{base}/{self.subfolder}" if self.subfolder else base

    @property
    def md_rel_path(self) -> str:
        return f"workspace/{self.full_folder_path}/{self.slug}.md"


def infer_logical_subfolder(title: str, category: str, description: str = "") -> str:
    """Automatically infer a logical nested subfolder inside characters/, items/, cards/, environments/, ui/."""
    combined = f"{title} {description}".lower()
    if category == "character":
        if any(w in combined for w in ("pawn", "levy", "footman", "militia", "peasant", "grunt", "thrall")):
            return "pawns_infantry"
        if any(w in combined for w in ("knight", "cavalry", "destrier", "rider", "lancer", "steed", "horse", "mount")):
            return "knights_cavalry"
        if any(w in combined for w in ("rook", "golem", "bastion", "colossus", "juggernaut", "tower", "giant", "tank")):
            return "rooks_heavy"
        if any(w in combined for w in ("bishop", "priest", "cleric", "monk", "zealot", "inquisitor", "cultist", "mage")):
            return "bishops_casters"
        if any(w in combined for w in ("queen", "king", "sovereign", "monarch", "emperor", "lord", "ruler", "boss")):
            return "royals_commanders"
        if any(w in combined for w in ("enemy", "monster", "beast", "undead", "skeleton", "demon")):
            return "enemies_creatures"
        return "heroes_units"
    if category == "item":
        if any(w in combined for w in ("sword", "blade", "axe", "spear", "dagger", "bow", "hammer", "mace", "scepter", "weapon")):
            return "weapons"
        if any(w in combined for w in ("shield", "helm", "armor", "gauntlet", "crown", "cloak", "boot")):
            return "armor_gear"
        if any(w in combined for w in ("potion", "elixir", "flask", "vial", "scroll", "tome", "book")):
            return "consumables"
        return "props_relics"
    if category == "card_art":
        if any(w in combined for w in ("siege", "ram", "trebuchet", "catapult", "ballista", "fortress")):
            return "siege_cards"
        if any(w in combined for w in ("spell", "curse", "hex", "fire", "lightning", "ritual", "aura")):
            return "spell_cards"
        return "unit_cards"
    if category == "environment":
        if any(w in combined for w in ("board", "grid", "tile", "arena", "table")):
            return "boards_arenas"
        return "dungeon_props"
    return "hud_elements"


def _folder_template_text(store: ProjectStore, category_folder: str) -> str:
    category_folder = normalize_category_folder_path(category_folder).split("/", 1)[0]
    archetype = {
        "characters": "character",
        "items": "prop",
        "cards": "custom",
        "environments": "environment",
        "ui": "ui_element",
    }[category_folder]
    bible = store.get_bible()
    specs: dict[str, Any] = {"poly_budget_tris": bible.polycount_budget}
    if bible.project_dimension in {"2d", "hybrid"}:
        specs["resolution_px"] = 1024
    template = {
        "archetype_template": {
            "archetype": archetype,
            "design_pillars": [
                {"pillar_name": "Gameplay Function", "value": ""},
                {"pillar_name": "Visual Hierarchy", "value": ""},
                {"pillar_name": "Material Breakdown", "value": ""},
            ],
            "modular_parts": [],
            "technical_specs": specs,
        }
    }
    return serialize_frontmatter(
        template,
        f"# {category_folder.replace('_', ' ').title()} Template\n\n"
        "Defaults inherited by blueprints in this folder; child folders may override any field.",
    )


def ensure_workspace_folder(ws_dir: Path, store: ProjectStore, folder_path: str) -> Path:
    """Create a validated category folder and its inheritable metadata markers."""
    normalized = normalize_category_folder_path(folder_path)
    parts = normalized.split("/")
    workspace_root = ws_dir.resolve()
    target = (ws_dir.joinpath(*parts)).resolve()
    if not target.is_relative_to(workspace_root):
        raise ValueError("Folder path is outside the workspace.")

    for index in range(1, len(parts) + 1):
        current = ws_dir.joinpath(*parts[:index])
        resolved = current.resolve()
        if not resolved.is_relative_to(workspace_root):
            raise ValueError("Folder path is outside the workspace.")
        if current.exists() and current.is_symlink():
            raise ValueError("Symlinked workspace folders are not supported.")
        current.mkdir(parents=True, exist_ok=True)
        marker = current / "_folder.md"
        if marker.exists():
            continue
        parent_marker = current.parent / "_folder.md"
        template_text = (
            parent_marker.read_text(encoding="utf-8", errors="ignore")
            if parent_marker.exists()
            else _folder_template_text(store, parts[0])
        )
        marker.write_text(template_text, encoding="utf-8")
    return target


def _encode_image_for_vision(path: Path, max_dim: int = 640) -> tuple[str, str] | None:
    """Resize and JPEG-encode an image file for fast multimodal vision input to GPT-6 / Qwen."""
    if not path.exists() or not path.is_file():
        return None
    try:
        img = Image.open(path).convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        return ("image/jpeg", base64.b64encode(buf.getvalue()).decode("utf-8"))
    except Exception:
        return None


class WorldPipelineManager:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store
        self.ws_dir = self.store.project_dir / "workspace"
        # Root category folders are created lazily (on first use) so a
        # blank workspace can be scaffolded with none of them.
        self.ws_dir.mkdir(parents=True, exist_ok=True)

        self.pipeline_file = self.ws_dir / "PIPELINE.json"
        self.style_rules_file = self.ws_dir / "STYLE_RULES.json"
        self.chat_file = self.ws_dir / "CHAT.json"
        self.game_md_file = self.ws_dir / "GAME.md"
        self.reqs_md_file = self.ws_dir / "ASSET_REQUIREMENTS.md"
        self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        # STYLE_RULES.json and PIPELINE.json belong to the old blueprint pipeline. They are written on first use,
        # not into every new workspace: a fresh project starts with no style of someone else's game in it.
        if self.pipeline_file.exists():
            # Keep existing human-edited blueprints intact; only create missing files.
            for item in self.list_pipeline_items():
                ensure_workspace_folder(self.ws_dir, self.store, item.full_folder_path)
                markdown_path = self.ws_dir / item.full_folder_path / f"{item.slug}.md"
                if not markdown_path.exists():
                    markdown_path.write_text(self._render_individual_item_md(item), encoding="utf-8")

    def get_chat_history(self) -> list[dict[str, Any]]:
        if not self.chat_file.exists():
            return []
        try:
            return json.loads(self.chat_file.read_text(encoding="utf-8"))
        except Exception:
            return []

    def save_chat_history(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.chat_file.write_text(json.dumps(messages[-60:], indent=2), encoding="utf-8")
        return messages

    def get_style_rules(self) -> dict[str, Any]:
        try:
            return json.loads(self.style_rules_file.read_text(encoding="utf-8"))
        except Exception:
            return {
                "categories": DEFAULT_CATEGORY_PROMPTS,
                "materials": DEFAULT_MATERIAL_TAGS,
                "workflows": WORKFLOW_PRESETS,
                "linked_game_folder": "",
            }

    def save_style_rules(self, rules: dict[str, Any]) -> dict[str, Any]:
        self.style_rules_file.write_text(json.dumps(rules, indent=2), encoding="utf-8")
        return rules

    def _folder_record(self, folder_path: Path, item_counts: dict[str, int] | None = None) -> dict[str, Any]:
        relative = folder_path.relative_to(self.ws_dir).as_posix()
        normalized = normalize_category_folder_path(relative)
        parts = normalized.split("/")
        count = (item_counts or {}).get(normalized, 0)
        has_children = any(child.name != "_folder.md" for child in folder_path.iterdir())
        return {
            "path": normalized,
            "name": parts[-1],
            "parent": "/".join(parts[:-1]),
            "category": FOLDER_TO_CATEGORY[parts[0]],
            "folder": parts[0],
            "subfolder": "/".join(parts[1:]),
            "depth": len(parts) - 1,
            "item_count": count,
            "is_empty": count == 0 and not has_children,
            "has_template": (folder_path / "_folder.md").is_file(),
        }

    def list_folders(self) -> list[dict[str, Any]]:
        """List every category folder, including nested folders with no assets."""
        item_counts: dict[str, int] = {}
        if self.pipeline_file.exists():
            for item in self.list_pipeline_items():
                item_counts[item.full_folder_path] = item_counts.get(item.full_folder_path, 0) + 1

        folders: list[dict[str, Any]] = []
        seen: set[str] = set()
        for root_name in CATEGORY_FOLDERS:
            root = self.ws_dir / root_name
            if not root.is_dir() or root.is_symlink():
                continue
            candidates = [root, *root.rglob("*")]
            for path in candidates:
                if not path.is_dir() or path.is_symlink():
                    continue
                try:
                    relative = path.relative_to(self.ws_dir).as_posix()
                    normalized = normalize_category_folder_path(relative)
                    resolved = path.resolve()
                    if not resolved.is_relative_to(self.ws_dir.resolve()) or normalized in seen:
                        continue
                    seen.add(normalized)
                    folders.append(self._folder_record(path, item_counts))
                except (OSError, ValueError):
                    continue
        return sorted(folders, key=lambda entry: (entry["folder"], entry["path"]))

    def create_folder(self, folder_path: str, actor: str = "user") -> dict[str, Any]:
        """Persist a validated category folder and its inheritable ``_folder.md`` marker."""
        normalized = normalize_category_folder_path(folder_path)
        target = ensure_workspace_folder(self.ws_dir, self.store, normalized)
        self.store.log_activity(
            ActivityLogEntry(
                actor=actor,  # type: ignore[arg-type]
                action="create_workspace_folder",
                reason=f"Created workspace folder {normalized}",
            )
        )
        item_counts: dict[str, int] = {}
        if self.pipeline_file.exists():
            for item in self.list_pipeline_items():
                item_counts[item.full_folder_path] = item_counts.get(item.full_folder_path, 0) + 1
        return self._folder_record(target, item_counts)

    def build_prompt_for_item(
        self,
        title: str,
        description: str,
        category: str,
        material_tags: list[str],
        goal: str = "",
        core_idea: str = "",
        gameplay_uniqueness: str = "",
        personality: str = "",
        visual_signature: str = "",
    ) -> str:
        bible = self.store.get_bible()
        rules = self.get_style_rules()
        cat_info = rules.get("categories", {}).get(category) or DEFAULT_CATEGORY_PROMPTS.get("character", {})
        mat_map = rules.get("materials") or DEFAULT_MATERIAL_TAGS

        mat_clauses = [mat_map.get(t, t) for t in material_tags if t]
        mat_str = f" Materials & faction motifs: {'; '.join(mat_clauses)}." if mat_clauses else ""
        blueprint = " ".join(
            f"{label}: {value}." for label, value in (
                ("Core concept", core_idea),
                ("Gameplay signature", gameplay_uniqueness),
                ("Personality", personality),
                ("Visual signature", visual_signature),
            ) if value
        )
        goal_str = f" Gameplay goal: {goal}." if goal else ""

        parts = [
            f"[GAME WORLD CONTEXT: {bible.project_name} — {bible.genre}. Art Style: {bible.art_style}. Camera: {bible.camera_perspective}.]",
            f"[CATEGORY RULE ({cat_info.get('label', category)}): {cat_info.get('prompt_rule', '')}]",
            f"Subject: {title} — {description}.{goal_str} {blueprint}{mat_str}",
        ]
        if category in ("character", "item", "ui"):
            parts.append("Isolated single subject on clean transparent background.")
        return " ".join(p.strip() for p in parts if p.strip())

    def _seed_default_pipeline(self) -> None:
        bible = self.store.get_bible()
        if self.store.project_name == "default":
            bible.art_style = (
                "Grimdark medieval tabletop fantasy, carved stone and weathered dark iron chess figurines on circular stone plinths, "
                "warm candlelit dungeon torchlight with glowing amber and crimson arcane runes"
            )
            bible.genre = "Tactical Chess Deckbuilder RPG"
            bible.tone = "Dark, atmospheric, tactile tabletop war-room"
            bible.camera_perspective = "Isometric 3/4 tabletop & vertical painterly card portraits"
            bible.palette = ["#1A1D24", "#4A525D", "#7A1C24", "#F59E0B", "#5C6B57", "#C8B89A"]
            bible.lore_keywords = ["iron", "mossy castle stone", "crimson velvet", "runic chessboard", "candlelit war table"]
            self.store.save_bible(bible)

        starter_specs = [
            {
                "slug": "pawn_phalanx_footman",
                "title": "Phalanx Pawn (Iron Footman)",
                "category": "character",
                "folder": "characters",
                "goal": "Frontline infantry chess piece that holds board chokepoints with an interlocking shield wall.",
                "material_tags": ["Weathered Iron"],
                "description": "Scarred medieval footman in chainmail and dark iron helmet holding a battle-worn kite shield and short sword on a carved PAWN stone pedestal.",
                "target_path": "res://assets/models/pieces/pawn_footman.glb",
                "workflow_preset": "make_3d_figurine",
            },
            {
                "slug": "knight_thunder_destrier",
                "title": "Thunder Cavalry (Armored Knight)",
                "category": "character",
                "folder": "characters",
                "goal": "High-mobility L-jump flanker figurine that leaps over frontline units and shatters enemy armor.",
                "material_tags": ["Weathered Iron", "Amber Runes"],
                "description": "Rearing warhorse destrier in segmented dark steel barding with glowing ember eyes and tattered crimson caparison on a KNIGHT stone pedestal.",
                "target_path": "res://assets/models/pieces/knight_destrier.glb",
                "workflow_preset": "make_3d_figurine",
            },
            {
                "slug": "rook_castle_golem",
                "title": "Rook Bastion (Living Castle Golem)",
                "category": "character",
                "folder": "characters",
                "goal": "Heavy siege tank figurine that charges down straight files and crushes fortifications.",
                "material_tags": ["Mossy Castle Stone", "Amber Runes"],
                "description": "Colossal living fortress golem with crenellated castle tower shoulders, mossy cracked masonry, iron helmet visage, and molten orange core fissures on a ROOK pedestal.",
                "target_path": "res://assets/models/pieces/rook_golem.glb",
                "workflow_preset": "split_body_parts",
            },
            {
                "slug": "queen_crimson_sovereign",
                "title": "Crimson Queen (Blood Sorceress)",
                "category": "character",
                "folder": "characters",
                "goal": "Apex omnidirectional commander piece channeling dark rune magic across the chessboard.",
                "material_tags": ["Crimson Royal", "Weathered Iron"],
                "description": "Regal dark-fantasy queen in jagged iron crown, steel pauldrons, and tattered crimson velvet gown wielding an obsidian spire scepter on a QUEEN pedestal.",
                "target_path": "res://assets/models/pieces/queen_sovereign.glb",
                "workflow_preset": "make_3d_figurine",
            },
            {
                "slug": "card_shadow_assassin",
                "title": "Shadow Assassin (Cost 5 Bishop Card)",
                "category": "card_art",
                "folder": "cards",
                "goal": "Diagonal stealth strike card played from the war-table hand to eliminate high-value targets.",
                "material_tags": ["Weathered Iron", "Aged Parchment & Wood"],
                "description": "Hooded rogue emerging from dungeon shadows wielding twin daggers dripping with glowing green venom, framed for deckbuilder card portrait.",
                "target_path": "res://assets/cards/shadow_assassin.png",
                "workflow_preset": "approve_2d",
            },
            {
                "slug": "card_battering_ram",
                "title": "Iron-Head Battering Ram (Cost 4 Siege Card)",
                "category": "card_art",
                "folder": "cards",
                "goal": "Siege spell card that pushes enemy figurines back and breaks stone barricades.",
                "material_tags": ["Weathered Iron", "Mossy Castle Stone"],
                "description": "Timber siege battering ram with a forged iron wolf-head prow crashing through castle gates.",
                "target_path": "res://assets/cards/battering_ram.png",
                "workflow_preset": "approve_2d",
            },
            {
                "slug": "runic_dungeon_chessboard",
                "title": "Runic Stone & Iron Chessboard",
                "category": "environment",
                "folder": "environments",
                "goal": "Central 8x8 tactical war-table arena where all figurines and spell VFX play out.",
                "material_tags": ["Mossy Castle Stone", "Weathered Iron", "Amber Runes"],
                "description": "Heavy iron-bound stone chessboard with alternating dark slate and limestone squares etched with glowing orange movement runes.",
                "target_path": "res://assets/models/board/runic_chessboard.glb",
                "workflow_preset": "make_3d_prop",
            },
        ]

        items: list[PipelineItem] = []
        for idx, s in enumerate(starter_specs):
            prompt = self.build_prompt_for_item(
                s["title"], s["description"], s["category"], s["material_tags"], s["goal"]
            )
            items.append(
                PipelineItem(
                    slug=s["slug"],
                    title=s["title"],
                    category=s["category"],
                    folder=s["folder"],
                    order_index=idx,
                    goal=s["goal"],
                    material_tags=s["material_tags"],
                    description=s["description"],
                    target_path=s["target_path"],
                    candidate_count=2,
                    prepared_prompt=prompt,
                    workflow_preset=s["workflow_preset"],
                )
            )

        self.save_pipeline_items(items)

    def list_pipeline_items(self) -> list[PipelineItem]:
        if not self.pipeline_file.exists():
            return []
        try:
            raw = json.loads(self.pipeline_file.read_text(encoding="utf-8"))
            items = [PipelineItem(**x) for x in raw]
            for idx, it in enumerate(items):
                it.ensure_folder()
                if it.order_index == 0 and idx > 0:
                    it.order_index = idx
            items.sort(key=lambda x: x.order_index)
            return items
        except Exception:
            return []

    def save_pipeline_items(self, items: list[PipelineItem]) -> list[PipelineItem]:
        for idx, it in enumerate(items):
            it.ensure_folder()
            it.order_index = idx
        self.pipeline_file.write_text(
            json.dumps([i.model_dump() for i in items], indent=2),
            encoding="utf-8",
        )
        self._sync_to_markdown_files(items)
        return items

    def _variant_assets_for_item(self, item: PipelineItem) -> list[Asset]:
        linked_ids = set(item.candidate_asset_ids + item.final_asset_ids)
        if item.winner_asset_id:
            linked_ids.add(item.winner_asset_id)
        if not linked_ids:
            return []

        assets = self.store.list_assets(include_archived=False)
        asset_by_id = {asset.id: asset for asset in assets}
        variants: list[Asset] = []
        for asset in assets:
            if asset.verb != "variant" and "variant" not in asset.tags:
                continue
            pending = list(asset.parent_ids)
            visited: set[str] = set()
            linked = False
            while pending:
                parent_id = pending.pop()
                if parent_id in visited:
                    continue
                if parent_id in linked_ids:
                    linked = True
                    break
                visited.add(parent_id)
                parent = asset_by_id.get(parent_id)
                if parent:
                    pending.extend(parent.parent_ids)
            if linked:
                variants.append(asset)
        return sorted(variants, key=lambda asset: asset.created_at)

    def _visual_assets_for_item(self, item: PipelineItem) -> list[Asset]:
        active_assets = {
            asset.id: asset for asset in self.store.list_assets(include_archived=False)
        }
        linked = {asset.id: asset for asset in self._variant_assets_for_item(item)}
        for asset_id in item.attached_asset_ids:
            asset = active_assets.get(asset_id)
            if asset:
                linked[asset_id] = asset
        return sorted(linked.values(), key=lambda asset: asset.created_at)

    def _render_visual_asset_section(self, item: PipelineItem) -> str:
        assets = self._visual_assets_for_item(item)
        if not assets:
            return ""

        markdown_dir = self.ws_dir / item.full_folder_path
        lines = ["## Visual Assets"]
        for asset in assets:
            original_path = os.path.relpath(self.store.project_dir / asset.file_path, markdown_dir).replace("\\", "/")
            preview_path = os.path.relpath(
                self.store.project_dir / (asset.thumbnail_path or asset.file_path), markdown_dir
            ).replace("\\", "/")
            if asset.verb == "variant" or "variant" in asset.tags:
                label_parts = [" · ".join(asset.chips_used), " ".join(asset.prompt_raw.split())]
                title = " — ".join(part for part in label_parts if part) or asset.name
            else:
                title = asset.name
            title = " ".join(title.split()).replace("[", "(").replace("]", ")")
            kind = getattr(asset.type, "value", str(asset.type))
            metadata = getattr(asset, "params", {}) or {}
            model = (
                getattr(asset, "model", "")
                or metadata.get("model", "")
                or metadata.get("ai_model", "")
                or getattr(asset, "provider", "")
                or asset.verb
                or "Not recorded"
            )
            model = " ".join(str(model).split()).replace("`", "'")
            lines.extend(
                [
                    "",
                    f"### {title}",
                    f"![{title}]({preview_path})",
                    f"- **Type**: `{kind}` · **Model / workflow**: `{model}`",
                    f"- [Open original]({original_path}) · `asset:{asset.id}`",
                ]
            )
        return "\n".join(lines)

    def sync_item_visual_assets(self, item: PipelineItem) -> None:
        """Refresh only the generated asset gallery, preserving human-authored blueprint text."""
        ensure_workspace_folder(self.ws_dir, self.store, item.full_folder_path)
        markdown_path = self.ws_dir / item.full_folder_path / f"{item.slug}.md"
        current = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else self._render_individual_item_md(item)
        lines = current.splitlines()
        section = self._render_visual_asset_section(item)
        start = next((i for i, line in enumerate(lines) if line.strip() in {"## Variants", "## Visual Assets"}), None)

        if start is not None:
            end = next(
                (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ") or lines[i].startswith("<details>")),
                len(lines),
            )
            lines[start:end] = section.splitlines() if section else []
        elif section:
            end = next((i for i, line in enumerate(lines) if line.startswith("<details>")), len(lines))
            lines[end:end] = [*section.splitlines(), ""]

        markdown_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _render_individual_item_md(self, item: PipelineItem) -> str:
        full_folder = item.full_folder_path
        cands = ", ".join(f"`{cid}`" for cid in item.candidate_asset_ids) or "None yet"
        finals = ", ".join(f"`{fid}`" for fid in item.final_asset_ids) or "None yet"
        variants_md = self._render_visual_asset_section(item)
        body = f"""# {item.title} — Design Blueprint

> {item.core_idea or "A clear concept statement has not been written yet."}

## 1. Design & Gameplay Blueprint

This section defines the asset's gameplay purpose, visual hierarchy, world context, and state changes. Edit the corresponding design pillars in the YAML frontmatter to tailor this contract to the project and archetype.

## At a Glance
- **Role**: {item.goal}
- **Category / Folder**: `{item.category}` · `{full_folder}/`
- **Status**: `{item.stage}`
- **In-game destination**: `{item.target_path}`

## Core Idea
{item.core_idea or "Describe the character's central fantasy: who they are and what makes them memorable."}

## Gameplay Identity
### What makes this character unique
{item.gameplay_uniqueness or "Describe the signature mechanic, decision, or trade-off that belongs to this character."}

### Role in play
{item.goal}

## Character & World
### Personality and lore
{item.personality or "Describe their temperament, motivation, and connection to the world."}

### Visual description
{item.description or "Describe the character's materials, colors, equipment, and overall mood."}

## Visual Blueprint
### Signature silhouette and details
{item.visual_signature or "Describe the instantly recognizable shape, focal detail, and read at gameplay distance."}

### Materials & faction
{", ".join(item.material_tags) or "Not defined"}

## Production Handoff
- **Workflow**: `{item.workflow_preset}`
- **2D candidates**: {cands}
- **Selected concept**: `{item.winner_asset_id or "None yet"}`
- **3D / final output**: {finals}

## 2. Modular Anatomy, Rigging & Sub-Parts

Define rig bones, attachment points, destructible chunks, sprite layers, or other modular slots in the `modular_parts` frontmatter array. Folder-level defaults can be extended or overridden for this asset.

## 3. Variant & State Progression

Record tier, faction, biome, damage, or other project-specific states in the `variants` frontmatter array. Each variant inherits the base blueprint and stores only its prompt delta and outputs.

{variants_md}

## 4. Pipeline & Prompt Log

The collapsible log below retains generation prompts and technical history without crowding the active blueprint.

<details>
<summary>Generation prompt and technical metadata</summary>

### Prepared generation prompt
```text
{item.prepared_prompt or "Not prepared yet"}
```

- **ID**: `{item.slug}`
- **Materials & Faction**: {", ".join(item.material_tags)}
</details>
"""
        return serialize_frontmatter(pipeline_item_frontmatter(item, self.store.get_bible()), body)

    def _sync_to_markdown_files(self, items: list[PipelineItem]) -> None:
        """Write `GAME.md`, `ASSET_REQUIREMENTS.md`, AND every asset's individual `workspace/<folder>/<slug>.md` file."""
        bible = self.store.get_bible()
        rules = self.get_style_rules()
        cats = rules.get("categories", DEFAULT_CATEGORY_PROMPTS)
        mats = rules.get("materials", DEFAULT_MATERIAL_TAGS)

        cat_lines = "\n".join(
            f"- **{v.get('label', k)} (`{k}` -> `workspace/{v.get('folder', CATEGORY_TO_FOLDER.get(k, 'characters'))}/`)**: {v.get('prompt_rule', '')}"
            for k, v in cats.items()
        )
        mat_lines = "\n".join(f"- **{k}**: {v}" for k, v in mats.items())

        lore_block = (
            bible.lore.strip()
            or "_Not written yet._"
        )
        extra_sections_md = ""
        for sec in bible.custom_sections or []:
            s_title = str(sec.get("title", "")).strip()
            s_body = str(sec.get("content", "")).strip()
            if s_title and s_body:
                extra_sections_md += f"\n## {s_title}\n{s_body}\n"

        game_md = f"""# Game World & Style Bible: {bible.project_name}

> Synchronized by Forge Studio. Every asset blueprint has its own `.md` file in its category folder.

## Lore
{lore_block}

## 1. Core Vision & World Rules
- **Genre**: {bible.genre}
- **Tone**: {bible.tone}
- **Art Style**: {bible.art_style}
- **Camera Perspective**: {bible.camera_perspective}
- **Target Engine**: {bible.target_engine}
- **Polycount Budget**: {bible.polycount_budget}
- **Unit Scale Meters**: {bible.unit_scale_meters}
- **Lore & Material Keywords**: {", ".join(bible.lore_keywords)}

## 2. Category Style Prompt Rules (Auto-Injected per Asset Type)
{cat_lines}

## 3. Material & Faction Presets
{mat_lines}
{extra_sections_md}"""
        existing_game_meta: dict[str, Any] = {}
        existing_game_body = ""
        if self.game_md_file.exists():
            old_meta, old_body, has_frontmatter = split_frontmatter(
                self.game_md_file.read_text(encoding="utf-8", errors="ignore")
            )
            existing_game_meta = old_meta if has_frontmatter else {}
            existing_game_body = old_body if has_frontmatter else self.game_md_file.read_text(encoding="utf-8", errors="ignore")
        game_meta = {**existing_game_meta, **game_metadata_from_bible(bible)}
        game_meta["global_style_tokens"] = {
            **(existing_game_meta.get("global_style_tokens") or {}),
            **game_metadata_from_bible(bible).get("global_style_tokens", {}),
        }
        # Only the asset pipeline creates GAME.md; empty projects stay empty.
        if self.game_md_file.exists() or items:
            self.game_md_file.write_text(
                serialize_frontmatter(game_meta, existing_game_body or game_md), encoding="utf-8"
            )

        req_lines = [
            "# Asset Requirements & Folder Index",
            "",
            "> Shared between your **External Game Coding Agent** and **Forge Studio**.",
            "> Each asset below links to its dedicated `.md` specification file.",
            "",
            "## Active Asset Queue",
            "",
        ]
        generated_req_ids = {item.slug for item in items}
        if self.reqs_md_file.exists():
            for line in self.reqs_md_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                match = re.match(r"^\s*-\s*\[[ xX~]\]\s*`([^`]+)`", line)
                if match and match.group(1).strip() not in generated_req_ids:
                    req_lines.append(line)

        active_md_paths: set[Path] = set()
        for item in items:
            full_folder = item.full_folder_path
            ensure_workspace_folder(self.ws_dir, self.store, full_folder)
            md_path = self.ws_dir / full_folder / f"{item.slug}.md"
            rendered = self._render_individual_item_md(item)
            generated_metadata, generated_body, _ = split_frontmatter(rendered)
            if md_path.exists():
                existing_metadata, existing_body, has_frontmatter = split_frontmatter(
                    md_path.read_text(encoding="utf-8", errors="ignore")
                )
                if has_frontmatter:
                    generated_metadata = merge_generated_asset_frontmatter(existing_metadata, generated_metadata)
                else:
                    existing_body = md_path.read_text(encoding="utf-8", errors="ignore")
                generated_body = existing_body or generated_body
            md_path.write_text(serialize_frontmatter(generated_metadata, generated_body), encoding="utf-8")
            active_md_paths.add(md_path.resolve())

            box = "x" if item.stage == "approved" else ("~" if item.stage in ("candidates_ready", "winner_picked") else " ")
            final_id = item.final_asset_ids[-1] if item.final_asset_ids else item.winner_asset_id
            meta = f" | spec:`workspace/{full_folder}/{item.slug}.md`"
            if final_id:
                meta += f" asset:{final_id}"
            if item.sha256:
                meta += f" sha256:{item.sha256[:12]}"
            req_lines.append(
                f"- [{box}] `{item.slug}` | **{item.category}** | `{item.target_path}` | {item.description}{meta}"
            )

        if self.reqs_md_file.exists() or items:
            self.reqs_md_file.write_text("\n".join(req_lines) + "\n", encoding="utf-8")

    def get_file_explorer_tree(self) -> dict[str, Any]:
        """Return item entries plus every persisted category folder, including empty folders."""
        items = self.list_pipeline_items()
        folders: dict[str, list[dict[str, Any]]] = {name: [] for name in CATEGORY_FOLDERS}
        nested_folders: dict[str, dict[str, list[dict[str, Any]]]] = {
            name: {} for name in CATEGORY_FOLDERS
        }
        folder_records = self.list_folders()
        folder_nodes: dict[str, dict[str, Any]] = {}
        for record in folder_records:
            path = record["path"]
            folder_nodes[path] = {
                **record,
                "children": [],
                "items": [],
            }
            if record["subfolder"]:
                nested_folders[record["folder"]].setdefault(record["subfolder"], [])

        for it in items:
            f = it.ensure_folder()
            sub = it.subfolder
            full_folder = f"{f}/{sub}" if sub else f
            entry = {
                "id": it.id,
                "slug": it.slug,
                "title": it.title,
                "category": it.category,
                "folder": f,
                "subfolder": sub,
                "full_folder": full_folder,
                "md_path": f"{f}/{sub}/{it.slug}.md" if sub else f"{f}/{it.slug}.md",
                "stage": it.stage,
                "order_index": it.order_index,
                "has_candidates": len(it.candidate_asset_ids) > 0,
                "has_3d": len(it.final_asset_ids) > 0,
            }
            folders.setdefault(f, []).append(entry)
            nested_folders.setdefault(f, {}).setdefault(sub, []).append(entry)
            folder_nodes.setdefault(
                full_folder,
                {
                    "path": full_folder,
                    "name": full_folder.rsplit("/", 1)[-1],
                    "parent": full_folder.rsplit("/", 1)[0] if "/" in full_folder else "",
                    "category": FOLDER_TO_CATEGORY.get(f, "character"),
                    "folder": f,
                    "subfolder": sub,
                    "depth": full_folder.count("/"),
                    "item_count": 0,
                    "is_empty": False,
                    "has_template": False,
                    "children": [],
                    "items": [],
                },
            )["items"].append(entry)

        for node in folder_nodes.values():
            parent = node["parent"]
            if parent in folder_nodes:
                folder_nodes[parent]["children"].append(node)
        folder_tree = sorted(
            (node for node in folder_nodes.values() if not node["parent"]),
            key=lambda node: node["path"],
        )
        for node in folder_nodes.values():
            node["children"].sort(key=lambda child: child["path"])
            node["items"].sort(key=lambda item: item["order_index"])

        return {
            "root_docs": ["GAME.md", "ASSET_REQUIREMENTS.md", "HANDOFFS.md"],
            "folders": folders,
            "nested_folders": nested_folders,
            "folder_paths": folder_records,
            "folder_tree": folder_tree,
        }

    def move_or_reorder_item(
        self,
        item_id: str,
        target_folder: str | None = None,
        direction: str | None = None,  # "up" | "down"
        new_index: int | None = None,
    ) -> list[PipelineItem]:
        """Move an asset `.md` to a different folder (and update its category) or reorder it."""
        items = self.list_pipeline_items()
        idx = next((i for i, x in enumerate(items) if x.id == item_id or x.slug == item_id), -1)
        if idx == -1:
            return items

        item = items[idx]
        old_full = item.full_folder_path
        old_md = self.ws_dir / old_full / f"{item.slug}.md"

        if target_folder:
            normalized = normalize_category_folder_path(target_folder)
            parts = normalized.split("/")
            root_f = parts[0]
            self.create_folder(normalized)
            item.folder = root_f
            item.category = FOLDER_TO_CATEGORY[root_f]
            item.subfolder = "/".join(parts[1:])
            item.prepared_prompt = self.build_prompt_for_item(
                item.title, item.description, item.category, item.material_tags, item.goal,
                item.core_idea, item.gameplay_uniqueness, item.personality, item.visual_signature
            )
            if old_md.exists() and item.full_folder_path != old_full:
                try:
                    old_md.unlink()
                except OSError:
                    pass

        if direction == "up" and idx > 0:
            items[idx - 1], items[idx] = items[idx], items[idx - 1]
        elif direction == "down" and idx < len(items) - 1:
            items[idx + 1], items[idx] = items[idx], items[idx + 1]
        elif new_index is not None and 0 <= new_index < len(items):
            popped = items.pop(idx)
            items.insert(new_index, popped)

        return self.save_pipeline_items(items)

    def delete_blueprint(
        self,
        item_id: str,
        *,
        confirm: bool = False,
        delete_generated_assets: bool = False,
    ) -> dict[str, Any]:
        """Remove a blueprint page. Generated assets stay unless deletion is separately confirmed."""
        if confirm is not True:
            raise ValueError("Blueprint deletion requires explicit confirmation.")
        items = self.list_pipeline_items()
        target = next((item for item in items if item.id == item_id or item.slug == item_id), None)
        if target is None:
            raise KeyError(f"Blueprint '{item_id}' not found.")

        asset_ids = list(dict.fromkeys([
            *target.candidate_asset_ids,
            *target.final_asset_ids,
            *target.attached_asset_ids,
            *([target.winner_asset_id] if target.winner_asset_id else []),
        ]))
        removed_asset_ids: list[str] = []
        preserved_asset_ids: list[str] = []
        for asset_id in asset_ids:
            asset = self.store.get_asset(asset_id)
            if asset is None:
                continue
            if delete_generated_assets and not asset.archived:
                asset.archived = True
                self.store.save_asset(asset)
                removed_asset_ids.append(asset_id)
            elif not asset.archived:
                preserved_asset_ids.append(asset_id)

        markdown_path = self.ws_dir / target.full_folder_path / f"{target.slug}.md"
        if markdown_path.is_file():
            markdown_path.unlink()
        if self.reqs_md_file.is_file():
            kept_lines = []
            for line in self.reqs_md_file.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^\s*-\s*\[[ xX~]\]\s*`([^`]+)`", line)
                if match and match.group(1).strip() == target.slug:
                    continue
                kept_lines.append(line)
            self.reqs_md_file.write_text("\n".join(kept_lines).rstrip() + "\n", encoding="utf-8")
        self.save_pipeline_items([item for item in items if item.id != target.id])
        return {
            "deleted_item_id": target.id,
            "deleted_title": target.title,
            "preserved_asset_ids": preserved_asset_ids,
            "removed_asset_ids": removed_asset_ids,
        }

    def _match_blueprint_items(self, items: list[PipelineItem], query: str) -> list[PipelineItem]:
        needle = query.strip().lower()
        if not needle:
            return []
        exact = [
            item for item in items
            if item.title.lower() == needle or item.slug.lower() == needle or item.id.lower() == needle
        ]
        if exact:
            return exact
        return [item for item in items if needle in item.title.lower() or needle in item.slug.lower()]

    def apply_souly_workspace_command(
        self,
        instruction: str,
        items: list[PipelineItem],
    ) -> tuple[str, list[str], list[PipelineItem]] | None:
        """Handle folder creation and confirmed blueprint deletion without calling a model."""
        text = " ".join(str(instruction or "").split())
        lowered = text.lower()
        if re.search(r"\b(?:create|add|make)\b", lowered) and re.search(r"\bfolders?\b", lowered) and "blueprint" not in lowered:
            return self._souly_create_folder(text, items)
        if re.search(r"\bdelete\b", lowered) and re.search(r"\bblueprint\b", lowered):
            return self._souly_delete_blueprint(text, items)
        return None

    def _souly_create_folder(
        self,
        text: str,
        items: list[PipelineItem],
    ) -> tuple[str, list[str], list[PipelineItem]]:
        cleaned = re.sub(
            r"(?i)^(?:please\s+)?(?:create|add|make)\s+(?:a\s+|an\s+)?(?:nested\s+)?folders?\s+(?:named\s+|called\s+|at\s+|for\s+)?",
            "",
            text,
        ).strip(" .\"'`")
        nested = re.match(r"(?i)(?:in|under|inside)\s+([^\s]+)\s+(?:called|named)\s+(.+)$", cleaned)
        path = f"{nested.group(1)}/{nested.group(2)}" if nested else cleaned.split(" for ")[0].strip()
        try:
            folder = self.create_folder(path, actor="agent")
        except ValueError as exc:
            return (
                f"I couldn't create that folder. {exc} Use a path like characters/bosses/infernal.",
                [f"Folder not created: {exc}"],
                items,
            )
        return (
            f"Created the folder `{folder['path']}/`. You can add blueprints there.",
            [f"Created folder {folder['path']}"],
            items,
        )

    def _souly_delete_blueprint(
        self,
        text: str,
        items: list[PipelineItem],
    ) -> tuple[str, list[str], list[PipelineItem]]:
        confirmed = bool(re.search(r"(?i)\bconfirm\b", text))
        delete_assets = bool(re.search(r"(?i)generated assets|and its assets|and the assets|with its assets|including assets", text))
        target_text = re.split(r"(?i)\bblueprint\b", text, maxsplit=1)[1]
        target_text = re.sub(r"(?i)\s+and\s+.*$", "", target_text)
        target_text = re.sub(r"(?i)^(?:named|called|for|the)\s+", "", target_text.strip()).strip(" .\"'`")
        matches = self._match_blueprint_items(items, target_text)
        if not matches:
            return (f"I couldn't find a blueprint named “{target_text}”.", ["No blueprint deleted"], items)
        if len(matches) > 1:
            names = ", ".join(item.title for item in matches[:6])
            return (f"Several blueprints match “{target_text}”: {names}. Name one exactly.", ["No blueprint deleted"], items)
        target = matches[0]
        if not confirmed:
            asset_clause = " Add “and its generated assets” only if those vault files should be removed too."
            return (
                f"Say `confirm delete blueprint {target.title}` to permanently delete that blueprint. "
                f"Generated images and 3D files stay in the vault.{asset_clause}",
                ["Deletion waiting for confirmation"],
                items,
            )
        result = self.delete_blueprint(target.id, confirm=True, delete_generated_assets=delete_assets)
        remaining = self.list_pipeline_items()
        if delete_assets:
            reply = f"Deleted the {target.title} blueprint and removed {len(result['removed_asset_ids'])} generated asset(s) from the vault."
        else:
            reply = f"Deleted the {target.title} blueprint. Its generated images and 3D files are still in the vault."
        return (reply, [reply], remaining)

    async def steer_world_and_fantasize(
        self,
        instruction: str,
        create_new_items: bool = True,
        attached_images: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Slime Agent Brain with FULL Workspace Read/Write & Multimodal Vision Access.

        - Ingests any images attached in the chat into the project (`mock_reference`).
        - Encodes the attached images + the project's existing mock reference images as base64 vision inputs.
        - Passes full workspace context (`GAME.md`, all folder `.md` items, mock image titles, and recent chat history).
        - Creates/updates any number of character, item, card, environment, or UI `.md` files!
        """
        bible = self.store.get_bible()
        items = self.list_pipeline_items()
        chat = self.get_chat_history()

        # 1. If user attached images in the chat message, ingest them into the project store immediately!
        newly_attached_names: list[str] = []
        vision_images: list[tuple[str, str]] = []

        for att in attached_images or []:
            data_url = str(att.get("data_url", ""))
            fname = str(att.get("name", "chat_image.png"))
            if ";base64," in data_url:
                header, b64_part = data_url.split(";base64,", 1)
                mime = header.replace("data:", "") or "image/png"
                raw_bytes = base64.b64decode(b64_part)
                ast = self.store.ingest_uploaded_file(
                    filename=fname,
                    content=raw_bytes,
                    name=f"Chat Ref: {Path(fname).stem}",
                    tags=["mock_reference", "chat_upload"],
                )
                ast.status = AssetStatus.FAVORITE
                self.store.save_asset(ast)
                newly_attached_names.append(ast.name)
                vision_images.append((mime, b64_part))

        # 2. Also attach the project's existing Mock Reference Screenshots to the vision payload so the Slime Agent can SEE them!
        all_assets = self.store.list_assets(include_archived=False)
        mock_assets = [
            a for a in all_assets if "mock_reference" in (a.tags or []) or a.verb == "upload"
        ]
        for m_ast in mock_assets[:4]:
            if len(vision_images) >= 4:
                break
            abs_p = self.store.resolve_safe_path(m_ast.file_path)
            encoded = _encode_image_for_vision(abs_p)
            if encoded:
                vision_images.append(encoded)

        chat.append(
            {
                "id": new_id("msg"),
                "role": "user",
                "text": instruction,
                "attached_images": newly_attached_names,
                "timestamp": time.time(),
            }
        )

        steps_taken: list[str] = []
        if newly_attached_names:
            steps_taken.append(f"Saved {len(newly_attached_names)} attached image(s) to project mock references")

        local_command = self.apply_souly_workspace_command(instruction, items)
        if local_command is not None:
            reply_text, command_steps, items = local_command
            steps_taken.extend(command_steps)
            agent_msg = {
                "id": new_id("msg"),
                "role": "agent",
                "text": reply_text.replace("Gloop", "Forge Agent").replace("Souly", "Forge Agent"),
                "steps": steps_taken,
                "timestamp": time.time(),
            }
            chat.append(agent_msg)
            self.save_chat_history(chat)
            return {
                "bible": bible.model_dump(),
                "items": [item.model_dump() for item in items],
                "agent_message": agent_msg,
                "chat_history": chat,
            }

        reply_text = ""
        added_titles: list[str] = []
        world_changed = False

        # Build full workspace context for the Slime Agent
        folder_summary = []
        for it in items:
            folder_summary.append(
                f"  - [{it.ensure_folder()}/{it.slug}.md] {it.title} ({it.category}, stage={it.stage}): Goal='{it.goal}' | Desc='{it.description}'"
            )
        mock_summary = [f"  - {m.name} (id={m.id}, file={m.file_path})" for m in mock_assets]
        recent_chat_str = "\n".join(
            f"{m['role'].upper()}: {m['text'][:200]}" for m in chat[-6:-1]
        )

        try:
            sys_prompt = (
                "You are **Forge Agent**, Forge's friendly, sharp creative companion and Lead Game Art Director.\n"
                "You have FULL READ/WRITE ACCESS to the entire workspace (`GAME.md`, `ASSET_REQUIREMENTS.md`, and all `.md` files in `characters/`, `items/`, `cards/`, `environments/`, `ui/`) "
                "AND direct MULTIMODAL VISION access to the user's attached mock reference images!\n"
                "Never say you can't access files or mock images — they are provided right below and in your vision input.\n\n"
                "Respond ONLY with valid JSON matching this schema:\n"
                "{\n"
                '  "reply": "Friendly, clear response as Forge Agent. Confirm the exact changes you made.",\n'
                '  "should_update_world": false,\n'
                '  "lore": null,\n'
                '  "game_md_sections": [\n'
                '    {"title": "Custom Section Name (e.g. Mechanics, Factions)", "content": "Markdown content..."}\n'
                '  ],\n'
                '  "art_style": null,\n'
                '  "genre": null,\n'
                '  "tone": null,\n'
                '  "camera_perspective": null,\n'
                '  "new_assets": [\n'
                '    {\n'
                '      "slug": "unique_snake_case_id",\n'
                '      "title": "Asset Display Title",\n'
                '      "category": "character|item|card_art|ui|environment",\n'
                '      "subfolder": "logical_subfolder_name (e.g. pawns_infantry, knights_cavalry, rooks_heavy, royals_commanders, bishops_casters, weapons, spell_cards, boards_arenas)",\n'
                '      "goal": "1-sentence gameplay role & visual goal for this asset",\n'
                '      "description": "Rich visual description for the image/3D generator"\n'
                "    }\n"
                "  ]\n"
                "}\n"
                "CRITICAL RULES:\n"
                "1. If the user asks to add or update Lore in `GAME.md`, populate the `\"lore\"` field with the full lore text and set `\"should_update_world\": true`.\n"
                "2. If the user asks to add any other section to `GAME.md` (e.g. Mechanics, Factions, Story, Rules), populate `\"game_md_sections\"` and set `\"should_update_world\": true`.\n"
                "3. If the user asks you to look at mock references and create N concepts, inspect the attached vision images + workspace context and populate `new_assets` with ALL requested concepts.\n"
                "4. If the user is just saying 'hi' or asking a question without requesting edits, set `should_update_world: false`, `lore: null`, `game_md_sections: []`, and `new_assets: []`."
            )

            full_context_prompt = (
                f"=== WORKSPACE CONTEXT ({bible.project_name}) ===\n"
                f"Genre: {bible.genre}\n"
                f"Art Style: {bible.art_style}\n"
                f"Camera: {bible.camera_perspective}\n"
                f"Current Lore in GAME.md: {bible.lore[:400]}\n"
                f"Custom Sections in GAME.md: {[s.get('title') for s in (bible.custom_sections or [])]}\n\n"
                f"=== MOCK REFERENCE IMAGES IN WORKSPACE ({len(mock_assets)} attached to your vision) ===\n"
                + ("\n".join(mock_summary) if mock_summary else "  (None uploaded yet)")
                + f"\n\n=== EXISTING .MD ASSET FILES ({len(items)}) ===\n"
                + ("\n".join(folder_summary) if folder_summary else "  (Empty project — 0 assets yet)")
                + f"\n\n=== RECENT CONVERSATION ===\n{recent_chat_str}\n\n"
                f"=== USER MESSAGE ===\n{instruction}"
            )

            res = await call_vscode_copilot_model(
                prompt=full_context_prompt,
                system_prompt=sys_prompt,
                images_b64=vision_images,
            )
            raw = res["text"].strip()
            if "```" in raw:
                for block in raw.split("```"):
                    b_clean = block.lstrip("json").strip()
                    if b_clean.startswith("{"):
                        raw = b_clean
                        break
            start_brace = raw.find("{")
            end_brace = raw.rfind("}")
            if start_brace != -1 and end_brace != -1:
                raw = raw[start_brace : end_brace + 1]

            parsed = json.loads(raw)
            reply_text = str(parsed.get("reply") or f"Updated workspace via {res['model']}.")
            should_update = bool(parsed.get("should_update_world", False))

            # Handle direct Lore & GAME.md section updates (even if the model forgot should_update_world=true!)
            if parsed.get("lore") and str(parsed["lore"]).strip():
                bible.lore = str(parsed["lore"]).strip()
                steps_taken.append("Updated ## Lore section in workspace/GAME.md")
                world_changed = True

            if isinstance(parsed.get("game_md_sections"), list) and parsed["game_md_sections"]:
                existing_secs = {s.get("title", "").lower(): s for s in (bible.custom_sections or [])}
                for sec in parsed["game_md_sections"]:
                    if isinstance(sec, dict) and sec.get("title") and sec.get("content"):
                        stitle = str(sec["title"]).strip()
                        scontent = str(sec["content"]).strip()
                        if stitle.lower() == "lore":
                            bible.lore = scontent
                            steps_taken.append("Updated ## Lore section in workspace/GAME.md")
                        else:
                            existing_secs[stitle.lower()] = {"title": stitle, "content": scontent}
                            steps_taken.append(f"Added/Updated ## {stitle} section in workspace/GAME.md")
                        world_changed = True
                bible.custom_sections = list(existing_secs.values())

            # Safety net: if user asked to add lore/section to game.md and the LLM put a `## Heading` or `Lore\n...` inside `reply_text`, extract and save it!
            inst_low = instruction.lower()
            if not world_changed and any(w in inst_low for w in ("lore", "game.md", "section", "story", "backstory")):
                m_sec = re.search(r"(?:##\s*Lore|^Lore)\s*\n+([\s\S]+)$", reply_text, re.MULTILINE | re.IGNORECASE)
                if m_sec:
                    bible.lore = m_sec.group(1).strip()
                    steps_taken.append("Updated ## Lore section in workspace/GAME.md")
                    world_changed = True

            if should_update:
                if parsed.get("art_style"):
                    bible.art_style = str(parsed["art_style"])
                    steps_taken.append(f"Updated Art Style -> {bible.art_style[:68]}...")
                    world_changed = True
                if parsed.get("genre"):
                    bible.genre = str(parsed["genre"])
                    steps_taken.append(f"Set Genre -> {bible.genre}")
                    world_changed = True
                if parsed.get("tone"):
                    bible.tone = str(parsed["tone"])
                    world_changed = True
                if parsed.get("camera_perspective"):
                    bible.camera_perspective = str(parsed["camera_perspective"])
                    world_changed = True

            if create_new_items and isinstance(parsed.get("new_assets"), list) and parsed["new_assets"]:
                existing_slugs = {i.slug for i in items}
                for na in parsed["new_assets"][:24]:  # Allow up to 24 assets in a single turn!
                    if not isinstance(na, dict):
                        continue
                    slug = re.sub(r"[^a-z0-9_]", "_", str(na.get("slug") or na.get("title", "asset")).lower()).strip("_")[:36]
                    if slug and slug not in existing_slugs:
                        existing_slugs.add(slug)
                        cat = str(na.get("category", "character")).lower()
                        if cat not in DEFAULT_CATEGORY_PROMPTS:
                            cat = "character"
                        folder = CATEGORY_TO_FOLDER.get(cat, "characters")
                        desc = str(na.get("description", ""))
                        goal = str(na.get("goal") or f"Core {cat} asset for {bible.project_name}.")
                        title = str(na.get("title", slug.replace("_", " ").title()))
                        sub_raw = str(na.get("subfolder") or "").strip().lower()
                        try:
                            sub = normalize_nested_folder_path(sub_raw, allow_empty=True) if sub_raw else ""
                        except ValueError:
                            sub = ""
                        sub = sub or infer_logical_subfolder(title, cat, desc)
                        items.append(
                            PipelineItem(
                                slug=slug,
                                title=title,
                                category=cat,
                                folder=folder,
                                subfolder=sub,
                                order_index=len(items),
                                goal=goal,
                                material_tags=["Weathered Iron"],
                                description=desc,
                                target_path=f"res://assets/{folder}/{sub}/{slug}.{'glb' if cat in ('character','item','environment') else 'png'}",
                                prepared_prompt=self.build_prompt_for_item(title, desc, cat, ["Weathered Iron"], goal),
                                workflow_preset=DEFAULT_CATEGORY_PROMPTS[cat]["default_workflow"],
                            )
                        )
                        added_titles.append(f"{folder}/{sub}/{slug}.md")
                        world_changed = True
                if added_titles:
                    steps_taken.append(
                        f"Created {len(added_titles)} .md files: {', '.join(added_titles[:6])}"
                        + (f" (+{len(added_titles) - 6} more)" if len(added_titles) > 6 else "")
                    )

            # Guarantee that onboarding a brand-new empty project with a game idea always creates starter .md files
            if create_new_items and len(items) == 0 and len(instruction.strip()) > 5:
                subject = instruction.strip().rstrip(".")
                for g_title, g_cat, g_goal, g_desc in [
                    (f"{subject[:22]} Hero", "character", "Main playable protagonist", f"Lead character for {subject}"),
                    (f"Signature Relic", "item", "Core interactive weapon or prop", f"Signature item crafted for {subject}"),
                    (f"Core Arena Tile", "environment", "Primary environment piece", f"Environment set piece for {subject}"),
                ]:
                    slug = re.sub(r"[^a-z0-9_]", "_", g_title.lower()).strip("_")[:28]
                    folder = CATEGORY_TO_FOLDER.get(g_cat, "characters")
                    items.append(
                        PipelineItem(
                            slug=slug,
                            title=g_title.title(),
                            category=g_cat,
                            folder=folder,
                            order_index=len(items),
                            goal=g_goal,
                            material_tags=["Weathered Iron"],
                            description=g_desc,
                            target_path=f"res://assets/{folder}/{slug}.glb",
                            prepared_prompt=self.build_prompt_for_item(g_title.title(), g_desc, g_cat, ["Weathered Iron"], g_goal),
                            workflow_preset=DEFAULT_CATEGORY_PROMPTS[g_cat]["default_workflow"],
                        )
                    )
                    added_titles.append(f"{folder}/{slug}.md")
                world_changed = True
                steps_taken.append(f"Created {len(added_titles)} starter .md files: {', '.join(added_titles)}")
        except Exception:
            low = instruction.strip().lower()
            if low in ("hi", "hey", "hello", "yo", "sup", "hola", "hallo", "test", "help", "?") or len(low) <= 3:
                reply_text = (
                    f"Hey! I'm **Forge Agent**, your workspace agent! Right now we have **{len(items)} `.md` asset files** across `characters/`, `items/`, `cards/`, and `environments/`, plus **{len(mock_assets)} mock reference images**.\n\n"
                    f"Drop an image here or tell me what to build or tweak!"
                )
            else:
                world_changed = True
                if "dark" in low or "grim" in low or "gothic" in low:
                    bible.art_style = f"Grimdark gothic {bible.art_style}, heavy chiaroscuro shadows, weathered iron and blood-crimson accents"
                    bible.tone = "Grimdark, ominous, tactical"
                    steps_taken.append("Shifted world tone & lighting to Grimdark Gothic")
                else:
                    bible.art_style = f"Stylized game art for '{instruction[:70]}' — crisp silhouettes, cohesive lighting, readable materials"
                    bible.genre = instruction[:45].strip().title()
                    steps_taken.append(f"Manifested world bible from idea: '{instruction[:50]}'")

                if len(items) == 0 or any(w in low for w in ("add", "create", "make", "need", "game", "about", "think", "figurine")):
                    subject = instruction.strip().rstrip(".")
                    generated_specs = [
                        (f"{subject[:22]} Vanguard", "character", "Frontline playable unit with unmistakable silhouette", f"Main protagonist for {subject}, signature armor and weapon"),
                        (f"{subject[:22]} Bruiser", "character", "Heavy tank archetype controlling space", f"Colossal armored unit matching {subject} world rules"),
                        (f"Signature Relic Blade", "item", "Primary melee weapon pickup", f"Iconic weapon prop crafted in the style of {subject}"),
                        (f"Tactical Spell Card", "card_art", "Key deckbuilder ability card", f"Painterly card illustration capturing the atmosphere of {subject}"),
                    ]
                    existing_slugs = {i.slug for i in items}
                    for g_title, g_cat, g_goal, g_desc in generated_specs:
                        slug = re.sub(r"[^a-z0-9_]", "_", g_title.lower()).strip("_")[:28]
                        if slug not in existing_slugs:
                            folder = CATEGORY_TO_FOLDER.get(g_cat, "characters")
                            items.append(
                                PipelineItem(
                                    slug=slug,
                                    title=g_title.title(),
                                    category=g_cat,
                                    folder=folder,
                                    order_index=len(items),
                                    goal=g_goal,
                                    material_tags=["Weathered Iron"],
                                    description=g_desc,
                                    target_path=f"res://assets/{folder}/{slug}.{'glb' if g_cat in ('character','item') else 'png'}",
                                    prepared_prompt=self.build_prompt_for_item(g_title.title(), g_desc, g_cat, ["Weathered Iron"], g_goal),
                                    workflow_preset=DEFAULT_CATEGORY_PROMPTS[g_cat]["default_workflow"],
                                )
                            )
                            added_titles.append(f"{folder}/{slug}.md")
                    if added_titles:
                        steps_taken.append(f"Created {len(added_titles)} .md files: {', '.join(added_titles)}")

                reply_text = (
                    f"Done! I updated `GAME.md` and organized **{len(items)} `.md` asset files** in your workspace folders."
                )

        if world_changed:
            self.store.save_bible(bible)
            for item in items:
                if item.stage == "planned":
                    item.prepared_prompt = self.build_prompt_for_item(
                        item.title, item.description, item.category, item.material_tags, item.goal,
                        item.core_idea, item.gameplay_uniqueness, item.personality, item.visual_signature
                    )
            steps_taken.append("Synchronized workspace/GAME.md & individual folder .md files")
            self.save_pipeline_items(items)

        agent_msg = {
            "id": new_id("msg"),
            "role": "agent",
            "text": reply_text,
            "steps": steps_taken,
            "timestamp": time.time(),
        }
        chat.append(agent_msg)
        self.save_chat_history(chat)

        return {
            "bible": bible.model_dump(),
            "items": [i.model_dump() for i in items],
            "agent_message": agent_msg,
            "chat_history": chat,
        }

    async def import_and_guess_game_folder(self, folder_path_str: str) -> dict[str, Any]:
        """Briefly inspect an existing game folder (Godot, Unity, Unreal, or generic code repo),
        have the Slime Agent guess what the game is about, populate `GAME.md`, and generate
        individual `.md` files in `characters/`, `items/`, `cards/`, `environments/`!
        """
        folder_path = Path(folder_path_str).resolve()
        if not folder_path.exists() or not folder_path.is_dir():
            raise ValueError(f"Folder not found: {folder_path_str}")

        # 1. Run fast, read-only scan of the game directory
        adapter = detect_engine_adapter(folder_path)
        profile = adapter.scan(folder_path)
        safe_bundle = collect_safe_game_files(folder_path)
        safe_files: list[Path] = safe_bundle["safe_files"]

        # Collect brief snippets from README/GDD/config/scripts + up to 2 sample images
        snippets: list[str] = [
            f"Folder Name: {folder_path.name}",
            f"Detected Engine: {adapter.display_name}",
            f"Title Clue: {profile.game_title.value}",
            f"Camera Clue: {profile.camera_and_perspective.value}",
            f"Extracted Catalog Items: {', '.join(c.name for c in profile.catalog[:10])}",
            f"Missing / Wishlist Refs: {', '.join(w.title for w in profile.wishlist[:10])}",
        ]
        sample_vision: list[tuple[str, str]] = []

        for f in safe_files[:30]:
            ext = f.suffix.lower()
            if ext in (".md", ".txt", ".godot", ".json") and len(snippets) < 14:
                txt = sanitize_untrusted_text(f.read_text(encoding="utf-8", errors="ignore"), max_chars=800)
                snippets.append(f"--- {f.name} ---\n{txt}")
            elif ext in (".png", ".jpg", ".jpeg") and len(sample_vision) < 2:
                enc = _encode_image_for_vision(f)
                if enc:
                    sample_vision.append(enc)
                    # Also ingest sample art as a mock reference
                    self.store.ingest_uploaded_file(
                        filename=f.name,
                        content=f.read_bytes(),
                        name=f"Imported Ref: {f.stem}",
                        tags=["mock_reference", "imported"],
                    )

        rules = self.get_style_rules()
        rules["linked_game_folder"] = str(folder_path)
        self.save_style_rules(rules)

        guess_prompt = (
            "I just imported an existing game project folder. Look at these files, scenes, and sample images, "
            "guess everything this game is about (genre, art style, camera, tone, palette), update the World Bible, "
            "and create 5 to 8 key asset `.md` studies (characters, items, environment, UI) that this game needs:\n\n"
            + "\n".join(snippets)
        )
        return await self.steer_world_and_fantasize(guess_prompt, create_new_items=True)

    async def auto_organize_subfolders(self) -> dict[str, Any]:
        """Group all pipeline items into logical nested subfolders (0 provider credit cost — uses Copilot / heuristics)."""
        items = self.list_pipeline_items()
        if not items:
            return {"organized_count": 0}

        try:
            summary_lines = [
                f"- slug={it.slug} | title={it.title} | cat={it.category} | desc={it.description[:90]}"
                for it in items
            ]
            sys_prompt = (
                "You are Forge Agent, organizing game assets into logical nested subfolders.\n"
                "Respond ONLY with JSON: {\"mapping\": {\"asset_slug\": {\"category\": \"character|item|card_art|environment|ui\", \"subfolder\": \"clean_snake_case_subfolder\"}}}\n"
                "For example, chess pieces should be grouped into `pawns_infantry`, `knights_cavalry`, `rooks_heavy`, `bishops_casters`, `royals_commanders`."
            )
            res = await call_vscode_copilot_model(
                prompt="Organize these assets into logical subfolders:\n" + "\n".join(summary_lines),
                system_prompt=sys_prompt,
            )
            raw = res["text"].strip()
            s_idx, e_idx = raw.find("{"), raw.rfind("}")
            if s_idx != -1 and e_idx != -1:
                mapping = json.loads(raw[s_idx : e_idx + 1]).get("mapping", {})
                for it in items:
                    if it.slug in mapping and isinstance(mapping[it.slug], dict):
                        cat = str(mapping[it.slug].get("category") or it.category).lower()
                        if cat in DEFAULT_CATEGORY_PROMPTS:
                            it.category = cat
                            it.folder = CATEGORY_TO_FOLDER[cat]
                        try:
                            sub = normalize_nested_folder_path(
                                str(mapping[it.slug].get("subfolder", "")).lower(),
                                allow_empty=True,
                            )
                        except ValueError:
                            sub = ""
                        if sub:
                            it.subfolder = sub
        except Exception:
            for it in items:
                it.subfolder = infer_logical_subfolder(it.title, it.category, it.description)

        # Sort items by folder + subfolder so related assets sit together cleanly
        items.sort(key=lambda x: (x.ensure_folder(), x.subfolder, x.order_index))
        self.save_pipeline_items(items)
        return {"organized_count": len(items)}

    async def recover_meshy_cloud_tasks(self) -> dict[str, Any]:
        """Fetch already-completed (`SUCCEEDED`) tasks from Meshy (`text-to-image` & `image-to-3d`) at 0 credit cost
        and download any missing images or 3D GLBs into the project so no past credits are wasted!
        """
        import httpx
        from forge.config import load_config

        cfg = load_config()
        mkey = cfg.get_key("meshy")
        if not mkey:
            return {"recovered_count": 0, "message": "No Meshy API key configured."}

        existing_task_ids = {
            a.metadata.get("meshy_task_id")
            for a in self.store.list_assets(include_archived=True)
            if a.metadata and a.metadata.get("meshy_task_id")
        }
        headers = {"Authorization": f"Bearer {mkey}"}
        recovered: list[dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=40.0) as client:
            # 1. Recover SUCCEEDED text-to-image tasks
            r_img = await client.get("https://api.meshy.ai/openapi/v1/text-to-image?page_size=20", headers=headers)
            if r_img.status_code == 200:
                tasks = r_img.json() if isinstance(r_img.json(), list) else []
                for t in tasks:
                    tid = t.get("id")
                    if not tid or tid in existing_task_ids or t.get("status") != "SUCCEEDED":
                        continue
                    urls = t.get("image_urls") or []
                    if not urls:
                        continue
                    try:
                        dl = await client.get(urls[0])
                        if dl.status_code == 200:
                            ast_id = f"ast_mimg_{tid[-8:]}"
                            rel_f = f"assets/{ast_id}.png"
                            abs_f = self.store.resolve_safe_path(rel_f)
                            abs_f.write_bytes(dl.content)
                            thumb_rel = f"assets/{ast_id}_thumb.png"
                            im = Image.open(abs_f).convert("RGBA")
                            im.thumbnail((256, 256), Image.Resampling.LANCZOS)
                            im.save(self.store.resolve_safe_path(thumb_rel), format="PNG")
                            prompt_str = str(t.get("prompt") or "Recovered Meshy Nano Banana Image")
                            short_title = prompt_str.split("Subject:")[-1].split("—")[0].strip()[:36] or f"Nano Banana ({tid[-4:]})"
                            ast = Asset(
                                id=ast_id,
                                name=short_title,
                                type=AssetType.IMAGE,
                                file_path=rel_f,
                                thumbnail_path=thumb_rel,
                                verb="recovered_meshy",
                                prompt_raw=prompt_str[:200],
                                prompt_full=prompt_str,
                                provider="meshy_image",
                                model=str(t.get("ai_model") or "nano-banana-2"),
                                cost_usd=0.0,
                                cached_hit=True,
                                tags=["recovered", "nano-banana-2"],
                                metadata={"meshy_task_id": tid, "recovered": True},
                            )
                            self.store.save_asset(ast)
                            existing_task_ids.add(tid)
                            recovered.append(ast.model_dump())
                    except Exception:
                        continue

            # 2. Recover SUCCEEDED image-to-3d tasks
            r_3d = await client.get("https://api.meshy.ai/openapi/v1/image-to-3d?page_size=20", headers=headers)
            if r_3d.status_code == 200:
                tasks_3d = r_3d.json() if isinstance(r_3d.json(), list) else []
                for t in tasks_3d:
                    tid = t.get("id")
                    if not tid or tid in existing_task_ids or t.get("status") != "SUCCEEDED":
                        continue
                    glb_url = (t.get("model_urls") or {}).get("glb")
                    if not glb_url:
                        continue
                    try:
                        dl_glb = await client.get(glb_url)
                        if dl_glb.status_code == 200:
                            ast_id = f"ast_m3d_{tid[-8:]}"
                            rel_glb = f"assets/{ast_id}.glb"
                            self.store.resolve_safe_path(rel_glb).write_bytes(dl_glb.content)
                            thumb_rel = ""
                            if t.get("thumbnail_url"):
                                dl_th = await client.get(t["thumbnail_url"])
                                if dl_th.status_code == 200:
                                    thumb_rel = f"assets/{ast_id}_thumb.png"
                                    self.store.resolve_safe_path(thumb_rel).write_bytes(dl_th.content)
                            ast = Asset(
                                id=ast_id,
                                name=f"Recovered 3D Mesh ({tid[-4:]})",
                                type=AssetType.MESH,
                                file_path=rel_glb,
                                thumbnail_path=thumb_rel,
                                verb="make_3d",
                                prompt_raw="Recovered from Meshy Cloud History (0 extra credits)",
                                prompt_full="Recovered from Meshy Cloud History",
                                provider="meshy",
                                model="meshy-7.1",
                                cost_usd=0.0,
                                cached_hit=True,
                                tags=["3d", "recovered", "glb"],
                                metadata={"meshy_task_id": tid, "recovered": True},
                            )
                            self.store.save_asset(ast)
                            existing_task_ids.add(tid)
                            recovered.append(ast.model_dump())
                    except Exception:
                        continue

        # Also link all local Meshy / Nano Banana assets in store back to matching pipeline items
        all_local = self.store.list_assets(include_archived=False)
        live_assets = [a for a in all_local if a.provider in ("meshy", "meshy_image", "gemini", "elevenlabs")]
        items = self.list_pipeline_items()
        relinked_count = 0
        for it in items:
            key_words = [w.lower() for w in re.findall(r"[a-zA-Z]{4,}", it.title)]
            matched_imgs = [
                a.id
                for a in live_assets
                if a.type in (AssetType.IMAGE, AssetType.ICON)
                and any(kw in (a.prompt_full + " " + a.name).lower() for kw in key_words[:2])
            ]
            matched_finals = [
                a.id
                for a in live_assets
                if a.type in (AssetType.MESH, AssetType.PART)
                and any(kw in (a.prompt_full + " " + a.name).lower() for kw in key_words[:2])
            ]
            if matched_imgs and not it.candidate_asset_ids:
                it.candidate_asset_ids = matched_imgs[:4]
                it.winner_asset_id = matched_imgs[0]
                it.stage = "candidates_ready"
                relinked_count += len(it.candidate_asset_ids)
            if matched_finals and not it.final_asset_ids:
                it.final_asset_ids = matched_finals[:6]
                it.stage = "approved"
                relinked_count += len(it.final_asset_ids)

        self.save_pipeline_items(items)
        return {
            "recovered_count": len(recovered) + relinked_count,
            "recovered": recovered,
        }


def seed_uploaded_chess_mocks(store: ProjectStore, upload_paths: list[tuple[str, str, str]]) -> list[Asset]:
    existing_names = {a.name for a in store.list_assets(include_archived=True)}
    seeded: list[Asset] = []
    bible = store.get_bible()

    for src_path_str, title, tag in upload_paths:
        if title in existing_names:
            continue
        src_p = Path(src_path_str)
        if not src_p.exists():
            continue
        ast = store.ingest_uploaded_file(
            filename=src_p.name,
            content=src_p.read_bytes(),
            name=title,
            tags=["mock_reference", tag],
            note="Uploaded Game Concept / Style Reference Anchor",
        )
        ast.status = AssetStatus.FAVORITE
        store.save_asset(ast)
        if ast.id not in bible.reference_anchor_asset_ids:
            bible.reference_anchor_asset_ids.append(ast.id)
        seeded.append(ast)

    if seeded:
        store.save_bible(bible)
    return seeded


def slice_lineup_image_into_assets(
    store: ProjectStore,
    source_asset: Asset,
    slice_names: list[str] | None = None,
) -> list[Asset]:
    names = slice_names or ["Pawn Footman", "Armored Knight", "Rook Stone Golem", "Crimson Queen"]
    src_abs = store.resolve_safe_path(source_asset.file_path)
    if not src_abs.exists():
        raise FileNotFoundError("Source lineup image not found.")

    img = Image.open(src_abs).convert("RGBA")
    w, h = img.size
    n = max(2, len(names))
    slice_w = w // n
    created: list[Asset] = []

    for idx, piece_name in enumerate(names):
        x0 = idx * slice_w
        x1 = w if idx == n - 1 else (idx + 1) * slice_w
        cropped = img.crop((x0, 0, x1, h))
        buf_id = new_id("ast")
        rel_file = f"assets/{buf_id}.png"
        abs_file = store.resolve_safe_path(rel_file)
        cropped.save(abs_file, format="PNG")

        thumb_rel = f"assets/{buf_id}_thumb.png"
        thumb = cropped.copy()
        thumb.thumbnail((256, 256), Image.Resampling.LANCZOS)
        thumb.save(store.resolve_safe_path(thumb_rel), format="PNG")

        piece_ast = Asset(
            id=buf_id,
            name=f"{piece_name} (Sliced from Sheet)",
            type=AssetType.IMAGE,
            file_path=rel_file,
            thumbnail_path=thumb_rel,
            parent_ids=[source_asset.id],
            verb="slice_sheet",
            prompt_raw=f"Sliced piece #{idx + 1} ({piece_name}) from {source_asset.name}",
            prompt_full=f"Isolated character figurine: {piece_name}, matching {source_asset.name} style.",
            provider="local",
            tags=["sliced", "character", piece_name.lower().replace(" ", "_")],
            metadata={"width": cropped.width, "height": cropped.height, "slice_index": idx},
        )
        store.save_asset(piece_ast)
        created.append(piece_ast)

    return created

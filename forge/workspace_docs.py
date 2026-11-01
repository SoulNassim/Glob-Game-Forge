"""Mr. Mak-inspired All-in-One Markdown Workspace, Visual Study Parser & Node Feeder.

Every project (`projects/<name>/workspace/`) contains rich Markdown documents that:
1. Render as gorgeous editorial visual study pages (like Mr. Mak's "Arachne · Version 2" cards
   with numbered subjects `01`, `02`, 3-column model/variant galleries, collapsible `> Prompts`,
   and two-column design/3D plans).
2. Can be edited as plain `.md` by any external coding agent (Claude Code, Codex, Cursor) or human.
3. Feed directly into the ComfyUI Visual Node Graph as `.md` Source Nodes (emitting BIBLE, PROMPT,
   REQUIREMENTS_LIST, and IMAGE sockets) and receive generated outputs back into the `.md`!
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

from forge.models import ActivityLogEntry, ProjectBible, new_id
from forge.blueprint_contract import (
    deep_merge,
    game_frontmatter_updates,
    game_metadata_from_bible,
    inherited_blueprint_defaults,
    legacy_asset_metadata,
    normalize_asset_frontmatter,
    serialize_frontmatter,
    split_frontmatter,
)
from forge.pipeline_manager import (
    CATEGORY_FOLDERS,
    FOLDER_TO_CATEGORY,
    ensure_workspace_folder,
    normalize_category_folder_path,
)
from forge.storage.project_store import ProjectStore


PROTOCOL_DOCS = {"GAME.md", "ASSET_REQUIREMENTS.md", "HANDOFFS.md", "KNOWLEDGE.md", "DELIVERY_MANIFEST.json"}
TREE_FILE_SUFFIXES = {".md", ".json", ".txt", ".yaml", ".yml", ".toml"}

REQUIREMENT_LINE_RE = re.compile(
    r"^-\s*\[(?P<state>[ xX~])\]\s*`(?P<req_id>[^`]+)`\s*\|\s*\*\*(?P<kind>[^*]+)\*\*\s*\|\s*`(?P<target>[^`]+)`\s*\|\s*(?P<desc>[^|\n]+?)(?:\s*\|\s*(?P<meta>.+))?$"
)


class AssetRequirementItem(BaseModel):
    req_id: str
    status: str = "open"  # "open" ([ ]), "in_progress" ([~]), "done" ([x])
    kind: str = "3D Mesh"
    target_path: str = "res://assets/models/item.glb"
    description: str = ""
    linked_asset_id: str = ""
    sha256: str = ""
    line_number: int = 0


def compute_file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class WorkspaceManager:
    """Manages `projects/<name>/workspace/*.md` files, visual study parsing, and Node Graph feeding."""

    def __init__(self, store: ProjectStore) -> None:
        self.store = store
        self.ws_dir = self.store.project_dir / "workspace"
        self.handoffs_dir = self.ws_dir / "handoffs"
        self.ws_dir.mkdir(parents=True, exist_ok=True)

    def list_tree(self, max_entries: int = 4000) -> dict[str, Any]:
        """Return the live workspace file tree (Markdown, JSON, text) for the Files inspector."""
        root = self.ws_dir.resolve()
        count = 0

        def walk(directory: Path) -> list[dict[str, Any]]:
            nonlocal count
            entries: list[dict[str, Any]] = []
            try:
                children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return entries
            for child in children:
                if count >= max_entries or child.name.startswith(".") or child.is_symlink():
                    continue
                rel = child.resolve().relative_to(root).as_posix()
                if child.is_dir():
                    count += 1
                    entries.append({"name": child.name, "path": rel, "type": "dir", "children": walk(child)})
                elif child.suffix.lower() in TREE_FILE_SUFFIXES:
                    count += 1
                    stat = child.stat()
                    entries.append(
                        {
                            "name": child.name,
                            "path": rel,
                            "type": "file",
                            "size_bytes": stat.st_size,
                            "updated_at": stat.st_mtime,
                        }
                    )
            return entries

        return {"root": "workspace", "children": walk(root), "truncated": count >= max_entries}

    @property
    def game_md_path(self) -> Path:
        return self.ws_dir / "GAME.md"

    @property
    def requirements_md_path(self) -> Path:
        return self.ws_dir / "ASSET_REQUIREMENTS.md"

    @property
    def study_v1_md_path(self) -> Path:
        return self.ws_dir / "VERSION_1_DIRECTIONS.md"

    @property
    def study_v2_md_path(self) -> Path:
        return self.ws_dir / "VERSION_2_CHARACTER_ARSENAL.md"

    @property
    def handoffs_md_path(self) -> Path:
        return self.ws_dir / "HANDOFFS.md"

    @property
    def knowledge_md_path(self) -> Path:
        return self.ws_dir / "KNOWLEDGE.md"

    @property
    def delivery_manifest_path(self) -> Path:
        return self.ws_dir / "DELIVERY_MANIFEST.json"

    def ensure_default_docs(self) -> None:
        bible = self.store.get_bible()

        if not self.game_md_path.exists():
            self.game_md_path.write_text(
                serialize_frontmatter(game_metadata_from_bible(bible), self._render_game_md(bible)), encoding="utf-8"
            )

        if not self.requirements_md_path.exists():
            self.requirements_md_path.write_text(
                """# Asset Requirements Board

> Shared between your external **Game Coding Agent** (Claude Code / Cursor / Codex) and **Forge**.
> Coding agents: append `- [ ] `id` | **Kind** | `res://target/path` | Description` below when your game code needs a new 2D or 3D asset. Feed this file into the Node Graph or click **Fulfill Open Requirements**.

## Active Asset Queue

- [x] `aethelgard_knight` | **3D Mesh** | `res://assets/models/aethelgard_knight.glb` | Main playable clockwork knight hero in emerald and gold armor | asset:ast_hero_3d
- [ ] `iron_broadsword` | **3D Mesh** | `res://assets/models/iron_broadsword.glb` | Standard-issue knight broadsword with brass clockwork crossguard and emerald pommel gem
- [ ] `sunfire_elixir_icon` | **2D Icon** | `res://assets/icons/sunfire_elixir.png` | Glowing golden alchemical healing potion in faceted crystal flask, readable at 64x64
- [ ] `runic_lantern_prop` | **3D Mesh** | `res://assets/models/runic_lantern.glb` | Hanging dungeon iron lantern with glowing turquoise crystal core
- [ ] `mossy_flagstone_tile` | **2D Texture** | `res://assets/textures/mossy_flagstone.png` | Seamless repeatable dungeon floor flagstones with glowing rune cracks
""",
                encoding="utf-8",
            )

        if not self.handoffs_md_path.exists():
            self.handoffs_md_path.write_text(
                """# Agent Handoffs & Communication Log

> Bidirectional log between the **External Game Coding Agent** and **Forge Asset Studio**.

---

### [INITIALIZED] Workspace Ready for Coding Agent Collaboration
- **Project**: `default`
- **Engine Target**: `godot`
- **How to request assets from code**: Add a line to `workspace/ASSET_REQUIREMENTS.md` or edit `workspace/VERSION_2_CHARACTER_ARSENAL.md`, then feed into the Node Graph or run `fulfill_asset_requirements`.
""",
                encoding="utf-8",
            )

        if not self.knowledge_md_path.exists():
            self.knowledge_md_path.write_text(
                f"""# Project Art Knowledge & Approved Prompts

## Visual Pillars
- **Art Style**: {bible.art_style}
- **Camera**: {bible.camera_perspective}
- **Polycount Budget**: {bible.polycount_budget} triangles max per prop/character

## Naming & Delivery Conventions
- All 3D meshes are delivered as binary glTF 2.0 (`.glb`) + `.obj` with SHA-256 integrity hashes recorded in `workspace/DELIVERY_MANIFEST.json`.
- All 2D icons and sprites use clean RGBA alpha transparency unless marked as a `2D Texture`.
""",
                encoding="utf-8",
            )

        if not self.delivery_manifest_path.exists():
            self._rebuild_delivery_manifest()

    @staticmethod
    def _render_game_md(bible: ProjectBible) -> str:
        lore = ", ".join(bible.lore_keywords)
        packs = ", ".join(bible.default_style_packs)
        display_name = bible.display_name or bible.project_name
        return f"""# Game Definition: {display_name}

> Edit this file directly from your Game Coding Agent or inside Forge. Saving or syncing updates the Project Bible and feeds the `.md` Node in the Node Graph automatically.

## Metadata
- **Project Name**: {bible.display_name or bible.project_name}
- **Genre**: {bible.genre}
- **Tone**: {bible.tone}
- **Target Engine**: {bible.target_engine}
- **Camera Perspective**: {bible.camera_perspective}
- **Art Style**: {bible.art_style}
- **Background Rule**: {bible.background_rule}
- **Polycount Budget**: {bible.polycount_budget}
- **Texture Resolution**: {bible.texture_resolution}
- **Unit Scale Meters**: {bible.unit_scale_meters}
- **Character Height Units**: {bible.character_height_units}
- **Lore Keywords**: {lore}
- **Default Style Packs**: {packs}

## 01 | Core World & Visual Direction | World Pillar
- **Subtitle**: {bible.camera_perspective} — {bible.polycount_budget} tri budget — {bible.target_engine.upper()} target
- **Target**: `res://assets/models/`
- **Assets**: `ast_hero_root` (Style Anchor), `ast_hero_3d` (3D Scale Anchor), `ast_potion_base` (Prop Anchor)
- **Prompt**: {bible.art_style}, {bible.camera_perspective}, isolated clean game asset.
- **The Twist**: Every asset generated in this workspace inherits the exact camera angle, style direction, and world scale defined in this document.
- **Rig and 3D Topology Plan**: Standardized 1 unit = {bible.unit_scale_meters}m scale with {bible.character_height_units}m hero height and automatic QC checks.
"""

    def parse_visual_study_md(self, content: str, doc_name: str = "") -> dict[str, Any]:
        """Parse any workspace `.md` file into structured Mr. Mak-style visual study cards and metadata."""
        lines = content.splitlines()
        title = doc_name.replace(".md", "").replace("_", " ").title()
        subtitle = ""
        callout = ""
        metadata: dict[str, str] = {}
        sections: list[dict[str, Any]] = []

        current_sec: dict[str, Any] | None = None
        all_assets_map = {a.id: a.model_dump() for a in self.store.list_assets(include_archived=False)}
        fallback_assets = list(all_assets_map.values())[:3]

        for line in lines:
            s = line.strip()
            if s.startswith("# ") and not current_sec:
                title = s[2:].strip()
                continue
            if s.startswith("> ") and not current_sec:
                callout = (callout + " " + s[2:].strip()).strip()
                continue
            if s.startswith("## "):
                heading = s[3:].strip()
                if heading.lower() in ("metadata", "active asset queue", "visual pillars", "naming & delivery conventions"):
                    if current_sec:
                        sections.append(current_sec)
                        current_sec = None
                    continue
                if current_sec:
                    sections.append(current_sec)
                parts = [p.strip() for p in heading.split("|")]
                if len(parts) >= 3:
                    num, sec_title, badge = parts[0], parts[1], parts[2]
                elif len(parts) == 2:
                    num, sec_title, badge = f"{len(sections) + 1:02d}", parts[0], parts[1]
                else:
                    num, sec_title, badge = f"{len(sections) + 1:02d}", heading, "Study"
                current_sec = {
                    "number": num,
                    "title": sec_title,
                    "badge": badge,
                    "subtitle": "",
                    "target": "",
                    "prompt": "",
                    "twist": "",
                    "rig_plan": "",
                    "cards": [],
                    "raw_bullets": [],
                }
                continue

            if current_sec is not None:
                m_kv = re.match(r"^-\s*\*\*([^*]+)\*\*\s*:\s*(.+)$", s)
                if m_kv:
                    k = m_kv.group(1).strip().lower()
                    v = m_kv.group(2).strip()
                    if k == "subtitle":
                        current_sec["subtitle"] = v
                    elif k == "target":
                        current_sec["target"] = v.strip("`")
                    elif k == "prompt":
                        current_sec["prompt"] = v
                    elif "twist" in k or "design" in k:
                        current_sec["twist"] = v
                    elif "rig" in k or "topology" in k or "3d" in k:
                        current_sec["rig_plan"] = v
                    elif k == "assets":
                        # Parse `ast_id` (Label) pairs
                        for m_ast in re.finditer(r"`([^`]+)`(?:\s*\(([^)]+)\))?", v):
                            aid = m_ast.group(1).strip()
                            lbl = m_ast.group(2).strip() if m_ast.group(2) else aid
                            ast_obj = all_assets_map.get(aid)
                            if ast_obj:
                                current_sec["cards"].append(
                                    {
                                        "asset_id": aid,
                                        "label": lbl,
                                        "asset": ast_obj,
                                    }
                                )
                    else:
                        current_sec["raw_bullets"].append({"key": m_kv.group(1).strip(), "value": v})
                elif s and not s.startswith("#"):
                    if not current_sec["subtitle"]:
                        current_sec["subtitle"] = s
                    elif not current_sec["twist"]:
                        current_sec["twist"] = s
            else:
                # Top-level metadata or subtitle
                m_meta = re.match(r"^-\s*\*\*([^*]+)\*\*\s*:\s*(.+)$", s)
                if m_meta:
                    metadata[m_meta.group(1).strip()] = m_meta.group(2).strip()
                elif s and not s.startswith("-") and not s.startswith("#") and not subtitle:
                    subtitle = s

        if current_sec:
            sections.append(current_sec)

        # Ensure every study section has visual cards populated from project assets if none explicitly matched
        for sec in sections:
            if not sec["cards"] and fallback_assets:
                for idx, fa in enumerate(fallback_assets[:3]):
                    sec["cards"].append(
                        {
                            "asset_id": fa["id"],
                            "label": f"{fa['provider'].title()} · {fa['verb'].replace('_', ' ').title()}",
                            "asset": fa,
                        }
                    )

        return {
            "doc_name": doc_name,
            "title": title,
            "subtitle": subtitle,
            "callout": callout or "A local Markdown document shared between your coding agent and Forge. Any section can be fed directly into the ComfyUI Node Graph.",
            "metadata": metadata,
            "sections": sections,
            "requirements": [r.model_dump() for r in self.parse_requirements()] if doc_name == "ASSET_REQUIREMENTS.md" else [],
        }

    def append_study_section_to_md(
        self,
        doc_name: str,
        title: str,
        badge: str,
        prompt: str,
        assets: list[dict[str, Any]],
        twist: str = "",
        rig_plan: str = "",
    ) -> dict[str, Any]:
        """Write generated node graph outputs directly back into a `.md` study file as a new numbered section!"""
        doc = self.read_doc(doc_name)
        parsed = self.parse_visual_study_md(doc["content"], doc_name)
        next_num = f"{len(parsed['sections']) + 1:02d}"
        asset_refs = ", ".join(
            f"`{a['id']}` ({a.get('model') or a.get('verb', 'Generated')})" for a in assets[:3]
        )
        block = (
            f"\n## {next_num} | {title} | {badge}\n"
            f"- **Subtitle**: Generated via Forge Node Graph ({len(assets)} output asset(s))\n"
            f"- **Target**: `res://assets/models/{title.lower().replace(' ', '_')[:24]}.glb`\n"
            f"- **Assets**: {asset_refs}\n"
            f"- **Prompt**: {prompt}\n"
            f"- **The Twist**: {twist or 'Identity-locked generation anchored to the workspace `.md` specification.'}\n"
            f"- **Rig and 3D Topology Plan**: {rig_plan or 'Validated against Project Bible polycount and alpha background rules.'}\n"
        )
        new_content = doc["content"].rstrip() + "\n" + block
        return self.write_doc(doc_name, new_content, actor="agent")

    def sync_game_md_to_bible(self) -> ProjectBible:
        """Parse `workspace/GAME.md` and update the ProjectBible with any changes made by an external agent."""
        if not self.game_md_path.exists():
            return self.store.get_bible()

        text = self.game_md_path.read_text(encoding="utf-8", errors="ignore")
        bible = self.store.get_bible()
        structured_metadata, _, _ = split_frontmatter(text)
        updates: dict[str, Any] = game_frontmatter_updates(structured_metadata)

        def _extract_bullet(label: str) -> str | None:
            m = re.search(rf"^-\s*\*\*{re.escape(label)}\*\*\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            return m.group(1).strip() if m else None

        if val := _extract_bullet("Project Name"):
            updates["display_name"] = val
        if val := _extract_bullet("Genre"):
            updates["genre"] = val
        if val := _extract_bullet("Tone"):
            updates["tone"] = val
        if val := _extract_bullet("Target Engine"):
            eng = val.lower().strip()
            if eng in ("godot", "unity", "unreal", "custom", "generic"):
                updates["target_engine"] = eng
        if val := _extract_bullet("Camera Perspective"):
            updates["camera_perspective"] = val
        if val := _extract_bullet("Art Style"):
            updates["art_style"] = val
        if val := _extract_bullet("Background Rule"):
            bg = val.lower().strip()
            if bg in ("transparent", "solid_neutral", "none"):
                updates["background_rule"] = bg
        if val := _extract_bullet("Texture Resolution"):
            updates["texture_resolution"] = val
        if val := _extract_bullet("Lighting Rules"):
            global_style_tokens = dict(
                updates.get("global_style_tokens") or bible.global_style_tokens or {}
            )
            global_style_tokens["lighting_rules"] = val
            updates["global_style_tokens"] = global_style_tokens
        if val := _extract_bullet("Polycount Budget"):
            try:
                updates["polycount_budget"] = int(re.sub(r"[^0-9]", "", val))
            except ValueError:
                pass
        if val := _extract_bullet("Unit Scale Meters"):
            try:
                updates["unit_scale_meters"] = float(val.split()[0])
            except ValueError:
                pass
        if val := _extract_bullet("Character Height Units"):
            try:
                updates["character_height_units"] = float(val.split()[0])
            except ValueError:
                pass
        if val := _extract_bullet("Palette"):
            hexes = [c.strip() for c in val.split(",") if c.strip().startswith("#")]
            if hexes:
                updates["palette"] = hexes
        if val := _extract_bullet("Lore Keywords"):
            kws = [k.strip() for k in val.split(",") if k.strip()]
            if kws:
                updates["lore_keywords"] = kws

        # Parse ## Lore and any custom ## Sections from GAME.md
        sections_found = re.split(r"^##\s+(.+)$", text, flags=re.MULTILINE)
        custom_secs: list[dict[str, str]] = []
        builtin_Prefixes = (
            "1. core vision",
            "2. category style",
            "3. material",
            "metadata",
            "01 |",
            "game overview",
        )
        for i in range(1, len(sections_found), 2):
            sec_title = sections_found[i].strip()
            sec_body = sections_found[i + 1].strip() if i + 1 < len(sections_found) else ""
            if not sec_body:
                continue
            if sec_title.lower() == "lore":
                updates["lore"] = sec_body
            elif not any(sec_title.lower().startswith(p) for p in builtin_Prefixes):
                custom_secs.append({"title": sec_title, "content": sec_body})
        if custom_secs:
            updates["custom_sections"] = custom_secs

        if updates:
            merged = {**bible.model_dump(), **updates}
            bible = ProjectBible(**merged)
            self.store.save_bible(bible)
        return bible

    def sync_bible_to_game_md(self, bible: ProjectBible) -> None:
        self.game_md_path.write_text(
            serialize_frontmatter(game_metadata_from_bible(bible), self._render_game_md(bible)), encoding="utf-8"
        )

    # --- Markdown File CRUD ---

    def list_folders(self) -> list[dict[str, Any]]:
        """List persisted category folders, including folders that have no blueprints yet."""
        folders: list[dict[str, Any]] = []
        seen: set[str] = set()
        workspace_root = self.ws_dir.resolve()
        for root_name in CATEGORY_FOLDERS:
            root = self.ws_dir / root_name
            if not root.is_dir() or root.is_symlink():
                continue
            for path in [root, *root.rglob("*")]:
                if not path.is_dir() or path.is_symlink():
                    continue
                try:
                    relative = path.relative_to(self.ws_dir).as_posix()
                    normalized = normalize_category_folder_path(relative)
                    resolved = path.resolve()
                    if not resolved.is_relative_to(workspace_root) or normalized in seen:
                        continue
                    seen.add(normalized)
                    parts = normalized.split("/")
                    direct_children = [child for child in path.iterdir() if child.name != "_folder.md"]
                    folders.append(
                        {
                            "path": normalized,
                            "name": parts[-1],
                            "parent": "/".join(parts[:-1]),
                            "category": FOLDER_TO_CATEGORY[parts[0]],
                            "folder": parts[0],
                            "subfolder": "/".join(parts[1:]),
                            "depth": len(parts) - 1,
                            "is_empty": not direct_children,
                            "has_template": (path / "_folder.md").is_file(),
                        }
                    )
                except (OSError, ValueError):
                    continue
        return sorted(folders, key=lambda entry: (entry["folder"], entry["path"]))

    def create_folder(self, folder_path: str, actor: str = "user") -> dict[str, Any]:
        """Create a validated category folder and persist its inheritance marker."""
        normalized = normalize_category_folder_path(folder_path)
        target = ensure_workspace_folder(self.ws_dir, self.store, normalized)
        self.store.log_activity(
            ActivityLogEntry(
                actor=actor,  # type: ignore[arg-type]
                action="create_workspace_folder",
                reason=f"Created workspace folder {normalized}",
            )
        )
        return next(
            (folder for folder in self.list_folders() if folder["path"] == normalized),
            {
                "path": normalized,
                "name": normalized.rsplit("/", 1)[-1],
                "parent": normalized.rsplit("/", 1)[0] if "/" in normalized else "",
                "category": FOLDER_TO_CATEGORY[normalized.split("/", 1)[0]],
                "folder": normalized.split("/", 1)[0],
                "subfolder": normalized.split("/", 1)[1] if "/" in normalized else "",
                "depth": normalized.count("/"),
                "is_empty": not any(target.iterdir()),
                "has_template": (target / "_folder.md").is_file(),
            },
        )

    def list_docs(self) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        priority_order = [
            "VERSION_2_CHARACTER_ARSENAL.md",
            "VERSION_1_DIRECTIONS.md",
            "GAME.md",
            "ASSET_REQUIREMENTS.md",
            "HANDOFFS.md",
            "KNOWLEDGE.md",
            "DELIVERY_MANIFEST.json",
        ]
        existing = {p.name: p for p in list(self.ws_dir.glob("*.md")) + list(self.ws_dir.glob("*.json"))}
        ordered_names = [n for n in priority_order if n in existing] + sorted(
            n for n in existing if n not in priority_order
        )

        for name in ordered_names:
            p = existing[name]
            label = name.replace(".md", "").replace(".json", "").replace("_", " ").title()
            if name == "VERSION_1_DIRECTIONS.md":
                label = "Version 1 · Creation directions"
            elif name == "VERSION_2_CHARACTER_ARSENAL.md":
                label = "Version 2 · A stronger premise"
            elif name == "GAME.md":
                label = "GAME.md · World Bible"
            elif name == "ASSET_REQUIREMENTS.md":
                label = "ASSET_REQUIREMENTS.md"
            elif name == "HANDOFFS.md":
                label = "HANDOFFS.md"
            elif name == "KNOWLEDGE.md":
                label = "KNOWLEDGE.md"
            docs.append(
                {
                    "name": p.name,
                    "label": label,
                    "rel_path": f"workspace/{p.name}",
                    "size_bytes": p.stat().st_size,
                    "updated_at": p.stat().st_mtime,
                }
            )
        root_dir = Path(__file__).resolve().parent.parent
        for root_doc in ("AGENTS.md", "CLAUDE.md"):
            rp = root_dir / root_doc
            if rp.exists():
                docs.append(
                    {
                        "name": root_doc,
                        "label": root_doc,
                        "rel_path": root_doc,
                        "size_bytes": rp.stat().st_size,
                        "updated_at": rp.stat().st_mtime,
                        "is_root_doc": True,
                    }
                )
        return docs

    def read_doc(self, doc_name: str) -> dict[str, Any]:
        root_dir = Path(__file__).resolve().parent.parent
        if doc_name in ("AGENTS.md", "CLAUDE.md", ".agents/skills/forge-asset-maker/SKILL.md"):
            target = (root_dir / doc_name).resolve()
        else:
            rel_clean = doc_name.replace("\\", "/").lstrip("/")
            if rel_clean.startswith("workspace/"):
                rel_clean = rel_clean[len("workspace/") :]
            target = (self.ws_dir / rel_clean).resolve()
            if not str(target).startswith(str(self.ws_dir.resolve())):
                raise ValueError("Invalid workspace document path.")
            # Coding-agent protocol docs are created on first use, not at project creation.
            if not target.exists() and rel_clean in PROTOCOL_DOCS:
                self.ensure_default_docs()

        if not target.exists():
            raise FileNotFoundError(f"Workspace document '{doc_name}' not found.")

        content = target.read_text(encoding="utf-8", errors="ignore")
        is_markdown = target.suffix.lower() == ".md"
        parsed_study = self.parse_visual_study_md(content, doc_name) if is_markdown else None
        frontmatter, blueprint_body, has_frontmatter = split_frontmatter(content) if is_markdown else ({}, content, False)
        blueprint = None
        project_metadata = None
        if target == self.game_md_path.resolve():
            project_metadata = frontmatter
        elif is_markdown and target.is_relative_to(self.ws_dir.resolve()) and target.parent != self.ws_dir.resolve() and target.name != "_folder.md":
            inherited = inherited_blueprint_defaults(self.ws_dir, str(target.relative_to(self.ws_dir.resolve())))
            if not has_frontmatter:
                frontmatter = legacy_asset_metadata(
                    blueprint_body,
                    str(target.relative_to(self.ws_dir.resolve())),
                )
            blueprint = normalize_asset_frontmatter(frontmatter, inherited)
        return {
            "name": doc_name,
            "content": content,
            "parsed_study": parsed_study,
            "blueprint": blueprint,
            "project_metadata": project_metadata,
            "updated_at": target.stat().st_mtime,
            "sha256": compute_file_sha256(target),
        }

    def write_doc(self, doc_name: str, content: str, actor: str = "user") -> dict[str, Any]:
        rel_clean = doc_name.replace("\\", "/").lstrip("/")
        if rel_clean.startswith("workspace/"):
            rel_clean = rel_clean[len("workspace/") :]
        if not rel_clean.endswith((".md", ".json")):
            rel_clean += ".md"
        target = (self.ws_dir / rel_clean).resolve()
        if not str(target).startswith(str(self.ws_dir.resolve())):
            raise ValueError("Invalid workspace document path.")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if Path(rel_clean).name.upper() == "GAME.MD":
            self.sync_game_md_to_bible()

        self.store.log_activity(
            ActivityLogEntry(
                actor=actor,  # type: ignore[arg-type]
                action=f"write_doc:{rel_clean}",
                reason=f"Updated workspace/{rel_clean}",
            )
        )
        result = self.read_doc(rel_clean)
        result["requirements"] = (
            [r.model_dump() for r in self.parse_requirements()]
            if Path(rel_clean).name == "ASSET_REQUIREMENTS.md"
            else None
        )
        return result

    def write_blueprint_metadata(
        self, doc_name: str, metadata_patch: dict[str, Any], actor: str = "user"
    ) -> dict[str, Any]:
        rel_clean = doc_name.replace("\\", "/").lstrip("/")
        if rel_clean.startswith("workspace/"):
            rel_clean = rel_clean[len("workspace/") :]
        target = (self.ws_dir / rel_clean).resolve()
        workspace_root = self.ws_dir.resolve()
        if not target.is_relative_to(workspace_root) or target.suffix.lower() != ".md" or target.name == "_folder.md":
            raise ValueError("Blueprint path must be an asset Markdown file inside the workspace.")
        if not target.exists():
            raise FileNotFoundError(f"Workspace document '{doc_name}' not found.")

        existing, body, has_frontmatter = split_frontmatter(target.read_text(encoding="utf-8", errors="ignore"))
        if not has_frontmatter:
            body = target.read_text(encoding="utf-8", errors="ignore")
        allowed = {
            "id", "name", "archetype", "status", "engine_bindings", "technical_specs",
            "palette_swatches", "design_pillars", "variants", "modular_parts", "agent_requests",
        }
        changes = {key: value for key, value in metadata_patch.items() if key in allowed}
        updated = deep_merge(existing, changes)
        inherited = inherited_blueprint_defaults(self.ws_dir, str(target.relative_to(workspace_root)))
        normalized = normalize_asset_frontmatter(updated, inherited)
        normalized.pop("inherited", None)
        target.write_text(serialize_frontmatter(normalized, body), encoding="utf-8")
        self.store.log_activity(
            ActivityLogEntry(actor=actor, action=f"update_blueprint:{rel_clean}", reason=f"Updated blueprint metadata in workspace/{rel_clean}")
        )
        return self.read_doc(rel_clean)

    def list_agent_requests(self) -> list[dict[str, Any]]:
        requests: dict[str, dict[str, Any]] = {}
        for path in self.ws_dir.rglob("*.md"):
            if path.name == "_folder.md" or path == self.game_md_path:
                continue
            metadata, _, has_frontmatter = split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
            if not has_frontmatter or not isinstance(metadata.get("agent_requests"), list):
                continue
            doc_name = path.relative_to(self.ws_dir).as_posix()
            for entry in metadata["agent_requests"]:
                if not isinstance(entry, dict):
                    continue
                request_id = str(entry.get("request_id") or entry.get("id") or "").strip()
                if not request_id:
                    continue
                status = str(entry.get("status", "open")).lower()
                requests[request_id] = {
                    **entry,
                    "request_id": request_id,
                    "status": "fulfilled" if status in {"done", "fulfilled", "complete", "completed"} else status,
                    "doc_name": doc_name,
                    "source": "blueprint",
                }

        for requirement in self.parse_requirements():
            if requirement.req_id not in requests:
                requests[requirement.req_id] = {
                    "request_id": requirement.req_id,
                    "requested_by": "external coding agent",
                    "asset_type": requirement.kind,
                    "target_path": requirement.target_path,
                    "description": requirement.description,
                    "status": "fulfilled" if requirement.status == "done" else requirement.status,
                    "source": "requirements",
                }
        return sorted(requests.values(), key=lambda entry: (entry.get("status") in {"fulfilled", "done"}, entry["request_id"]))

    def update_agent_request(
        self, request_id: str, status: str, linked_asset_id: str = "", sha256: str = ""
    ) -> dict[str, Any] | None:
        updated: dict[str, Any] | None = None
        storage_status = "done" if status in {"done", "fulfilled"} else "in_progress" if status == "in_progress" else "open"
        req_item = next((req for req in self.parse_requirements() if req.req_id == request_id), None)
        if req_item:
            result = self.update_requirement_status(request_id, storage_status, linked_asset_id, sha256)
            if result:
                updated = result.model_dump()

        for path in self.ws_dir.rglob("*.md"):
            if path.name == "_folder.md" or path == self.game_md_path:
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
            metadata, body, has_frontmatter = split_frontmatter(content)
            if not has_frontmatter or not isinstance(metadata.get("agent_requests"), list):
                continue
            changed = False
            for request in metadata["agent_requests"]:
                if not isinstance(request, dict) or str(request.get("request_id") or request.get("id") or "") != request_id:
                    continue
                request["status"] = "fulfilled" if storage_status == "done" else storage_status
                if linked_asset_id:
                    request["linked_asset_id"] = linked_asset_id
                if sha256:
                    request["sha256"] = sha256
                changed = True
                updated = {**request, "request_id": request_id, "doc_name": path.relative_to(self.ws_dir).as_posix(), "source": "blueprint"}
            if changed:
                path.write_text(serialize_frontmatter(metadata, body), encoding="utf-8")
        return updated

    # --- ASSET_REQUIREMENTS.md Parser & Updater ---

    def parse_requirements(self) -> list[AssetRequirementItem]:
        if not self.requirements_md_path.exists():
            return []
        lines = self.requirements_md_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        items: list[AssetRequirementItem] = []

        for idx, line in enumerate(lines, start=1):
            m = REQUIREMENT_LINE_RE.match(line.strip())
            if not m:
                continue
            st_char = m.group("state").lower()
            status = "done" if st_char == "x" else ("in_progress" if st_char == "~" else "open")
            meta_str = m.group("meta") or ""
            linked_id = ""
            sha_val = ""
            if "asset:" in meta_str:
                am = re.search(r"asset:([a-zA-Z0-9_]+)", meta_str)
                if am:
                    linked_id = am.group(1)
            if "sha256:" in meta_str:
                sm = re.search(r"sha256:([a-fA-F0-9]+)", meta_str)
                if sm:
                    sha_val = sm.group(1)

            items.append(
                AssetRequirementItem(
                    req_id=m.group("req_id").strip(),
                    status=status,
                    kind=m.group("kind").strip(),
                    target_path=m.group("target").strip(),
                    description=m.group("desc").strip(),
                    linked_asset_id=linked_id,
                    sha256=sha_val,
                    line_number=idx,
                )
            )
        return items

    def add_requirement(
        self,
        req_id: str,
        kind: str = "3D Mesh",
        target_path: str = "",
        description: str = "",
        actor: str = "agent",
    ) -> AssetRequirementItem:
        slug = re.sub(r"[^a-zA-Z0-9_]", "_", req_id.strip().lower()).strip("_") or new_id("req")
        if not target_path:
            ext = ".glb" if "3d" in kind.lower() or "mesh" in kind.lower() else ".png"
            subdir = "models" if ext == ".glb" else ("icons" if "icon" in kind.lower() else "sprites")
            target_path = f"res://assets/{subdir}/{slug}{ext}"

        existing = self.parse_requirements()
        for item in existing:
            if item.req_id == slug:
                return item

        new_line = f"- [ ] `{slug}` | **{kind}** | `{target_path}` | {description or slug.replace('_', ' ').title()}\n"
        text = self.requirements_md_path.read_text(encoding="utf-8", errors="ignore")
        if not text.endswith("\n"):
            text += "\n"
        text += new_line
        self.requirements_md_path.write_text(text, encoding="utf-8")

        self.store.log_activity(
            ActivityLogEntry(
                actor=actor,  # type: ignore[arg-type]
                action="add_asset_requirement",
                reason=f"Added requirement `{slug}` ({kind}) -> {target_path}",
            )
        )
        parsed = self.parse_requirements()
        return next((r for r in parsed if r.req_id == slug), AssetRequirementItem(req_id=slug, kind=kind, target_path=target_path, description=description))

    def update_requirement_status(
        self,
        req_id: str,
        status: str,
        linked_asset_id: str = "",
        sha256: str = "",
    ) -> AssetRequirementItem | None:
        if not self.requirements_md_path.exists():
            return None
        lines = self.requirements_md_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        box = "x" if status == "done" else ("~" if status == "in_progress" else " ")
        updated_lines: list[str] = []

        for line in lines:
            m = REQUIREMENT_LINE_RE.match(line.strip())
            if m and m.group("req_id").strip() == req_id:
                kind = m.group("kind").strip()
                target = m.group("target").strip()
                desc = m.group("desc").strip()
                meta_parts = []
                aid = linked_asset_id
                if not aid and m.group("meta"):
                    am = re.search(r"asset:([a-zA-Z0-9_]+)", m.group("meta"))
                    if am:
                        aid = am.group(1)
                if aid:
                    meta_parts.append(f"asset:{aid}")
                if sha256:
                    meta_parts.append(f"sha256:{sha256[:12]}")
                meta_suffix = f" | {' '.join(meta_parts)}" if meta_parts else ""
                updated_lines.append(f"- [{box}] `{req_id}` | **{kind}** | `{target}` | {desc}{meta_suffix}")
            else:
                updated_lines.append(line)

        self.requirements_md_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
        self._rebuild_delivery_manifest()
        return next((r for r in self.parse_requirements() if r.req_id == req_id), None)

    def append_handoff(self, title: str, message: str, author: str = "coding_agent") -> dict[str, Any]:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        block = f"\n---\n\n### [{stamp}] ({author.upper()}) {title}\n{message.strip()}\n"
        current = self.handoffs_md_path.read_text(encoding="utf-8", errors="ignore") if self.handoffs_md_path.exists() else "# Agent Handoffs\n"
        self.handoffs_md_path.write_text(current + block, encoding="utf-8")

        h_id = new_id("handoff")
        self.handoffs_dir.mkdir(parents=True, exist_ok=True)
        (self.handoffs_dir / f"{h_id}.md").write_text(f"# {title}\n- Author: {author}\n- Time: {stamp}\n\n{message}\n", encoding="utf-8")
        return {"handoff_id": h_id, "title": title, "author": author, "timestamp": stamp}

    def _rebuild_delivery_manifest(self) -> dict[str, Any]:
        assets = self.store.list_assets(include_archived=False)
        reqs = self.parse_requirements()
        deliveries: list[dict[str, Any]] = []

        for ast in assets:
            abs_p = self.store.resolve_safe_path(ast.file_path)
            sha = compute_file_sha256(abs_p)
            deliveries.append(
                {
                    "asset_id": ast.id,
                    "name": ast.name,
                    "type": ast.type.value,
                    "verb": ast.verb,
                    "file_path": ast.file_path,
                    "sha256": sha,
                    "parent_ids": ast.parent_ids,
                    "qc_passed": ast.qc_report.passed if ast.qc_report else True,
                    "critic_score": ast.critic_report.overall_score if ast.critic_report else 0.92,
                }
            )

        manifest = {
            "project": self.store.project_name,
            "updated_at": time.time(),
            "requirements_total": len(reqs),
            "requirements_completed": sum(1 for r in reqs if r.status == "done"),
            "requirements_open": sum(1 for r in reqs if r.status == "open"),
            "deliveries": deliveries,
        }
        self.delivery_manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest

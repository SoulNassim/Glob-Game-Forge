"""Import a game project (Godot, Unity, Unreal or any folder) into a Glob workspace.

`analyze()` walks the game folder read-only and sorts what it finds into units:

  * characters (from the game's own data, e.g. Godot `PlayableCharacter` resources, and from folder
    names), with their 3D models, animation clips, textures and texture variants,
  * single assets (a folder holding one model with its textures, e.g. `assets/Items/Katana/`),
  * kits (a folder with many models, e.g. a modular dungeon set), split into piece families,
  * galleries (folders of images: skies, texture packs, concept art),
  * audio, design docs, and game data entries (items, weapons, enemies) that have no art yet.

`import_game()` writes those units into the workspace using Glob's conventions: sections are numbered
top-level folders, every asset is a `.md` with `kind:` in its header and its files sit next to it in
`<asset>/{3d,animations,textures,variants,candidates}/`. Files are hard-linked (copied across drives); the game folder is never
changed. Importing into a workspace that already has sections merges into them, and into asset
documents of the same name (so a character the agent sketched earlier gets the real model).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from forge.blueprint_contract import serialize_frontmatter, split_frontmatter
from forge.gamescan.safety import SECRET_FILE_PATTERNS

import fnmatch

MODEL_EXT = {".glb", ".gltf", ".fbx", ".obj", ".blend", ".dae"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".tga", ".bmp", ".exr", ".hdr", ".psd", ".tif", ".tiff"}
WEB_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
AUDIO_EXT = {".wav", ".ogg", ".mp3", ".flac"}
FONT_EXT = {".ttf", ".otf", ".woff", ".woff2"}
SIDECAR_EXT = {".mtl", ".bin"}
DOC_EXT = {".md", ".txt"}
ARCHIVE_EXT = {".zip", ".rar", ".7z"}
MAX_FILE_BYTES = 300 * 1024 * 1024
KIT_MIN_MODELS = 9
MAX_KIT_FAMILIES = 120

# Engine caches, build output, version control and third-party code: never part of the game's art.
SKIP_DIRS = {
    ".git", ".godot", ".import", ".vs", ".vscode", ".idea", "node_modules", "__pycache__", ".venv", "venv",
    "build", "builds", "dist", "export", "exports", "addons", "plugins", "demo", "demos", "_retired", "retired",
    "library", "temp", "logs", "obj", "packages", "projectsettings", "usersettings", "intermediate", "binaries",
    "saved", "deriveddatacache", ".agent-out", ".kilo", ".claude", ".codex", ".cursor", ".gemini", ".agents", "glob_delivery",
}
# Folders that only say what format is inside; their files belong to the parent folder's asset.
GENERIC_DIRS = {
    "fbx", "glb", "gltf", "obj", "rigged", "rig", "textures", "texture", "tex", "materials", "material", "mats",
    "maps", "anims", "anim", "animations", "animation", "newanims", "meshes", "mesh", "source", "sources", "export",
    "prefabs", "models", "model",
}
DOC_DIRS = {"docs", "doc", "readmes", "design", "gdd", "documentation", "wiki", "notes"}
ANIM_WORDS = {
    "idle", "walk", "walking", "run", "running", "jog", "sprint", "strafe", "attack", "slash", "melee", "block", "jump",
    "fall", "land", "death", "die", "dying", "hit", "hurt", "dodge", "roll", "crouch", "climb", "swim", "cast", "shoot",
    "reload", "taunt", "dance", "emote", "punch", "kick", "backward", "torch",
}
TEXTURE_WORDS = {
    "basecolor", "base", "albedo", "diffuse", "diff", "normal", "nrm", "nor", "rough", "roughness", "metal", "metallic",
    "metalness", "ao", "occlusion", "emission", "emissive", "height", "disp", "displacement", "spec", "specular", "orm",
    "rm", "mask", "opacity", "alpha", "col", "color", "tex", "texture", "bump",
}
VARIANT_WORDS = {"pale", "dark", "light", "red", "blue", "green", "black", "white", "gold", "alt", "variant", "damaged", "clean", "dirty", "old", "new", "winter", "summer"}
PREVIEW_WORDS = {"concept", "tpose", "pose", "reference", "ref", "front", "preview", "render", "portrait", "art", "image", "screenshot", "thumb", "icon"}
NOISE_WORDS = {"3d", "model", "models", "fbx", "glb", "obj", "mesh", "meshy", "ai", "tripo", "convert", "final", "export", "asset", "assets", "the", "res"}

# (key, title, asset kind, keywords). Order matters: the first section whose words match wins.
SECTIONS: list[tuple[str, str, str | None, str]] = [
    ("creatures", "Creatures & enemies", "creature", r"enem|monster|creature|mob\b|boss|zombie|beast|demon|undead|ghoul"),
    ("characters", "Characters", "character", r"player|character|hero|npc|playable|protagonist|companion|rogue|knight|warrior|mage|wizard|soldier|merchant|villager|guard|archer|biped|humanoid"),
    ("weapons", "Weapons", "weapon", r"weapon|sword|katana|gun|axe|mace|bow|blade|dagger|spear|cannon|rifle|pistol|shotgun|hammer|staff|shield|sweeper|fists?\b"),
    ("items", "Items", "item", r"item|pickup|loot|potion|lantern|relic|key\b|coin|consumable|artifact|artefact|chalice|medkit|ammo|rounds|eye\b|bloodthirst"),
    ("first-person", "First-person", "prop", r"arms?\b|hands?\b|viewmodel|fps\b"),
    ("environment", "Environment", "environment", r"dungeon|environment|env\b|level|terrain|building|tile|modular|wall|floor|sky|skies|skys|hdri|panorama|spheremap|nature|tree|rock|struct|trap"),
    ("vehicles", "Vehicles", "vehicle", r"vehicle|car\b|ship|boat|cart\b|wagon"),
    ("ui", "UI", "ui", r"\bui\b|icon|hud|gui|menu|font|cursor|button"),
    ("vfx", "Effects", "vfx", r"vfx|\bfx\b|particle|effect|shader"),
    ("props", "Props", "prop", r"prop|crate|barrel|furniture|decor|crucifix"),
    ("textures", "Materials & textures", None, r"texture|material|pack\b|tileable"),
    ("art", "Art & images", None, r"$^"),
    ("audio", "Audio", None, r"$^"),
    ("docs", "Design docs", None, r"$^"),
]
SECTION = {key: (title, kind, re.compile(r"(?:^|\s)(?:" + words + ")")) for key, title, kind, words in SECTIONS}
# How the sections are numbered and shown: the cast first, then what they hold, then the world, then the raw material.
DISPLAY_ORDER = ["characters", "creatures", "weapons", "items", "first-person", "vehicles", "environment", "props", "ui", "vfx",
                 "audio", "art", "textures", "docs"]


def _tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text))
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _words(text: str) -> str:
    return " " + " ".join(_tokens(text)) + " "


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "asset"


def clean_title(text: str) -> str:
    """'hooded+rogue+3d+model' → 'Hooded Rogue'; 'Player_Model_Cahara' → 'Player Cahara'."""
    stem = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "", str(text), flags=re.I)
    words = [w for w in re.split(r"[\s_+\-.]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)) if w]
    kept = [w for w in words if w.lower() not in NOISE_WORDS] or words
    title = " ".join(kept).strip() or str(text)
    return " ".join(w if w.isupper() and len(w) <= 4 else w[:1].upper() + w[1:] for w in title.split())[:60]


def _skip_file(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith((".import", ".uid", ".meta", ".tmp")) or name.startswith("."):
        return True
    return any(fnmatch.fnmatch(name, pattern.lower()) for pattern in SECRET_FILE_PATTERNS)


@dataclass
class Unit:
    key: str
    title: str
    section: str = ""
    kind: str | None = None
    shape: str = "asset"  # asset | kit | gallery | audio | docs | data
    source: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def count(self, role: str) -> int:
        return sum(1 for f in self.files if f["role"] == role)


# ---------- game data ----------

_TRES_FIELD = re.compile(r'^([a-z_][a-z0-9_]*)\s*=\s*(.+)$', re.I)


def _tres_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("&\"") or raw.startswith('"'):
        return bytes(raw.lstrip("&")[1:-1], "utf-8").decode("unicode_escape", errors="ignore")
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


def _read_godot_data(path: Path) -> dict[str, Any] | None:
    """A Godot .tres resource with a display name: an item, weapon, character or enemy definition."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:20_000]
    except OSError:
        return None
    head = re.search(r'script_class="([^"]+)"', text)
    fields: dict[str, Any] = {}
    in_resource = False
    for line in text.splitlines():
        if line.startswith("["):
            in_resource = line.startswith("[resource")
            continue
        match = _TRES_FIELD.match(line) if in_resource else None
        if match:
            fields[match.group(1)] = _tres_value(match.group(2))
    name = fields.get("display_name") or fields.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    scene = None
    for ref in re.finditer(r'\[ext_resource type="PackedScene"[^\]]*path="([^"]+)"', text):
        scene = ref.group(1)
    return {
        "id": str(fields.get("id") or path.stem), "name": name.strip(), "class": head.group(1) if head else "",
        "description": str(fields.get("description") or "").strip(), "title": str(fields.get("class_title") or "").strip(),
        "scene": scene, "fields": fields, "file": path,
    }


def _engine(root: Path) -> tuple[str, str, str]:
    """(engine id, engine label, game title)."""
    godot = root / "project.godot"
    if godot.is_file():
        text = godot.read_text(encoding="utf-8", errors="ignore")
        name = re.search(r'^config/name="([^"]*)"', text, re.M)
        version = re.search(r'config/features=PackedStringArray\("([0-9.]+)"', text)
        label = f"Godot {version.group(1)}" if version else "Godot"
        return "godot", label, (name.group(1) if name else root.name)
    if (root / "Assets").is_dir() and (root / "ProjectSettings").is_dir():
        settings = root / "ProjectSettings" / "ProjectSettings.asset"
        name = re.search(r"productName:\s*(.+)", settings.read_text(encoding="utf-8", errors="ignore")) if settings.is_file() else None
        return "unity", "Unity", (name.group(1).strip() if name else root.name)
    if any(root.glob("*.uproject")):
        return "unreal", "Unreal Engine", next(root.glob("*.uproject")).stem
    return "folder", "Folder", root.name


def _res_path(engine: str, root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return f"res://{rel}" if engine == "godot" else rel


# ---------- analysis ----------

def _walk(root: Path) -> tuple[list[Path], dict[str, int]]:
    files: list[Path] = []
    skipped = {"engine_files": 0, "archives": 0, "too_large": 0, "unreal_binary": 0}
    for current, dirs, names in os.walk(root):
        # Also skip Glob's own home in the game (`glob/` with its manifest), so imports never copy it into itself.
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".") and not (Path(current) / d / "manifest.db").is_file()]
        for name in names:
            path = Path(current) / name
            suffix = path.suffix.lower()
            if _skip_file(path):
                continue
            if suffix in ARCHIVE_EXT:
                skipped["archives"] += 1
                continue
            if suffix == ".uasset":
                skipped["unreal_binary"] += 1
                continue
            if suffix not in MODEL_EXT | IMAGE_EXT | AUDIO_EXT | FONT_EXT | SIDECAR_EXT | DOC_EXT | {".anim", ".res", ".tres"}:
                skipped["engine_files"] += 1
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    skipped["too_large"] += 1
                    continue
            except OSError:
                continue
            files.append(path)
    return files, skipped


def _in_anim_dir(rel_parts: tuple[str, ...]) -> bool:
    return any("anim" in part.lower() for part in rel_parts[:-1])


def _unit_dir(root: Path, path: Path) -> Path:
    directory = path.parent
    while directory != root and (directory.name.lower() in GENERIC_DIRS or directory.name.lower().endswith(".fbm")):
        directory = directory.parent
    return directory


def _section_for(*texts: str, default: str) -> str:
    for text in texts:
        words = _words(text)
        for key, _title, _kind, _ in SECTIONS:
            if SECTION[key][2].search(words):
                return key
    return default


def analyze(folder: str | Path) -> dict[str, Any]:
    """Read-only scan of a game folder; returns the units plus a summary for the import preview."""
    root = Path(str(folder)).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"'{folder}' is not a folder.")
    engine, engine_label, game_title = _engine(root)
    files, skipped = _walk(root)

    # Game data (Godot resources with a display name): characters, items, weapons, enemies.
    data_entries = [entry for path in files if path.suffix.lower() == ".tres" and (entry := _read_godot_data(path))]
    characters = {
        tok: entry for entry in data_entries
        if re.search(r"character|player|hero|npc", entry["class"] + " " + entry["file"].parent.name, re.I)
        for tok in {entry["id"].lower(), entry["name"].lower()} if tok.isidentifier()
    }

    units: dict[str, Unit] = {}
    docs = Unit(key="docs", title="Design docs", section="docs", shape="docs")
    model_dirs = {p.parent for p in files if p.suffix.lower() in MODEL_EXT}

    def unit(key: str, title: str, source: str) -> Unit:
        if key not in units:
            units[key] = Unit(key=key, title=title, source=source)
        return units[key]

    for path in files:
        rel = path.relative_to(root)
        suffix = path.suffix.lower()
        stem_tokens = set(_tokens(path.stem))
        if suffix in DOC_EXT:
            top = rel.parts[0].lower() if len(rel.parts) > 1 else ""
            if suffix == ".md" and (len(rel.parts) == 1 or top in DOC_DIRS) or (suffix == ".txt" and top in DOC_DIRS):
                docs.files.append({"path": path, "role": "doc"})
            continue
        if suffix in (".res", ".tres") and not (_in_anim_dir(rel.parts) or suffix == ".res" and stem_tokens & ANIM_WORDS and "data" not in rel.parts):
            continue
        path_tokens = set(_tokens(rel.as_posix()))
        owner = next((name for name in characters if name in path_tokens), None)
        if owner:
            entry = characters[owner]
            target = unit(f"char:{entry['id']}", entry["name"], "")
            target.section, target.kind, target.data = "characters", "character", entry
        else:
            directory = _unit_dir(root, path)
            if suffix in AUDIO_EXT:
                key = f"audio:{directory.relative_to(root).as_posix()}"
                target = unit(key, clean_title(directory.name if directory != root else "Sounds"), directory.relative_to(root).as_posix())
                target.shape, target.section = "audio", "audio"
            else:
                target = unit(directory.relative_to(root).as_posix() or ".", clean_title(directory.name if directory != root else game_title), directory.relative_to(root).as_posix())
        # Role of the file inside its unit.
        if suffix in AUDIO_EXT:
            role = "audio"
        elif suffix in FONT_EXT:
            role = "font"
        elif suffix in SIDECAR_EXT:
            role = "sidecar"
        elif suffix in (".anim", ".res", ".tres"):
            role = "anim"
        elif suffix in MODEL_EXT:
            is_anim = "@" in path.stem or _in_anim_dir(rel.parts) or (bool(stem_tokens & ANIM_WORDS) and target.section == "characters")
            role = "anim" if is_anim else "model"
        else:
            siblings = [m.stem.lower() for m in path.parent.glob("*") if m.suffix.lower() in MODEL_EXT]
            model_words = {t for s in siblings for t in _tokens(s)}
            stem = path.stem.lower()
            is_texture = (
                bool(stem_tokens & TEXTURE_WORDS)
                or path.parent.name.lower().endswith(".fbm")
                or path.parent.name.lower() in {"textures", "texture", "materials", "maps", "tex"}
                or (path.parent in model_dirs and (re.search(r"_\d+$", stem) is not None or any(stem.startswith(s) for s in siblings)))
            )
            role = "variant" if is_texture and (stem_tokens - model_words) & VARIANT_WORDS else "texture" if is_texture else "image"
        target.files.append({"path": path, "rel": rel.as_posix(), "role": role, "size": path.stat().st_size})

    # Characters whose clips include the rigged mesh: use the first clip outside an animation folder as the model.
    for target in units.values():
        if target.section == "characters" and not target.count("model"):
            mesh = next((f for f in target.files if f["role"] == "anim" and Path(f["path"]).suffix.lower() in MODEL_EXT and not _in_anim_dir(Path(f["rel"]).parts)), None)
            if mesh:
                target.files.append({**mesh, "role": "model"})

    _split_mixed(units)
    units.pop(".", None) if units.get(".") and not any(f["role"] in ("model", "anim") for f in units["."].files) else None

    for target in list(units.values()):
        roles = {f["role"] for f in target.files}
        if target.shape == "audio":
            continue
        if not roles & {"model", "anim"}:
            # Only images (and fonts): a gallery, e.g. skies, a texture pack, concept art.
            target.shape = "gallery"
            all_textures = roles <= {"texture", "variant"}
            target.section = target.section or _section_for(target.source, default="textures" if all_textures else "art")
            if SECTION[target.section][1] is None or target.section in ("characters", "creatures", "weapons", "items"):
                target.section = target.section if target.section in ("textures", "art") else ("textures" if all_textures else "art")
            if roles == {"font"}:
                target.section = "ui"
            continue
        if not target.section:
            names = " ".join(Path(f["rel"]).stem for f in target.files if f["role"] == "model")
            texts = (target.title, names, target.source) if ":" in target.key else (target.source, target.title, names)
            target.section = _section_for(*texts, default="props")
            if SECTION[target.section][1] is None:
                target.section = "props"
        target.kind = target.kind or SECTION[target.section][1] or "prop"
        if target.section != "characters" and target.count("model") >= KIT_MIN_MODELS:
            target.shape = "kit"
        if target.title.lower() in ("", ".") or target.key == ".":
            target.title = game_title
        # Prefer the model's own name when the folder name says little ("Rogue1" → "Hooded Rogue").
        first = next((Path(f["rel"]) for f in target.files if f["role"] == "model"), None)
        if target.shape == "asset" and first is not None and re.search(r"\d$|^(enemy|items?|props?|models?)$", target.title.lower()):
            better = clean_title(first.stem)
            if better.lower() not in ("arm", "arms") and len(better) > 3:
                target.title = better

    # Data entries without art: weapons, items and enemies the game defines but has no model for yet.
    unit_words = {key: set(_tokens(u.title + " " + u.source + " " + " ".join(Path(f["rel"]).stem for f in u.files))) for key, u in units.items()}
    for entry in data_entries:
        if f"char:{entry['id']}" in units:
            continue
        if entry["id"].lower() in characters:
            units[f"data:{entry['id']}"] = Unit(key=f"data:{entry['id']}", title=entry["name"], section="characters", kind="character", shape="data", data=entry)
            continue
        ident = {t for t in _tokens(entry["id"] + " " + entry["name"]) if len(t) >= 4 and t not in NOISE_WORDS}
        match = next((units[k] for k, words in unit_words.items() if ident and ident <= words), None) or next(
            (units[k] for k, words in unit_words.items() if ident & words and units[k].shape == "asset" and units[k].section in ("weapons", "items", "creatures")), None
        )
        if match is not None and match.shape == "asset" and not match.data:
            match.data = entry
            match.title = entry["name"]
            continue
        section = _section_for(entry["class"] + " " + entry["id"] + " " + entry["name"], entry["file"].parent.as_posix(), default="items")
        if SECTION[section][1] is None:
            section = "items"
        units[f"data:{entry['id']}"] = Unit(key=f"data:{entry['id']}", title=entry["name"], section=section, kind=SECTION[section][1], shape="data", data=entry)

    # The same thing found in two folders (a sword copied next to another model) is one asset with both files.
    merged: dict[tuple[str, str], Unit] = {}
    for key, target in list(units.items()):
        if target.shape != "asset":
            continue
        twin = merged.setdefault((target.section, target.title.lower()), target)
        if twin is not target:
            twin.files.extend(target.files)
            del units[key]
    ordered = [u for u in units.values() if u.files or u.shape == "data"]
    if docs.files:
        ordered.append(docs)
    sections: dict[str, dict[str, Any]] = {}
    for target in ordered:
        info = sections.setdefault(target.section, {"key": target.section, "title": SECTION[target.section][0], "units": []})
        info["units"].append(_unit_summary(target))
    order = DISPLAY_ORDER
    summary = {
        "folder": str(root),
        "engine": engine,
        "engine_label": engine_label,
        "title": game_title,
        "sections": sorted(sections.values(), key=lambda s: order.index(s["key"])),
        "counts": {
            "models": sum(u.count("model") for u in ordered),
            "animations": sum(u.count("anim") for u in ordered),
            "textures": sum(u.count("texture") + u.count("variant") for u in ordered),
            "images": sum(u.count("image") for u in ordered),
            "audio": sum(u.count("audio") for u in ordered),
            "docs": docs.count("doc"),
            "characters": sum(1 for u in ordered if u.section == "characters"),
            "needs_art": sum(1 for u in ordered if u.shape == "data"),
            "files": sum(len(u.files) for u in ordered),
            "bytes": sum(f.get("size", 0) for u in ordered for f in u.files),
        },
        "skipped": skipped,
    }
    return {"summary": summary, "units": ordered, "root": root, "engine": engine}


def _split_mixed(units: dict[str, Unit]) -> None:
    """A folder holding a few unrelated models ('medieval sword' and 'zombie character') becomes one asset each."""
    for key, target in list(units.items()):
        models = [f for f in target.files if f["role"] == "model"]
        if target.section == "characters" or not 2 <= len(models) < KIT_MIN_MODELS:
            continue
        families: dict[str, list[dict[str, Any]]] = {}
        for info in models:
            families.setdefault(_family_key(Path(info["rel"]).stem), []).append(info)
        if len(families) < 2:
            continue
        words = {fam: {t for t in _tokens(fam) if t not in NOISE_WORDS} for fam in families}
        if all(words[a] & words[b] for a in words for b in words):
            continue  # arms.glb / arms_lowpoly.fbx: same thing in different files
        del units[key]
        rest = [f for f in target.files if f["role"] != "model"]
        for fam, members in families.items():
            own = [f for f in rest if Path(f["rel"]).stem.lower().startswith(fam) or set(_tokens(Path(f["rel"]).stem)) & words[fam] - TEXTURE_WORDS]
            rest = [f for f in rest if f not in own]
            units[f"{key}:{fam}"] = Unit(key=f"{key}:{fam}", title=clean_title(fam), source=target.source, files=members + own)
        if rest:
            units[f"{key}:{next(iter(families))}"].files.extend(rest)


def _unit_summary(target: Unit) -> dict[str, Any]:
    return {
        "id": target.key, "title": target.title, "shape": target.shape, "kind": target.kind, "source": target.source,
        "models": target.count("model"), "animations": target.count("anim"), "textures": target.count("texture"),
        "variants": target.count("variant"), "images": target.count("image"), "audio": target.count("audio"),
        "description": (target.data.get("description") or "")[:200] if target.data else "",
    }


def preview(folder: str | Path) -> dict[str, Any]:
    return analyze(folder)["summary"]


# ---------- writing the workspace ----------

def _section_folders(ws: Path) -> dict[str, Path]:
    """Existing top-level folders matched to section keys by their names and titles."""
    found: dict[str, Path] = {}
    for child in sorted(p for p in ws.iterdir() if p.is_dir() and not p.name.startswith((".", "_")) and p.name not in ("inbox", "pillars")):
        title = child.name
        readme = child / "README.md"
        if readme.is_file():
            heading = re.search(r"^#\s+(.+)$", readme.read_text(encoding="utf-8", errors="ignore"), re.M)
            title += " " + (heading.group(1) if heading else "")
        words = _words(title)
        for key, *_ in SECTIONS:
            if key not in found and (key.replace("-", " ") in words or SECTION[key][2].search(words)):
                found[key] = child
                break
    return found


def _next_number(ws: Path) -> int:
    numbers = [int(m.group(1)) for p in ws.iterdir() if p.is_dir() and (m := re.match(r"^(\d+)[\s._-]", p.name))]
    return max(numbers, default=0) + 1


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for index in range(2, 1000):
        candidate = dest.with_name(f"{dest.stem}-{index}{dest.suffix}")
        if not candidate.exists():
            return candidate
    return dest


class _Writer:
    def __init__(self, ws: Path, root: Path, engine: str, progress: Callable[[str], None]) -> None:
        self.ws, self.root, self.engine, self.progress = ws, root, engine, progress
        self.copied: list[dict[str, str]] = []
        self.bytes = 0  # what real copies took; hard links take no extra space
        self.linked = 0

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.ws.resolve()).as_posix()

    def copy(self, source: Path, folder: Path, name: str | None = None) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / (name or source.name)
        # Re-importing the same file into the same place is a no-op instead of a duplicate.
        if dest.exists() and dest.stat().st_size == source.stat().st_size:
            return dest
        dest = _unique(dest)
        # A hard link when the workspace is on the same drive (it usually lives in <game>/glob/): the file shows up
        # in Glob without taking space twice. Elsewhere (another drive, a file system without links) a real copy.
        try:
            os.link(source, dest)
            self.linked += 1
        except OSError:
            shutil.copy2(source, dest)
            self.bytes += dest.stat().st_size
        self.copied.append({"from": str(source), "to": self.rel(dest)})
        return dest

    def res(self, path: Path) -> str:
        return _res_path(self.engine, self.root, path)


def _file_list(writer: _Writer, entries: list[tuple[str, dict[str, Any], Path]]) -> list[str]:
    lines = []
    for label, info, dest in entries:
        lines.append(f"- **{label}** · {Path(info['rel']).suffix.lstrip('.').lower()} · `{writer.res(info['path'])}` → `{dest.parent.name}/{dest.name}`")
    return lines


def _data_table(entry: dict[str, Any]) -> list[str]:
    skip = {"script", "id", "display_name", "description", "class_title", "debug_color", "world_model_scene", "starting_items", "category", "max_stack"}
    rows = [
        (k, v) for k, v in entry.get("fields", {}).items()
        if k not in skip and not k.startswith("grid_") and not k.endswith("_type") and isinstance(v, (int, float, str)) and not isinstance(v, bool) and len(str(v)) < 60
    ]
    if not rows:
        return []
    return ["| Stat | Value |", "|---|---|", *[f"| {k.replace('_', ' ')} | {v} |" for k, v in rows[:14]]]


def _import_block(writer: _Writer, target: Unit, placed: dict[str, list[tuple[str, dict[str, Any], Path]]]) -> str:
    lines = ["<!-- glob-import -->", "## From the game", ""]
    facts = [("Engine", writer.engine.title())]
    if target.source:
        facts.append(("Source", f"`{writer.res(writer.root / target.source) if target.source != '.' else writer.res(writer.root)}`"))
    if target.data.get("scene"):
        facts.append(("Scene", f"`{target.data['scene']}`"))
    if target.data.get("class"):
        facts.append(("Data", f"`{writer.res(target.data['file'])}` ({target.data['class']})"))
    for role, label in (("model", "3D models"), ("anim", "Animations"), ("texture", "Textures"), ("variant", "Variants"), ("image", "Images")):
        if placed.get(role):
            facts.append((label, str(len(placed[role]))))
    lines += ["| | |", "|---|---|", *[f"| {k} | {v} |" for k, v in facts], ""]
    stats = _data_table(target.data) if target.data else []
    if stats:
        lines += ["### Stats", "", *stats, ""]
    for role, heading in (("anim", "Animations"), ("model", "3D models"), ("variant", "Variants"), ("texture", "Textures"), ("image", "Images")):
        if placed.get(role):
            lines += [f"### {heading}", "", *_file_list(writer, placed[role][:80])]
            if len(placed[role]) > 80:
                lines.append(f"- … and {len(placed[role]) - 80} more")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _pick_winner(images: list[Path]) -> Path | None:
    web = [p for p in images if p.suffix.lower() in WEB_IMAGE_EXT]
    for path in web:
        if set(_tokens(path.stem)) & PREVIEW_WORDS:
            return path
    return web[0] if web else None


def _write_asset(writer: _Writer, folder: Path, target: Unit, files: list[dict[str, Any]], title: str | None = None, existing: Path | None = None) -> Path:
    title = title or target.title
    md = existing or folder / f"{_slug(title)}.md"
    out = md.parent / md.stem
    placed: dict[str, list[tuple[str, dict[str, Any], Path]]] = {}
    destinations = {"model": "3d", "sidecar": "3d", "anim": "animations", "texture": "textures", "variant": "variants", "image": "candidates", "font": "files", "audio": "files"}
    for info in files:
        role = info["role"]
        dest = writer.copy(info["path"], out / destinations[role])
        placed.setdefault(role, []).append((clean_title(Path(info["rel"]).stem) if role == "anim" else Path(info["rel"]).name, info, dest))
        if role == "model" and dest.suffix.lower() in (".fbx", ".obj", ".gltf"):
            _copy_model_textures(writer, Path(info["path"]), dest.parent)
    images = [dest for _, _, dest in placed.get("image", [])]
    winner = _pick_winner(images)
    if md.is_file():
        meta, body, _ = split_frontmatter(md.read_text(encoding="utf-8", errors="ignore"))
    else:
        meta, body = {}, f"# {title}\n"
        description = " ".join(part for part in (target.data.get("title"), target.data.get("description")) if part) if target.data else ""
        if description:
            body += f"\n{description.rstrip('.')}.\n"
    meta.setdefault("kind", target.kind or "prop")
    if winner and not meta.get("winner"):
        meta["winner"] = writer.rel(winner)
    meta["source"] = writer.res(writer.root / target.source) if target.source else (target.data.get("scene") or "")
    meta["imported"] = True
    if not meta["source"]:
        meta.pop("source")
    block = _import_block(writer, target, placed)
    if "<!-- glob-import -->" in body:
        body = body.split("<!-- glob-import -->")[0].rstrip() + "\n\n" + block
    else:
        body = body.rstrip() + "\n\n" + block
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(serialize_frontmatter(meta, body), encoding="utf-8")
    return md


def _copy_model_textures(writer: _Writer, model: Path, folder: Path) -> None:
    """FBX/OBJ/glTF files point at texture files by relative path; keep those next to the model so the 3D view shows them."""
    for sibling in model.parent.iterdir():
        try:
            if sibling.is_file() and sibling.suffix.lower() in WEB_IMAGE_EXT | {".tga"} and not _skip_file(sibling):
                writer.copy(sibling, folder)
            elif sibling.is_dir() and sibling.name.lower().endswith(".fbm"):
                # Flat next to the model: the viewer looks textures up by file name there.
                for texture in sibling.iterdir():
                    if texture.is_file() and texture.suffix.lower() in IMAGE_EXT and not _skip_file(texture):
                        writer.copy(texture, folder)
        except OSError:
            continue


def _readme(folder: Path, title: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    page = folder / "README.md"
    if page.is_file():
        existing = page.read_text(encoding="utf-8", errors="ignore")
        if "<!-- glob-import -->" in existing:
            existing = existing.split("<!-- glob-import -->")[0].rstrip()
        page.write_text(existing.rstrip() + "\n\n<!-- glob-import -->\n" + text.strip() + "\n", encoding="utf-8")
    else:
        page.write_text(f"# {title}\n\n{text.strip()}\n", encoding="utf-8")


def _family_key(stem: str) -> str:
    return re.sub(r"[\s_\-]*\d+$", "", stem.lower()) or stem.lower()


def _write_kit(writer: _Writer, folder: Path, target: Unit) -> list[Path]:
    kit = folder / _slug(target.title)
    models = [f for f in target.files if f["role"] == "model"]
    families: dict[str, list[dict[str, Any]]] = {}
    for info in models:
        families.setdefault(_family_key(Path(info["rel"]).stem), []).append(info)
    if len(families) > 24:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for key, members in families.items():
            parts = re.split(r"[_\s\-]+", key)
            grouped.setdefault("_".join(parts[:2]) if len(parts) > 1 else key, []).extend(members)
        families = grouped
    written: list[Path] = []
    rows = []
    if len(families) <= MAX_KIT_FAMILIES:
        for key, members in sorted(families.items()):
            stems = {Path(m["rel"]).stem.lower() for m in members}
            extras = [
                f for f in target.files
                if f["role"] in ("sidecar", "texture", "variant", "image", "anim") and Path(f["rel"]).stem.lower() in stems
            ]
            family = Unit(key=f"{target.key}:{key}", title=clean_title(key), section=target.section, kind=target.kind, source=target.source)
            md = _write_asset(writer, kit, family, members + extras)
            written.append(md)
            rows.append(f"| [{family.title}]({md.name}) | {len(members)} | {', '.join(sorted(Path(m['rel']).stem for m in members)[:6])}{' …' if len(members) > 6 else ''} |")
        shared = [f for f in target.files if f["role"] in ("texture", "variant", "image") and not any(Path(f["rel"]).stem.lower() == Path(m["rel"]).stem.lower() for m in models)]
        for info in shared:
            writer.copy(info["path"], kit / "textures" if info["role"] != "image" else kit)
    else:
        for info in target.files:
            writer.copy(info["path"], kit / "files")
    intro = (
        f"A kit of **{len(models)} pieces** in **{len(families)} families**, imported from `{writer.res(writer.root / target.source)}`."
        "\n\n| Family | Pieces | Files |\n|---|---|---|\n" + "\n".join(rows)
    )
    _readme(kit, target.title, intro)
    return written


def _write_gallery(writer: _Writer, folder: Path, target: Unit) -> Path:
    gallery = folder / _slug(target.title)
    shown = 0
    for info in target.files:
        web = Path(info["rel"]).suffix.lower() in WEB_IMAGE_EXT
        writer.copy(info["path"], gallery if web else gallery / "files")
        shown += web
    hidden = len(target.files) - shown
    note = f"{shown} image{'s' if shown != 1 else ''} from `{writer.res(writer.root / target.source) if target.source else 'the game'}`."
    if hidden:
        note += f" {hidden} more file{'s' if hidden != 1 else ''} (EXR, TGA, PSD, fonts…) are in `files/`."
    _readme(gallery, target.title, note)
    return gallery


def _write_audio(writer: _Writer, folder: Path, target: Unit) -> Path:
    box = folder / _slug(target.title)
    rows = []
    for info in target.files:
        dest = writer.copy(info["path"], box / "files")
        rows.append(f"| {clean_title(Path(info['rel']).stem)} | `files/{dest.name}` | `{writer.res(info['path'])}` |")
    _readme(box, target.title, "| Sound | File | In the game |\n|---|---|---|\n" + "\n".join(rows))
    return box


def import_game(
    folder: str | Path,
    ws_dir: Path,
    *,
    title: str | None = None,
    progress: Callable[[str], None] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy a game's assets into the workspace as sections, assets, kits and galleries.

    `plan` is what the user approved in the import preview: {"exclude": [unit ids], "move": {unit id: section key},
    "sections": {section key: {"include": bool, "title": str}}}. Everything the game defines without art gets a
    "needs art" page.
    """
    say = progress or (lambda _message: None)
    say("Scanning the game folder…")
    result = analyze(folder)
    summary, units, root, engine = result["summary"], result["units"], result["root"], result["engine"]
    plan = plan or {}
    excluded = set(plan.get("exclude") or [])
    moves = {uid: key for uid, key in (plan.get("move") or {}).items() if key in SECTION}
    section_plan = plan.get("sections") or {}
    titles = {key: str(v.get("title")).strip()[:60] for key, v in section_plan.items() if isinstance(v, dict) and str(v.get("title") or "").strip()}
    for target in units:
        target.section = moves.get(target.key, target.section)
    units = [u for u in units if u.key not in excluded and (section_plan.get(u.section) or {}).get("include", True) is not False]
    ws = Path(ws_dir)
    ws.mkdir(parents=True, exist_ok=True)
    writer = _Writer(ws, root, engine, say)
    sections = _section_folders(ws)
    number = _next_number(ws)

    def section_dir(key: str) -> Path:
        nonlocal number
        if key not in sections:
            sections[key] = ws / f"{number:02d}-{_slug(titles[key]) if key in titles else key}"
            number += 1
            created.add(key)
            _readme(sections[key], titles.get(key) or SECTION[key][0], SECTION_INTRO.get(key, ""))
        return sections[key]

    created: set[str] = set()

    needs_art: list[tuple[Unit, Path]] = []

    assets_written: list[str] = []
    order = DISPLAY_ORDER
    for target in sorted(units, key=lambda u: (order.index(u.section), u.title.lower())):
        folder_path = section_dir(target.section)
        say(f"{SECTION[target.section][0]} · {target.title}")
        if target.shape == "docs":
            for info in target.files:
                writer.copy(info["path"], folder_path)
            continue
        if target.shape == "audio":
            _write_audio(writer, folder_path, target)
            continue
        if target.shape == "gallery":
            _write_gallery(writer, folder_path, target)
            continue
        if target.shape == "kit":
            assets_written += [writer.rel(p) for p in _write_kit(writer, folder_path, target)]
            continue
        existing = _find_existing(folder_path, target.title)
        if target.shape == "data":
            md = existing or folder_path / f"{_slug(target.title)}.md"
            if not md.is_file():
                meta = {"kind": target.kind or "item", "status": "needs art", "source": writer.res(target.data["file"]), "imported": True}
                description = target.data.get("description") or ""
                body = f"# {target.title}\n\n{description}\n\n" if description else f"# {target.title}\n\n"
                body += "The game defines this, but it has no model or image yet.\n\n" + "\n".join(_data_table(target.data)) + "\n"
                md.write_text(serialize_frontmatter(meta, body), encoding="utf-8")
            assets_written.append(writer.rel(md))
            needs_art.append((target, md))
            continue
        md = _write_asset(writer, folder_path, target, target.files, existing=existing)
        assets_written.append(writer.rel(md))

    # A new section's intro opens with what it holds ("Cahara, Elia, Hooded Rogue · 2 still need art"): the
    # overview shows that line on the section's row.
    for key in created:
        line = _section_line([u for u in units if u.section == key])
        if line:
            (sections[key] / "README.md").write_text(f"# {titles.get(key) or SECTION[key][0]}\n\n{line}.\n\n{SECTION_INTRO.get(key, '')}\n", encoding="utf-8")
    docs = next((u for u in units if u.shape == "docs"), None)
    pitch = game_pitch([Path(f["path"]) for f in docs.files] if docs else [], root)
    _write_front_page(ws, summary, title or summary["title"], sections, units, pitch)
    manifest = ws / "inbox" / "import.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    previous = []
    try:
        previous = json.loads(manifest.read_text(encoding="utf-8")).get("imports", [])
    except (OSError, ValueError):
        pass
    record = {"folder": str(root), "engine": engine, "at": time.time(), "files": len(writer.copied), "bytes": writer.bytes, "copied": writer.copied}
    manifest.write_text(json.dumps({"imports": [*previous[-4:], record]}, indent=1), encoding="utf-8")
    copies = len(writer.copied) - writer.linked
    say(f"Added {len(writer.copied)} files: {writer.linked} linked (no extra space)" + (f", {copies} copied ({writer.bytes / 1_048_576:.0f} MB)." if copies else "."))
    return {"summary": summary, "assets": assets_written, "copied": len(writer.copied), "linked": writer.linked, "bytes": writer.bytes,
            "sections": {key: writer.rel(path) for key, path in sections.items()}}


def _find_existing(folder: Path, title: str) -> Path | None:
    slug = _slug(title)
    for md in folder.rglob("*.md"):
        if any(part.startswith(".") for part in md.relative_to(folder).parts):
            continue
        if md.stem.lower() == slug:
            return md
        try:
            head = md.read_text(encoding="utf-8", errors="ignore")[:3000]
        except OSError:
            continue
        heading = re.search(r"^#\s+(.+)$", head, re.M)
        if heading and _slug(heading.group(1)) == slug and head.startswith("---") and "kind:" in head:
            return md
    return None


SECTION_INTRO = {
    "characters": "Playable and story characters with their 3D models, animation clips, textures and variants.",
    "creatures": "Enemies and creatures.",
    "weapons": "Weapons, with their stats from the game data. Entries marked *needs art* exist in the game but have no model yet.",
    "items": "Items, relics and consumables. Entries marked *needs art* exist in the game but have no model yet.",
    "first-person": "Arms, hands and other first-person view models.",
    "environment": "Level kits, skies and other world pieces.",
    "props": "Props and set dressing.",
    "ui": "Interface art, icons and fonts.",
    "vfx": "Effects.",
    "textures": "Texture packs and materials.",
    "art": "Concept art, screenshots and other images.",
    "audio": "Sound effects and music.",
    "docs": "Design documents copied from the game project.",
}


PITCH_HEADINGS = re.compile(r"^#{1,4}\s*(?:the\s+)?(?:vision|pitch|elevator pitch|concept|game concept|overview|about|summary|premise|the game)\b", re.I | re.M)


def _paragraph_after(text: str, start: int) -> str:
    """The first plain paragraph after the line at `start` (skipping lists, tables and quotes before it)."""
    lines: list[str] = []
    for line in text[start:].splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if stripped.startswith("#") or (lines and stripped.startswith(("|", "```", "-", "*"))):
            break
        if stripped.startswith(("|", "```", "<", "---", "!", "-", "*", ">")):
            continue
        lines.append(stripped)
    return re.sub(r"[*_`]", "", " ".join(lines)).strip()


def game_pitch(docs: list[Path], root: Path) -> str:
    """What the game is, in its own words: the paragraph under a Vision / Pitch / Overview heading in its README or
    design docs, else the first real paragraph of its README. Empty when the docs say nothing usable."""
    readme = next((root / name for name in ("README.md", "readme.md", "GAME.md") if (root / name).is_file()), None)
    ranked = sorted(docs, key=lambda p: (not re.search(r"pitch|vision|concept|overview|status|gdd|design|readme", p.stem, re.I), p.name.lower()))
    for path in ([readme] if readme else []) + ranked:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:60_000]
        except OSError:
            continue
        match = PITCH_HEADINGS.search(text)
        found = _paragraph_after(text, match.start()) if match else ""
        if len(found) >= 60:
            return found[:900]
    if readme:
        text = readme.read_text(encoding="utf-8", errors="ignore")[:20_000]
        title = re.search(r"^#\s.*$", text, re.M)
        found = _paragraph_after(text, title.start()) if title else ""
        if len(found) >= 60:
            return found[:900]
    return ""


def _section_line(units: list[Unit]) -> str:
    """'Cahara, Elia, Hooded Rogue · 2 still need art': what a section holds, for the front page."""
    names = [u.title for u in units if u.shape in ("asset", "kit", "gallery", "audio")]
    shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
    if not shown and any(u.shape == "docs" for u in units):
        shown = f"{sum(len(u.files) for u in units)} design documents"
    needs = sum(1 for u in units if u.shape == "data")
    if needs:
        shown += (" · " if shown else "") + f"{needs} still need{'s' if needs == 1 else ''} art"
    return shown


def _write_front_page(ws: Path, summary: dict[str, Any], title: str, sections: dict[str, Path], units: list[Unit] | None = None, pitch: str = "") -> None:
    # The sections list under the page already says what each section holds (their intros), so this block only
    # adds what is still missing and where the copies came from.
    counts = summary["counts"]
    needs = [u.title for u in units or [] if u.shape == "data"]
    totals = ", ".join(f"{counts[k]} {label}" for k, label in (("models", "3D models"), ("animations", "animations"), ("textures", "textures"),
                                                               ("images", "images"), ("audio", "sounds")) if counts.get(k))
    block = ""
    if needs:
        more = f" and {len(needs) - 24} more" if len(needs) > 24 else ""
        block += (f"## Needs art\n\nThe game defines these, but they have no model or image yet: {', '.join(f'**{n}**' for n in needs[:24])}{more}. "
                  "Open one and press **Generate**, or let your coding agent order them.\n")
    block += (f"\n---\n\nImported from {summary['engine_label']} (`{summary['folder']}`) on {time.strftime('%Y-%m-%d')}"
              f"{': ' + totals if totals else ''}. The game folder was not changed; these are copies.")
    front = next((ws / name for name in ("README.md", "GAME.md") if (ws / name).is_file()), None)
    if front is None:
        lede = pitch or f"A {summary['engine_label']} game, imported into Glob with its characters, models, animations and art."
        (ws / "README.md").write_text(f"# {title}\n\n{lede}\n\n<!-- glob-import -->\n{block}\n", encoding="utf-8")
        return
    text = front.read_text(encoding="utf-8", errors="ignore")
    if "<!-- glob-import -->" in text:
        text = text.split("<!-- glob-import -->")[0].rstrip()
    front.write_text(text.rstrip() + "\n\n<!-- glob-import -->\n" + block + "\n", encoding="utf-8")


# ---------- looking at the imported art ----------

DESCRIBE_PROMPT = (
    "You are looking at art from a video game ({kind}, called \"{title}\"). In one or two sentences, describe what the "
    "subject looks like so an artist could redraw it: who or what it is (gender, age and build for people), outfit or "
    "shape, materials, colours and notable details. No art-style words, no story. Reply with only the description."
)


def _preview_image(ws: Path, md: Path, meta: dict[str, Any]) -> Path | None:
    winner = str(meta.get("winner") or "")
    if winner and (ws / winner).is_file():
        return ws / winner
    for sub in ("3d", "rig", "candidates"):
        folder = md.parent / md.stem / sub
        if folder.is_dir():
            found = sorted(folder.glob("*_thumb.png")) or sorted(p for p in folder.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))
            if found:
                return found[0]
    return None


def assets_to_describe(ws: Path) -> list[tuple[Path, dict[str, Any], str, Path]]:
    """Imported assets that have a picture (image or rendered model preview) but no prompt yet."""
    found = []
    for md in sorted(ws.rglob("*.md")):
        if any(part.startswith(".") or part == "inbox" for part in md.relative_to(ws).parts):
            continue
        head = md.read_text(encoding="utf-8", errors="ignore")
        if not head.startswith("---") or "imported: true" not in head[:2000]:
            continue
        meta, body, _ = split_frontmatter(head)
        if str(meta.get("prompt") or "").strip() or not meta.get("kind"):
            continue
        image = _preview_image(ws, md, meta)
        if image is not None:
            found.append((md, meta, body, image))
    return found


async def describe_assets(ws: Path, model_call: Callable[..., Any], limit: int = 40, progress: Callable[[str], None] | None = None) -> int:
    """Ask the chat model to look at each imported asset's picture and write its `prompt:`."""
    import asyncio
    import base64
    import io

    from PIL import Image

    todo = assets_to_describe(ws)[:limit]
    semaphore = asyncio.Semaphore(3)
    done = 0

    async def one(md: Path, meta: dict[str, Any], body: str, image: Path) -> None:
        nonlocal done
        title = (re.search(r"^#\s+(.+)$", body, re.M) or [None, md.stem])[1]
        try:
            with Image.open(image) as picture:
                picture = picture.convert("RGB")
                picture.thumbnail((768, 768))
                buffer = io.BytesIO()
                picture.save(buffer, "JPEG", quality=85)
            async with semaphore:
                result = await model_call(
                    prompt=f"Describe the {meta.get('kind')} in the image.",
                    system_prompt=DESCRIBE_PROMPT.format(kind=meta.get("kind"), title=title),
                    images_b64=[("image/jpeg", base64.b64encode(buffer.getvalue()).decode("ascii"))],
                    effort="low",
                )
        except Exception:  # noqa: BLE001 — a model without vision or a broken image leaves the prompt empty
            return
        text = " ".join(str(result.get("text") or "").split()).strip().strip('"')
        if not text or len(text) < 12 or text.lower().startswith(("i can't", "i cannot", "sorry")):
            return
        meta["prompt"] = text[:600]
        md.write_text(serialize_frontmatter(meta, body), encoding="utf-8")
        done += 1
        if progress:
            progress(f"Looked at {title}")

    await asyncio.gather(*(one(*entry) for entry in todo))
    return done

"""Turn a level picture into an asset pack: props, trim, matching walls and floors, and a modular kit, from one press.

A wall can't be cut out of a picture, since perspective and lighting are baked into it, so each surface is redrawn from
the level picture, flat-on. The chain then does the free work itself: props and trim are cut from their sheets, surfaces
are made tileable, and the kit is built from them. Everything already done is skipped, so pressing again only finishes
what's missing and never pays twice.

Files, next to the level ``<level>.md``: ``<level>-props/`` (props), ``<level>-pack/trim/`` (pillars, arches, frames),
``<level>-pack/materials/`` (surfaces) and ``<level>-pack/<level>-kit.md`` (the kit).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from forge import asset_studio, game_tools, modular
from forge.asset_studio import _ws_rel, assets_under, output_dir, read_asset, update_asset
from forge.blueprint_contract import serialize_frontmatter
from forge.overview import project_dimension

# Each surface fills one kit slot. The prompt says what to draw; the level picture says what it looks like.
SURFACES = (
    {"id": "wall", "slot": "wall", "label": "Main wall", "what": "the main wall surface"},
    {"id": "wall-damaged", "slot": "wall_2", "label": "Damaged wall", "what": "the same wall surface, more worn and damaged, with cracks and stains"},
    {"id": "wall-detail", "slot": "wall_3", "label": "Wall with detail", "what": "the same wall surface with built-in details seen in the picture, such as pipes, panels or supports"},
    {"id": "floor", "slot": "floor", "label": "Main floor", "what": "the main floor surface, seen from straight above"},
    {"id": "floor-worn", "slot": "floor_2", "label": "Worn floor", "what": "the same floor surface, more worn and dirty, seen from straight above"},
    {"id": "ceiling", "slot": "ceiling", "label": "Ceiling", "what": "the ceiling surface, or if none is visible, one that suits this place, seen from straight below"},
)
SHEETS = (
    {"id": "props", "label": "Props", "hint": "Every prop on one sheet, cut out for free"},
    {"id": "trim", "label": "Pillars, arches and frames", "hint": "Pieces that also hide the joins between walls"},
)
SURFACE_RULE = (
    "The attached image is a game level. Use it only to match the materials, colours, wear and mood of this place: "
    "do not copy its camera angle, perspective, lighting or shadows, and leave out every object and character."
)
TRIM_MESH_POLYCOUNT = 12000  # game-ready, and far below what rigging or a level can't handle


def pack_dir(level_path: Path) -> Path:
    return level_path.parent / f"{level_path.stem}-pack"


def _rel_in(ws_dir: Path, path: Path) -> str:
    return _ws_rel(ws_dir, path)


def _material_path(level_path: Path, surface: dict[str, str]) -> Path:
    return pack_dir(level_path) / "materials" / f"{surface['id']}.md"


def kit_path(level_path: Path) -> Path:
    return pack_dir(level_path) / f"{level_path.stem}-kit.md"


def _sheet(ws_dir: Path, level_path: Path, trim: bool) -> str:
    """The newest prop or trim sheet of a level."""
    folder = output_dir(level_path) / "sheets"
    if not folder.is_dir():
        return ""
    sheets = sorted((p for p in folder.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} and game_tools.is_trim_sheet(p.name) == trim), key=lambda p: p.stat().st_mtime, reverse=True)
    return _ws_rel(ws_dir, sheets[0]) if sheets else ""


def _surface_done(level_path: Path, surface: dict[str, str]) -> bool:
    path = _material_path(level_path, surface)
    return path.is_file() and all(p.is_file() for p in modular.material_paths(path).values())


def _item_done(level_path: Path, meta: dict[str, Any], item: str) -> bool:
    if item == "props":
        return bool(meta.get("props_cut"))
    if item == "trim":
        return bool(meta.get("trim_cut"))
    surface = next(s for s in SURFACES if s["id"] == item)
    return _surface_done(level_path, surface)


def _ensure_material(ws_dir: Path, level_path: Path, meta: dict[str, Any], surface: dict[str, str]) -> str:
    path = _material_path(level_path, surface)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        place = " ".join(str(meta.get("prompt") or "").split())[:300]
        fields = {"kind": "material", "title": surface["label"], "prompt": f"{surface['what'][0].upper()}{surface['what'][1:]} of this place: {place}", "made_from": _ws_rel(ws_dir, level_path)}
        path.write_text(serialize_frontmatter(fields, f"# {surface['label']}\n\nMade from {level_path.stem}.\n"), encoding="utf-8")
    return _ws_rel(ws_dir, path)


def surface_request(ws_dir: Path, level_rel: str, material_rel: str, winner: str) -> dict[str, Any]:
    """The material's usual request, with the level picture attached to match."""
    request = asset_studio.build_request(ws_dir, material_rel, "candidates")
    request["references"] = [{"path": winner, "source": "Level picture", "role": "source"}, *request["references"]][: asset_studio.MAX_REFERENCES]
    request["parts"] = [*request.get("parts", []), {"source": "Level picture", "text": SURFACE_RULE, "role": "rule"}]
    request["prompt"] = f"{request['prompt']}\n\n{SURFACE_RULE}"
    request["from_level"] = level_rel
    return request


async def _cut_sheet(ws_dir: Path, level_rel: str, sheet: str, name_items: Callable[..., Awaitable[dict[str, Any]]]) -> str:
    """Cut a sheet into assets. Returns what went wrong, if anything."""
    boxes = await asyncio.to_thread(game_tools.detect_sheet_items, ws_dir, sheet)
    if not boxes:
        return f"Nothing could be found on {Path(sheet).name}: open the level and press Cut into props on it to draw the boxes."
    named = await game_tools.name_sheet_items(ws_dir, sheet, boxes, name_items)
    for box, name in zip(boxes, named["names"]):
        box["name"] = name
    await asyncio.to_thread(game_tools.split_image, ws_dir, level_rel, sheet, boxes, True)
    return ""


def _build_kit(ws_dir: Path, level_path: Path) -> str:
    """Create or update the level's kit from every surface that is ready, leaving the user's own settings alone."""
    path = kit_path(level_path)
    rel = _ws_rel(ws_dir, path) if path.is_file() else ""
    if not rel:
        path.parent.mkdir(parents=True, exist_ok=True)
        title = f"{level_path.stem.replace('-', ' ').title()} kit"
        path.write_text(serialize_frontmatter({"kind": "kit", "title": title, "prompt": f"Modular pieces for {level_path.stem}", "made_from": _ws_rel(ws_dir, level_path)}, f"# {title}\n"), encoding="utf-8")
        rel = _ws_rel(ws_dir, path)
    fields: dict[str, Any] = {"kit_props": _ws_rel(ws_dir, level_path.parent / f"{level_path.stem}-props"), "kit_trim": _ws_rel(ws_dir, pack_dir(level_path) / "trim")}
    for surface in SURFACES:
        if _surface_done(level_path, surface):
            fields[f"kit_{surface['slot']}"] = _ws_rel(ws_dir, _material_path(level_path, surface))
    update_asset(ws_dir, rel, fields)
    modular.build_kit(ws_dir, rel)
    return rel


def plan(ws_dir: Path, level_rel: str) -> list[dict[str, Any]]:
    """What the pack can hold. A 2D project gets its props only: kits are 3D pieces."""
    level_path, meta, _ = read_asset(ws_dir, level_rel)
    if meta.get("kind") != "level":
        raise ValueError("Only a level can become an asset pack.")
    sheets = [{**s, "group": "sheet"} for s in SHEETS]
    items = sheets[:1] if project_dimension(ws_dir) == "2d" else [*[{"id": s["id"], "label": s["label"], "group": "surface", "hint": ""} for s in SURFACES], *sheets]
    return [{**item, "done": _item_done(level_path, meta, item["id"])} for item in items]


async def run_pack(ws_dir: Path, level_rel: str, wanted: list[str], run_job: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]], name_items: Callable[..., Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Make what's wanted and not done yet, then do the free work. One failed image doesn't stop the rest."""
    level_path, meta, _ = read_asset(ws_dir, level_rel)
    winner = str(meta.get("winner") or "")
    if not winner:
        raise ValueError("Pick a level picture first.")
    known = {item["id"] for item in plan(ws_dir, level_rel)}
    todo = [item for item in wanted if item in known and not _item_done(level_path, meta, item)]
    notes: list[str] = []
    update_asset(ws_dir, level_rel, {"pack_note": ""})

    async def surface_step(surface: dict[str, str]) -> None:
        material = _ensure_material(ws_dir, level_path, meta, surface)
        current = read_asset(ws_dir, material)[1]
        if not current.get("winner"):
            job = await run_job(surface_request(ws_dir, level_rel, material, winner))
            if job.get("status") != "done" or not job.get("outputs"):
                notes.append(f"{surface['label']}: {job.get('error') or 'no image came back'}")
                return
            update_asset(ws_dir, material, {"winner": job["outputs"][0]})
        await asyncio.to_thread(modular.build_material, ws_dir, material)

    async def sheet_step(item: str) -> None:
        trim = item == "trim"
        sheet = _sheet(ws_dir, level_path, trim)
        if not sheet:
            request = asset_studio.build_request(ws_dir, level_rel, "trim_sheet" if trim else "prop_sheet")
            job = await run_job(request)
            if job.get("status") != "done" or not job.get("outputs"):
                notes.append(f"{'Trim' if trim else 'Props'}: {job.get('error') or 'no image came back'}")
                return
            sheet = job["outputs"][0]
        if problem := await _cut_sheet(ws_dir, level_rel, sheet, name_items):
            notes.append(problem)

    steps = [surface_step(s) for s in SURFACES if s["id"] in todo] + [sheet_step(item) for item in todo if item in ("props", "trim")]
    await asyncio.gather(*steps)

    kit = ""
    if any(_surface_done(level_path, s) for s in SURFACES):
        kit = await asyncio.to_thread(_build_kit, ws_dir, level_path)
    update_asset(ws_dir, level_rel, {"pack_note": " ".join(notes)[:600], **({"pack_kit": kit} if kit else {})})
    return {"kit": kit, "notes": notes}


def _pickable(ws_dir: Path, folder: Path, group: str) -> list[dict[str, Any]]:
    if not folder.is_dir():
        return []
    found = []
    for rel in assets_under(ws_dir, _ws_rel(ws_dir, folder)):
        asset = asset_studio.describe_asset(ws_dir, rel)
        if asset["kind"] in ("material", "kit") or not asset["winner"]:
            continue
        models = [o for o in asset["outputs"]["3d"] if o["type"] == "model"]
        found.append({"asset": rel, "title": str(asset["meta"].get("title") or asset["prompt"] or Path(rel).stem), "image": asset["winner"], "group": group, "model": models[0]["path"] if models else ""})
    return found


def status(ws_dir: Path, level_rel: str) -> dict[str, Any]:
    level_path, meta, _ = read_asset(ws_dir, level_rel)
    kit = kit_path(level_path)
    surfaces = []
    for surface in SURFACES:
        path = _material_path(level_path, surface)
        if path.is_file():
            current = read_asset(ws_dir, _ws_rel(ws_dir, path))[1]
            surfaces.append({"asset": _ws_rel(ws_dir, path), "title": surface["label"], "image": str(current.get("winner") or ""), "ready": _surface_done(level_path, surface)})
    return {
        "items": plan(ws_dir, level_rel),
        "note": str(meta.get("pack_note") or ""),
        "kit": _ws_rel(ws_dir, kit) if kit.is_file() else "",
        "surfaces": surfaces,
        "pickable": _pickable(ws_dir, level_path.parent / f"{level_path.stem}-props", "Props") + _pickable(ws_dir, pack_dir(level_path) / "trim", "Pillars, arches and frames"),
        "dimension": project_dimension(ws_dir),
    }


def make_3d_request(ws_dir: Path, asset_rel: str) -> dict[str, Any]:
    """A prop's Make 3D request, kept to a game-ready polycount."""
    request = asset_studio.build_request(ws_dir, asset_rel, "make_3d")
    request["settings"] = {**request.get("settings", {}), "should_remesh": True, "target_polycount": TRIM_MESH_POLYCOUNT}
    return request


def list_levels(ws_dir: Path) -> list[dict[str, Any]]:
    levels = []
    for rel in assets_under(ws_dir):
        path, meta, body = read_asset(ws_dir, rel)
        if meta.get("kind") != "level":
            continue
        heading = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), "")
        levels.append({
            "path": rel, "title": str(meta.get("title") or heading or path.stem.replace("-", " ").title()), "image": str(meta.get("winner") or ""),
            "props": int(meta.get("props_cut") or 0), "kit": bool(kit_path(path).is_file()),
        })
    return levels


def new_level_folder(ws_dir: Path) -> str:
    """Where a new level goes: beside the project's other levels, else a Levels folder."""
    existing = [Path(level["path"]).parent.as_posix() for level in list_levels(ws_dir)]
    plain = [folder for folder in existing if not folder.endswith(("-props", "-pack", "/trim"))]
    return plain[0] if plain and plain[0] != "." else "levels"

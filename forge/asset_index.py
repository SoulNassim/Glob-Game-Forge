"""`glob/ASSETS.md`: every asset of the project and its finished files, for the game's coding agent.

The coding agent never manages the workspace. It reads this index, copies the files it needs into the game's own
folders, and leaves `glob/` alone. Glob rewrites the file whenever the workspace changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forge.storage.project_store import GAME_HOME

INDEX_NAME = "ASSETS.md"


def index_path(ws_dir: Path) -> Path:
    return ws_dir.parent / INDEX_NAME


GAME_SIZE_PX = 1024
GAME_SIZE_DIR = "exports/game"


def _game_size_copy(ws_dir: Path, winner: str, rel: str) -> str:
    """A copy of the picked image no bigger than GAME_SIZE_PX, for games that don't want 3 MB textures.

    Returns its path relative to the workspace's parent (the game's glob/ folder), or "" when the original is
    already small, isn't an image Pillow can read, or the copy can't be written. The original is never touched.
    """
    source = ws_dir / winner
    if not winner or source.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp") or not source.is_file():
        return ""
    target = ws_dir.parent / GAME_SIZE_DIR / f"{Path(rel).with_suffix('').as_posix()}.png"
    try:
        from PIL import Image

        if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
            return target.relative_to(ws_dir.parent).as_posix()
        with Image.open(source) as img:
            if max(img.size) <= GAME_SIZE_PX and source.stat().st_size < 600_000:
                return ""
            img.thumbnail((GAME_SIZE_PX, GAME_SIZE_PX), Image.LANCZOS)
            target.parent.mkdir(parents=True, exist_ok=True)
            img.save(target, optimize=True)
        return target.relative_to(ws_dir.parent).as_posix()
    except Exception:  # noqa: BLE001 — a bad image must never stop the index from being written
        return ""


def _rows(ws_dir: Path) -> list[dict[str, Any]]:
    from forge.asset_studio import assets_under, list_outputs, read_asset

    rows = []
    for rel in assets_under(ws_dir):
        try:
            path, meta, _ = read_asset(ws_dir, rel)
        except (OSError, ValueError):
            continue
        outputs = list_outputs(ws_dir, path)
        first = lambda kind: next((o["path"] for o in outputs[kind] if o["type"] == "model"), "")  # noqa: E731
        rows.append({
            "rel": rel,
            "title": str(meta.get("title") or path.stem.replace("-", " ").replace("_", " ").title()),
            "kind": str(meta.get("kind") or ""),
            "winner": str(meta.get("winner") or ""),
            "model": (next((o["path"] for o in outputs["3d"] if o["type"] == "model" and Path(o["path"]).stem == path.stem), "")
                      if meta.get("kind") == "kit" else first("3d")),
            "kit_dir": f"{Path(rel).with_suffix('').as_posix()}/3d/" if meta.get("kind") == "kit" and outputs["3d"] else "",
            "maps_dir": f"{Path(rel).with_suffix('').as_posix()}/textures/" if meta.get("kind") == "material" and outputs["textures"] else "",
            "small": _game_size_copy(ws_dir, str(meta.get("winner") or ""), rel) if meta.get("kind") not in ("kit", "material") else "",
            "rig": first("rig"),
            "animations": len(outputs["animations"]),
            "anim_dir": f"{Path(rel).with_suffix('').as_posix()}/animations/",
        })
    return rows


def render(ws_dir: Path, title: str = "") -> str:
    from forge.overview import humanize

    # Paths are written from the game folder when the project lives in it (<game>/glob/workspace).
    prefix = f"{GAME_HOME}/{ws_dir.name}/" if ws_dir.parent.name == GAME_HOME else f"{ws_dir.name}/"
    game_prefix = f"{GAME_HOME}/" if ws_dir.parent.name == GAME_HOME else ""
    rows = _rows(ws_dir)
    done = sum(1 for r in rows if r["winner"] or r["model"])
    lines = [
        f"# {title + ': ' if title else ''}game art from Glob",
        "",
        "Written by Glob, the asset studio where this game's art is made. Glob rewrites this file whenever the art changes.",
        "",
        "- Copy the files you need into the game's own asset folders, then use the copies.",
        f"- Glob's picked images are full size (often 1-3 MB). Where a lighter copy exists it is listed under Other files as \"game-size copy\" ({GAME_SIZE_PX}px max): prefer it. Never resize the originals.",
        "- Don't edit, move or delete anything inside `glob/`: it is the user's Glob workspace.",
        "- Art that isn't made yet: keep a placeholder and tell the user what's missing. They make it in Glob.",
        "- A 3D game whose asset only has a picked image (no 3D model yet): show that image as a flat sprite or camera-facing billboard now, and swap in the model when its row gets one. The image is a reference sheet, not the final look in the game.",
        f"- Paths are relative to the game folder. {done} of {len(rows)} assets have finished files.",
    ]
    sections: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        top = row["rel"].split("/")[0] if "/" in row["rel"] else ""
        sections.setdefault(top, []).append(row)
    for top, items in sections.items():
        lines += ["", f"## {humanize(top) if top else 'Project'}", "", "| Asset | Kind | Picked image or sound | 3D model | Other files |", "|---|---|---|---|---|"]
        for r in items:
            cell = lambda p: f"`{prefix}{p}`" if p else "not made yet"  # noqa: E731
            extra = [f"rig `{prefix}{r['rig']}`"] if r["rig"] else []
            if r["small"]:
                extra.append(f"game-size copy ({GAME_SIZE_PX}px max, lighter) `{game_prefix}{r['small']}`")
            if r["animations"]:
                extra.append(f"{r['animations']} clip{'s' if r['animations'] != 1 else ''} in `{prefix}{r['anim_dir']}`")
            if r["kit_dir"]:
                extra.append(f"modular kit: read `{prefix}{r['kit_dir']}kit.json` for how the pieces fit")
            if r["maps_dir"]:
                extra.append(f"tiling maps (albedo, normal, roughness, height, orm) in `{prefix}{r['maps_dir']}`")
            # A kit has no picture and a material no model: "not made yet" would send the reader after art that never comes.
            picture = "—" if r["kind"] == "kit" else cell(r["winner"])
            model = "—" if r["kind"] == "material" else cell(r["model"])
            lines.append(f"| {r['title']} | {r['kind']} | {picture} | {model} | {', '.join(extra) or '—'} |")
    return "\n".join(lines) + "\n"


def write(ws_dir: Path, title: str = "") -> Path | None:
    """Rewrite the index when it changed. None when the workspace doesn't exist."""
    if not ws_dir.is_dir():
        return None
    target = index_path(ws_dir)
    text = render(ws_dir, title)
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            return target
        target.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return target

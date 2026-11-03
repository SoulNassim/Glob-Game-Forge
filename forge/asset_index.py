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
            "model": first("3d"),
            "rig": first("rig"),
            "animations": len(outputs["animations"]),
            "anim_dir": f"{Path(rel).with_suffix('').as_posix()}/animations/",
        })
    return rows


def render(ws_dir: Path, title: str = "") -> str:
    from forge.overview import humanize

    # Paths are written from the game folder when the project lives in it (<game>/glob/workspace).
    prefix = f"{GAME_HOME}/{ws_dir.name}/" if ws_dir.parent.name == GAME_HOME else f"{ws_dir.name}/"
    rows = _rows(ws_dir)
    done = sum(1 for r in rows if r["winner"] or r["model"])
    lines = [
        f"# {title + ': ' if title else ''}game art from Glob",
        "",
        "Written by Glob, the asset studio where this game's art is made. Glob rewrites this file whenever the art changes.",
        "",
        "- Copy the files you need into the game's own asset folders, then use the copies.",
        "- Don't edit, move or delete anything inside `glob/`: it is the user's Glob workspace.",
        "- Art that isn't made yet: keep a placeholder and tell the user what's missing. They make it in Glob.",
        f"- Paths are relative to the game folder. {done} of {len(rows)} assets have finished files.",
    ]
    sections: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        top = row["rel"].split("/")[0] if "/" in row["rel"] else ""
        sections.setdefault(top, []).append(row)
    for top, items in sections.items():
        lines += ["", f"## {humanize(top) if top else 'Project'}", "", "| Asset | Kind | Picked image or sound | 3D model | Rig and animations |", "|---|---|---|---|---|"]
        for r in items:
            cell = lambda p: f"`{prefix}{p}`" if p else "not made yet"  # noqa: E731
            extra = [f"rig `{prefix}{r['rig']}`"] if r["rig"] else []
            if r["animations"]:
                extra.append(f"{r['animations']} clip{'s' if r['animations'] != 1 else ''} in `{prefix}{r['anim_dir']}`")
            lines.append(f"| {r['title']} | {r['kind']} | {cell(r['winner'])} | {cell(r['model'])} | {', '.join(extra) or '—'} |")
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

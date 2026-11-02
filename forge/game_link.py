"""The one game folder a Glob project belongs to (`projects/<name>/game_link.json`)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from forge.config import get_projects_root

LINK_FILE = "game_link.json"


def _path(project: str, root: Path | None = None) -> Path:
    return (root or get_projects_root()) / project / LINK_FILE


def get_link(project: str, root: Path | None = None) -> dict[str, Any]:
    """{"game_folder": "/abs/path", "targets": [...], "connected_at": ts} or {} when the project is not linked."""
    try:
        data = json.loads(_path(project, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("game_folder") else {}


def game_folder(project: str, root: Path | None = None) -> Path | None:
    folder = get_link(project, root).get("game_folder")
    return Path(folder) if folder else None


def set_link(project: str, folder: Path, targets: list[str], root: Path | None = None) -> dict[str, Any]:
    path = _path(project, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = get_link(project, root) | {"game_folder": str(folder), "targets": targets, "connected_at": time.time()}
    data.setdefault("auto_deliver", True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def update_link(project: str, fields: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    data = get_link(project, root)
    if not data:
        raise ValueError("Connect a game folder first.")
    data.update(fields)
    _path(project, root).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def clear_link(project: str, root: Path | None = None) -> None:
    _path(project, root).unlink(missing_ok=True)


def linked_projects(root: Path | None = None) -> list[str]:
    base = root or get_projects_root()
    return sorted(p.parent.name for p in base.glob(f"*/{LINK_FILE}") if get_link(p.parent.name, base))

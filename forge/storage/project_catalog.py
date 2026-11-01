"""Persistent project defaults and organizational folders."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from forge.config import get_projects_root
from forge.storage.project_store import list_all_projects


_LOCK = threading.RLock()
_CATALOG_FILE = ".forge-project-catalog.json"


def _root(root_dir: Path | None = None) -> Path:
    return Path(root_dir or get_projects_root()).resolve()


def _catalog_path(root: Path) -> Path:
    return root / _CATALOG_FILE


def _read(root: Path) -> dict[str, Any]:
    try:
        data = json.loads(_catalog_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    projects = set(list_all_projects(root))
    folders = data.get("folders", []) if isinstance(data, dict) else []
    folders = [name for name in folders if isinstance(name, str) and _valid_folder_name(name)]
    folders = list(dict.fromkeys(folders))
    assignments = data.get("project_folders", {}) if isinstance(data, dict) else {}
    assignments = {
        project: folder
        for project, folder in assignments.items()
        if isinstance(project, str) and project in projects and folder in folders
    } if isinstance(assignments, dict) else {}
    default_project = data.get("default_project", "default") if isinstance(data, dict) else "default"
    if not isinstance(default_project, str) or default_project not in projects:
        default_project = "default"
    return {
        "default_project": default_project,
        "folders": folders,
        "project_folders": assignments,
    }


def _write(root: Path, data: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    target = _catalog_path(root)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(target)


def get_project_catalog(root_dir: Path | None = None) -> dict[str, Any]:
    root = _root(root_dir)
    with _LOCK:
        data = _read(root)
        return {"projects": list_all_projects(root), **data}


def get_default_project(root_dir: Path | None = None) -> str:
    return get_project_catalog(root_dir)["default_project"]


def set_default_project(project: str, root_dir: Path | None = None) -> dict[str, Any]:
    root = _root(root_dir)
    with _LOCK:
        data = _read(root)
        if project not in list_all_projects(root) or (project != "default" and not (root / project).is_dir()):
            raise ValueError(f"Project '{project}' was not found.")
        data["default_project"] = project
        _write(root, data)
        return {"projects": list_all_projects(root), **data}


def _valid_folder_name(name: str) -> bool:
    return bool(name and name == name.strip() and len(name) <= 64 and name not in {".", ".."}
                and not any(char in name for char in ("/", "\\", "\x00")))


def create_project_folder(name: str, root_dir: Path | None = None) -> dict[str, Any]:
    root = _root(root_dir)
    name = name.strip() if isinstance(name, str) else ""
    if not _valid_folder_name(name):
        raise ValueError("Folder names must be 1–64 characters and cannot contain path separators.")
    with _LOCK:
        data = _read(root)
        if any(folder.casefold() == name.casefold() for folder in data["folders"]):
            raise ValueError("A folder with that name already exists.")
        data["folders"].append(name)
        _write(root, data)
        return {"projects": list_all_projects(root), **data}


def delete_project_folder(name: str, root_dir: Path | None = None) -> dict[str, Any]:
    root = _root(root_dir)
    with _LOCK:
        data = _read(root)
        if name not in data["folders"]:
            raise ValueError(f"Folder '{name}' was not found.")
        data["folders"].remove(name)
        data["project_folders"] = {
            project: folder for project, folder in data["project_folders"].items() if folder != name
        }
        _write(root, data)
        return {"projects": list_all_projects(root), **data}


def assign_project_folder(projects: list[str], folder: str | None, root_dir: Path | None = None) -> dict[str, Any]:
    root = _root(root_dir)
    with _LOCK:
        data = _read(root)
        if folder is not None and folder not in data["folders"]:
            raise ValueError(f"Folder '{folder}' was not found.")
        available = set(list_all_projects(root))
        missing = [project for project in projects if project not in available]
        if missing:
            raise ValueError(f"Project '{missing[0]}' was not found.")
        for project in set(projects):
            if folder is None:
                data["project_folders"].pop(project, None)
            else:
                data["project_folders"][project] = folder
        _write(root, data)
        return {"projects": list_all_projects(root), **data}


def remove_project_from_catalog(project: str, root_dir: Path | None = None) -> None:
    root = _root(root_dir)
    with _LOCK:
        data = _read(root)
        data["project_folders"].pop(project, None)
        if data["default_project"] == project:
            data["default_project"] = "default"
        _write(root, data)

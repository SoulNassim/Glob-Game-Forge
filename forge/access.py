"""Folders outside the projects folder that the user has let Glob use.

Glob's agent works inside a project's workspace. To read a game project somewhere else on the
computer (or write into it), the folder has to be granted first: in Settings → Storage & access,
during first-run setup, or by answering the agent's access request in the chat. Grants are stored
in ~/.forge/config.json as {"path", "write"} and can only be changed through the dedicated API,
never through PUT /api/config.

Secrets stay out of reach even inside a granted folder: Glob's own config folder, key files,
.env files and the like are refused.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from forge.config import get_config_dir, load_config, save_config
from forge.gamescan.safety import SECRET_FILE_PATTERNS

MAX_GRANTS = 40


def _norm(path: str | Path) -> Path:
    return Path(str(path)).expanduser().resolve()


def list_grants() -> list[dict[str, Any]]:
    grants = []
    for entry in load_config().access_folders:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"].strip():
            path = _norm(entry["path"])
            grants.append({"path": str(path), "write": bool(entry.get("write")), "exists": path.is_dir()})
    return grants


def grant(path: str, write: bool = False) -> list[dict[str, Any]]:
    target = _norm(path)
    if not target.is_dir():
        raise ValueError(f"'{path}' is not a folder on this computer.")
    if target == Path(target.anchor):
        raise ValueError("Grant a project folder, not a whole drive.")
    config_dir = get_config_dir().resolve()
    if target == config_dir or target.is_relative_to(config_dir):
        raise ValueError("Glob's settings folder (with your API keys) can't be shared with the agent.")
    grants = [g for g in list_grants() if _norm(g["path"]) != target]
    grants.append({"path": str(target), "write": bool(write)})
    save_config({"access_folders": [{"path": g["path"], "write": g["write"]} for g in grants[-MAX_GRANTS:]]})
    return list_grants()


def revoke(path: str) -> list[dict[str, Any]]:
    target = _norm(path)
    save_config({"access_folders": [{"path": g["path"], "write": g["write"]} for g in list_grants() if _norm(g["path"]) != target]})
    return list_grants()


def is_secret(path: Path) -> bool:
    config_dir = get_config_dir().resolve()
    resolved = path.resolve()
    if resolved == config_dir or resolved.is_relative_to(config_dir):
        return True
    name = resolved.name.lower()
    return any(fnmatch.fnmatch(name, pattern.lower()) for pattern in SECRET_FILE_PATTERNS)


def granted_path(raw: str, need_write: bool = False) -> Path | None:
    """The resolved path when it lies inside a granted folder (with write access if needed), else None."""
    if not raw:
        return None
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        return None
    target = candidate.resolve()
    for entry in list_grants():
        root = _norm(entry["path"])
        if (target == root or target.is_relative_to(root)) and (entry["write"] or not need_write):
            return target
    return None


def check(raw: str, need_write: bool = False) -> Path:
    """Resolve an absolute path the agent asked for, or raise PermissionError explaining how to get access."""
    target = granted_path(raw, need_write)
    if target is None:
        if granted_path(raw):
            raise PermissionError(f"Glob may read '{raw}' but not write there. Ask the user for write access.")
        raise PermissionError(f"No access to '{raw}' yet. Use request_access so the user can allow it.")
    if is_secret(target):
        raise PermissionError(f"'{target.name}' looks like a secret (keys, credentials); Glob never reads it.")
    return target

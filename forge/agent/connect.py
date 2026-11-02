"""Connect a coding agent working in the user's game repo to Glob.

Writes the MCP server entry for each agent the user picks, a short note in AGENTS.md / CLAUDE.md / GEMINI.md and
a `glob` skill, so the agent knows Glob exists without the user ever saying "use Glob". Everything Glob writes
sits between `glob:start` / `glob:end` markers (or under the `glob` key in JSON), so the user's own content
is never touched and reconnecting just refreshes Glob's part.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from forge import access, game_link
from forge.config import get_projects_root

DELIVERY_DIR = "glob_delivery"

# id -> label, the files that agent reads, and the folder whose presence suggests the user has that agent.
TARGETS: dict[str, dict[str, Any]] = {
    "claude": {"label": "Claude Code", "files": [".mcp.json", "CLAUDE.md", ".claude/skills/glob/SKILL.md"], "hint": ".claude"},
    "codex": {"label": "Codex", "files": [".codex/config.toml", ".agents/skills/glob/SKILL.md"], "hint": ".codex"},
    "cursor": {"label": "Cursor", "files": [".cursor/mcp.json"], "hint": ".cursor"},
    "vscode": {"label": "VS Code / Copilot", "files": [".vscode/mcp.json"], "hint": ".vscode"},
    "opencode": {"label": "OpenCode", "files": ["opencode.json"], "hint": "opencode.json"},
    "gemini": {"label": "Gemini CLI", "files": [".gemini/settings.json", "GEMINI.md"], "hint": ".gemini"},
}
DEFAULT_TARGETS = ("claude", "codex")

MD_START, MD_END = "<!-- glob:start -->", "<!-- glob:end -->"
TOML_START, TOML_END = "# glob:start", "# glob:end"


def server_entry(project: str, root: Path | None = None) -> dict[str, Any]:
    """How an agent starts Glob's MCP server for this project, independent of the agent's working directory."""
    # The packaged desktop app is a single binary that also understands --mcp; otherwise run the installed package.
    launcher = [] if getattr(sys, "frozen", False) else ["-m", "forge.cli"]
    return {
        "command": sys.executable,
        "args": [*launcher, "--mcp", "--project", project],
        "env": {"FORGE_PROJECTS_DIR": str((root or get_projects_root()).resolve())},
    }


def agent_note(project: str) -> str:
    return f"""{MD_START}
## Game art and audio come from Glob

This game is connected to **Glob**, the asset studio (MCP server `glob`, project "{project}"). Glob makes the
final art, 3D models, animations and sounds. Do not draw, download or invent final assets yourself.

- **Need an asset?** Keep a placeholder in the game for now and order the real one with the `place_order` tool:
  what it is, its kind, and the placeholder path it will replace. One order per asset, and add as much
  context as you can (role in the game, size, style, faction). Order early: the user produces assets in Glob
  while you keep coding.
- **"Open Glob" / "run Glob"?** Call `open_glob`: it starts Glob on this computer if needed and opens this
  project in the browser, where the user sees every order, prompt, image and model.
- **What's the status?** `list_orders` shows every order: draft, filed, in progress, ready, delivered.
- **Deliveries** land in `{DELIVERY_DIR}/`, one folder per order (`{DELIVERY_DIR}/<order_id>/`), described in
  `{DELIVERY_DIR}/manifest.json`. Read `{DELIVERY_DIR}/README.md`, swap your placeholder for the delivered
  file, then call `update_order` with `status: integrated`.
- **Organizing the asset workspace** (sorting orders into sections, writing asset briefs, generating) is the
  Glob role: use the `glob` skill, ideally in a separate session.
{MD_END}"""


def skill_text(project: str) -> str:
    from forge.mcp.server import glob_session_prompt

    return (
        "---\nname: glob\ndescription: Become Glob, the manager of this game's asset workspace in the Glob app: sort "
        "agent orders into the workspace, write asset briefs, generate art, 3D models and audio, pick winners. Use "
        "when the user says \"glob\" or asks to organize or produce game assets.\n---\n\n"
        "<!-- Written by Glob; reconnecting the game in Glob refreshes this file. -->\n\n"
        + glob_session_prompt(project)
    )


def delivery_readme(project: str) -> str:
    return f"""# Glob deliveries

This folder is written by **Glob**, the asset studio this game is connected to (project "{project}").
Every finished asset that was ordered with `place_order` lands here automatically. Glob only ever writes inside
this folder, so it never changes your own files.

## Layout

```
{DELIVERY_DIR}/
  manifest.json            every delivered order (read this first)
  <order_id>/              one folder per order
    <order_id>.png         2D art (concept, sprite, icon, texture)
    <order_id>.glb         3D model (plus .fbx / .obj when made)
    animations/            rig and animation clips
    <order_id>.mp3|.wav    audio (sound effect, voice line, music)
```

`manifest.json` maps each order id to its title, kind, the placeholder it replaces, its files and their SHA-256.

## For the coding agent

1. Find your order by id in `manifest.json` (or call the `get_deliveries` tool of the `glob` MCP server).
2. Replace the placeholder with the delivered file. Import or convert it the way this engine needs.
3. Call `update_order` with `status: integrated` so the user sees it is in the game.

A newer delivery of the same order replaces its folder. Do not edit files in here: copy them into the game's
own asset folders if you need to change them.
"""


# --- file writers -----------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_key(path: Path, section: str, value: dict[str, Any] | None) -> None:
    data = _read_json(path) if path.is_file() else {}
    if path.is_file() and not data and path.read_text(encoding="utf-8", errors="ignore").strip():
        raise ValueError(f"{path.name} is not plain JSON (comments?); add the glob server there by hand.")
    servers = dict(data.get(section) or {})
    if value is None:
        if "glob" not in servers:
            return
        servers.pop("glob")
    else:
        servers["glob"] = value
    data[section] = servers
    if value is None and data == {section: {}}:
        path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _replace_block(text: str, start: str, end: str, block: str | None) -> str:
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.S)
    if block is None:
        return pattern.sub("", text).rstrip() + ("\n" if text.strip() else "")
    if pattern.search(text):
        return pattern.sub(lambda _m: block + "\n", text)
    return (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"


def _write_block(path: Path, start: str, end: str, block: str | None) -> None:
    text = path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else ""
    if block is None and not text:
        return
    result = _replace_block(text, start, end, block)
    if block is None and not result.strip():
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result, encoding="utf-8")


def _toml_block(entry: dict[str, Any]) -> str:
    env = ", ".join(f"{key} = {json.dumps(value)}" for key, value in entry["env"].items())
    return "\n".join([
        TOML_START,
        "[mcp_servers.glob]",
        f"command = {json.dumps(entry['command'])}",
        f"args = {json.dumps(entry['args'])}",
        f"env = {{ {env} }}",
        TOML_END,
    ])


def _write_target(game: Path, target: str, project: str, entry: dict[str, Any] | None) -> None:
    note = agent_note(project) if entry else None
    if target == "claude":
        _write_json_key(game / ".mcp.json", "mcpServers", entry and {"type": "stdio", **entry})
        _write_block(game / "CLAUDE.md", MD_START, MD_END, note)
        _write_skill(game / ".claude/skills/glob/SKILL.md", project, entry is not None)
    elif target == "codex":
        _write_block(game / ".codex/config.toml", TOML_START, TOML_END, entry and _toml_block(entry))
        _write_skill(game / ".agents/skills/glob/SKILL.md", project, entry is not None)
    elif target == "cursor":
        _write_json_key(game / ".cursor/mcp.json", "mcpServers", entry)
    elif target == "vscode":
        _write_json_key(game / ".vscode/mcp.json", "servers", entry and {"type": "stdio", **entry})
    elif target == "opencode":
        value = entry and {"type": "local", "command": [entry["command"], *entry["args"]], "environment": entry["env"], "enabled": True}
        _write_json_key(game / "opencode.json", "mcp", value)
    elif target == "gemini":
        _write_json_key(game / ".gemini/settings.json", "mcpServers", entry)
        _write_block(game / "GEMINI.md", MD_START, MD_END, note)


def _write_skill(path: Path, project: str, keep: bool) -> None:
    if keep:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(skill_text(project), encoding="utf-8")
    elif path.is_file():
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass


# --- public API --------------------------------------------------------------------


def detect_targets(game: Path) -> list[str]:
    """Agents that look set up in this folder or are installed on this computer, plus the defaults."""
    from forge.agent import cli_models

    found = [tid for tid, meta in TARGETS.items() if (game / meta["hint"]).exists()]
    found += [c["id"] for c in cli_models.detect() if c["installed"] and c["id"] in TARGETS]
    return sorted(set(found) | set(DEFAULT_TARGETS), key=list(TARGETS).index)


def connect(project: str, folder: str, targets: list[str] | None = None, root: Path | None = None) -> dict[str, Any]:
    game = Path(folder).expanduser()
    if not game.is_dir():
        raise ValueError(f"'{folder}' is not a folder.")
    game = game.resolve()
    chosen = [t for t in (targets or detect_targets(game)) if t in TARGETS] or list(DEFAULT_TARGETS)
    previous = set(game_link.get_link(project, root).get("targets") or [])
    entry = server_entry(project, root)
    for target in chosen:
        _write_target(game, target, project, entry)
    for target in previous - set(chosen):
        _write_target(game, target, project, None)
    _write_block(game / "AGENTS.md", MD_START, MD_END, agent_note(project))
    delivery = game / DELIVERY_DIR
    delivery.mkdir(exist_ok=True)
    (delivery / "README.md").write_text(delivery_readme(project), encoding="utf-8")
    access.grant(str(game), write=True)
    game_link.set_link(project, game, chosen, root)
    return status(project, root)


def disconnect(project: str, root: Path | None = None) -> dict[str, Any]:
    link = game_link.get_link(project, root)
    game = Path(link["game_folder"]) if link else None
    if game and game.is_dir():
        for target in link.get("targets") or []:
            if target in TARGETS:
                _write_target(game, target, project, None)
        _write_block(game / "AGENTS.md", MD_START, MD_END, None)
        for leftover in (".claude/skills", ".claude", ".agents/skills", ".agents", ".codex", ".cursor", ".vscode", ".gemini"):
            try:
                (game / leftover).rmdir()  # only removes folders that are now empty
            except OSError:
                pass
    game_link.clear_link(project, root)
    return status(project, root)


def refresh(project: str, root: Path | None = None) -> None:
    """Rewrite Glob's part of the connected game's files (after an update changed the prompt or paths)."""
    link = game_link.get_link(project, root)
    if link and Path(link["game_folder"]).is_dir():
        connect(project, link["game_folder"], link.get("targets"), root)


def status(project: str, root: Path | None = None) -> dict[str, Any]:
    link = game_link.get_link(project, root)
    targets = [{"id": tid, "label": meta["label"], "files": meta["files"]} for tid, meta in TARGETS.items()]
    if not link:
        return {"connected": False, "targets": targets}
    game = Path(link["game_folder"])
    entry = server_entry(project, root)
    healthy = game.is_dir()
    for target in link.get("targets") or []:
        if target == "claude":
            healthy &= (_read_json(game / ".mcp.json").get("mcpServers") or {}).get("glob", {}).get("args") == entry["args"]
        elif target == "codex":
            healthy &= TOML_START in ((game / ".codex/config.toml").read_text(encoding="utf-8", errors="ignore") if (game / ".codex/config.toml").is_file() else "")
    return {
        "connected": True,
        "game_folder": str(game),
        "game_name": game.name,
        "selected": link.get("targets") or [],
        "auto_deliver": bool(link.get("auto_deliver", True)),
        "healthy": bool(healthy),
        "delivery_folder": str(game / DELIVERY_DIR),
        "targets": targets,
    }


def connect_game(folder: str, targets: list[str] | None = None, root: Path | None = None) -> dict[str, Any]:
    """`glob connect`: the Glob project for this game folder (made the first time, in `<game>/glob/`), connected.

    Does what choosing the game in Glob's New project screen does, for a coding agent that set Glob up from a terminal."""
    from forge.agent.action_layer import ForgeActionLayer
    from forge.storage.project_store import home_owner, make_home, sanitize_project_name

    game = Path(folder).expanduser()
    if not game.is_dir():
        raise ValueError(f"'{folder}' is not a folder.")
    game = game.resolve()
    base = root or get_projects_root()
    name = home_owner(game, base)
    created = name is None
    if created:
        name = stem = sanitize_project_name(game.name) or "my_game"
        index = 2
        while (base / name).exists():
            name, index = f"{stem}-{index}", index + 1
        make_home(name, game, base)
        ForgeActionLayer(root_dir=base).get_store(name)
    return connect(name, str(game), targets, root) | {"project": name, "created": created}

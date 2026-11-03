"""Tell the coding agent working in the user's game repo where Glob's finished art is.

Writes a short note into AGENTS.md / CLAUDE.md / GEMINI.md: the art is listed in glob/ASSETS.md, copy what you
need, don't touch glob/. The coding agent needs no MCP server, no skill and no restart; Glob's own chat manages
the workspace. Earlier versions registered an MCP server and wrote a `glob` skill: connecting removes those.
Everything Glob writes sits between `glob:start` / `glob:end` markers (or under the `glob` key in JSON), so the
user's own content is never touched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from forge import access, game_link
from forge.config import get_projects_root

MD_START, MD_END = "<!-- glob:start -->", "<!-- glob:end -->"
TOML_START, TOML_END = "# glob:start", "# glob:end"


NOTE_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")


def agent_note(project: str, index: str = "glob/ASSETS.md") -> str:
    return f"""{MD_START}
## Game art comes from Glob

The user makes this game's art, 3D models, animations and sounds in **Glob**, a separate asset studio app
(project "{project}"). Don't draw, download or invent final assets yourself, and don't manage Glob's workspace.

- **`{index}`** lists every asset and its finished files: the picked image or sound, the 3D model, the
  rig and the animations. Glob keeps it up to date.
- **Copy** the files you need into the game's own asset folders and use the copies. Don't edit, move or delete
  anything in Glob's folder.
- **Not made yet?** Keep a placeholder and tell the user what's missing. They make it in Glob.
{MD_END}"""


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


def _remove_old_setup(game: Path) -> None:
    """Earlier versions registered Glob's MCP server and a `glob` skill in the game; v3 needs neither."""
    for path, section in ((".mcp.json", "mcpServers"), (".cursor/mcp.json", "mcpServers"), (".vscode/mcp.json", "servers"),
                          ("opencode.json", "mcp"), (".gemini/settings.json", "mcpServers")):
        if (game / path).is_file():
            try:
                _write_json_key(game / path, section, None)
            except ValueError:
                pass  # not plain JSON: leave the user's file alone
    _write_block(game / ".codex/config.toml", TOML_START, TOML_END, None)
    for skill in (".claude/skills/glob/SKILL.md", ".agents/skills/glob/SKILL.md"):
        path = game / skill
        if path.is_file():
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass
    readme = game / "glob_delivery" / "README.md"
    if readme.is_file():
        readme.unlink()
    for leftover in ("glob_delivery", ".claude/skills", ".claude", ".agents/skills", ".agents", ".codex", ".cursor", ".vscode", ".gemini"):
        try:
            (game / leftover).rmdir()  # only removes folders that are now empty
        except OSError:
            pass


# --- public API --------------------------------------------------------------------


def connect(project: str, folder: str, targets: list[str] | None = None, root: Path | None = None) -> dict[str, Any]:  # noqa: ARG001 — targets: older callers
    game = Path(folder).expanduser()
    if not game.is_dir():
        raise ValueError(f"'{folder}' is not a folder.")
    game = game.resolve()
    _remove_old_setup(game)
    _write_game_ignore(project, game, root)
    index = _index_ref(project, game, root)
    for name in NOTE_FILES:
        # AGENTS.md always; CLAUDE.md and GEMINI.md when the game has them (they'd otherwise be made just for this).
        if name == "AGENTS.md" or (game / name).is_file():
            _write_block(game / name, MD_START, MD_END, agent_note(project, index))
    access.grant(str(game), write=True)
    game_link.set_link(project, game, [], root)
    _write_index(project, root)
    return status(project, root)


def _write_game_ignore(project: str, game: Path, root: Path | None) -> None:
    """`<game>/glob/.gitignore` for a project living there, so the game's git skips Glob's internal files."""
    from forge.storage.project_store import GAME_HOME, ProjectStore, write_home_ignore

    home = ProjectStore(project_name=project, root_dir=root).project_dir
    if home == (game / GAME_HOME).resolve():
        write_home_ignore(home)


def _index_ref(project: str, game: Path, root: Path | None) -> str:
    """Where the note points: `glob/ASSETS.md` when the project lives in the game, else the index's full path."""
    from forge import asset_index
    from forge.storage.project_store import ProjectStore

    path = asset_index.index_path(ProjectStore(project_name=project, root_dir=root).project_dir / "workspace")
    try:
        return path.resolve().relative_to(game.resolve()).as_posix()
    except ValueError:
        return str(path)


def _write_index(project: str, root: Path | None) -> None:
    """glob/ASSETS.md right away, so the note never points at a missing file (Glob keeps it current afterwards)."""
    from forge import asset_index
    from forge.storage.project_store import ProjectStore

    store = ProjectStore(project_name=project, root_dir=root)
    asset_index.write(store.project_dir / "workspace", store.get_bible().display_name or project)


def disconnect(project: str, root: Path | None = None) -> dict[str, Any]:
    link = game_link.get_link(project, root)
    game = Path(link["game_folder"]) if link else None
    if game and game.is_dir():
        _remove_old_setup(game)
        for name in NOTE_FILES:
            _write_block(game / name, MD_START, MD_END, None)
    game_link.clear_link(project, root)
    return status(project, root)


def refresh(project: str, root: Path | None = None) -> None:
    """Rewrite Glob's note in the connected game (after an update changed it)."""
    link = game_link.get_link(project, root)
    if link and Path(link["game_folder"]).is_dir():
        connect(project, link["game_folder"], None, root)


def status(project: str, root: Path | None = None) -> dict[str, Any]:
    link = game_link.get_link(project, root)
    if not link:
        return {"connected": False}
    game = Path(link["game_folder"])
    return {
        "connected": True,
        "game_folder": str(game),
        "game_name": game.name,
        "healthy": game.is_dir() and MD_START in ((game / "AGENTS.md").read_text(encoding="utf-8", errors="ignore") if (game / "AGENTS.md").is_file() else ""),
        "notes": [name for name in NOTE_FILES if (game / name).is_file()],
        "index": _index_ref(project, game, root),
    }


def connect_game(folder: str, targets: list[str] | None = None, root: Path | None = None) -> dict[str, Any]:
    """`glob connect`: the Glob project for this game folder (made the first time, in `<game>/glob/`), connected.

    Does what choosing the game in Glob's New project screen does, for a coding agent that set Glob up from a terminal."""
    from forge.agent.action_layer import ForgeActionLayer
    from forge.game_import import _engine
    from forge.storage.project_store import home_owner, make_home, sanitize_project_name

    game = Path(folder).expanduser()
    if not game.is_dir():
        raise ValueError(f"'{folder}' is not a folder.")
    game = game.resolve()
    base = root or get_projects_root()
    name = home_owner(game, base)
    created = name is None
    if created:
        # Named like the game itself (project.godot's name, Unity's productName), not like its folder.
        title = _engine(game)[2].strip() or game.name
        name = stem = sanitize_project_name(title) or "my_game"
        index = 2
        while (base / name).exists():
            name, index = f"{stem}-{index}", index + 1
        make_home(name, game, base)
        store = ForgeActionLayer(root_dir=base).get_store(name)
        bible = store.get_bible()
        if title != name and not bible.display_name:
            store.save_bible(bible.model_copy(update={"display_name": title}))
    return connect(name, str(game), targets, root) | {"project": name, "created": created}

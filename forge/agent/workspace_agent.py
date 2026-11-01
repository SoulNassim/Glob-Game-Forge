"""Forge Agent — multi-session project agent with file tools and handoff memory.

Sessions live in `projects/<name>/workspace/inbox/chats/<id>.json`. Each turn runs a short
JSON tool loop against the workspace (read, write, move, delete-to-trash, folders, blueprints).
"Summarize and continue" writes `inbox/handoffs/<stamp>-<slug>-handoff.md` and opens a linked
session that starts from that handoff.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from forge.models import new_id
from forge import access, asset_studio, game_import
from forge.agent import cli_agents
from forge.config import load_config
from forge.overview import FRONT_PAGES, HIDDEN_NAMES, move_to_trash

ModelCall = Callable[..., Awaitable[dict[str, Any]]]

CHATS_DIR = Path("inbox") / "chats"
ATTACHMENTS_DIR = Path("inbox") / "attachments"
HANDOFFS_DIR = Path("inbox") / "handoffs"
MAX_TOOL_ROUNDS = 8
MAX_FILE_CHARS = 8000
MAX_COPY_BYTES = 2 * 1024 * 1024 * 1024
EFFORTS = {"low", "medium", "high", "max"}
_SESSION_ID_RE = re.compile(r"^chat_[a-z0-9]+$")

SYSTEM_PROMPT = """You are Glob, the project agent (a cheerful pink slime blacksmith) for the game project "{project}".
You work directly inside the project's workspace folder (all paths are relative to it) and you ACT:
when the user asks for something, you create, edit, move and organize the files yourself.

The user sees a live Project Overview next to this chat that is built from the folder structure:
- Every top-level folder is a section in the overview. Prefix folders with numbers to set their order
  (e.g. "01-intro", "02-characters", "03-locations", "04-development").
- A folder's README.md is that section's intro page; its first "# Heading" is the section title and
  the first paragraph is its summary. Other .md files in a folder show up as cards.
- The root README.md (or GAME.md) is the project's front page: a "# Title", a one-line pitch paragraph,
  then whatever overview content fits the game.
- An image called cover.png/jpg/webp in a folder becomes that section's hero image.
There is no fixed structure: choose folders and documents that fit THIS game (a puzzle game, an RPG,
a racing game and a visual novel all need different sections).
Groups are folders, things are files: a collection (creatures, characters, factions, items, weapons,
locations, UI screens…) is a FOLDER with a README.md describing the group, and every individual thing
that will need art is its own asset file inside it (05-characters/creatures/sump-stalker.md), or a
sub-folder when it groups further (05-characters/creatures/beasts/). Never put a roster of several
characters/items into one .md; a .md is one topic or one asset.

Reply with ONLY one JSON object, no prose outside it:
{{"reply": "<short markdown message to the user>", "actions": [<tool calls>], "done": true}}

Tools (paths relative to the workspace):
- {{"tool": "list", "path": ""}}                      list a folder ("" = workspace root)
- {{"tool": "read", "path": "README.md"}}
- {{"tool": "write", "path": "02-characters/hero.md", "content": "..."}}  create or overwrite; parent folders are created
- {{"tool": "append", "path": "04-development/tasks.md", "content": "..."}}
- {{"tool": "create_folder", "path": "03-locations/islands"}}
- {{"tool": "move", "from": "old.md", "to": "new/old.md"}}     also renames folders
- {{"tool": "delete", "path": "notes/old.md"}}                  moves a file or folder to .trash/ (recoverable)
- {{"tool": "generate", "path": "02-characters/knight.md", "action": "candidates", "count": 2}}
      starts image/3D generation for an asset. action: "candidates" (new images from the asset prompt),
      "variant" or "edit" (needs "instruction", works on the winner image), "views" (side and back
      turnaround views of the winner; make_3d then uses them for a better model), "make_3d" (3D model from
      the winner), "rig" (skeleton for a humanoid GLB, also gives walk and run clips), "animate" (needs a rig
      and "action_ids": up to 10 Meshy animation-library ids). Generation costs money: only run it when the
      user asks for images, 3D, rigs or animations.
      Splitting an image into parts, icons, sprite sheets, 3D preview and Godot/Unity/Unreal export are
      buttons under "Game tools" on the asset page; point the user there for those.
- {{"tool": "pick_winner", "path": "02-characters/knight.md", "file": "02-characters/knight/candidates/knight-1.png"}}

Folders elsewhere on the user's computer (their game project, an art folder): you can reach only folders the
user granted, listed under "Granted folders" in the context. Use ABSOLUTE paths for those with list, read and
copy_in. When the user mentions a folder that is not granted, call request_access right away (the user gets
Allow / Deny buttons in the chat and you continue once they answer). Never tell the user you cannot access
their computer, and never ask them to copy files by hand: ask for access instead.
- {{"tool": "request_access", "path": "D:\\Games\\MyGame", "reason": "to import your characters", "write": false}}
- {{"tool": "import_game", "path": "D:\\Games\\MyGame"}}  imports a Godot, Unity or Unreal project (or any asset
      folder): characters with their models, animations, textures and variants, weapons and items with their stats
      from the game data, kits, skies, textures and design docs. Files are copied into numbered sections; existing
      sections and assets with the same names are merged (the real model lands in the asset you sketched).
      Items the game defines without any art are added with `status: needs art`. Needs read access first.
- {{"tool": "copy_in", "from": "D:\\Games\\MyGame\\art\\elia.png", "to": "02-characters/elia/candidates"}}
      copies a file or folder from a granted folder into the workspace.
With write access the write tool also takes an absolute path inside that folder (for engine files).

Assets: an asset is a .md whose header (YAML front matter) has `kind:` (one of the kinds listed below)
and `prompt:` (a clear visual description of the subject only: silhouette, materials,
colours, details; no style words, the pillars carry the style). Example:
---
kind: character
prompt: A stout knight in dented bronze armour with a red tabard and a round shield
---
# Bronze Knight
Lore and gameplay notes…
Generated images land in <asset-name>/candidates|variants|edits|3d/ next to the asset; `winner:` in the
header is the chosen image.
Pillars: reference images in a `pillars/` folder keep the art style consistent. pillars/ at the workspace root
is the project bible (sent with every generation); pillars/ inside any folder applies to everything below it.
Text files in a `pillars/` folder (e.g. pillars/style-bible.md) are the style bible: their text is put in
front of every image prompt below that folder (pose such as T-pose, polygon budget, palette, camera,
rendering rules). Write the project-wide one at pillars/style-bible.md when the user describes the look.

Writing prompts that survive generation. The image model gets: the asset prompt, then the style bible, then
the reference images (the pillars, which show OTHER assets). So:
- Asset `prompt:` leads with WHO or WHAT it is: gender, age, build, face and hair for people, then outfit,
  materials and props. Keep the user's own words about appearance (beautiful, old, scarred, muscular, female…)
  exactly as they said them; never soften or swap them for the look of the pillars or the project's mood.
- Never describe the subject by pointing at the references ("like the others"); each prompt must stand alone.
- The style bible is short (about 10-15 bullets, under ~150 words) and only says HOW things are drawn:
  format, rendering and texture treatment, palette, lighting, camera, pose, polygon budget. Keep out
  subject descriptions, lore, and mood words that would colour a face or body (grimy, ugly, wretched,
  uncanny, vulnerable). Wear, dirt and horror belong to individual assets or to environment/creature
  folders' own pillars/style-bible.md, not to the project-wide file that every character inherits.
- Never write "match proportions/pose/anatomy exactly" in a style bible: references carry style only, and
  the asset prompt decides who is in the picture.
- Do not add a style-bible.md inside a sub-folder that just repeats the project one; add one there only
  for rules that differ for that folder.
When you create or edit a style bible, apply these rules. When the user complains that a generation looks
wrong (wrong gender, ugly, copies the references), check the asset prompt, the style bible and the
pillars for the cause, fix the text, and say what you changed.
Use only these kinds: {kinds}.

You may put many actions in one reply. Set "done": false only when you need list/read results before
continuing; you will get the results back. When asked to create a project structure, create the
folders AND write a README.md with real, specific starter content in each one, plus the root README.md,
and give collections their first few asset files (each with `kind:` and `prompt:`).
Write rich, concrete Markdown (headings, lists, tables), never placeholders like "TBD" or "lorem ipsum".
Only ask a clarifying question when the request is genuinely impossible to act on; otherwise make
sensible choices and say what you assumed. Keep the chat reply short: what you did and what's next."""

COMPACT_PROMPT = """Summarize the earlier part of this chat so the conversation can continue without it.
Keep every decision, name, path, number, style rule and open task; drop chit-chat. Use short Markdown bullets
under: ## Decisions, ## Files and assets, ## Open tasks. Reply with only the Markdown."""

HANDOFF_PROMPT = """Write a handoff note so a fresh agent can continue this Forge session without the chat.
Use Markdown with exactly these sections: ## Goal, ## Decisions, ## Art & style rules, ## Files touched,
## Open tasks, ## Next step. Be specific (names, paths, numbers). Reply with only the Markdown."""


def _now() -> float:
    return time.time()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "chat").lower()).strip("-")[:40] or "chat"


def _is_absolute(path: str) -> bool:
    """C:\\Games, D:/Games, /home/me/games or ~/games: a path on the computer rather than in the workspace."""
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|/|~[\\/])", path))


def _extract_json(text: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip(), flags=re.IGNORECASE)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class WorkspaceAgent:
    def __init__(self, actions: Any, model_call: ModelCall, job_starter: Callable[..., dict[str, Any]] | None = None) -> None:
        self.actions = actions
        self.model_call = model_call
        # Starts an asset-studio job: (project, request) -> job. Set by the server.
        self.job_starter = job_starter

    # --- storage -----------------------------------------------------------------

    def _ws(self, project: str) -> Path:
        return self.actions.get_workspace(project).ws_dir

    def _chats(self, project: str) -> Path:
        directory = self._ws(project) / CHATS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _session_path(self, project: str, session_id: str) -> Path:
        if not _SESSION_ID_RE.match(session_id):
            raise ValueError("Invalid chat id.")
        return self._chats(project) / f"{session_id}.json"

    def _save(self, project: str, session: dict[str, Any]) -> dict[str, Any]:
        session["updated_at"] = _now()
        self._session_path(project, session["id"]).write_text(json.dumps(session, indent=2), encoding="utf-8")
        return session

    def get_session(self, project: str, session_id: str) -> dict[str, Any]:
        path = self._session_path(project, session_id)
        if not path.is_file():
            raise KeyError(f"Chat '{session_id}' not found.")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_sessions(self, project: str) -> list[dict[str, Any]]:
        self._import_legacy_chat(project)
        sessions = []
        for path in self._chats(project).glob("chat_*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            sessions.append({
                key: data.get(key)
                for key in ("id", "title", "group", "created_at", "updated_at", "handoff_from", "handoff_to", "model", "effort", "agent")
            } | {"message_count": len(data.get("messages", [])), "running": cli_agents.is_running(data.get("id", ""))})
        return sorted(sessions, key=lambda entry: entry.get("created_at") or 0)

    def create_session(self, project: str, title: str = "New chat", group: str = "General", **extra: Any) -> dict[str, Any]:
        session = {
            "id": f"chat_{new_id('x')[2:].lower()}",
            "title": (title or "New chat").strip()[:80],
            "group": (group or "General").strip()[:40],
            "created_at": _now(),
            "updated_at": _now(),
            "model": extra.get("model") or "",
            "effort": extra.get("effort") if extra.get("effort") in EFFORTS else "medium",
            # "glob" is the built-in agent; otherwise a coding-agent CLI id (claude, codex, …).
            "agent": extra.get("agent") if extra.get("agent") in cli_agents.CLIS else "glob",
            "cli_session": "",
            # Coding-agent model and effort; "" uses the CLI's own default.
            "cli_model": "",
            "cli_effort": "",
            "handoff_from": extra.get("handoff_from"),
            "handoff_to": None,
            "messages": list(extra.get("messages") or []),
        }
        return self._save(project, session)

    def update_session(self, project: str, session_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        session = self.get_session(project, session_id)
        for key in ("title", "group", "model"):
            if isinstance(fields.get(key), str):
                session[key] = fields[key].strip()[:80] or session[key]
        if fields.get("effort") in EFFORTS:
            session["effort"] = fields["effort"]
        for key in ("cli_model", "cli_effort"):
            if isinstance(fields.get(key), str):
                session[key] = fields[key].strip()[:120]
        return self._save(project, session)

    def delete_session(self, project: str, session_id: str) -> str | None:
        return move_to_trash(self._ws(project), self._session_path(project, session_id))

    def _import_legacy_chat(self, project: str) -> None:
        chats = self._chats(project)
        legacy = self._ws(project) / "CHAT.json"
        marker = chats / ".legacy_imported"
        if marker.exists() or not legacy.is_file():
            return
        marker.write_text("1", encoding="utf-8")
        try:
            history = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(history, list) or not any(isinstance(entry, dict) and entry.get("role") == "user" for entry in history):
            return
        messages = [
            {
                "role": "user" if entry.get("role") == "user" else "agent",
                "text": re.sub(r"\b(Gloop|Souly)\b", "Forge Agent", str(entry.get("text") or "")),
                "actions": [{"tool": "legacy", "summary": str(step)} for step in entry.get("steps") or []],
                "at": entry.get("timestamp") or _now(),
            }
            for entry in history
            if isinstance(entry, dict)
        ]
        self.create_session(project, title="Earlier conversation", group="Archive", messages=messages)

    # --- tools -------------------------------------------------------------------

    def _resolve(self, project: str, rel: str) -> Path:
        ws = self._ws(project).resolve()
        clean = str(rel or "").replace("\\", "/").lstrip("/")
        if clean.startswith("workspace/"):
            clean = clean[len("workspace/") :]
        target = (ws / clean).resolve()
        if not target.is_relative_to(ws):
            raise ValueError(f"'{rel}' is outside the workspace.")
        return target

    def _outside(self, project: str, raw: Any, need_write: bool = False) -> Path | None:
        """An absolute path outside the workspace, checked against the user's grants; None for workspace paths."""
        text = str(raw or "").strip()
        if not text or not _is_absolute(text):
            return None
        head = text.lstrip("/").split("/")[0]
        if text.startswith("/") and head and (self._ws(project) / head).exists():
            return None  # "/02-characters/elia.md" means the workspace
        try:
            if Path(text).resolve().is_relative_to(self._ws(project).resolve()):
                return None
        except OSError:
            pass
        return access.check(text, need_write=need_write)

    def _outside_tool(self, tool: str, target: Path, call: dict[str, Any]) -> dict[str, Any]:
        if tool == "list":
            if not target.is_dir():
                raise FileNotFoundError(f"No folder '{target}'.")
            names = sorted(f"{p.name}/" if p.is_dir() else p.name for p in target.iterdir() if not p.name.startswith(".") and not access.is_secret(p))
            return {"tool": tool, "path": str(target), "result": "\n".join(names[:300]), "summary": f"Listed {target}"}
        if tool == "read":
            if not target.is_file():
                raise FileNotFoundError(f"No file '{target}'.")
            if target.stat().st_size > 2_000_000:
                raise ValueError("That file is too large to read; use copy_in or import_game.")
            text = target.read_text(encoding="utf-8", errors="ignore")
            return {"tool": tool, "path": str(target), "result": text[:MAX_FILE_CHARS], "summary": f"Read {target}"}
        target.parent.mkdir(parents=True, exist_ok=True)
        content = str(call.get("content") or "")
        if tool == "append" and target.is_file():
            content = f"{target.read_text(encoding='utf-8', errors='ignore').rstrip()}\n\n{content.strip()}\n"
        target.write_text(content, encoding="utf-8")
        return {"tool": tool, "path": str(target), "summary": f"Wrote {target}", "changed": True}

    def _find_item(self, project: str, ref: str):
        items = self.actions.get_pipeline(project).list_pipeline_items()
        key = str(ref or "").strip().lower()
        item = next((i for i in items if i.id == ref or i.slug == key or i.title.lower() == key), None)
        if item is None:
            raise KeyError(f"Blueprint '{ref}' not found.")
        return item

    def run_tool(self, project: str, call: dict[str, Any]) -> dict[str, Any]:
        tool = str(call.get("tool") or "")
        ws = self._ws(project).resolve()
        rel = lambda path: path.resolve().relative_to(ws).as_posix()  # noqa: E731
        outside = self._outside(project, call.get("path"), need_write=tool in ("write", "append")) if tool in ("list", "read", "write", "append") else None
        if outside is not None:
            return self._outside_tool(tool, outside, call)
        if tool == "request_access":
            raw = str(call.get("path") or "").strip()
            if not raw or not _is_absolute(raw):
                raise ValueError("request_access needs an absolute folder path.")
            folder = Path(raw)
            folder = folder.parent if folder.is_file() else folder
            want_write = bool(call.get("write"))
            if access.granted_path(str(folder), need_write=want_write):
                return {"tool": tool, "path": str(folder), "summary": f"Already allowed: {folder}", "result": "Access is already granted; go ahead."}
            request = {"path": str(folder), "reason": str(call.get("reason") or "")[:200], "write": want_write, "exists": folder.is_dir()}
            return {"tool": tool, "path": str(folder), "summary": f"Asked for access to {folder}", "access_request": request,
                    "result": "Waiting for the user to allow access. Stop here and tell them what you will do once allowed."}
        if tool == "import_game":
            folder = access.check(str(call.get("path") or ""))
            result = game_import.import_game(folder, self._ws(project), title=str(call.get("title") or "") or None)
            counts = result["summary"]["counts"]
            sections = ", ".join(sorted(result["sections"].values()))
            return {"tool": tool, "path": str(folder), "changed": True,
                    "summary": f"Imported {result['copied']} files from {folder.name}: {counts['characters']} characters, {counts['models']} models, {counts['animations']} animations",
                    "result": f"Sections: {sections}\nAssets written: {len(result['assets'])}\nNeeds art: {counts['needs_art']}"}
        if tool == "copy_in":
            source = access.check(str(call.get("from") or ""))
            dest_dir = self._resolve(project, call.get("to") or "")
            if source.is_dir():
                size = sum(p.stat().st_size for p in source.rglob("*") if p.is_file())
                if size > MAX_COPY_BYTES:
                    raise ValueError("That folder is over 2 GB; use import_game or pick a smaller folder.")
                target = dest_dir / source.name
                shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".*", "*.import", "*.uid", "*.meta"))
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                target = dest_dir / source.name
                shutil.copy2(source, target)
            return {"tool": tool, "path": rel(target), "summary": f"Copied {source.name} → {rel(target)}", "changed": True}
        if tool == "list":
            target = self._resolve(project, call.get("path") or "")
            if not target.is_dir():
                raise FileNotFoundError(f"No folder '{call.get('path')}'.")
            names = sorted(f"{p.name}/" if p.is_dir() else p.name for p in target.iterdir() if not p.name.startswith("."))
            return {"tool": tool, "path": rel(target) or ".", "result": "\n".join(names[:300]), "summary": f"Listed {rel(target) or 'workspace'}"}
        if tool == "read":
            target = self._resolve(project, call.get("path"))
            if not target.is_file():
                raise FileNotFoundError(f"No file '{call.get('path')}'.")
            text = target.read_text(encoding="utf-8", errors="ignore")
            return {"tool": tool, "path": rel(target), "result": text[:MAX_FILE_CHARS], "summary": f"Read {rel(target)}"}
        if tool in ("write", "append"):
            target = self._resolve(project, call.get("path"))
            target.parent.mkdir(parents=True, exist_ok=True)
            content = str(call.get("content") or "")
            if tool == "append" and target.is_file():
                existing = target.read_text(encoding="utf-8", errors="ignore")
                content = f"{existing.rstrip()}\n\n{content.strip()}\n"
            target.write_text(content, encoding="utf-8")
            if target.name.upper() == "GAME.MD":
                self.actions.get_workspace(project).sync_game_md_to_bible()
            return {"tool": tool, "path": rel(target), "summary": f"{'Appended to' if tool == 'append' else 'Wrote'} {rel(target)}", "changed": True}
        if tool == "move":
            source = self._resolve(project, call.get("from"))
            dest = self._resolve(project, call.get("to"))
            if not source.exists():
                raise FileNotFoundError(f"No file '{call.get('from')}'.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            source.rename(dest)
            return {"tool": tool, "path": rel(dest), "summary": f"Moved {call.get('from')} → {rel(dest)}", "changed": True}
        if tool == "delete":
            target = self._resolve(project, call.get("path"))
            if target == ws:
                raise ValueError("The workspace root cannot be deleted.")
            moved = move_to_trash(ws, target)
            if moved is None:
                raise FileNotFoundError(f"No file '{call.get('path')}'.")
            return {"tool": tool, "path": moved, "summary": f"Moved {call.get('path')} to .trash/", "changed": True}
        if tool == "create_folder":
            target = self._resolve(project, call.get("path"))
            if target == ws:
                raise ValueError("create_folder needs a path.")
            target.mkdir(parents=True, exist_ok=True)
            return {"tool": tool, "path": rel(target), "summary": f"Created folder {rel(target)}/", "changed": True}
        if tool == "generate":
            if self.job_starter is None:
                raise ValueError("Generation is not available here.")
            path = str(call.get("path") or "")
            action = str(call.get("action") or "candidates")
            request = asset_studio.build_request(ws, path, action, source=str(call.get("source") or ""), instruction=str(call.get("instruction") or ""))
            if call.get("count"):
                request["count"] = int(call["count"])
            if action == "animate":
                request["action_ids"] = [int(a) for a in call.get("action_ids") or []]
            job = self.job_starter(project, request)
            return {"tool": tool, "path": path, "summary": f"Started {action.replace('_', ' ')} for {path}", "changed": True, "job": job["id"]}
        if tool == "pick_winner":
            path = str(call.get("path") or "")
            asset_studio.update_asset(ws, path, {"winner": str(call.get("file") or "")})
            return {"tool": tool, "path": path, "summary": f"Picked winner for {path}", "changed": True}
        raise ValueError(f"Unknown tool '{tool}'.")

    # --- context -----------------------------------------------------------------

    def _context_block(self, project: str, session: dict[str, Any], page: dict[str, Any], refs: list[dict[str, Any]]) -> tuple[str, list[tuple[str, str]]]:
        ws = self._ws(project)
        parts: list[str] = []
        front = next((ws / name for name in FRONT_PAGES if (ws / name).is_file()), None)
        if front:
            parts.append(f"## {front.name} (front page excerpt)\n{front.read_text(encoding='utf-8', errors='ignore')[:2500]}")
        parts.append("## Workspace tree\n" + (self._tree(ws) or "(empty — nothing has been created yet)"))
        parts.append(f"## Workspace folder on disk\n{ws.resolve()}")
        grants = access.list_grants()
        parts.append("## Granted folders\n" + ("\n".join(f"- {g['path']} ({'read & write' if g['write'] else 'read'})" for g in grants) or "(none yet: use request_access)"))
        if page:
            parts.append(f"## User is viewing\n{json.dumps(page)[:600]}")
        if session.get("handoff_from"):
            try:
                handoff = self._resolve(project, session["handoff_from"]).read_text(encoding="utf-8", errors="ignore")
                parts.append(f"## Handoff from the previous session ({session['handoff_from']})\n{handoff[:6000]}")
            except (OSError, ValueError):
                pass
        images: list[tuple[str, str]] = []
        store = self.actions.get_store(project)
        for ref in refs[:8]:
            kind = ref.get("kind")
            try:
                if kind == "md" and ref.get("path"):
                    text = self._resolve(project, ref["path"]).read_text(encoding="utf-8", errors="ignore")
                    parts.append(f"## @{ref.get('token') or ref['path']} ({ref['path']})\n{text[:MAX_FILE_CHARS]}")
                elif kind == "item" and ref.get("id"):
                    item = self._find_item(project, ref["id"])
                    md = self._resolve(project, item.md_rel_path)
                    parts.append(f"## @{ref.get('token')} — blueprint {item.md_rel_path}\n{md.read_text(encoding='utf-8', errors='ignore')[:MAX_FILE_CHARS] if md.is_file() else item.description}")
                elif kind in ("image", "anchor", "pillar", "mesh") and ref.get("id"):
                    asset = store.get_asset(ref["id"])
                    if asset:
                        parts.append(f"## @{ref.get('token')} — asset {asset.id} '{asset.name}' ({asset.file_path})")
                        image_rel = asset.thumbnail_path if kind == "mesh" else asset.file_path
                        image_path = store.resolve_safe_path(image_rel) if image_rel else None
                        if image_path and image_path.is_file() and image_path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                            mime = "image/png" if image_path.suffix.lower() == ".png" else "image/webp" if image_path.suffix.lower() == ".webp" else "image/jpeg"
                            images.append((mime, base64.b64encode(image_path.read_bytes()).decode("ascii")))
            except (OSError, ValueError, KeyError):
                continue
        return "\n\n".join(parts), images

    def _tree(self, ws: Path, depth: int = 3, limit: int = 160) -> str:
        lines: list[str] = []

        def walk(directory: Path, level: int) -> None:
            for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if len(lines) >= limit or child.name.startswith(".") or child.name in HIDDEN_NAMES:
                    continue
                lines.append("  " * level + child.name + ("/" if child.is_dir() else ""))
                if child.is_dir() and level + 1 < depth:
                    walk(child, level + 1)

        walk(ws, 0)
        return "\n".join(lines)

    @staticmethod
    def context_tokens(session: dict[str, Any]) -> int:
        """Rough token count of what the next turn sends as conversation (4 characters ≈ 1 token)."""
        chars = len(session.get("summary") or "")
        for message in session.get("messages", [])[session.get("compacted", 0):]:
            chars += min(len(str(message.get("text") or "")), 1800)
            chars += sum(len(a.get("summary", "")) for a in message.get("actions") or [])
        return chars // 4

    async def compact(self, project: str, session: dict[str, Any], force: bool = False) -> dict[str, Any]:
        """Fold older messages into a running summary once the chat passes the configured size."""
        cfg = load_config()
        keep = max(2, cfg.agent_keep_recent_messages)
        if not force and (not cfg.agent_auto_compact or self.context_tokens(session) < cfg.agent_compact_at_tokens):
            return session
        start = session.get("compacted", 0)
        cut = len(session.get("messages", [])) - keep
        if cut <= start:
            return session
        older = {"messages": session["messages"][start:cut]}
        previous = f"# Summary so far\n{session['summary']}\n\n" if session.get("summary") else ""
        try:
            result = await self.model_call(prompt=previous + "# Messages to fold in\n" + self._transcript(older, limit=10_000), system_prompt=COMPACT_PROMPT, model_selector=session.get("model") or None, effort="low")
            summary = str(result.get("text") or "").strip()
        except Exception:  # noqa: BLE001 — keep the full chat if the summary can't be made
            return session
        if not summary:
            return session
        session["summary"] = summary
        session["compacted"] = cut
        session["messages"].append({"role": "note", "text": f"Compacted {cut - start} earlier messages into a summary to keep the context small.", "at": _now()})
        return self._save(project, session)

    def _transcript(self, session: dict[str, Any], limit: int = 16) -> str:
        lines = [f"(Summary of the earlier chat)\n{session['summary']}"] if session.get("summary") else []
        messages = session.get("messages", [])[session.get("compacted", 0):]
        # With auto-compact the summary bounds the size; without it, only the last messages are sent.
        if not load_config().agent_auto_compact:
            messages = messages[-limit:]
        for message in messages:
            role = "User" if message.get("role") == "user" else "Agent" if message.get("role") == "agent" else "Note"
            text = str(message.get("text") or "")[:1800]
            done = "; ".join(action.get("summary", "") for action in message.get("actions") or [] if action.get("summary"))
            lines.append(f"{role}: {text}" + (f"\n(Actions: {done})" if done else ""))
        return "\n\n".join(lines)

    def _store_attachments(self, project: str, session_id: str, attachments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        saved: list[dict[str, Any]] = []
        images: list[tuple[str, str]] = []
        directory = self._ws(project) / ATTACHMENTS_DIR / session_id
        for index, attachment in enumerate(attachments[:4]):
            match = re.match(r"^data:(image/(?:png|jpeg|webp|gif));base64,(.+)$", str(attachment.get("data_url") or ""), re.S)
            if not match:
                continue
            mime, b64 = match.groups()
            directory.mkdir(parents=True, exist_ok=True)
            extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}[mime]
            path = directory / f"{int(_now() * 1000)}_{index}.{extension}"
            path.write_bytes(base64.b64decode(b64))
            saved.append({"name": str(attachment.get("name") or path.name)[:80], "path": path.relative_to(self._ws(project)).as_posix()})
            images.append((mime, b64))
        return saved, images

    # --- turns -------------------------------------------------------------------

    async def send(
        self,
        project: str,
        session_id: str,
        text: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        refs: list[dict[str, Any]] | None = None,
        page: dict[str, Any] | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        session = self.get_session(project, session_id)
        text = str(text or "").strip()
        saved_attachments, uploaded_images = self._store_attachments(project, session_id, attachments or [])
        if not text and not saved_attachments:
            raise ValueError("Write a message or attach an image.")
        if model:
            session["model"] = model
        if effort in EFFORTS:
            session["effort"] = effort
        session["messages"].append({"role": "user", "text": text, "attachments": saved_attachments, "refs": [r.get("token") for r in refs or [] if r.get("token")], "at": _now()})
        if session.get("title") in ("", "New chat") and text:
            session["title"] = text[:48] + ("…" if len(text) > 48 else "")
        self._save(project, session)

        context, ref_images = self._context_block(project, session, page or {}, refs or [])
        images = (uploaded_images + ref_images)[:4]
        system = SYSTEM_PROMPT.format(project=project, kinds=", ".join(asset_studio.ASSET_KINDS)) + f"\n\nReasoning effort: {session.get('effort', 'medium')} — scale the depth of your analysis accordingly."
        session = await self.compact(project, session)
        transcript = self._transcript(session)
        tool_log: list[str] = []
        performed: list[dict[str, Any]] = []
        reply_text = ""
        used_model = ""
        for _round in range(max(1, load_config().agent_max_tool_rounds or MAX_TOOL_ROUNDS)):
            prompt = f"# Context\n{context}\n\n# Conversation\n{transcript}"
            if tool_log:
                prompt += "\n\n# Tool results\n" + "\n\n".join(tool_log)
            prompt += "\n\nRespond with the JSON object now."
            try:
                result = await self.model_call(prompt=prompt, system_prompt=system, model_selector=session.get("model") or None, images_b64=images or None, effort=session.get("effort", "medium"))
            except Exception as exc:  # noqa: BLE001 — surface any provider failure in the chat
                detail = str(exc).split("For more information check:")[0].strip()
                reply_text = f"I couldn't reach the model ({detail[:240]}). Open Settings to connect a model."
                break
            used_model = str(result.get("model") or "")
            data = _extract_json(result.get("text", ""))
            if data is None:
                reply_text = str(result.get("text") or "").strip()
                break
            reply_text = str(data.get("reply") or "").strip()
            calls = [call for call in data.get("actions") or [] if isinstance(call, dict)]
            needs_results = False
            waiting = False
            for call in calls[:20]:
                try:
                    if call.get("tool") in ("import_game", "copy_in"):
                        outcome = await asyncio.to_thread(self.run_tool, project, call)
                    else:
                        outcome = self.run_tool(project, call)
                    outcome["ok"] = True
                except Exception as exc:  # noqa: BLE001 — report tool errors back to the model and user
                    outcome = {"tool": call.get("tool"), "path": call.get("path") or call.get("id"), "ok": False, "summary": f"{call.get('tool')} failed: {exc}"}
                result_text = outcome.pop("result", None)
                performed.append(outcome)
                tool_log.append(f"{json.dumps(call)[:300]}\n→ {result_text if result_text is not None else outcome['summary']}"[: MAX_FILE_CHARS + 400])
                if call.get("tool") in ("list", "read", "import_game") and outcome.get("ok"):
                    needs_results = True
                if outcome.get("access_request"):
                    waiting = True
            if waiting or (data.get("done", True) is not False and not (needs_results and not reply_text)):
                break
        session = self.get_session(project, session_id)
        session["messages"].append({"role": "agent", "text": reply_text or "Done.", "actions": performed, "model": used_model, "at": _now()})
        session["context_tokens"] = self.context_tokens(session)
        return self._save(project, session)

    async def send_cli(self, project: str, session_id: str, text: str, on_event: Callable[[dict[str, Any]], Any], allow_edits: bool = True) -> dict[str, Any]:
        """One turn with the session's coding-agent CLI, running in the workspace folder."""
        session = self.get_session(project, session_id)
        cli_id = session.get("agent")
        if cli_id not in cli_agents.CLIS:
            raise ValueError("This chat is not a coding-agent chat.")
        text = str(text or "").strip()
        if not text:
            raise ValueError("Write a message.")
        session["messages"].append({"role": "user", "text": text, "attachments": [], "at": _now()})
        if session.get("title") in ("", "New chat"):
            session["title"] = text[:48] + ("…" if len(text) > 48 else "")
        self._save(project, session)
        try:
            result = await cli_agents.run_turn(
                session_id, cli_id, self._ws(project), text, session.get("cli_session") or "", allow_edits, on_event,
                model=session.get("cli_model") or "", effort=session.get("cli_effort") or "",
            )
            message = {"role": "agent", "text": result["text"], "actions": result["actions"], "model": session.get("cli_model") or cli_agents.CLIS[cli_id]["label"], "at": _now()}
        except Exception as exc:  # noqa: BLE001 — show CLI failures in the chat
            result = {}
            message = {"role": "agent", "text": str(exc), "actions": [], "model": cli_agents.CLIS[cli_id]["label"], "error": True, "at": _now()}
        session = self.get_session(project, session_id)
        if result.get("session_id"):
            session["cli_session"] = result["session_id"]
        session["messages"].append(message)
        return self._save(project, session)

    async def handoff(self, project: str, session_id: str) -> dict[str, Any]:
        session = self.get_session(project, session_id)
        transcript = self._transcript(session, limit=60)
        try:
            result = await self.model_call(prompt=f"# Session: {session['title']}\n\n{transcript}", system_prompt=HANDOFF_PROMPT, model_selector=session.get("model") or None)
            summary = str(result.get("text") or "").strip()
        except Exception:  # noqa: BLE001 — fall back to a mechanical summary when no model is reachable
            summary = ""
        if not summary:
            requests = [m["text"] for m in session["messages"] if m.get("role") == "user" and m.get("text")][-8:]
            touched = sorted({a.get("path") for m in session["messages"] for a in m.get("actions") or [] if a.get("changed") and a.get("path")})
            decisions = [f"- {text[:200]}" for text in requests[1:]] or ["- (none recorded)"]
            files = [f"- {path}" for path in touched] or ["- (none)"]
            summary = "\n".join([
                "## Goal", requests[0] if requests else "(not stated)",
                "", "## Decisions", *decisions,
                "", "## Art & style rules", "- See the project README.md",
                "", "## Files touched", *files,
                "", "## Open tasks", "- Continue from the last request above.",
                "", "## Next step", requests[-1][:200] if requests else "Ask the user.",
            ])
        stamp = time.strftime("%Y%m%d-%H%M")
        rel = (HANDOFFS_DIR / f"{stamp}-{_slug(session['title'])}-handoff.md").as_posix()
        path = self._resolve(project, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nsession: {session['id']}\ntitle: \"{session['title']}\"\ncreated_at: {int(_now())}\n---\n\n# Handoff — {session['title']}\n\n{summary}\n", encoding="utf-8")
        continued = self.create_session(
            project,
            title=f"{session['title'][:60]} · continued",
            group=session.get("group") or "General",
            model=session.get("model"),
            effort=session.get("effort"),
            handoff_from=rel,
            messages=[{"role": "note", "text": f"Continuing from handoff `{rel}`. The agent reads it before every reply.", "at": _now()}],
        )
        session["handoff_to"] = continued["id"]
        self._save(project, session)
        return {"handoff_path": rel, "session": continued}

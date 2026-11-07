"""Claude Code as the Glob chat's agent: the real CLI, working in the workspace with its own file tools.

Each chat message runs `claude -p` in the workspace folder and continues the same conversation with --resume, so
pages and folders have no step limit and it behaves like Claude Code. Three things keep it inside Glob's rules:
- `--restricted` removes the tools that run code and confines the file tools to the workspace and the granted
  folders. Glob's permission rules (`permission_rules`) add what `forge.access` enforces for the old chat.
- Whatever spends credits, or isn't a plain file edit, is a tool on Glob's own MCP endpoint (`handle_rpc`) and goes
  through `WorkspaceAgent.run_tool`, where the limit on generations lives.
- It gets no user, project or local settings and no CLAUDE.md: a game's notes tell agents to stay out of Glob's folder.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from forge import access
from forge.agent import cli_models
from forge.config import get_config_dir
from forge.gamescan.safety import SECRET_FILE_PATTERNS

FILE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
SERVER = "glob"
TOOL_PREFIX = f"mcp__{SERVER}__"

ADDENDUM = """## Tools in this session

You are working through Claude Code, and your current folder is the Glob workspace. Use your own tools for pages and
notes: Read, Write, Edit, Glob and Grep. There is no limit on how many pages you write, so finish the whole job
in this one message. You can look at generated images with Read. You have no shell.

Everything else is a tool on the `glob` server, because those need the app or spend credits:
- generate (spends credits), pick_winner, models, skill
- move, delete (to .trash/, recoverable), create_folder: you have no shell to do these yourself
- service_tools, service_tool_info, service_call (may spend credits), save_url
- request_access, import_game, copy_in: for folders outside the workspace

Agents get a limit of 8 generations before the user's next message; pages and other file work don't count. When a
generation is refused for that reason, tell the user what is left and ask them to confirm.

Folders elsewhere on the user's computer: you can reach only the folders listed under "Granted folders" in the
context. When the user mentions another one, call request_access right away (the user gets Allow / Deny buttons),
then end your reply and continue when they answer. Never tell the user you cannot access their computer, and
never ask them to copy files by hand.

Skills: before you generate, call a service, or answer how something was made or what a service can do, open the
matching skill (listed under "Skills" in the context) unless you already read it in this chat."""


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties, "required": required}}


_PATH = {"type": "string", "description": "Path relative to the workspace root."}
MCP_TOOLS: list[dict[str, Any]] = [
    _tool("generate", "Start a generation for an asset page; spends the user's credits. action: candidates (new images from the page's prompt), "
          "variant or edit (need instruction, work on the winner), views (turnaround of the winner), prop_sheet (a level: every prop of its picture on one sheet), "
          "make_3d, rig, animate (needs action_ids). Sounds use candidates. Read the generating skill first.", {
              "path": _PATH,
              "action": {"type": "string", "enum": ["candidates", "variant", "edit", "views", "prop_sheet", "make_3d", "rig", "animate"]},
              "count": {"type": "integer", "minimum": 1},
              "model": {"type": "string", "description": "Optional; the default model otherwise. See the models tool."},
              "instruction": {"type": "string"},
              "source": {"type": "string", "description": "Image to work on instead of the winner."},
              "action_ids": {"type": "array", "items": {"type": ["integer", "string"]}, "description": "Meshy: animation ids. Tripo: presets such as preset:walk."},
          }, ["path", "action"]),
    _tool("pick_winner", "Choose the winning image for an asset page.", {"path": _PATH, "file": {"type": "string", "description": "Workspace path of the chosen image."}}, ["path", "file"]),
    _tool("models", "List every image or 3D model and how it is reached.", {"kind": {"type": "string", "enum": ["image", "3d"]}}, []),
    _tool("skill", "Open a how-to guide listed under Skills in the context.", {"name": {"type": "string"}}, ["name"]),
    _tool("move", "Move or rename a file or folder.", {"from": _PATH, "to": _PATH}, ["from", "to"]),
    _tool("delete", "Move a file or folder to .trash/ (recoverable).", {"path": _PATH}, ["path"]),
    _tool("create_folder", "Create a folder.", {"path": _PATH}, ["path"]),
    _tool("request_access", "Ask the user for access to a folder outside the workspace. End your reply after calling it.", {
        "path": {"type": "string", "description": "Absolute path."}, "reason": {"type": "string"}, "write": {"type": "boolean"}}, ["path"]),
    _tool("import_game", "Import a Godot, Unity or Unreal project (or an asset folder) from a granted folder into numbered sections.", {"path": {"type": "string", "description": "Absolute path."}}, ["path"]),
    _tool("copy_in", "Copy a file or folder from a granted folder into the workspace.", {"from": {"type": "string", "description": "Absolute path."}, "to": _PATH}, ["from", "to"]),
    _tool("service_tools", "List a connected service's tools; query searches them.", {"service": {"type": "string"}, "query": {"type": "string"}}, ["service"]),
    _tool("service_tool_info", "One service tool's description and input schema.", {"service": {"type": "string"}, "name": {"type": "string"}}, ["service", "name"]),
    _tool("service_call", "Run a service tool on the user's account. It may spend credits: only when the user asked, and never just to retry.", {
        "service": {"type": "string"}, "name": {"type": "string"}, "arguments": {"type": "object"}}, ["service", "name"]),
    _tool("save_url", "Download an https result into the workspace.", {"url": {"type": "string"}, "to": _PATH}, ["url", "to"]),
]
_TOOL_NAMES = {tool["name"] for tool in MCP_TOOLS}
_BLOCKING_TOOLS = {"models", "import_game", "copy_in", "service_tools", "service_tool_info", "service_call", "save_url"}
_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


async def handle_rpc(agent: Any, project: str, session_id: str, request: dict[str, Any]) -> dict[str, Any] | None:
    """Glob's own tools for a running chat. None for a notification, which has no answer."""
    method = str(request.get("method") or "")
    if method.startswith("notifications/") or "id" not in request:
        return None
    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request["id"]}
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        reply["result"] = {"protocolVersion": asked if asked in _PROTOCOL_VERSIONS else _PROTOCOL_VERSIONS[0], "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER, "version": "1"}}
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": MCP_TOOLS}
    elif method == "tools/call":
        reply["result"] = await _call_tool(agent, project, session_id, str(params.get("name") or ""), params.get("arguments") or {})
    else:
        reply["error"] = {"code": -32601, "message": f"Method not found: {method}"}
    return reply


async def _call_tool(agent: Any, project: str, session_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    call = {**arguments, "tool": name}
    try:
        if name not in _TOOL_NAMES:
            raise ValueError(f"Unknown tool '{name}'.")
        # generate starts its job on the event loop, so it stays on it; the rest wait on the network or the disk.
        outcome = await asyncio.to_thread(agent.run_tool, project, call) if name in _BLOCKING_TOOLS else agent.run_tool(project, call)
    except Exception as exc:  # noqa: BLE001 — a tool failure goes back to the model as a readable result
        detail = f"{name} failed: {exc}"
        agent.note_tool_action(session_id, {"tool": name, "path": arguments.get("path") or arguments.get("to"), "ok": False, "summary": detail})
        return {"content": [{"type": "text", "text": detail}], "isError": True}
    result = outcome.pop("result", None)
    agent.note_tool_action(session_id, {**outcome, "ok": True})
    text = str(result) if result is not None else str(outcome.get("summary") or "Done.")
    if outcome.get("access_request"):
        text += "\nThe user now sees Allow / Deny buttons. End your reply here; you continue when they answer."
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _rule_path(path: Path) -> str:
    """A path as Claude Code's permission rules write it: //d/Games/My Game on Windows too."""
    text = path.as_posix()
    if len(text) > 1 and text[1] == ":":
        return f"//{text[0].lower()}{text[2:]}"
    return f"/{text}"


def permission_rules(ws: Path, grants: list[dict[str, Any]], config_dir: Path) -> dict[str, list[str]]:
    """What `forge.access` promises, for tools that don't pass through it. --restricted already keeps them out of everything else."""
    deny = [f"{tool}({_rule_path(ws)}/inbox/**)" for tool in ("Edit", "Write")]  # the chats themselves
    deny += [f"{tool}({_rule_path(config_dir)}/**)" for tool in FILE_TOOLS]
    for grant in grants:
        root = _rule_path(Path(grant["path"]))
        if not grant.get("write"):
            deny += [f"{tool}({root}/**)" for tool in ("Edit", "Write")]
        deny += [f"{tool}({root}/**/{pattern})" for pattern in SECRET_FILE_PATTERNS for tool in ("Read", "Edit", "Write")]
    return {"allow": [*FILE_TOOLS, f"mcp__{SERVER}"], "deny": deny}


def build_command(exe: str, model: str, effort: str, ws: Path, folder: Path, system_prompt: str, prompt: str, images: list[tuple[str, str]],
                  mcp_url: str, resume: str = "") -> tuple[list[str], str]:
    """The argv for one chat message and the text fed on stdin. Its files go in `folder`."""
    grants = [grant for grant in access.list_grants() if grant.get("exists")]
    (folder / "system.md").write_text(system_prompt, encoding="utf-8")
    (folder / "settings.json").write_text(json.dumps({"permissions": permission_rules(ws, grants, get_config_dir().resolve())}), encoding="utf-8")
    (folder / "mcp.json").write_text(json.dumps({"mcpServers": {SERVER: {"type": "http", "url": mcp_url}}}), encoding="utf-8")
    cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--restricted", "--permission-mode", "dontAsk", "--tools", ",".join(FILE_TOOLS),
           "--append-system-prompt-file", str(folder / "system.md"), "--settings", str(folder / "settings.json"),
           "--strict-mcp-config", "--mcp-config", str(folder / "mcp.json"), "--thinking-display", "summarized"]
    cmd += ["--add-dir", *[grant["path"] for grant in grants]] if grants else []
    cmd += ["--resume", resume] if resume else []
    cmd += (["--model", model] if model else []) + (["--effort", effort] if effort in cli_models.CLI_EFFORTS["claude"] else [])
    content: list[dict[str, Any]] = [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}} for mime, data in images[:4]]
    content.append({"type": "text", "text": prompt})
    return cmd, json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


_VERBS = {"Read": ("read", "Read"), "Write": ("write", "Wrote"), "Edit": ("edit", "Edited"), "Glob": ("list", "Searched for"), "Grep": ("list", "Searched for")}
_FILE_CHANGES = {"Write", "Edit"}


class Events:
    """Turns the CLI's stream into the chat's live steps and the list of what was done."""

    def __init__(self, ws: Path, report: Callable[[dict[str, Any]], None], drain_tool_actions: Callable[[], list[dict[str, Any]]]) -> None:
        self.ws, self.report, self.drain_tool_actions = ws, report, drain_tool_actions
        self.progress = cli_models._Progress(cli_models.progress_hook.get())
        self.running: dict[str, dict[str, Any]] = {}
        self.actions: list[dict[str, Any]] = []
        self.session_id, self.reply, self.error = "", "", ""

    def _rel(self, raw: Any) -> str:
        text = str(raw or "")
        try:
            return Path(text).resolve().relative_to(self.ws.resolve()).as_posix() if text else ""
        except (ValueError, OSError):
            return text

    def _target(self, tool: str, data: dict[str, Any]) -> str:
        if tool in ("Glob", "Grep"):
            return str(data.get("pattern") or "")
        if tool in _VERBS:
            return self._rel(data.get("file_path"))
        return str(data.get("path") or data.get("to") or data.get("name") or "")

    def _started(self, block: dict[str, Any]) -> None:
        tool = str(block.get("name") or "")
        data = block.get("input") if isinstance(block.get("input"), dict) else {}
        target = self._target(tool, data)
        shown = _VERBS[tool][0] if tool in _VERBS else tool.removeprefix(TOOL_PREFIX)
        self.running[str(block.get("id"))] = {"tool": tool, "target": target}
        self.report({"phase": "acting", "step": {"id": str(block.get("id")), "tool": shown, "path": target, "state": "running"}})

    def _finished(self, block: dict[str, Any]) -> None:
        call_id = str(block.get("tool_use_id"))
        started = self.running.pop(call_id, None)
        if not started:
            return
        tool, target = started["tool"], started["target"]
        failed = bool(block.get("is_error"))
        self.actions += self.drain_tool_actions()
        if tool in _VERBS:
            kind, verb = _VERBS[tool]
            summary = f"{verb} {target}".strip()
            if failed:
                summary = f"{kind} {target} failed: {_result_text(block)[:200]}"
            action = {"tool": kind, "path": target, "summary": summary, "ok": not failed}
            if tool in _FILE_CHANGES and not failed:
                action["changed"] = True
            self.actions.append(action)
        else:
            summary = (self.actions[-1].get("summary") if self.actions else "") or f"Ran {tool.removeprefix(TOOL_PREFIX)}"
            if failed and not str(summary).startswith(tool.removeprefix(TOOL_PREFIX)):
                summary = f"{tool.removeprefix(TOOL_PREFIX)} failed: {_result_text(block)[:200]}"
                self.actions.append({"tool": tool.removeprefix(TOOL_PREFIX), "path": target, "summary": summary, "ok": False})
        self.report({"phase": "acting", "step_done": {"id": call_id, "summary": summary, "state": "failed" if failed else "done"}})

    def feed(self, line: str) -> None:
        self.progress.feed(line)
        try:
            data = json.loads(line)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "system" and data.get("session_id"):
            self.session_id = str(data["session_id"])
        elif kind == "assistant":
            for block in (data.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and str(block.get("text") or "").strip():
                    self.report({"step": {"say": str(block["text"]).strip()[:2000]}})
                elif block.get("type") == "tool_use":
                    self._started(block)
        elif kind == "user":
            content = (data.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    self._finished(block)
        elif kind == "result":
            self.session_id = str(data.get("session_id") or self.session_id)
            if data.get("is_error"):
                self.error = str(data.get("result") or "; ".join(str(e) for e in data.get("errors") or []) or "The run failed.")
            else:
                self.reply = str(data.get("result") or "").strip()


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return re.sub(r"</?tool_use_error>", "", str(content or "")).strip()


class ResumeFailed(RuntimeError):
    """The CLI no longer has this conversation (cleared, or its files were moved)."""


def run(ws: Path, model: str, effort: str, system_prompt: str, prompt: str, images: list[tuple[str, str]], mcp_url: str, resume: str,
        report: Callable[[dict[str, Any]], None], drain_tool_actions: Callable[[], list[dict[str, Any]]],
        on_start: Callable[[subprocess.Popen[str]], None] | None = None) -> Events:
    """One chat message, start to finish. Raises RuntimeError when it can't run; a run that fails partway returns with `error` set."""
    exe = cli_models.find("claude")
    if not exe:
        raise RuntimeError(f"Claude Code is not installed. Install it with: {cli_models.CLIS['claude']['install']}")
    events = Events(ws, report, drain_tool_actions)
    with tempfile.TemporaryDirectory(prefix="glob-chat-", ignore_cleanup_errors=True) as tmp:
        cmd, stdin_text = build_command(exe, model, effort, ws, Path(tmp), system_prompt, prompt, images, mcp_url, resume)
        try:
            # No limit on the run as a whole: only silence ends it, or the user's Stop.
            proc = cli_models._run_streaming(cmd, stdin_text, str(ws), {**cli_models.child_env(), "NO_COLOR": "1"}, cli_models.TIMEOUT, events.feed, max_total=math.inf, on_start=on_start)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Claude Code stopped: it went {cli_models.TIMEOUT // 60} minutes without output.") from exc
        except OSError as exc:
            raise RuntimeError(f"Could not start Claude Code: {exc}") from exc
    events.actions += events.drain_tool_actions()
    tail = (events.error or proc.stderr.strip() or "")[-600:]
    if resume and "no conversation found" in tail.lower():
        raise ResumeFailed(tail)
    if "unknown option" in tail.lower():
        # Never run it without --restricted: that would lift the limits on its file tools.
        raise RuntimeError(f"This version of Claude Code can't run here yet. Run `claude update` in a terminal, or turn off \"Claude Code works directly\" in Settings.\n\n{tail}")
    if events.reply or events.session_id and not tail:
        cli_models._login["claude"] = "ok"
    elif any(word in tail.lower() for word in ("login", "log in", "auth", "api key", "credential", "unauthorized")):
        cli_models._login["claude"] = "login"
        events.error = f"Claude Code isn't logged in: run `{cli_models.CLIS['claude']['login']}` in a terminal, or press Log in under Settings → Chat models.\n\n{tail}"
    elif not events.error and proc.returncode:
        events.error = tail or f"Claude Code stopped (exit {proc.returncode})."
    return events

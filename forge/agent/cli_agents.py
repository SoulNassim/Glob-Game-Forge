"""Coding-agent CLIs (Claude Code, Codex, OpenCode, Gemini CLI) as chat tabs inside a Glob workspace.

Each CLI runs headless in the project's workspace folder with the user's own login, one turn per
message, resuming its own session id. Several tabs can run at the same time. Output is parsed from
the CLI's JSON event stream into text and short action summaries for the chat.
Claude Code also gets Glob's MCP server, so it can use Glob's asset tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

CLIS: dict[str, dict[str, Any]] = {
    "claude": {"label": "Claude Code", "bin": "claude", "install": "npm install -g @anthropic-ai/claude-code", "login": "claude", "site": "https://claude.com/claude-code"},
    "codex": {"label": "Codex", "bin": "codex", "install": "npm install -g @openai/codex", "login": "codex login", "site": "https://github.com/openai/codex"},
    "opencode": {"label": "OpenCode", "bin": "opencode", "install": "npm install -g opencode-ai", "login": "opencode auth login", "site": "https://opencode.ai"},
    "gemini": {"label": "Gemini CLI", "bin": "gemini", "install": "npm install -g @google/gemini-cli", "login": "gemini", "site": "https://github.com/google-gemini/gemini-cli"},
}
WORKSPACE_NOTE = (
    "You are working inside a Glob game-project workspace. Conventions: every top-level folder is a section "
    "(numeric prefix sets order), a folder's README.md is its page, an asset is a .md whose YAML header has "
    "`kind:` and `prompt:`, and pillars/ folders hold style reference images and style-bible text."
)
# Models each CLI accepts by name. "" means the CLI's own default (whatever the user set up there).
# OpenCode lists its models itself; every CLI also takes any model id typed in by hand.
CLI_MODELS: dict[str, list[tuple[str, str]]] = {
    "claude": [("fable", "Fable (latest)"), ("opus", "Opus (latest)"), ("sonnet", "Sonnet (latest)"), ("haiku", "Haiku (latest)")],
    "codex": [("gpt-5-codex", "GPT-5 Codex"), ("gpt-5", "GPT-5")],
    "opencode": [],
    "gemini": [("gemini-2.5-pro", "Gemini 2.5 Pro"), ("gemini-2.5-flash", "Gemini 2.5 Flash")],
}
CLI_EFFORTS: dict[str, list[str]] = {
    "claude": ["low", "medium", "high", "xhigh", "max"],
    "codex": ["low", "medium", "high"],
}
_running: dict[str, subprocess.Popen] = {}
_model_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def detect() -> list[dict[str, Any]]:
    out = []
    for cli_id, spec in CLIS.items():
        path = shutil.which(spec["bin"])
        out.append({"id": cli_id, **spec, "installed": bool(path), "path": path or ""})
    return out


def _opencode_models() -> list[dict[str, str]]:
    exe = shutil.which("opencode")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ids = [line.strip() for line in out.splitlines() if "/" in line and " " not in line.strip()]
    return [{"id": i, "label": i.split("/", 1)[1], "group": i.split("/", 1)[0]} for i in ids]


def models(cli_id: str) -> dict[str, Any]:
    """The models and reasoning efforts a CLI tab can pick from."""
    if cli_id not in CLIS:
        raise ValueError(f"Unknown coding agent '{cli_id}'.")
    cached = _model_cache.get(cli_id)
    if cached and time.time() - cached[0] < 300:
        items = cached[1]
    else:
        label = CLIS[cli_id]["label"]
        items = _opencode_models() if cli_id == "opencode" else [{"id": i, "label": l, "group": label} for i, l in CLI_MODELS[cli_id]]
        _model_cache[cli_id] = (time.time(), items)
    return {"models": items, "efforts": CLI_EFFORTS.get(cli_id, [])}


def _mcp_config() -> str:
    return json.dumps({"mcpServers": {"glob": {"command": sys.executable, "args": ["-m", "forge.cli", "--mcp"]}}})


def _granted_dirs() -> list[str]:
    from forge import access

    return [g["path"] for g in access.list_grants() if g["exists"]]


def build_command(cli_id: str, prompt: str, resume: str, allow_edits: bool, model: str = "", effort: str = "", extra_dirs: list[str] | None = None) -> tuple[list[str], str | None]:
    """The argv for one turn and the text to feed on stdin (None when the prompt is an argument).

    `extra_dirs` are folders the user granted in Glob (Settings → Storage & access); CLIs that support it
    may read them too."""
    spec = CLIS[cli_id]
    dirs = list(extra_dirs or [])
    exe = shutil.which(spec["bin"]) or spec["bin"]
    effort = effort if effort in CLI_EFFORTS.get(cli_id, []) else ""
    if cli_id == "claude":
        cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--append-system-prompt", WORKSPACE_NOTE, "--mcp-config", _mcp_config()]
        cmd += (["--model", model] if model else []) + (["--effort", effort] if effort else [])
        for folder in dirs:
            cmd += ["--add-dir", folder]
        if allow_edits:
            cmd += ["--permission-mode", "acceptEdits"]
        if resume:
            cmd += ["--resume", resume]
        return cmd, prompt
    if cli_id == "codex":
        cmd = [exe, "exec"] + (["resume", resume] if resume else []) + ["--json", "--skip-git-repo-check"]
        cmd += (["-m", model] if model else []) + (["-c", f"model_reasoning_effort={effort}"] if effort else [])
        if allow_edits:
            cmd += ["--sandbox", "workspace-write"]
        for folder in dirs:
            cmd += ["--add-dir", folder]
        return cmd + ["-"], prompt
    if cli_id == "opencode":
        cmd = [exe, "run", "--format", "json"] + (["--model", model] if model else [])
        if allow_edits:
            cmd.append("--auto")
        if resume:
            cmd += ["--session", resume]
        return cmd + [prompt], None
    cmd = [exe, "--output-format", "stream-json"] + (["-m", model] if model else [])
    if dirs:
        cmd += ["--include-directories", ",".join(dirs)]
    if allow_edits:
        cmd += ["--approval-mode", "auto_edit"]
    if resume:
        cmd += ["--resume", resume]
    return cmd + ["-p", prompt], None


def _short(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_summary(name: str, args: Any) -> str:
    args = args if isinstance(args, dict) else {}
    target = args.get("file_path") or args.get("path") or args.get("filePath") or args.get("command") or args.get("pattern") or args.get("url") or ""
    return f"{name} {_short(target, 70)}".strip()


def parse_event(cli_id: str, line: str) -> list[dict[str, Any]]:
    """Turn one output line into chat events: {"kind": "text"|"tool"|"session"|"error", ...}."""
    try:
        data = json.loads(line)
    except ValueError:
        return [{"kind": "text", "text": line + "\n"}] if line.strip() else []
    if not isinstance(data, dict):
        return []
    events: list[dict[str, Any]] = []
    sid = data.get("session_id") or data.get("sessionID") or data.get("thread_id")
    if sid:
        events.append({"kind": "session", "id": str(sid)})
    kind = data.get("type")
    if cli_id == "claude":
        if kind == "assistant":
            for block in (data.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    events.append({"kind": "text", "text": block["text"]})
                elif block.get("type") == "tool_use":
                    events.append({"kind": "tool", "text": _tool_summary(block.get("name", "tool"), block.get("input"))})
        elif kind == "result" and data.get("is_error"):
            events.append({"kind": "error", "text": _short(data.get("result") or "The run failed.", 400)})
        return events
    if cli_id == "codex":
        item = data.get("item") or {}
        if kind == "item.completed":
            if item.get("type") == "agent_message" and item.get("text"):
                events.append({"kind": "text", "text": item["text"]})
            elif item.get("type") == "command_execution":
                events.append({"kind": "tool", "text": f"ran {_short(item.get('command', ''), 70)}"})
            elif item.get("type") == "file_change":
                for change in item.get("changes") or []:
                    events.append({"kind": "tool", "text": f"{change.get('kind', 'edit')} {change.get('path', '')}"})
        elif kind in ("error", "turn.failed"):
            events.append({"kind": "error", "text": _short(data.get("message") or (data.get("error") or {}).get("message") or "The run failed.", 400)})
        return events
    if cli_id == "opencode":
        part = data.get("part") or {}
        if kind == "text" and part.get("text"):
            events.append({"kind": "text", "text": part["text"]})
        elif part.get("type") == "tool" or kind in ("tool", "tool_use"):
            state = part.get("state") or {}
            events.append({"kind": "tool", "text": state.get("title") or _tool_summary(part.get("tool", "tool"), state.get("input"))})
        elif kind == "error":
            events.append({"kind": "error", "text": _short(data.get("error") or data.get("message") or "The run failed.", 400)})
        return events
    # Gemini CLI and anything else: take whatever text it reports.
    text = data.get("text") or data.get("content") or data.get("response")
    if isinstance(text, str) and text:
        events.append({"kind": "text", "text": text})
    elif kind in ("tool_use", "tool_call"):
        events.append({"kind": "tool", "text": _tool_summary(data.get("tool_name") or data.get("name") or "tool", data.get("parameters") or data.get("args"))})
    return events


async def run_turn(
    key: str,
    cli_id: str,
    cwd: Path,
    prompt: str,
    resume: str,
    allow_edits: bool,
    on_event: Callable[[dict[str, Any]], Any],
    model: str = "",
    effort: str = "",
) -> dict[str, Any]:
    """Run one turn; `on_event` gets every parsed event as it arrives. Returns text, actions and session id."""
    if cli_id not in CLIS:
        raise ValueError(f"Unknown coding agent '{cli_id}'.")
    if not shutil.which(CLIS[cli_id]["bin"]):
        raise RuntimeError(f"{CLIS[cli_id]['label']} is not installed. Install it in Settings → Coding agents.")
    cmd, stdin_text = build_command(cli_id, prompt, resume, allow_edits, model, effort, _granted_dirs())
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stderr_tail: list[str] = []

    def pump() -> None:
        # A thread with Popen works the same under every asyncio loop on Windows, macOS and Linux.
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env={**os.environ, "NO_COLOR": "1"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("fail", str(exc)))
            return
        _running[key] = proc
        if stdin_text is not None:
            try:
                proc.stdin.write(stdin_text)
            except OSError:
                pass
        proc.stdin.close()
        threading.Thread(target=lambda: stderr_tail.extend(proc.stderr.read().splitlines()[-12:]), daemon=True).start()
        for line in proc.stdout:
            loop.call_soon_threadsafe(queue.put_nowait, ("line", line.rstrip("\n")))
        proc.wait()
        loop.call_soon_threadsafe(queue.put_nowait, ("exit", proc.returncode))

    threading.Thread(target=pump, daemon=True).start()
    texts: list[str] = []
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    session_id = resume
    started = time.time()
    try:
        while True:
            tag, value = await queue.get()
            if tag == "fail":
                raise RuntimeError(f"Could not start {CLIS[cli_id]['label']}: {value}")
            if tag == "exit":
                code = value
                break
            for event in parse_event(cli_id, value):
                if event["kind"] == "session":
                    session_id = event["id"]
                    continue
                if event["kind"] == "text":
                    texts.append(event["text"])
                elif event["kind"] == "tool":
                    actions.append({"tool": "cli", "summary": event["text"], "ok": True, "changed": True})
                elif event["kind"] == "error":
                    errors.append(event["text"])
                await on_event(event)
    finally:
        _running.pop(key, None)
    text = "\n\n".join(t.strip() for t in texts if t.strip())
    if code and not text:
        tail = "\n".join(stderr_tail[-6:]) or "\n".join(errors)
        needs_login = any(word in tail.lower() for word in ("login", "log in", "auth", "api key", "credential", "unauthorized"))
        raise RuntimeError(
            f"{CLIS[cli_id]['label']} stopped (exit {code})."
            + (" It looks like it isn't logged in: open Settings → Coding agents and press Log in." if needs_login else "")
            + (f"\n\n{tail[-600:]}" if tail else "")
        )
    if errors and not text:
        text = "\n".join(errors)
    return {"text": text or "Done.", "actions": actions, "session_id": session_id, "seconds": round(time.time() - started, 1)}


def stop(key: str) -> bool:
    proc = _running.get(key)
    if not proc:
        return False
    proc.terminate()
    return True


def is_running(key: str) -> bool:
    return key in _running


def open_login(cli_id: str) -> None:
    """Open a real terminal window running the CLI's login, so the user signs in their usual way."""
    spec = CLIS[cli_id]
    command = spec["login"] if shutil.which(spec["bin"]) else spec["install"]
    if sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", command])
    elif sys.platform == "darwin":
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{command}"'])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("konsole") or shutil.which("xterm")
        if not term:
            raise RuntimeError(f"No terminal found. Run this yourself: {command}")
        subprocess.Popen([term, "-e", "bash", "-lc", f"{command}; exec bash"])

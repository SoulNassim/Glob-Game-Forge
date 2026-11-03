"""Coding-agent CLIs (Claude Code, Codex, Gemini CLI, OpenCode) as models behind the Glob chat.

Each CLI runs headless with the user's own login (their subscription, no API key), one call per model turn.
Its own tools are switched off where the CLI allows it: Glob's chat drives the workspace tools itself, so the
CLI only has to answer in Glob's chat protocol. Model ids look like ``cli:<agent>/<model>``; an empty model
means the CLI's own default.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CLIS: dict[str, dict[str, Any]] = {
    "claude": {"label": "Claude Code", "bin": "claude", "install": "npm install -g @anthropic-ai/claude-code", "login": "claude", "site": "https://claude.com/claude-code"},
    "codex": {"label": "Codex", "bin": "codex", "install": "npm install -g @openai/codex", "login": "codex login", "site": "https://github.com/openai/codex"},
    "gemini": {"label": "Gemini CLI", "bin": "gemini", "install": "npm install -g @google/gemini-cli", "login": "gemini", "site": "https://github.com/google-gemini/gemini-cli"},
    "opencode": {"label": "OpenCode", "bin": "opencode", "install": "npm install -g opencode-ai", "login": "opencode auth login", "site": "https://opencode.ai"},
}
# Models each CLI accepts by name. OpenCode lists its own; "" is the CLI's default.
CLI_MODELS: dict[str, list[tuple[str, str]]] = {
    "claude": [("", "Default"), ("fable", "Fable"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")],
    "codex": [("", "Default"), ("gpt-5-codex", "GPT-5 Codex"), ("gpt-5", "GPT-5")],
    "gemini": [("", "Default"), ("gemini-2.5-pro", "Gemini 2.5 Pro"), ("gemini-2.5-flash", "Gemini 2.5 Flash")],
    "opencode": [("", "Default")],
}
CLI_EFFORTS: dict[str, list[str]] = {"claude": ["low", "medium", "high", "max"], "codex": ["low", "medium", "high"]}
TIMEOUT = 600
_model_cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}
# What the last real call or check said about each CLI: "ok", "login" (needs a login) or "error" (didn't answer).
# Nothing recorded = not checked yet.
# Only "ok" counts as connected: an installed CLI may still be logged out.
_login: dict[str, str] = {}


def login_status(cli_id: str) -> str:
    """ok (a call worked), login (it needs a login), error (it didn't answer), unknown (not checked yet) or missing."""
    if not find(cli_id):
        return "missing"
    return _login.get(cli_id, "unknown")


def _search_path() -> str:
    """PATH plus the usual install folders: an app started from the Dock or Finder gets a bare PATH."""
    home = Path.home()
    extra = [home / ".local/bin", home / ".claude/local", home / ".npm-global/bin", home / ".bun/bin", home / ".opencode/bin",
             home / "Library/pnpm", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    extra += sorted((home / ".nvm/versions/node").glob("*/bin"), reverse=True)
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        extra.append(Path(os.environ["APPDATA"]) / "npm")
    parts = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(parts + [str(p) for p in extra if str(p) not in parts])


# Set by a running Claude Code session for its own child processes. A coding agent that started Glob passes them
# on, and a `claude` Glob runs with them would talk to that session's (soon gone) proxy instead of the user's login.
_SESSION_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")


def child_env() -> dict[str, str]:
    """The environment for a CLI Glob runs: the user's own, with the PATH widened and no parent agent session."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SESSION_PREFIXES)}
    if "CLAUDE_CODE_ENTRYPOINT" in os.environ:
        env.pop("ANTHROPIC_BASE_URL", None)
    env["PATH"] = _search_path()
    return env


_found: dict[str, tuple[float, str | None]] = {}


def find(cli_id: str) -> str | None:
    """The CLI's executable. Searching a long PATH takes tens of milliseconds per CLI, so the answer is kept a minute."""
    hit = _found.get(cli_id)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    exe = shutil.which(CLIS[cli_id]["bin"], path=_search_path())
    _found[cli_id] = (time.time(), exe)
    return exe


def detect() -> list[dict[str, Any]]:
    return [{"id": cli_id, **spec, "installed": bool(find(cli_id)), "status": login_status(cli_id)} for cli_id, spec in CLIS.items()]


def _opencode_models(exe: str) -> list[tuple[str, str]]:
    try:
        out = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                             env=child_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [(line.strip(), line.strip()) for line in out.splitlines() if "/" in line and " " not in line.strip()]


def list_models() -> list[dict[str, Any]]:
    """One entry per (installed CLI, model), for the chat's model picker."""
    out = []
    for cli_id, spec in CLIS.items():
        exe = find(cli_id)
        if not exe:
            continue
        ready = _login.get(cli_id) == "ok"
        items = CLI_MODELS[cli_id]
        if cli_id == "opencode":
            cached = _model_cache.get(cli_id)
            if not cached or time.time() - cached[0] > 300:
                cached = (time.time(), _opencode_models(exe))
                _model_cache[cli_id] = cached
            items = items + cached[1]
        # Labels name the agent too: the chat's model pill shows the label alone.
        out += [{"id": f"cli:{cli_id}/{mid}", "label": spec["label"] if not mid else f"{spec['label']} · {label}", "provider": "cli", "group": f"{spec['label']} · your login",
                 "ready": ready, "status": login_status(cli_id), "efforts": CLI_EFFORTS.get(cli_id, [])} for mid, label in items]
    return out


def _write_images(folder: Path, images: list[tuple[str, str]]) -> list[str]:
    paths = []
    for i, (mime, data) in enumerate(images[:4]):
        path = folder / f"image{i}.{mime.split('/')[-1].replace('jpeg', 'jpg')}"
        path.write_bytes(base64.b64decode(data))
        paths.append(str(path))
    return paths


def build_command(cli_id: str, exe: str, model: str, effort: str, system_prompt: str, prompt: str,
                  images: list[tuple[str, str]], folder: Path) -> tuple[list[str], str]:
    """The argv for one call and the text fed on stdin."""
    effort = effort if effort in CLI_EFFORTS.get(cli_id, []) else ""
    combined = f"{system_prompt}\n\n---\n\n{prompt}"
    if cli_id == "claude":
        # stream-json input carries images; --tools "" leaves only text, and no user MCP servers are loaded.
        # The system prompt goes in a file: on Windows `claude` is a .cmd, and cmd.exe stops at 8191 characters.
        (folder / "system.md").write_text(system_prompt, encoding="utf-8")
        cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--system-prompt-file", str(folder / "system.md"), "--tools", "", "--strict-mcp-config"]
        cmd += (["--model", model] if model else []) + (["--effort", effort] if effort else [])
        content: list[dict[str, Any]] = [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}} for mime, data in images[:4]]
        content.append({"type": "text", "text": prompt})
        return cmd, json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
    if cli_id == "codex":
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--output-last-message", str(folder / "reply.txt")]
        cmd += (["-m", model] if model else []) + (["-c", f"model_reasoning_effort={effort}"] if effort else [])
        for path in _write_images(folder, images):
            cmd += ["-i", path]
        return cmd + ["-"], combined
    if cli_id == "gemini":
        # Gemini appends stdin to the -p text.
        return [exe, "--output-format", "json", *(["-m", model] if model else []), "-p", "Follow the instructions above."], combined
    (folder / "prompt.md").write_text(combined, encoding="utf-8")
    cmd = [exe, "run", "--format", "json", *(["--model", model] if model else []), "-f", str(folder / "prompt.md")]
    return cmd + ["Follow the attached prompt.md exactly and reply as it asks."], ""


def parse_output(cli_id: str, stdout: str, folder: Path) -> tuple[str, str]:
    """(reply text, error text) from a finished run."""
    if cli_id == "codex":
        reply = folder / "reply.txt"
        return (reply.read_text(encoding="utf-8").strip() if reply.exists() else stdout.strip()), ""
    if cli_id == "gemini":
        try:
            data = json.loads(stdout)
        except ValueError:
            return stdout.strip(), ""
        return str(data.get("response") or "").strip(), str((data.get("error") or {}).get("message") or "")
    texts, error = [], ""
    for line in stdout.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if cli_id == "claude" and data.get("type") == "result":
            if data.get("is_error"):
                error = str(data.get("result") or "The run failed.")
            else:
                texts = [str(data.get("result") or "")]
        elif cli_id == "opencode":
            part = data.get("part") or {}
            if data.get("type") == "text" and part.get("text"):
                texts.append(part["text"])
            elif data.get("type") == "error":
                error = str(data.get("error") or data.get("message") or "The run failed.")
    return "".join(texts).strip(), error


async def call(model_id: str, prompt: str, system_prompt: str, images: list[tuple[str, str]], effort: str) -> dict[str, Any]:
    cli_id, _, model = model_id.removeprefix("cli:").partition("/")
    if cli_id not in CLIS:
        raise RuntimeError(f"Unknown coding agent '{cli_id}'.")
    label = CLIS[cli_id]["label"]
    exe = find(cli_id)
    if not exe:
        raise RuntimeError(f"{label} is not installed. Install it with: {CLIS[cli_id]['install']}")
    # An empty folder of its own, so the CLI picks up no project instructions or files.
    with tempfile.TemporaryDirectory(prefix="glob-chat-") as tmp:
        folder = Path(tmp)
        cmd, stdin_text = build_command(cli_id, exe, model, effort, system_prompt, prompt, images, folder)
        try:
            proc = await asyncio.to_thread(
                subprocess.run, cmd, input=stdin_text, cwd=tmp, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=TIMEOUT, env={**child_env(), "NO_COLOR": "1"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{label} did not answer within {TIMEOUT // 60} minutes.") from exc
        except OSError as exc:
            raise RuntimeError(f"Could not start {label}: {exc}") from exc
        text, error = parse_output(cli_id, proc.stdout, folder)
    if not text:
        tail = (error or proc.stderr.strip() or proc.stdout.strip())[-600:]
        needs_login = any(w in tail.lower() for w in ("login", "log in", "auth", "api key", "credential", "unauthorized"))
        if needs_login:
            _login[cli_id] = "login"
        raise RuntimeError(
            f"{label} returned no answer (exit {proc.returncode})."
            + (f" It looks like it isn't logged in: run `{CLIS[cli_id]['login']}` in a terminal, or press Log in under Settings → Chat models." if needs_login else "")
            + (f"\n\n{tail}" if tail else "")
        )
    _login[cli_id] = "ok"
    return {"text": text, "model": f"{label}{' · ' + model if model else ''}", "provider": "cli"}


# The cheapest model to ask "are you there?" with.
CHECK_MODELS = {"claude": "haiku", "codex": "", "gemini": "gemini-2.5-flash", "opencode": ""}


async def check(cli_id: str) -> dict[str, Any]:
    """Whether the CLI is installed and answers, i.e. is logged in."""
    if cli_id not in CLIS:
        raise ValueError(f"Unknown coding agent '{cli_id}'.")
    _found.pop(cli_id, None)  # it may have been installed a moment ago
    if not find(cli_id):
        return {"id": cli_id, "installed": False, "ok": False, "needs_login": False, "error": ""}
    try:
        await call(f"cli:{cli_id}/{CHECK_MODELS[cli_id]}", "Reply with exactly: ok", "You are a connection test.", [], "low")
    except RuntimeError as exc:
        text = str(exc)
        needs_login = _login.get(cli_id) == "login"
        if not needs_login:
            _login[cli_id] = "error"  # installed, but it didn't answer for another reason
        return {"id": cli_id, "installed": True, "ok": False, "needs_login": needs_login, "error": text[:400]}
    return {"id": cli_id, "installed": True, "ok": True, "needs_login": False, "error": ""}


def open_login(cli_id: str) -> None:
    """Open a real terminal window running the CLI's login (or install), so the user signs in their usual way."""
    spec = CLIS[cli_id]
    command = spec["login"] if find(cli_id) else spec["install"]
    if sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", command], env=child_env())
    elif sys.platform == "darwin":
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{command}"'])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("konsole") or shutil.which("xterm")
        if not term:
            raise RuntimeError(f"No terminal found. Run this yourself: {command}")
        subprocess.Popen([term, "-e", "bash", "-lc", f"{command}; exec bash"])

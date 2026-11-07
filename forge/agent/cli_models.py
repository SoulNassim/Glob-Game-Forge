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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

CLIS: dict[str, dict[str, Any]] = {
    "claude": {"label": "Claude Code", "bin": "claude", "install": "curl -fsSL https://claude.ai/install.sh | bash", "login": "claude auth login", "site": "https://claude.com/claude-code"},
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
TIMEOUT = 900  # seconds of silence before a CLI call is given up on
MAX_CALL_SECONDS = 2400  # the longest one call may run, however much it is still writing
_thinking_display = {"ok": True}  # switched off for this run if an older Claude Code refuses the flag
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
               "--include-partial-messages", "--system-prompt-file", str(folder / "system.md"), "--tools", "", "--strict-mcp-config"]
        # Without this the model's thinking streams as empty text, and the chat has nothing to show for minutes.
        cmd += ["--thinking-display", "summarized"] if _thinking_display["ok"] else []
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


# Set by the server around a chat message: called (from a worker thread) with {"phase", "chars", "thinking", "tail", "elapsed"}
# while the agent works, so the chat can show what is happening instead of one long wait.
progress_hook: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar("progress_hook", default=None)


_REPLY_SO_FAR = re.compile(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)')
_ACTION_SO_FAR = re.compile(r'"tool"\s*:\s*"(\w+)"\s*,\s*"(?:path|to)"\s*:\s*"((?:[^"\\]|\\.)+)"')


class _Progress:
    """Turns Claude Code's stream of partial messages into a small status the chat can show."""

    def __init__(self, hook: Callable[[dict[str, Any]], None] | None) -> None:
        self.hook, self.started, self.last = hook, time.time(), 0.0
        self.phase, self.chars, self.thinking, self.text = "starting", 0, 0, ""
        self.thought, self.head, self.say = "", "", ""
        self.doing: dict[str, str] | None = None

    def _read_reply(self, piece: str) -> None:
        """Pick out of the JSON being written what the model is saying and which file it is working on."""
        if len(self.head) < 2000:
            self.head += piece
            said = _REPLY_SO_FAR.search(self.head)
            if said:
                self.say = said.group(1).replace("\\n", " ").replace('\\"', '"')[:400]
        actions = _ACTION_SO_FAR.findall(self.text)
        if actions:
            self.doing = {"tool": actions[-1][0], "path": actions[-1][1].replace("\\\\", "\\")}

    def feed(self, line: str) -> None:
        if not self.hook or '"stream_event"' not in line:
            return
        try:
            event = (json.loads(line) or {}).get("event") or {}
        except ValueError:
            return
        kind = event.get("type")
        if kind == "content_block_start":
            self.phase = {"thinking": "thinking", "tool_use": "acting"}.get((event.get("content_block") or {}).get("type"), "writing")
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "thinking_delta":
                piece = delta.get("thinking") or ""
                self.phase, self.thinking = "thinking", self.thinking + len(piece)
                self.thought = (self.thought + piece)[-600:]
            elif delta.get("type") == "text_delta":
                piece = delta.get("text") or ""
                self.phase, self.chars = "writing", self.chars + len(piece)
                self.text = (self.text + piece)[-600:]
                self._read_reply(piece)
        else:
            return
        self.emit()

    def emit(self, force: bool = False) -> None:
        now = time.time()
        if not self.hook or (not force and now - self.last < 0.7):
            return
        self.last = now
        try:
            self.hook({"phase": self.phase, "chars": self.chars, "thinking": self.thinking, "tail": self.text[-280:], "thought": self.thought[-360:],
                       "say": self.say, "doing": self.doing, "elapsed": int(now - self.started)})
        except Exception:  # noqa: BLE001 — progress is a courtesy, never a reason to fail the call
            pass


def kill_tree(proc: subprocess.Popen[str]) -> None:
    """End a CLI and what it started: on Windows `claude` can be a .cmd, and killing that leaves the real program running."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc.kill()


def _run_streaming(cmd: list[str], stdin_text: str, cwd: str, env: dict[str, str], timeout: int, on_line: Callable[[str], None],
                   max_total: float = MAX_CALL_SECONDS, on_start: Callable[[subprocess.Popen[str]], None] | None = None) -> subprocess.CompletedProcess[str]:
    """subprocess.run, but each line of output is handed to `on_line` as it arrives.

    `timeout` counts silence, so a model that is still streaming isn't killed mid-answer; `max_total` caps the whole call.
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env, text=True,
                            encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if on_start:
        on_start(proc)
    out: list[str] = []
    err: list[str] = []
    expired = threading.Event()
    finished = threading.Event()
    started = time.monotonic()
    last_output = [started]

    def watch() -> None:
        while not finished.wait(min(1.0, timeout)):
            now = time.monotonic()
            if now - last_output[0] > timeout or now - started > max_total:
                expired.set()
                kill_tree(proc)
                return

    threading.Thread(target=watch, daemon=True).start()
    reader = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    reader.start()
    try:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except OSError:
            pass
        for line in proc.stdout:
            last_output[0] = time.monotonic()
            out.append(line)
            on_line(line)
        proc.wait()
        reader.join(timeout=2)
    finally:
        finished.set()
    if expired.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    return subprocess.CompletedProcess(cmd, proc.returncode, "".join(out), "".join(err))


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
        progress = _Progress(progress_hook.get())
        progress.emit(force=True)
        try:
            proc = await asyncio.to_thread(
                _run_streaming, cmd, stdin_text, tmp, {**child_env(), "NO_COLOR": "1"}, TIMEOUT, progress.feed,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{label} stopped: it went {TIMEOUT // 60} minutes without output, or passed the {MAX_CALL_SECONDS // 60}-minute limit for one answer.") from exc
        except OSError as exc:
            raise RuntimeError(f"Could not start {label}: {exc}") from exc
        if cli_id == "claude" and proc.returncode and _thinking_display["ok"] and "thinking-display" in (proc.stderr or ""):
            # An older Claude Code without the flag: it refused at once, so nothing was spent. Go on without it.
            _thinking_display["ok"] = False
            cmd, stdin_text = build_command(cli_id, exe, model, effort, system_prompt, prompt, images, folder)
            proc = await asyncio.to_thread(_run_streaming, cmd, stdin_text, tmp, {**child_env(), "NO_COLOR": "1"}, TIMEOUT, progress.feed)
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


# Installers that need Node's npm. Claude Code has its own installer, so it never does.
_NATIVE_INSTALL = {
    "claude": {"win32": 'powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"',
               "posix": "curl -fsSL https://claude.ai/install.sh | bash"},
}
LOGOUT_ARGS = {"claude": ["auth", "logout"], "codex": ["logout"], "opencode": ["auth", "logout"]}
# Gemini CLI has no logout command: its sign-in is these files.
GEMINI_LOGIN_FILES = (".gemini/oauth_creds.json", ".gemini/google_accounts.json")


def install_script(cli_id: str, windows: bool) -> str:
    """The shell lines that install a CLI. npm-based ones install Node.js first when `npm` is missing."""
    native = _NATIVE_INSTALL.get(cli_id)
    if native:
        return native["win32" if windows else "posix"]
    install = CLIS[cli_id]["install"]
    if windows:
        return "\r\n".join([
            "where npm >nul 2>nul || (",
            "  echo Node.js is missing. Installing it first...",
            "  winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements",
            r'  set "PATH=%ProgramFiles%\nodejs;%APPDATA%\npm;%PATH%"',
            ")",
            "where npm >nul 2>nul || (echo Could not install Node.js. Install it from https://nodejs.org, then press Install again. & exit /b 1)",
            "call " + install,
        ])
    return "\n".join([
        "if ! command -v npm >/dev/null 2>&1; then",
        "  if command -v brew >/dev/null 2>&1; then brew install node;",
        '  else echo "Node.js (npm) is missing. Install it from https://nodejs.org, then press Install again."; exit 1; fi',
        "fi",
        install,
    ])


def open_login(cli_id: str) -> None:
    """Open a real terminal window running the CLI's login (or install), so the user signs in their usual way."""
    installed = bool(find(cli_id))
    if sys.platform == "win32":
        # A script file, not an inline command: the installers use pipes and quotes cmd.exe would mangle.
        script = CLIS[cli_id]["login"] if installed else install_script(cli_id, True)
        path = Path(tempfile.gettempdir()) / f"glob-{cli_id}-{'login' if installed else 'install'}.cmd"
        path.write_text("@echo off\r\n" + script + "\r\necho.\r\necho Finished. You can close this window and go back to Glob.\r\n", encoding="utf-8")
        subprocess.Popen(["cmd", "/c", "start", "", "cmd", "/k", str(path)], env=child_env())
        return
    command = CLIS[cli_id]["login"] if installed else install_script(cli_id, False)
    if sys.platform == "darwin":
        path = Path(tempfile.gettempdir()) / f"glob-{cli_id}.command"
        path.write_text("#!/bin/bash\n" + command + "\n", encoding="utf-8")
        path.chmod(0o755)
        subprocess.Popen(["open", "-a", "Terminal", str(path)])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("konsole") or shutil.which("xterm")
        if not term:
            raise RuntimeError(f"No terminal found. Run this yourself: {command}")
        subprocess.Popen([term, "-e", "bash", "-lc", f"{command}; exec bash"], env=child_env())


def logout(cli_id: str) -> dict[str, Any]:
    """Sign the CLI out of its account, so the user can log in again (another account, or to clear a stale login)."""
    if cli_id not in CLIS:
        raise ValueError(f"Unknown coding agent '{cli_id}'.")
    exe = find(cli_id)
    if not exe:
        raise RuntimeError(f"{CLIS[cli_id]['label']} is not installed.")
    if cli_id == "gemini":
        for rel in GEMINI_LOGIN_FILES:
            (Path.home() / rel).unlink(missing_ok=True)
    else:
        try:
            proc = subprocess.run([exe, *LOGOUT_ARGS[cli_id]], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=30, env=child_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Could not log out of {CLIS[cli_id]['label']}: {exc}") from exc
        if proc.returncode != 0:
            raise RuntimeError(f"{CLIS[cli_id]['label']} could not log out: {(proc.stderr or proc.stdout).strip()[-300:]}")
    _login[cli_id] = "login"
    return {"id": cli_id, "status": "login"}

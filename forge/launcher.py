"""Start Glob for a coding agent: the agent says "open Glob" and the studio opens on its project.

The MCP server a coding agent runs (`forge --mcp`) uses this when Glob isn't running yet. It starts the local
studio in the background on the same projects folder, waits until it answers, and opens Glob's app window on
the agent's project. The server keeps running after the agent stops (its tray icon quits it). The Glob shortcut
and the installed app use the same path.

`FORGE_NO_AUTOSTART=1` turns the automatic start off; `open_glob` still works when asked for.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from forge.appwindow import open_window
from forge.config import get_config_dir, get_projects_root, load_config

START_TIMEOUT = 30.0
# Set on a studio started this way: it has no window or terminal, so Settings offers "Stop Glob".
BACKGROUND_ENV = "FORGE_BACKGROUND"


def app_url() -> str:
    return (os.environ.get("FORGE_APP_URL") or f"http://127.0.0.1:{load_config().port or 8420}").rstrip("/")


def autostart_enabled() -> bool:
    return os.environ.get("FORGE_NO_AUTOSTART", "").strip().lower() not in ("1", "true", "yes")


def _health(base: str) -> dict[str, Any] | None:
    import json

    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=1.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def _same_projects(health: dict[str, Any]) -> bool:
    return Path(str(health.get("projects_dir") or "")).resolve() == get_projects_root().resolve()


def is_running(base: str | None = None) -> bool:
    """Glob answers at `base` and serves the same projects folder as this process."""
    health = _health(base or app_url())
    return bool(health) and _same_projects(health)


def project_url(project: str, base: str | None = None) -> str:
    return f"{base or app_url()}/?{urllib.parse.urlencode({'project': project})}" if project else f"{base or app_url()}/"


def _spawn(port: int, project: str) -> None:
    log_dir = get_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "glob-server.log", "ab")  # noqa: SIM115 — handed to the child, which outlives us
    env = os.environ | {"FORGE_PROJECTS_DIR": str(get_projects_root()), "FORGE_OPEN_PROJECT": project, BACKGROUND_ENV: "1"}
    env.pop("FORGE_PROJECT", None)  # the studio serves every project
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    # The installed app (a frozen build) is its own CLI: any argument makes it run the server, not the launcher.
    server = ["--no-browser", "--port", str(port)]
    command = [sys.executable, *server] if getattr(sys, "frozen", False) else [sys.executable, "-m", "forge.cli", *server]
    if sys.platform == "win32":
        # A coding agent's terminal (or any tool runner) may run us inside a job object that kills every process
        # in it when the command ends. Break away from it so Glob keeps running; not every job allows that.
        try:
            subprocess.Popen(command, **(kwargs | {"creationflags": kwargs["creationflags"] | subprocess.CREATE_BREAKAWAY_FROM_JOB}))
            return
        except OSError:
            pass
    subprocess.Popen(command, **kwargs)


def ensure_running(project: str = "", timeout: float = START_TIMEOUT) -> bool:
    """Start the studio if it isn't up. Returns True when this call started it."""
    base = app_url()
    health = _health(base)
    if health and _same_projects(health):
        return False
    if health:
        raise RuntimeError(f"Another Glob is running at {base} with a different projects folder ({health.get('projects_dir')}). Close it, or open that one.")
    port = urllib.parse.urlsplit(base).port or 8420
    _spawn(port, project)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running(base):
            return True
        time.sleep(0.25)
    raise RuntimeError(f"Glob did not start at {base}. Run `glob` yourself; the log is in {get_config_dir() / 'logs' / 'glob-server.log'}.")


LOADING_PAGE = Path(__file__).resolve().parent / "ui" / "loading.html"


def open_glob(project: str, *, browser: bool = True, start: bool = True) -> dict[str, Any]:
    """Make sure the studio runs and show it on `project`.

    When Glob has to start first, its window opens at once on a small "Starting Glob" page that switches to the
    studio as soon as it answers, instead of the user staring at nothing for a few seconds."""
    url = project_url(project)
    opened = False
    if start and browser and LOADING_PAGE.is_file() and not _health(app_url()):
        opened = open_window(f"{LOADING_PAGE.as_uri()}#{urllib.parse.quote(url, safe='')}")
    started = ensure_running(project) if start else False
    if browser and not opened:
        opened = open_window(url)
    return {"url": url, "started": started, "opened": opened, "project": project}

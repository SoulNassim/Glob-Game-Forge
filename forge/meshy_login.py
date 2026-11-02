"""Meshy without an API key: the user's Meshy CLI login.

Meshy's sign-in page only accepts apps it has approved, and the Meshy CLI is one of them. So Glob signs in
through the CLI's browser login (the device flow, which needs no terminal), and then uses the login the CLI
keeps in ~/.config/meshy/credentials.json as the Meshy key for every Meshy model. The CLI refreshes it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from forge.agent.cli_models import _search_path

PACKAGE = "meshy-cli@0.4.0"
REFRESH_MARGIN_MS = 120_000

_lock = threading.Lock()
_login: dict[str, Any] = {}  # the running login: {"proc": Popen, "started": time}


class MeshyLoginError(RuntimeError):
    pass


def _credentials_path() -> Path:
    if os.environ.get("MESHY_CREDENTIALS_PATH", "").strip():
        return Path(os.environ["MESHY_CREDENTIALS_PATH"].strip())
    root = os.environ.get("MESHY_CONFIG_DIR", "").strip()
    return (Path(root) if root else Path.home() / ".config" / "meshy") / "credentials.json"


def _profile() -> dict[str, Any]:
    try:
        state = json.loads(_credentials_path().read_text(encoding="utf-8"))
        return state["profiles"][state.get("active_profile", "default")] or {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _command() -> list[str]:
    path = _search_path()
    if exe := shutil.which("meshy", path=path):
        return [exe]
    if npx := shutil.which("npx", path=path):
        return [npx, "--yes", f"--package={PACKAGE}", "--", "meshy"]
    raise MeshyLoginError("Meshy sign-in needs Node.js (for the Meshy CLI). Install it from nodejs.org, or paste a Meshy API key instead.")


def _run(args: list[str], timeout: float = 120) -> dict[str, Any]:
    proc = subprocess.run(_command() + args + ["--format", "json", "--no-update-check"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env={**os.environ, "PATH": _search_path()},
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = proc.stdout.strip()
    # The CLI pretty-prints one JSON value, or writes one JSON event per line.
    for text in (out, *reversed(out.splitlines())):
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise MeshyLoginError((proc.stderr or proc.stdout).strip()[-400:] or "The Meshy CLI gave no answer.")


def signed_in() -> bool:
    profile = _profile()
    return profile.get("kind") != "api_key" and bool(profile.get("access_token"))


def logging_in() -> bool:
    proc = _login.get("proc")
    return bool(proc and proc.poll() is None)


def token() -> str:
    """The Meshy CLI's login, refreshed when it is about to expire; "" when the user is not signed in."""
    profile = _profile()
    if profile.get("kind") == "api_key" or not profile.get("access_token"):
        return ""
    if float(profile.get("expires_at") or 0) - time.time() * 1000 < REFRESH_MARGIN_MS:
        with _lock:
            try:
                _run(["auth", "status", "--offline"], timeout=60)  # the CLI refreshes the login as a side effect
            except (MeshyLoginError, OSError, subprocess.SubprocessError):
                pass
            profile = _profile()
    return str(profile.get("access_token") or "")


def start() -> str:
    """Start the CLI's browser login and return the page the user approves it on. It finishes in the background."""
    with _lock:
        if logging_in():
            _login["proc"].kill()
        flow = _run(["auth", "login", "--device", "--no-wait"])
        url, flow_id = flow.get("verification_url") or flow.get("verification_uri_complete"), flow.get("flow_id")
        if not url or not flow_id:
            raise MeshyLoginError(f"The Meshy CLI did not start a login: {json.dumps(flow)[:300]}")
        _login["proc"] = subprocess.Popen(_command() + ["auth", "login", "--device-flow", str(flow_id), "--format", "json", "--no-update-check"],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          env={**os.environ, "PATH": _search_path()}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _login["started"] = time.time()
        return str(url)


def sign_out() -> None:
    if signed_in():
        _run(["auth", "logout"], timeout=60)

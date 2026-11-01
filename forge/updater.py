"""Keeping Glob up to date from its GitHub releases.

`check()` asks GitHub for the latest release (cached for a few hours) and compares it with the
running version. How an update is installed depends on how Glob was installed:

  * windows-app: the Inno Setup installer is downloaded and run silently; it replaces the app
    and starts it again, so Glob quits right after launching it.
  * mac-app:     the .dmg is downloaded, Glob.app is swapped in place by a small script that runs
    after Glob quits, and the new version is opened.
  * uv-tool:     `uv tool install --force` with the release's source archive; restart afterwards.
  * source:      a git checkout or development install; Glob only shows the command to run.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from forge import __version__

REPO = "SoulNassim/Glob-Game-Forge"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CACHE_SECONDS = 6 * 3600
ASSETS = {"windows-app": "Glob-Setup-Windows.exe", "mac-app": "Glob-macOS.dmg"}

_cache: dict[str, Any] = {"at": 0.0, "release": None}
state: dict[str, Any] = {"status": "idle", "progress": 0.0, "error": "", "message": ""}


def parse_version(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(text).split("-")[0].split("+")[0])
    return tuple(int(n) for n in numbers[:3]) or (0,)


def install_kind() -> str:
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            return "windows-app"
        if sys.platform == "darwin":
            return "mac-app"
        return "source"
    prefix = Path(sys.prefix).resolve()
    if prefix.parent.name == "tools" and "uv" in {p.lower() for p in prefix.parts}:
        return "uv-tool"
    return "source"


def _mac_bundle() -> Path | None:
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


async def latest_release(force: bool = False) -> dict[str, Any] | None:
    if not force and _cache["release"] is not None and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["release"]
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        response = await client.get(LATEST_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": f"Glob/{__version__}"})
    if response.status_code == 404:
        release = {}
    else:
        response.raise_for_status()
        data = response.json()
        release = {
            "version": str(data.get("tag_name") or "").lstrip("v"),
            "tag": data.get("tag_name") or "",
            "name": data.get("name") or "",
            "notes": str(data.get("body") or "")[:4000],
            "url": data.get("html_url") or f"https://github.com/{REPO}/releases",
            "published": data.get("published_at") or "",
            "assets": {a["name"]: a["browser_download_url"] for a in data.get("assets") or [] if a.get("name")},
        }
    _cache.update(at=time.time(), release=release)
    return release


async def check(force: bool = False) -> dict[str, Any]:
    kind = install_kind()
    info: dict[str, Any] = {"current": __version__, "kind": kind, "available": False, "state": dict(state)}
    try:
        release = await latest_release(force)
    except (httpx.HTTPError, ValueError) as exc:
        return {**info, "error": f"Couldn't reach GitHub: {exc}"}
    if not release or not release.get("version"):
        return info
    newer = parse_version(release["version"]) > parse_version(__version__)
    info.update(latest=release["version"], notes=release["notes"], url=release["url"], published=release["published"], available=newer)
    asset = ASSETS.get(kind)
    info["can_install"] = newer and (kind == "uv-tool" and shutil.which("uv") is not None or bool(asset and asset in release["assets"]))
    if kind == "source":
        info["command"] = "git pull && uv sync" if (Path.cwd() / ".git").exists() else f"uv tool install --force \"forge-assets[desktop] @ https://github.com/{REPO}/archive/refs/tags/{release['tag']}.zip\""
    return info


async def _download(url: str, dest: Path) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": f"Glob/{__version__}"}) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as sink:
                async for chunk in response.aiter_bytes(1 << 20):
                    sink.write(chunk)
                    done += len(chunk)
                    state["progress"] = done / total if total else 0.0


def _quit_soon(delay: float = 1.5) -> None:
    """Exit the whole app (server and window) shortly, after the HTTP response went out."""
    loop = asyncio.get_running_loop()
    loop.call_later(delay, lambda: os._exit(0))


async def install() -> dict[str, Any]:
    """Download and install the latest release. Runs in the background; poll `state` for progress."""
    if state["status"] in ("downloading", "installing"):
        return dict(state)
    kind = install_kind()
    release = await latest_release(force=True)
    if not release or parse_version(release.get("version", "0")) <= parse_version(__version__):
        state.update(status="idle", message="Glob is up to date.")
        return dict(state)
    state.update(status="downloading", progress=0.0, error="", message=f"Downloading Glob {release['version']}…")
    try:
        if kind == "uv-tool":
            state.update(status="installing", message=f"Installing Glob {release['version']}…")
            source = f"forge-assets[desktop] @ https://github.com/{REPO}/archive/refs/tags/{release['tag']}.zip"
            proc = await asyncio.create_subprocess_exec(
                shutil.which("uv") or "uv", "tool", "install", "--force", "--python", "3.12", source,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await proc.communicate()
            if proc.returncode:
                raise RuntimeError(output.decode(errors="ignore")[-600:])
            state.update(status="restart", message=f"Glob {release['version']} is installed. Restart Glob to use it.")
            return dict(state)
        asset = ASSETS.get(kind)
        if not asset or asset not in release["assets"]:
            raise RuntimeError("This install can't update itself. Download the new version from GitHub.")
        folder = Path(tempfile.mkdtemp(prefix="glob-update-"))
        file = folder / asset
        await _download(release["assets"][asset], file)
        state.update(status="installing", progress=1.0, message=f"Installing Glob {release['version']}; Glob restarts by itself.")
        if kind == "windows-app":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([str(file), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"], creationflags=flags, close_fds=True)
        else:
            bundle = _mac_bundle()
            if bundle is None:
                raise RuntimeError("Couldn't find Glob.app to replace.")
            script = folder / "update.sh"
            script.write_text(
                "#!/bin/sh\n"
                f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.5; done\n"
                f"mount=$(hdiutil attach -nobrowse -noautoopen '{file}' | tail -1 | cut -f3-)\n"
                f"rm -rf '{bundle}.old' && mv '{bundle}' '{bundle}.old' && ditto \"$mount/Glob.app\" '{bundle}' && rm -rf '{bundle}.old' "
                f"|| {{ rm -rf '{bundle}'; mv '{bundle}.old' '{bundle}'; }}\n"
                "hdiutil detach \"$mount\" -quiet\n"
                f"xattr -dr com.apple.quarantine '{bundle}' 2>/dev/null\n"
                f"open '{bundle}'\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            subprocess.Popen(["/bin/sh", str(script)], start_new_session=True, close_fds=True)
        _quit_soon()
    except Exception as exc:  # noqa: BLE001 — shown in the update banner
        state.update(status="failed", error=str(exc)[:600], message="The update failed.")
    return dict(state)

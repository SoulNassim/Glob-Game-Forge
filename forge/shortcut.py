"""A "Glob" shortcut on the desktop and in the Start menu / app list, so the user can open Glob without an agent.

The shortcut runs `glob open`: it starts Glob in the background when it isn't running and opens it in the browser.
`glob shortcut` (and the installers, and the first `glob connect`) make it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ICON_DIR = Path(__file__).with_name("ui")


def glob_command() -> Path | None:
    """The installed `glob` executable next to this Python (a uv tool or a venv), else the one on PATH."""
    scripts = Path(sys.executable).parent
    for name in ("glob.exe", "glob"):
        if (scripts / name).is_file():
            return scripts / name
    found = shutil.which("glob")
    return Path(found) if found else None


def _desktop() -> Path:
    if sys.platform == "win32":
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                                 capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if out.stdout.strip():
                return Path(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    return Path.home() / "Desktop"


def locations() -> list[Path]:
    """Where the shortcut goes on this system."""
    if sys.platform == "win32":
        start_menu = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Microsoft/Windows/Start Menu/Programs"
        return [_desktop() / "Glob.lnk", start_menu / "Glob.lnk"]
    if sys.platform == "darwin":
        return [_desktop() / "Glob.command"]
    return [Path.home() / ".local/share/applications/glob.desktop"]


def exists() -> bool:
    return any(path.exists() for path in locations())


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def create() -> list[Path]:
    """Make (or refresh) the shortcuts; returns the paths written."""
    exe = glob_command()
    if exe is None:
        raise RuntimeError("Couldn't find the installed `glob` command.")
    made: list[Path] = []
    for path in locations():
        path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            # WindowStyle 7: the console `glob open` needs for a second starts minimised instead of flashing.
            script = (
                f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(path))}); "
                f"$s.TargetPath = {_ps_quote(str(exe))}; $s.Arguments = 'open'; "
                f"$s.WorkingDirectory = {_ps_quote(str(Path.home()))}; $s.WindowStyle = 7; "
                f"$s.IconLocation = {_ps_quote(str(ICON_DIR / 'glob.ico'))}; "
                "$s.Description = 'Open Glob, the asset studio for your game'; $s.Save()"
            )
            result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                                    timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0 or not path.exists():
                raise RuntimeError(f"Couldn't create {path}: {(result.stderr or result.stdout).strip()[:200]}")
        elif sys.platform == "darwin":
            path.write_text(f'#!/bin/sh\n"{exe}" open\nosascript -e \'tell application "Terminal" to close front window\' >/dev/null 2>&1 &\n', encoding="utf-8")
            path.chmod(0o755)
        else:
            path.write_text(
                "[Desktop Entry]\nType=Application\nName=Glob\nComment=The asset studio for your game\n"
                f'Exec="{exe}" open\nIcon={ICON_DIR / "glob-icon.png"}\nTerminal=false\nCategories=Graphics;Development;\n',
                encoding="utf-8",
            )
            path.chmod(0o755)
        made.append(path)
    return made

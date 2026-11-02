"""Show Glob in its own app window: no tabs, no address bar, Glob's icon in the taskbar.

Uses the Chromium the computer already has in app mode (`--app=<url>`): Edge ships with Windows, Chrome or
Chromium elsewhere. Without one, Glob opens in the default browser.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

WINDOW_SIZE = "1480,940"


def _candidates() -> list[str]:
    if sys.platform == "win32":
        roots = [os.environ.get(k, "") for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        paths = [str(Path(r) / sub) for r in roots if r for sub in (
            "Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe")]
        return paths + [p for p in (shutil.which("msedge"), shutil.which("chrome")) if p]
    if sys.platform == "darwin":
        return [f"/Applications/{app}.app/Contents/MacOS/{app}" for app in ("Google Chrome", "Microsoft Edge", "Chromium")]
    return [p for p in (shutil.which(n) for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")) if p]


def find_browser() -> str | None:
    """A Chromium that can open app windows, or None."""
    return next((p for p in _candidates() if Path(p).is_file()), None)


def open_window(url: str) -> bool:
    """Open `url` as an app window, else in the default browser. True when something opened."""
    exe = find_browser()
    if exe:
        try:
            kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([exe, f"--app={url}", f"--window-size={WINDOW_SIZE}"], **kwargs)
            return True
        except OSError:
            pass
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001 — no browser (CI, SSH): the caller still has the URL
        return False

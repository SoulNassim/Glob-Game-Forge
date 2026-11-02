"""Glob's icon in the system tray while it runs in the background: Open Glob, Quit Glob.

Only for a studio started in the background (no window or terminal to close it with). Windows only for now:
pystray needs the main thread on macOS, and the main thread runs the server.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

ICON = Path(__file__).resolve().parent / "ui" / "glob-icon.png"


def start(url: str) -> bool:
    """Show the tray icon in a background thread. False when there is no tray here."""
    if sys.platform != "win32":
        return False
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return False

    from forge.appwindow import open_window

    def _open(icon, item) -> None:  # noqa: ARG001 — pystray's callback signature
        open_window(url)

    def _quit(icon, item) -> None:  # noqa: ARG001
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(pystray.MenuItem("Open Glob", _open, default=True), pystray.MenuItem("Quit Glob", _quit))
    icon = pystray.Icon("glob", Image.open(ICON), "Glob is running", menu)
    threading.Thread(target=icon.run, daemon=True, name="glob-tray").start()
    return True

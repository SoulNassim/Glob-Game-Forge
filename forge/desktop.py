"""Glob as a native desktop window (`uv run --extra desktop forge --desktop` or `glob-desktop`).

Starts the studio server on a free loopback port in a background thread and shows it in a
native webview window (WebView2 on Windows, WebKit on macOS, GTK/Qt on Linux). Closing the
window stops the server.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

# WinForms (Windows) only accepts .ico; GTK/Qt take the PNG.
ICON = Path(__file__).resolve().parent / "ui" / ("glob.ico" if sys.platform == "win32" else "glob-icon.png")


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free port on 127.0.0.1")


def _wait_until_up(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1):
                return
        except Exception:
            time.sleep(0.15)
    raise RuntimeError(f"Glob server did not start at {url}")


def main(port: int | None = None) -> None:
    try:
        import webview
    except ImportError as exc:
        raise SystemExit("The desktop app needs pywebview: uv run --extra desktop forge --desktop") from exc
    import uvicorn

    port = _free_port(port or 8420)
    os.environ["FORGE_BIND_HOST"] = "127.0.0.1"
    # Installed app (not a repo checkout with its own projects/): keep projects in the home folder.
    if not os.environ.get("FORGE_PROJECTS_DIR") and not (Path.cwd() / "projects").is_dir():
        os.environ["FORGE_PROJECTS_DIR"] = str(Path.home() / "Glob" / "projects")
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config("forge.server.app:app", host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_until_up(url)

    webview.create_window("Glob · Game Asset Forge", url, width=1480, height=940, min_size=(1100, 720), background_color="#0b0710")
    try:
        webview.start(icon=str(ICON) if ICON.exists() else None)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()

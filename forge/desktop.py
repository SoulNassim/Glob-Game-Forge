"""Open Glob as an app: what the Glob shortcut, the Start menu entry and the installed Glob.exe run.

Starts the studio in the background if it isn't running (it stays running, with a tray icon to quit it) and
opens Glob's app window on the projects home. A second click opens another window at once: the server is
already up.
"""

from __future__ import annotations

import os
from pathlib import Path


def main(port: int | None = None) -> None:  # noqa: ARG001 — kept for `glob --desktop`; the port comes from config
    from forge.config import configured_projects_dir

    # The app keeps projects in the home folder (unless one was chosen in Settings), wherever it was started from:
    # the shortcut's folder must not decide which projects show.
    if not os.environ.get("FORGE_PROJECTS_DIR") and not configured_projects_dir():
        os.environ["FORGE_PROJECTS_DIR"] = str(Path.home() / "Glob" / "projects")
    from forge import launcher

    try:
        launcher.open_glob(os.environ.pop("FORGE_OPEN_PROJECT", ""))
    except RuntimeError as exc:
        _tell(str(exc))


def _tell(message: str) -> None:
    """Show an error without a console (the app has none)."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Glob", 0x10)
    except Exception:  # noqa: BLE001 — not Windows: stderr is all there is
        print(message)


if __name__ == "__main__":
    main()

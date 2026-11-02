"""Notice workspace edits made outside Glob (a coding agent, an editor, Finder) so the overview refreshes."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

# Projects stay watched this long after the UI last loaded their overview.
WATCH_SECONDS = 15 * 60
SKIP_DIRS = {"inbox", "node_modules"}


def signature(ws_dir: Path) -> tuple[int, int]:
    """(file count, newest mtime in ns) of the visible workspace; cheap enough to poll every couple of seconds."""
    count = newest = 0
    for root, dirs, files in os.walk(ws_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
        for name in files:
            if name.startswith("."):
                continue
            try:
                stat = os.stat(os.path.join(root, name))
            except OSError:
                continue
            count += 1
            newest = max(newest, stat.st_mtime_ns)
    try:
        newest = max(newest, ws_dir.stat().st_mtime_ns)
    except OSError:
        pass
    return count, newest


class WorkspaceWatcher:
    def __init__(self, ws_dir_fn: Callable[[str], Path], notify: Callable[[str], Awaitable[Any]]) -> None:
        self.ws_dir_fn = ws_dir_fn
        self.notify = notify
        self.watched: dict[str, float] = {}
        self.signatures: dict[str, tuple[int, int]] = {}
        self._running = False

    def touch(self, project: str) -> None:
        self.watched[project] = time.time()

    async def tick(self) -> None:
        now = time.time()
        for project, seen in list(self.watched.items()):
            if now - seen > WATCH_SECONDS:
                self.watched.pop(project, None)
                self.signatures.pop(project, None)
                continue
            try:
                current = await asyncio.to_thread(signature, self.ws_dir_fn(project))
            except OSError:
                continue
            previous = self.signatures.get(project)
            self.signatures[project] = current
            if previous is not None and previous != current:
                await self.notify(project)

    async def loop(self, interval: float = 2.0) -> None:
        self._running = True
        while self._running:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — never let the watcher kill the server
                pass
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False

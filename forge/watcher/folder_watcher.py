"""Watch Folder service (Section 4).

Monitors `projects/<name>/inbox_watch/` (or any user-configured watch folder).
When an image is dropped in, ingests it into the Asset Store and automatically runs
the project's `watch_default_recipe` (e.g. `make_3d` or `variant`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Awaitable

from forge.storage.project_store import ProjectStore


class WatchFolderService:
    def __init__(
        self,
        get_store_fn: Callable[[str], ProjectStore],
        run_verb_fn: Callable[[str, str, list[str], dict[str, Any], str], Awaitable[dict[str, Any]]],
    ) -> None:
        self.get_store_fn = get_store_fn
        self.run_verb_fn = run_verb_fn
        self._seen_files: set[str] = set()
        self._running = False

    async def scan_once(self, project_name: str = "default") -> list[dict[str, Any]]:
        store = self.get_store_fn(project_name)
        bible = store.get_bible()
        processed: list[dict[str, Any]] = []

        if not store.watch_dir.exists():
            return processed

        for f in sorted(store.watch_dir.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".glb"):
                continue
            sig = f"{project_name}:{f.name}:{f.stat().st_mtime_ns}"
            if sig in self._seen_files:
                continue
            self._seen_files.add(sig)

            content = f.read_bytes()
            asset = store.ingest_uploaded_file(
                filename=f.name,
                content=content,
                name=f"Watched: {f.stem}",
                tags=["watch-folder"],
            )
            # Move processed file into inbox_watch/processed/ so it isn't re-triggered on restart
            proc_dir = store.watch_dir / "processed"
            proc_dir.mkdir(parents=True, exist_ok=True)
            f.replace(proc_dir / f.name)

            recipe_id = bible.watch_default_recipe or "make_3d"
            res = await self.run_verb_fn(
                project_name,
                recipe_id,
                [asset.id],
                {},
                "watch_folder",
            )
            processed.append({"ingested_asset": asset.model_dump(), "recipe_run": res})

        return processed

    async def loop(self, poll_seconds: float = 3.0) -> None:
        self._running = True
        while self._running:
            try:
                store = self.get_store_fn("default")
                if store.get_bible().watch_folder_enabled:
                    await self.scan_once("default")
            except Exception:
                pass
            await asyncio.sleep(poll_seconds)

    def stop(self) -> None:
        self._running = False

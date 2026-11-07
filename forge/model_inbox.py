"""Bring finished 3D models from a folder (usually Downloads) into a project as assets.

For models made by hand on a website, such as Tripo Studio, whose credits can't be used by Glob. It only ever
copies: the source file is left alone, nothing older than the moment it was switched on is touched, and no
paid service is called. A file is taken once its size has held still between two looks, so a download still
being written is left for later. Each file's hash is kept in the workspace, so a re-download is not added twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from forge.blueprint_contract import serialize_frontmatter
from forge.config import load_config
from forge.game_import import clean_title
from forge.overview import slugify

MODEL_EXT = {".glb", ".fbx", ".obj"}
MAX_BYTES = 500 * 1024 * 1024
FOLDER = "studio-downloads"
SEEN_FILE = ".model-inbox.json"
RECENT = 10


def default_folder() -> Path:
    return Path.home() / "Downloads"


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seen(ws_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((ws_dir / SEEN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _title(path: Path) -> str:
    return clean_title(re.sub(r"\s*\(\d+\)$", "", path.stem))  # "rogue (1).glb" is the browser's second copy of rogue


def _check(path: Path) -> None:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"{path.name} is over {MAX_BYTES // (1024 * 1024)} MB.")
    if path.suffix.lower() == ".glb":
        with path.open("rb") as handle:
            if handle.read(4) != b"glTF":
                raise ValueError(f"{path.name} isn't a complete GLB file.")


def import_file(ws_dir: Path, path: Path) -> str | None:
    """Copy one model into its own new asset. Returns the asset's path, or None when this file is already in."""
    _check(path)
    file_hash = _hash(path)
    seen = _seen(ws_dir)
    if file_hash in seen:
        return None
    folder = ws_dir / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    readme = folder / "README.md"
    if not readme.exists():
        readme.write_text("# Studio downloads\n\n3D models Glob picked up from your watched folder.\n", encoding="utf-8")
    title = _title(path)
    slug = slugify(title) or "model"
    name, number = slug, 2
    while (folder / f"{name}.md").exists() or (folder / name).exists():
        name, number = f"{slug}-{number}", number + 1
    model = folder / name / "3d" / f"{name}{path.suffix.lower()}"
    model.parent.mkdir(parents=True)
    shutil.copy2(path, model)
    page = folder / f"{name}.md"
    page.write_text(serialize_frontmatter({"kind": "prop", "imported": True}, f"# {title}\n\nDownloaded as `{path.name}`.\n"), encoding="utf-8")
    rel = page.relative_to(ws_dir).as_posix()
    seen[file_hash] = rel
    (ws_dir / SEEN_FILE).write_text(json.dumps(seen, indent=1), encoding="utf-8")
    return rel


class ModelInbox:
    """Polls the folder chosen in Settings. Turned off, it does nothing but read the config."""

    def __init__(self, ws_dir_fn: Callable[[str], Path], notify: Callable[[str, list[str]], Awaitable[Any]]) -> None:
        self.ws_dir_fn = ws_dir_fn
        self.notify = notify
        self.pending: dict[str, tuple[int, int]] = {}  # file -> (size, mtime) at the last look
        self.done: dict[str, tuple[int, int]] = {}  # files already handled, so a bad one reports only once
        self.recent: list[str] = []
        self.error = ""
        self._running = False

    def reset(self) -> None:
        self.pending.clear()
        self.done.clear()
        self.recent.clear()
        self.error = ""

    def scan(self, ws_dir: Path, folder: Path, since: float) -> list[str]:
        imported: list[str] = []
        current: dict[str, tuple[int, int]] = {}
        for path in sorted(folder.iterdir()):
            try:
                if path.name.startswith(".") or path.suffix.lower() not in MODEL_EXT or not path.is_file():
                    continue
                stat = path.stat()
                if stat.st_mtime < since or stat.st_size == 0:
                    continue
                signature = (stat.st_size, stat.st_mtime_ns)
                key = str(path)
                if self.done.get(key) == signature:
                    continue
                current[key] = signature
                if self.pending.get(key) != signature:
                    continue  # new or still growing: look again next time
                self.done[key] = signature
                rel = import_file(ws_dir, path)
                if rel:
                    imported.append(rel)
                    self.error = ""
            except (OSError, ValueError) as exc:
                self.error = str(exc)
        self.pending = current
        return imported

    async def tick(self) -> None:
        cfg = load_config()
        if not cfg.model_inbox_dir or not cfg.model_inbox_project:
            return
        folder = Path(cfg.model_inbox_dir)
        if not folder.is_dir():
            self.error = f"Can't find the folder {folder}."
            return
        project = cfg.model_inbox_project
        imported = await asyncio.to_thread(self.scan, self.ws_dir_fn(project), folder, cfg.model_inbox_since)
        if imported:
            self.recent = (imported + self.recent)[:RECENT]
            await self.notify(project, imported)

    async def loop(self, interval: float = 3.0) -> None:
        self._running = True
        while self._running:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — never let the inbox kill the server
                self.error = str(exc)
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False

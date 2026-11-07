"""Bring finished 3D models from a folder (usually Downloads) into a project as assets.

For models made by hand on a website, such as Tripo Studio, whose credits can't be used by Glob. It only ever
copies: the source file is left alone, nothing older than the moment it was switched on is touched, and no
paid service is called. A file is taken once its size has held still between two looks, so a download still
being written is left for later. Each file's hash is kept in the workspace, so a re-download is not added twice.

Sites like Tripo Studio don't keep an uploaded image's name: their download is named after what the model looks
like. So a download whose name means the same as a waiting asset's goes straight onto that asset. Anything else
becomes its own asset, and `pairing()` / `pair()` let the user attach it by eye. `export_batches()` is the other
direction: the picked images of waiting assets, with readable names, in folders of the size the site's batch takes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from forge import asset_studio
from forge.blueprint_contract import serialize_frontmatter
from forge.config import load_config
from forge.game_import import clean_title
from forge.overview import move_to_trash, resolve_rel, slugify

MODEL_EXT = {".glb", ".fbx", ".obj"}
MAX_BYTES = 500 * 1024 * 1024
FOLDER = "studio-downloads"
SEEN_FILE = ".model-inbox.json"
RECENT = 10
BATCH_DIR = ".tripo-batch"
BATCH_SIZE = 30  # the most images Tripo Studio's batch takes at once
NO_MODEL_KINDS = {"sfx", "voice", "music", "material", "kit", "ui", "vfx"}


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


def _words(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", clean_title(name).lower()))


def waiting_assets(ws_dir: Path) -> list[dict[str, str]]:
    """Assets with a picked image and no 3D model yet, in the order the overview shows them."""
    found = []
    for rel in asset_studio.assets_under(ws_dir):
        try:
            path, meta, _ = asset_studio.read_asset(ws_dir, rel)
            winner = str(meta.get("winner") or "")
            if str(meta.get("kind")) in NO_MODEL_KINDS or meta.get("cover") or not winner or not resolve_rel(ws_dir, winner).is_file():
                continue
            if asset_studio.list_outputs(ws_dir, path)["3d"]:
                continue
        except (OSError, ValueError):
            continue
        found.append({"path": rel, "title": clean_title(path.stem), "winner": winner})
    return found


def export_batches(ws_dir: Path) -> dict[str, Any]:
    """Copy the picked images of waiting assets to .tripo-batch/batch-01/hooded-rogue.png and so on, replacing the last export."""
    root = ws_dir / BATCH_DIR
    if root.exists():
        shutil.rmtree(root)
    waiting = waiting_assets(ws_dir)
    used: set[str] = set()
    counts: dict[str, int] = {}
    for index, asset in enumerate(waiting):
        batch = f"batch-{index // BATCH_SIZE + 1:02d}"
        winner = resolve_rel(ws_dir, asset["winner"])
        slug = slugify(Path(asset["path"]).stem) or "asset"
        name, number = slug, 2
        while name in used:
            name, number = f"{slug}-{number}", number + 1
        used.add(name)
        (root / batch).mkdir(parents=True, exist_ok=True)
        shutil.copy2(winner, root / batch / f"{name}{winner.suffix.lower()}")
        counts[batch] = counts.get(batch, 0) + 1
    return {"folder": BATCH_DIR, "total": len(waiting), "batches": counts}


def _remember(ws_dir: Path, file_hash: str, rel: str) -> None:
    seen = _seen(ws_dir)
    seen[file_hash] = rel
    (ws_dir / SEEN_FILE).write_text(json.dumps(seen, indent=1), encoding="utf-8")


def _match(ws_dir: Path, title: str) -> str:
    """The one waiting asset whose name has exactly the same words. A near match could be the wrong model, so it is left to the user."""
    words = _words(title)
    same = [a["path"] for a in waiting_assets(ws_dir) if _words(Path(a["path"]).stem) == words]
    return same[0] if words and len(same) == 1 else ""


def import_file(ws_dir: Path, path: Path) -> str | None:
    """Put one model on the waiting asset of the same name, else in its own new asset. Returns the asset's path,
    or None when this file is already in."""
    _check(path)
    file_hash = _hash(path)
    if file_hash in _seen(ws_dir):
        return None
    title = _title(path)
    target = _match(ws_dir, title)
    if target:
        page = resolve_rel(ws_dir, target)
        model = asset_studio.output_dir(page) / "3d" / f"{page.stem}{path.suffix.lower()}"
        model.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, model)
        _remember(ws_dir, file_hash, target)
        return target
    folder = ws_dir / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    readme = folder / "README.md"
    if not readme.exists():
        readme.write_text("# Studio downloads\n\n3D models Glob picked up from your watched folder.\n", encoding="utf-8")
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
    _remember(ws_dir, file_hash, rel)
    return rel


def pairing(ws_dir: Path) -> dict[str, Any]:
    """Downloads that found no asset of their own, each with its likeliest waiting asset, and all the waiting assets."""
    waiting = waiting_assets(ws_dir)
    models = []
    for rel in asset_studio.assets_under(ws_dir, FOLDER) if (ws_dir / FOLDER).is_dir() else []:
        path, meta, _ = asset_studio.read_asset(ws_dir, rel)
        outputs = asset_studio.list_outputs(ws_dir, path)["3d"]
        if meta.get("studio_kept") or not meta.get("imported") or not outputs:
            continue
        words = _words(path.stem)
        scored = [(len(words & _words(Path(a["path"]).stem)) / (len(words | _words(Path(a["path"]).stem)) or 1), a["path"]) for a in waiting]
        best = max(scored, default=(0.0, ""), key=lambda item: item[0])
        models.append({"path": rel, "title": clean_title(path.stem), "model": outputs[0]["path"], "thumb": outputs[0].get("thumb", ""), "suggested": best[1] if best[0] else ""})
    return {"models": models, "waiting": waiting}


def pair(ws_dir: Path, source: str, target: str) -> str:
    """Move a download's model files onto a waiting asset and put the download's now-empty asset in the trash."""
    if not source.startswith(f"{FOLDER}/"):
        raise ValueError("That isn't a downloaded model.")
    if target not in {a["path"] for a in waiting_assets(ws_dir)}:
        raise ValueError("That asset isn't waiting for a model.")
    page, goal = resolve_rel(ws_dir, source), resolve_rel(ws_dir, target)
    if not page.is_file():
        raise FileNotFoundError(f"No model '{source}'.")
    old, destination = asset_studio.output_dir(page), asset_studio.output_dir(goal) / "3d"
    destination.mkdir(parents=True, exist_ok=True)
    for file in sorted((old / "3d").iterdir()):
        shutil.move(str(file), destination / file.name.replace(page.stem, goal.stem, 1))
    move_to_trash(ws_dir, old)
    move_to_trash(ws_dir, page)
    seen = _seen(ws_dir)
    (ws_dir / SEEN_FILE).write_text(json.dumps({key: target if value == source else value for key, value in seen.items()}, indent=1), encoding="utf-8")
    return target


def keep_separate(ws_dir: Path, source: str) -> None:
    """The user wants this download as its own asset: stop offering to pair it."""
    if not source.startswith(f"{FOLDER}/"):
        raise ValueError("That isn't a downloaded model.")
    asset_studio.update_asset(ws_dir, source, {"studio_kept": True})


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

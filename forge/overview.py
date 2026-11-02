"""Generic project overview built from whatever folder structure a workspace has.

Nothing here assumes particular folder names. The conventions are:
  * every top-level folder is a section; a numeric prefix (``01-intro``) sets its order,
  * a folder's ``README.md`` (or ``index.md``) is its intro page,
  * the root ``README.md`` or ``GAME.md`` is the project front page,
  * an image named ``cover.*`` is used as the hero image for its folder,
  * the first ``# Heading`` of a Markdown file is its title.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

TRASH_DIR = ".trash"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv"}
MODEL_SUFFIXES = {".glb", ".gltf", ".obj", ".fbx"}
FRONT_PAGES = ("README.md", "GAME.md", "index.md")
FOLDER_PAGES = ("README.md", "index.md")
# Forge's own bookkeeping, never shown as project content.
HIDDEN_NAMES = {"inbox"}
PILLAR_DIR = "pillars"
MAX_EDIT_BYTES = 2_000_000
_ORDER_RE = re.compile(r"^(\d+)[\s._-]+(.*)$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.S)


def inside(ws_dir: Path, target: Path) -> Path:
    root = ws_dir.resolve()
    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Path is outside the workspace.")
    return resolved


def resolve_rel(ws_dir: Path, rel: str) -> Path:
    clean = str(rel or "").replace("\\", "/").strip().lstrip("/")
    if clean.startswith("workspace/"):
        clean = clean[len("workspace/"):]
    if any(part == ".." for part in clean.split("/")):
        raise ValueError("Path is outside the workspace.")
    return inside(ws_dir, ws_dir / clean)


def move_to_trash(ws_dir: Path, target: Path) -> str | None:
    """Move a workspace file or folder into `.trash/`, returning its new workspace-relative path."""
    target = inside(ws_dir, target)
    if not target.exists():
        return None
    trash = ws_dir / TRASH_DIR
    trash.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = trash / f"{stamp}-{target.relative_to(ws_dir.resolve()).as_posix().replace('/', '__')}"
    target.rename(destination)
    return destination.relative_to(ws_dir).as_posix()


def _visible(path: Path) -> bool:
    if path.name.startswith((".", "_")) or path.name in HIDDEN_NAMES or path.is_symlink():
        return False
    # Forge's own state files (PIPELINE.json, STYLE_RULES.json, …) are all-caps JSON.
    return not (path.suffix.lower() == ".json" and path.stem.replace("_", "").isupper())


def humanize(name: str) -> str:
    stem = Path(name).stem if "." in name else name
    match = _ORDER_RE.match(stem)
    if match:
        stem = match.group(2) or stem
    words = re.sub(r"[_-]+", " ", stem).strip()
    if words.isupper():
        words = words.lower()
    return words[:1].upper() + words[1:] if words else name


def _sort_key(path: Path) -> tuple[int, str]:
    match = _ORDER_RE.match(path.name)
    return (int(match.group(1)) if match else 10_000, path.name.lower())


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def read_markdown_meta(path: Path) -> dict[str, str]:
    try:
        text = _strip_frontmatter(path.read_text(encoding="utf-8", errors="ignore")[:20_000])
    except OSError:
        return {"title": humanize(path.name), "excerpt": ""}
    title = ""
    excerpt = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not title and stripped.startswith("# "):
            title = stripped[2:].strip()
            continue
        if not excerpt and stripped and not stripped.startswith(("#", ">", "|", "-", "*", "!", "```", "<")):
            excerpt = re.sub(r"[*_`]", "", stripped)
        if title and excerpt:
            break
    # The whole first paragraph is the pitch (the key art is painted from it); the excerpt is its card-sized start.
    short = excerpt if len(excerpt) <= 220 else excerpt[:220].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return {"title": title or humanize(path.name), "excerpt": short, "pitch": excerpt}


def _kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "doc"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in MODEL_SUFFIXES:
        return "model"
    return "file"


def _page(directory: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _cover(directory: Path) -> Path | None:
    for child in sorted(directory.iterdir(), key=_sort_key) if directory.is_dir() else []:
        if child.is_file() and child.stem.lower() == "cover" and child.suffix.lower() in IMAGE_SUFFIXES:
            return child
    return None


def _first_image(directory: Path, depth: int = 2) -> Path | None:
    if depth < 0 or not directory.is_dir():
        return None
    children = sorted((c for c in directory.iterdir() if _visible(c) and c.name != PILLAR_DIR), key=_sort_key)
    for child in children:
        if child.is_file() and child.suffix.lower() in IMAGE_SUFFIXES:
            return child
    for child in children:
        if child.is_dir():
            found = _first_image(child, depth - 1)
            if found:
                return found
    return None


def _count_files(directory: Path, limit: int = 2000) -> int:
    count = 0
    for path in directory.rglob("*"):
        if path.is_file() and all(_visible(Path(part)) for part in path.relative_to(directory).parts):
            count += 1
            if count >= limit:
                break
    return count


def _asset_meta(path: Path) -> dict[str, Any] | None:
    """Kind and preview image of an asset document (a .md whose header has `kind:`)."""
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    from forge.blueprint_contract import split_frontmatter

    meta, _, _ = split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
    kind = str(meta.get("kind") or "").strip()
    if not kind:
        return None
    # `winner` is workspace-relative; otherwise fall back to the newest output (absolute path).
    winner = str(meta.get("winner") or "")
    outputs = path.parent / path.stem
    # A sound's winner is its take and a model can turn on hover, so tiles get both alongside the picture.
    audio = winner if Path(winner).suffix.lower() in {".mp3", ".wav", ".ogg", ".flac"} else ""
    model = next((m for sub in ("animations", "rig", "3d") for m in sorted((outputs / sub).glob("*.glb"), reverse=True)), None) if outputs.is_dir() else None
    thumb: str | Path | None = winner if winner and not audio else None
    if not thumb:
        for sub in ("candidates", "variants", "edits"):
            folder = outputs / sub
            if folder.is_dir():
                images = sorted((p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES), key=lambda p: p.stat().st_mtime, reverse=True)
                if images:
                    thumb = images[0]
                    break
    if not thumb:
        # Imported or generated 3D models get a rendered preview next to them.
        for sub in ("3d", "rig"):
            folder = outputs / sub
            previews = sorted(folder.glob("*_thumb.png")) if folder.is_dir() else []
            if previews:
                thumb = previews[0]
                break
    return {"kind": kind, "thumb": thumb, "audio": audio, "model": model}


def _folder_summary(ws_dir: Path, directory: Path, limit: int = 400) -> dict[str, Any]:
    """Asset count, direct sub-folder count and a few asset previews for a folder tile."""
    root = ws_dir.resolve()
    assets = 0
    thumbs: list[str] = []
    for path in sorted(directory.rglob("*.md"))[:limit]:
        if not all(_visible(Path(part)) for part in path.relative_to(directory).parts):
            continue
        meta = _asset_meta(path)
        if not meta:
            continue
        assets += 1
        thumb = meta["thumb"]
        if thumb and len(thumbs) < 3:
            thumbs.append(thumb.resolve().relative_to(root).as_posix() if isinstance(thumb, Path) else thumb)
    stems = {c.stem for c in directory.iterdir() if c.is_file() and c.suffix.lower() == ".md" and _asset_meta(c)}
    subfolders = sum(1 for c in directory.iterdir() if c.is_dir() and _visible(c) and c.name != PILLAR_DIR and c.name not in stems)
    return {"assets": assets, "subfolders": subfolders, "thumbs": thumbs}


def describe_folder(ws_dir: Path, rel: str = "") -> dict[str, Any]:
    """List one folder: its intro page, sub-folders and files, ready for the overview UI."""
    root = ws_dir.resolve()
    directory = resolve_rel(ws_dir, rel) if rel else root
    if not directory.is_dir():
        raise FileNotFoundError(f"No folder '{rel}'.")
    to_rel = lambda p: p.resolve().relative_to(root).as_posix()  # noqa: E731
    page = _page(directory, FRONT_PAGES if directory == root else FOLDER_PAGES)
    cover = _cover(directory)
    folders: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    asset_stems = {c.stem for c in directory.iterdir() if c.is_file() and c.suffix.lower() == ".md" and _asset_meta(c)}
    pillar_dir = directory / PILLAR_DIR
    pillars = [
        to_rel(p) for p in sorted(pillar_dir.iterdir(), key=_sort_key)
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ] if pillar_dir.is_dir() else []
    # Style-bible text in pillars/ goes in front of every prompt below this folder.
    text_pillars = [
        {"path": to_rel(p), "excerpt": _strip_frontmatter(p.read_text(encoding="utf-8", errors="ignore")).strip()[:600]}
        for p in sorted(pillar_dir.iterdir(), key=_sort_key)
        if p.is_file() and p.suffix.lower() in {".md", ".txt"}
    ] if pillar_dir.is_dir() else []
    for child in sorted((c for c in directory.iterdir() if _visible(c)), key=_sort_key):
        if child.is_dir() and (child.name == PILLAR_DIR or child.name in asset_stems):
            continue
        if child.is_dir():
            sub_page = _page(child, FOLDER_PAGES)
            meta = read_markdown_meta(sub_page) if sub_page else {"title": humanize(child.name), "excerpt": ""}
            image = _cover(child) or _first_image(child)
            folders.append({
                "name": child.name,
                "path": to_rel(child),
                "title": meta["title"],
                "excerpt": meta["excerpt"],
                "count": _count_files(child),
                "image": to_rel(image) if image else None,
                **_folder_summary(ws_dir, child),
            })
        elif child.is_file() and child != page and child != cover:
            kind = _kind(child)
            if kind == "file" and child.suffix.lower() not in TEXT_SUFFIXES:
                continue
            entry: dict[str, Any] = {"name": child.name, "path": to_rel(child), "kind": kind, "title": humanize(child.name)}
            if kind == "doc":
                entry.update(read_markdown_meta(child))
                asset = _asset_meta(child)
                if asset:
                    entry["kind"] = "asset"
                    entry["asset_kind"] = asset["kind"]
                    thumb = asset["thumb"]
                    entry["thumb"] = to_rel(thumb) if isinstance(thumb, Path) else thumb
                    entry["audio"] = asset["audio"]
                    entry["model"] = to_rel(asset["model"]) if asset["model"] else ""
            files.append(entry)
    page_meta = read_markdown_meta(page) if page else {"title": humanize(directory.name), "excerpt": ""}
    return {
        "path": "" if directory == root else to_rel(directory),
        "title": page_meta["title"],
        "excerpt": page_meta["excerpt"],
        "pitch": page_meta.get("pitch", ""),
        "page": to_rel(page) if page else None,
        "cover": to_rel(cover) if cover else None,
        "folders": folders,
        "files": files,
        "pillars": pillars,
        "text_pillars": text_pillars,
    }


def folder_tree(ws_dir: Path, directory: Path, depth: int = 4) -> list[dict[str, Any]]:
    """Visible sub-folders of `directory` (no pillars or asset outputs), nested for the sidebar."""
    if depth <= 0 or not directory.is_dir():
        return []
    root = ws_dir.resolve()
    asset_stems = {c.stem for c in directory.iterdir() if c.is_file() and c.suffix.lower() == ".md" and _asset_meta(c)}
    tree = []
    for child in sorted((c for c in directory.iterdir() if c.is_dir() and _visible(c)), key=_sort_key):
        if child.name == PILLAR_DIR or child.name in asset_stems:
            continue
        page = _page(child, FOLDER_PAGES)
        tree.append({
            "path": child.resolve().relative_to(root).as_posix(),
            "title": read_markdown_meta(page)["title"] if page else humanize(child.name),
            "children": folder_tree(ws_dir, child, depth - 1),
        })
    return tree


def project_card(ws_dir: Path, project: str) -> dict[str, Any]:
    """Title, pitch, last change and a few recent images for the projects page."""
    card: dict[str, Any] = {"project": project, "title": humanize(project), "excerpt": "", "updated": 0.0, "images": [], "sections": 0, "assets": 0}
    if not ws_dir.is_dir():
        return card
    page = _page(ws_dir, FRONT_PAGES)
    if page:
        card.update(read_markdown_meta(page))
    images: list[tuple[float, Path]] = []
    previews: list[tuple[float, str]] = []
    for path in ws_dir.rglob("*"):
        parts = path.relative_to(ws_dir).parts
        if not path.is_file() or not all(_visible(Path(part)) for part in parts):
            continue
        mtime = path.stat().st_mtime
        card["updated"] = max(card["updated"], mtime)
        if path.suffix.lower() in IMAGE_SUFFIXES and PILLAR_DIR not in parts and "textures" not in parts:
            images.append((mtime, path))
        elif path.suffix.lower() == ".md" and (asset := _asset_meta(path)):
            card["assets"] += 1
            # An asset's own picture (winner or model preview) says more than any texture or reference image.
            thumb = asset["thumb"]
            if isinstance(thumb, Path) and thumb.is_file():
                previews.append((thumb.stat().st_mtime, thumb.relative_to(ws_dir).as_posix()))
            elif isinstance(thumb, str) and thumb and (ws_dir / thumb).is_file():
                previews.append(((ws_dir / thumb).stat().st_mtime, thumb))
    cover = _cover(ws_dir)
    card["cover"] = cover.relative_to(ws_dir).as_posix() if cover else None
    card["sections"] = sum(1 for c in ws_dir.iterdir() if c.is_dir() and _visible(c) and c.name != PILLAR_DIR)
    chosen = [rel for _, rel in sorted(previews, key=lambda t: t[0], reverse=True)]
    chosen += [p.relative_to(ws_dir).as_posix() for _, p in sorted(images, key=lambda t: t[0], reverse=True)]
    card["images"] = list(dict.fromkeys(chosen))[:3]
    return card


def build_overview(ws_dir: Path, project: str) -> dict[str, Any]:
    root = describe_folder(ws_dir)
    if not root["page"]:
        root["title"] = humanize(project)
    # A folder named "08-vfx" is section 08 even when 07 doesn't exist; unnumbered ones count on from the last.
    sections, last = [], 0
    for folder in root["folders"]:
        match = _ORDER_RE.match(folder["name"])
        last = int(match.group(1)) if match else last + 1
        sections.append({"number": f"{last:02d}", **folder, "children": folder_tree(ws_dir, ws_dir / folder["path"])})
    return {"project": project, **root, "sections": sections, "showcase": showcase(ws_dir, sections),
            "empty": not root["page"] and not sections and not root["files"]}


def showcase(ws_dir: Path, sections: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    """The project's best face for its front page: assets with a picture, taken in turn from each section so the
    cast, their weapons and the world all show (section order, so characters lead)."""
    root = ws_dir.resolve()
    per_section: list[list[dict[str, Any]]] = []
    for section in sections:
        found = []
        for path in sorted((root / section["path"]).rglob("*.md"))[:300]:
            if not all(_visible(Path(part)) for part in path.relative_to(root).parts) or PILLAR_DIR in path.parts:
                continue
            meta = _asset_meta(path)
            if not meta or not meta["thumb"]:
                continue
            thumb = meta["thumb"]
            found.append({"path": path.relative_to(root).as_posix(), "title": read_markdown_meta(path)["title"], "kind": meta["kind"],
                          "thumb": thumb.resolve().relative_to(root).as_posix() if isinstance(thumb, Path) else thumb})
            if len(found) >= limit:
                break
        if found:
            per_section.append(found)
    picked: list[dict[str, Any]] = []
    while per_section and len(picked) < limit:
        for found in list(per_section):
            picked.append(found.pop(0))
            if not found:
                per_section.remove(found)
            if len(picked) >= limit:
                break
    return picked


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")[:60]


def create_folder(ws_dir: Path, parent: str, title: str) -> str:
    """Create a sub-folder with a README.md page; returns its workspace-relative path."""
    title = str(title or "").strip()
    slug = slugify(title)
    if not slug:
        raise ValueError("Give the folder a name.")
    base = resolve_rel(ws_dir, parent) if parent else ws_dir.resolve()
    target = inside(ws_dir, base / slug)
    if target.exists():
        raise ValueError(f"'{target.relative_to(ws_dir.resolve()).as_posix()}' already exists.")
    target.mkdir(parents=True)
    (target / "README.md").write_text(f"# {title}\n", encoding="utf-8")
    return target.relative_to(ws_dir.resolve()).as_posix()


def doc_to_folder(ws_dir: Path, rel: str) -> str:
    """Turn `notes/creatures.md` into the folder `notes/creatures/` with the document as its README.md."""
    path = resolve_rel(ws_dir, rel)
    if not path.is_file() or path.suffix.lower() != ".md":
        raise FileNotFoundError(f"No document '{rel}'.")
    if path.name in FRONT_PAGES:
        raise ValueError("This is already a folder page.")
    if _asset_meta(path):
        raise ValueError("Assets can't become folders; their generated images live in the folder of the same name.")
    target = path.with_suffix("")
    if (target / "README.md").exists() or target.is_file():
        raise ValueError(f"'{target.relative_to(ws_dir.resolve()).as_posix()}' already exists.")
    target.mkdir(exist_ok=True)
    path.rename(target / "README.md")
    return target.relative_to(ws_dir.resolve()).as_posix()


def read_text_file(ws_dir: Path, rel: str) -> dict[str, Any]:
    path = resolve_rel(ws_dir, rel)
    if not path.is_file():
        raise FileNotFoundError(f"No file '{rel}'.")
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError("Only text files can be opened here.")
    return {"path": path.relative_to(ws_dir.resolve()).as_posix(), "content": path.read_text(encoding="utf-8", errors="ignore"), "updated_at": path.stat().st_mtime}


def remove_style_text(ws_dir: Path, rel: str) -> str | None:
    """Move a style-bible text file out of a `pillars/` folder into `.trash/` (recoverable)."""
    path = resolve_rel(ws_dir, rel)
    if path.parent.name != PILLAR_DIR or path.suffix.lower() not in {".md", ".txt"} or not path.is_file():
        raise ValueError("That is not a style text file.")
    return move_to_trash(ws_dir, path)


def remove_pillar_image(ws_dir: Path, rel: str) -> str | None:
    """Move a pillar reference image out of a `pillars/` folder into `.trash/` (recoverable)."""
    path = resolve_rel(ws_dir, rel)
    if path.parent.name != PILLAR_DIR or path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
        raise ValueError("That is not a pillar image.")
    return move_to_trash(ws_dir, path)


def write_text_file(ws_dir: Path, rel: str, content: str) -> dict[str, Any]:
    path = resolve_rel(ws_dir, rel)
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError("Only text files can be saved here.")
    if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise ValueError("File is too large.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return read_text_file(ws_dir, rel)

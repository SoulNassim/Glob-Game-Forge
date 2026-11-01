"""Export a project to a .zip and import one back (from Glob on another computer, or a backup).

A project is the folder `<projects>/<name>/`: `workspace/` holds everything you see in Glob (pages,
assets, images, models, chats), the rest is Glob's own records (asset manifest, vault, logs). The
archive contains that folder as `<name>/…`, without `.trash/`. Importing accepts that layout, a bare
`workspace/` folder, or a zip of just the workspace content.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

from forge.storage.project_store import sanitize_project_name

STORED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".glb", ".fbx", ".zip", ".mp4", ".ogg", ".mp3", ".exr"}
SKIP_PARTS = {".trash", "__pycache__"}
MAX_IMPORT_BYTES = 8 * 1024 * 1024 * 1024


def export_project(project_dir: Path, dest: Path, progress: Callable[[int, int], None] | None = None) -> dict[str, int | str]:
    project_dir = project_dir.resolve()
    if not project_dir.is_dir():
        raise FileNotFoundError(f"No project folder '{project_dir.name}'.")
    files = [
        p for p in sorted(project_dir.rglob("*"))
        if p.is_file() and not p.is_symlink() and not SKIP_PARTS & set(p.relative_to(project_dir).parts)
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".part")
    total = 0
    with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
        for index, path in enumerate(files):
            arcname = f"{project_dir.name}/{path.relative_to(project_dir).as_posix()}"
            method = zipfile.ZIP_STORED if path.suffix.lower() in STORED_SUFFIXES else zipfile.ZIP_DEFLATED
            archive.write(path, arcname, compress_type=method)
            total += path.stat().st_size
            if progress and index % 50 == 0:
                progress(index, len(files))
    temporary.replace(dest)
    return {"path": str(dest), "files": len(files), "bytes": total, "size": dest.stat().st_size}


def _layout(names: list[str]) -> tuple[str, bool]:
    """(prefix to strip, whether the content below the prefix is a whole project rather than a bare workspace)."""
    tops = {PurePosixPath(n).parts[0] for n in names if PurePosixPath(n).parts}
    if len(tops) == 1:
        top = next(iter(tops))
        inner = {PurePosixPath(n).parts[1] for n in names if len(PurePosixPath(n).parts) > 1}
        if top == "workspace":
            return "", True
        if "workspace" in inner:
            return top + "/", True
        if any(n.lower() in (f"{top}/readme.md".lower(), f"{top}/game.md".lower()) for n in names):
            return top + "/", False
    if "workspace" in tops:
        return "", True
    return "", False


def import_archive(archive_path: Path, projects_root: Path, name: str = "") -> str:
    """Unpack a project archive into a new project folder; returns the new project's name."""
    with zipfile.ZipFile(archive_path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if not members:
            raise ValueError("The archive is empty.")
        if sum(m.file_size for m in members) > MAX_IMPORT_BYTES:
            raise ValueError("The archive is larger than 8 GB.")
        names = [m.filename.replace("\\", "/") for m in members]
        prefix, whole_project = _layout(names)
        base = sanitize_project_name(name or (prefix.strip("/") if prefix else archive_path.stem.removesuffix(".glob")) or "imported")
        root = projects_root.resolve()
        project, index = base, 2
        while (root / project).exists():
            project, index = f"{base}-{index}", index + 1
        target = root / project
        destination = target if whole_project else target / "workspace"
        try:
            for member, posix in zip(members, names):
                if not posix.startswith(prefix):
                    continue
                relative = PurePosixPath(posix[len(prefix):])
                if not relative.parts or relative.is_absolute() or ".." in relative.parts or ":" in relative.parts[0]:
                    continue
                out = (destination / Path(*relative.parts)).resolve()
                if not out.is_relative_to(target.resolve()):
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, open(out, "wb") as sink:
                    shutil.copyfileobj(source, sink)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
    if not (target / "workspace").is_dir():
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError("No Glob workspace was found in the archive.")
    return project

"""The Prototype page: a playable sketch an agent wrote, served with its art built in.

The page runs in a sandbox (no access to Glob's own pages or API), and a sandboxed page can't always fetch files
from the server. So when it is served, every quoted relative path to a picture, sound or GLB model in it is replaced
by the file itself as a data: URL. The file on disk is never changed.
"""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path

IMAGE_MAX_PX = 512  # a sprite in a sketch doesn't need more, and the page stays light
MAX_FILE_BYTES = 6 * 1024 * 1024
MAX_MODEL_BYTES = 24 * 1024 * 1024  # a model can't be shrunk here the way a picture can
MAX_TOTAL_BYTES = 64 * 1024 * 1024

_REF = re.compile(r"""(?P<q>['"`])(?P<path>(?:\.{1,2}/|/)?[\w@%+\-. /]+\.(?P<ext>png|jpe?g|webp|gif|mp3|wav|ogg|glb))(?P=q)""", re.I)
_BARE_PREFIX = re.compile(r"""(?P<q>['"`])(?:\.\./)+(?P=q)""")
_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
         "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg", "glb": "model/gltf-binary"}


def _data_url(path: Path, ext: str) -> str | None:
    try:
        if path.stat().st_size > MAX_MODEL_BYTES:
            return None
        raw = path.read_bytes()
        mime = _MIME[ext]
        if mime.startswith("image/") and ext != "gif":
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as img:
                if max(img.size) > IMAGE_MAX_PX:
                    img.thumbnail((IMAGE_MAX_PX, IMAGE_MAX_PX), Image.LANCZOS)
                out = io.BytesIO()
                img.save(out, format="PNG", optimize=True)
                raw, mime = out.getvalue(), "image/png"
        if ext != "glb" and len(raw) > MAX_FILE_BYTES:
            return None
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:  # noqa: BLE001 — a missing or odd file just stays a path; the page draws its placeholder
        return None


def inline_assets(html_path: Path, root: Path) -> bytes:
    """The page's bytes with its picture and sound paths replaced by data URLs. `root` is the folder files must stay inside."""
    text = html_path.read_text(encoding="utf-8", errors="replace")
    root = root.resolve()
    cache: dict[str, str | None] = {}
    total = 0
    joined = [False]  # a path that only exists from the workspace root: the page adds its own prefix when it loads it

    def swap(match: re.Match[str]) -> str:
        nonlocal total
        ref = match.group("path")
        if ref.startswith(("http", "data:", "//")):
            return match.group(0)
        if ref not in cache:
            target = (html_path.parent / ref.lstrip("/")).resolve() if not ref.startswith("/") else (root / ref.lstrip("/")).resolve()
            if not target.is_file() and not ref.startswith(("/", ".")):
                target = (root / ref).resolve()
                joined[0] = target.is_file() or joined[0]
            url = _data_url(target, match.group("ext").lower()) if target.is_file() and target.is_relative_to(root) else None
            if url and total + len(url) <= MAX_TOTAL_BYTES:
                total += len(url)
            else:
                url = None
            cache[ref] = url
        url = cache[ref]
        return f"{match.group('q')}{url}{match.group('q')}" if url else match.group(0)

    text = _REF.sub(swap, text)
    if joined[0]:
        # `var P = '../../'; img.src = P + 'art/x.png'`: the path is a data URL now, so the prefix has to go.
        text = _BARE_PREFIX.sub(lambda m: f"{m.group('q')}{m.group('q')}", text)
    return text.encode("utf-8")

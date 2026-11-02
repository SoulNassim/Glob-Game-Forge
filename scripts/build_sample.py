"""Pack a finished Glob workspace into the sample project that ships with Glob (forge/samples/<name>/).

Only what a first look needs is kept: pages, asset documents, the winner and newest candidate of each
asset (as JPEG), the winner take of each sound, and the newest 3D model with its textures shrunk.

    uv run python scripts/build_sample.py "<game>/glob/workspace" pocket-kaiju
"""

from __future__ import annotations

import io
import json
import re
import shutil
import struct
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forge.blueprint_contract import serialize_frontmatter, split_frontmatter  # noqa: E402

IMAGE = {".png", ".jpg", ".jpeg", ".webp"}
SKIP = {".trash", "inbox", "pillars"}


def jpeg(src: Path, dest: Path, size: int) -> Path:
    dest = dest.with_suffix(".jpg")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        img.thumbnail((size, size))
        img.convert("RGB").save(dest, "JPEG", quality=84, optimize=True)
    return dest


def shrink_glb(src: Path, dest: Path, size: int = 1024) -> None:
    """Re-encode the textures inside a GLB at `size` px; geometry is copied as is."""
    data = src.read_bytes()
    _, _, _ = struct.unpack_from("<4sII", data, 0)
    json_len, _ = struct.unpack_from("<II", data, 12)
    gltf = json.loads(data[20:20 + json_len])
    bin_start = 20 + json_len + 8
    bin_len = struct.unpack_from("<I", data, 20 + json_len)[0]
    blob = data[bin_start:bin_start + bin_len]
    image_views = {img["bufferView"]: img for img in gltf.get("images", []) if "bufferView" in img}
    out = bytearray()
    for index, view in enumerate(gltf["bufferViews"]):
        chunk = blob[view.get("byteOffset", 0):view.get("byteOffset", 0) + view["byteLength"]]
        if index in image_views:
            with Image.open(io.BytesIO(chunk)) as img:
                img.thumbnail((size, size))
                buf = io.BytesIO()
                if img.mode in ("RGBA", "LA") and img.getextrema()[-1][0] < 255:
                    img.save(buf, "PNG", optimize=True)
                    image_views[index]["mimeType"] = "image/png"
                else:
                    img.convert("RGB").save(buf, "JPEG", quality=85)
                    image_views[index]["mimeType"] = "image/jpeg"
                chunk = buf.getvalue()
        while len(out) % 4:
            out.append(0)
        view["byteOffset"] = len(out)
        view["byteLength"] = len(chunk)
        out += chunk
    while len(out) % 4:
        out.append(0)
    gltf["buffers"][0]["byteLength"] = len(out)
    head = json.dumps(gltf, separators=(",", ":")).encode()
    head += b" " * (-len(head) % 4)
    total = 12 + 8 + len(head) + 8 + len(out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(head), b"JSON") + head
                     + struct.pack("<I4s", len(out), b"BIN\0") + bytes(out))


def newest(folder: Path, suffixes: set[str], n: int = 1) -> list[Path]:
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in suffixes and not p.stem.endswith("_thumb")]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:n]


def build(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    rel = lambda p: p.relative_to(dest).as_posix()  # noqa: E731
    for doc in sorted(src.rglob("*.md")):
        parts = doc.relative_to(src).parts
        if SKIP & set(parts[:-1]) and parts[0] != "pillars":
            continue
        target = dest / doc.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        meta, body, _ = split_frontmatter(doc.read_text(encoding="utf-8"))
        outputs = doc.with_suffix("")
        if meta.get("kind"):
            out = target.with_suffix("")
            winner = src / str(meta.get("winner") or "")
            if winner.is_file() and winner.suffix.lower() in IMAGE:
                meta["winner"] = rel(jpeg(winner, out / "candidates" / winner.name, 1280))
            for extra in newest(outputs / "candidates", IMAGE, 2):
                if extra != winner:
                    jpeg(extra, out / "candidates" / extra.name, 1024)
                    break
            if winner.is_file() and winner.suffix.lower() in {".mp3", ".wav", ".ogg"}:
                (out / "audio").mkdir(parents=True, exist_ok=True)
                shutil.copy2(winner, out / "audio" / winner.name)
                meta["winner"] = rel(out / "audio" / winner.name)
            for glb in newest(outputs / "3d", {".glb"}):
                shrink_glb(glb, out / "3d" / glb.name)
                thumb = glb.with_name(f"{glb.stem}_thumb.png")
                if thumb.is_file():
                    with Image.open(thumb) as img:
                        img.thumbnail((384, 384))
                        img.save(out / "3d" / thumb.name, optimize=True)
        if meta.get("kind") and doc.stem == "key-art":
            meta["cover"] = True  # its winner is the project cover
        target.write_text(serialize_frontmatter(meta, body) if meta else body, encoding="utf-8")
    # Pages show images by their old .png names: point them at the JPEGs.
    for doc in dest.rglob("*.md"):
        text = doc.read_text(encoding="utf-8")
        fixed = re.sub(r"([\w./-]+)\.png\b", lambda m: f"{m[1]}.jpg" if (dest / f"{m[1]}.jpg").is_file() else m[0], text)
        if fixed != text:
            doc.write_text(fixed, encoding="utf-8")
    for image in newest(src / "pillars", IMAGE, 4):
        jpeg(image, dest / "pillars" / image.name, 1024)
    cover = next((p for p in src.iterdir() if p.stem.lower() == "cover" and p.suffix.lower() in IMAGE), None)
    if cover:
        jpeg(cover, dest / "cover.jpg", 1600)
    total = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    print(f"{dest}: {total / 1e6:.1f} MB")


if __name__ == "__main__":
    build(Path(sys.argv[1]).expanduser(), Path(__file__).resolve().parents[1] / "forge" / "samples" / sys.argv[2])

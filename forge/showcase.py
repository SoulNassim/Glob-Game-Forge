"""Whole-project views: the board (every asset on one canvas) and the press kit (a shareable web page).

Both are read straight from the workspace: an asset's picture is its winner image, else its model preview,
else its newest candidate; a sound shows its winner take.
"""

from __future__ import annotations

import io
import time
import zipfile
from html import escape
from pathlib import Path
from typing import Any

from forge.asset_studio import AUDIO_KINDS, AUDIO_SUFFIXES, assets_under, describe_asset
from forge.overview import IMAGE_SUFFIXES, build_overview, humanize, resolve_rel

# Pinned so a press kit made today keeps working; it only loads when someone opens a page with a 3D model.
MODEL_VIEWER = "https://unpkg.com/@google/model-viewer@3.5.0/dist/model-viewer.min.js"
# Height in metres used to line models up when an asset doesn't set `height_m`.
KIND_HEIGHT = {"character": 1.7, "creature": 2.5, "vehicle": 2.0, "prop": 1.0, "item": 0.4, "weapon": 1.0}


def _title(asset: dict[str, Any]) -> str:
    for line in asset["body"].splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return str(asset["meta"].get("title") or humanize(Path(asset["path"]).stem))


def board_entry(ws_dir: Path, rel: str) -> dict[str, Any]:
    asset = describe_asset(ws_dir, rel)
    out = asset["outputs"]
    models = [m for kind in ("animations", "rig", "3d") for m in out[kind] if m["type"] == "model"]
    winner = asset["winner"]
    image = winner if winner and Path(winner).suffix.lower() in IMAGE_SUFFIXES else ""
    if not image and models and models[0].get("thumb"):
        image = models[0]["thumb"]
    if not image:
        image = next((o["path"] for o in out["candidates"] if o["type"] == "image"), "")
    audio = winner if winner and Path(winner).suffix.lower() in AUDIO_SUFFIXES else next((o["path"] for o in out["audio"]), "")
    folder = Path(asset["path"]).parent.as_posix()
    try:
        height = float(asset["meta"].get("height_m") or KIND_HEIGHT.get(asset["kind"], 1.0))
    except (TypeError, ValueError):
        height = KIND_HEIGHT.get(asset["kind"], 1.0)
    return {
        "path": asset["path"],
        "title": _title(asset),
        "kind": asset["kind"],
        "folder": "" if folder == "." else folder,
        "image": image,
        "picked": bool(winner),
        "audio": audio if asset["kind"] in AUDIO_KINDS else "",
        "model": models[0]["path"] if models else "",
        "height_m": height,
        "prompt": asset["prompt"],
    }


def board(ws_dir: Path, project: str) -> dict[str, Any]:
    """Every asset, grouped by top-level section in the overview's order."""
    ov = build_overview(ws_dir, project)
    groups: dict[str, list[dict[str, Any]]] = {s["path"]: [] for s in ov["sections"]}
    for rel in assets_under(ws_dir):
        try:
            entry = board_entry(ws_dir, rel)
        except (FileNotFoundError, ValueError):
            continue
        groups.setdefault(entry["folder"].split("/")[0], []).append(entry)
    titles = {s["path"]: (s["number"], s["title"]) for s in ov["sections"]}
    return {
        "title": ov["title"],
        "excerpt": ov["excerpt"],
        "cover": ov["cover"],
        "groups": [{"section": key, "number": titles.get(key, ("", ""))[0], "title": titles.get(key, ("", humanize(key) or "Project root"))[1], "assets": items}
                   for key, items in groups.items() if items],
    }


def _media(ws_dir: Path, rel: str, files: dict[str, Path]) -> str:
    """Add a workspace file to the kit once and return its path inside the kit."""
    if not rel:
        return ""
    source = resolve_rel(ws_dir, rel)
    if not source.is_file():
        return ""
    name = f"media/{rel.replace('/', '__')}"
    files[name] = source
    return name


def press_kit(ws_dir: Path, project: str) -> tuple[str, dict[str, Path]]:
    """The press kit's index.html and the files it shows."""
    data = board(ws_dir, project)
    files: dict[str, Path] = {}
    cover = _media(ws_dir, data["cover"] or "", files)
    has_3d = False
    sections = []
    for group in data["groups"]:
        cards = []
        for a in group["assets"]:
            image = _media(ws_dir, a["image"], files)
            if a["audio"]:
                audio = _media(ws_dir, a["audio"], files)
                cards.append(f'<figure class="snd"><div class="wave">♪</div><figcaption>{escape(a["title"])}<small>{escape(a["kind"])}</small></figcaption>'
                             f'<audio controls preload="none" src="{escape(audio)}"></audio></figure>')
            elif a["model"] and a["model"].lower().endswith(".glb"):
                has_3d = True
                model = _media(ws_dir, a["model"], files)
                cards.append(f'<figure><model-viewer src="{escape(model)}" poster="{escape(image)}" camera-controls auto-rotate shadow-intensity="1" '
                             f'alt="{escape(a["title"])}"></model-viewer><figcaption>{escape(a["title"])}<small>{escape(a["kind"])} · 3D</small></figcaption></figure>')
            elif image:
                cards.append(f'<figure><img src="{escape(image)}" alt="{escape(a["title"])}" loading="lazy"><figcaption>{escape(a["title"])}<small>{escape(a["kind"])}</small></figcaption></figure>')
        if cards:
            sections.append(f'<section><h2><span>{escape(group["number"])}</span>{escape(group["title"])}</h2><div class="grid">{"".join(cards)}</div></section>')
    title = escape(data["title"])
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Press kit</title>
{f'<script type="module" src="{MODEL_VIEWER}"></script>' if has_3d else ""}
<style>
:root {{ --bg:#0d0d12; --card:#17171f; --line:#2a2a36; --text:#f2f2f7; --muted:#9a9aab; --accent:#ff7eb6; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
header {{ position:relative; min-height:62vh; display:flex; align-items:flex-end; padding:48px max(16px,6vw); overflow:hidden; }}
header img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
header::after {{ content:""; position:absolute; inset:0; background:linear-gradient(180deg,transparent 35%,var(--bg)); }}
header div {{ position:relative; z-index:1; max-width:760px; }}
h1 {{ font-size:clamp(40px,7vw,84px); line-height:1; margin:0 0 12px; letter-spacing:-.03em; }}
header p {{ color:#d8d8e2; font-size:clamp(16px,2vw,20px); margin:0; }}
main {{ padding:0 max(16px,6vw) 80px; }}
h2 {{ font-size:22px; margin:56px 0 18px; display:flex; gap:12px; align-items:baseline; }} h2 span {{ color:var(--accent); font-size:14px; font-variant-numeric:tabular-nums; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:16px; }}
figure {{ margin:0; background:var(--card); border:1px solid var(--line); border-radius:16px; overflow:hidden; display:flex; flex-direction:column; }}
figure img, model-viewer {{ width:100%; aspect-ratio:1; object-fit:cover; display:block; background:#1f1f29; }}
figcaption {{ padding:12px 14px; font-weight:600; display:flex; justify-content:space-between; gap:8px; }} figcaption small {{ color:var(--muted); font-weight:400; }}
.snd .wave {{ aspect-ratio:2.2; display:grid; place-items:center; font-size:42px; color:var(--accent); background:radial-gradient(circle,#2a1a26,#17171f); }}
.snd audio {{ width:calc(100% - 20px); margin:0 10px 12px; }}
footer {{ color:var(--muted); text-align:center; padding:32px 16px 48px; font-size:13px; }}
</style></head><body>
<header>{f'<img src="{escape(cover)}" alt="">' if cover else ""}<div><h1>{title}</h1>{f"<p>{escape(data['excerpt'])}</p>" if data["excerpt"] else ""}</div></header>
<main>{"".join(sections)}</main>
<footer>Made with Glob · {time.strftime("%B %Y")}</footer>
</body></html>"""
    return html, files


def press_kit_zip(ws_dir: Path, project: str) -> bytes:
    html, files = press_kit(ws_dir, project)
    buffer = io.BytesIO()
    root = f"{project}-press-kit"
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{root}/index.html", html)
        for name, source in files.items():
            archive.write(source, f"{root}/{name}")
        archive.writestr(f"{root}/README.txt", "Open index.html in a browser, or upload this folder to any static host (itch.io, GitHub Pages, Netlify).\n")
    return buffer.getvalue()


"""Order modes: how far Glob goes on its own when a coding agent orders an asset.

    manual  new orders are filed as asset pages with their prompt; you press Generate (Glob asks first)
    2d      also generate 2D candidates for every new order
    full    everything the order's kind needs: candidates, the first one picked as winner, then the 3D model
            (or the audio take) and a rig when the order asks for one

Settings live per project in `workspace/ORDER_MODE.json`. Auto modes stop once they have spent `spend_cap_usd`
since the mode or the cap was last changed, so an agent ordering overnight can't run up a bill.

Each automatic step runs once per order (`auto_steps` on the order). A step that fails is not retried on its
own: the asset page shows the error and you take it from there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from forge import asset_studio, orders
from forge.overview import HIDDEN_NAMES, PILLAR_DIR, resolve_rel

MODES = ("manual", "2d", "full")
MODE_FILE = "ORDER_MODE.json"
DEFAULTS: dict[str, Any] = {"mode": "manual", "auto_file": True, "spend_cap_usd": 5.0, "spent_baseline_usd": 0.0}
FALLBACK_FOLDER = "agent-orders"

# Words in a folder name that say "this kind goes here", best match first.
KIND_HINTS: dict[str, tuple[str, ...]] = {
    "character": ("character", "hero", "player", "npc", "survivor", "people"),
    "creature": ("creature", "enem", "monster", "mob", "boss", "beast"),
    "item": ("item", "pickup", "loot", "equip", "inventory"),
    "weapon": ("weapon", "equip", "item"),
    "prop": ("prop", "object", "decor", "furniture"),
    "vehicle": ("vehicle", "car", "ship", "mount"),
    "environment": ("environment", "world", "level", "scene", "tile", "map"),
    "ui": ("ui", "interface", "hud", "icon", "menu"),
    "vfx": ("vfx", "effect", "fx", "particle"),
    "sfx": ("sfx", "sound", "audio"),
    "voice": ("voice", "dialog", "audio", "sound"),
    "music": ("music", "audio", "sound"),
}
_TOKEN_RE = re.compile(r"[^a-z0-9]+")


# ---------- settings ----------------------------------------------------------------


def load(ws: Path) -> dict[str, Any]:
    try:
        data = json.loads((ws / MODE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    settings = DEFAULTS | {k: v for k, v in data.items() if k in DEFAULTS}
    if settings["mode"] not in MODES:
        settings["mode"] = "manual"
    return settings


def save(ws: Path, fields: dict[str, Any], spent_now: float) -> dict[str, Any]:
    """Change the mode, auto filing or the cap. Changing the mode or the cap restarts the spend count."""
    settings = load(ws)
    before = (settings["mode"], settings["spend_cap_usd"])
    if "mode" in fields:
        if fields["mode"] not in MODES:
            raise ValueError(f"mode must be one of: {', '.join(MODES)}.")
        settings["mode"] = fields["mode"]
    if "auto_file" in fields:
        settings["auto_file"] = bool(fields["auto_file"])
    if "spend_cap_usd" in fields:
        try:
            cap = float(fields["spend_cap_usd"])
        except (TypeError, ValueError) as exc:
            raise ValueError("spend_cap_usd must be a number.") from exc
        if cap < 0:
            raise ValueError("spend_cap_usd can't be negative.")
        settings["spend_cap_usd"] = round(cap, 2)
    if (settings["mode"], settings["spend_cap_usd"]) != before:
        settings["spent_baseline_usd"] = round(spent_now, 4)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / MODE_FILE).write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


def status(ws: Path, spent_now: float) -> dict[str, Any]:
    settings = load(ws)
    spent = round(max(0.0, spent_now - settings["spent_baseline_usd"]), 4)
    paused = settings["mode"] != "manual" and spent >= settings["spend_cap_usd"]
    return settings | {"spent_usd": spent, "paused": paused}


# ---------- filing new orders -------------------------------------------------------


def _folders(ws: Path) -> list[tuple[int, str, set[str]]]:
    found = []
    for depth, pattern in ((1, "*"), (2, "*/*")):
        for path in sorted(ws.glob(pattern)):
            parts = path.relative_to(ws).parts
            if not path.is_dir() or any(p.startswith((".", "_")) or p in HIDDEN_NAMES or p == PILLAR_DIR for p in parts):
                continue
            if (path.parent / f"{path.name}.md").is_file():
                continue  # an asset's output folder, not a section
            tokens = {t for t in _TOKEN_RE.split(path.name.lower()) if t}
            found.append((depth, path.relative_to(ws).as_posix(), tokens))
    return found


def folder_for(ws: Path, kind: str) -> str:
    """The workspace section an order of this kind belongs in, or `agent-orders/` when nothing fits."""
    folders = _folders(ws)
    for hint in KIND_HINTS.get(kind, (kind,)):
        for _, rel, tokens in folders:  # top-level sections come first
            if any(token.startswith(hint) for token in tokens):
                return rel
    return FALLBACK_FOLDER


def _free_path(ws: Path, folder: str, title: str) -> str:
    stem = orders.slug(title).replace("_", "-")
    rel, n = f"{folder}/{stem}.md", 2
    while (ws / rel).exists():
        rel, n = f"{folder}/{stem}-{n}.md", n + 1
    return rel


def file_drafts(ws: Path) -> list[str]:
    """File every draft as an asset page with the agent's brief as its prompt. Returns the order ids filed."""
    filed = []
    for order in orders.list_orders(ws, "draft"):
        if order.get("auto_filed"):
            continue  # filed once already; it was moved or deleted on purpose, so leave it a draft
        folder = folder_for(ws, order["kind"])
        orders.file_order(ws, order["id"], _free_path(ws, folder, order["title"]))
        orders._write(ws, order["id"], lambda entry: entry.__setitem__("auto_filed", True))
        filed.append(order["id"])
    return filed


# ---------- generating --------------------------------------------------------------


def next_step(ws: Path, order: dict[str, Any], mode: str) -> tuple[str, dict[str, Any]] | None:
    """What auto mode does next for this order: ("run", request), ("winner", {"path": ...}) or None."""
    if mode == "manual" or order["status"] not in ("filed", "in_progress") or not order.get("asset"):
        return None
    done = set(order.get("auto_steps") or [])
    try:
        asset = asset_studio.describe_asset(ws, order["asset"])
    except (FileNotFoundError, ValueError):
        return None
    outputs = asset["outputs"]
    audio = asset["kind"] in asset_studio.AUDIO_KINDS
    takes = [o for o in outputs["candidates"] + outputs["audio"] if o["type"] == ("audio" if audio else "image")]
    winner = str(asset["winner"] or "")

    if not winner and not takes:
        return ("run", {"step": "candidates"}) if "candidates" not in done else None
    if mode != "full":
        return None
    if not winner and takes:
        return "winner", {"path": takes[-1]["path"]}  # newest first, so the last is the first one made
    have = order.get("have") or {}
    if "model" in have and not have["model"] and not audio:
        return ("run", {"step": "make_3d"}) if "make_3d" not in done else None
    if "rig" in have and not have["rig"] and have.get("model"):
        return ("run", {"step": "rig"}) if "rig" not in done else None
    return None


def mark_step(ws: Path, order_id: str, step: str) -> None:
    def add(entry: dict[str, Any]) -> None:
        steps = entry.setdefault("auto_steps", [])
        if step not in steps:
            steps.append(step)

    orders._write(ws, order_id, add)


def pick_winner(ws: Path, order: dict[str, Any], path: str) -> None:
    resolve_rel(ws, path)
    asset_studio.update_asset(ws, order["asset"], {"winner": path})
    mark_step(ws, order["id"], "winner")

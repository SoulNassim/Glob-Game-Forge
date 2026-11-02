"""Agent orders: a coding agent asks Glob for an asset, Glob makes it, the finished files are delivered to the game.

One order is one asset the game needs. Orders live in `workspace/ORDERS.json`:

    draft        ordered, not in the workspace yet (no asset .md)
    filed        sorted into the workspace as an asset .md (`order:` in its header links back)
    in_progress  something has been generated for it
    ready        the asset has everything its kind needs (image, 3D model, audio, ...)
    delivered    copied to `<game>/glob_delivery/<id>/`, listed in `glob_delivery/manifest.json`
    integrated   the coding agent swapped its placeholder for the delivered files
    cancelled    no longer wanted

draft/filed/in_progress/ready are worked out from the workspace every time, so they can never go stale; only
delivered, integrated and cancelled are stored. A delivered order whose asset changes becomes ready again and is
delivered anew.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from forge.blueprint_contract import serialize_frontmatter, split_frontmatter
from forge.overview import IMAGE_SUFFIXES, MODEL_SUFFIXES, resolve_rel

ORDERS_FILE = "ORDERS.json"
DELIVERY_DIR = "glob_delivery"
MANIFEST = "manifest.json"
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".flac"}

STATUSES = ("draft", "filed", "in_progress", "ready", "delivered", "integrated", "cancelled")
NEEDS = ("image", "model", "rig", "animations", "audio")
# What "done" means for each kind unless the order says otherwise.
DEFAULT_NEEDS: dict[str, list[str]] = {
    "character": ["image", "model"],
    "creature": ["image", "model"],
    "item": ["image", "model"],
    "weapon": ["image", "model"],
    "prop": ["image", "model"],
    "vehicle": ["image", "model"],
    "environment": ["image"],
    "ui": ["image"],
    "vfx": ["image"],
    "sfx": ["audio"],
    "voice": ["audio"],
    "music": ["audio"],
}
KINDS = tuple(DEFAULT_NEEDS)
MAX_ORDERS = 2000

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _now() -> float:
    return time.time()


def slug(text: str) -> str:
    return _SLUG_RE.sub("_", str(text or "").lower()).strip("_")[:48] or "asset"


# ---------- storage ----------------------------------------------------------------


def _path(ws: Path) -> Path:
    return ws / ORDERS_FILE


def load(ws: Path) -> list[dict[str, Any]]:
    path = _path(ws)
    if not path.is_file():
        return _migrate_requirements(ws)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [o for o in data.get("orders", []) if isinstance(o, dict) and o.get("id")]


def save(ws: Path, orders: list[dict[str, Any]]) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    tmp = _path(ws).with_suffix(".tmp")
    tmp.write_text(json.dumps({"orders": orders[-MAX_ORDERS:]}, indent=2), encoding="utf-8")
    tmp.replace(_path(ws))


def _migrate_requirements(ws: Path) -> list[dict[str, Any]]:
    """Open lines of the old ASSET_REQUIREMENTS.md become draft orders, once."""
    old = ws / "ASSET_REQUIREMENTS.md"
    orders: list[dict[str, Any]] = []
    if old.is_file():
        pattern = re.compile(r"^- \[( |~)\]\s*`([^`]+)`\s*\|\s*\*\*([^*]+)\*\*\s*\|\s*`?([^`|]*)`?\s*\|\s*(.*)$")
        for line in old.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = pattern.match(line.strip())
            if not match or match.group(2) == "aethelgard_knight":  # the old seeded example row
                continue
            kind = "item" if "icon" in match.group(3).lower() or "ui" in match.group(3).lower() else "prop"
            orders.append(_new_order(orders, match.group(2), kind, match.group(5).split("|")[0].strip(), match.group(4).strip(), "migrated"))
    if orders:
        save(ws, orders)
    return orders


def _new_order(existing: list[dict[str, Any]], title: str, kind: str, brief: str, placeholder: str, requested_by: str,
               needs: list[str] | None = None, context: str = "") -> dict[str, Any]:
    base = slug(title)
    taken = {o["id"] for o in existing}
    order_id, n = base, 2
    while order_id in taken:
        order_id, n = f"{base}_{n}", n + 1
    return {
        "id": order_id,
        "title": str(title).strip()[:120] or order_id,
        "kind": kind,
        "brief": str(brief or "").strip()[:4000],
        "context": str(context or "").strip()[:2000],
        "placeholder": str(placeholder or "").strip()[:400],
        "needs": [n for n in (needs or DEFAULT_NEEDS[kind]) if n in NEEDS] or DEFAULT_NEEDS[kind],
        "asset": "",
        "status": "draft",
        "requested_by": str(requested_by or "agent")[:60],
        "created_at": _now(),
        "updated_at": _now(),
    }


# ---------- workspace state ---------------------------------------------------------


def _asset_meta(ws: Path, rel: str) -> dict[str, Any] | None:
    try:
        path = resolve_rel(ws, rel)
    except ValueError:
        return None
    if not path.is_file():
        return None
    meta, _, _ = split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
    return meta


def _files(folder: Path, suffixes: set[str]) -> list[Path]:
    if not folder.is_dir():
        return []
    found = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in suffixes and not p.stem.endswith("_thumb")]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def deliverables(ws: Path, order: dict[str, Any]) -> dict[str, list[Path]]:
    """The files an order would deliver now, by need. Empty lists mean that part is missing."""
    meta = _asset_meta(ws, order.get("asset") or "")
    if meta is None:
        return {}
    asset_path = resolve_rel(ws, order["asset"])
    out = asset_path.parent / asset_path.stem
    winner = str(meta.get("winner") or "")
    winner_path = resolve_rel(ws, winner) if winner else None
    has_winner = bool(winner_path and winner_path.is_file())
    image = [winner_path] if has_winner and winner_path.suffix.lower() in IMAGE_SUFFIXES else []
    audio = [winner_path] if has_winner and winner_path.suffix.lower() in AUDIO_SUFFIXES else _files(out / "audio", AUDIO_SUFFIXES)[:1]
    models = _files(out / "3d", MODEL_SUFFIXES)
    model: list[Path] = []
    if models:
        main = next((p for p in models if p.suffix.lower() == ".glb"), models[0])
        model = [p for p in sorted((out / "3d").iterdir()) if p.is_file() and p.stem == main.stem and not p.stem.endswith("_thumb")]
    return {
        "image": image,
        "model": model,
        "rig": _files(out / "rig", MODEL_SUFFIXES),
        "animations": _files(out / "animations", MODEL_SUFFIXES),
        "audio": audio,
    }


def _started(ws: Path, order: dict[str, Any]) -> bool:
    try:
        asset_path = resolve_rel(ws, order["asset"])
    except (ValueError, KeyError):
        return False
    out = asset_path.parent / asset_path.stem
    return out.is_dir() and any(p.is_file() for p in out.rglob("*"))


def _fingerprint(files: dict[str, list[Path]]) -> str:
    digest = hashlib.sha256()
    for need in NEEDS:
        for path in files.get(need, []):
            digest.update(f"{need}:{path.name}:{path.stat().st_size}:{path.stat().st_mtime_ns}".encode())
    return digest.hexdigest()[:16]


def evaluate(ws: Path, order: dict[str, Any]) -> dict[str, Any]:
    """The order with its live status and which needs are met."""
    view = dict(order)
    stored = order.get("status", "draft")
    meta = _asset_meta(ws, order.get("asset") or "") if order.get("asset") else None
    if order.get("asset") and meta is None:
        view["asset"] = ""  # the asset was moved or deleted
    files = deliverables(ws, view) if view.get("asset") else {}
    view["have"] = {need: bool(files.get(need)) for need in order.get("needs", [])}
    complete = bool(view["have"]) and all(view["have"].values())
    view["fingerprint"] = _fingerprint(files) if complete else ""
    if stored == "cancelled":
        view["status"] = "cancelled"
    elif not view.get("asset"):
        view["status"] = "draft"
    elif complete and stored in ("delivered", "integrated") and order.get("delivered_fingerprint") == view["fingerprint"]:
        view["status"] = stored
    elif complete:
        view["status"] = "ready"
    elif _started(ws, view):
        view["status"] = "in_progress"
    else:
        view["status"] = "filed"
    return view


def summary(orders: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for order in orders:
        counts[order["status"]] = counts.get(order["status"], 0) + 1
    return counts


# ---------- operations --------------------------------------------------------------


def list_orders(ws: Path, status: str | None = None) -> list[dict[str, Any]]:
    views = [evaluate(ws, o) for o in load(ws)]
    return [v for v in views if not status or v["status"] == status]


def get(ws: Path, order_id: str) -> dict[str, Any]:
    order = next((o for o in load(ws) if o["id"] == order_id), None)
    if order is None:
        raise KeyError(f"No order '{order_id}'.")
    return evaluate(ws, order)


def place(ws: Path, title: str, kind: str, brief: str = "", placeholder: str = "", *, needs: list[str] | None = None,
          context: str = "", requested_by: str = "agent") -> dict[str, Any]:
    kind = str(kind or "").strip().lower()
    if kind not in DEFAULT_NEEDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}.")
    if not str(title or "").strip():
        raise ValueError("An order needs a title (what the asset is).")
    orders = load(ws)
    same = next((o for o in orders if placeholder and o.get("placeholder") == placeholder and o.get("status") != "cancelled"), None)
    if same is not None:
        return evaluate(ws, same) | {"note": "An order for this placeholder already exists."}
    order = _new_order(orders, title, kind, brief, placeholder, requested_by, needs, context)
    orders.append(order)
    save(ws, orders)
    return evaluate(ws, order)


def _write(ws: Path, order_id: str, change: Any) -> dict[str, Any]:
    orders = load(ws)
    order = next((o for o in orders if o["id"] == order_id), None)
    if order is None:
        raise KeyError(f"No order '{order_id}'.")
    change(order)
    order["updated_at"] = _now()
    save(ws, orders)
    return evaluate(ws, order)


def update(ws: Path, order_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    def apply(order: dict[str, Any]) -> None:
        for key in ("title", "brief", "context", "placeholder"):
            if isinstance(fields.get(key), str):
                order[key] = fields[key].strip()[:4000]
        if isinstance(fields.get("needs"), list):
            order["needs"] = [n for n in fields["needs"] if n in NEEDS] or order["needs"]
        if fields.get("kind") in DEFAULT_NEEDS:
            order["kind"] = fields["kind"]
        status = fields.get("status")
        if status == "integrated":
            if order.get("status") not in ("delivered", "integrated"):
                raise ValueError("Only a delivered order can be marked integrated.")
            order["status"] = "integrated"
        elif status == "cancelled":
            order["status"] = "cancelled"
        elif status == "reopen":
            order["status"] = "draft"
        elif status:
            raise ValueError("status can be set to integrated, cancelled or reopen; the rest follows the workspace.")
        if isinstance(fields.get("note"), str) and fields["note"].strip():
            order.setdefault("notes", []).append({"at": _now(), "text": fields["note"].strip()[:1000]})

    return _write(ws, order_id, apply)


def remove(ws: Path, order_id: str) -> None:
    orders = load(ws)
    save(ws, [o for o in orders if o["id"] != order_id])


def file_order(ws: Path, order_id: str, path: str) -> dict[str, Any]:
    """Sort an order into the workspace: link an existing asset .md, or create one at `path`."""
    from forge import asset_studio

    order = get(ws, order_id)
    rel = str(path or "").strip().replace("\\", "/").strip("/")
    if not rel:
        raise ValueError("Give the workspace path for the asset, e.g. 02-characters/blue-faction/orc-warrior.md.")
    if not rel.endswith(".md"):
        rel = f"{rel}/{order_id.replace('_', '-')}.md"
    target = resolve_rel(ws, rel)
    asset_kind = order["kind"] if order["kind"] in asset_studio.ASSET_KINDS else ("prop" if order["kind"] not in ("sfx", "voice", "music") else order["kind"])
    if target.is_file():
        meta, body, _ = split_frontmatter(target.read_text(encoding="utf-8", errors="ignore"))
        meta["order"] = order_id
        meta.setdefault("kind", asset_kind)
        target.write_text(serialize_frontmatter(meta, body), encoding="utf-8")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        meta = {"kind": asset_kind, "prompt": order.get("brief") or order["title"], "order": order_id}
        lines = [f"# {order['title']}", ""]
        who = order.get("requested_by") or "the coding agent"
        lines.append(f"> Ordered by {who}" + (f" for `{order['placeholder']}`." if order.get("placeholder") else "."))
        if order.get("brief"):
            lines += ["", order["brief"]]
        if order.get("context"):
            lines += ["", "## Context", "", order["context"]]
        target.write_text(serialize_frontmatter(meta, "\n".join(lines) + "\n"), encoding="utf-8")
    final_rel = target.resolve().relative_to(ws.resolve()).as_posix()

    def link(entry: dict[str, Any]) -> None:
        entry["asset"] = final_rel
        if entry.get("status") == "cancelled":
            entry["status"] = "draft"

    return _write(ws, order_id, link)


# ---------- delivery ----------------------------------------------------------------


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(game: Path) -> dict[str, Any]:
    try:
        data = json.loads((game / DELIVERY_DIR / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"orders": {}}
    return data if isinstance(data.get("orders"), dict) else {"orders": {}}


def deliver(ws: Path, game: Path, order_id: str, project: str = "") -> dict[str, Any]:
    """Copy a ready order's files to `<game>/glob_delivery/<id>/` and record them in the manifest."""
    view = get(ws, order_id)
    if view["status"] not in ("ready", "delivered", "integrated"):
        missing = [need for need, ok in view.get("have", {}).items() if not ok]
        raise ValueError(f"'{order_id}' is not ready yet" + (f" (missing: {', '.join(missing)})." if missing else "."))
    files = deliverables(ws, view)
    root = game / DELIVERY_DIR
    folder = root / order_id
    staging = root / f".{order_id}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    written: list[dict[str, Any]] = []

    def put(src: Path, rel: str, role: str) -> None:
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        written.append({"path": f"{DELIVERY_DIR}/{order_id}/{rel}", "role": role, "sha256": _sha(dest), "bytes": dest.stat().st_size})

    for src in files.get("image", [])[:1]:
        put(src, f"{order_id}{src.suffix.lower()}", "image")
    for src in files.get("model", []):
        role = "model" if src.suffix.lower() in MODEL_SUFFIXES else "model_file"
        put(src, f"{order_id}{src.suffix.lower()}" if role == "model" else src.name, role)
    for need in ("rig", "animations"):
        for src in files.get(need, []):
            put(src, f"animations/{src.name}", need)
    for src in files.get("audio", [])[:1]:
        put(src, f"{order_id}{src.suffix.lower()}", "audio")

    shutil.rmtree(folder, ignore_errors=True)
    staging.replace(folder)
    manifest = read_manifest(game)
    manifest["project"] = project
    manifest["updated_at"] = _now()
    manifest["orders"][order_id] = {
        "title": view["title"],
        "kind": view["kind"],
        "placeholder": view.get("placeholder", ""),
        "asset": view.get("asset", ""),
        "delivered_at": _now(),
        "files": written,
    }
    (root / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def mark(entry: dict[str, Any]) -> None:
        entry["status"] = "delivered"
        entry["delivered_at"] = _now()
        entry["delivered_fingerprint"] = view["fingerprint"]

    return _write(ws, order_id, mark) | {"files": written}


def auto_deliver(ws: Path, game: Path | None, project: str = "") -> list[str]:
    """Deliver every ready order; returns the ids delivered. Errors on one order never stop the others."""
    if game is None or not game.is_dir():
        return []
    delivered = []
    for view in list_orders(ws, "ready"):
        try:
            deliver(ws, game, view["id"], project)
            delivered.append(view["id"])
        except (OSError, ValueError, KeyError):
            continue
    return delivered

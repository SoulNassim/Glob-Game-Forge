"""Final Vault: every asset's chosen image and 3D model, laid out for a game engine.

The vault is a view over the workspace, not a copy: an asset is in it once it has a winner image or a
3D model. Exporting writes one folder per asset that mirrors the workspace folders::

    <project>-vault/
      manifest.json
      <folder path>/<asset>/
        <asset>.png            the winner image
        <asset>.glb (.obj …)   the newest 3D model, in every format that was made
        <asset>_thumb.png      the model's preview, when there is one
        <asset>.json           kind, prompt, sources and the 3D settings used
        history/<kind>/…       every candidate, variant, edit and older model (optional)
"""

from __future__ import annotations

import io
import json
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from forge.asset_studio import describe_asset, output_dir
from forge.overview import _visible, humanize, resolve_rel

EXPORT_DIR = "exports"


def _asset_docs(ws_dir: Path) -> list[Path]:
    root = ws_dir.resolve()
    docs = []
    for path in sorted(root.rglob("*.md")):
        parts = path.relative_to(root).parts
        if all(_visible(Path(part)) for part in parts) and "pillars" not in parts:
            docs.append(path)
    return docs


def _title(asset: dict[str, Any]) -> str:
    for line in asset["body"].splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return str(asset["meta"].get("title") or humanize(Path(asset["path"]).stem))


def _sidecar(ws_dir: Path, file_rel: str) -> dict[str, Any]:
    """The vault record written when a file was generated (prompt, model, 3D settings)."""
    file_path = Path(file_rel)
    asset_rel = file_path.parent.parent.as_posix() + ".md"
    record = ws_dir.parent / "vault" / Path(asset_rel).with_suffix("") / file_path.parent.name / f"{file_path.stem}.json"
    try:
        return json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def vault_entry(ws_dir: Path, rel: str) -> dict[str, Any] | None:
    asset = describe_asset(ws_dir, rel)
    if not asset["is_asset"]:
        return None
    outputs = asset["outputs"]
    models = outputs["3d"]
    model = models[0] if models else None
    if not asset["winner"] and not model:
        return None
    folder = Path(asset["path"]).parent.as_posix()
    return {
        "path": asset["path"],
        "name": Path(asset["path"]).stem,
        "title": _title(asset),
        "kind": asset["kind"],
        "folder": "" if folder == "." else folder,
        "winner": asset["winner"],
        "model": model,
        "model_settings": _sidecar(ws_dir, model["path"]).get("settings", {}) if model else {},
        "counts": {kind: len(items) for kind, items in outputs.items()},
        "ready": bool(asset["winner"] and model),
        "prompt": asset["prompt"],
    }


def list_vault(ws_dir: Path) -> dict[str, Any]:
    """Every asset with a winner or a model, grouped by top-level section."""
    entries = []
    for doc in _asset_docs(ws_dir):
        try:
            entry = vault_entry(ws_dir, doc.relative_to(ws_dir.resolve()).as_posix())
        except (FileNotFoundError, ValueError):
            continue
        if entry:
            entries.append(entry)
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(entry["folder"].split("/")[0], []).append(entry)
    return {
        "groups": [{"section": key, "title": humanize(key) if key else "Project root", "assets": items} for key, items in groups.items()],
        "totals": {
            "assets": len(entries),
            "ready": sum(1 for e in entries if e["ready"]),
            "images": sum(1 for e in entries if e["winner"]),
            "models": sum(1 for e in entries if e["model"]),
        },
    }


def export_files(ws_dir: Path, project: str, paths: list[str] | None = None, history: bool = False) -> tuple[list[tuple[str, Path | bytes]], dict[str, Any]]:
    """The (archive name, source file or contents) pairs of an export, plus its manifest."""
    vault = list_vault(ws_dir)
    wanted = {p for p in paths or [] if p}
    entries = [e for g in vault["groups"] for e in g["assets"] if not wanted or e["path"] in wanted]
    if wanted and not entries:
        raise FileNotFoundError("None of those assets are in the vault yet.")
    root = f"{project}-vault"
    files: list[tuple[str, Path | bytes]] = []
    manifest: dict[str, Any] = {"project": project, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "assets": []}
    for entry in entries:
        base = "/".join(part for part in (root, entry["folder"], entry["name"]) if part)
        name = entry["name"]
        record: dict[str, Any] = {k: entry[k] for k in ("title", "kind", "prompt", "path")}
        record["folder"] = base[len(root) + 1:]
        record["files"] = {}
        if entry["winner"]:
            image = resolve_rel(ws_dir, entry["winner"])
            if image.is_file():
                files.append((f"{base}/{name}{image.suffix.lower()}", image))
                record["files"]["image"] = f"{name}{image.suffix.lower()}"
        if entry["model"]:
            for fmt, rel in entry["model"]["formats"].items():
                files.append((f"{base}/{name}.{fmt}", resolve_rel(ws_dir, rel)))
                record["files"][fmt] = f"{name}.{fmt}"
            if entry["model"].get("thumb"):
                files.append((f"{base}/{name}_thumb.png", resolve_rel(ws_dir, entry["model"]["thumb"])))
                record["files"]["thumbnail"] = f"{name}_thumb.png"
            record["model_settings"] = entry["model_settings"]
        if history:
            out = output_dir(resolve_rel(ws_dir, entry["path"]))
            for kind_dir in sorted(p for p in out.iterdir() if p.is_dir()) if out.is_dir() else []:
                for file in sorted(p for p in kind_dir.iterdir() if p.is_file()):
                    files.append((f"{base}/history/{kind_dir.name}/{file.name}", file))
        files.append((f"{base}/{name}.json", json.dumps(record, indent=2).encode("utf-8")))
        manifest["assets"].append(record)
    return files, manifest


def export_zip(ws_dir: Path, project: str, paths: list[str] | None = None, history: bool = False) -> bytes:
    files, manifest = export_files(ws_dir, project, paths, history)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{project}-vault/manifest.json", json.dumps(manifest, indent=2))
        for arcname, source in files:
            if isinstance(source, bytes):
                archive.writestr(arcname, source)
            elif source.is_file():
                archive.write(source, arcname)
    return buffer.getvalue()


def export_folder(ws_dir: Path, project: str, paths: list[str] | None = None, history: bool = False) -> str:
    """Write the export into `<project>/exports/<project>-vault/`, replacing the previous one."""
    files, manifest = export_files(ws_dir, project, paths, history)
    target_root = ws_dir.parent / EXPORT_DIR
    dest = target_root / f"{project}-vault"
    if dest.is_dir():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for arcname, source in files:
        target = target_root / arcname
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, bytes):
            target.write_bytes(source)
        elif source.is_file():
            shutil.copy2(source, target)
    return dest.resolve().as_posix()

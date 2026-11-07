"""FastAPI server for Forge (Section 5, 6, 7, 9, 14).

Binds to 127.0.0.1 by default, validates all file paths against traversal,
restricts asset proxy to allowed hosts, never echoes raw API keys,
and routes all UI and Agent actions through `ForgeActionLayer`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from html import escape as html_escape
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from forge import access, asset_studio, game_import, game_tools, level_pack, modular, overview as project_overview, showcase, updater, vault
from forge.agent.action_layer import ForgeActionLayer
from forge.agent.autonomous import execute_agent_brief
from forge.agent import cli_models, cli_runner, endpoints as model_endpoints
from forge.agent.llm import call_agent_model, list_agent_models
from forge.agent.workspace_agent import WorkspaceAgent
from forge.server.security import LocalOnlyGuard
from forge.config import get_config_dir, get_projects_root, load_config, save_config
from forge.mcp.server import MUTATING_TOOLS, ForgeMCPDispatcher, advertised_tools, default_project
from forge.models import JobStatus
from forge.providers.auth_manager import (
    get_all_auth_status,
    switch_codex_profile,
)
from forge.providers.base import ProviderError
from forge.project_scaffolder import scaffold_project, suggest_folders
from forge.providers.registry import REGISTRY
from forge.recipes.engine import list_recipes
from forge.storage.project_catalog import (
    assign_project_folder,
    create_project_folder,
    delete_project_folder,
    get_project_catalog,
    set_default_project,
)
from forge.storage.project_store import home_owner, list_all_projects, make_home, project_home, sanitize_project_name
from forge.storage import workspace_archive
from forge.stylepacks.builtins import VARIANT_CHIP_PRESETS, save_custom_style_pack
from forge.models import StylePack
from forge.watcher.folder_watcher import WatchFolderService
from forge.watcher.workspace_watcher import WorkspaceWatcher
from forge import game_link, service_gen, services
from forge import tripo as tripo_client
from forge.agent import connect as connect_agent


UI_DIR = Path(__file__).resolve().parent.parent / "ui"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLE_GODOT_DIR = REPO_ROOT / "sample_godot_game"


class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


ws_manager = ConnectionManager()
actions = ForgeActionLayer(broadcast_cb=ws_manager.broadcast)


async def _agent_model_call(**kwargs: Any) -> dict[str, Any]:
    if not kwargs.get("model_selector"):
        kwargs["model_selector"] = load_config().agent_model or None
    return await call_agent_model(**kwargs)


workspace_agent = WorkspaceAgent(actions, _agent_model_call)
mcp_dispatcher = ForgeMCPDispatcher(action_layer=actions, workspace_agent=workspace_agent, in_app=True)


async def workspace_changed(project: str) -> None:
    await ws_manager.broadcast({"type": "workspace_changed", "project": project})
    try:
        await asyncio.to_thread(write_asset_index, project)
    except Exception:  # noqa: BLE001 — the index must never break the edit that triggered it
        pass


def write_asset_index(project: str) -> None:
    """Keep glob/ASSETS.md, the list the game's coding agent copies finished art from, up to date."""
    from forge import asset_index

    store = actions.get_store(project)
    asset_index.write(_ws_dir(project), store.get_bible().display_name or project)


workspace_watcher = WorkspaceWatcher(lambda project: actions.get_workspace(project).ws_dir, workspace_changed)


async def _watcher_run_verb(
    project: str, verb: str, parent_ids: list[str], inputs: dict[str, Any], initiated_by: str
) -> dict[str, Any]:
    return await actions.run_verb(
        verb=verb,
        parent_asset_ids=parent_ids,
        inputs=inputs,
        project=project,
        initiated_by=initiated_by,
        agent_reason="Triggered by Watch Folder",
    )


watcher_service = WatchFolderService(get_store_fn=actions.get_store, run_verb_fn=_watcher_run_verb)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure default project & starter lineage tree exist (never auto-run provider jobs on startup!)
    default_store = actions.get_store("default")
    for job in default_store.list_jobs(limit=20):
        if job.status in (JobStatus.RUNNING, JobStatus.QUEUED):
            job.status = JobStatus.CANCELED
            job.step_label = "Interrupted by server restart (not re-run to protect credits)"
            default_store.save_job(job)
    for linked in game_link.linked_projects():
        try:
            connect_agent.refresh(linked)  # new prompt or app location after an update
        except (OSError, ValueError):
            pass
    watch_task = asyncio.create_task(watcher_service.loop(3.0))
    workspace_task = asyncio.create_task(workspace_watcher.loop(2.0))
    yield
    watcher_service.stop()
    workspace_watcher.stop()
    watch_task.cancel()
    workspace_task.cancel()


app = FastAPI(
    title="Forge Asset Maker API",
    version="1.0.0",
    description="Playful, lightweight, agent-drivable asset maker for game prototyping",
    lifespan=lifespan,
)

# The UI is served from this same origin, so no CORS is granted; the guard refuses
# cross-site and DNS-rebinding requests (see forge/server/security.py).
app.add_middleware(LocalOnlyGuard, bind_host=os.environ.get("FORGE_BIND_HOST") or load_config().host)

# Server-level settings that the HTTP API may not change (edit ~/.forge/config.json or use CLI flags).
LOCKED_CONFIG_KEYS = {"host", "port", "allowed_proxy_hosts", "access_folders", "projects_dir"}


# --- Request Payloads ---

class RunVerbRequest(BaseModel):
    verb: str
    parent_asset_ids: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    project: str = "default"
    initiated_by: str = "user"
    reason: str = ""
    dry_run: bool = False
    force_uncached: bool = False


class RateAssetRequest(BaseModel):
    asset_id: str
    rating: str
    note: str | None = None
    project: str = "default"


class VariantPreviewRequest(BaseModel):
    parent_asset_id: str | None = None
    chips: list[str] = Field(default_factory=lambda: ["Rarity tiers"])
    prompt: str = ""
    count: int = 4
    project: str = "default"


class CreateRecipeRequest(BaseModel):
    recipe_id: str
    yaml_content: str
    fmt: str = "yaml"


class ExportRequest(BaseModel):
    engine: str = "godot"
    asset_ids: list[str] | None = None
    formats: list[str] | None = None
    project: str = "default"
    layout: str = "flat"


class GameScanRequest(BaseModel):
    game_path: str
    consent_granted: bool = True
    lock_into_bible: bool = False
    project: str = "default"


class ProposeCatalogRequest(BaseModel):
    item_id: str
    project: str = "default"


class WritebackRequest(BaseModel):
    asset_ids: list[str] | None = None
    project: str = "default"


class AgentBriefRequest(BaseModel):
    brief: str
    parent_asset_id: str | None = None
    project: str = "default"
    dry_run: bool = False
    approve_checkpoint: bool = False


class UrlIngestRequest(BaseModel):
    url: str
    name: str | None = None
    project: str = "default"


# --- API Endpoints ---

@app.get("/api/health")
def api_health() -> dict[str, Any]:
    # The MCP bridge checks projects_dir so it only forwards to a Glob app that serves the same projects.
    from forge import __version__

    return {"ok": True, "version": __version__, "projects_dir": str((actions.root_dir or get_projects_root()).resolve()), "background": _background()}


def _background() -> bool:
    """Started by a coding agent (forge/launcher.py): no window or terminal to close it with."""
    return os.environ.get("FORGE_BACKGROUND") == "1"


@app.post("/api/quit")
async def api_quit() -> dict[str, Any]:
    if not _background():
        raise HTTPException(status_code=400, detail="Close Glob where you started it (its window or terminal).")
    asyncio.get_running_loop().call_later(0.4, os._exit, 0)
    return {"stopping": True}


@app.get("/api/bootstrap")
def api_bootstrap(project: str = "default") -> dict[str, Any]:
    store = actions.get_store(project)
    cfg = load_config()
    asset_bundle = actions.list_assets(project=project)
    prof = store.get_game_profile()
    workspace = actions.list_workspace_docs(project=project)
    workspace["folders"] = actions.get_workspace(project).list_folders()
    return {
        "project": store.project_name,
        **get_project_catalog(actions.root_dir),
        "bible": store.get_bible().model_dump(),
        "assets": asset_bundle["assets"],
        "lineage": asset_bundle["lineage"],
        "recipes": list_recipes(),
        "style_packs": actions.list_style_packs()["style_packs"],
        "variant_chips": VARIANT_CHIP_PRESETS,
        "queue": actions.queue_status(project=project),
        "budget": actions.get_budget(project=project),
        "activity": [a.model_dump() for a in store.list_activity(limit=60)],
        "game_profile": prof.model_dump() if prof else None,
        "workspace": workspace,
        "pipeline": actions.get_pipeline_state(project=project),
        "project_cards": actions.list_project_cards(),
        "sample_godot_path": str(SAMPLE_GODOT_DIR) if SAMPLE_GODOT_DIR.exists() else "sample_godot_game",
        "providers": REGISTRY.list_providers(),
        "mcp_tools_count": len(advertised_tools()),
        "config": cfg.masked_dict(),
        "auth": get_all_auth_status(),
    }


@app.get("/api/projects")
def api_list_projects() -> dict[str, Any]:
    return get_project_catalog(actions.root_dir)


@app.get("/api/projects/cards")
def api_project_cards() -> dict[str, Any]:
    names = list_all_projects(actions.root_dir)
    root = actions.root_dir or get_projects_root()
    return {"projects": [project_overview.project_card(project_home(name, root) / "workspace", name) for name in names]}


@app.post("/api/projects/default")
def api_set_default_project(payload: dict[str, Any]) -> dict[str, Any]:
    project = payload.get("project")
    if not isinstance(project, str):
        raise HTTPException(status_code=400, detail="Project name must be a string.")
    try:
        return set_default_project(project, actions.root_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/folders")
def api_create_project_folder(payload: dict[str, Any]) -> dict[str, Any]:
    name = payload.get("name")
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="Folder name must be a string.")
    try:
        return create_project_folder(name, actions.root_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/projects/folders/{folder}")
def api_delete_project_folder(folder: str) -> dict[str, Any]:
    try:
        return delete_project_folder(folder, actions.root_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/folders/assign")
def api_assign_project_folder(payload: dict[str, Any]) -> dict[str, Any]:
    projects = payload.get("projects")
    folder = payload.get("folder")
    if not isinstance(projects, list) or not all(isinstance(project, str) for project in projects):
        raise HTTPException(status_code=400, detail="Projects must be a list of project names.")
    if folder is not None and not isinstance(folder, str):
        raise HTTPException(status_code=400, detail="Folder must be a name or null.")
    try:
        return assign_project_folder(projects, folder, actions.root_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/projects")
def api_bulk_delete_projects(payload: dict[str, Any]) -> dict[str, Any]:
    projects = payload.get("projects")
    if not isinstance(projects, list) or not all(isinstance(project, str) for project in projects):
        raise HTTPException(status_code=400, detail="Projects must be a list of project names.")
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    for project in dict.fromkeys(projects):
        try:
            actions.delete_project(project)
            deleted.append(project)
        except (FileNotFoundError, ValueError) as exc:
            failed.append({"project": project, "error": str(exc)})
    return {**get_project_catalog(actions.root_dir), "deleted": deleted, "failed": failed}


@app.delete("/api/projects/{project}")
def api_delete_project(project: str) -> dict[str, Any]:
    try:
        return actions.delete_project(project)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects")
async def api_create_project(payload: dict[str, Any]) -> dict[str, Any]:
    raw_name: Any = payload.get("name")
    if raw_name is None:
        raw_name = payload.get("project")
    if raw_name is None:
        raw_name = payload.get("project_name")
    if raw_name is None:
        raw_name = "new_project"
    if not isinstance(raw_name, str):
        raise HTTPException(status_code=400, detail="Project name must be a string.")
    raw_name = raw_name.strip()
    if not raw_name:
        raise HTTPException(status_code=400, detail="Project name cannot be empty.")
    name = sanitize_project_name(raw_name)
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Project name is invalid.")
    game = Path(str(payload.get("game_folder") or "")).expanduser() if payload.get("game_folder") else None
    if game is not None and game.is_absolute() and not game.exists() and payload.get("create_game_folder"):
        try:
            game.mkdir(parents=True)  # a typed path to a game that has no folder yet
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Could not make the folder {game}: {exc.strerror or exc}") from exc
    if game is not None and not (game.is_absolute() and game.is_dir()):
        raise HTTPException(status_code=400, detail="Choose your game's folder.")
    # Optional: keep Glob's files somewhere other than the game's glob/ folder (say, in Documents).
    home_folder = Path(str(payload.get("home_folder"))).expanduser() if payload.get("home_folder") and game else None
    if home_folder is not None and not (home_folder.is_absolute() and home_folder.is_dir()):
        raise HTTPException(status_code=400, detail="Choose an existing folder for Glob's files.")
    existing = home_owner(game, actions.root_dir) if game else None
    if existing:
        # The game already has a Glob project: open that one instead of starting a second.
        name = existing
    elif payload.get("unique"):
        # New projects from the UI never open an existing one by accident.
        base, root, index = name, actions.root_dir or get_projects_root(), 2
        while (root / name).exists():
            name, index = f"{base}-{index}", index + 1
    try:
        if game and not existing:
            make_home(name, game, actions.root_dir, home_folder)
        store = actions.get_store(name)
        if game and not existing:
            # Choosing the game in Glob is the user's consent: link it and tell its coding agents about Glob.
            await asyncio.to_thread(connect_agent.connect, name, str(game), None)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "project": store.project_name,
        "existing": bool(existing),
        "home": str(store.project_dir),
        "bible": store.get_bible().model_dump(),
        "projects": list_all_projects(actions.root_dir),
    }


@app.post("/api/projects/suggest-folders")
async def api_suggest_project_folders(payload: dict[str, Any]) -> dict[str, Any]:
    return {"folders": await suggest_folders(str(payload.get("pitch") or ""), payload.get("existing") or [], _agent_model_call)}


@app.post("/api/projects/scaffold")
async def api_scaffold_project(payload: dict[str, Any]) -> dict[str, Any]:
    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise HTTPException(status_code=400, detail="Project name cannot be empty.")
    name = sanitize_project_name(raw_name.strip())
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Project name is invalid.")
    projects_base = (actions.root_dir or get_projects_root()).resolve()
    if (projects_base / name).exists():
        raise HTTPException(status_code=409, detail=f"A project named '{name}' already exists.")
    try:
        result = await scaffold_project(actions, name, payload, _agent_model_call)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=f"Workspace scaffolding failed: {exc}") from exc
    welcome = (
        f"I scaffolded **{name.replace('_', ' ').title()}** for {result['engine']} ({result['dimension']}). "
        f"I set up {len(result['folders'])} folders and {result['blueprint_count']} starter concepts; "
        f"the asset queue contains {len(result['requirements'])} items. "
        f"{'I used a deterministic draft because the configured model was unavailable. ' if result['fallback'] else ''}"
        "Before your first generation: **1)** Which visual reference should dominate the silhouettes and materials? "
        "**2)** What lighting mood and contrast should stay consistent across the world? "
        "**3)** Which lore detail or gameplay constraint must every concept preserve?"
    )
    session = workspace_agent.create_session(
        name, title="Workspace kickoff", group="General",
        messages=[{"role": "agent", "text": welcome, "at": time.time()}],
    )
    return {**result, "agent_session": session["id"]}


@app.get("/api/assets")
def api_list_assets(
    project: str = "default",
    asset_type: str | None = None,
    status: str | None = None,
    approval_state: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    return actions.list_assets(
        project=project,
        asset_type=asset_type,
        status=status,
        approval_state=approval_state,
        tag=tag,
    )


@app.get("/api/assets/{asset_id}")
def api_get_asset(asset_id: str, project: str = "default") -> dict[str, Any]:
    try:
        return actions.get_asset(asset_id, project=project)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/assets/{asset_id}")
def api_delete_asset(asset_id: str, project: str = "default") -> dict[str, Any]:
    try:
        return actions.delete_asset(asset_id, project=project)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/upload")
async def api_upload_file(
    file: UploadFile = File(...),
    project: str = Form("default"),
    name: str | None = Form(None),
    note: str = Form(""),
) -> dict[str, Any]:
    store = actions.get_store(project)
    content = await file.read()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Uploaded file exceeds 25 MB limit.")
    asset = store.ingest_uploaded_file(
        filename=file.filename or "upload.png",
        content=content,
        name=name,
        note=note,
    )
    await ws_manager.broadcast({"type": "asset_uploaded", "project": project, "asset": asset.model_dump()})
    return {"asset": asset.model_dump()}


@app.post("/api/upload-url")
async def api_upload_url(req: UrlIngestRequest) -> dict[str, Any]:
    parsed = urlparse(req.url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http/https URLs are supported.")
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(req.url)
        resp.raise_for_status()
        fname = Path(parsed.path).name or "url_asset.png"
        store = actions.get_store(req.project)
        asset = store.ingest_uploaded_file(filename=fname, content=resp.content, name=req.name)
        return {"asset": asset.model_dump()}


@app.post("/api/rate")
async def api_rate_asset(req: RateAssetRequest) -> dict[str, Any]:
    try:
        return await actions.rate_asset(
            asset_id=req.asset_id,
            rating=req.rating,
            note=req.note,
            project=req.project,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/variant/preview")
def api_variant_preview(req: VariantPreviewRequest) -> dict[str, Any]:
    return actions.preview_variant_prompts(
        parent_asset_id=req.parent_asset_id,
        chips=req.chips,
        prompt=req.prompt,
        count=req.count,
        project=req.project,
    )


@app.post("/api/verbs/run")
async def api_run_verb(req: RunVerbRequest) -> dict[str, Any]:
    try:
        return await actions.run_verb(
            verb=req.verb,
            parent_asset_ids=req.parent_asset_ids,
            inputs=req.inputs,
            project=req.project,
            initiated_by=req.initiated_by,
            agent_reason=req.reason,
            dry_run=req.dry_run,
            force_uncached=req.force_uncached,
        )
    except ProviderError as p_err:
        raise HTTPException(status_code=p_err.status_code, detail=p_err.to_dict()) from p_err
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "how_to_fix": "Check recipe parameters or enable Demo Mode."}) from exc


@app.post("/api/recipes/run")
async def api_run_recipe(req: RunVerbRequest) -> dict[str, Any]:
    return await api_run_verb(req)


@app.get("/api/recipes")
def api_list_recipes() -> dict[str, Any]:
    return {"recipes": list_recipes()}


@app.post("/api/recipes")
def api_create_recipe(req: CreateRecipeRequest) -> dict[str, Any]:
    try:
        recipe = actions.create_recipe(req.recipe_id, req.yaml_content, fmt=req.fmt)
        return {"recipe": recipe}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/style-packs")
def api_list_style_packs() -> dict[str, Any]:
    return actions.list_style_packs()


@app.post("/api/style-packs")
def api_create_style_pack(payload: dict[str, Any]) -> dict[str, Any]:
    pack = StylePack(**payload, builtin=False)
    saved = save_custom_style_pack(pack)
    return {"style_pack": saved.model_dump()}


@app.get("/api/bible")
def api_get_bible(project: str = "default") -> dict[str, Any]:
    return actions.get_project_bible(project=project)


@app.put("/api/bible")
def api_update_bible(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    return actions.update_project_bible(payload, project=project)


@app.get("/api/queue")
def api_queue_status(project: str = "default") -> dict[str, Any]:
    return actions.queue_status(project=project)


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str, project: str = "default") -> dict[str, Any]:
    executor = actions.get_executor(project)
    executor.cancel_job(job_id)
    store = actions.get_store(project)
    job = store.get_job(job_id)
    if job:
        job.status = JobStatus.CANCELED
        job.step_label = "Canceled by user"
        store.save_job(job)
    return {"canceled": True, "job_id": job_id}


@app.get("/api/budget")
def api_get_budget(project: str = "default") -> dict[str, Any]:
    return actions.get_budget(project=project)


@app.get("/api/activity")
def api_get_activity(project: str = "default") -> dict[str, Any]:
    store = actions.get_store(project)
    return {"activity": [a.model_dump() for a in store.list_activity(limit=80)]}


@app.post("/api/undo")
def api_undo(payload: dict[str, Any] | None = None, project: str = "default") -> dict[str, Any]:
    act_id = (payload or {}).get("activity_id")
    return actions.undo_action(activity_id=act_id, project=project)


@app.post("/api/export")
def api_export(req: ExportRequest) -> dict[str, Any]:
    return actions.export(
        engine=req.engine,
        asset_ids=req.asset_ids,
        formats=req.formats,
        project=req.project,
        layout=req.layout,
    )


@app.post("/api/watch/trigger")
async def api_watch_trigger(project: str = "default") -> dict[str, Any]:
    processed = await watcher_service.scan_once(project)
    return {"processed_count": len(processed), "processed": processed}


# --- Game Scan Endpoints (Section 14) ---

@app.post("/api/gamescan/consent")
def api_gamescan_consent(payload: dict[str, Any]) -> dict[str, Any]:
    game_path = str(payload.get("game_path") or SAMPLE_GODOT_DIR)
    try:
        return actions.preview_game_scan_consent(game_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/gamescan/scan")
def api_gamescan_scan(req: GameScanRequest) -> dict[str, Any]:
    try:
        return actions.scan_game_project(
            game_path=req.game_path,
            consent_granted=req.consent_granted,
            lock_into_bible=req.lock_into_bible,
            project=req.project,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/gamescan/profile")
def api_gamescan_profile(project: str = "default") -> dict[str, Any]:
    return actions.get_game_profile(project=project)


@app.put("/api/gamescan/profile")
def api_gamescan_update_profile(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    approve = bool(payload.pop("approve_and_lock", True))
    return actions.update_game_profile(payload, approve_and_lock=approve, project=project)


@app.get("/api/gamescan/wishlist")
def api_gamescan_wishlist(project: str = "default") -> dict[str, Any]:
    return actions.list_asset_wishlist(project=project)


@app.get("/api/gamescan/placeholders")
def api_gamescan_placeholders(project: str = "default") -> dict[str, Any]:
    return actions.find_placeholders(project=project)


@app.post("/api/gamescan/propose")
def api_gamescan_propose(req: ProposeCatalogRequest) -> dict[str, Any]:
    try:
        return actions.propose_assets_for(req.item_id, project=req.project)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/gamescan/writeback/preview")
def api_writeback_preview(req: WritebackRequest) -> dict[str, Any]:
    try:
        return actions.writeback_preview(asset_ids=req.asset_ids, project=req.project)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/gamescan/writeback/apply")
def api_writeback_apply(req: WritebackRequest) -> dict[str, Any]:
    try:
        return actions.writeback_apply(asset_ids=req.asset_ids, project=req.project)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/gamescan/writeback/undo")
def api_writeback_undo(project: str = "default") -> dict[str, Any]:
    return actions.writeback_undo(project=project)


# --- Markdown Workspace & Coding Agent Collaboration Endpoints ---

@app.get("/api/workspace")
def api_list_workspace(project: str = "default") -> dict[str, Any]:
    workspace = actions.list_workspace_docs(project=project)
    workspace["folders"] = actions.get_workspace(project).list_folders()
    return workspace


def _skill_summary(path: Path, source: str) -> dict[str, Any]:
    name = path.parent.name if path.name.upper() == "SKILL.MD" else path.stem
    description = ""
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        head = ""
    if head.startswith("---"):
        for line in head.split("\n")[1:]:
            if line.strip() == "---":
                break
            key, _, value = line.partition(":")
            if key.strip() == "name" and value.strip():
                name = value.strip().strip("\"'")
            elif key.strip() == "description" and value.strip():
                description = value.strip().strip("\"'")
    if not description:
        description = next(
            (line.strip("# ").strip() for line in head.splitlines() if line.strip() and not line.startswith("---")),
            "",
        )
    return {"name": name, "description": description[:280], "path": str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else path.name, "source": source}


@app.get("/api/workspace/inspector")
def api_workspace_inspector(project: str = "default") -> dict[str, Any]:
    """Live data for the right-hand Files / Skills / MCP inspector."""
    workspace = actions.get_workspace(project)
    skills: list[dict[str, Any]] = []
    for skills_root, source in ((REPO_ROOT / ".claude" / "skills", "Claude Code"), (REPO_ROOT / ".agents" / "skills", "Agents")):
        if skills_root.is_dir():
            skills.extend(_skill_summary(p, source) for p in sorted(skills_root.glob("*/SKILL.md")))
    processes_dir = workspace.ws_dir / "processes"
    if processes_dir.is_dir():
        for process in sorted(processes_dir.rglob("*.md")):
            entry = _skill_summary(process, "Workspace process")
            entry["path"] = f"workspace/{process.relative_to(workspace.ws_dir).as_posix()}"
            skills.append(entry)
    mcp_config = REPO_ROOT / ".mcp.json"
    return {
        "project": project,
        "tree": workspace.list_tree(),
        "skills": skills,
        "mcp": {
            "server": "forge",
            "stdio_command": "uv run forge --mcp",
            "http_endpoint": "/api/mcp",
            "config_file": ".mcp.json" if mcp_config.is_file() else None,
            "tools": [tool.get("name") for tool in advertised_tools()],
        },
    }


@app.get("/api/workspace/doc")
def api_read_workspace_doc(name: str = Query("GAME.md"), project: str = "default") -> dict[str, Any]:
    try:
        return actions.read_workspace_doc(name, project=project)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/workspace/doc")
def api_write_workspace_doc(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    doc_name = str(payload.get("name", "GAME.md"))
    content = str(payload.get("content", ""))
    actor = str(payload.get("actor", "user"))
    try:
        return actions.write_workspace_doc(doc_name, content, project=project, actor=actor)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/workspace/requirements")
def api_list_requirements(project: str = "default") -> dict[str, Any]:
    return actions.list_asset_requirements(project=project)


@app.get("/api/workspace/agent-requests")
def api_list_agent_requests(project: str = "default") -> dict[str, Any]:
    return actions.list_agent_requests(project=project)


@app.post("/api/workspace/agent-requests/{request_id}/fulfill")
def api_fulfill_agent_request(request_id: str, project: str = "default") -> dict[str, Any]:
    try:
        return actions.fulfill_agent_request(request_id=request_id, project=project)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/workspace/blueprint/propose")
async def api_propose_blueprint(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    doc_name = str(payload.get("doc_name", ""))
    instruction = str(payload.get("instruction", "")).strip()
    if not doc_name or not instruction:
        raise HTTPException(status_code=400, detail="doc_name and instruction are required.")
    try:
        return await actions.propose_blueprint_update(doc_name, instruction, payload.get("context"), project)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.put("/api/workspace/blueprint")
def api_update_blueprint(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    doc_name = str(payload.get("doc_name", ""))
    patch = payload.get("metadata_patch")
    if not doc_name or not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="doc_name and metadata_patch are required.")
    try:
        return actions.write_blueprint_metadata(
            doc_name, patch, project=project, actor=str(payload.get("actor", "user"))
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/workspace/requirements")
def api_add_requirement(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    return actions.add_asset_requirement(
        req_id=str(payload.get("req_id", "new_item")),
        kind=str(payload.get("kind", "3D Mesh")),
        target_path=str(payload.get("target_path", "")),
        description=str(payload.get("description", "")),
        project=project,
        actor=str(payload.get("actor", "user")),
    )


@app.put("/api/workspace/requirements/{req_id}")
def api_update_requirement(req_id: str, payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    return actions.update_asset_requirement(
        req_id=req_id,
        status=str(payload.get("status", "done")),
        linked_asset_id=str(payload.get("linked_asset_id", "")),
        project=project,
    )


@app.post("/api/workspace/requirements/fulfill")
async def api_fulfill_requirements(payload: dict[str, Any] | None = None, project: str = "default") -> dict[str, Any]:
    body = payload or {}
    return await actions.fulfill_asset_requirements(
        req_ids=body.get("req_ids"),
        max_items=int(body.get("max_items", 6)),
        project=project,
        actor=str(body.get("actor", "agent")),
    )


@app.post("/api/workspace/handoffs")
def api_post_handoff(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    return actions.post_agent_handoff(
        title=str(payload.get("title", "Agent Update")),
        message=str(payload.get("message", "")),
        author=str(payload.get("author", "coding_agent")),
        project=project,
    )


@app.post("/api/workspace/study-section")
def api_append_study_section(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    return actions.append_study_section(
        doc_name=str(payload.get("doc_name", "VERSION_2_CHARACTER_ARSENAL.md")),
        title=str(payload.get("title", "Node Pipeline Study")),
        badge=str(payload.get("badge", "Node Output")),
        prompt=str(payload.get("prompt", "")),
        asset_ids=list(payload.get("asset_ids") or []),
        twist=str(payload.get("twist", "")),
        rig_plan=str(payload.get("rig_plan", "")),
        project=project,
    )


# --- Step-by-Step Game World & Asset Pipeline Endpoints ---

@app.get("/api/pipeline")
def api_get_pipeline(project: str = "default") -> dict[str, Any]:
    return actions.get_pipeline_state(project=project)


def _folder_path_from_payload(payload: dict[str, Any]) -> str:
    path = payload.get("path") or payload.get("folder_path")
    if path:
        return str(path)

    parent = payload.get("parent") or payload.get("parent_path") or payload.get("category") or payload.get("folder")
    name = payload.get("name") or payload.get("folder_name") or payload.get("subfolder")
    if parent and name:
        return f"{parent}/{name}"
    if payload.get("category") and payload.get("folder"):
        return f"{payload['category']}/{payload['folder']}"
    if payload.get("folder"):
        return str(payload["folder"])
    raise ValueError("Provide a category folder path or both parent and name.")


def _project_from_payload(payload: dict[str, Any], query_project: str | None) -> str:
    """Resolve an optional query project before body aliases, then use the default."""
    if query_project is not None:
        return query_project
    body_project = payload.get("project")
    if body_project is None:
        body_project = payload.get("project_name")
    if body_project is None:
        return "default"
    if not isinstance(body_project, str) or not body_project.strip():
        raise ValueError("Project name must be a non-empty string.")
    return body_project.strip()


@app.get("/api/pipeline/folders")
@app.get("/api/pipeline/folder")
def api_list_pipeline_folders(project: str = "default") -> dict[str, Any]:
    pm = actions.get_pipeline(project)
    tree = pm.get_file_explorer_tree()
    return {
        "project": pm.store.project_name,
        "folders": tree["folder_paths"],
        "folder_tree": tree["folder_tree"],
        "file_tree": tree,
    }


@app.post("/api/pipeline/folders")
@app.post("/api/pipeline/folder")
def api_create_pipeline_folder(payload: dict[str, Any], project: str | None = None) -> dict[str, Any]:
    try:
        folder_path = _folder_path_from_payload(payload)
        project = _project_from_payload(payload, project)
        pm = actions.get_pipeline(project)
        folder = pm.create_folder(folder_path, actor=str(payload.get("actor", "user")))
        tree = pm.get_file_explorer_tree()
        return {
            "project": pm.store.project_name,
            "folder": folder,
            "folders": tree["folder_paths"],
            "folder_tree": tree["folder_tree"],
            "file_tree": tree,
        }
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/pipeline/folders")
def api_delete_pipeline_folder(
    path: str = Query(...),
    project: str = "default",
    confirm: bool = False,
) -> dict[str, Any]:
    from forge.pipeline_manager import normalize_category_folder_path

    try:
        if confirm is not True:
            raise ValueError("Folder deletion requires explicit confirmation.")
        normalized = normalize_category_folder_path(path, allow_root=False)
        pm = actions.get_pipeline(project)
        folder_path = pm.ws_dir / Path(*normalized.split("/"))
        workspace_root = pm.ws_dir.resolve()
        if not folder_path.resolve().is_relative_to(workspace_root):
            raise ValueError("Folder path must stay inside the project workspace.")
        cursor = folder_path
        while cursor != pm.ws_dir.parent:
            if cursor.is_symlink():
                raise ValueError("Folders containing symbolic links cannot be deleted.")
            cursor = cursor.parent
        if not folder_path.is_dir():
            raise FileNotFoundError(f"Folder '{normalized}' was not found.")

        items = pm.list_pipeline_items()
        folder_items = [
            item for item in items
            if item.full_folder_path == normalized or item.full_folder_path.startswith(f"{normalized}/")
        ]
        allowed_markdown = {
            (pm.ws_dir / item.full_folder_path / f"{item.slug}.md").resolve()
            for item in folder_items
        }
        candidates = [folder_path, *folder_path.rglob("*")]
        for candidate in candidates:
            if candidate.is_symlink():
                raise ValueError("Folders containing symbolic links cannot be deleted.")
            if candidate.is_file() and candidate.name != "_folder.md" and candidate.resolve() not in allowed_markdown:
                raise ValueError("Folder contains files outside its blueprint records; move them before deleting.")

        deletion_results = [
            actions.delete_pipeline_item(
                item_id=item.id,
                project=project,
                confirm=True,
                delete_generated_assets=False,
            )
            for item in folder_items
        ]
        directories = [folder_path, *(entry for entry in folder_path.rglob("*") if entry.is_dir())]
        for directory in sorted(directories, key=lambda entry: len(entry.parts), reverse=True):
            marker = directory / "_folder.md"
            if marker.is_file():
                marker.unlink()
            directory.rmdir()
        return {
            "project": pm.store.project_name,
            "deleted_folder": normalized,
            "deleted_blueprint_ids": [item.id for item in folder_items],
            "preserved_asset_ids": sorted({
                asset_id
                for result in deletion_results
                for asset_id in result.get("preserved_asset_ids", [])
            }),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/workspace/folders")
@app.get("/api/workspace/folder")
def api_list_workspace_folders(project: str = "default") -> dict[str, Any]:
    ws = actions.get_workspace(project)
    return {"project": ws.store.project_name, "folders": ws.list_folders()}


@app.post("/api/workspace/folders")
@app.post("/api/workspace/folder")
def api_create_workspace_folder(payload: dict[str, Any], project: str | None = None) -> dict[str, Any]:
    try:
        folder_path = _folder_path_from_payload(payload)
        project = _project_from_payload(payload, project)
        ws = actions.get_workspace(project)
        folder = ws.create_folder(folder_path, actor=str(payload.get("actor", "user")))
        return {"project": ws.store.project_name, "folder": folder, "folders": ws.list_folders()}
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/steer")
async def api_steer_world(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    instruction = str(payload.get("instruction", ""))
    create_new = bool(payload.get("create_new_items", True))
    attached_images = payload.get("attached_images")
    return await actions.steer_world_pipeline(
        instruction, create_new_items=create_new, attached_images=attached_images, project=project
    )


def _native_dialog_macos(kind: str, title: str) -> str:
    # Tk must be created on the main thread on macOS (newer releases abort otherwise),
    # so use the native picker in a separate process instead.
    prompt = title.replace("\\", "\\\\").replace('"', '\\"')
    verb = {"folder": "choose folder", "save": "choose file name"}.get(kind, "choose file")
    script = f'POSIX path of ({verb} with prompt "{prompt}")'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        if "-128" in result.stderr:  # user cancelled
            return ""
        raise RuntimeError(result.stderr.strip() or "osascript failed")
    return result.stdout.strip()


def _native_dialog(kind: str, title: str, **options: Any) -> str:
    """A native folder/open/save dialog on the user's own screen (Glob only listens on 127.0.0.1)."""
    if sys.platform == "darwin":
        return _native_dialog_macos(kind, title)
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError("The local file picker is unavailable. Enter the path manually.") from exc

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        if kind == "folder":
            return filedialog.askdirectory(parent=root, title=title, mustexist=True) or ""
        if kind == "save":
            return filedialog.asksaveasfilename(parent=root, title=title, **options) or ""
        return filedialog.askopenfilename(parent=root, title=title, **options) or ""
    finally:
        root.destroy()


def _choose_local_game_folder() -> str:
    return _native_dialog("folder", "Choose a game folder")


@app.post("/api/system/choose-game-folder")
async def api_choose_game_folder() -> dict[str, str]:
    try:
        folder_path = await asyncio.to_thread(_choose_local_game_folder)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="The local folder picker could not open. Enter the folder path manually instead.",
        ) from exc
    return {"folder_path": folder_path}


@app.post("/api/pipeline/import-game")
async def api_import_game_folder(payload: dict[str, Any]) -> dict[str, Any]:
    folder_path = str(payload.get("folder_path") or SAMPLE_GODOT_DIR)
    project_name = payload.get("project_name")
    try:
        return await actions.import_existing_game(folder_path, project_name=project_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- Updates from GitHub releases ---

@app.get("/api/update")
async def api_update_check(force: bool = False) -> dict[str, Any]:
    return await updater.check(force)


@app.post("/api/update/install")
async def api_update_install() -> dict[str, Any]:
    if updater.state["status"] not in ("downloading", "installing"):
        asyncio.create_task(updater.install())
        await asyncio.sleep(0)  # let the download start before answering
    return dict(updater.state)


@app.get("/api/update/state")
def api_update_state() -> dict[str, Any]:
    return dict(updater.state)


@app.post("/api/system/choose-folder")
async def api_choose_folder(payload: dict[str, Any]) -> dict[str, str]:
    try:
        folder = await asyncio.to_thread(_native_dialog, "folder", str(payload.get("title") or "Choose a folder"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="The folder picker could not open. Paste the folder path instead.") from exc
    return {"folder_path": folder}


# --- Connected game: the coding agent in the user's game repo talks to Glob over MCP ---

@app.get("/api/connect")
def api_connect_status(project: str = "default", folder: str = "") -> dict[str, Any]:
    status = connect_agent.status(project)
    if folder and Path(folder).expanduser().is_dir():
        status["suggested"] = connect_agent.detect_targets(Path(folder).expanduser())
    return status


@app.post("/api/connect")
async def api_connect(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    try:
        return await asyncio.to_thread(connect_agent.connect, project, str(payload.get("folder") or ""), list(payload.get("targets") or []) or None)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/connect")
async def api_disconnect(project: str = "default") -> dict[str, Any]:
    return await asyncio.to_thread(connect_agent.disconnect, project)


# --- Generation services the user signs in to (hosted MCP + OAuth), no API key ---

@app.get("/api/services")
async def api_services() -> dict[str, Any]:
    return {"services": await asyncio.to_thread(services.status)}


@app.post("/api/services/{service}/connect")
async def api_service_connect(service: str, request: Request) -> dict[str, Any]:
    # The browser comes back to this same Glob server once the user has signed in.
    redirect_uri = str(request.base_url).rstrip("/").replace("://localhost", "://127.0.0.1") + services.CALLBACK_PATH
    try:
        return {"authorize_url": await asyncio.to_thread(services.start_sign_in, service, redirect_uri)}
    except (services.ServiceError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/services/callback", response_class=HTMLResponse)
async def api_service_callback(state: str = "", code: str = "", error: str = "", error_description: str = "") -> HTMLResponse:
    def page(title: str, text: str, ok: bool) -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Glob</title><body style=\"margin:0;display:grid;place-items:center;height:100vh;"
            "background:#070708;color:#f4f4f5;font:16px system-ui\"><div style=\"text-align:center;max-width:420px\">"
            f"<div style=\"font-size:46px\">{'🤜🤛' if ok else '⚠️'}</div><h1 style=\"font-size:24px\">{html_escape(title)}</h1>"
            f"<p style=\"color:#a1a1aa\">{html_escape(text)}</p></div>{'<script>setTimeout(()=>window.close(),1800)</script>' if ok else ''}",
            status_code=200 if ok else 400)

    if error or not code:
        return page("Sign-in cancelled", error_description or error or "No code came back. Try again from Glob.", False)
    try:
        service = await asyncio.to_thread(services.finish_sign_in, state, code)
    except (services.ServiceError, httpx.HTTPError) as exc:
        return page("Sign-in failed", str(exc), False)
    label = services.SERVICES[service]["label"]
    await ws_manager.broadcast({"type": "service_connected", "service": service})
    return page(f"{label} is connected", "Glob can use it now. You can close this tab and go back to Glob.", True)


@app.delete("/api/services/{service}")
def api_service_disconnect(service: str) -> dict[str, Any]:
    services.disconnect(service)
    return {"services": services.status()}


@app.post("/api/services/{service}/test")
async def api_service_test(service: str) -> dict[str, Any]:
    try:
        tools = await asyncio.to_thread(services.list_tools, service, True)
    except (services.ServiceError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"tools": [t.get("name") for t in tools]}


# --- Folder access: what Glob's agent may read (and write) outside the projects folder ---

@app.get("/api/access")
def api_access_list() -> dict[str, Any]:
    return {"grants": access.list_grants()}


@app.post("/api/access")
def api_access_grant(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"grants": access.grant(str(payload.get("path") or ""), bool(payload.get("write")))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/access")
def api_access_revoke(payload: dict[str, Any]) -> dict[str, Any]:
    return {"grants": access.revoke(str(payload.get("path") or ""))}


# --- Importing a game project (Godot, Unity, Unreal or any folder) ---

import_jobs: dict[str, dict[str, Any]] = {}


@app.post("/api/import/analyze")
async def api_import_analyze(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(game_import.preview, str(payload.get("folder") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/import/start")
async def api_import_start(payload: dict[str, Any]) -> dict[str, Any]:
    folder = str(payload.get("folder") or "").strip()
    if not folder or not Path(folder).expanduser().is_dir():
        raise HTTPException(status_code=400, detail="Choose a game folder first.")
    project = str(payload.get("project") or "").strip()
    game = Path(folder).expanduser().resolve()
    if not project:
        project = home_owner(game, actions.root_dir) or ""
    if not project:
        name = sanitize_project_name(str(payload.get("name") or Path(folder).name).strip() or "imported_game")
        base, root, index = name, actions.root_dir or get_projects_root(), 2
        while (root / name).exists():
            name, index = f"{base}-{index}", index + 1
        project = name
        # A new project lives in the game it comes from, linked to it.
        make_home(project, game, actions.root_dir)
    try:
        ws_dir = _ws_dir(project)
        if not game_link.get_link(project, actions.root_dir):
            await asyncio.to_thread(connect_agent.connect, project, str(game), None)
        # Choosing the folder in Glob's own import screen is the user's consent to read it.
        access.grant(folder, write=bool(access.granted_path(str(Path(folder).expanduser().resolve()), need_write=True)))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = {"id": f"imp_{int(time.time() * 1000)}", "project": project, "folder": folder, "status": "running", "log": [], "result": None, "error": ""}
    import_jobs[job["id"]] = job
    title = str(payload.get("title") or "").strip() or None
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None

    async def run() -> None:
        try:
            result = await asyncio.to_thread(game_import.import_game, folder, ws_dir, title=title, progress=job["log"].append, plan=plan)
            job["result"] = {key: result[key] for key in ("summary", "copied", "linked", "bytes", "sections")} | {"assets": len(result["assets"])}
            job["status"] = "done"
        except Exception as exc:  # noqa: BLE001 — shown in the import screen
            job["status"], job["error"] = "failed", str(exc)
        await workspace_changed(project)

    asyncio.create_task(run())
    return job


@app.get("/api/import/jobs/{job_id}")
def api_import_job(job_id: str) -> dict[str, Any]:
    job = import_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such import.")
    return job


@app.get("/api/import/models")
def api_import_models(project: str = "default") -> dict[str, Any]:
    """Imported 3D models without a preview yet; the browser renders and uploads one for each."""
    ws = _ws_dir(project)
    models = []
    for folder in ws.rglob("3d"):
        if not folder.is_dir() or any(part.startswith(".") for part in folder.relative_to(ws).parts):
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() in (".glb", ".fbx", ".obj") and not path.with_name(f"{path.stem}_thumb.png").exists() and path.stat().st_size < 60_000_000:
                models.append(path.relative_to(ws).as_posix())
    return {"models": models[:400]}


@app.post("/api/import/describe")
async def api_import_describe(project: str = "default") -> dict[str, Any]:
    described = await game_import.describe_assets(_ws_dir(project), _agent_model_call)
    if described:
        await workspace_changed(project)
    return {"described": described}


# --- Where projects live, and moving projects between computers ---

def _open_in_file_manager(path: Path) -> None:
    import subprocess
    import sys as _sys

    if _sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — a folder Glob itself chose
    elif _sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


@app.get("/api/storage")
def api_storage(project: str = "default") -> dict[str, Any]:
    root = (actions.root_dir or get_projects_root()).resolve()
    return {
        "projects_dir": str(root),
        "workspace_dir": str(_ws_dir(project).resolve()) if project else "",
        "config_dir": str(get_config_dir().resolve()),
        "env_override": bool(os.environ.get("FORGE_PROJECTS_DIR")) and not load_config().projects_dir,
    }


def _install_dir() -> Path:
    """Where Glob itself is installed: the app folder (installed app) or the Python environment it runs in."""
    import sys as _sys

    return Path(_sys.executable).resolve().parent if getattr(_sys, "frozen", False) else Path(_sys.prefix).resolve()


@app.get("/api/about")
async def api_about() -> dict[str, Any]:
    """Settings → About: what Glob is, where it lives, and how to open it again."""
    from forge import __version__, shortcut, updater

    kind = updater.install_kind()
    places = await asyncio.to_thread(lambda: [str(p) for p in shortcut.locations() if p.exists()])
    return {
        "version": __version__,
        "install_kind": kind,
        "install_dir": str(_install_dir()),
        "shortcuts": places,
        "projects_dir": str((actions.root_dir or get_projects_root()).resolve()),
        "config_dir": str(get_config_dir().resolve()),
        "log": str(get_config_dir().resolve() / "logs" / "glob-server.log"),
        "background": _background(),
    }


@app.post("/api/shortcut")
async def api_shortcut() -> dict[str, Any]:
    """Put a Glob shortcut on the desktop and in the Start menu / app list (see forge/shortcut.py)."""
    from forge import shortcut

    try:
        made = await asyncio.to_thread(shortcut.create)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"shortcuts": [str(p) for p in made]}


@app.post("/api/storage/open")
def api_storage_open(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    target = str(payload.get("target") or "workspace")
    sub = str(payload.get("path") or "")
    if target == "projects":
        path = (actions.root_dir or get_projects_root()).resolve()
    elif target == "install":
        path = _install_dir()
    elif target == "config":
        path = get_config_dir().resolve()
    elif target == "grant":
        path = access.check(sub) if sub else None
    else:
        path = project_overview.resolve_rel(_ws_dir(project), sub) if sub else _ws_dir(project).resolve()
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="That folder does not exist.")
    try:
        _open_in_file_manager(path if path.is_dir() else path.parent)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Could not open the file manager: {exc}") from exc
    return {"opened": str(path)}


@app.post("/api/storage/location")
def api_storage_location(payload: dict[str, Any]) -> dict[str, Any]:
    raw = str(payload.get("path") or "").strip()
    if raw:
        target = Path(raw).expanduser()
        if not target.is_absolute():
            raise HTTPException(status_code=400, detail="Use a full folder path.")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Can't use that folder: {exc}") from exc
        raw = str(target.resolve())
    save_config({"projects_dir": raw})
    if os.environ.get("FORGE_PROJECTS_DIR"):
        if raw:
            os.environ["FORGE_PROJECTS_DIR"] = raw
        else:
            os.environ.pop("FORGE_PROJECTS_DIR", None)
    actions._stores.clear()
    actions._executors.clear()
    return {"projects_dir": str(get_projects_root().resolve()), **get_project_catalog(actions.root_dir)}


def _project_dir(project: str) -> Path:
    root = (actions.root_dir or get_projects_root()).resolve()
    target = (root / project).resolve()
    if sanitize_project_name(project) != project or target.parent != root or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Project '{project}' was not found.")
    return project_home(project, root).resolve()


@app.post("/api/projects/{project}/export")
async def api_export_project(project: str, payload: dict[str, Any]) -> dict[str, Any]:
    source = _project_dir(project)
    dest = str(payload.get("dest") or "").strip()
    if not dest:
        try:
            dest = await asyncio.to_thread(
                _native_dialog, "save", "Export project", initialfile=f"{project}.glob.zip",
                defaultextension=".zip", filetypes=[("Glob project", "*.zip")],
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="The save dialog could not open.") from exc
        if not dest:
            return {"cancelled": True}
    target = Path(dest).expanduser()
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="Use a full file path.")
    result = await asyncio.to_thread(workspace_archive.export_project, source, target, None, project)
    return result


@app.get("/api/projects/{project}/export.zip")
async def api_download_project(project: str) -> FileResponse:
    import tempfile

    source = _project_dir(project)
    target = Path(tempfile.gettempdir()) / f"{project}.glob.zip"
    await asyncio.to_thread(workspace_archive.export_project, source, target, None, project)
    return FileResponse(target, media_type="application/zip", filename=f"{project}.glob.zip")


@app.post("/api/projects/import-archive")
async def api_import_archive(file: UploadFile | None = File(None), path: str = Form(""), name: str = Form("")) -> dict[str, Any]:
    import tempfile

    root = (actions.root_dir or get_projects_root()).resolve()
    temporary: Path | None = None
    try:
        if file is not None:
            handle, raw = tempfile.mkstemp(suffix=".zip")
            os.close(handle)
            temporary = Path(raw)
            with open(temporary, "wb") as sink:
                while chunk := await file.read(8 * 1024 * 1024):
                    sink.write(chunk)
            archive = temporary
        else:
            if not path:
                try:
                    path = await asyncio.to_thread(_native_dialog, "open", "Import a Glob project", filetypes=[("Glob project", "*.zip")])
                except Exception as exc:
                    raise HTTPException(status_code=503, detail="The file picker could not open.") from exc
                if not path:
                    return {"cancelled": True}
            archive = Path(path).expanduser()
        project = await asyncio.to_thread(workspace_archive.import_archive, archive, root, name or (Path(file.filename).stem.removesuffix(".glob") if file and file.filename else ""))
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # zipfile.BadZipFile and friends
        raise HTTPException(status_code=400, detail=f"Not a Glob project archive: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"project": project, **get_project_catalog(actions.root_dir)}


@app.post("/api/pipeline/item/{item_id}/move")
def api_move_pipeline_item(item_id: str, payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    try:
        return actions.move_pipeline_item(
            item_id=item_id,
            target_folder=payload.get("target_folder"),
            direction=payload.get("direction"),
            new_index=payload.get("new_index"),
            project=project,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/auto-organize")
async def api_auto_organize(project: str = "default") -> dict[str, Any]:
    return await actions.auto_organize_folders(project=project)


@app.post("/api/pipeline/recover-meshy")
async def api_recover_meshy(project: str = "default") -> dict[str, Any]:
    return await actions.recover_meshy_tasks(project=project)


@app.put("/api/pipeline/style-rules")
def api_update_style_rules(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    return actions.update_style_rules(payload, project=project)


@app.post("/api/pipeline/item")
def api_upsert_pipeline_item(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    return actions.upsert_pipeline_item(payload, project=project)


@app.post("/api/pipeline/item/{item_id}/assets")
def api_attach_asset_to_blueprint(
    item_id: str, payload: dict[str, Any], project: str = "default"
) -> dict[str, Any]:
    asset_id = str(payload.get("asset_id", "")).strip()
    if not asset_id:
        raise HTTPException(status_code=400, detail="asset_id is required")
    try:
        return actions.attach_asset_to_blueprint(item_id, asset_id, project=project)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/pipeline/item/{item_id}/blueprint")
async def api_generate_pipeline_item_blueprint(
    item_id: str, payload: dict[str, Any] | None = None, project: str = "default"
) -> dict[str, Any]:
    body = payload or {}
    try:
        return await actions.generate_pipeline_item_blueprint(
            item_id=item_id,
            overwrite_existing=body.get("overwrite_existing") is True,
            draft_blueprint=body.get("draft_blueprint") if isinstance(body.get("draft_blueprint"), dict) else None,
            project=project,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/pipeline/item/{item_id}/prompt-preview")
def api_preview_pipeline_candidate_prompts(
    item_id: str, payload: dict[str, Any] | None = None, project: str = "default"
) -> dict[str, Any]:
    body = payload or {}
    return actions.preview_pipeline_candidate_prompts(
        item_id=item_id,
        count=body.get("count"),
        custom_prompt=body.get("prepared_prompt"),
        image_params=body.get("image_params"),
        prompt_recipe=body.get("prompt_recipe"),
        project=project,
    )


@app.post("/api/pipeline/item/{item_id}/candidates")
async def api_generate_pipeline_candidates(
    item_id: str, payload: dict[str, Any] | None = None, project: str = "default"
) -> dict[str, Any]:
    from ..providers.base import ProviderError

    body = payload or {}
    try:
        return await actions.generate_pipeline_candidates(
            item_id=item_id,
            count=body.get("count"),
            custom_prompt=body.get("prepared_prompt"),
            image_params=body.get("image_params"),
            project=project,
            candidate_prompts=body.get("candidate_prompts"),
            slot_index=body.get("slot_index"),
            reference_asset_ids=body.get("reference_asset_ids"),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc


@app.post("/api/pipeline/item/{item_id}/workflow")
async def api_run_pipeline_workflow(
    item_id: str, payload: dict[str, Any] | None = None, project: str = "default"
) -> dict[str, Any]:
    body = payload or {}
    return await actions.run_pipeline_item_workflow(
        item_id=item_id,
        winner_asset_id=body.get("winner_asset_id"),
        workflow_preset=body.get("workflow_preset"),
        mesh_params=body.get("mesh_params"),
        project=project,
    )


@app.post("/api/pipeline/slice-sheet")
def api_slice_sheet(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    asset_id = str(payload.get("asset_id", ""))
    slice_names = payload.get("slice_names")
    return actions.slice_sheet_asset(asset_id=asset_id, slice_names=slice_names, project=project)


@app.delete("/api/pipeline/item/{item_id}")
def api_delete_pipeline_item(
    item_id: str,
    project: str = "default",
    confirm: bool = False,
    delete_generated_assets: bool = False,
) -> dict[str, Any]:
    try:
        return actions.delete_pipeline_item(
            item_id=item_id,
            project=project,
            confirm=confirm,
            delete_generated_assets=delete_generated_assets,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/item/{item_id}/attach-upload")
async def api_attach_upload_to_item(
    item_id: str,
    file: UploadFile = File(...),
    project: str = Form("default"),
    slot_index: int | None = Form(None),
) -> dict[str, Any]:
    store = actions.get_store(project)
    content = await file.read()
    asset = store.ingest_uploaded_file(
        filename=file.filename or "dropped_candidate.png",
        content=content,
        name=Path(file.filename or "Custom Image").stem.replace("_", " ").title(),
        tags=["custom_candidate"],
    )
    pipe = actions.attach_asset_to_pipeline_item(
        item_id=item_id,
        asset_id=asset.id,
        slot_index=slot_index,
        as_winner=True,
        project=project,
    )
    return {"asset": asset.model_dump(), "pipeline": pipe}


def _find_pipeline_item(project: str, item_id: str):
    item = next(
        (entry for entry in actions.get_pipeline(project).list_pipeline_items() if entry.id == item_id or entry.slug == item_id),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail=f"Blueprint '{item_id}' not found.")
    return item


# --- Project overview (generic folder structure) ---

def _overview_call(fn, *args: Any) -> Any:
    try:
        return fn(*args)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _ws_dir(project: str) -> Path:
    return actions.get_workspace(project).ws_dir


def _prototype_items(project: str) -> list[dict[str, Any]]:
    """Every playable sketch in the workspace's prototype/ folder: prototype/<name>/index.html (and an older prototype/index.html)."""
    import re

    root = _ws_dir(project) / "prototype"
    found: list[tuple[str, Path]] = []
    if (root / "index.html").is_file():
        found.append(("", root / "index.html"))
    if root.is_dir():
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and not sub.name.startswith(".") and (sub / "index.html").is_file():
                found.append((sub.name, sub / "index.html"))
    items = []
    for slug, path in found:
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:6000]
            stat = path.stat()
        except OSError:
            continue
        title = re.search(r"<title>(.*?)</title>", head, re.I | re.S)
        desc = re.search(r"""<meta[^>]+name=["']description["'][^>]+content=["'](.*?)["']""", head, re.I | re.S)
        items.append({
            "slug": slug,
            "title": (title.group(1).strip() if title and title.group(1).strip() else (slug.replace("-", " ").title() or "First prototype"))[:80],
            "description": (desc.group(1).strip() if desc else "")[:200],
            "path": f"prototype/{slug + '/' if slug else ''}index.html",
            "updated": stat.st_mtime,
            "size": stat.st_size,
        })
    items.sort(key=lambda i: i["updated"], reverse=True)
    return items


@app.get("/api/prototypes")
def api_prototypes(project: str = "default") -> dict[str, Any]:
    return {"prototypes": _prototype_items(project)}


@app.delete("/api/prototypes")
def api_delete_prototype(project: str, slug: str = "") -> dict[str, Any]:
    """Moves one prototype to the workspace's .trash so it can be restored."""
    import shutil
    import time

    if slug and (slug != Path(slug).name or slug.startswith(".")):
        raise HTTPException(status_code=400, detail="Bad prototype name")
    root = _ws_dir(project) / "prototype"
    trash = _ws_dir(project) / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if slug:
        src = root / slug
        if not src.is_dir():
            raise HTTPException(status_code=404, detail="No such prototype")
        shutil.move(str(src), str(trash / f"prototype-{slug}-{stamp}"))
    else:
        src = root / "index.html"
        if not src.is_file():
            raise HTTPException(status_code=404, detail="No such prototype")
        shutil.move(str(src), str(trash / f"prototype-first-{stamp}.html"))
    return {"ok": True}


@app.get("/api/overview")
def api_overview(project: str = "default") -> dict[str, Any]:
    workspace_watcher.touch(project)
    overview = project_overview.build_overview(_ws_dir(project), project)
    folder = game_link.game_folder(project, actions.root_dir)
    if overview.get("empty") and folder and folder.is_dir():
        # An empty project made for a game: the overview offers to build its workspace from the game itself.
        from forge.game_import import _engine

        _, engine, title = _engine(folder)
        overview["game"] = {"folder": str(folder), "title": title, "engine": engine}
    return overview


@app.get("/api/overview/folder")
def api_overview_folder(path: str = "", project: str = "default") -> dict[str, Any]:
    return _overview_call(project_overview.describe_folder, _ws_dir(project), path)


@app.get("/api/overview/file")
def api_overview_read_file(path: str, project: str = "default") -> dict[str, Any]:
    return _overview_call(project_overview.read_text_file, _ws_dir(project), path)


@app.put("/api/overview/file")
async def api_overview_write_file(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    saved = _overview_call(project_overview.write_text_file, _ws_dir(project), str(payload.get("path") or ""), str(payload.get("content") or ""))
    await workspace_changed(project)
    return saved


@app.delete("/api/overview/style-text")
async def api_overview_remove_style_text(path: str, project: str = "default") -> dict[str, Any]:
    _overview_call(project_overview.remove_style_text, _ws_dir(project), path)
    await workspace_changed(project)
    return {"removed": path}


@app.delete("/api/overview/pillar")
async def api_overview_remove_pillar(path: str, project: str = "default") -> dict[str, Any]:
    _overview_call(project_overview.remove_pillar_image, _ws_dir(project), path)
    await workspace_changed(project)
    return {"removed": path}


@app.post("/api/overview/folder")
async def api_overview_create_folder(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    path = _overview_call(project_overview.create_folder, _ws_dir(project), str(payload.get("parent") or ""), str(payload.get("title") or ""))
    await workspace_changed(project)
    return {"path": path}


@app.post("/api/overview/to-folder")
async def api_overview_to_folder(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    path = _overview_call(project_overview.doc_to_folder, _ws_dir(project), str(payload.get("path") or ""))
    await workspace_changed(project)
    return {"path": path}


UPLOAD_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@app.post("/api/overview/upload")
async def api_overview_upload(
    project: str = "default", path: str = "", files: list[UploadFile] = File(...)
) -> dict[str, Any]:
    """Save images into a workspace folder, e.g. a `pillars/` folder or an asset's references."""
    ws_dir = _ws_dir(project)
    folder = _overview_call(project_overview.resolve_rel, ws_dir, path) if path else ws_dir.resolve()
    saved: list[str] = []
    for upload in files[:20]:
        name = Path(upload.filename or "image.png").name
        suffix = Path(name).suffix.lower()
        if suffix not in UPLOAD_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"'{name}' is not a supported image.")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem).strip("-")[:60] or "image"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{stem}{suffix}"
        index = 2
        while target.exists():
            target = folder / f"{stem}-{index}{suffix}"
            index += 1
        target.write_bytes(await upload.read())
        saved.append(target.relative_to(ws_dir.resolve()).as_posix())
    await workspace_changed(project)
    return {"saved": saved}


# --- Asset studio: candidates, winner, variants, edits, 3D ---

studio_jobs: dict[str, dict[str, Any]] = {}
_jobs_loaded: set[str] = set()


# Jobs are kept on disk per project, so a restart cannot make a run vanish: anything still "running"
# when Forge starts again is marked interrupted and shown on the asset page.
def _jobs_file(project: str) -> Path:
    return actions.get_store(project).project_dir / "studio_jobs.json"


def _load_jobs(project: str) -> None:
    if project in _jobs_loaded:
        return
    _jobs_loaded.add(project)
    try:
        saved = json.loads(_jobs_file(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for job in saved:
        if job.get("status") in ("running", "queued"):
            job.update(status="interrupted", finished_at=time.time(),
                       error="Forge restarted while this was running, so the result was not saved. Run it again.")
        studio_jobs.setdefault(job["id"], job)
    _save_jobs(project)


def _save_jobs(project: str) -> None:
    jobs = [job for job in studio_jobs.values() if job["project"] == project][-50:]
    try:
        _jobs_file(project).write_text(json.dumps(jobs, indent=1), encoding="utf-8")
    except OSError:
        pass


@app.get("/api/studio/asset")
def api_studio_asset(path: str, project: str = "default") -> dict[str, Any]:
    return _overview_call(asset_studio.describe_asset, _ws_dir(project), path)


@app.patch("/api/studio/asset")
async def api_studio_update_asset(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    # The voice sheet remembers its pick; pillars, aspect and height are set from the asset page and the viewer.
    allowed = {"kind", "prompt", "winner", "style", "title", "voice", "voice_name", "pillars", "aspect", "height_m", "tile_m", "kit_cell", "kit_height", "kit_thickness", "kit_relief", "kit_wall", "kit_wall_2", "kit_wall_3", "kit_floor", "kit_floor_2", "kit_ceiling"}
    fields = {key: value for key, value in dict(payload.get("fields") or {}).items() if key in allowed}
    result = _overview_call(asset_studio.update_asset, _ws_dir(project), str(payload.get("path") or ""), fields)
    await workspace_changed(project)
    return result


@app.delete("/api/studio/output")
async def api_studio_remove_output(path: str, output: str, project: str = "default") -> dict[str, Any]:
    result = _overview_call(asset_studio.remove_output, _ws_dir(project), path, output)
    await workspace_changed(project)
    return result


@app.post("/api/studio/output/restore")
async def api_studio_restore_outputs(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    restored = _overview_call(asset_studio.restore_outputs, _ws_dir(project), list(payload.get("moves") or []))
    await workspace_changed(project)
    return {"restored": restored}


@app.post("/api/studio/preview")
def api_studio_preview(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    try:
        return asset_studio.build_request(
            _ws_dir(project),
            str(payload.get("path") or ""),
            str(payload.get("action") or "candidates"),
            source=str(payload.get("source") or ""),
            instruction=str(payload.get("instruction") or ""),
            model=str(payload.get("model") or ""),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _is_offline_error(exc: BaseException) -> bool:
    """True for a failed DNS lookup or refused connection anywhere in the exception chain."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (socket.gaierror, ConnectionRefusedError)) or type(exc).__name__ == "ConnectError"                 or "getaddrinfo failed" in str(exc) or "Name or service not known" in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def _run_studio_job(project: str, job: dict[str, Any], request: dict[str, Any]) -> None:
    try:
        job["outputs"] = await asset_studio.run_action(actions, project, request)
        job["status"] = "done"
    except ProviderError as exc:
        # The fix is what the user needs next, so it goes on the asset page with the error.
        specific = exc.how_to_fix and exc.how_to_fix != ProviderError("", provider="").how_to_fix  # skip the generic default
        job["status"], job["error"] = "failed", f"{exc.message} {exc.how_to_fix}" if specific else exc.message
        _note_credits(request, exc.provider, exc.message, exc.status_code)
    except Exception as exc:  # noqa: BLE001 — report any failure on the asset page
        reason = str(exc).strip() or type(exc).__name__
        if "Timeout" in type(exc).__name__:
            reason = "The request took too long (timed out). Large models upload slowly; try a remeshed, smaller model."
        elif _is_offline_error(exc):
            reason = ("Could not reach the image service: this computer is offline or could not look up its address "
                      "(Wi-Fi, VPN or DNS). Check the connection and press Try again.")
        job["status"], job["error"] = "failed", reason[:400]
        _note_credits(request, "", reason, 0)
    job["finished_at"] = time.time()
    _credit_balance_cache.clear()  # the job may have spent credits: the next balance read is fresh
    _save_jobs(project)
    await ws_manager.broadcast({"type": "studio_job", "project": project, "job": dict(job)})
    await workspace_changed(project)


# Providers that said "out of credits" lately, so the generate sheet can warn before the next run.
credit_alerts: dict[str, dict[str, Any]] = {}


def _note_credits(request: dict[str, Any], provider: str, message: str, status: int) -> None:
    if status != 402 and not re.search(r"credit|insufficient|balance|quota", message, re.I):
        return
    model = str(request.get("model") or "")
    if model.startswith("svc:"):
        provider = model[4:].split("/")[0]
    elif not provider:
        provider = model.partition(":")[0] or ("meshy" if request.get("action") == "make_3d" else "")
    if provider:
        credit_alerts[provider] = {"at": time.time(), "error": message[:200]}


# Batch runs go through a small queue so twenty assets don't hit a provider all at once.
_batch_slots: dict[str, asyncio.Semaphore] = {}


def queue_studio_job(project: str, request: dict[str, Any]) -> dict[str, Any]:
    asset_studio.read_asset(_ws_dir(project), str(request.get("asset") or ""))
    job = {
        "id": f"job_{int(time.time() * 1000)}_{len(studio_jobs)}",
        "project": project,
        "asset": request.get("asset"),
        "action": request.get("action"),
        "status": "queued",
        "batch": True,
        "started_at": time.time(),
    }
    _load_jobs(project)
    studio_jobs[job["id"]] = job
    asyncio.create_task(_run_queued(project, job, request))
    return dict(job)


async def _run_queued(project: str, job: dict[str, Any], request: dict[str, Any]) -> None:
    await ws_manager.broadcast({"type": "studio_job", "project": project, "job": dict(job)})
    async with _batch_slots.setdefault(project, asyncio.Semaphore(2)):
        if job["status"] == "cancelled":
            return
        job.update(status="running", started_at=time.time())
        _save_jobs(project)
        await ws_manager.broadcast({"type": "studio_job", "project": project, "job": dict(job)})
        await _run_studio_job(project, job, request)


def start_studio_job(project: str, request: dict[str, Any]) -> dict[str, Any]:
    if request.get("action") not in asset_studio.ACTIONS:
        raise ValueError(f"Unknown action '{request.get('action')}'.")
    asset_studio.read_asset(_ws_dir(project), str(request.get("asset") or ""))
    job = {
        "id": f"job_{int(time.time() * 1000)}",
        "project": project,
        "asset": request.get("asset"),
        "action": request.get("action"),
        "status": "running",
        "started_at": time.time(),
    }
    _load_jobs(project)
    studio_jobs[job["id"]] = job
    _save_jobs(project)
    asyncio.create_task(_start_studio_job(project, job, request))
    return dict(job)


async def _start_studio_job(project: str, job: dict[str, Any], request: dict[str, Any]) -> None:
    await ws_manager.broadcast({"type": "studio_job", "project": project, "job": dict(job)})
    await _run_studio_job(project, job, request)


workspace_agent.job_starter = start_studio_job


@app.get("/api/studio/service-models")
async def api_studio_service_models() -> dict[str, Any]:
    """Image and 3D models of the services the user signed in to, for the studio's source menu."""
    image, mesh = await asyncio.gather(asyncio.to_thread(service_gen.catalog, "image"), asyncio.to_thread(service_gen.catalog, "3d"))
    return {"image": image, "3d": mesh}


@app.post("/api/studio/run")
async def api_studio_run(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    return _overview_call(start_studio_job, project, dict(payload.get("request") or {}))


@app.get("/api/studio/options")
def api_studio_options() -> dict[str, Any]:
    return {"models": asset_studio.list_image_models(), "audio_models": asset_studio.list_audio_models(), "selected": asset_studio.default_image_model(load_config()), "selected_3d": load_config().default_3d_service_model, "kinds": list(asset_studio.ASSET_KINDS),
            "kind_defaults": asset_studio.kind_defaults(load_config()), "tripo_connected": bool(load_config().get_key("tripo"))}


@app.get("/api/studio/voices")
async def api_studio_voices() -> dict[str, Any]:
    try:
        from forge.providers.elevenlabs_audio import list_voices
    except ImportError:
        return {"voices": []}
    error = ""
    try:
        voices = await list_voices()
    except Exception as exc:  # noqa: BLE001 — an empty list keeps the voice picker usable
        voices, error = [], str(exc)[:200]
    if not voices:
        # No key, or a rejected one: the account the user signed in to has the same voices.
        from forge import service_gen, services

        if "elevenlabs" in services.connected():
            try:
                return {"voices": await asyncio.to_thread(service_gen.list_voices)}
            except Exception as exc:  # noqa: BLE001
                error = str(exc)[:200]
    return {"voices": voices, **({"error": error} if error else {})}


@app.post("/api/studio/audio-upload")
async def api_studio_audio_upload(project: str = "default", path: str = "", files: list[UploadFile] = File(...)) -> dict[str, Any]:
    ws_dir = _ws_dir(project)
    saved = [_overview_call(asset_studio.save_audio, ws_dir, path, upload.filename or "sound.mp3", await upload.read()) for upload in files[:10]]
    await workspace_changed(project)
    return {"saved": saved}


@app.get("/api/studio/pillar-targets")
def api_studio_pillar_targets(path: str, project: str = "default") -> dict[str, Any]:
    return {"targets": _overview_call(asset_studio.pillar_targets, _ws_dir(project), path)}


@app.post("/api/studio/make-pillar")
async def api_studio_make_pillar(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    saved = _overview_call(asset_studio.make_pillar, _ws_dir(project), str(payload.get("image") or ""), str(payload.get("folder") or ""))
    await workspace_changed(project)
    return {"saved": saved}


@app.get("/api/studio/history")
def api_studio_history(path: str, project: str = "default") -> dict[str, Any]:
    """What each output of an asset was made from, so an older prompt can be brought back."""
    return {"history": _overview_call(asset_studio.output_history, _ws_dir(project), path)}


@app.get("/api/studio/credits")
async def api_studio_credits() -> dict[str, Any]:
    """Providers that ran out of credits in the last six hours, and Meshy's balance when a key is set."""
    fresh = {k: v for k, v in credit_alerts.items() if time.time() - v["at"] < 6 * 3600}
    balance = None
    key = load_config().get_key("meshy")
    if key:
        balance = _cached_meshy_balance(key)
        if balance is None:
            try:
                async with httpx.AsyncClient(timeout=6) as client:
                    res = await client.get("https://api.meshy.ai/openapi/v1/balance", headers={"Authorization": f"Bearer {key}"})
                if res.status_code == 200:
                    balance = res.json().get("balance")
                    _credit_balance_cache["meshy"] = (time.time(), _key_fingerprint(key), balance)
            except (httpx.HTTPError, ValueError):
                pass
    tripo_balance = None
    tripo_key = load_config().get_key("tripo")
    if tripo_key:
        cached = _credit_balance_cache.get("tripo")
        if cached and cached[1] == _key_fingerprint(tripo_key) and time.time() - cached[0] < CREDIT_CACHE_SECONDS:
            tripo_balance = cached[2]
        else:
            try:
                tripo_balance = (await tripo_client.balance(tripo_key))["balance"]
                _credit_balance_cache["tripo"] = (time.time(), _key_fingerprint(tripo_key), tripo_balance)
            except (ProviderError, httpx.HTTPError):
                pass
    return {"alerts": fresh, "meshy_balance": balance, "tripo_balance": tripo_balance}


# Meshy's balance, kept per provider for a minute: the generate sheet asks for it every time it opens.
# Cleared when a generation job finishes, since that is when the balance changes.
CREDIT_CACHE_SECONDS = 60
_credit_balance_cache: dict[str, tuple[float, str, Any]] = {}


def _key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _cached_meshy_balance(key: str) -> Any | None:
    """The cached balance for this key, while it is less than a minute old (a new key never gets another's)."""
    entry = _credit_balance_cache.get("meshy")
    if not entry:
        return None
    at, fingerprint, balance = entry
    return balance if fingerprint == _key_fingerprint(key) and time.time() - at < CREDIT_CACHE_SECONDS else None


@app.get("/api/studio/batch")
def api_studio_batch_plan(folder: str = "", project: str = "default") -> dict[str, Any]:
    """The assets "Generate all missing" would make, with the request each one would send."""
    ws_dir = _ws_dir(project)
    plan = []
    for rel in _overview_call(asset_studio.missing_assets, ws_dir, folder):
        try:
            req = asset_studio.build_request(ws_dir, rel, "candidates")
        except (FileNotFoundError, ValueError):
            continue
        plan.append({"asset": rel, "audio": bool(req.get("audio")), "model": req.get("model"), "count": req.get("count", 1)})
    return {"folder": folder, "assets": plan}


@app.post("/api/studio/batch")
async def api_studio_batch(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    ws_dir = _ws_dir(project)
    wanted = [str(p) for p in payload.get("assets") or []] or _overview_call(asset_studio.missing_assets, ws_dir, str(payload.get("folder") or ""))
    count = payload.get("count")
    jobs, skipped = [], []
    for rel in wanted:
        try:
            req = asset_studio.build_request(ws_dir, rel, "candidates")
        except (FileNotFoundError, ValueError) as exc:
            skipped.append({"asset": rel, "reason": str(exc)})
            continue
        if count:
            req["count"] = int(count)
        jobs.append(queue_studio_job(project, req))
    _save_jobs(project)
    return {"jobs": jobs, "skipped": skipped}


@app.post("/api/studio/jobs/{job_id}/cancel")
async def api_studio_cancel_job(job_id: str, project: str = "default") -> dict[str, Any]:
    """Take a job that hasn't started out of the batch queue."""
    job = studio_jobs.get(job_id)
    if not job or job.get("project") != project:
        raise HTTPException(status_code=404, detail="No such job.")
    if job["status"] != "queued":
        raise HTTPException(status_code=400, detail="Only waiting jobs can be cancelled.")
    job.update(status="cancelled", finished_at=time.time())
    _save_jobs(project)
    await ws_manager.broadcast({"type": "studio_job", "project": project, "job": dict(job)})
    return dict(job)


@app.post("/api/overview/key-art")
async def api_overview_key_art(project: str = "default", start: bool = True) -> dict[str, Any]:
    """Make (or reuse) the project's key-art asset and start two candidates; the first becomes the cover.

    With start=false only the asset is made: the app then opens its Generate sheet, so the user sees and can change
    the prompt, model and framing before anything is spent."""
    ws_dir = _ws_dir(project)
    ov = project_overview.build_overview(ws_dir, project)
    rel = _overview_call(asset_studio.key_art_asset, ws_dir, ov["title"], ov.get("pitch") or ov["excerpt"] or "")
    if not start:
        await workspace_changed(project)
        return {"asset": rel, "job": None}
    request = _overview_call(asset_studio.build_request, ws_dir, rel, "candidates")
    job = _overview_call(start_studio_job, project, request)
    await workspace_changed(project)
    return {"asset": rel, "job": job}


@app.post("/api/overview/cover")
async def api_overview_cover(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    """Use any workspace image as the project's cover."""
    name = _overview_call(asset_studio.set_project_cover, _ws_dir(project), str(payload.get("image") or ""))
    await workspace_changed(project)
    return {"cover": name}


@app.get("/api/board")
async def api_board(project: str = "default") -> dict[str, Any]:
    return await asyncio.to_thread(showcase.board, _ws_dir(project), project)


@app.get("/api/press-kit")
async def api_press_kit(project: str = "default") -> Response:
    data = await asyncio.to_thread(showcase.press_kit_zip, _ws_dir(project), project)
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{project}-press-kit.zip"'})


SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


@app.post("/api/projects/sample")
async def api_install_sample(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy a finished sample project (Pocket Kaiju) in as a new project to look around in."""
    import shutil

    sample = str((payload or {}).get("sample") or "pocket-kaiju")
    source = SAMPLES_DIR / sample
    if not re.fullmatch(r"[a-z0-9-]+", sample) or not source.is_dir():
        raise HTTPException(status_code=404, detail="No such sample project.")
    root = actions.root_dir or get_projects_root()
    base = name = sanitize_project_name(sample.replace("-", "_"))
    index = 2
    while (root / name).exists():
        name, index = f"{base}_{index}", index + 1
    actions.get_store(name)
    ws_dir = _ws_dir(name)
    await asyncio.to_thread(shutil.copytree, source, ws_dir, dirs_exist_ok=True)
    return {"project": name, "projects": list_all_projects(actions.root_dir)}


# --- Final Vault: every asset's chosen image and newest model, ready to export ---

@app.get("/api/vault")
def api_vault(project: str = "default") -> dict[str, Any]:
    return _overview_call(vault.list_vault, _ws_dir(project))


@app.get("/api/vault/export")
def api_vault_export(project: str = "default", path: list[str] | None = Query(default=None), history: bool = False) -> Response:
    data = _overview_call(vault.export_zip, _ws_dir(project), project, path, history)
    name = f"{project}-vault.zip" if not path or len(path) > 1 else f"{Path(path[0]).stem}.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/vault/export")
def api_vault_export_folder(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    folder = _overview_call(vault.export_folder, _ws_dir(project), project, list(payload.get("paths") or []), bool(payload.get("history")))
    return {"folder": folder}


# --- Game tools: dropped models, parts, icons, sprites, rigging, animation, engine export ---

def _png_bytes(value: str) -> bytes:
    import base64

    try:
        return base64.b64decode(value.split(",", 1)[-1], validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Image data is not valid base64.") from exc


@app.post("/api/studio/model-upload")
async def api_studio_model_upload(project: str = "default", path: str = "", files: list[UploadFile] = File(...)) -> dict[str, Any]:
    ws_dir = _ws_dir(project)
    uploads = [(upload.filename or "model.glb", await upload.read()) for upload in files[:40]]  # a model plus its texture maps
    saved = _overview_call(game_tools.save_model_files, ws_dir, path, uploads)
    await workspace_changed(project)
    return {"saved": saved}


@app.post("/api/studio/thumb")
def api_studio_thumb(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    return {"thumb": _overview_call(game_tools.save_thumb, _ws_dir(project), str(payload.get("model") or ""), _png_bytes(str(payload.get("png") or "")))}


@app.get("/api/studio/detect-parts")
def api_studio_detect_parts(path: str, project: str = "default", sheet: bool = False) -> dict[str, Any]:
    return {"boxes": _overview_call(game_tools.detect_sheet_items if sheet else game_tools.detect_parts, _ws_dir(project), path)}


@app.post("/api/studio/name-parts")
async def api_studio_name_parts(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    """Names for the boxes on a prop sheet, from Glob's chat model when it can see images."""
    image = str(payload.get("image") or "")
    _overview_call(game_tools._asset_image, _ws_dir(project), image)
    return await game_tools.name_sheet_items(_ws_dir(project), image, list(payload.get("boxes") or [])[:24], _agent_model_call)


@app.post("/api/studio/guess-body-parts")
async def api_studio_guess_body_parts(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    image = str(payload.get("image") or "")
    _overview_call(game_tools._asset_image, _ws_dir(project), image)
    return await game_tools.guess_body_parts(_ws_dir(project), image, _agent_model_call)


@app.post("/api/studio/split")
async def api_studio_split(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    created = _overview_call(
        game_tools.split_image, _ws_dir(project), str(payload.get("asset") or ""), str(payload.get("image") or ""),
        list(payload.get("boxes") or []), bool(payload.get("as_assets")),
    )
    await workspace_changed(project)
    return {"created": created}


# A pack in progress, per project and level, so one level can't run twice at once.
_pack_runs: dict[tuple[str, str], asyncio.Task] = {}


async def _run_pack_job(project: str, request: dict[str, Any]) -> dict[str, Any]:
    """One image of a pack: an ordinary queued job, shown in the job list, that the chain waits for."""
    job = {
        "id": f"job_{int(time.time() * 1000)}_{len(studio_jobs)}", "project": project, "asset": request.get("asset"),
        "action": request.get("action"), "status": "queued", "batch": True, "started_at": time.time(),
    }
    _load_jobs(project)
    studio_jobs[job["id"]] = job
    await _run_queued(project, job, request)
    return job


async def _run_pack(project: str, level: str, items: list[str]) -> None:
    ws_dir = _ws_dir(project)
    try:
        await level_pack.run_pack(ws_dir, level, items, lambda request: _run_pack_job(project, request), _agent_model_call)
    except Exception as exc:  # noqa: BLE001 — say so on the level's page
        asset_studio.update_asset(ws_dir, level, {"pack_note": f"The pack stopped: {str(exc)[:300]}"})
    finally:
        _pack_runs.pop((project, level), None)
    await workspace_changed(project)


@app.get("/api/studio/levels")
def api_studio_levels(project: str = "default") -> dict[str, Any]:
    ws_dir = _ws_dir(project)
    return {"levels": level_pack.list_levels(ws_dir), "folder": level_pack.new_level_folder(ws_dir)}


@app.get("/api/studio/level-pack")
def api_studio_level_pack(path: str, project: str = "default") -> dict[str, Any]:
    state = _overview_call(level_pack.status, _ws_dir(project), path)
    return {**state, "running": (project, path) in _pack_runs, "mesh_usd": 0.3 if asset_studio.mesh_settings(load_config()).get("should_texture") else 0.2}


@app.post("/api/studio/level-pack")
async def api_studio_level_pack_start(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    level = str(payload.get("asset") or "")
    items = [str(i) for i in payload.get("items") or []]
    known = _overview_call(level_pack.plan, _ws_dir(project), level)
    if not items:
        raise HTTPException(status_code=400, detail="Tick at least one thing to make.")
    if not asset_studio.describe_asset(_ws_dir(project), level)["winner"]:
        raise HTTPException(status_code=400, detail="Pick a level picture first.")
    if (project, level) in _pack_runs:
        raise HTTPException(status_code=409, detail="This pack is already being made.")
    _pack_runs[(project, level)] = asyncio.create_task(_run_pack(project, level, items))
    return {"started": [item["id"] for item in known if item["id"] in items and not item["done"]]}


@app.post("/api/studio/level-pack/make-3d")
async def api_studio_level_pack_make_3d(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    ws_dir = _ws_dir(project)
    jobs, skipped = [], []
    for rel in [str(a) for a in payload.get("assets") or []][:48]:
        try:
            jobs.append(queue_studio_job(project, level_pack.make_3d_request(ws_dir, rel)))
        except (FileNotFoundError, ValueError) as exc:
            skipped.append({"asset": rel, "reason": str(exc)})
    _save_jobs(project)
    return {"jobs": jobs, "skipped": skipped}


@app.get("/api/studio/materials")
def api_studio_materials(project: str = "default") -> dict[str, Any]:
    return {"materials": modular.list_materials(_ws_dir(project))}


@app.post("/api/studio/material/build")
async def api_studio_material_build(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    created = await asyncio.to_thread(_overview_call, modular.build_material, _ws_dir(project), str(payload.get("asset") or ""))
    await workspace_changed(project)
    return {"created": created}


@app.post("/api/studio/kit/build")
async def api_studio_kit_build(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    built = await asyncio.to_thread(_overview_call, modular.build_kit, _ws_dir(project), str(payload.get("asset") or ""))
    await workspace_changed(project)
    return built


@app.post("/api/studio/icons")
async def api_studio_icons(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    created = _overview_call(
        game_tools.make_icons, _ws_dir(project), str(payload.get("asset") or ""), str(payload.get("image") or ""),
        [int(s) for s in payload.get("sizes") or []], float(payload.get("padding") or 0.08),
    )
    await workspace_changed(project)
    return {"created": created}


@app.post("/api/studio/sprite")
async def api_studio_sprite(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    saved = _overview_call(
        game_tools.save_sprite, _ws_dir(project), str(payload.get("asset") or ""), str(payload.get("name") or "sprites"),
        _png_bytes(str(payload.get("png") or "")), payload.get("meta") or None,
    )
    await workspace_changed(project)
    return {"saved": saved}


@app.get("/api/studio/animations")
async def api_studio_animations(search: str = "", engine: str = "") -> dict[str, Any]:
    try:
        return {"animations": await game_tools.animation_library(search, engine)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- Tripo: connect with an API key (Tripo has no OAuth), balance, and its model tools ---

def _tripo_status(balance: dict[str, float] | None = None, error: str = "") -> dict[str, Any]:
    cfg = load_config()
    key = cfg.get_key("tripo")
    return {"connected": bool(key), "balance": balance, "error": error, "model": cfg.tripo_model,
            "is_default": cfg.default_3d_provider == "tripo", "models": tripo_client.MODELS, "animations": tripo_client.ANIMATIONS}


@app.get("/api/tripo")
async def api_tripo_status() -> dict[str, Any]:
    """Whether a Tripo key is saved, and the credits left on it."""
    key = load_config().get_key("tripo")
    if not key:
        return _tripo_status()
    try:
        return _tripo_status(await tripo_client.balance(key))
    except ProviderError as exc:
        return _tripo_status(error=exc.message)


@app.post("/api/tripo/connect")
async def api_tripo_connect(payload: dict[str, Any]) -> dict[str, Any]:
    """Check a pasted key with Tripo, and only then save it. Makes Tripo the 3D engine when `make_default` is set."""
    key = str(payload.get("api_key") or "").strip()
    try:
        balance = await tripo_client.connect(key)
    except ProviderError as exc:
        raise HTTPException(status_code=exc.status_code if 400 <= exc.status_code < 500 else 400, detail=exc.message) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Tripo: {exc}") from exc
    updates: dict[str, Any] = {"tripo_api_key": key}
    if payload.get("make_default"):
        updates |= {"default_3d_provider": "tripo", "default_3d_service_model": ""}
    save_config(updates)
    return _tripo_status(balance)


@app.delete("/api/tripo")
def api_tripo_disconnect() -> dict[str, Any]:
    cfg = save_config({"tripo_api_key": "", **({"default_3d_provider": "meshy"} if load_config().default_3d_provider == "tripo" else {})})
    return _tripo_status() | {"connected": bool(cfg.get_key("tripo"))}  # still true when the key comes from the environment


@app.post("/api/tripo/process")
async def api_tripo_process(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    """Retopo, re-texture or convert a GLB with Tripo. The result is saved with the asset's 3D models."""
    try:
        created = await game_tools.tripo_process(_ws_dir(project), str(payload.get("asset") or ""), str(payload.get("model") or ""),
                                                 str(payload.get("op") or ""), payload.get("options") or {})
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await workspace_changed(project)
    return {"created": created}


@app.get("/api/studio/export")
def api_studio_export(path: str = "", engine: str = "godot", project: str = "default") -> Response:
    data, name = _overview_call(game_tools.export_zip, _ws_dir(project), path, engine)
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/studio/jobs")
def api_studio_jobs(project: str = "default") -> dict[str, Any]:
    _load_jobs(project)
    return {"jobs": [job for job in studio_jobs.values() if job["project"] == project][-30:]}


_cli_checks: set[str] = set()


async def _check_cli_once(cli_id: str) -> None:
    try:
        await cli_models.check(cli_id)
    finally:
        _cli_checks.discard(cli_id)
    await ws_manager.broadcast({"type": "models_changed"})


@app.get("/api/agent/models")
async def api_agent_models() -> dict[str, Any]:
    cfg = load_config()
    # Finding the CLIs and the editor's models touches the disk: off the event loop, so the rest of Glob stays quick.
    clis, models = await asyncio.to_thread(lambda: (cli_models.detect(), list_agent_models()))
    # Installed coding agents nobody has checked yet: test their login in the background, so "connected" is true.
    for cli in clis:
        if cli["status"] == "unknown" and cli["id"] not in _cli_checks and "PYTEST_CURRENT_TEST" not in os.environ:
            _cli_checks.add(cli["id"])
            asyncio.create_task(_check_cli_once(cli["id"]))
    return {
        "models": models,
        "selected": cfg.agent_model,
        "effort": cfg.agent_reasoning_effort,
        "presets": model_endpoints.PRESETS,
        "endpoints": [model_endpoints.public(e) for e in model_endpoints.load_endpoints()],
        "clis": clis,
    }


@app.post("/api/agent/cli/{cli_id}/check")
async def api_cli_check(cli_id: str) -> dict[str, Any]:
    if cli_id not in cli_models.CLIS:
        raise HTTPException(status_code=404, detail=f"Unknown coding agent '{cli_id}'.")
    return await cli_models.check(cli_id)


@app.post("/api/agent/cli/{cli_id}/login")
def api_cli_login(cli_id: str) -> dict[str, Any]:
    """Open a terminal with the coding agent's login (or install command when it isn't installed yet)."""
    if cli_id not in cli_models.CLIS:
        raise HTTPException(status_code=404, detail=f"Unknown coding agent '{cli_id}'.")
    try:
        cli_models.open_login(cli_id)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"opened": cli_id}


@app.post("/api/agent/cli/{cli_id}/logout")
async def api_cli_logout(cli_id: str) -> dict[str, Any]:
    """Sign a coding agent out, so it can be logged in again."""
    if cli_id not in cli_models.CLIS:
        raise HTTPException(status_code=404, detail=f"Unknown coding agent '{cli_id}'.")
    try:
        result = await asyncio.to_thread(cli_models.logout, cli_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await ws_manager.broadcast({"type": "models_changed"})
    return result


def _endpoint_call(fn, *args: Any) -> Any:
    try:
        return fn(*args)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agent/endpoints/probe")
async def api_probe_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    preset = model_endpoints.PRESETS.get(str(payload.get("preset") or "custom"), model_endpoints.PRESETS["custom"])
    try:
        models = await model_endpoints.probe(str(payload.get("base_url") or preset["base_url"]), str(payload.get("api_key") or ""), preset["kind"])
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Could not reach the endpoint.") from exc
    return {"models": models}


@app.post("/api/agent/endpoints")
def api_add_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    return _endpoint_call(model_endpoints.add_endpoint, payload)


@app.delete("/api/agent/endpoints/{endpoint_id}")
def api_remove_endpoint(endpoint_id: str) -> dict[str, Any]:
    model_endpoints.remove_endpoint(endpoint_id)
    return {"removed": endpoint_id}


@app.post("/api/agent/test")
async def api_test_model(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await call_agent_model(prompt="Reply with exactly: ok", system_prompt="You are a connection test.", model_selector=str(payload.get("model") or ""), effort="low")
    except Exception as exc:  # noqa: BLE001 — report any failure to the settings card
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    return {"ok": True, "reply": result.get("text", "")[:80]}


# --- Forge Agent dock ---

def _agent_call(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/agent/sessions")
def api_agent_sessions(project: str = "default") -> dict[str, Any]:
    cfg = load_config()
    return {
        "sessions": workspace_agent.list_sessions(project),
        "default_effort": cfg.agent_reasoning_effort,
    }


@app.post("/api/agent/sessions")
def api_agent_create_session(payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    cfg = load_config()
    return workspace_agent.create_session(
        project,
        title=str(payload.get("title") or "New chat"),
        group=str(payload.get("group") or "General"),
        model=payload.get("model") or "",
        effort=payload.get("effort") or cfg.agent_reasoning_effort,
        agent=payload.get("agent") or "glob",
    )


@app.get("/api/agent/sessions/{session_id}")
def api_agent_get_session(session_id: str, project: str = "default") -> dict[str, Any]:
    session = _agent_call(workspace_agent.get_session, project, session_id)
    return {**session, "context_tokens": workspace_agent.context_tokens(session)}


@app.patch("/api/agent/sessions/{session_id}")
def api_agent_update_session(session_id: str, payload: dict[str, Any], project: str = "default") -> dict[str, Any]:
    return _agent_call(workspace_agent.update_session, project, session_id, payload)


@app.delete("/api/agent/sessions/{session_id}")
def api_agent_delete_session(session_id: str, project: str = "default") -> dict[str, Any]:
    return {"trashed": _agent_call(workspace_agent.delete_session, project, session_id)}


@app.post("/api/agent/sessions/{session_id}/messages")
async def api_agent_send(session_id: str, payload: dict[str, Any], project: str = "default", request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    # While the model works, tell the chat what it is doing (thinking, or writing and how much), so the wait isn't blind.
    loop = asyncio.get_running_loop()
    started = time.time()
    steps: list[dict[str, Any]] = []
    latest: dict[str, Any] = {}
    finished = asyncio.Event()

    async def deliver(message: dict[str, Any]) -> None:
        # Reports are queued: one still waiting when "done" goes out must not switch the chat back to "working".
        if not finished.is_set():
            await ws_manager.broadcast(message)

    def report(info: dict[str, Any]) -> None:
        # Every message carries the steps so far, so a chat opened or reloaded mid-run catches up at once.
        info = dict(info)
        step, step_done = info.pop("step", None), info.pop("step_done", None)
        if step:
            steps.append(dict(step))
        if step_done and steps:
            # Several tools can run at once; an id says which one finished.
            done_id = step_done.get("id")
            target = next((s for s in reversed(steps) if s.get("id") == done_id), steps[-1]) if done_id else steps[-1]
            target.update(step_done)
        latest.update(info)
        message = {"type": "agent_progress", "project": project, "session": session_id, "started": started, **latest, "steps": [dict(s) for s in steps[-40:]]}
        asyncio.run_coroutine_threadsafe(deliver(message), loop)

    token = cli_models.progress_hook.set(report)
    try:
        session = await workspace_agent.send(
            project,
            session_id,
            str(payload.get("text") or ""),
            attachments=payload.get("attachments") or [],
            refs=payload.get("refs") or [],
            page=payload.get("page") or {},
            model=payload.get("model") or None,
            effort=payload.get("effort") or None,
            mcp_base=str(request.base_url) if request else "",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        cli_models.progress_hook.reset(token)
        finished.set()
        await ws_manager.broadcast({"type": "agent_progress", "project": project, "session": session_id, "done": True})
    if any(action.get("changed") for action in session["messages"][-1].get("actions") or []):
        await workspace_changed(project)
    return session


@app.post("/api/agent/sessions/{session_id}/stop")
def api_agent_stop(session_id: str) -> dict[str, Any]:
    return {"stopped": workspace_agent.stop_run(session_id)}


@app.post("/api/agent/mcp")
async def api_agent_mcp(rpc: dict[str, Any], project: str = "default", session: str = "") -> Any:
    """Glob's own tools for a chat running as the real CLI (forge.agent.cli_runner)."""
    reply = await cli_runner.handle_rpc(workspace_agent, project, session, rpc)
    return reply if reply is not None else Response(status_code=202)


@app.post("/api/agent/sessions/{session_id}/compact")
async def api_agent_compact(session_id: str, project: str = "default") -> dict[str, Any]:
    session = _agent_call(workspace_agent.get_session, project, session_id)
    return await workspace_agent.compact(project, session, force=True)


@app.post("/api/agent/sessions/{session_id}/handoff")
async def api_agent_handoff(session_id: str, project: str = "default") -> dict[str, Any]:
    try:
        return await workspace_agent.handoff(project, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc


# --- Agent Brief & MCP Endpoints ---

@app.post("/api/agent/brief")
async def api_agent_brief(req: AgentBriefRequest) -> dict[str, Any]:
    return await execute_agent_brief(
        action_layer=actions,
        brief=req.brief,
        parent_asset_id=req.parent_asset_id,
        project=req.project,
        dry_run=req.dry_run,
        approve_checkpoint=req.approve_checkpoint,
    )


@app.get("/api/mcp/tools")
def api_mcp_tools() -> dict[str, Any]:
    return {"tools": advertised_tools()}


@app.post("/api/mcp")
async def api_mcp_rpc(request: dict[str, Any]) -> dict[str, Any]:
    if not load_config().mcp_http_enabled:
        raise HTTPException(status_code=403, detail="The MCP HTTP endpoint is turned off in Settings.")
    resp = await mcp_dispatcher.handle_jsonrpc(request)
    params = request.get("params") or {}
    if request.get("method") == "tools/call" and params.get("name") in MUTATING_TOOLS:
        project = str((params.get("arguments") or {}).get("project") or "") or default_project()
        await workspace_changed(project)
    return resp or {"jsonrpc": "2.0", "result": "ok"}


# --- Config & Host-Restricted Proxy ---

@app.get("/api/config")
def api_get_config() -> dict[str, Any]:
    return load_config().masked_dict()


@app.put("/api/config")
def api_update_config(updates: dict[str, Any]) -> dict[str, Any]:
    locked = LOCKED_CONFIG_KEYS & updates.keys()
    if locked:
        raise HTTPException(status_code=403, detail=f"{', '.join(sorted(locked))} can only be changed in ~/.forge/config.json.")
    cfg = save_config(updates)
    return cfg.masked_dict()


# --- Coding-agent logins ---

@app.get("/api/auth/status")
def api_auth_status() -> dict[str, Any]:
    return get_all_auth_status()


@app.post("/api/auth/codex/switch")
def api_codex_switch_profile(payload: dict[str, Any]) -> dict[str, Any]:
    prof = str(payload.get("profile", "config.toml"))
    try:
        return switch_codex_profile(prof)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/proxy")
async def api_safe_proxy(url: str = Query(...)) -> Response:
    """Host-restricted proxy for external provider thumbnails/meshes (Section 9)."""
    cfg = load_config()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in cfg.allowed_proxy_hosts:
        raise HTTPException(
            status_code=403,
            detail=f"Host '{parsed.hostname}' is not in allowed_proxy_hosts.",
        )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/octet-stream"),
        )


# --- Project Asset & Export File Serving ---

@app.api_route("/projects/{project}/{file_path:path}", methods=["GET", "HEAD"])  # the 3D viewer asks HEAD for a file's size
def serve_project_file(request: Request, project: str, file_path: str) -> Response:
    store = actions.get_store(project)
    try:
        abs_path = store.resolve_safe_path(file_path)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not abs_path.exists() or not abs_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = None
    if abs_path.suffix.lower() == ".glb":
        media_type = "model/gltf-binary"
    elif abs_path.suffix.lower() == ".obj":
        media_type = "text/plain"
    # Tools replace files under the same name (a rebuilt kit, material maps): always ask whether it changed.
    sandbox = {"Content-Security-Policy": "sandbox allow-scripts allow-pointer-lock allow-popups"}
    if abs_path.suffix.lower() in (".html", ".htm") and "prototype" in Path(file_path).parts[:-1] and request.method == "GET":
        # The Prototype: its pictures and sounds are put into the page, so the sandboxed page needs no further requests.
        from forge import prototype as _prototype

        body = _prototype.inline_assets(abs_path, _ws_dir(project))
        return Response(body, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache", **sandbox})
    if abs_path.suffix.lower() in (".html", ".htm", ".svg"):
        # A page an agent wrote runs with no access to Glob's own pages or API, even if opened in its own tab.
        return FileResponse(abs_path, media_type=media_type, headers={"Cache-Control": "no-cache", **sandbox})
    return FileResponse(abs_path, media_type=media_type, headers={"Cache-Control": "no-cache"})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# --- Serve UI Static Files ---

class NoCacheStaticFiles(StaticFiles):
    """Always revalidate UI assets so code changes show up without hard-refreshes."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


if UI_DIR.exists():
    app.mount("/ui", NoCacheStaticFiles(directory=str(UI_DIR)), name="ui")


@app.get("/favicon.ico", include_in_schema=False)
def serve_favicon() -> FileResponse:
    # Browsers ask for /favicon.ico whatever the page's <link rel="icon"> says.
    return FileResponse(UI_DIR / "glob-icon.png", media_type="image/png")


@app.get("/", response_class=HTMLResponse)
def serve_index() -> HTMLResponse:
    idx = UI_DIR / "index.html"
    if idx.exists():
        response = HTMLResponse(idx.read_text(encoding="utf-8"))
        response.headers["Cache-Control"] = "no-cache"
        return response
    # Not "loading": the page files are gone, so the app can't show itself. Usually a partial or interrupted install.
    return HTMLResponse(
        "<!doctype html><meta charset=\"utf-8\"><title>Glob</title>"
        "<h1>Glob's app files are missing</h1>"
        "<p>This copy of Glob is incomplete (its <code>ui</code> folder is not there). Quit Glob, then reinstall it "
        "from the latest release on GitHub.</p>",
        status_code=503,
    )

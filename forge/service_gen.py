"""Generating in the studio with a service the user signed in to (ElevenLabs, Higgsfield), not an API key.

A studio model id `svc:<service>/<model>` runs that service's own generate tools over MCP: reference images are
uploaded to the service first, the job is polled until it finishes, and the result is downloaded into Glob like
any other generation. Each service has its own tools, so each has a small adapter here. Results are read
loosely (ids and URLs are searched for by key), since the services only document their inputs.
"""

from __future__ import annotations

import mimetypes
import threading
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import httpx

from forge import services

PREFIX = "svc:"
POLL_LIMIT_S = 900
CATALOG_TTL_S = 1800
CONTEXT = "Glob's asset studio generates a game asset the user asked for."
# ElevenLabs utilities that are not text-to-image models.
SKIP_MODELS = {"birefnet-v2-bg-removal", "topaz-image-upscale-v2"}
NOT_GENERATORS = ("remover", "upscale", "topaz", "outpaint", "decompose", "image_command", "autosprite")
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".webp", ".glb", ".gltf", ".fbx", ".obj", ".usdz", ".mp3", ".wav", ".ogg", ".flac", ".m4a"}
# Sound kinds a signed-in service can make: ElevenLabs flow node type and the model used when the service doesn't say.
AUDIO_NODES = {"sfx": ("sfx", "eleven_text_to_sound_v2"), "tts": ("tts", "eleven_v4"), "music": ("music", "eleven_music_v2_5")}
AUDIO_SERVICES = ("elevenlabs",)

_catalog: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}


class ServiceGenError(RuntimeError):
    pass


# ---------- reading loosely shaped results ----------

def _walk(obj: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _first(obj: Any, *keys: str) -> Any:
    return next((v for k, v in _walk(obj) if k in keys and v not in (None, "", [])), None)


def _media_urls(obj: Any) -> list[str]:
    """Result URLs: values of url-ish keys first, then any https link to an image or model file."""
    keyed = [v for k, v in _walk(obj) if isinstance(v, str) and v.startswith("https://") and k in (
        "full_res_url", "result_url", "glb_url", "model_url", "output_url", "image_url", "download_url", "url")]
    loose = [v for _, v in _walk(obj) if isinstance(v, str) and v.startswith("https://") and Path(urlsplit(v).path).suffix.lower() in MEDIA_EXT]
    skip = ("/app/flows/", "upload")  # canvas links and upload slots are not results
    return list(dict.fromkeys(u for u in keyed + loose if not any(s in u for s in skip)))


def _call(service: str, tool: str, arguments: dict[str, Any]) -> Any:
    return services.call_tool_data(service, tool, arguments)


def _put(url: str, path: Path, mime: str) -> None:
    resp = httpx.put(url, content=path.read_bytes(), headers={"Content-Type": mime}, timeout=120)
    if resp.status_code >= 300:
        raise ServiceGenError(f"Uploading {path.name} failed ({resp.status_code}).")


def _download(url: str, out_dir: Path, stem: str) -> Path:
    with httpx.Client(follow_redirects=True, timeout=300) as client:
        resp = client.get(url)
        resp.raise_for_status()
    ext = Path(urlsplit(url).path).suffix.lower()
    if ext not in MEDIA_EXT:
        ext = mimetypes.guess_extension(resp.headers.get("content-type", "").split(";")[0].strip()) or ".png"
        ext = {".jpe": ".jpg"}.get(ext, ext)
    target = out_dir / f"{stem}{ext}"
    target.write_bytes(resp.content)
    return target


# ---------- model lists ----------

def _pretty(model: str) -> str:
    words = model.replace("_", "-").split("-")
    return " ".join(w.upper() if w in ("gpt", "glb", "sam", "3d") else w.capitalize() for w in words)


def catalog(kind: str) -> list[dict[str, Any]]:
    """Models of each signed-in service for `kind` ("image" or "3d"), as studio model entries."""
    out = []
    for service in services.connected():
        cached = _catalog.get((service, kind))
        if cached and time.time() - cached[0] < CATALOG_TTL_S:
            out += cached[1]
            continue
        try:
            models = _service_models(service, kind)
        except (services.ServiceError, httpx.HTTPError, ServiceGenError):
            models = []
        _catalog[(service, kind)] = (time.time(), models)
        out += models
    return out


def cached_catalog(kind: str) -> list[dict[str, Any]]:
    """What is already cached, without waiting on the services; refreshes in the background when stale."""
    out, stale = [], False
    for service in services.connected():
        cached = _catalog.get((service, kind))
        out += cached[1] if cached else []
        stale = stale or not cached or time.time() - cached[0] >= CATALOG_TTL_S
    if stale:
        threading.Thread(target=catalog, args=(kind,), daemon=True).start()
    return out


def _service_models(service: str, kind: str) -> list[dict[str, Any]]:
    label = services.SERVICES[service]["label"]
    entry = lambda mid, name, **extra: {"id": f"{PREFIX}{service}/{mid}", "label": f"{label} · {name}", "service": service,
                                        "price": None, "available": True, "via": "signed in", **extra}
    if service == "elevenlabs" and kind == "image":
        schema = services.tool_schema(service, "creative_generate_image")["inputSchema"]
        enum = (schema.get("properties", {}).get("model_id") or {}).get("enum") or []
        return [entry(m, _pretty(m)) for m in enum if m not in SKIP_MODELS]
    if service == "higgsfield" and kind in ("image", "3d"):
        items = [i for i in (_call(service, "models_explore", {"action": "list", "type": kind, "limit": 100}) or {}).get("items", []) if i.get("id")]
        if kind == "image":
            # Tools that rework a finished picture (cut-outs, upscales, outpainting) can't make an asset from a prompt.
            items = [i for i in items if not any(w in i["id"] for w in NOT_GENERATORS)]
        if kind == "3d":
            # Only models that turn pictures into a mesh: rigging, remesh and retexture take a finished model.
            items = [i for i in items if any(m.get("type") == "image" for m in i.get("medias") or []) and not any(w in i["id"] for w in ("rig", "remesh", "retexture"))]
        names = [i.get("name") or _pretty(i["id"]) for i in items]
        return [entry(item["id"], f"{name} ({item['id']})" if names.count(name) > 1 else name,
                      roles=[r for m in item.get("medias") or [] for r in m.get("roles") or []],
                      max_refs=sum(int(m.get("max") or 1) for m in item.get("medias") or []), aspect_ratios=item.get("aspect_ratios") or [])
                for item, name in zip(items, names)]
    return []


def _model_info(service: str, model: str, kind: str) -> dict[str, Any]:
    return next((m for m in catalog(kind) if m["id"] == f"{PREFIX}{service}/{model}"), {})


def parse(model_id: str) -> tuple[str, str]:
    service, _, model = model_id.removeprefix(PREFIX).partition("/")
    if service not in services.SERVICES or not model:
        raise ServiceGenError(f"Unknown service model '{model_id}'.")
    if service not in services.connected():
        raise ServiceGenError(f"Sign in to {services.SERVICES[service]['label']} first (Settings → Generators).")
    return service, model


# ---------- generating ----------

def generate(model_id: str, kind: str, prompt: str, references: list[Path], out_dir: Path, stem: str,
             aspect_ratio: str = "", transparent: bool = False) -> Path:
    """Run one generation and download its file. `transparent` runs the service's own background remover after
    the image (a second, small charge), since the image models themselves paint a background."""
    service, model = parse(model_id)
    if service == "elevenlabs" and kind == "image":
        urls = _elevenlabs_image(model, prompt, references, aspect_ratio, transparent)
    elif service == "higgsfield":
        urls = _higgsfield(model, kind, prompt, references, aspect_ratio, transparent and kind == "image")
    else:
        raise ServiceGenError(f"{services.SERVICES[service]['label']} can't make {kind} assets from the studio yet.")
    if not urls:
        raise ServiceGenError(f"{services.SERVICES[service]['label']} finished but returned no file.")
    return _download(urls[0], out_dir, stem)


def generate_audio(service: str, mode: str, prompt: str, out_dir: Path, stem: str, settings: dict[str, Any] | None = None) -> Path:
    """A sound effect, spoken line or music track from a signed-in service, downloaded like any other output."""
    if service not in AUDIO_SERVICES or mode not in AUDIO_NODES:
        raise ServiceGenError(f"{services.SERVICES.get(service, {}).get('label', service)} can't make {mode} sounds from the studio yet.")
    if service not in services.connected():
        raise ServiceGenError(f"Sign in to {services.SERVICES[service]['label']} first (Settings → Generators).")
    settings = settings or {}
    params: dict[str, Any] = {}
    if mode == "tts":
        params["voice_id"] = str(settings.get("voice_id") or "") or default_voice()
    else:
        if settings.get("duration"):
            params["duration_seconds"] = float(settings["duration"])
        if mode == "sfx":
            params["loop"] = bool(settings.get("loop"))
            if settings.get("prompt_influence") is not None:
                params["prompt_influence"] = float(settings["prompt_influence"])
        if mode == "music":
            params["instrumental"] = not settings.get("vocals")  # game beds are instrumental unless asked
    svc = "elevenlabs"
    node_type, fallback_model = AUDIO_NODES[mode]
    flow_id = _first(_call(svc, "creative_create_flow", {"context": CONTEXT}), "flow_id")
    node = _call(svc, "creative_add_flow_node", {"flow_id": flow_id, "node_type": node_type, "model_id": _default_model(node_type) or fallback_model,
                                                 "prompt": prompt, "model_parameters": params, "label": "Glob", "context": CONTEXT})
    urls = _elevenlabs_run(flow_id, _first(node, "node_id", "id"))
    if not urls:
        raise ServiceGenError("ElevenLabs finished but returned no sound.")
    return _download(urls[0], out_dir, stem)


_node_models: dict[str, str] = {}


def _default_model(node_type: str) -> str:
    """The service's own default model for a node type, asked once."""
    if not _node_models:
        try:
            listing = _call("elevenlabs", "creative_get_flow_node_types", {"context": CONTEXT}) or {}
            _node_models.update({t.get("type"): t.get("default_model_id") or "" for t in listing.get("node_types") or []})
        except (ServiceGenError, services.ServiceError, httpx.HTTPError):
            return ""
    return _node_models.get(node_type, "")


def list_voices(search: str = "") -> list[dict[str, Any]]:
    """Voices of the signed-in ElevenLabs account, shaped like the API-key voice list."""
    args: dict[str, Any] = {"context": CONTEXT}
    if search:
        args["search"] = search
    data = _call("elevenlabs", "creative_list_voices", args) or {}
    return [{"id": v.get("voice_id", ""), "name": v.get("name", ""), "category": v.get("category", ""), "preview_url": v.get("preview_url", "")}
            for v in data.get("voices") or [] if v.get("voice_id")]


def default_voice() -> str:
    voices = list_voices()
    if not voices:
        raise ServiceGenError("ElevenLabs has no voices to speak with. Pick a voice on the asset.")
    return voices[0]["id"]


def _poll(check, what: str) -> Any:
    deadline = time.time() + POLL_LIMIT_S
    while True:
        done, result, wait = check()
        if done:
            return result
        if time.time() > deadline:
            raise ServiceGenError(f"{what} took too long. It may still finish in your account.")
        time.sleep(max(2.0, min(float(wait or 5), 15.0)))


_el_params: dict[str, dict[str, list[str]]] = {}


def _elevenlabs_options(model: str) -> dict[str, list[str]]:
    """A model's settings and their allowed values, e.g. {"aspect_ratio": ["1:1", "16:9", ...]}."""
    if model not in _el_params:
        schema = _call("elevenlabs", "creative_get_model_schema", {"node_type": "image-generation", "model_id": model, "context": CONTEXT})
        _el_params[model] = {str(p.get("name")): [str(o.get("value")) for o in p.get("options") or []]
                             for p in (schema or {}).get("parameters") or [] if p.get("name")}
    return _el_params[model]


def _elevenlabs_run(flow_id: str, node_id: str) -> list[str]:
    """Run one node of a flow, wait for it and return its result URLs."""
    svc = "elevenlabs"
    run = _call(svc, "creative_run_flow_nodes", {"flow_id": flow_id, "node_ids": [node_id], "generations_count": 1, "context": CONTEXT})
    sessions = _first(run, "session_ids") or [v for k, v in _walk(run) if k == "session_id" and isinstance(v, str)]
    if not sessions:
        raise ServiceGenError(f"ElevenLabs did not start the run: {str(run)[:300]}")

    def check():
        status = _call(svc, "creative_get_flow_run_status", {"flow_id": flow_id, "session_ids": sessions, "context": CONTEXT})
        if _first(status, "has_failures"):
            raise ServiceGenError(f"ElevenLabs could not make it: {str(_first(status, 'error', 'message', 'failure_reason') or status)[:300]}")
        return bool(_first(status, "all_completed")), status, _first(status, "poll_after_seconds")

    status = _poll(check, "The ElevenLabs run")
    gen_ids = [v for k, v in _walk(status) if k in ("generation_id", "generation_ids") for v in (v if isinstance(v, list) else [v]) if isinstance(v, str)]
    if gen_ids:
        urls = _media_urls(_call(svc, "creative_get_generations", {"flow_id": flow_id, "generation_ids": gen_ids[:4], "context": CONTEXT}))
        if urls:
            return urls
    return _media_urls(status)


def _elevenlabs_image(model: str, prompt: str, references: list[Path], aspect_ratio: str, transparent: bool) -> list[str]:
    # A flow node rather than creative_generate_image: only a node takes model settings, and without them
    # ElevenLabs paints 16:9.
    svc = "elevenlabs"
    flow_id = _first(_call(svc, "creative_create_flow", {"context": CONTEXT}), "flow_id")
    refs = []
    for ref in references[:8]:
        mime = mimetypes.guess_type(ref.name)[0] or "image/png"
        slot = _call(svc, "creative_create_asset_upload", {"name": ref.name, "mime_type": mime, "file_size": ref.stat().st_size, "context": CONTEXT})
        _put(_first(slot, "upload_url"), ref, mime)
        refs.append(_first(_call(svc, "creative_finalize_asset_upload", {"asset_id": _first(slot, "asset_id"), "flow_id": flow_id, "context": CONTEXT}), "node_id"))
    options = _elevenlabs_options(model)
    settings = {"aspect_ratio": aspect_ratio} if aspect_ratio and aspect_ratio in options.get("aspect_ratio", []) else {}
    node = _call(svc, "creative_add_flow_node", {"flow_id": flow_id, "node_type": "image-generation", "model_id": model, "prompt": prompt,
                                                 "model_parameters": settings, "connect_from": [r for r in refs if r], "label": "Glob", "context": CONTEXT})
    urls = _elevenlabs_run(flow_id, _first(node, "node_id", "id"))
    if transparent and urls:
        # The remover is an image-generation model here (this surface can't create image-transform nodes).
        image_node = _first(node, "node_id", "id")
        urls = _keep_on_failure(urls, lambda: _elevenlabs_run(flow_id, _first(_call(svc, "creative_add_flow_node", {
            "flow_id": flow_id, "node_type": "image-generation", "model_id": "birefnet-v2-bg-removal",
            "connect_from": [image_node], "label": "Glob · cut out", "context": CONTEXT}), "node_id", "id")))
    return urls


def _keep_on_failure(urls: list[str], cut_out) -> list[str]:
    """The cut-out is a second step: if it fails, keep the image already paid for, uncut."""
    try:
        return cut_out() or urls
    except (ServiceGenError, services.ServiceError, httpx.HTTPError):
        return urls


def _higgsfield_upload(ref: Path) -> str:
    svc = "higgsfield"
    mime = mimetypes.guess_type(ref.name)[0] or "image/png"
    slot = _call(svc, "media_upload", {"filename": ref.name, "content_type": mime})
    _put(_first(slot, "upload_url"), ref, mime)
    media_id = _first(slot, "media_id", "id")
    _call(svc, "media_confirm", {"type": "image", "media_id": media_id})
    return str(media_id)


def _higgsfield(model: str, kind: str, prompt: str, references: list[Path], aspect_ratio: str, transparent: bool = False) -> list[str]:
    svc = "higgsfield"
    info = _model_info(svc, model, kind)
    roles = info.get("roles") or ["image"]
    params: dict[str, Any] = {"model": model, "count": 1}
    if kind == "image":
        params["prompt"] = prompt
        if aspect_ratio and aspect_ratio in (info.get("aspect_ratios") or []):
            params["aspect_ratio"] = aspect_ratio
    elif model == "sam_3_3d":
        params["prompt"] = prompt
    refs = references[: max(1, int(info.get("max_refs") or 1))] if references else []
    if kind == "3d" and not refs:
        raise ServiceGenError("Making 3D needs a source image.")
    if refs:
        params["medias"] = [{"value": _higgsfield_upload(ref), "role": roles[min(i, len(roles) - 1)]} for i, ref in enumerate(refs)]
    job_id, urls = _higgsfield_start_and_wait(kind, params)
    if transparent and urls:
        # The remover takes the finished job as its input.
        urls = _keep_on_failure(urls, lambda: _higgsfield_start_and_wait("image", {"model": "image_background_remover", "count": 1, "medias": [{"value": job_id, "role": "image_references"}]})[1])
    return urls


def _higgsfield_start_and_wait(kind: str, params: dict[str, Any]) -> tuple[str, list[str]]:
    """Start one Higgsfield job and wait for it: (job id, result URLs)."""
    svc = "higgsfield"
    if kind == "image":
        started = _call(svc, "generate_image_batch", {"requests": [params], "context": CONTEXT})
    else:
        started = _call(svc, "generate_3d", {"params": params})
    job_id = _first(started, "job_id", "jobId", "id")
    if not job_id:
        raise ServiceGenError(f"Higgsfield did not start the job: {str(started)[:300]}")

    def check():
        if kind == "image":
            status = _call(svc, "jobs_wait", {"jobs": [{"index": 0, "job_id": job_id}], "timeout_seconds": 15})
        else:
            status = _call(svc, "job_status", {"jobId": job_id})
        state = str(_first(status, "status", "state") or "").lower()
        if state in ("failed", "error", "nsfw", "canceled", "cancelled"):
            raise ServiceGenError(f"Higgsfield could not finish it: {str(_first(status, 'error', 'message', 'reason') or state)[:300]}")
        return bool(_first(status, "all_terminal")) or state in ("completed", "succeeded", "done"), status, _first(status, "poll_after_seconds") or 2

    return str(job_id), _media_urls(_poll(check, "The Higgsfield job"))

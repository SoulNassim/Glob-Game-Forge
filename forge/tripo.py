"""Tripo (v3 API): one small async client shared by the 3D provider and the game tools.

Tripo has no OAuth or CLI login: an account is connected with an API key from the Tripo Console
(https://platform.tripo3d.ai/api-keys). `connect()` checks the key against the balance endpoint, so a bad key
fails at once and the Settings card can show the credits that are left.

Every job is the same shape: POST a task, get a `task_id`, poll `GET /tasks/{id}` until it is `success`.
Source models and images are passed as `input`: a `task_id` of an earlier Tripo task, a `file_token` from
`POST /files`, or a public URL. Reference: https://developers.tripo3d.com/en/docs/introduction
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from forge.providers.base import ProviderError

# Tripo runs two regions that each issue their own keys: keys made on platform.tripo3d.ai belong to the .ai host,
# the others to .com. A key is only valid on its own region, so Connect tries both and remembers the one that answered.
HOSTS = ("https://openapi.tripo3d.com/v3", "https://openapi.tripo3d.ai/v3")
BASE_URL = HOSTS[0]
PROVIDER = "tripo"
USD_PER_CREDIT = 0.01  # Tripo's price for credits on a plan runs 1-1.2 cents; used only for estimates

# Models, newest first. The H series is the general one; the P series is tuned for low-poly output.
MODELS: dict[str, str] = {
    "v3.1-20260211": "Tripo 3.1 · best detail",
    "v3.0-20250812": "Tripo 3.0",
    "v2.5-20250123": "Tripo 2.5 · fastest",
    "P1-20260311": "Tripo P1 · low-poly",
    "P2-20260801": "Tripo P2 · low-poly with quads",
}
DEFAULT_MODEL = "v3.1-20260211"
TEXTURE_QUALITIES = ("fast", "standard", "detailed", "extreme")
GEOMETRY_QUALITIES = ("standard", "detailed")

# What each endpoint takes, by model. `generation_fields()` filters a settings dict down to these.
_COMMON = {"texture", "pbr", "face_limit", "model_seed", "texture_seed", "orientation", "texture_alignment", "export_uv",
           "enable_image_autofix"}
_FROM_V3 = {"texture_quality", "texture_version", "delight", "auto_size", "export_orientation"}
_H_ONLY_V3 = {"geometry_quality", "quad", "smart_low_poly", "generate_parts", "compress"}
_BOOLEANS = {"texture", "pbr", "quad", "smart_low_poly", "generate_parts", "auto_size", "export_uv", "enable_image_autofix", "delight"}
_INTEGERS = {"face_limit", "model_seed", "texture_seed", "image_seed"}

ENDPOINTS = {"text": "/generation/text-to-model", "image": "/generation/image-to-model", "multiview": "/generation/multiview-to-model"}
VIEW_KEYS = ("front", "left", "back", "right")

# Credits are estimates (Tripo bills what the task really used, which `credits_consumed` reports afterwards).
CREDITS = {"text": 30.0, "image": 40.0, "multiview": 40.0, "rig": 25.0, "retarget": 10.0, "convert": 10.0, "decimate": 35.0, "texture": 20.0}

FAILED = {"failed", "cancelled", "banned", "expired"}

# Animation presets for rigs made with Tripo's v2.5+ rigger (biped unless the name says otherwise).
ANIMATIONS: list[dict[str, str]] = [
    *({"id": f"preset:{n}", "name": n.title(), "category": "Biped", "sub_category": "", "rig_type": "biped"}
      for n in ("idle", "walk", "run", "jump", "fall", "turn", "climb", "dive", "slash", "shoot", "hurt")),
    {"id": "preset:quadruped:walk", "name": "Quadruped walk", "category": "Creature", "sub_category": "quadruped", "rig_type": "quadruped"},
    {"id": "preset:hexapod:walk", "name": "Hexapod walk", "category": "Creature", "sub_category": "hexapod", "rig_type": "hexapod"},
    {"id": "preset:octopod:walk", "name": "Octopod walk", "category": "Creature", "sub_category": "octopod", "rig_type": "octopod"},
    {"id": "preset:serpentine:march", "name": "Serpent slither", "category": "Creature", "sub_category": "serpentine", "rig_type": "serpentine"},
    {"id": "preset:aquatic:march", "name": "Aquatic swim", "category": "Creature", "sub_category": "aquatic", "rig_type": "aquatic"},
]
RIG_TYPES = ("biped", "quadruped", "hexapod", "octopod", "avian", "serpentine", "aquatic", "others")
RIG_SPECS = ("tripo", "mixamo", "actorcore", "unity", "unreal", "vrm")
MAX_ANIMATIONS = 5  # per retarget task


def headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def check(resp: httpx.Response, what: str) -> None:
    """Raise a ProviderError that says what to do about it."""
    code = resp.status_code
    if code == 401:
        raise ProviderError("Tripo says the API key is not valid (401).", provider=PROVIDER, status_code=401,
                            how_to_fix="Paste a fresh key in Settings → Accounts → Tripo.")
    if code in (402, 403):
        raise ProviderError(f"Tripo refused the {what} request ({code}): not enough credits, or not allowed on this plan.",
                            provider=PROVIDER, status_code=code, how_to_fix="Top up your Tripo credits at platform.tripo3d.ai.")
    if code == 429 or code >= 500:
        raise ProviderError(f"Tripo is busy or rate limiting ({code}).", provider=PROVIDER, status_code=code, retryable=True)
    if code >= 400:
        raise ProviderError(f"Tripo rejected the {what} request ({code}): {resp.text[:240]}", provider=PROVIDER, status_code=code)


def _body(resp: httpx.Response, what: str) -> dict[str, Any]:
    check(resp, what)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise ProviderError(f"Tripo sent something unreadable for the {what} request.", provider=PROVIDER, status_code=502) from exc
    if not isinstance(payload, dict) or payload.get("code", 0) != 0:
        raise ProviderError(f"Tripo could not do the {what} request: {str(payload)[:240]}", provider=PROVIDER, status_code=502)
    return payload.get("data") or {}


# ---------- account ----------

def base_url() -> str:
    """The region this key was connected on (saved by Connect), else the default."""
    from forge.config import load_config

    saved = str(getattr(load_config(), "tripo_base_url", "") or "")
    return saved if saved in HOSTS else BASE_URL


async def balance(key: str, base: str = "") -> dict[str, float]:
    """`{"balance": credits you can spend, "frozen": credits held by running tasks}`."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        data = _body(await client.get(f"{base or base_url()}/account/balance", headers=headers(key)), "balance")
    return {"balance": float(data.get("balance") or 0), "frozen": float(data.get("frozen") or 0)}


async def connect(key: str) -> tuple[dict[str, float], str]:
    """Check a pasted key on each Tripo region. Returns the balance and the host that accepted it; raises
    ProviderError when neither does."""
    key = key.strip()
    if not key:
        raise ProviderError("Paste your Tripo API key first.", provider=PROVIDER, status_code=400)
    first: ProviderError | None = None
    for host in HOSTS:
        try:
            return await balance(key, host), host
        except ProviderError as exc:
            if exc.status_code != 401:
                raise
            first = first or exc
        except httpx.HTTPError:
            continue  # this region is unreachable from here: the other may not be
    raise first or ProviderError("Could not reach Tripo.", provider=PROVIDER, status_code=502)


# ---------- files ----------

def _mime(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "glb": "model/gltf-binary",
            "gltf": "model/gltf+json", "fbx": "application/octet-stream", "obj": "text/plain", "stl": "model/stl"}.get(ext, "application/octet-stream")


async def upload(client: httpx.AsyncClient, key: str, path: Path) -> str:
    """Upload an image (up to 20 MB) or a model (up to 150 MB); returns its `file_token`."""
    resp = await client.post(f"{base_url()}/files", headers=headers(key), files={"file": (path.name, path.read_bytes(), _mime(path))})
    token = _body(resp, "upload").get("file_token")
    if not token:
        raise ProviderError("Tripo upload returned no file token.", provider=PROVIDER, status_code=502)
    return str(token)


# ---------- tasks ----------

Notify = Callable[[int, str], Awaitable[None]]


async def create(client: httpx.AsyncClient, key: str, endpoint: str, body: dict[str, Any]) -> str:
    data = _body(await client.post(f"{base_url()}{endpoint}", headers=headers(key), json=body), "task")
    task_id = data.get("task_id")
    if not task_id:
        raise ProviderError("Tripo did not create a task.", provider=PROVIDER, status_code=502)
    return str(task_id)


async def wait(client: httpx.AsyncClient, key: str, task_id: str, notify: Notify | None = None,
               poll_interval: float = 2.5, max_polls: int = 360) -> dict[str, Any]:
    """Poll a task until it succeeds; returns the task's `data` (with `output`, `credits_consumed`)."""
    for _ in range(max_polls):
        resp = await client.get(f"{base_url()}/tasks/{task_id}", headers=headers(key))
        if resp.status_code < 400:
            data = (resp.json() or {}).get("data") or {}
            status = str(data.get("status", "queued")).lower()
            progress = int(data.get("progress") or 0)
            if notify:
                await notify(progress, f"Tripo {status} ({progress}%)")
            if status == "success":
                return data
            if status in FAILED:
                detail = data.get("error_message") or "no details"
                raise ProviderError(f"Tripo task {status}: {detail}", provider=PROVIDER, status_code=500, retryable=status == "failed",
                                    how_to_fix="Retry, or try a cleaner source; 'banned' means the content was blocked.")
        await asyncio.sleep(poll_interval)
    raise ProviderError("Tripo took too long to finish.", provider=PROVIDER, status_code=504, retryable=True)


def model_url(data: dict[str, Any]) -> str:
    """The finished model's link, across the v3 field and the older `pbr_model` / `model` ones."""
    out = data.get("output") or {}
    return str(out.get("model_url") or out.get("pbr_model") or out.get("model") or out.get("base_model") or "")


async def download(client: httpx.AsyncClient, url: str) -> bytes:
    """Tripo's links expire, so every result is saved to disk at once."""
    resp = await client.get(url)
    if resp.status_code != 200:
        raise ProviderError(f"Tripo file download failed ({resp.status_code}).", provider=PROVIDER, status_code=resp.status_code, retryable=True)
    return resp.content


async def run(client: httpx.AsyncClient, key: str, endpoint: str, body: dict[str, Any], notify: Notify | None = None,
              poll_interval: float = 2.5, max_polls: int = 360) -> tuple[str, dict[str, Any]]:
    """Create a task and wait for it. Returns `(task_id, task data)`."""
    task_id = await create(client, key, endpoint, body)
    return task_id, await wait(client, key, task_id, notify, poll_interval, max_polls)


# ---------- generation settings ----------

def is_p_series(model: str) -> bool:
    return model.upper().startswith("P")


def _version(model: str) -> tuple[int, ...]:
    digits = model.lstrip("vV").split("-")[0].split(".")
    try:
        return tuple(int(d) for d in digits)
    except ValueError:
        return (0,)


def generation_fields(model: str, settings: dict[str, Any], text: bool = False) -> dict[str, Any]:
    """The optional generation fields this model accepts, from a flat settings dict (unknown or empty ones dropped)."""
    allowed = set(_COMMON)
    p_series = is_p_series(model)
    if p_series or _version(model) >= (3, 0):
        allowed |= _FROM_V3
    if not p_series and _version(model) >= (3, 0):
        allowed |= _H_ONLY_V3
    if model.upper().startswith("P2"):
        allowed.add("quad")
    if text:
        allowed |= {"negative_prompt", "image_seed"}
        allowed.discard("enable_image_autofix")
    out: dict[str, Any] = {}
    for field in allowed:
        value = settings.get(field)
        if value is None or value == "":
            continue
        if field in _BOOLEANS:
            out[field] = bool(value)
        elif field in _INTEGERS:
            if int(value):  # 0 means "automatic"
                out[field] = int(value)
        elif value:
            out[field] = str(value)[:255]
    # Tripo rejects parts together with textures, and smart low poly takes precedence over parts and quads.
    if out.get("generate_parts"):
        out["texture"] = out["pbr"] = False
    if out.get("pbr"):
        out["texture"] = True
    if out.get("texture_quality") == "fast":
        out["texture_version"] = "v3.5-20260815"  # "fast" only exists on the newest texture model
    return out


def settings_from_config(cfg: Any, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make 3D settings for Tripo: the Settings defaults, with valid values from the send sheet on top."""
    o = overrides or {}

    def pick(key: str, default: Any, choices: Any = None) -> Any:
        value = o.get(key, default)
        return value if choices is None or value in choices else default

    texture = bool(o.get("texture", getattr(cfg, "tripo_texture", True)))
    try:
        faces = int(o.get("face_limit", getattr(cfg, "tripo_face_limit", 0)) or 0)
    except (TypeError, ValueError):
        faces = 0
    return {
        "model": pick("model", getattr(cfg, "tripo_model", DEFAULT_MODEL), MODELS),
        "texture": texture,
        "pbr": texture and bool(o.get("pbr", getattr(cfg, "tripo_pbr", True))),
        "texture_quality": pick("texture_quality", getattr(cfg, "tripo_texture_quality", "standard"), TEXTURE_QUALITIES),
        "geometry_quality": pick("geometry_quality", getattr(cfg, "tripo_geometry_quality", "standard"), GEOMETRY_QUALITIES),
        "face_limit": max(0, min(2_000_000, faces)),
        "quad": bool(o.get("quad", getattr(cfg, "tripo_quad", False))),
        "smart_low_poly": bool(o.get("smart_low_poly", getattr(cfg, "tripo_smart_low_poly", False))),
        "auto_size": bool(o.get("auto_size", getattr(cfg, "tripo_auto_size", True))),
    }


TRIPO_CHOICES = {"model": MODELS, "texture_quality": list(TEXTURE_QUALITIES), "geometry_quality": list(GEOMETRY_QUALITIES),
                 "face_presets": [5000, 10000, 20000, 50000, 100000]}


def estimate_credits(kind: str, settings: dict[str, Any]) -> float:
    credits = CREDITS.get(kind, 40.0)
    if settings.get("texture") is False:
        credits = max(10.0, credits - 10.0)
    if settings.get("texture_quality") == "detailed":
        credits += 10.0
    elif settings.get("texture_quality") == "extreme":
        credits += 30.0
    if settings.get("geometry_quality") == "detailed":
        credits += 20.0
    return credits


# ---------- model tools ----------

async def rig(client: httpx.AsyncClient, key: str, source: str, rig_type: str = "biped", spec: str = "tripo",
              out_format: str = "glb", notify: Notify | None = None) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {"input": source, "model": "v2.5-20260210", "rig_type": rig_type if rig_type in RIG_TYPES else "biped",
                            "out_format": out_format}
    if body["rig_type"] == "biped" and spec in RIG_SPECS:
        body["spec"] = spec
    return await run(client, key, "/animations/rig", body, notify)


async def retarget(client: httpx.AsyncClient, key: str, rig_task: str, animations: list[str], out_format: str = "glb",
                   in_place: bool = False, notify: Notify | None = None) -> tuple[str, dict[str, Any]]:
    clips = list(dict.fromkeys(animations))[:MAX_ANIMATIONS]
    body: dict[str, Any] = {"input": rig_task, "out_format": out_format, "animate_in_place": in_place}
    body.update({"animation": clips[0]} if len(clips) == 1 else {"animations": clips})
    return await run(client, key, "/animations/retarget", body, notify)


async def decimate(client: httpx.AsyncClient, key: str, source: str, face_limit: int, quad: bool = False, smart_uv: bool = True,
                   notify: Notify | None = None) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {"input": source, "model": "v3.0", "face_limit": int(face_limit), "quad": quad, "smart_uv": smart_uv, "bake": True}
    return await run(client, key, "/mesh/decimate", body, notify)


async def retexture(client: httpx.AsyncClient, key: str, source: str, prompt: str = "", quality: str = "standard", pbr: bool = True,
                    notify: Notify | None = None) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {"input": source, "pbr": pbr, "texture_quality": quality if quality in TEXTURE_QUALITIES else "standard"}
    if prompt.strip():
        body["texture_prompt"] = {"text": prompt.strip()[:1024]}
    return await run(client, key, "/models/texture", body, notify)


async def convert(client: httpx.AsyncClient, key: str, source: str, fmt: str, options: dict[str, Any] | None = None,
                  notify: Notify | None = None) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {"input": source, "format": fmt.upper(), **(options or {})}
    return await run(client, key, "/models/convert", body, notify)

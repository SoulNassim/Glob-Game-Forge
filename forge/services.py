"""Generation services the user signs in to instead of pasting an API key (ElevenLabs, Meshy, fal, Higgsfield).

Each one runs a hosted MCP server behind OAuth. Glob signs in like any MCP client: it discovers the server's
authorization server, registers itself (dynamic registration when offered, otherwise the client metadata
document Glob publishes on its website), runs the authorization-code flow with PKCE in the user's browser,
and keeps the tokens on this computer. The Glob chat then sees each connected service's tools and can call
them, so a new service is one entry in SERVICES, with no code per provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from forge import meshy_login
from forge.config import get_config_dir, write_secret_file

SERVICES: dict[str, dict[str, str]] = {
    "elevenlabs": {"label": "ElevenLabs", "url": "https://api.elevenlabs.io/v1/mcp", "makes": "Images, video, voices and sound effects",
                   "scope": "flows image_video_generation text_to_speech voice_generation speech_history_read"},
    # Meshy only lets its own apps sign in, so Glob uses the Meshy CLI's login (forge/meshy_login.py) for its Meshy models.
    "meshy": {"label": "Meshy", "login": "meshy-cli", "makes": "3D models, textures, rigs and animations"},
    "fal": {"label": "fal", "url": "https://mcp.fal.ai/mcp-relay", "makes": "Hundreds of image, video, 3D and audio models"},
    "higgsfield": {"label": "Higgsfield", "url": "https://mcp.higgsfield.ai/mcp", "makes": "Images and cinematic video"},
}
# Glob's client metadata document, for servers without dynamic registration. Served by GitHub Pages from docs/.
CLIENT_METADATA_URL = "https://soulnassim.github.io/Glob-Game-Forge/oauth/client.json"
CALLBACK_PATH = "/api/services/callback"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
MAX_RESULT_CHARS = 12000

_lock = threading.RLock()
_pending: dict[str, dict[str, Any]] = {}  # OAuth state -> {service, verifier, redirect_uri, started}
_sessions: dict[str, str] = {}  # service -> MCP session id
_tools: dict[str, tuple[float, list[dict[str, Any]]]] = {}


class ServiceError(RuntimeError):
    pass


# ---------- storage ----------

def _path():
    return get_config_dir() / "services.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    write_secret_file(_path(), json.dumps(data, indent=2))


def _update(service: str, fields: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = _load()
        data[service] = data.get(service, {}) | fields
        _save(data)
        return data[service]


def status() -> list[dict[str, Any]]:
    data = _load()
    out = []
    for sid, spec in SERVICES.items():
        entry = data.get(sid, {})
        tools = _tools.get(sid)
        if spec.get("login") == "meshy-cli":
            # Powers Glob's own Meshy models, not chat tools.
            out.append({"id": sid, "label": spec["label"], "makes": spec["makes"], "connected": meshy_login.signed_in(),
                        "pending": meshy_login.logging_in(), "error": "", "account": "", "tools": None, "use": "generators"})
            continue
        out.append({"id": sid, "label": spec["label"], "makes": spec["makes"], "connected": bool(entry.get("tokens")),
                    "error": entry.get("error", ""), "account": entry.get("account", ""), "tools": len(tools[1]) if tools else None, "use": "chat"})
    return out


def connected() -> list[str]:
    data = _load()
    return [sid for sid, spec in SERVICES.items() if "url" in spec and data.get(sid, {}).get("tokens")]


def disconnect(service: str) -> None:
    if _spec(service).get("login") == "meshy-cli":
        meshy_login.sign_out()
        return
    with _lock:
        data = _load()
        data.pop(service, None)
        _save(data)
    _sessions.pop(service, None)
    _tools.pop(service, None)


# ---------- OAuth ----------

def _spec(service: str) -> dict[str, str]:
    if service not in SERVICES or "url" not in SERVICES[service] and "login" not in SERVICES[service]:
        raise ServiceError(f"Unknown service '{service}'.")
    return SERVICES[service]


def _get_json(client: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        resp = client.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def discover(service: str) -> dict[str, Any]:
    """The server's protected-resource metadata and its authorization server's metadata (RFC 9728, RFC 8414)."""
    url = _spec(service)["url"]
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        resource = _get_json(client, f"{origin}/.well-known/oauth-protected-resource{parts.path}") or _get_json(client, f"{origin}/.well-known/oauth-protected-resource")
        if not resource or not resource.get("authorization_servers"):
            raise ServiceError(f"{_spec(service)['label']} doesn't publish how to sign in.")
        issuer = str(resource["authorization_servers"][0]).rstrip("/")
        issuer_parts = urlsplit(issuer)
        issuer_origin = f"{issuer_parts.scheme}://{issuer_parts.netloc}"
        auth = (_get_json(client, f"{issuer_origin}/.well-known/oauth-authorization-server{issuer_parts.path}")
                or _get_json(client, f"{issuer}/.well-known/oauth-authorization-server")
                or _get_json(client, f"{issuer}/.well-known/openid-configuration"))
    if not auth or not auth.get("authorization_endpoint") or not auth.get("token_endpoint"):
        raise ServiceError(f"Could not read {_spec(service)['label']}'s sign-in settings.")
    return {"resource": resource, "auth": auth}


def _register(service: str, auth: dict[str, Any], redirect_uri: str) -> dict[str, str]:
    """A client id for this redirect: dynamic registration (RFC 7591) when offered, else Glob's metadata document."""
    saved = _load().get(service, {}).get("client") or {}
    if saved.get("redirect_uri") == redirect_uri and saved.get("client_id"):
        return saved
    if auth.get("registration_endpoint"):
        body = {"client_name": "Glob", "client_uri": "https://soulnassim.github.io/Glob-Game-Forge/", "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"}
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(auth["registration_endpoint"], json=body)
        if resp.status_code >= 400:
            raise ServiceError(f"{_spec(service)['label']} refused to register Glob ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        client_info = {"client_id": data["client_id"], "client_secret": data.get("client_secret") or "", "redirect_uri": redirect_uri}
    elif auth.get("client_id_metadata_document_supported"):
        client_info = {"client_id": CLIENT_METADATA_URL, "client_secret": "", "redirect_uri": redirect_uri}
    else:
        raise ServiceError(f"{_spec(service)['label']} doesn't let apps like Glob sign in yet. Use an API key instead.")
    _update(service, {"client": client_info})
    return client_info


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def start_sign_in(service: str, redirect_uri: str) -> str:
    """The URL to open in the browser. The callback finishes with `finish_sign_in`."""
    spec = _spec(service)
    if spec.get("login") == "meshy-cli":
        try:
            return meshy_login.start()
        except (meshy_login.MeshyLoginError, OSError, subprocess.SubprocessError) as exc:
            raise ServiceError(str(exc)) from exc
    meta = discover(service)
    auth, resource = meta["auth"], meta["resource"]
    client_info = _register(service, auth, redirect_uri)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    scopes = spec.get("scope") or " ".join(resource.get("scopes_supported") or auth.get("scopes_supported") or [])
    if "offline_access" in (auth.get("scopes_supported") or []) and "offline_access" not in scopes.split():
        scopes = f"{scopes} offline_access".strip()
    with _lock:
        for key in [k for k, v in _pending.items() if time.time() - v["started"] > 900]:
            _pending.pop(key, None)
        _pending[state] = {"service": service, "verifier": verifier, "redirect_uri": redirect_uri, "started": time.time(), "auth": auth}
    params = {"response_type": "code", "client_id": client_info["client_id"], "redirect_uri": redirect_uri, "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256", "resource": spec["url"]}
    if scopes:
        params["scope"] = scopes
    return f"{auth['authorization_endpoint']}?{urlencode(params)}"


def _token_request(service: str, auth: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
    client_info = _load().get(service, {}).get("client") or {}
    form = form | {"client_id": client_info.get("client_id", ""), "resource": _spec(service)["url"]}
    if client_info.get("client_secret"):
        form["client_secret"] = client_info["client_secret"]
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(auth["token_endpoint"], data=form, headers={"Accept": "application/json"})
    if resp.status_code >= 400:
        raise ServiceError(f"{_spec(service)['label']} sign-in failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    if not data.get("access_token"):
        raise ServiceError(f"{_spec(service)['label']} sent no access token.")
    return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token") or "",
            "expires_at": time.time() + float(data.get("expires_in") or 3600) - 60}


def finish_sign_in(state: str, code: str) -> str:
    """Exchange the code from the browser callback for tokens. Returns the service id."""
    with _lock:
        pending = _pending.pop(state, None)
    if not pending:
        raise ServiceError("This sign-in link expired. Start it again from Glob.")
    service = pending["service"]
    tokens = _token_request(service, pending["auth"], {"grant_type": "authorization_code", "code": code,
                                                       "redirect_uri": pending["redirect_uri"], "code_verifier": pending["verifier"]})
    _update(service, {"tokens": tokens, "auth": {"token_endpoint": pending["auth"]["token_endpoint"]}, "error": "", "connected_at": time.time()})
    _sessions.pop(service, None)
    _tools.pop(service, None)
    return service


def _access_token(service: str, force_refresh: bool = False) -> str:
    entry = _load().get(service, {})
    tokens = entry.get("tokens")
    if not tokens:
        raise ServiceError(f"Sign in to {_spec(service)['label']} first (Settings → Generators).")
    if force_refresh or time.time() >= float(tokens.get("expires_at") or 0):
        if not tokens.get("refresh_token"):
            _update(service, {"tokens": None, "error": "Signed out: sign in again."})
            raise ServiceError(f"{_spec(service)['label']} signed you out. Sign in again in Settings → Generators.")
        try:
            fresh = _token_request(service, entry.get("auth") or {}, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        except (ServiceError, httpx.HTTPError) as exc:
            _update(service, {"tokens": None, "error": "Signed out: sign in again."})
            raise ServiceError(f"{_spec(service)['label']} signed you out. Sign in again in Settings → Generators.") from exc
        fresh["refresh_token"] = fresh["refresh_token"] or tokens["refresh_token"]
        _update(service, {"tokens": fresh, "error": ""})
        return fresh["access_token"]
    return tokens["access_token"]


# ---------- MCP over Streamable HTTP ----------

def _parse(resp: httpx.Response, request_id: int) -> dict[str, Any]:
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    message = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
        raise ServiceError("The service closed the stream without answering.")
    return resp.json() if resp.content else {}


def _post(service: str, payload: dict[str, Any], retry: bool = True) -> httpx.Response:
    headers = {"Authorization": f"Bearer {_access_token(service)}", "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL_VERSION}
    if _sessions.get(service) and payload.get("method") != "initialize":
        headers["Mcp-Session-Id"] = _sessions[service]
    with httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
        resp = client.post(_spec(service)["url"], json=payload, headers=headers)
    if resp.status_code == 401 and retry:
        _access_token(service, force_refresh=True)
        return _post(service, payload, retry=False)
    return resp


def _initialize(service: str) -> None:
    resp = _post(service, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                           "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "Glob", "version": "2"}}})
    if resp.status_code >= 400:
        raise ServiceError(f"{_spec(service)['label']} error {resp.status_code}: {resp.text[:200]}")
    message = _parse(resp, 0)
    if "error" in message:
        raise ServiceError(f"{_spec(service)['label']}: {message['error'].get('message', message['error'])}")
    if resp.headers.get("mcp-session-id"):
        _sessions[service] = resp.headers["mcp-session-id"]
    _post(service, {"jsonrpc": "2.0", "method": "notifications/initialized"})


def request(service: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """One JSON-RPC call; starts (or restarts) the MCP session when needed."""
    for attempt in range(2):
        if service not in _sessions:
            _initialize(service)
        request_id = secrets.randbelow(1_000_000) + 1
        resp = _post(service, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        if resp.status_code in (400, 404) and _sessions.get(service) and attempt == 0:
            _sessions.pop(service, None)  # session expired on the server
            continue
        if resp.status_code >= 400:
            raise ServiceError(f"{_spec(service)['label']} error {resp.status_code}: {resp.text[:300]}")
        message = _parse(resp, request_id)
        if "error" in message:
            raise ServiceError(f"{_spec(service)['label']}: {message['error'].get('message', message['error'])}")
        return message.get("result") or {}
    raise ServiceError(f"{_spec(service)['label']} keeps dropping the session.")


def list_tools(service: str, fresh: bool = False) -> list[dict[str, Any]]:
    cached = _tools.get(service)
    if cached and not fresh and time.time() - cached[0] < 600:
        return cached[1]
    tools: list[dict[str, Any]] = []
    cursor = None
    for _ in range(10):
        result = request(service, "tools/list", {"cursor": cursor} if cursor else {})
        tools += [t for t in result.get("tools") or [] if isinstance(t, dict)]
        cursor = result.get("nextCursor")
        if not cursor:
            break
    _tools[service] = (time.time(), tools)
    return tools


GENERATION_WORDS = ("generate", "image", "video", "3d", "speech", "audio", "music", "sound", "voice", "upload", "status", "job", "model")


def tool_summary(service: str, query: str = "") -> str:
    """Tool names with the first line of each description, for the chat. Generation tools come first, since a
    service can have well over a hundred tools and the list gets cut; `query` keeps only matching tools."""
    tools = list_tools(service)
    words = [w for w in query.lower().split() if w]
    if words:
        tools = [t for t in tools if any(w in f"{t.get('name')} {t.get('description')}".lower() for w in words)]
    tools.sort(key=lambda t: not any(w in str(t.get("name") or "").lower() for w in GENERATION_WORDS))
    lines = [f"  - {t.get('name')}: {' '.join(str(t.get('description') or '').split())[:140]}" for t in tools]
    head = f"{len(tools)} tools{f' matching {query!r}' if words else ''} (generation tools first; pass query to search):"
    return "\n".join([head, *lines])


def tool_schema(service: str, name: str) -> dict[str, Any]:
    for t in list_tools(service):
        if t.get("name") == name:
            return {"name": name, "description": t.get("description", ""), "inputSchema": t.get("inputSchema", {})}
    raise ServiceError(f"{_spec(service)['label']} has no tool '{name}'.")


def call_tool(service: str, name: str, arguments: dict[str, Any]) -> str:
    """Run a tool; returns its text (and any links) for the chat."""
    result = request(service, "tools/call", {"name": name, "arguments": arguments or {}})
    parts = []
    for item in result.get("content") or []:
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "resource_link":
            parts.append(f"[file] {item.get('uri')}")
        elif kind == "resource":
            res = item.get("resource") or {}
            parts.append(str(res.get("text") or f"[file] {res.get('uri')}"))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} returned inline, {item.get('mimeType', '')}]")
    if result.get("structuredContent") and not parts:
        parts.append(json.dumps(result["structuredContent"])[:MAX_RESULT_CHARS])
    text = "\n".join(p for p in parts if p).strip() or "(no output)"
    if result.get("isError"):
        raise ServiceError(text[:600])
    return text[:MAX_RESULT_CHARS]


def call_tool_data(service: str, name: str, arguments: dict[str, Any]) -> Any:
    """Run a tool for Glob itself: its structured result, or its text parsed as JSON when it is JSON."""
    result = request(service, "tools/call", {"name": name, "arguments": arguments or {}})
    texts = [str(item.get("text") or "") for item in result.get("content") or [] if item.get("type") == "text"]
    if result.get("isError"):
        raise ServiceError(("\n".join(texts) or "The tool failed.")[:600])
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for text in texts:
        try:
            return json.loads(text)
        except ValueError:
            continue
    return {"text": "\n".join(texts)}

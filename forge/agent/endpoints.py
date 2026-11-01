"""Chat-model endpoints the user adds in the app: pick a preset, paste a key, pick models.

Saved in ``<config dir>/endpoints.json``. Model ids look like ``ep:<endpoint id>/<model id>``.
Two wire formats are supported: OpenAI-compatible (Chat Completions or Responses) and Anthropic.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import anthropic
import httpx

from forge.config import get_config_dir, write_secret_file

PRESETS: dict[str, dict[str, Any]] = {
    "anthropic": {"label": "Anthropic", "base_url": "https://api.anthropic.com", "kind": "anthropic", "needs_key": True, "key_url": "https://console.anthropic.com/settings/keys"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "kind": "openai", "needs_key": True, "key_url": "https://platform.openai.com/api-keys"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "kind": "openai", "needs_key": True, "key_url": "https://openrouter.ai/keys"},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "kind": "openai", "needs_key": True, "key_url": "https://aistudio.google.com/apikey"},
    "ollama": {"label": "Ollama (local)", "base_url": "http://localhost:11434/v1", "kind": "openai", "needs_key": False, "key_url": ""},
    "lmstudio": {"label": "LM Studio (local)", "base_url": "http://localhost:1234/v1", "kind": "openai", "needs_key": False, "key_url": ""},
    "custom": {"label": "Custom endpoint", "base_url": "", "kind": "openai", "needs_key": False, "key_url": ""},
}
EFFORTS = ("low", "medium", "high", "max")
_OPENAI_EFFORT = {"low": "low", "medium": "medium", "high": "high", "max": "high"}


def _path():
    return get_config_dir() / "endpoints.json"


def load_endpoints() -> list[dict[str, Any]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    return [e for e in data.get("endpoints", []) if isinstance(e, dict) and e.get("id")]


def _save(endpoints: list[dict[str, Any]]) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    write_secret_file(_path(), json.dumps({"endpoints": endpoints}, indent=2))


def public(endpoint: dict[str, Any]) -> dict[str, Any]:
    """An endpoint without its key, for the UI."""
    key = str(endpoint.get("api_key") or "")
    return {**{k: v for k, v in endpoint.items() if k != "api_key"}, "has_key": bool(key), "key_hint": f"…{key[-4:]}" if len(key) > 8 else ""}


def _base(url: str) -> str:
    return re.sub(r"/(chat/completions|responses)/?$", "", str(url or "").strip()).rstrip("/")


async def probe(base_url: str, api_key: str = "", kind: str = "openai") -> list[str]:
    """Ask an endpoint which models it offers."""
    base = _base(base_url)
    if not base.startswith(("http://", "https://")):
        raise ValueError("Enter the endpoint URL, starting with https:// (or http:// for local servers).")
    async with httpx.AsyncClient(timeout=20.0) as client:
        if kind == "anthropic":
            resp = await client.get(f"{base}/v1/models", headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        else:
            resp = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    if resp.status_code in (401, 403):
        raise ValueError("The endpoint rejected the key. Check it and try again.")
    if resp.status_code >= 400:
        raise ValueError(f"The endpoint answered {resp.status_code}. Check the URL.")
    items = resp.json().get("data") or resp.json().get("models") or []
    ids = [str(m.get("id") or m.get("name") or "") for m in items if isinstance(m, dict)]
    return sorted({i.removeprefix("models/") for i in ids if i})


def add_endpoint(fields: dict[str, Any]) -> dict[str, Any]:
    preset = PRESETS.get(str(fields.get("preset") or "custom"), PRESETS["custom"])
    models = [m for m in fields.get("models") or [] if isinstance(m, (str, dict))]
    if not models:
        raise ValueError("Pick at least one model.")
    endpoint = {
        "id": uuid.uuid4().hex[:8],
        "name": str(fields.get("name") or preset["label"]).strip()[:60],
        "preset": str(fields.get("preset") or "custom"),
        "kind": preset["kind"] if fields.get("preset") in PRESETS else str(fields.get("kind") or "openai"),
        "base_url": _base(str(fields.get("base_url") or preset["base_url"])),
        "api_key": str(fields.get("api_key") or "").strip(),
        "models": [m if isinstance(m, dict) else {"id": m, "label": m, "api": "chat"} for m in models],
    }
    endpoints = load_endpoints()
    endpoints.append(endpoint)
    _save(endpoints)
    return public(endpoint)


def remove_endpoint(endpoint_id: str) -> None:
    _save([e for e in load_endpoints() if e["id"] != endpoint_id])


def import_vscode() -> list[dict[str, Any]]:
    """Copy the models configured in VS Code (with their keys) into Glob, one endpoint per provider."""
    from forge.providers.auth_manager import discover_vscode_copilot_models

    existing = load_endpoints()
    known = {(e.get("source"), e["name"]) for e in existing}
    grouped: dict[str, dict[str, Any]] = {}
    for m in discover_vscode_copilot_models(include_secret_key=True):
        name = m["provider_name"]
        if ("vscode", name) in known:
            continue
        ep = grouped.setdefault(name, {
            "id": uuid.uuid4().hex[:8], "name": name, "preset": "custom", "kind": "openai", "source": "vscode",
            "base_url": _base(m["url"]), "api_key": m.get("api_key") or "", "models": [],
        })
        ep["models"].append({
            "id": m["model_id"], "label": m["display_name"], "url": m["url"],
            "api": "responses" if m.get("api_type") == "responses" or m["url"].endswith("/responses") else "chat",
            "vision": m.get("vision", False),
        })
    added = list(grouped.values())
    if added:
        _save(existing + added)
    return [public(e) for e in added]


def list_models() -> list[dict[str, Any]]:
    out = []
    for e in load_endpoints():
        preset = PRESETS.get(e.get("preset", ""), {})
        for m in e.get("models", []):
            out.append({
                "id": f"ep:{e['id']}/{m['id']}",
                "label": m.get("label") or m["id"],
                "provider": e["name"],
                "group": e["name"],
                "endpoint": e["id"],
                "ready": bool(e.get("api_key")) or not preset.get("needs_key", False),
                # Endpoints that don't understand reasoning effort get a retry without it.
                "efforts": list(EFFORTS),
            })
    return out


def find(model_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ep_id, _, mid = model_id.removeprefix("ep:").partition("/")
    for e in load_endpoints():
        if e["id"] == ep_id:
            for m in e.get("models", []):
                if m["id"] == mid:
                    return e, m
    raise RuntimeError("This model was removed. Pick another one in the model menu.")


async def call(model_id: str, prompt: str, system_prompt: str, images: list[tuple[str, str]], effort: str) -> dict[str, Any]:
    endpoint, model = find(model_id)
    key = str(endpoint.get("api_key") or "")
    if endpoint["kind"] == "anthropic":
        client = anthropic.AsyncAnthropic(api_key=key, base_url=endpoint["base_url"])
        content: list[dict[str, Any]] = [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}} for mime, data in images[:4]]
        content.append({"type": "text", "text": prompt})
        try:
            async with client.messages.stream(
                model=model["id"], max_tokens=32000, system=system_prompt,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": effort if effort in EFFORTS else "medium"},
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise RuntimeError(f"{endpoint['name']} rejected the key. Update it in Settings.") from exc
        except anthropic.APIStatusError as exc:
            raise RuntimeError(f"{endpoint['name']} error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise RuntimeError(f"Could not reach {endpoint['name']}.") from exc
        text = "".join(block.text for block in message.content if block.type == "text")
        return {"text": text.strip(), "model": model.get("label") or model["id"], "provider": endpoint["name"]}

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    responses = model.get("api") == "responses"
    url = model.get("url") or f"{endpoint['base_url']}/{'responses' if responses else 'chat/completions'}"
    if responses:
        parts: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        parts += [{"type": "input_image", "image_url": f"data:{mime};base64,{data}"} for mime, data in images[:4] if model.get("vision")]
        body: dict[str, Any] = {"model": model["id"], "instructions": system_prompt, "input": [{"role": "user", "content": parts}], "reasoning": {"effort": _OPENAI_EFFORT.get(effort, "medium")}}
    else:
        user: Any = prompt
        if images and model.get("vision", True):
            user = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}} for mime, data in images[:4]]
        body = {"model": model["id"], "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}], "reasoning_effort": _OPENAI_EFFORT.get(effort, "medium")}
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code == 400:
            # Not every endpoint knows about reasoning effort; retry without it.
            body.pop("reasoning", None)
            body.pop("reasoning_effort", None)
            resp = await client.post(url, headers=headers, json=body)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"{endpoint['name']} rejected the key. Update it in Settings.")
    if resp.status_code >= 400:
        raise RuntimeError(f"{endpoint['name']} error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if responses:
        text = data.get("output_text") or "".join(
            c.get("text", "") for item in data.get("output", []) if isinstance(item, dict) for c in item.get("content") or [] if isinstance(c, dict)
        )
    else:
        text = data["choices"][0]["message"]["content"] or ""
    return {"text": text.strip(), "model": model.get("label") or model["id"], "provider": endpoint["name"]}

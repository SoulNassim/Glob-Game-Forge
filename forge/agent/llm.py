"""Model router for the Forge agent.

Model ids are prefixed by provider:
  ``anthropic:<model>``  — Claude via the Anthropic API (key from Settings or ANTHROPIC_API_KEY)
  ``openai:<model>``     — OpenAI Chat Completions (key from Settings or OPENAI_API_KEY)
  ``vscode:<id>``        — a model configured in VS Code Copilot's chatLanguageModels.json
  ``ep:<endpoint>/<id>`` — a model from an endpoint added in the app (see ``forge.agent.endpoints``)
  ``cli:<agent>/<model>`` — a coding-agent CLI on the user's own login (see ``forge.agent.cli_models``)
An empty id picks the first provider that has credentials.
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx

from forge.agent import cli_models, endpoints
from forge.config import load_config
from forge.providers.auth_manager import call_vscode_copilot_model, discover_vscode_copilot_models

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_OPENAI_MODEL = "gpt-4o"
EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "max": "max"}

ANTHROPIC_MODELS = [
    ("anthropic:claude-opus-5-5", "Claude Opus 5.5"),
    ("anthropic:claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("anthropic:claude-haiku-5-5", "Claude Haiku 5.5"),
]
OPENAI_MODELS = [("openai:gpt-4o", "GPT-4o")]


def list_agent_models() -> list[dict[str, Any]]:
    """Every model the agent can call right now, with whether its credentials are present."""
    cfg = load_config()
    has_anthropic = bool(cfg.get_key("anthropic"))
    has_openai = bool(cfg.get_key("openai"))
    models = [{"id": mid, "label": label, "provider": "anthropic", "group": "Anthropic", "ready": has_anthropic, "efforts": list(endpoints.EFFORTS)} for mid, label in ANTHROPIC_MODELS]
    models += [{"id": mid, "label": label, "provider": "openai", "group": "OpenAI", "ready": has_openai, "efforts": []} for mid, label in OPENAI_MODELS]
    models += endpoints.list_models()
    models += cli_models.list_models()
    imported = {m["id"] for e in endpoints.load_endpoints() if e.get("source") == "vscode" for m in e.get("models", [])}
    try:
        vscode = discover_vscode_copilot_models()
    except Exception:  # noqa: BLE001 — VS Code discovery is best-effort
        vscode = []
    models += [
        {"id": vm["id"], "label": vm["display_name"], "provider": "vscode", "group": "VS Code", "ready": True, "efforts": []}
        for vm in vscode
        if vm["model_id"] not in imported
    ]
    return models


def resolve_model(selector: str | None) -> str:
    """Turn a stored selector into a concrete, prefixed model id."""
    selector = (selector or "").strip()
    if selector.startswith(("anthropic:", "openai:", "vscode:", "ep:", "cli:")):
        return selector
    if selector.startswith("claude-"):
        return f"anthropic:{selector}"
    ready = [m for m in list_agent_models() if m["ready"]]
    if not ready:
        raise RuntimeError("No AI model is connected. Open Settings and paste an Anthropic or OpenAI API key, or install a coding agent such as Claude Code.")
    return ready[0]["id"]


async def _call_anthropic(model: str, prompt: str, system_prompt: str, images: list[tuple[str, str]], effort: str) -> dict[str, Any]:
    key = load_config().get_key("anthropic")
    if not key:
        raise RuntimeError("Add your Anthropic API key in Settings to use Claude.")
    content: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}} for mime, data in images[:4]
    ]
    content.append({"type": "text", "text": prompt})
    client = anthropic.AsyncAnthropic(api_key=key)
    try:
        async with client.beta.messages.stream(
            model=model,
            max_tokens=32000,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": EFFORT_MAP.get(effort, "medium")},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            message = await stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise RuntimeError("Anthropic rejected the API key. Check it in Settings.") from exc
    except anthropic.RateLimitError as exc:
        raise RuntimeError("Anthropic rate limit reached. Wait a moment and try again.") from exc
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError("Could not reach the Anthropic API. Check your internet connection.") from exc
    if message.stop_reason == "refusal":
        raise RuntimeError("The model declined this request.")
    text = "".join(block.text for block in message.content if block.type == "text")
    return {"text": text.strip(), "model": message.model, "provider": "anthropic"}


async def _call_openai(model: str, prompt: str, system_prompt: str, images: list[tuple[str, str]]) -> dict[str, Any]:
    key = load_config().get_key("openai")
    if not key:
        raise RuntimeError("Add your OpenAI API key in Settings to use OpenAI models.")
    user_content: Any = prompt
    if images:
        user_content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}} for mime, data in images[:4]
        ]
    body = {"model": model, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]}
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post("https://api.openai.com/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:200]}")
    return {"text": resp.json()["choices"][0]["message"]["content"].strip(), "model": model, "provider": "openai"}


async def call_agent_model(
    prompt: str,
    system_prompt: str,
    model_selector: str | None = None,
    images_b64: list[tuple[str, str]] | None = None,
    effort: str = "medium",
    **_: Any,
) -> dict[str, Any]:
    model_id = resolve_model(model_selector)
    provider, _, name = model_id.partition(":")
    images = list(images_b64 or [])
    if provider == "anthropic":
        return await _call_anthropic(name or DEFAULT_ANTHROPIC_MODEL, prompt, system_prompt, images, effort)
    if provider == "openai":
        return await _call_openai(name or DEFAULT_OPENAI_MODEL, prompt, system_prompt, images)
    if provider == "ep":
        return await endpoints.call(model_id, prompt, system_prompt, images, effort)
    if provider == "cli":
        return await cli_models.call(model_id, prompt, system_prompt, images, effort)
    return await call_vscode_copilot_model(prompt=prompt, system_prompt=system_prompt, model_selector=model_id, images_b64=images or None)

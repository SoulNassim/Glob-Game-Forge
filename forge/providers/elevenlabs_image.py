"""ElevenLabs Image & Video provider (api.elevenlabs.io flows API).

Image models are selectable in the studio (`elevenlabs:…` in the image model list).
Picking one routes generation through `POST /v1/flows/image`, then polls
`GET /v1/flows/image/{id}` and downloads the signed result.

Video uses the same submit-and-poll shape on `/v1/flows/video`. The client is
implemented so a later video workflow can call `TEXT_TO_VIDEO` / `IMAGE_TO_VIDEO`.
Studio verbs do not offer video generation yet.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from forge.config import load_config
from forge.prompt_sanitize import finalize_generation_prompt
from forge.providers.base import (
    CostEstimate,
    ProgressCallback,
    ProviderAdapter,
    ProviderCapability,
    ProviderError,
    ProviderResult,
)
from forge.storage.demo_assets import generate_demo_image


API_BASE = "https://api.elevenlabs.io"
MODEL_PREFIX = "elevenlabs:"

_QUALITY_ALIASES = {
    "standard": "medium",
    "draft": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

_IMAGE_ASPECTS = ("auto", "1:1", "4:5", "5:4", "3:4", "4:3", "2:3", "3:2", "1:2", "2:1", "9:16", "16:9", "21:9", "1:3", "3:1")
_GEMINI_ASPECTS = ("auto", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
_SEEDREAM_ASPECTS = ("auto", "1:1", "3:4", "16:9", "4:3", "9:16")


@dataclass(frozen=True)
class _ImageModel:
    model_id: str
    label: str
    usd: float
    aspects: tuple[str, ...]
    max_refs: int
    resolutions: tuple[str, ...] = ()
    default_resolution: str = ""
    qualities: tuple[str, ...] = ()
    backgrounds: tuple[str, ...] = ()
    # Only Seedream accepts seed on the ElevenLabs image API; Gemini/GPT reject it as extra input.
    supports_seed: bool = False


@dataclass(frozen=True)
class _VideoModel:
    model_id: str
    label: str
    usd: float
    aspects: tuple[str, ...]
    durations: tuple[int, ...]
    default_duration: int
    resolutions: tuple[str, ...]
    default_resolution: str
    negative_prompt: bool = False


IMAGE_MODELS: dict[str, _ImageModel] = {
    spec.model_id: spec
    for spec in (
        _ImageModel("gpt-image-1.5", "ElevenLabs · GPT Image 1.5", 0.05, ("1:1", "3:2", "2:3"), 5, qualities=("low", "medium", "high"), backgrounds=("transparent", "opaque", "auto")),
        _ImageModel("gpt-image-1", "ElevenLabs · GPT Image 1", 0.04, ("1:1", "3:2", "2:3"), 5, qualities=("low", "medium", "high"), backgrounds=("transparent", "opaque", "auto")),
        _ImageModel("gpt-image-2", "ElevenLabs · GPT Image 2", 0.06, _IMAGE_ASPECTS, 10, ("1K", "2K", "4K"), "1K", ("low", "medium", "high")),
        _ImageModel("gpt-image-2.5-flare", "ElevenLabs · GPT Image 2.5 Flare", 0.05, _IMAGE_ASPECTS, 10, ("1K", "2K", "4K"), "1K", ("low", "medium", "high", "xhigh", "max")),
        _ImageModel("gpt-image-2.5-sunburst", "ElevenLabs · GPT Image 2.5 Sunburst", 0.10, _IMAGE_ASPECTS, 10, ("1K", "2K", "4K"), "1K", ("low", "medium", "high", "xhigh", "max")),
        _ImageModel("gemini-3.1-flash-lite-image", "ElevenLabs · Nano Banana 2 Lite", 0.02, _GEMINI_ASPECTS, 14, ("1K",), "1K"),
        _ImageModel("gemini-3.1-flash-image", "ElevenLabs · Nano Banana 2", 0.04, _GEMINI_ASPECTS + ("1:4", "4:1", "1:8", "8:1"), 14, ("512", "1K", "2K", "4K"), "1K"),
        _ImageModel("gemini-3-pro-image", "ElevenLabs · Nano Banana Pro", 0.07, _GEMINI_ASPECTS, 10, ("1K", "2K", "4K"), "1K"),
        _ImageModel("gemini-2.5-flash-image", "ElevenLabs · Nano Banana", 0.03, _GEMINI_ASPECTS, 5),
        _ImageModel("bytedance-seedream-5-lite", "ElevenLabs · Seedream 5 Lite", 0.03, _SEEDREAM_ASPECTS, 10, ("2K", "3K"), "2K", supports_seed=True),
        _ImageModel("bytedance-seedream-5-pro", "ElevenLabs · Seedream 5 Pro", 0.05, _SEEDREAM_ASPECTS, 10, ("1K", "2K"), "2K", supports_seed=True),
    )
}

VIDEO_MODELS: dict[str, _VideoModel] = {
    spec.model_id: spec
    for spec in (
        _VideoModel("veo-3.1-fast-generate-001", "ElevenLabs · Veo 3.1 Fast", 0.40, ("16:9", "9:16"), (4, 6, 8), 8, ("720p", "1080p", "4K"), "720p", True),
        _VideoModel("veo-3.1-generate-001", "ElevenLabs · Veo 3.1", 0.80, ("16:9", "9:16"), (4, 6, 8), 8, ("720p", "1080p", "4K"), "720p", True),
        _VideoModel("bytedance-seedance-v2", "ElevenLabs · Seedance 2.0", 0.35, ("auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"), (), 5, ("480p", "720p", "1080p", "4k"), "720p"),
        _VideoModel("bytedance-seedance-v2-fast", "ElevenLabs · Seedance 2.0 Fast", 0.20, ("auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"), (), 5, ("480p", "720p"), "720p"),
    )
}

# Shown in the studio image-model menus. Other catalog models stay callable by id.
UI_IMAGE_MODEL_IDS = (
    "gpt-image-1.5",
    "gpt-image-2",
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
    "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-image",
    "gemini-3-pro-image",
    "gemini-2.5-flash-image",
)


def prefixed_model_id(model_id: str) -> str:
    model_id = str(model_id or "").strip()
    if model_id.startswith(MODEL_PREFIX):
        return model_id
    return f"{MODEL_PREFIX}{model_id}"


def bare_model_id(model: str) -> str:
    model = str(model or "").strip()
    if model.startswith(MODEL_PREFIX):
        return model[len(MODEL_PREFIX) :]
    return model


# Google's Nano Banana 2.1 id is not in the ElevenLabs image API.
# Their newest Gemini image model is Nano Banana 2.
_MODEL_ALIASES = {
    "gemini-nano-banana-2.1": "gemini-3.1-flash-image",
}


def canonical_image_model(model: str) -> str:
    bare = bare_model_id(model)
    return _MODEL_ALIASES.get(bare, bare)


def resolve_image_provider(provider_id: str, params: dict[str, Any]) -> str:
    """Route `elevenlabs:*` image selections off the Gemini adapter, and `svc:*` to the signed-in services."""
    if str(params.get("model") or "").startswith("svc:"):
        return "service"
    if provider_id != "gemini":
        if provider_id == "elevenlabs" and params.get("model"):
            # Rewrite removed/unsupported ids (e.g. gemini-nano-banana-2.1) to API tags.
            params["model"] = prefixed_model_id(canonical_image_model(str(params["model"])))
        return provider_id
    model = str(params.get("model") or "").strip()
    if not model:
        model = (load_config().default_image_model or "").strip()
        if model.startswith(MODEL_PREFIX):
            params["model"] = model
    if model.startswith(MODEL_PREFIX):
        params["model"] = prefixed_model_id(canonical_image_model(model))
        return "elevenlabs"
    return provider_id


def _pick(value: str, allowed: tuple[str, ...], fallback: str) -> str:
    if value in allowed:
        return value
    if fallback in allowed:
        return fallback
    for item in allowed:
        if item != "auto":
            return item
    return allowed[0]


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _snap_duration(value: Any, allowed: tuple[int, ...], default: int) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        return default
    if not allowed:
        return parsed if 1 <= parsed <= 15 else default
    if parsed in allowed:
        return parsed
    return min(allowed, key=lambda item: abs(item - parsed))


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    return ""


def _inline_image(path: Path) -> dict[str, str] | None:
    mime = _mime_for(path)
    if not mime or not path.is_file():
        return None
    return {
        "type": "inline_base64",
        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        "mime_type": mime,
    }


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return resp.text[:800]
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)[:800]
    if isinstance(detail, list) and detail:
        parts: list[str] = []
        for item in detail[:4]:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            loc = item.get("loc") or []
            field = ".".join(str(part) for part in loc if part not in ("body",))
            msg = str(item.get("msg") or item)
            parts.append(f"{field}: {msg}" if field else msg)
        return "; ".join(parts)[:800]
    if isinstance(detail, str):
        return detail[:800]
    return resp.text[:800]


def _http_error(resp: httpx.Response) -> ProviderError:
    text = _error_text(resp)
    retryable = resp.status_code in (429, 500, 502, 503, 504)
    if resp.status_code in (401, 403):
        how = "Check the ElevenLabs API key in Settings, or set ELEVENLABS_API_KEY. The key needs the Image & Video permission."
    elif resp.status_code == 402:
        how = (
            "ElevenLabs Studio (the editor) is not enough for API image/video. "
            "The workspace billing tier must be Pro, Scale, Business, or Enterprise. "
            "Also edit the API key under Developers → API Keys and enable Image & Video / Flows "
            "(or turn off key restrictions)."
        )
    else:
        how = "Check the model, aspect ratio, and reference images, then try again."
    return ProviderError(
        message=f"ElevenLabs API error ({resp.status_code}): {text}",
        provider="elevenlabs",
        status_code=resp.status_code,
        retryable=retryable,
        how_to_fix=how,
    )


async def _report(progress_cb: ProgressCallback | None, pct: int, label: str) -> None:
    if not progress_cb:
        return
    res = progress_cb(pct, label)
    if hasattr(res, "__await__"):
        await res


class ElevenLabsImageAdapter(ProviderAdapter):
    provider_id = "elevenlabs"
    display_name = "ElevenLabs Image & Video"
    capabilities = [
        ProviderCapability.TEXT_TO_IMAGE,
        ProviderCapability.IMAGE_EDIT,
        ProviderCapability.MULTI_REFERENCE_IMAGE,
        ProviderCapability.TEXT_TO_VIDEO,
        ProviderCapability.IMAGE_TO_VIDEO,
    ]

    def __init__(self, api_key: str | None = None, demo_mode: bool | None = None) -> None:
        self._explicit_key = api_key
        self._explicit_demo = demo_mode

    def _key_and_demo(self) -> tuple[str, bool]:
        cfg = load_config()
        key = self._explicit_key if self._explicit_key is not None else cfg.get_key("elevenlabs")
        if self._explicit_demo is not None:
            demo = self._explicit_demo
        else:
            demo = bool(cfg.demo_mode)
        return key, demo

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "default": prefixed_model_id("gpt-image-2"),
                    "enum": [prefixed_model_id(model_id) for model_id in IMAGE_MODELS],
                    "description": "ElevenLabs image model",
                },
                "video_model": {
                    "type": "string",
                    "default": prefixed_model_id("veo-3.1-fast-generate-001"),
                    "enum": [prefixed_model_id(model_id) for model_id in VIDEO_MODELS],
                    "description": "ElevenLabs video model. Studio image verbs do not call this yet.",
                },
                "aspect_ratio": {"type": "string", "default": "1:1"},
                "resolution": {"type": "string", "default": "1K"},
                "quality": {"type": "string", "default": "medium"},
                "duration_secs": {"type": "integer", "default": 8},
                "generate_audio": {"type": "boolean", "default": True},
            },
        }

    def estimate_cost(self, capability: ProviderCapability, params: dict[str, Any]) -> CostEstimate:
        params = params or {}
        if capability in (ProviderCapability.TEXT_TO_VIDEO, ProviderCapability.IMAGE_TO_VIDEO):
            model_id = bare_model_id(str(params.get("video_model") or params.get("model") or "veo-3.1-fast-generate-001"))
            spec = VIDEO_MODELS.get(model_id)
            unit = spec.usd if spec else 0.40
            label = spec.label if spec else model_id
            return CostEstimate(
                provider=self.provider_id,
                model=prefixed_model_id(model_id),
                estimated_usd=round(unit, 4),
                unit_description=f"~${unit:.2f} per video ({label})",
            )
        model_id = canonical_image_model(str(params.get("model") or "gpt-image-2"))
        spec = IMAGE_MODELS.get(model_id)
        unit = spec.usd if spec else 0.06
        return CostEstimate(
            provider=self.provider_id,
            model=prefixed_model_id(model_id),
            estimated_usd=round(unit, 4),
            unit_description=f"~${unit:.3f} per image ({spec.label if spec else model_id})",
        )

    async def execute(
        self,
        capability: ProviderCapability,
        prompt: str,
        output_dir: Path,
        filename_stem: str,
        reference_files: list[Path] | None = None,
        params: dict[str, Any] | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> ProviderResult:
        params = dict(params or {})
        if capability in (ProviderCapability.TEXT_TO_VIDEO, ProviderCapability.IMAGE_TO_VIDEO):
            return await self._execute_video(
                prompt=prompt,
                output_dir=output_dir,
                filename_stem=filename_stem,
                reference_files=reference_files,
                params=params,
                progress_cb=progress_cb,
            )
        return await self._execute_image(
            capability=capability,
            prompt=prompt,
            output_dir=output_dir,
            filename_stem=filename_stem,
            reference_files=reference_files,
            params=params,
            progress_cb=progress_cb,
        )

    async def _execute_image(
        self,
        *,
        capability: ProviderCapability,
        prompt: str,
        output_dir: Path,
        filename_stem: str,
        reference_files: list[Path] | None,
        params: dict[str, Any],
        progress_cb: ProgressCallback | None,
    ) -> ProviderResult:
        cfg = load_config()
        key, demo = self._key_and_demo()
        requested = canonical_image_model(str(params.get("model") or ""))
        params["model"] = prefixed_model_id(requested) if requested else params.get("model")
        if requested in VIDEO_MODELS:
            raise ProviderError(
                message=f"'{requested}' is a video model. Video generation is not offered from the image workflow yet.",
                provider=self.provider_id,
                status_code=400,
                how_to_fix="Pick an ElevenLabs image model, or call the video capability when video generation is added.",
            )
        if requested and requested not in IMAGE_MODELS:
            raise ProviderError(
                message=(
                    f"ElevenLabs does not support image model '{bare_model_id(str(params.get('model') or requested))}'. "
                    f"Their API accepts: {', '.join(IMAGE_MODELS)}."
                ),
                provider=self.provider_id,
                status_code=400,
                how_to_fix="Pick ElevenLabs · Nano Banana 2 / Pro, or use Gemini · Nano Banana 2.1 for Google’s model.",
            )
        spec = IMAGE_MODELS.get(requested) or IMAGE_MODELS["gpt-image-2"]

        aspect = _pick(str(params.get("aspect_ratio") or cfg.image_aspect_ratio or "1:1"), spec.aspects, "1:1")
        pose_mode = str(params.get("pose_mode") if "pose_mode" in params else cfg.image_pose_mode)
        prompt = finalize_generation_prompt(prompt, pose_mode=pose_mode, aspect_ratio=aspect)
        negative = str(params.get("negative_prompt") or "").strip()
        if negative:
            prompt = f"{prompt}\nAvoid: {negative}"
        seed = _optional_int(params.get("seed"))

        if demo:
            out_file = output_dir / f"{filename_stem}.png"
            meta = generate_demo_image(
                output_path=out_file,
                prompt=prompt,
                seed=1000 if seed is None else seed,
                transparent=bool(params.get("remove_background", params.get("transparent", cfg.image_remove_bg))),
                mode=str(params.get("render_mode") or "standard"),
            )
            return ProviderResult(
                provider="demo",
                model=prefixed_model_id(spec.model_id),
                file_path=f"assets/{out_file.name}",
                thumbnail_path=f"assets/{meta['thumb_name']}",
                cost_usd=spec.usd,
                cost_estimated=True,
                seed=seed,
                metadata={**meta, "demo_mode": True, "capability": capability.value},
            )
        if not key:
            raise ProviderError(
                message="ElevenLabs API key is required for ElevenLabs image models.",
                provider=self.provider_id,
                status_code=401,
                how_to_fix="Add your ElevenLabs API key in Settings, or set ELEVENLABS_API_KEY.",
            )

        use_refs = bool(params.get("use_reference_image")) or capability in (
            ProviderCapability.IMAGE_EDIT,
            ProviderCapability.MULTI_REFERENCE_IMAGE,
        )
        refs: list[dict[str, str]] = []
        if use_refs:
            for path in reference_files or []:
                inline = _inline_image(path)
                if inline:
                    refs.append(inline)
                if len(refs) >= spec.max_refs:
                    break

        # Only send fields the selected model_id schema allows. Extra keys return 422.
        body: dict[str, Any] = {
            "model_id": spec.model_id,
            "prompt": prompt,
            "aspect_ratio": aspect,
        }
        if spec.resolutions:
            requested_res = str(params.get("resolution") or spec.default_resolution)
            body["resolution"] = requested_res if requested_res in spec.resolutions else spec.default_resolution
        quality = _QUALITY_ALIASES.get(str(params.get("quality") or "").strip().lower())
        if quality and quality in spec.qualities:
            body["quality"] = quality
        transparent = bool(params.get("remove_background", params.get("transparent", False)))
        if transparent and "transparent" in spec.backgrounds:
            body["background"] = "transparent"
        if seed is not None and spec.supports_seed:
            body["seed"] = seed
        if refs:
            body["images"] = refs

        await _report(progress_cb, 20, f"Generating image with {spec.label}...")
        generation_id, content, _result = await self._submit_and_collect(
            kind="image",
            body=body,
            api_key=key,
            progress_cb=progress_cb,
            poll_interval=2.0,
            timeout_s=180.0,
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        out_file = output_dir / f"{filename_stem}.png"
        image = Image.open(io.BytesIO(content)).convert("RGBA")
        image.save(out_file, format="PNG")
        thumb_path = out_file.with_name(f"{out_file.stem}_thumb.png")
        thumb = image.copy()
        thumb.thumbnail((256, 256), Image.Resampling.LANCZOS)
        thumb.save(thumb_path, format="PNG")
        width, height = image.size

        return ProviderResult(
            provider=self.provider_id,
            model=prefixed_model_id(spec.model_id),
            file_path=f"assets/{out_file.name}",
            thumbnail_path=f"assets/{thumb_path.name}",
            cost_usd=spec.usd,
            cost_estimated=True,
            seed=seed,
            metadata={
                "width": width,
                "height": height,
                "aspect_ratio": aspect,
                "generation_id": generation_id,
                "capability": capability.value,
            },
        )

    async def _execute_video(
        self,
        *,
        prompt: str,
        output_dir: Path,
        filename_stem: str,
        reference_files: list[Path] | None,
        params: dict[str, Any],
        progress_cb: ProgressCallback | None,
    ) -> ProviderResult:
        """Submit a video generation. Not called by studio image verbs."""
        key, demo = self._key_and_demo()
        if demo:
            raise ProviderError(
                message="Video generation is not available in Demo Mode.",
                provider=self.provider_id,
                status_code=400,
                how_to_fix="Turn off Demo Mode and add an ElevenLabs API key when video generation is connected.",
            )
        if not key:
            raise ProviderError(
                message="ElevenLabs API key is required for video generation.",
                provider=self.provider_id,
                status_code=401,
                how_to_fix="Add your ElevenLabs API key in Settings, or set ELEVENLABS_API_KEY.",
            )

        requested = bare_model_id(str(params.get("video_model") or params.get("model") or "veo-3.1-fast-generate-001"))
        spec = VIDEO_MODELS.get(requested)
        if spec is None:
            raise ProviderError(
                message=f"Unknown ElevenLabs video model '{requested}'.",
                provider=self.provider_id,
                status_code=400,
                how_to_fix=f"Choose one of: {', '.join(VIDEO_MODELS)}.",
            )

        aspect = _pick(str(params.get("aspect_ratio") or "16:9"), spec.aspects, "16:9")
        duration = _snap_duration(params.get("duration_secs"), spec.durations, spec.default_duration)
        resolution = str(params.get("resolution") or spec.default_resolution)
        if resolution not in spec.resolutions:
            resolution = spec.default_resolution
        body: dict[str, Any] = {
            "model_id": spec.model_id,
            "prompt": prompt,
            "aspect_ratio": aspect,
            "duration_secs": duration,
            "resolution": resolution,
            "generate_audio": bool(params.get("generate_audio", True)),
        }
        negative = str(params.get("negative_prompt") or "").strip()
        if negative and spec.negative_prompt:
            body["negative_prompt"] = negative
        seed = _optional_int(params.get("seed"))
        if seed is not None:
            body["seed"] = seed
        for path in reference_files or []:
            inline = _inline_image(path)
            if inline:
                body["start_frame"] = inline
                break

        await _report(progress_cb, 15, f"Generating video with {spec.label}...")
        generation_id, content, _result = await self._submit_and_collect(
            kind="video",
            body=body,
            api_key=key,
            progress_cb=progress_cb,
            poll_interval=10.0,
            timeout_s=600.0,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        out_file = output_dir / f"{filename_stem}.mp4"
        out_file.write_bytes(content)
        thumb_path = out_file.with_name(f"{out_file.stem}_thumb.png")
        Image.new("RGB", (256, 144), (24, 24, 28)).save(thumb_path, format="PNG")
        return ProviderResult(
            provider=self.provider_id,
            model=prefixed_model_id(spec.model_id),
            file_path=f"assets/{out_file.name}",
            thumbnail_path=f"assets/{thumb_path.name}",
            cost_usd=spec.usd,
            cost_estimated=True,
            seed=seed,
            metadata={
                "generation_id": generation_id,
                "aspect_ratio": aspect,
                "duration_secs": duration,
                "resolution": resolution,
                "capability": "text_to_video",
                "video_pending_studio": True,
            },
        )

    async def _submit_and_collect(
        self,
        *,
        kind: str,
        body: dict[str, Any],
        api_key: str,
        progress_cb: ProgressCallback | None,
        poll_interval: float,
        timeout_s: float,
    ) -> tuple[str, bytes, dict[str, Any]]:
        headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
        create_url = f"{API_BASE}/v1/flows/{kind}"
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            created = await client.post(create_url, headers=headers, json=body)
            if created.status_code >= 400:
                raise _http_error(created)
            payload = created.json()
            generation_id = str(payload.get("id") or "")
            if not generation_id:
                raise ProviderError(
                    message="ElevenLabs did not return a generation id.",
                    provider=self.provider_id,
                    status_code=502,
                    retryable=True,
                )
            result = await self._poll(
                client,
                f"{create_url}/{generation_id}",
                headers,
                progress_cb,
                poll_interval,
                timeout_s,
            )
            content_url = str(result.get("content_url") or "")
            if not content_url:
                raise ProviderError(
                    message="ElevenLabs completed the generation without a download URL.",
                    provider=self.provider_id,
                    status_code=502,
                    retryable=True,
                )
            await _report(progress_cb, 85, "Downloading ElevenLabs result...")
            media = await client.get(content_url)
            if media.status_code >= 400:
                raise _http_error(media)
            return generation_id, media.content, result

    async def _poll(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        progress_cb: ProgressCallback | None,
        interval: float,
        timeout_s: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                raise _http_error(resp)
            data = resp.json()
            status = str(data.get("status") or "")
            if status == "completed":
                return data
            if status == "failed":
                reason = str(data.get("failure_reason") or "")
                raise ProviderError(
                    message=str(data.get("error_message") or "ElevenLabs generation failed."),
                    provider=self.provider_id,
                    status_code=422,
                    retryable=reason in {"timeout", "internal_error", "dependency_failed"},
                    how_to_fix="Adjust the prompt or references and generate again. Failed generations are not charged.",
                )
            if time.monotonic() >= deadline:
                raise ProviderError(
                    message="ElevenLabs generation timed out while polling.",
                    provider=self.provider_id,
                    status_code=504,
                    retryable=True,
                    how_to_fix="Try again. Image polls wait about 3 minutes; video polls wait about 10 minutes.",
                )
            await _report(progress_cb, 55, f"ElevenLabs status: {status or 'pending'}...")
            await asyncio.sleep(interval)

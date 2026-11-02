"""Stability AI Stable Audio Provider Adapter (music + sound effects).

Verified against the Stability docs (https://docs.stability.ai/api-reference/stable-audio-2/text-to-audio.md):
- `POST https://api.stability.ai/v2beta/audio/stable-audio-2/text-to-audio` (path keeps `stable-audio-2`
  even for the `stable-audio-2.5` model), synchronous, multipart/form-data.
- Fields: prompt (<=10000 chars), duration (1-190 s), seed, steps, cfg_scale, model
  (`stable-audio-2` | `stable-audio-2.5`), output_format (`mp3` | `wav`).
- `Authorization: Bearer <key>`, `Accept: audio/*` returns the raw audio bytes.
- Credits: 8 per 2.5 result; 17 + 0.06*steps for 2.0. (Stable Audio 3.0 is an async service; not used here.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from forge.config import load_config
from forge.providers.base import (
    CostEstimate,
    ProgressCallback,
    ProviderAdapter,
    ProviderCapability,
    ProviderError,
    ProviderResult,
)
from forge.providers.elevenlabs_audio import write_silent_mp3

STABILITY_BASE_URL = "https://api.stability.ai"
TEXT_TO_AUDIO_PATH = "/v2beta/audio/stable-audio-2/text-to-audio"
DEFAULT_MODEL = "stable-audio-2.5"
USD_PER_CREDIT = 0.01


class StabilityAudioAdapter(ProviderAdapter):
    provider_id = "stability_audio"
    display_name = "Stable Audio"
    capabilities = [ProviderCapability.TEXT_TO_MUSIC, ProviderCapability.SOUND_EFFECT]

    def __init__(self, api_key: str | None = None, demo_mode: bool | None = None) -> None:
        self._explicit_key = api_key
        self._explicit_demo = demo_mode

    def _key_and_demo(self) -> tuple[str, bool]:
        cfg = load_config()
        key = self._explicit_key if self._explicit_key is not None else cfg.get_key("stability")
        if self._explicit_demo is not None:
            demo = self._explicit_demo
        elif cfg.demo_mode:
            demo = True
        else:
            demo = not bool(key)
        return key, demo

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "duration": {"type": "number", "default": 30, "minimum": 1, "maximum": 190, "description": "Length in seconds"},
                "model": {"type": "string", "default": DEFAULT_MODEL, "enum": ["stable-audio-2.5", "stable-audio-2"]},
                "seed": {"type": "integer", "minimum": 0, "maximum": 4294967294},
                "steps": {"type": "integer", "description": "4-8 for 2.5, 30-100 for 2.0"},
                "cfg_scale": {"type": "number", "minimum": 1, "maximum": 25, "description": "Stable Audio 2.0 only"},
            },
        }

    def estimate_cost(self, capability: ProviderCapability, params: dict[str, Any]) -> CostEstimate:
        model = params.get("model") or DEFAULT_MODEL
        credits = 8.0 if model == "stable-audio-2.5" else 17 + 0.06 * int(params.get("steps") or 50)
        return CostEstimate(
            provider=self.provider_id,
            model=model,
            estimated_usd=round(credits * USD_PER_CREDIT, 3),
            credits=credits,
            unit_description=f"{credits:g} Stability credits (~$0.01 each)",
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
        params = params or {}
        key, is_demo = self._key_and_demo()
        model = str(params.get("model") or DEFAULT_MODEL)
        # Music defaults to a 30 s clip; sound effects default to a short 5 s one.
        default_duration = 30 if capability == ProviderCapability.TEXT_TO_MUSIC else 5
        duration = max(1.0, min(190.0, float(params.get("duration") or default_duration)))
        est = self.estimate_cost(capability, params)
        out_path = output_dir / f"{filename_stem}.mp3"

        async def _notify(pct: int, msg: str) -> None:
            if progress_cb:
                res = progress_cb(pct, msg)
                if hasattr(res, "__await__"):
                    await res

        if is_demo:
            await _notify(50, "Composing placeholder audio...")
            await asyncio.sleep(0.05)
            write_silent_mp3(out_path, min(duration, 3.0))
            await _notify(100, "Audio ready")
            return ProviderResult(
                provider="demo",
                model=model,
                file_path=f"assets/{out_path.name}",
                cost_usd=est.estimated_usd,
                cost_estimated=True,
                metadata={"demo_mode": True, "capability": capability.value, "duration": duration},
            )

        form: dict[str, str] = {
            "prompt": prompt[:10000],
            "duration": f"{duration:g}",
            "model": model,
            "output_format": "mp3",
        }
        for field in ("seed", "steps", "cfg_scale"):
            if params.get(field) is not None:
                form[field] = str(params[field])
        headers = {"Authorization": f"Bearer {key}", "Accept": "audio/*"}

        await _notify(10, "Requesting audio from Stable Audio...")
        async with httpx.AsyncClient(timeout=180.0) as client:
            async def _request() -> httpx.Response:
                # Stability expects multipart/form-data; `files` forces it even for text-only fields.
                resp = await client.post(
                    f"{STABILITY_BASE_URL}{TEXT_TO_AUDIO_PATH}",
                    headers=headers,
                    files={k: (None, v) for k, v in form.items()},
                )
                if resp.status_code in (401, 403):
                    raise ProviderError(
                        f"Stability rejected the request ({resp.status_code}): {resp.text[:160]}",
                        provider=self.provider_id,
                        status_code=resp.status_code,
                        how_to_fix="Set a valid STABILITY_API_KEY in Settings; 403 can also mean content moderation.",
                    )
                if resp.status_code == 402:
                    raise ProviderError(
                        "Insufficient Stability credits (402).",
                        provider=self.provider_id,
                        status_code=402,
                        how_to_fix="Top up your Stability credits or switch to Demo Mode.",
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise ProviderError(
                        f"Stability rate limit or server error ({resp.status_code}).",
                        provider=self.provider_id,
                        status_code=resp.status_code,
                        retryable=True,
                    )
                if resp.status_code >= 400:
                    raise ProviderError(
                        f"Stability rejected the request ({resp.status_code}): {resp.text[:240]}",
                        provider=self.provider_id,
                        status_code=resp.status_code,
                        how_to_fix="Prompts must be English and not mimic copyrighted audio.",
                    )
                return resp

            resp = await self.with_retries(_request)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        seed = resp.headers.get("seed")
        await _notify(100, "Audio ready")
        return ProviderResult(
            provider=self.provider_id,
            model=model,
            file_path=f"assets/{out_path.name}",
            cost_usd=est.estimated_usd,
            cost_estimated=True,
            seed=int(seed) if seed and seed.isdigit() else None,
            metadata={"capability": capability.value, "duration": duration},
        )

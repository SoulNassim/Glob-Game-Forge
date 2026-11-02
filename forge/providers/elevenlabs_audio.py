"""ElevenLabs Audio Provider Adapter (sound effects + text-to-speech).

Endpoints (https://elevenlabs.io/docs/api-reference):
- Sound effects: `POST https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_44100_128`
- Text-to-speech: `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128`
- Voices: `GET https://api.elevenlabs.io/v1/voices`
Both generation endpoints answer with the raw audio bytes, saved as `.mp3`. Uses the same
ElevenLabs key as image generation.
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

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"
OUTPUT_FORMAT = "mp3_44100_128"
DEFAULT_TTS_MODEL = "eleven_multilingual_v2"
# One MPEG-1 Layer III frame (128 kbps, 44.1 kHz, ~26 ms of silence).
_SILENT_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def write_silent_mp3(path: Path, seconds: float = 1.0) -> None:
    """Write a valid silent MP3 of roughly `seconds` length (used by demo mode)."""
    frames = max(1, int(seconds / 0.026))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_SILENT_MP3_FRAME * frames)


async def _notify(progress_cb: ProgressCallback | None, pct: int, msg: str) -> None:
    if progress_cb:
        res = progress_cb(pct, msg)
        if hasattr(res, "__await__"):
            await res


async def list_voices() -> list[dict[str, Any]]:
    """Return the account's voices as [{id, name, category, preview_url}] ([] without a key)."""
    key = load_config().get_key("elevenlabs")
    if not key:
        return []
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{ELEVENLABS_BASE_URL}/v1/voices", headers={"xi-api-key": key})
    if resp.status_code >= 400:
        raise ProviderError(
            f"ElevenLabs voices request failed ({resp.status_code}).",
            provider="elevenlabs_audio",
            status_code=resp.status_code,
        )
    return [
        {
            "id": v.get("voice_id", ""),
            "name": v.get("name", ""),
            "category": v.get("category", ""),
            "preview_url": v.get("preview_url", ""),
        }
        for v in resp.json().get("voices", [])
    ]


class ElevenLabsAudioAdapter(ProviderAdapter):
    provider_id = "elevenlabs_audio"
    display_name = "ElevenLabs Audio"
    capabilities = [ProviderCapability.SOUND_EFFECT, ProviderCapability.TEXT_TO_SPEECH]

    def __init__(self, api_key: str | None = None, demo_mode: bool | None = None) -> None:
        self._explicit_key = api_key
        self._explicit_demo = demo_mode

    def _key_and_demo(self) -> tuple[str, bool]:
        cfg = load_config()
        key = self._explicit_key if self._explicit_key is not None else cfg.get_key("elevenlabs")
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
                "duration": {"type": "number", "minimum": 0.5, "maximum": 30, "description": "SFX length in seconds (omit for auto)"},
                "prompt_influence": {"type": "number", "minimum": 0, "maximum": 1, "description": "SFX: how closely to follow the prompt"},
                "loop": {"type": "boolean", "default": False, "description": "SFX: make the sound loop seamlessly"},
                "voice_id": {"type": "string", "description": "Text-to-speech voice (required for TTS; see list_voices)"},
                "model_id": {"type": "string", "default": DEFAULT_TTS_MODEL, "description": "Text-to-speech model"},
            },
        }

    def estimate_cost(self, capability: ProviderCapability, params: dict[str, Any]) -> CostEstimate:
        if capability == ProviderCapability.TEXT_TO_SPEECH:
            # ~1 credit per character; roughly $0.0003 per credit on mid-tier plans.
            chars = len(str(params.get("text") or params.get("prompt") or "")) or 200
            usd = round(max(0.01, chars * 0.0003), 3)
            return CostEstimate(
                provider=self.provider_id,
                model=params.get("model_id") or DEFAULT_TTS_MODEL,
                estimated_usd=usd,
                credits=float(chars),
                unit_description="~1 credit per character",
            )
        # SFX: ~100 credits per auto-length clip, 20 per second when a duration is set.
        duration = params.get("duration")
        credits = float(duration) * 20 if duration else 100.0
        return CostEstimate(
            provider=self.provider_id,
            model="eleven_text_to_sound_v2",
            estimated_usd=round(credits * 0.0003, 3),
            credits=credits,
            unit_description="~20 credits per second (100 for auto length)",
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
        if capability not in self.capabilities:
            raise ProviderError(
                f"ElevenLabs Audio does not support {capability.value}.",
                provider=self.provider_id,
                status_code=400,
                how_to_fix="Use sound_effect or text_to_speech.",
            )
        key, is_demo = self._key_and_demo()
        is_tts = capability == ProviderCapability.TEXT_TO_SPEECH
        voice_id = str(params.get("voice_id") or "").strip()
        if is_tts and not voice_id:
            raise ProviderError(
                "Text-to-speech needs a voice_id.",
                provider=self.provider_id,
                status_code=400,
                how_to_fix="Pick a voice (list_voices) and pass its id as voice_id.",
            )
        model = (params.get("model_id") or DEFAULT_TTS_MODEL) if is_tts else "eleven_text_to_sound_v2"
        est = self.estimate_cost(capability, {**params, "text": prompt})
        out_path = output_dir / f"{filename_stem}.mp3"

        if is_demo:
            await _notify(progress_cb, 50, "Synthesizing placeholder audio...")
            await asyncio.sleep(0.05)
            write_silent_mp3(out_path, float(params.get("duration") or 1.0))
            await _notify(progress_cb, 100, "Audio ready")
            return ProviderResult(
                provider="demo",
                model=model,
                file_path=f"assets/{out_path.name}",
                cost_usd=est.estimated_usd,
                cost_estimated=True,
                metadata={"demo_mode": True, "capability": capability.value},
            )

        headers = {"xi-api-key": key}
        if is_tts:
            url = f"{ELEVENLABS_BASE_URL}/v1/text-to-speech/{voice_id}"
            payload: dict[str, Any] = {"text": prompt, "model_id": model}
        else:
            url = f"{ELEVENLABS_BASE_URL}/v1/sound-generation"
            payload = {"text": prompt}
            if params.get("duration") is not None:
                payload["duration_seconds"] = max(0.5, min(30.0, float(params["duration"])))
            if params.get("prompt_influence") is not None:
                payload["prompt_influence"] = max(0.0, min(1.0, float(params["prompt_influence"])))
            if params.get("loop") is not None:
                payload["loop"] = bool(params["loop"])

        await _notify(progress_cb, 10, "Requesting audio from ElevenLabs...")
        async with httpx.AsyncClient(timeout=120.0) as client:
            async def _request() -> bytes:
                resp = await client.post(url, params={"output_format": OUTPUT_FORMAT}, headers=headers, json=payload)
                if resp.status_code in (401, 403):
                    raise ProviderError(
                        f"ElevenLabs API key was rejected ({resp.status_code}).",
                        provider=self.provider_id,
                        status_code=resp.status_code,
                        how_to_fix="Set a valid ELEVENLABS_API_KEY in Settings.",
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise ProviderError(
                        f"ElevenLabs rate limit or server error ({resp.status_code}).",
                        provider=self.provider_id,
                        status_code=resp.status_code,
                        retryable=True,
                    )
                if resp.status_code >= 400:
                    raise ProviderError(
                        f"ElevenLabs rejected the request ({resp.status_code}): {resp.text[:240]}",
                        provider=self.provider_id,
                        status_code=resp.status_code,
                        how_to_fix="Check the prompt, duration and voice_id.",
                    )
                return resp.content

            audio = await self.with_retries(_request)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        await _notify(progress_cb, 100, "Audio ready")
        return ProviderResult(
            provider=self.provider_id,
            model=model,
            file_path=f"assets/{out_path.name}",
            cost_usd=est.estimated_usd,
            cost_estimated=True,
            metadata={"capability": capability.value, "voice_id": voice_id, "bytes": len(audio)},
        )

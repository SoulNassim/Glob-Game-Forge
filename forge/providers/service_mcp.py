"""Studio models of the services the user signed in to (`svc:<service>/<model>`); see forge/service_gen.py."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from forge import service_gen, services
from forge.providers.base import CostEstimate, ProgressCallback, ProviderAdapter, ProviderCapability, ProviderError, ProviderResult

THREE_D = {ProviderCapability.IMAGE_TO_3D, ProviderCapability.MULTI_IMAGE_TO_3D}
AUDIO = {ProviderCapability.SOUND_EFFECT: "sfx", ProviderCapability.TEXT_TO_SPEECH: "tts", ProviderCapability.TEXT_TO_MUSIC: "music"}


class ServiceAdapter(ProviderAdapter):
    provider_id = "service"
    display_name = "Signed-in service"
    capabilities = [ProviderCapability.TEXT_TO_IMAGE, ProviderCapability.IMAGE_EDIT, *THREE_D, *AUDIO]

    def parameter_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"model": {"type": "string", "description": "svc:<service>/<model>"}}}

    def estimate_cost(self, capability: ProviderCapability, params: dict[str, Any]) -> CostEstimate:
        # Paid in the service's own credits, which Glob can't price up front.
        return CostEstimate(provider=self.provider_id, model=str(params.get("model") or ""), estimated_usd=0.0, unit_description="service credits")

    async def execute(self, capability: ProviderCapability, prompt: str, output_dir: Path, filename_stem: str,
                      reference_files: list[Path] | None = None, params: dict[str, Any] | None = None,
                      progress_cb: ProgressCallback | None = None) -> ProviderResult:
        params = params or {}
        model = str(params.get("model") or "")
        kind = "3d" if capability in THREE_D else "audio" if capability in AUDIO else "image"
        try:
            if kind == "audio":
                service = model.removeprefix(service_gen.PREFIX).split("/")[0]
                out = await asyncio.to_thread(service_gen.generate_audio, service, AUDIO[capability], prompt, output_dir, filename_stem, params)
            else:
                out = await asyncio.to_thread(service_gen.generate, model, kind, prompt, list(reference_files or []), output_dir, filename_stem,
                                              str(params.get("aspect_ratio") or ""), bool(params.get("transparent")))
        except (service_gen.ServiceGenError, services.ServiceError) as exc:
            raise ProviderError(str(exc), provider=self.provider_id, status_code=400, how_to_fix="Check the service under Settings → Generators.") from exc
        except Exception as exc:  # network or download failure
            raise ProviderError(f"The service failed: {exc}", provider=self.provider_id, retryable=False) from exc
        return ProviderResult(provider=self.provider_id, model=model, file_path=f"assets/{out.name}", thumbnail_path=f"assets/{out.name}" if kind == "image" else "",
                              cost_usd=0.0, cost_estimated=True, metadata={"service": model.removeprefix(service_gen.PREFIX).split("/")[0]})

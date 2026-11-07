"""Tripo 3D Provider Adapter (Tripo v3 API, see `forge/tripo.py`).

Flow: upload image(s) -> create task -> poll task -> download the GLB server-side.
- Text, image and multi-view generation; H series (v3.1, v3.0, v2.5) and P series (P1, P2, low-poly) models.
- `quad` output is an FBX on Tripo's side, so Glob asks for a GLB conversion of it and keeps the FBX next to it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from forge import tripo
from forge.config import load_config
from forge.providers.base import (
    CostEstimate,
    ProgressCallback,
    ProviderAdapter,
    ProviderCapability,
    ProviderError,
    ProviderResult,
)
from forge.storage.demo_assets import generate_demo_image
from forge.storage.glb_generator import generate_demo_glb

KIND = {
    ProviderCapability.IMAGE_TO_3D: "image",
    ProviderCapability.MULTI_IMAGE_TO_3D: "multiview",
    ProviderCapability.TEXT_TO_3D: "text",
}
DEFAULT_MODEL_VERSION = tripo.DEFAULT_MODEL
USD_PER_CREDIT = tripo.USD_PER_CREDIT


class Tripo3DAdapter(ProviderAdapter):
    provider_id = "tripo"
    display_name = "Tripo"
    capabilities = [
        ProviderCapability.IMAGE_TO_3D,
        ProviderCapability.MULTI_IMAGE_TO_3D,
        ProviderCapability.TEXT_TO_3D,
    ]

    def __init__(self, api_key: str | None = None, demo_mode: bool | None = None) -> None:
        self._explicit_key = api_key
        self._explicit_demo = demo_mode

    def _get_key_and_mode(self) -> tuple[str, bool]:
        cfg = load_config()
        key = self._explicit_key if self._explicit_key is not None else cfg.get_key("tripo")
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
                "model": {"type": "string", "default": DEFAULT_MODEL_VERSION, "enum": list(tripo.MODELS)},
                "texture": {"type": "boolean", "default": True},
                "pbr": {"type": "boolean", "default": True},
                "face_limit": {"type": "integer", "minimum": 1, "description": "Max triangle count (omit for auto)"},
                "quad": {"type": "boolean", "default": False},
                "smart_low_poly": {"type": "boolean", "default": False},
                "auto_size": {"type": "boolean", "default": False, "description": "Scale the model to real-world metres"},
                "texture_quality": {"type": "string", "default": "standard", "enum": list(tripo.TEXTURE_QUALITIES)},
                "geometry_quality": {"type": "string", "default": "standard", "enum": list(tripo.GEOMETRY_QUALITIES)},
                "negative_prompt": {"type": "string", "description": "Text-to-3D only"},
            },
        }

    @staticmethod
    def _model(params: dict[str, Any]) -> str:
        model = str(params.get("model") or params.get("model_version") or load_config().tripo_model or DEFAULT_MODEL_VERSION)
        return model if model[:1] in "vVpP" and not model.startswith(("svc:", "meshy")) else DEFAULT_MODEL_VERSION

    def estimate_cost(self, capability: ProviderCapability, params: dict[str, Any]) -> CostEstimate:
        credits = tripo.estimate_credits(KIND.get(capability, "image"), params)
        usd = round(credits * USD_PER_CREDIT, 3)
        return CostEstimate(
            provider=self.provider_id,
            model=self._model(params),
            estimated_usd=usd,
            credits=credits,
            unit_description=f"~{int(credits)} Tripo credits (~${usd:.2f})",
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
        params = {**tripo.settings_from_config(load_config()), **(params or {})}
        key, is_demo = self._get_key_and_mode()
        model = self._model(params)
        est = self.estimate_cost(capability, params)
        out_glb = output_dir / f"{filename_stem}.glb"

        async def _notify(pct: int, msg: str) -> None:
            if progress_cb:
                res = progress_cb(pct, msg)
                if hasattr(res, "__await__"):
                    await res

        if capability not in KIND:
            raise ProviderError(
                f"Tripo does not support {capability.value}.",
                provider=self.provider_id,
                status_code=400,
                how_to_fix="Use image_to_3d, multi_image_to_3d or text_to_3d.",
            )

        if is_demo:
            for pct, msg in (
                (30, "Building base mesh..."),
                (70, "Texturing..."),
                (100, "GLB mesh ready"),
            ):
                await _notify(pct, msg)
                await asyncio.sleep(0.05)
            stats = generate_demo_glb(output_path=out_glb, prompt=prompt, palette=params.get("palette"), write_obj_also=False)
            if reference_files and reference_files[0].exists() and reference_files[0].suffix.lower() == ".png":
                thumb_rel = f"assets/{reference_files[0].name}"
            else:
                thumb_path = output_dir / f"{filename_stem}_thumb.png"
                generate_demo_image(thumb_path, prompt=prompt, palette=params.get("palette"))
                thumb_rel = f"assets/{thumb_path.name}"
            return ProviderResult(
                provider="demo",
                model=model,
                file_path=f"assets/{out_glb.name}",
                thumbnail_path=thumb_rel,
                cost_usd=est.estimated_usd,
                cost_estimated=True,
                metadata={**stats, "demo_mode": True, "capability": capability.value},
            )

        refs = [p for p in (reference_files or []) if p.exists()]
        if capability == ProviderCapability.IMAGE_TO_3D and not refs:
            raise ProviderError("Image-to-3D requires a source reference image.", provider=self.provider_id, status_code=400)
        if capability == ProviderCapability.MULTI_IMAGE_TO_3D and len(refs) < 2:
            raise ProviderError(
                "Multi-view 3D needs at least 2 images (front first, then left, back, right).",
                provider=self.provider_id,
                status_code=400,
            )

        notify = _notify
        poll_interval = float(params.get("poll_interval", 2.5))
        max_polls = int(params.get("max_polls", 360))
        text = capability == ProviderCapability.TEXT_TO_3D

        async with httpx.AsyncClient(timeout=120.0) as client:
            async def _upload(path: Path) -> str:
                return await self.with_retries(lambda: tripo.upload(client, key, path))

            body: dict[str, Any] = {"model": model}
            if text:
                body["prompt"] = prompt[:1024]
            elif capability == ProviderCapability.IMAGE_TO_3D:
                await _notify(5, "Uploading reference image...")
                body["input"] = await _upload(refs[0])
            else:
                await _notify(5, "Uploading reference images...")
                tokens = [await _upload(p) for p in refs[:4]]
                body["inputs"] = [{view: token} for view, token in zip(tripo.VIEW_KEYS, tokens)]  # front, left, back, right
            body.update(tripo.generation_fields(model, params, text=text))

            task_id = await self.with_retries(lambda: tripo.create(client, key, tripo.ENDPOINTS[KIND[capability]], body))
            task_data = await tripo.wait(client, key, task_id, notify, poll_interval, max_polls)

            url = tripo.model_url(task_data)
            if not url:
                raise ProviderError("Tripo task succeeded but returned no model URL.", provider=self.provider_id, status_code=500)
            out_glb.parent.mkdir(parents=True, exist_ok=True)
            extra_files: dict[str, str] = {}
            data = await tripo.download(client, url)
            if not data.startswith(b"glTF"):
                # Quad meshes come as FBX. Keep it, and ask Tripo for the GLB the rest of Glob works with.
                ext = url.split("?")[0].rsplit(".", 1)[-1].lower() or "fbx"
                raw_path = output_dir / f"{filename_stem}.{ext}"
                raw_path.write_bytes(data)
                extra_files[ext] = f"assets/{raw_path.name}"
                await _notify(92, "Converting to GLB...")
                _, conv = await tripo.convert(client, key, task_id, "GLTF", {"face_limit": body.get("face_limit")} if body.get("face_limit") else None,
                                              notify=None)
                converted = await tripo.download(client, tripo.model_url(conv))
                if not converted.startswith(b"glTF"):
                    raise ProviderError("Tripo made a quad mesh but could not give it as a GLB.", provider=self.provider_id, status_code=502,
                                        how_to_fix="Turn Quads off, or use the FBX saved next to this asset.")
                data = converted
                task_data["credits_consumed"] = float(task_data.get("credits_consumed") or 0) + float(conv.get("credits_consumed") or 0)
            out_glb.write_bytes(data)

            thumb_rel = ""
            rendered = (task_data.get("output") or {}).get("rendered_image_url") or (task_data.get("output") or {}).get("rendered_image")
            if rendered:
                try:
                    thumb = await tripo.download(client, rendered)
                    ext = "png" if thumb.startswith(b"\x89PNG") else "webp"
                    t_path = output_dir / f"{filename_stem}_thumb.{ext}"
                    t_path.write_bytes(thumb)
                    thumb_rel = f"assets/{t_path.name}"
                except Exception:
                    pass

        reported = task_data.get("credits_consumed")
        credits = float(reported) if reported is not None else est.credits
        return ProviderResult(
            provider=self.provider_id,
            model=model,
            file_path=f"assets/{out_glb.name}",
            thumbnail_path=thumb_rel,
            extra_files=extra_files,
            cost_usd=round(credits * USD_PER_CREDIT, 3),
            cost_estimated=reported is None,
            metadata={"tripo_task_id": task_id, "credits": credits, "capability": capability.value},
        )

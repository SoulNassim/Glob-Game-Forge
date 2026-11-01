"""Meshy 3D Provider Adapter.

Verified against October 2026 official Meshy API documentation (https://docs.meshy.ai/en/api):
- Image-to-3D: `POST https://api.meshy.ai/openapi/v1/image-to-3d`
- Multi-Image-to-3D: `POST https://api.meshy.ai/openapi/v1/multi-image-to-3d`
- Text-to-3D: `POST https://api.meshy.ai/openapi/v2/text-to-3d`
- Retexture: `POST https://api.meshy.ai/openapi/v1/retexture`
- Remesh: `POST https://api.meshy.ai/openapi/v1/remesh`
- Models:
  - Standard: `meshy-7.1` (`latest`), `meshy-6`, `meshy-6-lite`
  - Smart Topology (`model_type: "smart-topology"`): `meshy-t2.5`, `meshy-t2`
    (replaces `lowpoly`, which retires Oct 30, 2026)
- Server-side download of `.glb`, `.obj`, `.fbx` and `thumbnail_url` into the project folder
  so assets survive Meshy signed URL expiration.
"""

from __future__ import annotations

import asyncio
import base64
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
from forge.storage.demo_assets import generate_demo_image
from forge.storage.glb_generator import generate_demo_glb


MESHY_BASE_URL = "https://api.meshy.ai"

CAPABILITY_ENDPOINTS = {
    ProviderCapability.IMAGE_TO_3D: "/openapi/v1/image-to-3d",
    ProviderCapability.MULTI_IMAGE_TO_3D: "/openapi/v1/multi-image-to-3d",
    ProviderCapability.TEXT_TO_3D: "/openapi/v2/text-to-3d",
    ProviderCapability.RETEXTURE_3D: "/openapi/v1/retexture",
    ProviderCapability.REMESH_3D: "/openapi/v1/remesh",
}


def _file_to_data_uri(path: Path) -> str:
    ext = path.suffix.lower()
    mime = "image/png"
    if ext in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif ext == ".webp":
        mime = "image/webp"
    elif ext == ".glb":
        mime = "model/gltf-binary"
    b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


class Meshy3DAdapter(ProviderAdapter):
    provider_id = "meshy"
    display_name = "Meshy 3D"
    capabilities = [
        ProviderCapability.IMAGE_TO_3D,
        ProviderCapability.MULTI_IMAGE_TO_3D,
        ProviderCapability.TEXT_TO_3D,
        ProviderCapability.RETEXTURE_3D,
        ProviderCapability.REMESH_3D,
    ]

    def __init__(self, api_key: str | None = None, demo_mode: bool | None = None) -> None:
        self._explicit_key = api_key
        self._explicit_demo = demo_mode

    def _get_key_and_mode(self) -> tuple[str, bool, str]:
        cfg = load_config()
        key = self._explicit_key if self._explicit_key is not None else cfg.get_key("meshy")
        if self._explicit_demo is not None:
            demo = self._explicit_demo
        elif cfg.demo_mode:
            demo = True
        else:
            demo = not bool(key)
        return key, demo, cfg.default_3d_model

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "model_type": {
                    "type": "string",
                    "default": "standard",
                    "enum": ["standard", "smart-topology"],
                    "description": "Mesh topology type (smart-topology uses meshy-t2.5 for clean game triangles)",
                },
                "ai_model": {
                    "type": "string",
                    "default": "meshy-7.1",
                    "enum": ["meshy-7.1", "latest", "meshy-6", "meshy-6-lite", "meshy-t2.5", "meshy-t2"],
                },
                "geometry_resolution": {
                    "type": "string",
                    "default": "standard",
                    "enum": ["standard", "2k", "4k"],
                },
                "should_texture": {"type": "boolean", "default": True},
                "enable_pbr": {"type": "boolean", "default": True},
                "target_polycount": {"type": "integer", "default": 12000, "minimum": 100, "maximum": 300000},
                "topology": {"type": "string", "default": "triangle", "enum": ["triangle", "quad"]},
                "pose_mode": {"type": "string", "default": "", "enum": ["", "a-pose", "t-pose"]},
                "target_formats": {
                    "type": "array",
                    "default": ["glb", "obj"],
                    "items": {"type": "string", "enum": ["glb", "obj", "fbx", "usdz", "stl"]},
                },
            },
        }

    def estimate_cost(self, capability: ProviderCapability, params: dict[str, Any]) -> CostEstimate:
        _, _, default_model = self._get_key_and_mode()
        model = params.get("ai_model") or default_model
        should_texture = params.get("should_texture", True)
        credits = 30.0 if should_texture else 20.0
        if capability == ProviderCapability.REMESH_3D:
            credits = 5.0
        elif capability == ProviderCapability.RETEXTURE_3D:
            credits = 10.0
        usd = round(credits * 0.01, 3)  # ~$0.01 per Meshy credit
        return CostEstimate(
            provider=self.provider_id,
            model=model,
            estimated_usd=usd,
            credits=credits,
            unit_description=f"{int(credits)} Meshy credits (~${usd:.2f})",
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
        cfg = load_config()
        key, is_demo, default_model = self._get_key_and_mode()
        ai_model = params.get("ai_model") or default_model or "meshy-7.1"
        model_type = params.get("model_type") or cfg.mesh_model_type or "standard"
        if ai_model in ("meshy-t2", "meshy-t2.5"):
            model_type = "smart-topology"
        geometry_resolution = params.get("geometry_resolution") or cfg.mesh_geometry_resolution or "standard"
        should_texture = bool(params.get("should_texture", cfg.mesh_should_texture))
        enable_pbr = bool(params.get("enable_pbr", cfg.mesh_enable_pbr)) and should_texture
        texture_resolution = str(params.get("texture_resolution") or cfg.mesh_texture_resolution or "2k")
        target_polycount = int(params.get("target_polycount") or cfg.mesh_target_polycount or 12000)
        topology = str(params.get("topology") or cfg.mesh_topology or "triangle")
        should_remesh = bool(params.get("should_remesh", cfg.mesh_should_remesh))
        pose_mode = str(params.get("pose_mode") if "pose_mode" in params else cfg.mesh_pose_mode)
        origin_at = str(params.get("origin_at") or cfg.mesh_origin_at or "bottom")
        auto_size = bool(params.get("auto_size", cfg.mesh_auto_size))
        target_formats = params.get("target_formats") or cfg.mesh_target_formats or ["glb", "obj"]
        palette = params.get("palette")

        est = self.estimate_cost(capability, params)
        out_glb = output_dir / f"{filename_stem}.glb"

        if is_demo:
            # Simulate progressive 3D generation steps cleanly
            steps = [
                (25, "Constructing sparse 3D voxel hull..."),
                (60, f"Refining mesh topology ({ai_model}, target {target_polycount} tris)..."),
                (85, "Baking PBR base color, metallic & roughness maps..." if should_texture else "Finalizing untextured geometry..."),
                (100, "GLB mesh ready"),
            ]
            for pct, msg in steps:
                if progress_cb:
                    res = progress_cb(pct, msg)
                    if hasattr(res, "__await__"):
                        await res
                await asyncio.sleep(0.08)

            stats = generate_demo_glb(
                output_path=out_glb,
                prompt=prompt,
                palette=palette,
                write_obj_also="obj" in target_formats,
            )
            thumb_rel = ""
            if reference_files and reference_files[0].exists() and reference_files[0].suffix.lower() == ".png":
                thumb_rel = f"assets/{reference_files[0].name}"
            else:
                thumb_path = output_dir / f"{filename_stem}_thumb.png"
                generate_demo_image(thumb_path, prompt=prompt, palette=palette)
                thumb_rel = f"assets/{thumb_path.name}"

            extra_files: dict[str, str] = {}
            if stats.get("obj_path"):
                extra_files["obj"] = f"assets/{stats['obj_path']}"

            return ProviderResult(
                provider="demo",
                model=ai_model,
                file_path=f"assets/{out_glb.name}",
                thumbnail_path=thumb_rel,
                extra_files=extra_files,
                cost_usd=est.estimated_usd,
                cost_estimated=True,
                metadata={
                    **stats,
                    "demo_mode": True,
                    "capability": capability.value,
                    "should_texture": should_texture,
                    "enable_pbr": enable_pbr,
                    "model_type": model_type,
                },
            )

        # Live Meshy OpenAPI v1 / v2 execution
        endpoint_path = CAPABILITY_ENDPOINTS.get(capability, "/openapi/v1/image-to-3d")
        create_url = f"{MESHY_BASE_URL}{endpoint_path}"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        payload: dict[str, Any] = {
            "ai_model": ai_model,
            "should_texture": should_texture,
            "target_formats": target_formats,
        }
        if should_texture:
            payload["enable_pbr"] = enable_pbr
            if texture_resolution in ("2k", "4k", "8k") and ai_model != "meshy-6-lite":
                payload["texture_resolution"] = texture_resolution
            if params.get("texture_prompt"):
                payload["texture_prompt"] = str(params["texture_prompt"])[:800]

        if capability == ProviderCapability.IMAGE_TO_3D:
            if not reference_files or not reference_files[0].exists():
                raise ProviderError(
                    "Image-to-3D requires a source reference image.",
                    provider=self.provider_id,
                    status_code=400,
                )
            payload["image_url"] = _file_to_data_uri(reference_files[0])
            payload["model_type"] = model_type
            if model_type == "smart-topology":
                max_smart = 15000 if ai_model == "meshy-t2" else 30000
                payload["target_polycount"] = min(max_smart, max(100, target_polycount))
                payload["topology"] = "triangle"
            else:
                if geometry_resolution in ("2k", "4k") and ai_model in ("meshy-7.1", "latest"):
                    payload["geometry_resolution"] = geometry_resolution
                if should_remesh:
                    payload["should_remesh"] = True
                    payload["target_polycount"] = min(300000, max(100, target_polycount))
                    payload["topology"] = topology
            if pose_mode in ("a-pose", "t-pose"):
                payload["pose_mode"] = pose_mode
            if auto_size:
                payload["auto_size"] = True
            if origin_at in ("bottom", "center"):
                payload["origin_at"] = origin_at

        elif capability == ProviderCapability.MULTI_IMAGE_TO_3D:
            if not reference_files:
                raise ProviderError(
                    "Multi-Image-to-3D requires 1 to 4 reference images.",
                    provider=self.provider_id,
                    status_code=400,
                )
            payload["image_urls"] = [_file_to_data_uri(p) for p in reference_files[:4] if p.exists()]

        elif capability == ProviderCapability.TEXT_TO_3D:
            payload = {
                "mode": "preview",
                "prompt": prompt[:600],
                "ai_model": ai_model if ai_model != "meshy-t2.5" else "latest",
                "target_polycount": target_polycount,
            }

        elif capability == ProviderCapability.RETEXTURE_3D:
            if reference_files and reference_files[0].exists():
                payload["model_url"] = _file_to_data_uri(reference_files[0])
            payload["text_style_prompt"] = prompt[:600]

        async with httpx.AsyncClient(timeout=60.0) as client:
            async def _create_task() -> str:
                resp = await client.post(create_url, headers=headers, json=payload)
                if resp.status_code == 401:
                    raise ProviderError(
                        "Meshy API key is unauthorized (401).",
                        provider=self.provider_id,
                        status_code=401,
                        retryable=False,
                        how_to_fix="Set a valid MESHY_API_KEY in Settings.",
                    )
                if resp.status_code == 402:
                    raise ProviderError(
                        "Insufficient Meshy credits or paid plan required (402).",
                        provider=self.provider_id,
                        status_code=402,
                        retryable=False,
                        how_to_fix="Top up your Meshy credits or switch to Demo Mode.",
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise ProviderError(
                        f"Meshy rate limit or server error ({resp.status_code}).",
                        provider=self.provider_id,
                        status_code=resp.status_code,
                        retryable=True,
                    )
                if resp.status_code >= 400:
                    raise ProviderError(
                        f"Meshy API rejected request ({resp.status_code}): {resp.text[:240]}",
                        provider=self.provider_id,
                        status_code=resp.status_code,
                        retryable=False,
                    )
                return resp.json()["result"]

            task_id = await self.with_retries(_create_task)
            status_url = f"{create_url}/{task_id}"

            # Poll task until SUCCEEDED or FAILED
            poll_interval = float(params.get("poll_interval", 2.5))
            max_polls = int(params.get("max_polls", 240))
            task_data: dict[str, Any] = {}

            for _ in range(max_polls):
                resp = await client.get(status_url, headers=headers)
                if resp.status_code >= 400:
                    await asyncio.sleep(poll_interval)
                    continue
                task_data = resp.json()
                status = task_data.get("status", "PENDING")
                prog = int(task_data.get("progress", 0))
                if progress_cb:
                    res = progress_cb(prog, f"Meshy {status} ({prog}%)")
                    if hasattr(res, "__await__"):
                        await res
                if status == "SUCCEEDED":
                    break
                if status in ("FAILED", "CANCELED"):
                    err_msg = task_data.get("task_error", {}).get("message") or f"Meshy task {status}"
                    raise ProviderError(
                        f"Meshy 3D generation failed: {err_msg}",
                        provider=self.provider_id,
                        status_code=500,
                        retryable=True,
                    )
                await asyncio.sleep(poll_interval)
            else:
                raise ProviderError(
                    "Meshy task polling timed out.",
                    provider=self.provider_id,
                    status_code=504,
                    retryable=True,
                )

            # Download GLB and optional formats server-side so URLs never expire!
            model_urls = task_data.get("model_urls", {})
            glb_url = model_urls.get("glb")
            if not glb_url:
                raise ProviderError(
                    "Meshy task succeeded but returned no GLB URL.",
                    provider=self.provider_id,
                    status_code=500,
                )
            glb_resp = await client.get(glb_url)
            out_glb.parent.mkdir(parents=True, exist_ok=True)
            out_glb.write_bytes(glb_resp.content)

            extra_files = {}
            for fmt in ("obj", "fbx", "usdz", "stl"):
                if model_urls.get(fmt):
                    try:
                        f_resp = await client.get(model_urls[fmt])
                        if f_resp.status_code == 200:
                            fmt_path = output_dir / f"{filename_stem}.{fmt}"
                            fmt_path.write_bytes(f_resp.content)
                            extra_files[fmt] = f"assets/{fmt_path.name}"
                    except Exception:
                        pass

            thumb_rel = ""
            thumb_url = task_data.get("thumbnail_url")
            if thumb_url:
                try:
                    t_resp = await client.get(thumb_url)
                    if t_resp.status_code == 200:
                        t_path = output_dir / f"{filename_stem}_thumb.png"
                        t_path.write_bytes(t_resp.content)
                        thumb_rel = f"assets/{t_path.name}"
                except Exception:
                    pass

            consumed = float(task_data.get("consumed_credits", est.credits))
            return ProviderResult(
                provider=self.provider_id,
                model=ai_model,
                file_path=f"assets/{out_glb.name}",
                thumbnail_path=thumb_rel,
                extra_files=extra_files,
                cost_usd=round(consumed * 0.01, 3),
                cost_estimated=False,
                metadata={
                    "meshy_task_id": task_id,
                    "consumed_credits": consumed,
                    "capability": capability.value,
                },
            )

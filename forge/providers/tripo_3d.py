"""Tripo 3D Provider Adapter.

Flow: upload image(s) -> create task -> poll task -> download the GLB server-side.
- Base URL: `https://api.tripo3d.ai/v2/openapi`
- Upload: `POST /upload` (multipart `file`) -> `data.image_token` (older name) / `data.file_token`
- Create: `POST /task` with `type` = `image_to_model` | `multiview_to_model` | `text_to_model`
- Poll: `GET /task/{task_id}` -> `data.status`, `data.progress`, `data.output.{pbr_model,model,rendered_image}`
- Statuses: queued, running, success; failed, cancelled, unknown, banned, expired (terminal errors).
Note: platform.tripo3d.ai is a JS app, so these were confirmed from the community SDK/ComfyUI node and
guides rather than the official reference; the upload response and `consumed_credit` are best-effort.
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
from forge.storage.demo_assets import generate_demo_image
from forge.storage.glb_generator import generate_demo_glb

TRIPO_BASE_URL = "https://api.tripo3d.ai/v2/openapi"
DEFAULT_MODEL_VERSION = "v2.5-20250123"
TASK_TYPES = {
    ProviderCapability.IMAGE_TO_3D: "image_to_model",
    ProviderCapability.MULTI_IMAGE_TO_3D: "multiview_to_model",
    ProviderCapability.TEXT_TO_3D: "text_to_model",
}
# Approximate credits per task (textured); ~$0.01 per Tripo credit.
CREDITS = {
    ProviderCapability.TEXT_TO_3D: 30.0,
    ProviderCapability.IMAGE_TO_3D: 40.0,
    ProviderCapability.MULTI_IMAGE_TO_3D: 70.0,
}
USD_PER_CREDIT = 0.01
FAILED_STATUSES = {"failed", "cancelled", "unknown", "banned", "expired"}


def _image_type(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    return "jpeg" if ext == "jpg" else (ext or "png")


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
                "model_version": {"type": "string", "default": DEFAULT_MODEL_VERSION},
                "texture": {"type": "boolean", "default": True},
                "pbr": {"type": "boolean", "default": True},
                "face_limit": {"type": "integer", "minimum": 1, "description": "Max triangle count (omit for auto)"},
                "quad": {"type": "boolean", "default": False},
                "texture_quality": {"type": "string", "default": "standard", "enum": ["standard", "detailed"]},
                "negative_prompt": {"type": "string", "description": "Text-to-3D only"},
            },
        }

    def estimate_cost(self, capability: ProviderCapability, params: dict[str, Any]) -> CostEstimate:
        credits = CREDITS.get(capability, 40.0)
        if params.get("texture", True) is False:
            credits = max(10.0, credits - 10.0)
        usd = round(credits * USD_PER_CREDIT, 3)
        return CostEstimate(
            provider=self.provider_id,
            model=params.get("model_version") or DEFAULT_MODEL_VERSION,
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
        params = params or {}
        key, is_demo = self._get_key_and_mode()
        model_version = str(params.get("model_version") or DEFAULT_MODEL_VERSION)
        est = self.estimate_cost(capability, params)
        out_glb = output_dir / f"{filename_stem}.glb"

        async def _notify(pct: int, msg: str) -> None:
            if progress_cb:
                res = progress_cb(pct, msg)
                if hasattr(res, "__await__"):
                    await res

        if capability not in TASK_TYPES:
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
                model=model_version,
                file_path=f"assets/{out_glb.name}",
                thumbnail_path=thumb_rel,
                cost_usd=est.estimated_usd,
                cost_estimated=True,
                metadata={**stats, "demo_mode": True, "capability": capability.value},
            )

        refs = [p for p in (reference_files or []) if p.exists()]
        if capability == ProviderCapability.IMAGE_TO_3D and not refs:
            raise ProviderError(
                "Image-to-3D requires a source reference image.",
                provider=self.provider_id,
                status_code=400,
            )
        if capability == ProviderCapability.MULTI_IMAGE_TO_3D and len(refs) < 2:
            raise ProviderError(
                "Multi-view 3D needs at least 2 images (front first, then left, back, right).",
                provider=self.provider_id,
                status_code=400,
            )

        auth = {"Authorization": f"Bearer {key}"}

        def _raise_for_status(resp: httpx.Response, what: str) -> None:
            if resp.status_code == 401:
                raise ProviderError(
                    "Tripo API key is unauthorized (401).",
                    provider=self.provider_id,
                    status_code=401,
                    how_to_fix="Set a valid TRIPO_API_KEY in Settings.",
                )
            if resp.status_code in (402, 403):
                raise ProviderError(
                    f"Tripo refused the {what} request ({resp.status_code}): insufficient credits or not allowed.",
                    provider=self.provider_id,
                    status_code=resp.status_code,
                    how_to_fix="Top up your Tripo credits or switch to Demo Mode.",
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise ProviderError(
                    f"Tripo rate limit or server error ({resp.status_code}).",
                    provider=self.provider_id,
                    status_code=resp.status_code,
                    retryable=True,
                )
            if resp.status_code >= 400:
                raise ProviderError(
                    f"Tripo rejected the {what} request ({resp.status_code}): {resp.text[:240]}",
                    provider=self.provider_id,
                    status_code=resp.status_code,
                )

        async with httpx.AsyncClient(timeout=120.0) as client:
            async def _upload(path: Path) -> dict[str, str]:
                async def _do() -> dict[str, str]:
                    resp = await client.post(
                        f"{TRIPO_BASE_URL}/upload",
                        headers=auth,
                        files={"file": (path.name, path.read_bytes(), f"image/{_image_type(path)}")},
                    )
                    _raise_for_status(resp, "upload")
                    data = resp.json().get("data") or {}
                    token = data.get("image_token") or data.get("file_token")
                    if not token:
                        raise ProviderError("Tripo upload returned no token.", provider=self.provider_id)
                    return {"type": _image_type(path), "file_token": token}

                return await self.with_retries(_do)

            body: dict[str, Any] = {"type": TASK_TYPES[capability], "model_version": model_version}
            if capability == ProviderCapability.TEXT_TO_3D:
                body["prompt"] = prompt[:1024]
                if params.get("negative_prompt"):
                    body["negative_prompt"] = str(params["negative_prompt"])[:255]
            elif capability == ProviderCapability.IMAGE_TO_3D:
                await _notify(5, "Uploading reference image...")
                body["file"] = await _upload(refs[0])
            else:
                await _notify(5, "Uploading reference images...")
                tokens = [await _upload(p) for p in refs[:4]]
                # Slot order is front, left, back, right; empty slots are `{}`.
                body["files"] = tokens + [{}] * (4 - len(tokens))
            for field in ("texture", "pbr", "quad"):
                if params.get(field) is not None:
                    body[field] = bool(params[field])
            if params.get("face_limit"):
                body["face_limit"] = int(params["face_limit"])
            if params.get("texture_quality"):
                body["texture_quality"] = str(params["texture_quality"])

            async def _create_task() -> str:
                resp = await client.post(f"{TRIPO_BASE_URL}/task", headers=auth, json=body)
                _raise_for_status(resp, "task")
                payload = resp.json()
                task_id = (payload.get("data") or {}).get("task_id")
                if payload.get("code", 0) != 0 or not task_id:
                    raise ProviderError(
                        f"Tripo did not create a task: {str(payload)[:240]}",
                        provider=self.provider_id,
                        status_code=502,
                    )
                return task_id

            task_id = await self.with_retries(_create_task)

            poll_interval = float(params.get("poll_interval", 2.5))
            max_polls = int(params.get("max_polls", 240))
            task_data: dict[str, Any] = {}
            for _ in range(max_polls):
                resp = await client.get(f"{TRIPO_BASE_URL}/task/{task_id}", headers=auth)
                if resp.status_code >= 400:
                    await asyncio.sleep(poll_interval)
                    continue
                task_data = resp.json().get("data") or {}
                status = str(task_data.get("status", "queued")).lower()
                prog = int(task_data.get("progress") or 0)
                await _notify(prog, f"Tripo {status} ({prog}%)")
                if status == "success":
                    break
                if status in FAILED_STATUSES:
                    raise ProviderError(
                        f"Tripo 3D generation {status}: {task_data.get('message') or 'no details'}",
                        provider=self.provider_id,
                        status_code=500,
                        retryable=status in ("failed", "unknown"),
                        how_to_fix="Retry, or try a cleaner reference image; 'banned' means content was blocked.",
                    )
                await asyncio.sleep(poll_interval)
            else:
                raise ProviderError(
                    "Tripo task polling timed out.",
                    provider=self.provider_id,
                    status_code=504,
                    retryable=True,
                )

            # Download server-side because Tripo's signed URLs expire.
            output = task_data.get("output") or {}
            glb_url = output.get("pbr_model") or output.get("model") or output.get("base_model")
            if not glb_url:
                raise ProviderError(
                    "Tripo task succeeded but returned no model URL.",
                    provider=self.provider_id,
                    status_code=500,
                )
            glb_resp = await client.get(glb_url)
            if glb_resp.status_code != 200:
                raise ProviderError(
                    f"Tripo model download failed ({glb_resp.status_code}).",
                    provider=self.provider_id,
                    status_code=glb_resp.status_code,
                    retryable=True,
                )
            out_glb.parent.mkdir(parents=True, exist_ok=True)
            out_glb.write_bytes(glb_resp.content)

            extra_files: dict[str, str] = {}
            # Keep the untextured base GLB alongside the PBR one when both are offered.
            if output.get("pbr_model") and output.get("model") and output["model"] != output["pbr_model"]:
                try:
                    b_resp = await client.get(output["model"])
                    if b_resp.status_code == 200:
                        base_path = output_dir / f"{filename_stem}_base.glb"
                        base_path.write_bytes(b_resp.content)
                        extra_files["glb_base"] = f"assets/{base_path.name}"
                except Exception:
                    pass

            thumb_rel = ""
            if output.get("rendered_image"):
                try:
                    t_resp = await client.get(output["rendered_image"])
                    if t_resp.status_code == 200:
                        t_path = output_dir / f"{filename_stem}_thumb.webp"
                        t_path.write_bytes(t_resp.content)
                        thumb_rel = f"assets/{t_path.name}"
                except Exception:
                    pass

        reported = task_data.get("consumed_credit")
        credits = float(reported) if reported is not None else est.credits
        return ProviderResult(
            provider=self.provider_id,
            model=model_version,
            file_path=f"assets/{out_glb.name}",
            thumbnail_path=thumb_rel,
            extra_files=extra_files,
            cost_usd=round(credits * USD_PER_CREDIT, 3),
            cost_estimated=reported is None,
            metadata={"tripo_task_id": task_id, "credits": credits, "capability": capability.value},
        )

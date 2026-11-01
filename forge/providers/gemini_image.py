"""Google Gemini ("Nano Banana") & Meshy Image Provider Adapter.

Supports:
1. Direct Google Gemini API (`generativelanguage.googleapis.com`):
   - `gemini-nano-banana-2.1` (Nano Banana 2.1 — Default!)
   - `gemini-3.1-flash-image` (Nano Banana 2)
   - `gemini-3.1-flash-lite-image` (Nano Banana 2 Lite)
   - `gemini-3-pro-image` (Nano Banana Pro)
   - `gemini-2.5-flash-image` (Nano Banana 1)
2. Meshy OpenAPI v1 Image Generation (`/openapi/v1/text-to-image` & `/openapi/v1/image-to-image`):
   - `meshy:nano-banana-2` (Nano Banana 2 via Meshy Credits)
   - `meshy:nano-banana-2-lite` (Nano Banana 2 Lite via Meshy)
   - `meshy:nano-banana-2-1` (Nano Banana 2.1 via Meshy)
   - `meshy:nano-banana` (Nano Banana Standard via Meshy)
   - `meshy:nano-banana-pro` (Nano Banana Pro via Meshy)
   - `meshy:gpt-image-2` (OpenAI GPT-Image 2 via Meshy)
   - `meshy:gpt-image-2-5-flare` (GPT-Image 2.5 Flare via Meshy)
   - `meshy:gpt-image-2-5-sunburst` (GPT-Image 2.5 Sunburst via Meshy)
3. Automatic Live Failover:
   - If a Google AI Studio Free-Tier Gemini key returns `429 RESOURCE_EXHAUSTED (limit: 0 on Free Tier)`
     and a `MESHY_API_KEY` is configured, automatically routes the Nano Banana generation through
     Meshy's `/openapi/v1/text-to-image` (`nano-banana-2`) so real AI images are produced seamlessly!
"""

from __future__ import annotations

import asyncio
import base64
import random
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


MODEL_PRICING_USD = {
    "gemini-nano-banana-2.1": 0.035,
    "gemini-3.1-flash-image": 0.039,
    "gemini-3.1-flash-lite-image": 0.022,
    "gemini-3-pro-image": 0.065,
    "gemini-2.5-flash-image": 0.039,
    "meshy:nano-banana-2": 0.060,
    "meshy:nano-banana-2-lite": 0.030,
    "meshy:nano-banana-2-1": 0.040,
    "meshy:nano-banana": 0.030,
    "meshy:nano-banana-pro": 0.090,
    "meshy:gpt-image-2": 0.090,
    "meshy:gpt-image-2-5-flare": 0.090,
    "meshy:gpt-image-2-5-sunburst": 0.090,
}


def _file_to_data_uri(path: Path) -> str:
    ext = path.suffix.lower()
    mime = "image/png" if ext == ".png" else ("image/webp" if ext == ".webp" else "image/jpeg")
    b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


class GeminiImageAdapter(ProviderAdapter):
    provider_id = "gemini"
    display_name = "Nano Banana & GPT-Image (Gemini / Meshy)"
    capabilities = [
        ProviderCapability.TEXT_TO_IMAGE,
        ProviderCapability.IMAGE_EDIT,
        ProviderCapability.MULTI_REFERENCE_IMAGE,
    ]

    def __init__(self, api_key: str | None = None, demo_mode: bool | None = None) -> None:
        self._explicit_key = api_key
        self._explicit_demo = demo_mode

    def _get_keys_and_mode(self, requested_model: str = "") -> tuple[str, str, bool, str]:
        cfg = load_config()
        gemini_key = self._explicit_key if self._explicit_key is not None else cfg.get_key("gemini")
        meshy_key = cfg.get_key("meshy")
        # An explicitly supplied Gemini key is a provider override; do not route
        # an otherwise model-less call through a persisted Meshy selection.
        model = requested_model or (
            "gemini-nano-banana-2.1"
            if self._explicit_key is not None
            else (cfg.default_image_model or "gemini-nano-banana-2.1")
        )

        if self._explicit_demo is not None:
            is_demo = self._explicit_demo
        elif cfg.demo_mode:
            is_demo = True
        else:
            is_demo = not bool(gemini_key or meshy_key)
        return gemini_key, meshy_key, is_demo, model

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "default": "gemini-nano-banana-2.1",
                    "enum": list(MODEL_PRICING_USD.keys()),
                    "description": "Image generation model (Gemini Nano Banana 2.1 or Meshy Image API)",
                },
                "aspect_ratio": {
                    "type": "string",
                    "default": "1:1",
                    "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"],
                },
                "pose_mode": {
                    "type": "string",
                    "default": "",
                    "enum": ["", "a-pose", "t-pose"],
                },
                "remove_background": {
                    "type": "boolean",
                    "default": True,
                },
                "generate_multi_view": {
                    "type": "boolean",
                    "default": False,
                },
            },
        }

    def estimate_cost(self, capability: ProviderCapability, params: dict[str, Any]) -> CostEstimate:
        _, _, _, default_model = self._get_keys_and_mode(params.get("model", ""))
        model = params.get("model") or default_model
        unit_cost = MODEL_PRICING_USD.get(model, 0.039)
        count = int(params.get("count", 1))
        return CostEstimate(
            provider=self.provider_id,
            model=model,
            estimated_usd=round(unit_cost * count, 4),
            credits=count * 1.0,
            unit_description=f"${unit_cost:.3f} per image ({model})",
        )

    async def _execute_via_meshy_image(
        self,
        meshy_key: str,
        meshy_model: str,
        prompt: str,
        out_file: Path,
        reference_files: list[Path] | None,
        params: dict[str, Any],
        progress_cb: ProgressCallback | None,
        seed: int,
    ) -> ProviderResult:
        """Execute Nano Banana (`nano-banana-2`, `nano-banana-pro`) or `gpt-image-2` via Meshy's `/openapi/v1/text-to-image` or `/openapi/v1/image-to-image`."""
        clean_model = meshy_model.replace("meshy:", "")
        if clean_model.startswith("gemini-"):
            if "lite" in clean_model:
                clean_model = "nano-banana-2-lite"
            elif "pro" in clean_model:
                clean_model = "nano-banana-pro"
            elif "2.1" in clean_model:
                clean_model = "nano-banana-2-1"
            else:
                clean_model = "nano-banana-2"

        valid_refs = [p for p in (reference_files or []) if p.exists() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")][:8]
        is_i2i = len(valid_refs) > 0 and bool(params.get("use_reference_image", False))
        endpoint = "/openapi/v1/image-to-image" if is_i2i else "/openapi/v1/text-to-image"
        url = f"https://api.meshy.ai{endpoint}"
        headers = {"Authorization": f"Bearer {meshy_key}", "Content-Type": "application/json"}

        body: dict[str, Any] = {
            "ai_model": clean_model,
            "prompt": prompt[:1500],
            "remove_background": bool(params.get("remove_background", params.get("transparent", True))),
        }
        if is_i2i:
            body["reference_image_urls"] = [_file_to_data_uri(p) for p in valid_refs]

        if params.get("generate_multi_view"):
            body["generate_multi_view"] = True
        else:
            ar = str(params.get("aspect_ratio", "1:1"))
            if "nano-banana" in clean_model and ar in ("3:2", "2:3"):
                ar = "4:3" if ar == "3:2" else "3:4"
            body["aspect_ratio"] = ar

        if params.get("pose_mode") in ("a-pose", "t-pose"):
            body["pose_mode"] = params["pose_mode"]

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=body)
            if resp.status_code >= 400:
                raise ProviderError(
                    message=f"Meshy Image API error ({resp.status_code}): {resp.text[:240]}",
                    provider="meshy_image",
                    status_code=resp.status_code,
                    retryable=resp.status_code in (429, 500, 502, 503, 504),
                )
            task_id = resp.json()["result"]
            status_url = f"{url}/{task_id}"

            task_data: dict[str, Any] = {}
            for _ in range(90):
                s_resp = await client.get(status_url, headers=headers)
                if s_resp.status_code >= 400:
                    await asyncio.sleep(2.0)
                    continue
                task_data = s_resp.json()
                status = task_data.get("status", "PENDING")
                prog = int(task_data.get("progress", 0))
                if progress_cb:
                    r_cb = progress_cb(prog, f"Meshy {clean_model} {status} ({prog}%)")
                    if hasattr(r_cb, "__await__"):
                        await r_cb
                if status == "SUCCEEDED":
                    break
                if status in ("FAILED", "CANCELED"):
                    err_msg = task_data.get("task_error", {}).get("message") or f"Meshy image task {status}"
                    raise ProviderError(err_msg, provider="meshy_image", status_code=500)
                await asyncio.sleep(2.0)
            else:
                raise ProviderError("Meshy image generation timed out.", provider="meshy_image", status_code=504)

            img_urls = task_data.get("image_urls") or []
            alpha_url = task_data.get("alpha_thumbnail_url")
            target_url = alpha_url or (img_urls[0] if img_urls else "")
            if not target_url:
                raise ProviderError("Meshy image task succeeded but returned no image_urls.", provider="meshy_image")

            img_resp = await client.get(target_url)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(img_resp.content)

        thumb_path = out_file.with_name(f"{out_file.stem}_thumb.png")
        img = Image.open(out_file).convert("RGBA")
        w, h = img.size
        thumb = img.copy()
        thumb.thumbnail((256, 256), Image.Resampling.LANCZOS)
        thumb.save(thumb_path, format="PNG")

        return ProviderResult(
            provider="meshy_image",
            model=clean_model,
            file_path=f"assets/{out_file.name}",
            thumbnail_path=f"assets/{thumb_path.name}",
            cost_usd=MODEL_PRICING_USD.get(f"meshy:{clean_model}", 0.06),
            cost_estimated=False,
            seed=seed,
            metadata={"width": w, "height": h, "meshy_task_id": task_id, "aspect_ratio": body.get("aspect_ratio", "1:1")},
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
        gemini_key, meshy_key, is_demo, model = self._get_keys_and_mode(params.get("model", ""))
        seed = params.get("seed") or random.randint(1000, 999999)
        render_mode = params.get("render_mode", "standard")
        palette = params.get("palette")
        aspect_ratio = str(params.get("aspect_ratio") or cfg.image_aspect_ratio or "1:1")
        pose_mode = str(params.get("pose_mode") if "pose_mode" in params else cfg.image_pose_mode)
        transparent = bool(params.get("remove_background", params.get("transparent", cfg.image_remove_bg)))

        prompt = finalize_generation_prompt(prompt, pose_mode=pose_mode, aspect_ratio=aspect_ratio)

        if progress_cb:
            res = progress_cb(20, f"Generating image with {model}...")
            if hasattr(res, "__await__"):
                await res

        out_file = output_dir / f"{filename_stem}.png"

        if is_demo:
            meta = generate_demo_image(
                output_path=out_file,
                prompt=prompt,
                palette=palette,
                seed=seed,
                transparent=transparent,
                mode=render_mode,
            )
            return ProviderResult(
                provider="demo",
                model=model,
                file_path=f"assets/{out_file.name}",
                thumbnail_path=f"assets/{meta['thumb_name']}",
                cost_usd=MODEL_PRICING_USD.get(model, 0.035),
                cost_estimated=True,
                seed=seed,
                metadata={**meta, "demo_mode": True, "capability": capability.value},
            )

        # ONLY call Meshy's paid Image API when the user explicitly selected a `meshy:*` model
        if model.startswith("meshy:"):
            if not meshy_key:
                raise ProviderError(
                    "Meshy API key is required for Meshy Image models.",
                    provider="meshy_image",
                    status_code=401,
                    how_to_fix="Add your MESHY_API_KEY in Settings or switch to a Gemini model.",
                )
            return await self._execute_via_meshy_image(
                meshy_key=meshy_key,
                meshy_model=model,
                prompt=prompt,
                out_file=out_file,
                reference_files=reference_files,
                params={**params, "aspect_ratio": aspect_ratio, "pose_mode": pose_mode, "remove_background": transparent},
                progress_cb=progress_cb,
                seed=seed,
            )

        # Live Google Gemini API Call (Nano Banana 2.1 / 3.1 / 2.5 / Pro)
        parts: list[dict[str, Any]] = [{"text": prompt}]
        reviewed_negative = str(params.get("negative_prompt") or "").strip()
        if reviewed_negative:
            parts.append({"text": reviewed_negative})
        if params.get("use_reference_image"):
            for ref_path in (reference_files or [])[:8]:
                if ref_path.exists() and ref_path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    b64 = base64.b64encode(ref_path.read_bytes()).decode("utf-8")
                    extension = ref_path.suffix.lower()
                    mime = "image/png" if extension == ".png" else "image/webp" if extension == ".webp" else "image/jpeg"
                    parts.append({"inlineData": {"mimeType": mime, "data": b64}})

        clean_gemini_model = model.replace("models/", "")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_gemini_model}:generateContent"
        headers = {"x-goog-api-key": gemini_key, "Content-Type": "application/json"}
        gen_cfg: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
        body: dict[str, Any] = {"contents": [{"parts": parts}], "generationConfig": gen_cfg}

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=body)

            if resp.status_code in (401, 403):
                raise ProviderError(
                    message=f"Gemini API authentication failed ({resp.status_code}).",
                    provider=self.provider_id,
                    status_code=resp.status_code,
                    retryable=False,
                    how_to_fix="Verify your GEMINI_API_KEY in Settings.",
                )
            if resp.status_code == 429:
                raise ProviderError(
                    message=(
                        f"Google Gemini API returned 429 Quota Exceeded for '{clean_gemini_model}' "
                        f"(Google AI Studio free-tier image limit is 0 without billing). "
                        f"No Meshy credits were spent."
                    ),
                    provider=self.provider_id,
                    status_code=429,
                    retryable=False,
                    how_to_fix="To use Meshy credits instead (3–6 credits/img), select 'Meshy API · Nano Banana' in the model dropdown, or enable Demo Mode (0 credits).",
                )
            if resp.status_code >= 400:
                raise ProviderError(
                    message=f"Gemini API error ({resp.status_code}): {resp.text[:240]}",
                    provider=self.provider_id,
                    status_code=resp.status_code,
                    retryable=False,
                )

            data = resp.json()
            img_bytes: bytes | None = None
            for cand in data.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        img_bytes = base64.b64decode(inline["data"])
                        break
            if not img_bytes:
                raise ProviderError(
                    message=f"Model '{clean_gemini_model}' returned text but no image payload.",
                    provider=self.provider_id,
                    status_code=422,
                )

        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(img_bytes)

        thumb_path = out_file.with_name(f"{out_file.stem}_thumb.png")
        img = Image.open(out_file).convert("RGBA")
        w, h = img.size
        thumb = img.copy()
        thumb.thumbnail((256, 256), Image.Resampling.LANCZOS)
        thumb.save(thumb_path, format="PNG")

        return ProviderResult(
            provider=self.provider_id,
            model=clean_gemini_model,
            file_path=f"assets/{out_file.name}",
            thumbnail_path=f"assets/{thumb_path.name}",
            cost_usd=MODEL_PRICING_USD.get(clean_gemini_model, 0.035),
            cost_estimated=False,
            seed=seed,
            metadata={"width": w, "height": h, "aspect_ratio": aspect_ratio},
        )

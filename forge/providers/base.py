"""Base Provider Adapter interface, typed errors, retry with backoff, and cost estimates."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Awaitable
from pydantic import BaseModel, Field


class ProviderCapability(str, Enum):
    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_EDIT = "image_edit"
    MULTI_REFERENCE_IMAGE = "multi_reference_image"
    IMAGE_TO_3D = "image_to_3d"
    MULTI_IMAGE_TO_3D = "multi_image_to_3d"
    TEXT_TO_3D = "text_to_3d"
    RETEXTURE_3D = "retexture_3d"
    REMESH_3D = "remesh_3d"
    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    VISION_CRITIC = "vision_critic"
    SOUND_EFFECT = "sound_effect"
    TEXT_TO_SPEECH = "text_to_speech"
    TEXT_TO_MUSIC = "text_to_music"


class ProviderError(Exception):
    """Typed provider error with user-friendly explanation and fix suggestion."""

    def __init__(
        self,
        message: str,
        provider: str,
        status_code: int = 500,
        retryable: bool = False,
        how_to_fix: str = "Check your API key in Settings or enable Demo Mode.",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.how_to_fix = how_to_fix

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.message,
            "provider": self.provider,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "how_to_fix": self.how_to_fix,
        }


class CostEstimate(BaseModel):
    provider: str
    model: str
    estimated_usd: float
    credits: float = 0.0
    unit_description: str = "per generation"


class ProviderResult(BaseModel):
    provider: str
    model: str
    file_path: str
    thumbnail_path: str = ""
    extra_files: dict[str, str] = Field(default_factory=dict)
    cost_usd: float = 0.0
    cost_estimated: bool = True
    seed: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


ProgressCallback = Callable[[int, str], Awaitable[None] | None]


class ProviderAdapter(ABC):
    provider_id: str = "base"
    display_name: str = "Base Provider"
    capabilities: list[ProviderCapability] = []

    @abstractmethod
    def parameter_schema(self) -> dict[str, Any]:
        """Return JSON schema of configurable parameters for this provider."""

    @abstractmethod
    def estimate_cost(self, capability: ProviderCapability, params: dict[str, Any]) -> CostEstimate:
        """Estimate USD cost before running a job."""

    @abstractmethod
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
        """Execute the provider request with async polling and save outputs to output_dir."""

    async def with_retries(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> Any:
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return await coro_factory()
            except ProviderError as err:
                last_err = err
                if not err.retryable or attempt >= max_retries:
                    raise
                await asyncio.sleep(base_delay * (2**attempt))
            except Exception as err:
                last_err = err
                if attempt >= max_retries:
                    raise ProviderError(
                        message=f"Unexpected error from {self.display_name}: {err}",
                        provider=self.provider_id,
                        retryable=False,
                    ) from err
                await asyncio.sleep(base_delay * (2**attempt))
        raise last_err or RuntimeError("Retry loop exhausted")

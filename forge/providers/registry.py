"""Provider Registry for Forge."""

from __future__ import annotations

from typing import Any
from forge.providers.base import ProviderAdapter, ProviderCapability
from forge.providers.elevenlabs_image import ElevenLabsImageAdapter
from forge.providers.gemini_image import GeminiImageAdapter
from forge.providers.meshy_3d import Meshy3DAdapter
from forge.providers.llm_critic import VisionCriticAdapter


class ProviderRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        self.register(GeminiImageAdapter())
        self.register(ElevenLabsImageAdapter())
        self.register(Meshy3DAdapter())
        self.critic = VisionCriticAdapter()

    def register(self, adapter: ProviderAdapter) -> None:
        self._adapters[adapter.provider_id] = adapter

    def get(self, provider_id: str) -> ProviderAdapter:
        if provider_id not in self._adapters:
            raise KeyError(f"Unknown provider '{provider_id}'. Available: {list(self._adapters.keys())}")
        return self._adapters[provider_id]

    def find_for_capability(self, capability: ProviderCapability) -> ProviderAdapter:
        for adapter in self._adapters.values():
            if capability in adapter.capabilities:
                return adapter
        raise LookupError(f"No provider registered for capability {capability}")

    def list_providers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": a.provider_id,
                "name": a.display_name,
                "capabilities": [c.value for c in a.capabilities],
                "parameter_schema": a.parameter_schema(),
            }
            for a in self._adapters.values()
        ]


REGISTRY = ProviderRegistry()

"""EngineAdapter abstract base class for Game Scan & Write-back (Section 14.7)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from forge.models import Asset, CatalogEntry, GameProfile, WishlistItem


class EngineAdapter(ABC):
    engine_id: str = "generic"
    display_name: str = "Generic Folder"

    @abstractmethod
    def detect(self, game_dir: Path) -> bool:
        """Return True if game_dir matches this game engine."""

    @abstractmethod
    def parse_scenes(self, game_dir: Path, safe_files: list[Path]) -> dict[str, Any]:
        """Extract camera perspective, node types, collision shapes, and referenced res:// paths."""

    @abstractmethod
    def extract_data_catalogs(self, game_dir: Path, safe_files: list[Path]) -> list[CatalogEntry]:
        """Extract items, enemies, NPCs, and lore from JSON/CSV/.tres/script databases."""

    @abstractmethod
    def infer_scale(self, scene_data: dict[str, Any]) -> dict[str, Any]:
        """Infer world unit scale and character height from collision shapes and camera setup."""

    @abstractmethod
    def find_missing_refs(
        self, game_dir: Path, safe_files: list[Path], scene_data: dict[str, Any], catalog: list[CatalogEntry]
    ) -> tuple[list[WishlistItem], list[WishlistItem]]:
        """Find missing res:// references, placeholder assets, and TODO/FIXME art comments."""

    @abstractmethod
    def scan(self, game_dir: Path, previous_profile: GameProfile | None = None) -> GameProfile:
        """Run full read-only scan and return a structured GameProfile with evidence & confidence."""

    @abstractmethod
    def export_layout(self, asset: Asset, profile: GameProfile) -> dict[str, str]:
        """Compute target relative paths and engine glue files for an asset."""

    @abstractmethod
    def writeback_preview(
        self, game_dir: Path, assets: list[Asset], profile: GameProfile
    ) -> dict[str, Any]:
        """Stage files and return a non-destructive diff preview before writing to the game folder."""

    @abstractmethod
    def writeback_apply(
        self, game_dir: Path, assets: list[Asset], profile: GameProfile
    ) -> dict[str, Any]:
        """Write staged assets + engine glue into allowed asset directories without overwriting existing files."""

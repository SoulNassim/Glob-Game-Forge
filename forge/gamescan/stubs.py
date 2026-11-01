"""Documented EngineAdapter stubs for Unity, Unreal, Love2D, Phaser, Bevy, and Generic folders (Section 14.7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from forge.gamescan.base import EngineAdapter
from forge.gamescan.godot_adapter import Godot4Adapter
from forge.models import Asset, CatalogEntry, GameProfile, WishlistItem


class _BaseStubAdapter(Godot4Adapter):
    """Inherits generic JSON/CSV/image scanning from Godot4Adapter with engine-specific detection and folder conventions."""

    def scan(self, game_dir: Path, previous_profile: GameProfile | None = None) -> GameProfile:
        profile = super().scan(game_dir, previous_profile)
        profile.engine = self.engine_id
        return profile


class UnityAdapter(_BaseStubAdapter):
    engine_id = "unity"
    display_name = "Unity (ProjectSettings / Assets)"

    def detect(self, game_dir: Path) -> bool:
        return (game_dir / "ProjectSettings" / "ProjectVersion.txt").exists() or (game_dir / "Assets").is_dir()


class UnrealAdapter(_BaseStubAdapter):
    engine_id = "unreal"
    display_name = "Unreal Engine 5 (.uproject)"

    def detect(self, game_dir: Path) -> bool:
        return len(list(game_dir.glob("*.uproject"))) > 0


class Love2DAdapter(_BaseStubAdapter):
    engine_id = "love2d"
    display_name = "LÖVE 2D (conf.lua / main.lua)"

    def detect(self, game_dir: Path) -> bool:
        return (game_dir / "conf.lua").exists() or (game_dir / "main.lua").exists()


class PhaserAdapter(_BaseStubAdapter):
    engine_id = "phaser"
    display_name = "Phaser HTML5 Game"

    def detect(self, game_dir: Path) -> bool:
        pkg = game_dir / "package.json"
        return pkg.exists() and "phaser" in pkg.read_text(encoding="utf-8", errors="ignore").lower()


class BevyAdapter(_BaseStubAdapter):
    engine_id = "bevy"
    display_name = "Bevy Rust Engine (Cargo.toml)"

    def detect(self, game_dir: Path) -> bool:
        cargo = game_dir / "Cargo.toml"
        return cargo.exists() and "bevy" in cargo.read_text(encoding="utf-8", errors="ignore").lower()


def detect_engine_adapter(game_dir: Path) -> EngineAdapter:
    adapters: list[EngineAdapter] = [
        Godot4Adapter(),
        UnityAdapter(),
        UnrealAdapter(),
        Love2DAdapter(),
        PhaserAdapter(),
        BevyAdapter(),
    ]
    for adapter in adapters:
        if adapter.detect(game_dir):
            return adapter
    return Godot4Adapter()

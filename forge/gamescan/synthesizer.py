"""Context-aware Prompt Synthesizer from GameProfile + CatalogEntry (Section 14.3).

Builds prompts and recipe plans from the scanned GameProfile + specific catalog rows
(e.g., `iron_sword` in `items.json` -> yields inventory icon, 3D prop, and rarity variants
in the game's exact style, palette, scale, and naming convention).
"""

from __future__ import annotations

from typing import Any
from forge.models import CatalogEntry, GameProfile, ProjectBible


def apply_game_profile_to_bible(profile: GameProfile, bible: ProjectBible) -> ProjectBible:
    """Lock an approved GameProfile into the ProjectBible."""
    bible.art_style = str(profile.art_style.value)
    bible.genre = str(profile.genre.value)
    bible.tone = str(profile.tone.value)
    if isinstance(profile.setting_and_lore.value, list):
        bible.lore_keywords = [str(x) for x in profile.setting_and_lore.value]
    bible.camera_perspective = str(profile.camera_and_perspective.value)
    if isinstance(profile.palette.value, list) and profile.palette.value:
        bible.palette = [str(c) for c in profile.palette.value]
    if isinstance(profile.scale_and_units.value, dict):
        bible.unit_scale_meters = float(profile.scale_and_units.value.get("unit_meters", 1.0))
        bible.character_height_units = float(profile.scale_and_units.value.get("char_height", 1.8))
    if isinstance(profile.budgets.value, dict):
        bible.polycount_budget = int(profile.budgets.value.get("max_tris", 10000))
        bible.texture_resolution = str(profile.budgets.value.get("texture_size", "2k"))
    if profile.engine in ("godot", "unity", "unreal", "generic"):
        bible.target_engine = profile.engine  # type: ignore[assignment]
    if profile.auto_style_pack and profile.auto_style_pack.id not in bible.default_style_packs:
        bible.default_style_packs.insert(0, profile.auto_style_pack.id)
    return bible


def propose_assets_for_catalog_item(
    item_id: str,
    profile: GameProfile,
) -> dict[str, Any]:
    """Synthesize context-aware prompts for an item/enemy ID from the GameProfile catalog."""
    entry: CatalogEntry | None = None
    for c in profile.catalog:
        if c.id.lower() == item_id.lower() or c.name.lower() == item_id.lower():
            entry = c
            break

    if not entry:
        entry = CatalogEntry(
            id=item_id.lower().replace(" ", "_"),
            name=item_id.replace("_", " ").title(),
            category="item",
            description=f"Game asset for {item_id}",
        )

    style = profile.art_style.value
    cam = profile.camera_and_perspective.value
    scale_dict = profile.scale_and_units.value if isinstance(profile.scale_and_units.value, dict) else {}
    budgets_dict = profile.budgets.value if isinstance(profile.budgets.value, dict) else {}
    stats_str = ", ".join(f"{k}: {v}" for k, v in list(entry.stats.items())[:5])

    base_context = (
        f"Game: '{profile.game_title.value}' ({profile.genre.value}, {profile.tone.value}). "
        f"Subject: '{entry.name}' ({entry.category}) — {entry.description}. "
        f"{f'Stats/Attributes: {stats_str}. ' if stats_str else ''}"
        f"Style: {style}. Camera: {cam}."
    )

    low_confidence_fields = []
    for fname in ("art_style", "camera_and_perspective", "scale_and_units", "palette"):
        field_obj = getattr(profile, fname, None)
        if field_obj and field_obj.confidence < 0.65:
            low_confidence_fields.append({"field": fname, "confidence": field_obj.confidence, "value": field_obj.value})

    proposals = [
        {
            "role": "inventory_icon",
            "verb": "icon_maker",
            "suggested_name": f"icon_{entry.id}",
            "target_res_path": f"res://assets/icons/{entry.id}.png",
            "prompt": f"{base_context} Clean 1:1 RPG inventory icon, bold silhouette readable at 64x64px, transparent background.",
        },
        {
            "role": "3d_prop_or_character",
            "verb": "make_3d",
            "suggested_name": f"mesh_{entry.id}",
            "target_res_path": entry.existing_asset_path or f"res://assets/models/{entry.id}.glb",
            "prompt": f"{base_context} 3D game asset ready for {profile.engine.title()}, target polycount {budgets_dict.get('max_tris', 10000)} tris, unit scale {scale_dict.get('unit_meters', 1.0)}m.",
            "params": {
                "target_polycount": int(budgets_dict.get("max_tris", 10000)),
                "should_texture": True,
            },
        },
        {
            "role": "rarity_variants",
            "verb": "variant",
            "suggested_name": f"{entry.id}_variants",
            "prompt": f"{base_context} 4 escalating rarity tier variants (Common, Rare, Epic, Legendary) with locked silhouette.",
            "params": {"chips": ["Rarity tiers"], "count": 4},
        },
    ]

    return {
        "item_id": entry.id,
        "entry": entry.model_dump(),
        "low_confidence_warnings": low_confidence_fields,
        "needs_human_confirmation": len(low_confidence_fields) > 0,
        "proposals": proposals,
    }

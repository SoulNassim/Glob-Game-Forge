"""Built-in Style Packs and Variant Chip prompt-injection presets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from forge.config import get_config_dir
from forge.models import ProjectBible, StylePack


BUILTIN_STYLE_PACKS: dict[str, StylePack] = {
    "hand_painted": StylePack(
        id="hand_painted",
        name="Hand-Painted Fantasy",
        description="Warm painterly brushstrokes, sculpted bevels, readable silhouettes, soft rim light.",
        prompt_prefix="Stylized hand-painted game asset, visible gouache brushwork, chunky readable forms,",
        prompt_suffix="warm rim lighting, ambient occlusion in crevices, clean studio silhouette",
        negative_prompt="photorealistic noise, harsh high-frequency grain, blurry edges, watermark, text",
        provider_settings={"model_type": "standard", "ai_model": "meshy-7.1"},
        constraints={"max_tris": 15000, "texture_resolution": "2k"},
        palette_hex=["#2B2D42", "#06D6A0", "#FFD166", "#EF233C"],
    ),
    "low_poly_iso": StylePack(
        id="low_poly_iso",
        name="Low-Poly Isometric",
        description="Crisp faceted geometry, clean gradients, diorama-ready 3/4 isometric proportions.",
        prompt_prefix="Low-poly isometric game prop/character, crisp faceted geometric planes, tactile toy-like proportions,",
        prompt_suffix="soft gradient vertex shading, diorama clarity, isolated single subject",
        negative_prompt="micro-detail noise, messy wires, realistic pores, complex background",
        provider_settings={"model_type": "smart-topology", "ai_model": "meshy-t2.5", "target_polycount": 4500},
        constraints={"max_tris": 5000, "texture_resolution": "2k"},
        palette_hex=["#118AB2", "#06D6A0", "#FFD166", "#F77F00"],
    ),
    "ps1_retro": StylePack(
        id="ps1_retro",
        name="PS1 32-Bit Retro",
        description="Low-poly affine texture feel, crunchy dithered color palette, nostalgic 1998 aesthetic.",
        prompt_prefix="1998 PS1 32-bit era 3D game asset, ultra-low polygon silhouette, crunchy 256-color dithered texture,",
        prompt_suffix="vertex-lit retro shading, sharp low-res pixel charm",
        negative_prompt="smooth modern subsurface scattering, 8k raytracing, depth of field blur",
        provider_settings={"model_type": "smart-topology", "ai_model": "meshy-t2.5", "target_polycount": 900},
        constraints={"max_tris": 1200, "texture_resolution": "2k"},
        palette_hex=["#3A0CA3", "#4CC9F0", "#F72585", "#FFBE0B"],
    ),
    "pixel_art": StylePack(
        id="pixel_art",
        name="Crisp 16-Bit Pixel Art",
        description="Hand-placed pixel clusters, dark selective outlines, limited palette ramp.",
        prompt_prefix="Crisp 16-bit pixel art sprite, deliberate pixel cluster shading, selective dark outline,",
        prompt_suffix="clean pixel grid alignment, vibrant arcade color ramps, no anti-aliasing blur",
        negative_prompt="mixels, blurry bilinear scaling, gradients, 3d render, jpeg artifacts",
        provider_settings={"transparent": True},
        constraints={"grid_size": "64x64"},
        palette_hex=["#1A1C2C", "#5D275D", "#B13E53", "#EF7D57", "#FFCD75", "#38B764"],
    ),
    "cel_shaded": StylePack(
        id="cel_shaded",
        name="Anime Cel-Shaded",
        description="Bold ink outlines, two-tone cel shadows, expressive graphic highlights.",
        prompt_prefix="Cel-shaded anime game style, bold variable-width ink outlines, crisp two-tone shadow steps,",
        prompt_suffix="graphic specular highlights, vibrant saturated colors",
        negative_prompt="soft muddy airbrush shading, photorealism, desaturated grey",
        provider_settings={"model_type": "standard", "ai_model": "meshy-7.1"},
        constraints={"max_tris": 18000},
        palette_hex=["#FF0055", "#00F5D4", "#FEE440", "#1E1E2E"],
    ),
    "clean_icon": StylePack(
        id="clean_icon",
        name="Clean UI Inventory Icon",
        description="High-contrast centered item icon, bold outer contour, readable at 64px.",
        prompt_prefix="Game UI inventory icon, centered at slight 3/4 tilt, bold outer contour, oversized readable details,",
        prompt_suffix="glossy rim highlight, clean transparent background, readable at 64x64 pixels",
        negative_prompt="tiny unreadable filigree, cropped edges, cluttered background, text labels",
        provider_settings={"transparent": True},
        constraints={"aspect_ratio": "1:1"},
        palette_hex=["#FFD166", "#06D6A0", "#118AB2", "#EF233C"],
    ),
}


VARIANT_CHIP_PRESETS: dict[str, dict[str, Any]] = {
    "Colors": {
        "label": "Colors",
        "icon": "🎨",
        "base_instruction": "Shift primary and secondary color harmonies while preserving material read:",
        "tiers": [
            "Emerald & Sun-Gold harmonic palette with teal shadows",
            "Crimson Ruby & Obsidian Iron palette with warm amber rim",
            "Glacial Sapphire & Silver Mithril palette with cyan highlights",
            "Amethyst Void & Astral Bronze palette with magenta accents",
        ],
    },
    "Outfit": {
        "label": "Outfit",
        "icon": "👘",
        "base_instruction": "Redesign attire/casing trim while keeping core silhouette and proportions locked:",
        "tiers": [
            "Scout Ranger leather harness, hooded mantle, and utility pouches",
            "Royal Vanguard gilded plate pauldrons, silk tabard, and filigreed greaves",
            "Arcane Scholar runic robes, astrolabe sash, and enchanted collar",
            "Shadowalker reinforced night-weave cloak, asymmetrical shoulder guard",
        ],
    },
    "Material": {
        "label": "Material",
        "icon": "🪨",
        "base_instruction": "Transform primary surface construction material and specular response:",
        "tiers": [
            "Hammered Damascus steel with oiled leather and brass rivets",
            "Carved ancient elder-wood inlaid with glowing jade veins",
            "Translucent obsidian crystal glass with molten gold core",
            "Weathered patinated bronze and sun-bleached bone",
        ],
    },
    "Era": {
        "label": "Era",
        "icon": "⏳",
        "base_instruction": "Adapt design motifs across historical/speculative eras while keeping the subject recognizable:",
        "tiers": [
            "Ancient Mythic Bronze Age — hammered spirals and megalithic runes",
            "High Chivalric Gothic — fluted crests and cathedral arches",
            "Steampunk Clockwork — brass gears, pressure gauges, and copper coils",
            "Solarpunk Neo-Arcane — sleek ceramic plates and hard-light conduits",
        ],
    },
    "Mood": {
        "label": "Mood",
        "icon": "🌙",
        "base_instruction": "Shift emotional atmosphere, lighting temperature, and shape language:",
        "tiers": [
            "Heroic & Noble — uplifting warm golden hour bounce light",
            "Cursed & Foreboding — eerie eldritch green under-glow and jagged trim",
            "Whimsical & Cozy — soft pastel highlights and rounded charming proportions",
            "Tempestuous & Fierce — crackling storm-blue electric arcs",
        ],
    },
    "Species or creature type": {
        "label": "Species or creature type",
        "icon": "🐉",
        "base_instruction": "Adapt anatomy or motif lineage to a distinct fantasy species:",
        "tiers": [
            "Draconic kin — scaled plating, swept horns, and ember crest",
            "Sylvan Fae — leaf-forged contours, antler motifs, and bioluminescent moss",
            "Clockwork Automaton — articulated ball-joints and porcelain faceplate",
            "Abyssal Leviathan — iridescent fin crests and coral-encrusted trim",
        ],
    },
    "Rarity tiers": {
        "label": "Rarity tiers",
        "icon": "💎",
        "base_instruction": "Escalate RPG loot rarity tier with progressive ornament, material quality, and magical glow:",
        "tiers": [
            "Common Tier — simple sturdy iron and rough-hewn timber, matte finish, zero glow",
            "Rare Tier — polished steel with sapphire trim and subtle frost-blue rune shimmer",
            "Epic Tier — ornate masterwork mithril with amethyst core and pulsing arcane aura",
            "Legendary Tier — exalted celestial gold and crimson phoenix crest with radiant halo particles",
        ],
    },
    "Seasons": {
        "label": "Seasons",
        "icon": "🍂",
        "base_instruction": "Infuse seasonal foliage, weather motifs, and seasonal color palette:",
        "tiers": [
            "Spring Bloom — fresh moss, cherry blossom petals, and morning dew",
            "High Summer — sun-baked gold, vibrant emerald, and solar flare accents",
            "Autumn Harvest — russet maple leaves, amber bronze, and twilight mist",
            "Deep Winter — hoarfrost rime, icicle fringes, and aurora borealis tint",
        ],
    },
    "Damage states": {
        "label": "Damage states",
        "icon": "⚔️",
        "base_instruction": "Apply progressive wear, battle damage, and structural weathering:",
        "tiers": [
            "Factory Pristine — freshly forged, mirror-clean bevels and immaculate paint",
            "Field Tested — light sword nicks, scuffed edges, and trail dust",
            "Battle Scarred — deep cleaves, cracked gemstone, scorched soot marks, field-repaired straps",
            "Ancient Ruined Relic — heavily fractured, overgrown with lichen, held together by arcane energy",
        ],
    },
    "Surprise me": {
        "label": "Surprise me",
        "icon": "🎲",
        "base_instruction": "Invent bold, playful creative twists while keeping the core silhouette unmistakable:",
        "tiers": [
            "Alchemical Candy-Glass — translucent gummy-potion aesthetic with sparkling sugar crystals",
            "Starlight Constellation — deep cosmos indigo body mapped with glowing golden star-charts",
            "Volcanic Magma-Forged — basalt crust split by veins of incandescent lava",
            "Origami Papercraft — folded parchment planes with gold-leaf painted edges",
        ],
    },
}


IDENTITY_LOCK_INSTRUCTION = (
    "[IDENTITY LOCK: Treat the attached primary source image as the sole subject reference. "
    "Recreate the exact same character, preserving its identity, silhouette, pose, face or helmet, "
    "armor, weapons, pedestal, proportions, camera angle, perspective, framing, and background. "
    "Edit only the requested variation; keep every unmentioned feature unchanged. Do not replace, "
    "redesign, or invent a different character.]"
)


def list_all_style_packs() -> list[StylePack]:
    packs = dict(BUILTIN_STYLE_PACKS)
    custom_file = get_config_dir() / "custom_style_packs.json"
    if custom_file.exists():
        try:
            raw = json.loads(custom_file.read_text(encoding="utf-8"))
            for item in raw:
                sp = StylePack(**item)
                packs[sp.id] = sp
        except Exception:
            pass
    return list(packs.values())


def save_custom_style_pack(pack: StylePack) -> StylePack:
    custom_file = get_config_dir() / "custom_style_packs.json"
    existing: list[dict[str, Any]] = []
    if custom_file.exists():
        try:
            existing = json.loads(custom_file.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    existing = [p for p in existing if p.get("id") != pack.id]
    existing.append(pack.model_dump())
    custom_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return pack


def build_variant_prompts(
    source_name: str,
    chips: list[str],
    user_prompt: str = "",
    count: int = 4,
    bible: ProjectBible | None = None,
    style_pack_ids: list[str] | None = None,
    custom_chip_overrides: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Expand selected Variant chips + user prompt + Project Bible + Style Packs into N full prompts."""
    bible = bible or ProjectBible()
    all_packs = {p.id: p for p in list_all_style_packs()}
    active_pack_ids = style_pack_ids if style_pack_ids is not None else bible.default_style_packs
    if bible.locked_style_packs:
        active_pack_ids = bible.default_style_packs

    pack_prefixes = [all_packs[pid].prompt_prefix for pid in active_pack_ids if pid in all_packs and all_packs[pid].prompt_prefix]
    pack_suffixes = [all_packs[pid].prompt_suffix for pid in active_pack_ids if pid in all_packs and all_packs[pid].prompt_suffix]
    pack_negatives = [all_packs[pid].negative_prompt for pid in active_pack_ids if pid in all_packs and all_packs[pid].negative_prompt]

    bg_clause = (
        "isolated single subject on clean transparent background"
        if bible.background_rule == "transparent"
        else f"background rule: {bible.background_rule}"
    )

    results: list[dict[str, Any]] = []
    selected_chips = chips or ["Rarity tiers"]

    for i in range(count):
        chip_fragments: list[str] = []
        tier_labels: list[str] = []
        for chip_name in selected_chips:
            preset = VARIANT_CHIP_PRESETS.get(chip_name)
            if not preset:
                chip_fragments.append(f"Variation focus: {chip_name} (variant #{i + 1})")
                tier_labels.append(f"{chip_name} #{i + 1}")
                continue
            tiers = (
                custom_chip_overrides.get(chip_name)
                if custom_chip_overrides and chip_name in custom_chip_overrides
                else preset["tiers"]
            )
            tier_text = tiers[i % len(tiers)]
            chip_fragments.append(f"{preset['base_instruction']} {tier_text}.")
            short_lbl = tier_text.split("—")[0].strip() if "—" in tier_text else f"{chip_name} #{i + 1}"
            tier_labels.append(short_lbl)

        parts = [
            IDENTITY_LOCK_INSTRUCTION,
            f"Subject based on '{source_name}'.",
            *pack_prefixes,
            f"Project Bible style: {bible.art_style} ({bible.camera_perspective}).",
            *chip_fragments,
        ]
        if user_prompt.strip():
            parts.append(
                "Apply the user's direction as the exact requested edit; it takes precedence over any conflicting variation preset."
            )
            parts.append(f"User direction: {user_prompt.strip()}.")
        parts.extend(pack_suffixes)
        parts.append(bg_clause + ".")

        full_prompt = " ".join(p.strip() for p in parts if p.strip())
        variant_title = f"{source_name} • {' + '.join(tier_labels)}"
        results.append(
            {
                "index": i,
                "title": variant_title,
                "tier_label": " + ".join(tier_labels),
                "prompt_full": full_prompt,
                "negative_prompt": ", ".join(pack_negatives),
                "chips": selected_chips,
                "style_packs": active_pack_ids,
            }
        )
    return results

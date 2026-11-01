"""Procedural 2D game asset renderer using Pillow for Demo Mode & starter projects.

Generates crisp, high-craft RGBA PNGs (transparent background by default) for:
- Characters, Potions, Swords, Shields, Chests, Crystals, Extracted Helmets
- Variant tiers (Common, Rare, Epic, Legendary, Seasons, Damage states, Palette shifts)
- Turnaround 3-view sheets (Front, Side, Back)
- Sprite sheets (4-frame walk/idle animation strip)
- Seamless tileable textures
- Clean game icons with stylized borders
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter


def _hex_rgba(hex_str: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = hex_str.lstrip("#")
    if len(h) != 6:
        return (16, 185, 129, alpha)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def _pick_colors(prompt: str, palette: list[str] | None = None, seed: int = 42) -> list[tuple[int, int, int, int]]:
    h = int(hashlib.sha256(f"{prompt}:{seed}".encode()).hexdigest()[:8], 16)
    p_low = prompt.lower()
    if "legendary" in p_low or "gold" in p_low or "solar" in p_low:
        return [_hex_rgba("#FFD166"), _hex_rgba("#F77F00"), _hex_rgba("#EF233C"), _hex_rgba("#FFF3B0")]
    if "epic" in p_low or "arcane" in p_low or "void" in p_low or "purple" in p_low:
        return [_hex_rgba("#9B5DE5"), _hex_rgba("#F15BB5"), _hex_rgba("#00BBF9"), _hex_rgba("#E0AAFF")]
    if "rare" in p_low or "frost" in p_low or "ice" in p_low or "winter" in p_low:
        return [_hex_rgba("#00BBF9"), _hex_rgba("#00F5D4"), _hex_rgba("#4361EE"), _hex_rgba("#CAF0F8")]
    if "fire" in p_low or "crimson" in p_low or "ruby" in p_low or "damage" in p_low:
        return [_hex_rgba("#EF233C"), _hex_rgba("#FF9F1C"), _hex_rgba("#D90429"), _hex_rgba("#FFBF69")]
    if "emerald" in p_low or "forest" in p_low or "poison" in p_low or "spring" in p_low:
        return [_hex_rgba("#06D6A0"), _hex_rgba("#FFD166"), _hex_rgba("#118AB2"), _hex_rgba("#B5E48C")]

    base = palette or ["#06D6A0", "#FFD166", "#EF233C", "#118AB2", "#9B5DE5", "#F77F00"]
    rotated = [base[(i + h) % len(base)] for i in range(len(base))]
    while len(rotated) < 4:
        rotated.append("#FFD166")
    return [_hex_rgba(c) for c in rotated[:4]]


def _draw_subject(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    scale: float,
    prompt: str,
    colors: list[tuple[int, int, int, int]],
    view: str = "front",
    frame_idx: int = 0,
) -> None:
    p = prompt.lower()
    c_main, c_sec, c_acc, c_glow = colors[0], colors[1], colors[2], colors[3]
    outline = (22, 24, 34, 255)
    bob = int(math.sin(frame_idx * math.pi / 2) * 6 * scale)

    # Subtle magical aura if legendary/epic/glow
    if any(k in p for k in ("legendary", "epic", "glow", "arcane", "mana", "rare")):
        r_aura = int(125 * scale)
        draw.ellipse(
            [cx - r_aura, cy - r_aura + bob, cx + r_aura, cy + r_aura + bob],
            fill=(c_glow[0], c_glow[1], c_glow[2], 42),
            outline=(c_glow[0], c_glow[1], c_glow[2], 110),
            width=max(1, int(2 * scale)),
        )

    if any(k in p for k in ("helmet", "helm", "crown", "hat", "mask")) or ("extract" in p and "helm" in p):
        # Isolated Helmet Part
        w, h = int(95 * scale), int(105 * scale)
        # Plume
        draw.polygon(
            [
                (cx, cy - h - int(35 * scale) + bob),
                (cx - int(35 * scale), cy - int(55 * scale) + bob),
                (cx + int(40 * scale), cy - int(45 * scale) + bob),
            ],
            fill=c_acc,
            outline=outline,
        )
        # Dome
        draw.pieslice(
            [cx - w, cy - h + bob, cx + w, cy + int(60 * scale) + bob],
            start=180,
            end=360,
            fill=c_main,
            outline=outline,
            width=max(2, int(4 * scale)),
        )
        # Visor & Cheek guards
        draw.rectangle(
            [cx - w, cy - int(25 * scale) + bob, cx + w, cy + int(5 * scale) + bob],
            fill=c_sec,
            outline=outline,
            width=max(2, int(3 * scale)),
        )
        if view != "back":
            # Glowing visor slit
            slit_x = cx + (int(20 * scale) if view == "side" else 0)
            draw.rectangle(
                [slit_x - int(48 * scale), cy - int(16 * scale) + bob, slit_x + int(48 * scale), cy - int(4 * scale) + bob],
                fill=(20, 240, 255, 255),
            )
        # Cheek plates
        draw.polygon(
            [
                (cx - w, cy + int(5 * scale) + bob),
                (cx - int(55 * scale), cy + int(75 * scale) + bob),
                (cx - int(25 * scale), cy + int(70 * scale) + bob),
                (cx - int(40 * scale), cy + int(5 * scale) + bob),
            ],
            fill=c_main,
            outline=outline,
        )
        draw.polygon(
            [
                (cx + w, cy + int(5 * scale) + bob),
                (cx + int(55 * scale), cy + int(75 * scale) + bob),
                (cx + int(25 * scale), cy + int(70 * scale) + bob),
                (cx + int(40 * scale), cy + int(5 * scale) + bob),
            ],
            fill=c_main,
            outline=outline,
        )

    elif any(k in p for k in ("potion", "flask", "elixir", "vial", "bottle")):
        # Alchemical Potion Bottle
        r_bulb = int(85 * scale)
        # Outer glass bottle
        draw.ellipse(
            [cx - r_bulb, cy - int(35 * scale) + bob, cx + r_bulb, cy + int(125 * scale) + bob],
            fill=(220, 242, 255, 195),
            outline=outline,
            width=max(2, int(4 * scale)),
        )
        # Liquid inside
        r_liq = int(72 * scale)
        draw.pieslice(
            [cx - r_liq, cy - int(22 * scale) + bob, cx + r_liq, cy + int(112 * scale) + bob],
            start=0,
            end=180,
            fill=c_main,
            outline=outline,
            width=max(1, int(2 * scale)),
        )
        draw.ellipse(
            [cx - r_liq, cy + int(28 * scale) + bob, cx + r_liq, cy + int(62 * scale) + bob],
            fill=c_glow,
            outline=outline,
        )
        # Bottle neck
        nw = int(28 * scale)
        draw.rectangle(
            [cx - nw, cy - int(95 * scale) + bob, cx + nw, cy - int(28 * scale) + bob],
            fill=(220, 242, 255, 220),
            outline=outline,
            width=max(2, int(4 * scale)),
        )
        # Gold collar & gem
        draw.rectangle(
            [cx - int(38 * scale), cy - int(55 * scale) + bob, cx + int(38 * scale), cy - int(35 * scale) + bob],
            fill=c_sec,
            outline=outline,
            width=max(2, int(3 * scale)),
        )
        draw.ellipse(
            [cx - int(12 * scale), cy - int(56 * scale) + bob, cx + int(12 * scale), cy - int(32 * scale) + bob],
            fill=c_acc,
            outline=outline,
        )
        # Cork
        draw.polygon(
            [
                (cx - int(24 * scale), cy - int(95 * scale) + bob),
                (cx + int(24 * scale), cy - int(95 * scale) + bob),
                (cx + int(30 * scale), cy - int(130 * scale) + bob),
                (cx - int(30 * scale), cy - int(130 * scale) + bob),
            ],
            fill=(186, 122, 68, 255),
            outline=outline,
        )
        # Specular glass highlight
        draw.arc(
            [cx - int(65 * scale), cy - int(15 * scale) + bob, cx + int(40 * scale), cy + int(95 * scale) + bob],
            start=195,
            end=255,
            fill=(255, 255, 255, 230),
            width=max(3, int(6 * scale)),
        )

    elif any(k in p for k in ("sword", "blade", "dagger", "weapon", "claymore")):
        # Stylized Fantasy Sword
        bw = int(26 * scale) if view != "side" else int(10 * scale)
        # Blade
        draw.polygon(
            [
                (cx, cy - int(155 * scale) + bob),
                (cx + bw, cy - int(105 * scale) + bob),
                (cx + int(bw * 1.15), cy + int(25 * scale) + bob),
                (cx - int(bw * 1.15), cy + int(25 * scale) + bob),
                (cx - bw, cy - int(105 * scale) + bob),
            ],
            fill=c_main,
            outline=outline,
        )
        # Fuller highlight
        draw.line(
            [(cx, cy - int(135 * scale) + bob), (cx, cy + int(20 * scale) + bob)],
            fill=(255, 255, 255, 210),
            width=max(2, int(4 * scale)),
        )
        # Crossguard
        gw = int(76 * scale) if view != "side" else int(24 * scale)
        draw.polygon(
            [
                (cx - gw, cy + int(18 * scale) + bob),
                (cx, cy + int(8 * scale) + bob),
                (cx + gw, cy + int(18 * scale) + bob),
                (cx + int(gw * 0.85), cy + int(42 * scale) + bob),
                (cx, cy + int(36 * scale) + bob),
                (cx - int(gw * 0.85), cy + int(42 * scale) + bob),
            ],
            fill=c_sec,
            outline=outline,
        )
        # Center rune gem
        r_gem = int(16 * scale)
        draw.ellipse(
            [cx - r_gem, cy + int(12 * scale) + bob, cx + r_gem, cy + int(44 * scale) + bob],
            fill=c_acc,
            outline=outline,
        )
        # Grip & Pommel
        draw.rectangle(
            [cx - int(11 * scale), cy + int(42 * scale) + bob, cx + int(11 * scale), cy + int(115 * scale) + bob],
            fill=(110, 65, 45, 255),
            outline=outline,
        )
        draw.ellipse(
            [cx - int(22 * scale), cy + int(110 * scale) + bob, cx + int(22 * scale), cy + int(148 * scale) + bob],
            fill=c_sec,
            outline=outline,
        )

    else:
        # Stylized Hero Character (Knight / Alchemist / Creature)
        leg_spread = int(math.sin(frame_idx * math.pi / 2) * 16 * scale)
        # Cape (drawn behind in front view, in front in back view)
        cape_poly = [
            (cx - int(48 * scale), cy - int(35 * scale) + bob),
            (cx + int(48 * scale), cy - int(35 * scale) + bob),
            (cx + int(72 * scale), cy + int(110 * scale) + bob),
            (cx - int(72 * scale), cy + int(110 * scale) + bob),
        ]
        if view != "back":
            draw.polygon(cape_poly, fill=c_acc, outline=outline)

        # Boots / Legs
        draw.rounded_rectangle(
            [
                cx - int(38 * scale) + leg_spread,
                cy + int(55 * scale) + bob,
                cx - int(10 * scale) + leg_spread,
                cy + int(135 * scale) + bob,
            ],
            radius=int(8 * scale),
            fill=(55, 58, 78, 255),
            outline=outline,
            width=max(2, int(3 * scale)),
        )
        draw.rounded_rectangle(
            [
                cx + int(10 * scale) - leg_spread,
                cy + int(55 * scale) + bob,
                cx + int(38 * scale) - leg_spread,
                cy + int(135 * scale) + bob,
            ],
            radius=int(8 * scale),
            fill=(55, 58, 78, 255),
            outline=outline,
            width=max(2, int(3 * scale)),
        )

        # Armored Torso
        tw = int(52 * scale) if view != "side" else int(34 * scale)
        draw.rounded_rectangle(
            [cx - tw, cy - int(35 * scale) + bob, cx + tw, cy + int(68 * scale) + bob],
            radius=int(16 * scale),
            fill=c_main,
            outline=outline,
            width=max(2, int(4 * scale)),
        )
        # Belt & buckle
        draw.rectangle(
            [cx - tw, cy + int(36 * scale) + bob, cx + tw, cy + int(54 * scale) + bob],
            fill=(75, 52, 38, 255),
            outline=outline,
        )
        draw.rectangle(
            [cx - int(14 * scale), cy + int(33 * scale) + bob, cx + int(14 * scale), cy + int(57 * scale) + bob],
            fill=c_sec,
            outline=outline,
        )

        # Pauldrons
        if view != "side":
            draw.ellipse(
                [cx - tw - int(26 * scale), cy - int(42 * scale) + bob, cx - tw + int(18 * scale), cy + int(4 * scale) + bob],
                fill=c_sec,
                outline=outline,
                width=max(2, int(3 * scale)),
            )
            draw.ellipse(
                [cx + tw - int(18 * scale), cy - int(42 * scale) + bob, cx + tw + int(26 * scale), cy + int(4 * scale) + bob],
                fill=c_sec,
                outline=outline,
                width=max(2, int(3 * scale)),
            )

        # Head & Helmet
        hr = int(46 * scale)
        draw.ellipse(
            [cx - hr, cy - int(122 * scale) + bob, cx + hr, cy - int(30 * scale) + bob],
            fill=(248, 212, 180, 255),
            outline=outline,
            width=max(2, int(4 * scale)),
        )
        # Helmet crown + plume
        draw.polygon(
            [
                (cx, cy - int(158 * scale) + bob),
                (cx - int(24 * scale), cy - int(115 * scale) + bob),
                (cx + int(28 * scale), cy - int(115 * scale) + bob),
            ],
            fill=c_acc,
            outline=outline,
        )
        draw.pieslice(
            [cx - hr - int(4 * scale), cy - int(128 * scale) + bob, cx + hr + int(4 * scale), cy - int(35 * scale) + bob],
            start=180,
            end=360,
            fill=c_sec,
            outline=outline,
            width=max(2, int(3 * scale)),
        )

        if view == "front":
            # Eyes
            draw.ellipse([cx - int(20 * scale), cy - int(78 * scale) + bob, cx - int(8 * scale), cy - int(62 * scale) + bob], fill=outline)
            draw.ellipse([cx + int(8 * scale), cy - int(78 * scale) + bob, cx + int(20 * scale), cy - int(62 * scale) + bob], fill=outline)
            # Chest emblem
            draw.polygon(
                [
                    (cx, cy - int(18 * scale) + bob),
                    (cx + int(18 * scale), cy + int(4 * scale) + bob),
                    (cx, cy + int(26 * scale) + bob),
                    (cx - int(18 * scale), cy + int(4 * scale) + bob),
                ],
                fill=c_sec,
                outline=outline,
            )
        elif view == "side":
            draw.ellipse([cx + int(12 * scale), cy - int(78 * scale) + bob, cx + int(24 * scale), cy - int(62 * scale) + bob], fill=outline)
        elif view == "back":
            draw.polygon(cape_poly, fill=c_acc, outline=outline)

        # Kitbash / Prop in hand if mentioned
        if any(k in p for k in ("kitbash", "sword", "staff", "shield", "potion", "equipped", "holding")):
            sx = cx + tw + int(18 * scale)
            draw.line(
                [(sx, cy - int(95 * scale) + bob), (sx, cy + int(65 * scale) + bob)],
                fill=c_sec,
                width=max(4, int(8 * scale)),
            )
            draw.ellipse(
                [sx - int(18 * scale), cy - int(115 * scale) + bob, sx + int(18 * scale), cy - int(79 * scale) + bob],
                fill=c_glow,
                outline=outline,
            )


def generate_demo_image(
    output_path: Path,
    prompt: str = "Stylized fantasy knight",
    palette: list[str] | None = None,
    seed: int = 42,
    size: tuple[int, int] = (512, 512),
    transparent: bool = True,
    mode: str = "standard",  # "standard", "turnaround", "sprite_sheet", "tileable_texture", "icon"
) -> dict[str, int | str | bool]:
    colors = _pick_colors(prompt, palette, seed)
    w, h = size
    bg = (0, 0, 0, 0) if transparent and mode != "tileable_texture" else (30, 34, 48, 255)
    img = Image.new("RGBA", (w, h), bg)
    draw = ImageDraw.Draw(img)

    if mode == "turnaround":
        # 3-view sheet: Front, Side, Back
        img = Image.new("RGBA", (768, 512), bg)
        draw = ImageDraw.Draw(img)
        views = [("front", 135, "FRONT"), ("side", 384, "SIDE"), ("back", 633, "BACK")]
        for v_name, vx, label in views:
            _draw_subject(draw, vx, 265, 0.82, prompt, colors, view=v_name)
            # Subtle ground shadow
            draw.ellipse([vx - 65, 390, vx + 65, 415], fill=(10, 12, 20, 75))
            draw.rounded_rectangle([vx - 42, 435, vx + 42, 465], radius=8, fill=(22, 25, 38, 210))
            draw.text((vx - 22, 444), label, fill=(230, 238, 255, 255))
        w, h = 768, 512

    elif mode == "sprite_sheet":
        # 4-frame animation strip
        img = Image.new("RGBA", (1024, 256), bg)
        draw = ImageDraw.Draw(img)
        for idx in range(4):
            fx = 128 + idx * 256
            _draw_subject(draw, fx, 130, 0.56, prompt, colors, view="front", frame_idx=idx)
        w, h = 1024, 256

    elif mode == "tileable_texture":
        # Seamless procedural stylized stone/crystal/wood pattern
        c1, c2, c3, _ = colors
        img = Image.new("RGBA", (w, h), (c1[0] // 2, c1[1] // 2, c1[2] // 2, 255))
        draw = ImageDraw.Draw(img)
        grid_n = 4
        cell_w = w // grid_n
        cell_h = h // grid_n
        for gx in range(-1, grid_n + 1):
            for gy in range(-1, grid_n + 1):
                ox = (gy % 2) * (cell_w // 2)
                x0 = gx * cell_w + ox + 4
                y0 = gy * cell_h + 4
                x1 = x0 + cell_w - 8
                y1 = y0 + cell_h - 8
                fill_c = c1 if (gx + gy) % 2 == 0 else c2
                draw.rounded_rectangle(
                    [x0, y0, x1, y1],
                    radius=14,
                    fill=(fill_c[0], fill_c[1], fill_c[2], 235),
                    outline=(20, 24, 34, 255),
                    width=3,
                )
                draw.line([(x0 + 12, y0 + 12), (x1 - 16, y0 + 12)], fill=(c3[0], c3[1], c3[2], 140), width=3)

    elif mode == "icon":
        # Framed glossy RPG inventory icon with alpha corner padding
        pad = 28
        draw.rounded_rectangle(
            [pad, pad, w - pad, h - pad],
            radius=56,
            fill=(26, 30, 46, 245),
            outline=colors[1],
            width=10,
        )
        draw.rounded_rectangle(
            [pad + 14, pad + 14, w - pad - 14, h - pad - 14],
            radius=44,
            fill=(38, 44, 68, 255),
            outline=(255, 255, 255, 60),
            width=3,
        )
        _draw_subject(draw, w // 2, h // 2 + 8, 1.05, prompt, colors, view="front")

    else:
        # Soft contact shadow on alpha canvas
        draw.ellipse([w // 2 - 95, h - 92, w // 2 + 95, h - 52], fill=(10, 12, 20, 80))
        _draw_subject(draw, w // 2, h // 2 + 10, 1.15, prompt, colors, view="front")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, format="PNG")

    # Also save 256px thumbnail
    thumb_path = output_path.with_name(f"{output_path.stem}_thumb.png")
    thumb = img.copy()
    thumb.thumbnail((256, 256), Image.Resampling.LANCZOS)
    thumb.save(thumb_path, format="PNG")

    return {
        "width": w,
        "height": h,
        "transparent": transparent and mode != "tileable_texture",
        "thumb_name": thumb_path.name,
    }

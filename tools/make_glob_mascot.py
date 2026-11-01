"""Generate Glob, the pixel-art smith slime, as animated SVGs in forge/ui/.

Edit the pixel maps below and run:  uv run python tools/make_glob_mascot.py
  glob-forge.svg  hammering on the anvil (header, onboarding, "thinking")
  glob.svg        resting with the hammer down, blinking
  glob-face.svg   just the slime, blinking (chat avatars, favicon)
  glob-icon.png / glob.ico   app icon for the desktop window and installer
"""

from __future__ import annotations

from pathlib import Path

PALETTE = {
    "K": "#3b0a1e",  # slime outline
    "P": "#ff7a9c",  # body
    "L": "#ffb3c6",  # light
    "W": "#ffffff",  # shine
    "D": "#e0436b",  # shadow
    "E": "#1a0610",  # eyes
    "C": "#ff4f7b",  # cheeks
    "B": "#7c2d12",  # apron strap
    "A": "#3f3f46",  # anvil outline
    "M": "#71717a",  # anvil
    "N": "#d4d4d8",  # anvil highlight
    "H": "#a16207",  # hammer handle
    "G": "#52525b",  # hammer head
    "g": "#a1a1aa",  # hammer head light
    "S": "#fde047",  # spark
    "s": "#ffd1dc",  # pink spark
}

# 32 columns; row numbers are the y coordinate.
SLIME = {
    7: "       KKKKKK",
    8: "     KKLLPPPPKK",
    9: "    KLWLPPPPPPPK",
    10: "   KPLWPPPPPPPPPK",
    11: "   KPPPPPPPPPPPPK",
    12: "  KPPPEEPPPPEEPPPK",
    13: "  KPPPEWPPPPEWPPPK",
    14: "  KPPCCPPPPPPCCPPK",
    15: "  KPPPPPPKKPPPPPPK",
    16: "  KDBPPPPPPPPPPBDK",
    17: "  KDDBBPPPPPPBBDDK",
    18: "   KDDDBBBBBBDDDK",
    19: "    KKKKKKKKKKKK",
}
EYELIDS = {12: "      KK    KK", 13: "      KK    KK"}
ANVIL = {
    14: "                    AAAAAAAAAAA",
    15: "                    ANNNNNNNNMA",
    16: "                     AAMMMMMAA",
    17: "                       AMMMA",
    18: "                      AMMMMMA",
    19: "                     AAAAAAAAA",
}
HAMMER_UP = {
    5: "                  AGGGGA",
    6: "                  AggGGA",
    7: "                  AGGGGA",
    8: "                    H",
    9: "                   H",
    10: "                  H",
    11: "                 H",
    12: "                H",
}
HAMMER_DOWN = {
    10: "                       AGGA",
    11: "                       AggA",
    12: "                 HHHHHHAGGA",
    13: "                       AGGA",
}
SPARKS = {
    7: "                         S",
    8: "                    s",
    9: "                            S",
    10: "                             s",
    12: "                           S",
}


def rects(pixels: dict[int, str]) -> str:
    out = []
    for y, row in pixels.items():
        for x, ch in enumerate(row):
            if ch != " ":
                out.append(f'<rect x="{x}" y="{y}" width="1" height="1" fill="{PALETTE[ch]}"/>')
    return "".join(out)


def svg(animated: bool, face: bool = False) -> str:
    style = """
.lid{opacity:0;animation:blink 4.2s steps(1) infinite}
@keyframes blink{0%,94%{opacity:0}95%,99%{opacity:1}}
"""
    if animated:
        style += """
.up{animation:up .8s steps(1) infinite}.down{animation:down .8s steps(1) infinite}
.spark{animation:spark .8s steps(1) infinite}.body{animation:squish .8s steps(1) infinite}
@keyframes up{0%{opacity:1}55%{opacity:0}}
@keyframes down{0%{opacity:0}55%{opacity:1}}
@keyframes spark{0%{opacity:0}55%{opacity:1}80%{opacity:0}}
@keyframes squish{0%{transform:none}55%{transform:translateY(.4px)}}
"""
    if face:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="2 6 16 15" shape-rendering="crispEdges">'
            f"<style>{style.strip()}</style>"
            f'{rects(SLIME)}<g class="lid">{rects(EYELIDS)}</g>'
            "</svg>\n"
        )
    hammer = (
        f'<g class="up">{rects(HAMMER_UP)}</g><g class="down">{rects(HAMMER_DOWN)}</g><g class="spark">{rects(SPARKS)}</g>'
        if animated
        else rects(HAMMER_DOWN)
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="1 4 31 16" shape-rendering="crispEdges">'
        f"<style>{style.strip()}</style>"
        f"{rects(ANVIL)}{hammer}"
        f'<g class="body">{rects(SLIME)}<g class="lid">{rects(EYELIDS)}</g></g>'
        "</svg>\n"
    )


def write_icons(ui: Path) -> None:
    """Pixel-perfect app icon: the slime centred on a dark rounded square."""
    from PIL import Image, ImageDraw

    px = 12
    size = 256
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(icon).rounded_rectangle([0, 0, size - 1, size - 1], 56, fill="#140b12")
    x0 = (size - 16 * px) // 2 - 2 * px
    y0 = (size - 13 * px) // 2 - 7 * px
    draw = ImageDraw.Draw(icon)
    for y, row in SLIME.items():
        for x, ch in enumerate(row):
            if ch != " ":
                draw.rectangle([x0 + x * px, y0 + y * px, x0 + (x + 1) * px - 1, y0 + (y + 1) * px - 1], fill=PALETTE[ch])
    icon.save(ui / "glob-icon.png")
    icon.save(ui / "glob.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    ui = Path(__file__).resolve().parents[1] / "forge" / "ui"
    (ui / "glob-forge.svg").write_text(svg(animated=True), encoding="utf-8")
    (ui / "glob.svg").write_text(svg(animated=False), encoding="utf-8")
    (ui / "glob-face.svg").write_text(svg(animated=False, face=True), encoding="utf-8")
    write_icons(ui)
    print("wrote", ui / "glob-forge.svg", "and", ui / "glob.svg")

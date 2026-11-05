"""Modular level kits: tiling materials and exact-size pieces for procedural levels, made on this computer for free.

AI meshes never line up, so Glob builds the shapes itself and AI only paints them:

* A **material** asset (`kind: material`) is a picked image made seamless, with normal, roughness, height and ORM maps
  derived from it. The maps go in ``<asset>/textures/``.
* A **kit** asset (`kind: kit`) is one GLB in ``<asset>/3d/`` holding every piece as a named node, sharing its materials,
  plus ``kit.json``, which tells a coding agent how the pieces fit, and a sample room built from the same pieces.

Conventions (also written into kit.json): metres, +Y up, north is -Z, and every piece's origin is the centre of its
cell on the floor plane, so turning a piece about Y keeps it in its cell. Walls sit inside the cell, on its north edge.
"""

from __future__ import annotations

import json
import math
import struct
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from forge.asset_studio import _ws_rel, assets_under, output_dir, read_asset, update_asset
from forge.overview import resolve_rel

TEXTURE_SIZE = 1024
MAP_NAMES = ("albedo", "normal", "roughness", "height", "orm")
DEFAULT_TILE_M = 2.0
SLOTS = ("wall", "floor", "ceiling")
# Extra surfaces of the same size, so generated levels don't repeat one texture. They exist only when a material is set.
VARIANT_SLOTS = ("wall_2", "wall_3", "floor_2")
PLACEHOLDER_GREY = {"wall": 0.55, "floor": 0.4, "ceiling": 0.65}
DEFAULT_RELIEF_M = 0.05
RELIEF_STEP_M = 0.05  # one grid square of a relief surface, about a hand's width: enough for a close-up camera
RELIEF_MAX_STEPS = 120
FLOOR_RELIEF_SHARE = 0.4  # floors stay low, so feet and props sit level
KIT_MANIFEST = "kit.json"
SAMPLE_SUFFIX = "-sample-room"


# ---------- materials ----------

def _blur_wrapped(image: Image.Image, radius: float) -> Image.Image:
    """A blur that sees across the edges, so what it returns still tiles."""
    size = image.width
    pad = min(size, int(radius * 3) + 1)
    canvas = Image.new(image.mode, (size + 2 * pad, size + 2 * pad))
    for dx in (-size, 0, size):
        for dy in (-size, 0, size):
            canvas.paste(image, (pad + dx, pad + dy))
    return canvas.filter(ImageFilter.GaussianBlur(radius)).crop((pad, pad, pad + size, pad + size))


def _even_lighting(image: Image.Image) -> Image.Image:
    """Flatten large patches of light and dark: on a repeated texture they are what the eye catches as a grid."""
    bands = []
    for band in image.split():
        mean = round(ImageStat.Stat(band).mean[0])
        wide = band.filter(ImageFilter.GaussianBlur(image.width / 6))
        bands.append(ImageChops.subtract(band, wide, scale=1, offset=mean))
    return Image.blend(image, Image.merge("RGB", bands), 0.7)


def _left_of_seam(cost: Image.Image, band: int, margin: int) -> Image.Image:
    """Black left of the cheapest top-to-bottom path through the band, white right of it."""
    width, rows = cost.size
    pixels = cost.tobytes()
    columns = range(margin, band)
    totals = [pixels[x] for x in columns]
    steps: list[list[int]] = []
    last = len(totals) - 1
    for y in range(1, rows):
        row = pixels[y * width + margin:y * width + band]
        came_from, updated = [], []
        for i in range(last + 1):
            best = i
            if i and totals[i - 1] < totals[best]:
                best = i - 1
            if i < last and totals[i + 1] < totals[best]:
                best = i + 1
            came_from.append(best)
            updated.append(totals[best] + row[i])
        steps.append(came_from)
        totals = updated
    at = totals.index(min(totals))
    path = [at]
    for came_from in reversed(steps):
        at = came_from[at]
        path.append(at)
    path.reverse()
    mask = bytearray(b"\xff" * (width * rows))
    for y, i in enumerate(path):
        cut = margin + i
        mask[y * width:y * width + cut] = bytes(cut)
    return Image.frombytes("L", cost.size, bytes(mask))


def _already_tiles(image: Image.Image) -> bool:
    """True when crossing the wrap is no bigger a jump than stepping between any two neighbouring rows or columns."""
    grey = image.convert("L")

    def jump(a: Image.Image, b: Image.Image) -> float:
        return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]

    for flat in (grey, grey.transpose(Image.TRANSPOSE)):
        w, h = flat.size
        wrap = jump(flat.crop((0, 0, 1, h)), flat.crop((w - 1, 0, w, h)))
        inside = jump(flat.crop((0, 0, w - 1, h)), flat.crop((1, 0, w, h)))
        if wrap > inside * 1.5 + 1:
            return False
    return True


def make_seamless(image: Image.Image, band: float = 0.2) -> Image.Image:
    """Tile an image. A copy shifted by half already meets itself at the edges, so it gives the border. The original is
    laid over the middle, cut along the paths where the two agree most: stones stay whole, with no see-through blend."""
    size = image.width
    if _already_tiles(image):
        return image  # any cut would only make a good picture worse
    shifted = ImageChops.offset(image, size // 2, size // 2)
    half = size // 2
    cost = ImageChops.difference(image, shifted).convert("L").filter(ImageFilter.GaussianBlur(1)).resize((half, half), Image.BOX)
    width, margin = max(6, int(half * band)), 4
    # One seam per side; each side is turned to the left, cut, and turned back.
    mask = Image.new("L", (half, half), 255)
    for turn, back in ((None, None), (Image.ROTATE_90, Image.ROTATE_270), (Image.ROTATE_180, Image.ROTATE_180), (Image.ROTATE_270, Image.ROTATE_90)):
        side = _left_of_seam(cost.transpose(turn) if turn is not None else cost, width, margin)
        mask = ImageChops.darker(mask, side.transpose(back) if back is not None else side)
    mask = mask.resize((size, size), Image.BILINEAR).filter(ImageFilter.GaussianBlur(2.5))
    return Image.composite(image, shifted, mask)


def _normal_map(height: Image.Image, strength: float = 2.5) -> Image.Image:
    """OpenGL-style normals (green points up the image), as glTF expects; neighbours wrap, so the map tiles."""
    dx = ImageChops.subtract(ImageChops.offset(height, -1, 0), ImageChops.offset(height, 1, 0), scale=2, offset=128).tobytes()
    dy = ImageChops.subtract(ImageChops.offset(height, 0, -1), ImageChops.offset(height, 0, 1), scale=2, offset=128).tobytes()
    tables = [bytearray(65536), bytearray(65536), bytearray(65536)]
    k = strength * 2 / 255
    for a in range(256):
        nx = -(a - 128) * k
        for b in range(256):
            ny = (b - 128) * k
            scale = 1 / math.sqrt(nx * nx + ny * ny + 1)
            index = a << 8 | b
            tables[0][index] = round((nx * scale + 1) * 127.5)
            tables[1][index] = round((ny * scale + 1) * 127.5)
            tables[2][index] = round((scale + 1) * 127.5)
    keys = [a << 8 | b for a, b in zip(dx, dy)]
    bands = [Image.frombytes("L", height.size, bytes(map(table.__getitem__, keys))) for table in tables]
    return Image.merge("RGB", bands)


def material_paths(asset_path: Path) -> dict[str, Path]:
    folder = output_dir(asset_path) / "textures"
    return {name: folder / f"{asset_path.stem}-{name}.png" for name in MAP_NAMES}


def build_material(ws_dir: Path, asset_rel: str) -> list[str]:
    """Turn a material's picked image into seamless maps. Free and local; running it again replaces them."""
    asset_path, meta, _ = read_asset(ws_dir, asset_rel)
    if meta.get("kind") != "material":
        raise ValueError("Only a material asset can be made tileable.")
    winner = str(meta.get("winner") or "")
    if not winner:
        raise ValueError("Pick an image for this material first.")
    source = Image.open(resolve_rel(ws_dir, winner)).convert("RGBA")
    flat = Image.new("RGB", source.size, (128, 128, 128))
    flat.paste(source, mask=source.getchannel("A"))
    albedo = make_seamless(_even_lighting(ImageOps.fit(flat, (TEXTURE_SIZE, TEXTURE_SIZE), Image.LANCZOS)))

    # Height: the fine detail of the picture (bright is high). Roughness and shading in the crevices follow from it.
    grey = albedo.convert("L")
    height = ImageOps.autocontrast(ImageChops.subtract(grey, _blur_wrapped(grey, TEXTURE_SIZE / 16), scale=1, offset=128), cutoff=1)
    height = _blur_wrapped(height, 1)
    roughness = height.point(lambda v: round(150 + (255 - v) * 0.35))
    occlusion = height.point(lambda v: min(255, 110 + v * 2))
    orm = Image.merge("RGB", (occlusion, roughness, Image.new("L", height.size, 0)))

    paths = material_paths(asset_path)
    paths["albedo"].parent.mkdir(parents=True, exist_ok=True)
    images = {"albedo": albedo, "normal": _normal_map(height), "roughness": roughness, "height": height, "orm": orm}
    for name, image in images.items():
        image.save(paths[name], optimize=False)
    update_asset(ws_dir, asset_rel, {"material_source": winner, "material_built": time.strftime("%Y%m%d-%H%M%S")})
    return [_ws_rel(ws_dir, paths[name]) for name in MAP_NAMES]


def list_materials(ws_dir: Path) -> list[dict[str, Any]]:
    """The project's materials, for a kit to choose from. `built`: its maps exist, so a kit can use it."""
    found = []
    for rel in assets_under(ws_dir):
        asset_path, meta, _ = read_asset(ws_dir, rel)
        if meta.get("kind") == "material":
            built = all(path.is_file() for path in material_paths(asset_path).values())
            found.append({"path": rel, "title": str(meta.get("title") or asset_path.stem.replace("-", " ").title()), "built": built})
    return found


# ---------- pieces ----------

# Each face: its normal, the direction its texture runs across (U) and the direction that is up in the picture.
# U x Up = normal, so the corners below wind counter-clockwise from outside and the tangent's handedness is always +1.
_FACES = {
    "+x": ((1, 0, 0), (0, 0, -1), (0, 1, 0)),
    "-x": ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
    "+y": ((0, 1, 0), (1, 0, 0), (0, 0, -1)),
    "-y": ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
    "+z": ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
    "-z": ((0, 0, -1), (-1, 0, 0), (0, 1, 0)),
}


class _Relief:
    """A material's height map, sampled by texture position, for surfaces that really have depth."""

    def __init__(self, height_png: Path, depth: float) -> None:
        image = Image.open(height_png).convert("L").resize((512, 512), Image.BOX)
        self.values = _blur_wrapped(image, 1.5).tobytes()
        self.size = 512
        self.depth = depth

    def at(self, u: float, v: float) -> float:
        """How far below the surface's plane this spot sits, in metres: the highest parts touch the plane."""
        x = int(u % 1 * self.size) % self.size
        y = int(v % 1 * self.size) % self.size
        return (1 - self.values[y * self.size + x] / 255) * self.depth


class _Geometry:
    """Axis-aligned boxes, one vertex list per material slot. UVs come from position, so a surface runs on across
    neighbouring pieces when the cell is a whole number of texture repeats.

    A relief face is a grid pushed into the box and never out of it: the piece keeps its exact size, and the height map
    tiles, so neighbouring walls still meet. Its open edges end against the box's own side faces."""

    def __init__(self, tiles: dict[str, float], reliefs: dict[str, _Relief] | None = None) -> None:
        self.tiles = tiles
        self.reliefs = reliefs or {}
        self.slots: dict[str, dict[str, list]] = {}

    def _relief_face(self, slot: str, face: tuple, centre: tuple, half: tuple) -> None:
        normal, across, up = face
        data = self.slots.setdefault(slot, {"p": [], "n": [], "t": [], "uv": [], "i": []})
        relief, tile = self.reliefs[slot], self.tiles[slot]
        width = 2 * sum(abs(across[i]) * half[i] for i in range(3))
        height = 2 * sum(abs(up[i]) * half[i] for i in range(3))
        columns = max(1, min(RELIEF_MAX_STEPS, round(width / RELIEF_STEP_M)))
        rows = max(1, min(RELIEF_MAX_STEPS, round(height / RELIEF_STEP_M)))
        start = len(data["p"])
        plane = tuple(centre[i] + normal[i] * half[i] for i in range(3))
        nudge = 1 / relief.size
        for row in range(rows + 1):
            for column in range(columns + 1):
                a = (column / columns - 0.5) * width
                b = (row / rows - 0.5) * height
                flat = tuple(plane[i] + across[i] * a + up[i] * b for i in range(3))
                u = sum(flat[i] * across[i] for i in range(3)) / tile
                v = -sum(flat[i] * up[i] for i in range(3)) / tile
                sink = relief.at(u, v)
                # The slope of the surface, from the heights either side, tilts the normal.
                slope_a = (relief.at(u - nudge, v) - relief.at(u + nudge, v)) / (2 * nudge * tile)
                slope_b = (relief.at(u, v + nudge) - relief.at(u, v - nudge)) / (2 * nudge * tile)
                raw = tuple(normal[i] - across[i] * slope_a - up[i] * slope_b for i in range(3))
                length = math.sqrt(sum(value * value for value in raw))
                data["p"].append(tuple(flat[i] - normal[i] * sink for i in range(3)))
                data["n"].append(tuple(value / length for value in raw))
                data["t"].append((*across, 1))
                data["uv"].append((u, v))
        stride = columns + 1
        for row in range(rows):
            for column in range(columns):
                a = start + row * stride + column
                data["i"] += [a, a + 1, a + stride + 1, a, a + stride + 1, a + stride]

    def box(self, slot: str, x: tuple[float, float], y: tuple[float, float], z: tuple[float, float], skip: tuple[str, ...] = (), relief: tuple[str, ...] = ()) -> None:
        data = self.slots.setdefault(slot, {"p": [], "n": [], "t": [], "uv": [], "i": []})
        centre = ((x[0] + x[1]) / 2, (y[0] + y[1]) / 2, (z[0] + z[1]) / 2)
        half = ((x[1] - x[0]) / 2, (y[1] - y[0]) / 2, (z[1] - z[0]) / 2)
        tile = self.tiles[slot]
        for name, (normal, across, up) in _FACES.items():
            if name in skip:
                continue
            if name in relief and slot in self.reliefs:
                self._relief_face(slot, (normal, across, up), centre, half)
                continue
            start = len(data["p"])
            for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                point = tuple(centre[i] + (normal[i] + su * across[i] + sv * up[i]) * half[i] for i in range(3))
                data["p"].append(point)
                data["n"].append(normal)
                data["t"].append((*across, 1))
                data["uv"].append((sum(point[i] * across[i] for i in range(3)) / tile, -sum(point[i] * up[i] for i in range(3)) / tile))
            data["i"] += [start, start + 1, start + 2, start, start + 2, start + 3]

    def triangles(self) -> int:
        return sum(len(data["i"]) // 3 for data in self.slots.values())

    def bounds(self) -> dict[str, list[float]]:
        points = [p for data in self.slots.values() for p in data["p"]]
        return {"min": [round(min(p[i] for p in points), 4) for i in range(3)], "max": [round(max(p[i] for p in points), 4) for i in range(3)]}


def _pieces(cell: float, height: float, thick: float, tiles: dict[str, float], reliefs: dict[str, _Relief] | None = None) -> list[dict[str, Any]]:
    edge = cell / 2
    north = (-edge, -edge + thick)  # a wall's depth: inside the cell, on its north edge
    door_w, door_h = min(1.2, cell * 0.6), min(2.2, height * 0.8)
    window_w, sill, window_top = min(1.0, cell * 0.5), min(0.9, height * 0.3), min(2.1, height * 0.75)
    post = max(0.4, thick * 2) / 2
    open_sides = {"north": "open", "east": "open", "south": "open", "west": "open"}

    def piece(name: str, layer: str, sides: dict[str, str] | None = None, footprint: tuple[int, int] = (1, 1), variant_of: str = "") -> dict[str, Any]:
        entry = {"name": name, "layer": layer, "sides": {**open_sides, **(sides or {})}, "footprint": list(footprint), "geo": _Geometry(tiles, reliefs), "variant_of": variant_of}
        made.append(entry)
        return entry["geo"]

    made: list[dict[str, Any]] = []
    room_face = ("+z",)  # the side of a north wall that faces into the room
    piece("floor", "floor").box("floor", (-edge, edge), (-thick, 0), (-edge, edge), relief=("+y",))
    if "floor_2" in tiles:
        piece("floor_2", "floor", variant_of="floor").box("floor_2", (-edge, edge), (-thick, 0), (-edge, edge), relief=("+y",))
    piece("ceiling", "ceiling").box("ceiling", (-edge, edge), (height, height + thick), (-edge, edge))
    piece("wall", "wall", {"north": "wall"}).box("wall", (-edge, edge), (0, height), north, skip=("-y",), relief=room_face)
    for variant in ("wall_2", "wall_3"):
        if variant in tiles:
            piece(variant, "wall", {"north": "wall"}, variant_of="wall").box(variant, (-edge, edge), (0, height), north, skip=("-y",), relief=room_face)
    piece("wall_low", "wall", {"north": "low"}).box("wall", (-edge, edge), (0, min(1.0, height / 2)), north, skip=("-y",), relief=room_face)

    door = piece("doorway", "wall", {"north": "door"})
    door.box("wall", (-edge, -door_w / 2), (0, height), north, skip=("-y",), relief=room_face)
    door.box("wall", (door_w / 2, edge), (0, height), north, skip=("-y",), relief=room_face)
    door.box("wall", (-door_w / 2, door_w / 2), (door_h, height), north, skip=("+x", "-x"), relief=room_face)

    window = piece("window", "wall", {"north": "window"})
    window.box("wall", (-edge, -window_w / 2), (0, height), north, skip=("-y",), relief=room_face)
    window.box("wall", (window_w / 2, edge), (0, height), north, skip=("-y",), relief=room_face)
    window.box("wall", (-window_w / 2, window_w / 2), (0, sill), north, skip=("+x", "-x", "-y"), relief=room_face)
    window.box("wall", (-window_w / 2, window_w / 2), (window_top, height), north, skip=("+x", "-x"), relief=room_face)

    # The two walls of a corner overlap, so a relief hollow in one never opens a slit beside the other.
    inner = piece("corner_inner", "wall", {"north": "wall", "east": "wall"})
    inner.box("wall", (-edge, edge), (0, height), north, skip=("-y",), relief=room_face)
    inner.box("wall", (edge - thick, edge), (0, height), (-edge, edge), skip=("-y",), relief=("-x",))

    piece("corner_outer", "wall").box("wall", (edge - thick, edge), (0, height), north, skip=("-y",))
    piece("pillar", "decor").box("wall", (-post, post), (0, height), (-post, post), skip=("-y",), relief=("+x", "-x", "+z", "-z"))

    # Stairs climb one storey towards the north, across this cell and the one north of it.
    stairs = piece("stairs", "floor", {"north": "stairs_top", "south": "stairs_bottom", "east": "wall", "west": "wall"}, footprint=(1, 2))
    steps = max(2, round(height / 0.2))
    tread = cell * 2 / steps
    for i in range(steps):
        z_near = edge - i * tread
        stairs.box("floor", (-edge, edge), (0, height * (i + 1) / steps), (z_near - tread, z_near), skip=("-y",))
    return made


def _sample_room() -> list[dict[str, Any]]:
    """A 3 by 3 room with a door and a window: proof that the pieces fit, and an example of how to place them."""
    placed = [{"piece": "floor", "cell": [x, z], "rotation": 0} for z in range(3) for x in range(3)]
    placed += [
        {"piece": "corner_inner", "cell": [2, 0], "rotation": 0},
        {"piece": "corner_inner", "cell": [0, 0], "rotation": 90},
        {"piece": "corner_inner", "cell": [0, 2], "rotation": 180},
        {"piece": "corner_inner", "cell": [2, 2], "rotation": 270},
        {"piece": "wall", "cell": [1, 0], "rotation": 0},
        {"piece": "wall", "cell": [0, 1], "rotation": 90},
        {"piece": "doorway", "cell": [1, 2], "rotation": 180},
        {"piece": "window", "cell": [2, 1], "rotation": 270},
        {"piece": "pillar", "cell": [1, 1], "rotation": 0},
    ]
    return placed


# ---------- GLB ----------

def _write_glb(pieces: list[dict[str, Any]], materials: dict[str, dict[str, Any]], nodes: list[dict[str, Any]]) -> bytes:
    """One binary glTF: `materials` by slot (several slots may share one), every piece a mesh, `nodes` placing them."""
    binary = bytearray()
    views: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []

    def add_view(data: bytes, target: int | None = None) -> int:
        while len(binary) % 4:
            binary.append(0)
        view = {"buffer": 0, "byteOffset": len(binary), "byteLength": len(data)}
        if target:
            view["target"] = target
        binary.extend(data)
        views.append(view)
        return len(views) - 1

    def add_floats(rows: list[tuple], kind: str, bounds: bool = False) -> int:
        size = len(rows[0])
        accessor: dict[str, Any] = {
            "bufferView": add_view(struct.pack(f"<{len(rows) * size}f", *(v for row in rows for v in row)), 34962),
            "componentType": 5126, "count": len(rows), "type": kind,
        }
        if bounds:
            accessor["min"] = [min(row[i] for row in rows) for i in range(size)]
            accessor["max"] = [max(row[i] for row in rows) for i in range(size)]
        accessors.append(accessor)
        return len(accessors) - 1

    images: list[dict[str, Any]] = []
    textures: list[dict[str, Any]] = []
    gltf_materials: list[dict[str, Any]] = []
    index_of: dict[str, int] = {}
    image_index: dict[Path, int] = {}

    def texture(path: Path) -> dict[str, int]:
        if path not in image_index:
            images.append({"bufferView": add_view(path.read_bytes()), "mimeType": "image/png", "name": path.stem})
            textures.append({"source": len(images) - 1, "sampler": 0})
            image_index[path] = len(textures) - 1
        return {"index": image_index[path]}

    for slot, material in materials.items():
        key = material["key"]
        if key not in index_of:
            maps = material.get("maps")
            if maps:
                entry = {
                    "name": material["name"],
                    "pbrMetallicRoughness": {"baseColorTexture": texture(maps["albedo"]), "metallicRoughnessTexture": texture(maps["orm"]), "metallicFactor": 0.0},
                    "normalTexture": texture(maps["normal"]),
                    "occlusionTexture": texture(maps["orm"]),
                }
            else:
                grey = PLACEHOLDER_GREY[slot.split("_")[0]]
                entry = {"name": material["name"], "pbrMetallicRoughness": {"baseColorFactor": [grey, grey, grey, 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.9}}
            gltf_materials.append(entry)
            index_of[key] = len(gltf_materials) - 1

    meshes = []
    mesh_index = {}
    for item in pieces:
        primitives = []
        for slot, data in item["geo"].slots.items():
            indices = add_view(struct.pack(f"<{len(data['i'])}I", *data["i"]), 34963)
            accessors.append({"bufferView": indices, "componentType": 5125, "count": len(data["i"]), "type": "SCALAR"})
            index_accessor = len(accessors) - 1
            primitives.append({
                "attributes": {
                    "POSITION": add_floats(data["p"], "VEC3", bounds=True), "NORMAL": add_floats(data["n"], "VEC3"),
                    "TANGENT": add_floats(data["t"], "VEC4"), "TEXCOORD_0": add_floats(data["uv"], "VEC2"),
                },
                "indices": index_accessor, "material": index_of[materials[slot]["key"]],
            })
        mesh_index[item["name"]] = len(meshes)
        meshes.append({"name": item["name"], "primitives": primitives})

    gltf_nodes = []
    for node in nodes:
        angle = math.radians(node.get("rotation", 0))
        entry = {"name": node["name"], "mesh": mesh_index[node["piece"]], "translation": node["position"]}
        if angle:
            entry["rotation"] = [0.0, math.sin(angle / 2), 0.0, math.cos(angle / 2)]
        gltf_nodes.append(entry)

    while len(binary) % 4:
        binary.append(0)
    document: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "Glob modular kit"},
        "scene": 0, "scenes": [{"nodes": list(range(len(gltf_nodes)))}], "nodes": gltf_nodes, "meshes": meshes,
        "materials": gltf_materials, "accessors": accessors, "bufferViews": views, "buffers": [{"byteLength": len(binary)}],
    }
    if images:
        document |= {"images": images, "textures": textures, "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]}
    text = json.dumps(document, separators=(",", ":")).encode("utf-8")
    text += b" " * (-len(text) % 4)
    total = 12 + 8 + len(text) + 8 + len(binary)
    return b"".join((struct.pack("<4sII", b"glTF", 2, total), struct.pack("<I4s", len(text), b"JSON"), text, struct.pack("<I4s", len(binary), b"BIN\0"), bytes(binary)))


# ---------- kits ----------

def _number(meta: dict[str, Any], key: str, default: float, low: float, high: float, label: str) -> float:
    try:
        value = float(meta.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {low:g} and {high:g} metres.")
    return value


def _kit_material(ws_dir: Path, rel: str, slot: str, cell: float) -> tuple[dict[str, Any], float, str]:
    """The slot's material, its real tile size (a whole number of repeats per cell, so surfaces meet) and its asset."""
    if not rel:
        return {"key": f"blockout-{slot}", "name": f"blockout_{slot}"}, cell, ""
    asset_path, meta, _ = read_asset(ws_dir, rel)
    if meta.get("kind") != "material":
        raise ValueError(f"'{rel}' is not a material.")
    maps = material_paths(asset_path)
    if not all(path.is_file() for path in maps.values()):
        raise ValueError(f"Press Make tileable on {asset_path.stem} first, so the kit can use it.")
    try:
        wanted = float(meta.get("tile_m") or DEFAULT_TILE_M)
    except (TypeError, ValueError):
        wanted = DEFAULT_TILE_M
    repeats = max(1, round(cell / max(0.01, wanted)))
    return {"key": rel, "name": asset_path.stem, "maps": maps}, cell / repeats, rel


KIT_RULES = (
    "Use the nodes of `file` by name. Their position in the file only lays them out for viewing: place each one yourself.",
    "A piece at cell [x, z] on storey s goes at position [x * cell, s * height, z * cell], turned rotation_y degrees about +Y.",
    "`sides` are for rotation 0. Turning by 90 moves north to west, west to south, south to east and east to north.",
    "To build a room: put a floor in every room cell. On each edge between a room cell and anything else, the room cell gets a wall, "
    "doorway or window turned to face that edge; a cell with walls on two touching edges uses corner_inner instead of two walls.",
    "Where a room bends around a corner, walls meet only at a point: fill it with corner_outer in the room cell whose diagonal neighbour is outside.",
    "Two rooms sharing an edge: only one of them places the doorway; the other leaves that edge open.",
    "`stairs` covers its own cell and the one north of it: leave the ceiling out above them, and walk off at the north end one storey up.",
    "For wave function collapse: `open`, `door` and `stairs_*` can be walked through; `wall`, `window` and `low` block the way.",
    "`sample` is a working room: match it to check your placement code.",
    "A piece with `variant_of` fits anywhere the named piece does: swap at random so walls and floors don't repeat.",
    "`lod1` is the same piece without relief, in the same file: use it far from the camera, and as the collision shape.",
)


def build_kit(ws_dir: Path, asset_rel: str) -> dict[str, Any]:
    """Build a kit's GLB, sample room and kit.json from its header. Free and local; running it again replaces them."""
    asset_path, meta, _ = read_asset(ws_dir, asset_rel)
    if meta.get("kind") != "kit":
        raise ValueError("Only a modular kit can be built.")
    cell = _number(meta, "kit_cell", 2.0, 0.5, 20, "Cell size")
    height = _number(meta, "kit_height", 3.0, 0.5, 30, "Wall height")
    thick = _number(meta, "kit_thickness", 0.2, 0.02, 2, "Wall thickness")
    if thick > cell / 4:
        raise ValueError("Walls can be at most a quarter of the cell thick.")

    depth = _number(meta, "kit_relief", DEFAULT_RELIEF_M, 0, 0.5, "Relief depth")
    materials, tiles, assets, reliefs = {}, {}, {}, {}
    wanted = list(SLOTS) + [slot for slot in VARIANT_SLOTS if meta.get(f"kit_{slot}")]
    for slot in wanted:
        materials[slot], tiles[slot], assets[slot] = _kit_material(ws_dir, str(meta.get(f"kit_{slot}") or ""), slot, cell)
        maps = materials[slot].get("maps")
        if maps and depth and not slot.startswith("ceiling"):
            reliefs[slot] = _Relief(maps["height"], depth * (FLOOR_RELIEF_SHARE if slot.startswith("floor") else 1))
    pieces = _pieces(cell, height, thick, tiles, reliefs)
    by_name = {item["name"]: item for item in pieces}
    # A plain version of any piece with relief, for distance and collision.
    plain = {item["name"]: item for item in _pieces(cell, height, thick, tiles)} if reliefs else {}
    lods = [{**plain[item["name"]], "name": f"{item['name']}_lod1"} for item in pieces if item["geo"].triangles() != plain.get(item["name"], item)["geo"].triangles()]

    spacing = cell * 1.5
    row = [{"name": item["name"], "piece": item["name"], "position": [(i - (len(pieces) - 1) / 2) * spacing, 0.0, 0.0]} for i, item in enumerate(pieces)]
    row += [{"name": item["name"], "piece": item["name"], "position": [(i - (len(lods) - 1) / 2) * spacing, 0.0, height + spacing]} for i, item in enumerate(lods)]
    sample = _sample_room()
    room = [{"name": f"{p['piece']}_{i}", "piece": p["piece"], "rotation": p["rotation"], "position": [(p["cell"][0] - 1) * cell, 0.0, (p["cell"][1] - 1) * cell]} for i, p in enumerate(sample)]

    folder = output_dir(asset_path) / "3d"
    folder.mkdir(parents=True, exist_ok=True)
    kit_file = folder / f"{asset_path.stem}.glb"
    room_file = folder / f"{asset_path.stem}{SAMPLE_SUFFIX}.glb"
    for model in (kit_file, room_file):
        model.with_name(f"{model.stem}_thumb.png").unlink(missing_ok=True)  # it shows the kit as it was
    kit_file.write_bytes(_write_glb(pieces + lods, materials, row))
    room_file.write_bytes(_write_glb([by_name[name] for name in sorted({p["piece"] for p in sample})], materials, room))

    manifest = {
        "format": "glob-modular-kit", "version": 1, "name": asset_path.stem, "file": kit_file.name, "sample_file": room_file.name,
        "units": "metres", "up": "+Y",
        "grid": {"cell": cell, "height": height, "wall_thickness": thick, "relief_depth": depth if reliefs else 0},
        "directions": {"north": "-Z", "east": "+X", "south": "+Z", "west": "-X"},
        "pivot": "centre of the piece's first cell, on the floor plane",
        "rotation_y": "degrees about +Y; 90 turns north to west",
        "materials": {slot: {"asset": assets[slot] or None, "tile_metres": round(tiles[slot], 4)} for slot in wanted},
        "rules": list(KIT_RULES),
        "pieces": [{
            "name": item["name"], "layer": item["layer"], "footprint_cells": item["footprint"], "sides": item["sides"],
            "bounds": item["geo"].bounds(), "triangles": item["geo"].triangles(), "materials": sorted(item["geo"].slots),
            "rotations": [0, 90, 180, 270],
            **({"variant_of": item["variant_of"]} if item["variant_of"] else {}),
            **({"lod1": f"{item['name']}_lod1"} if any(lod["name"] == f"{item['name']}_lod1" for lod in lods) else {}),
        } for item in pieces],
        "sample": [{"piece": p["piece"], "cell": p["cell"], "rotation_y": p["rotation"]} for p in sample],
        **({"also_in_this_pack": {"props": str(meta["kit_props"]), "trim": str(meta["kit_trim"]), "note": "Find these folders' assets in ASSETS.md. Put trim over wall joins and in corners to hide them."}} if meta.get("kit_props") else {}),
    }
    manifest_file = folder / KIT_MANIFEST
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    update_asset(ws_dir, asset_rel, {"kit_built": time.strftime("%Y%m%d-%H%M%S")})
    return {"kit": _ws_rel(ws_dir, kit_file), "sample": _ws_rel(ws_dir, room_file), "manifest": _ws_rel(ws_dir, manifest_file), "pieces": [item["name"] for item in pieces]}

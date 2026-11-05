"""Modular kits: seamless materials and maps, exact-size pieces in a valid GLB, kit.json and the index."""

from __future__ import annotations

import io
import json
import math
import random
import struct
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat

from forge import asset_index, asset_studio, game_tools, modular


def _asset(ws: Path, rel: str, header: str) -> None:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{header}\n---\n# {path.stem}\n", encoding="utf-8")


def _picture(path: Path, size: int = 300) -> None:
    """Speckles on a strong left-to-right gradient: full of edges, and far from tiling."""
    rng = random.Random(7)
    image = Image.new("RGB", (size, size))
    image.putdata([(int(255 * (i % size) / size), rng.randrange(60, 120), 90) for i in range(size * size)])
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _material(ws: Path, name: str = "stone", header: str = "") -> str:
    rel = f"materials/{name}.md"
    picture = ws / "materials" / name / "candidates" / "pick.png"
    _picture(picture)
    _asset(ws, rel, f"kind: material\nprompt: {name}\nwinner: materials/{name}/candidates/pick.png{header}")
    modular.build_material(ws, rel)
    return rel


def _read_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    magic, version, total = struct.unpack("<4sII", data[:12])
    assert magic == b"glTF" and version == 2 and total == len(data)
    json_len, json_type = struct.unpack("<I4s", data[12:20])
    assert json_type == b"JSON" and json_len % 4 == 0
    doc = json.loads(data[20:20 + json_len])
    bin_len, bin_type = struct.unpack("<I4s", data[20 + json_len:28 + json_len])
    assert bin_type == b"BIN\0" and bin_len % 4 == 0 and bin_len == doc["buffers"][0]["byteLength"]
    return doc, data[28 + json_len:]


def _edge_jump(image: Image.Image) -> float:
    """How far apart the two sides of the wrap are, on average."""
    size = image.width
    left, right = image.crop((0, 0, 1, size)), image.crop((size - 1, 0, size, size))
    return sum(ImageStat.Stat(ImageChops.difference(left, right)).mean) / 3


def test_make_seamless_closes_the_wrap() -> None:
    size = 256
    image = Image.new("RGB", (size, size))
    image.putdata([(i % size, (i // size), 80) for i in range(size * size)])
    assert _edge_jump(image) > 60
    assert _edge_jump(modular.make_seamless(image)) < 3


def test_a_picture_that_already_tiles_is_left_alone() -> None:
    size = 128
    image = Image.new("RGB", (size, size))
    image.putdata([(int(127 + 120 * math.sin(2 * math.pi * (i % size) / size)), int(127 + 120 * math.cos(2 * math.pi * (i // size) / size)), 60) for i in range(size * size)])
    assert modular.make_seamless(image) is image


def test_seam_cut_keeps_the_picture_solid() -> None:
    """Each pixel comes from one copy or the other (bar a thin feather), never a half-and-half ghost across the band."""
    size = 512
    image = Image.new("RGB", (size, size), (0, 0, 0))
    image.paste((255, 255, 255), (0, 0, size // 2, size))  # black and white halves: the shifted copy is the opposite colour in places
    grey = modular.make_seamless(image).convert("L")
    mid_tones = sum(1 for value in grey.tobytes() if 60 < value < 195)
    assert mid_tones / (size * size) < 0.05


def test_normal_map_points_away_from_the_slope_and_tiles() -> None:
    # Height rising to the right: the surface leans left, so red is below the middle. Blue stays high.
    size = 64
    height = Image.new("L", (size, size))
    height.putdata([min(255, (i % size) * 4) for i in range(size * size)])
    red, green, blue = modular._normal_map(height).getpixel((20, 20))
    assert red < 122 and abs(green - 127) <= 2 and blue > 180
    # Height rising down the picture: "up the picture" faces downhill, so green is above the middle (OpenGL style).
    falling = height.transpose(Image.ROTATE_270).transpose(Image.FLIP_LEFT_RIGHT)
    assert modular._normal_map(falling).getpixel((20, 20))[1] > 132


def test_build_material_makes_every_map(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    rel = _material(ws)
    paths = modular.material_paths(ws / rel)
    for path in paths.values():
        assert Image.open(path).size == (modular.TEXTURE_SIZE, modular.TEXTURE_SIZE)
    assert _edge_jump(Image.open(paths["albedo"])) < 12  # the picture's own left and right were 255 apart
    occlusion, roughness, metal = Image.open(paths["orm"]).split()
    assert metal.getextrema() == (0, 0) and roughness.getextrema()[0] >= 150
    meta = asset_studio.describe_asset(ws, rel)["meta"]
    assert meta["material_source"] == meta["winner"]
    assert modular.list_materials(ws) == [{"path": rel, "title": "Stone", "built": True}]


def test_material_needs_a_picked_image_and_the_right_kind(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _asset(ws, "a.md", "kind: material\nprompt: moss")
    _asset(ws, "b.md", "kind: prop\nprompt: crate")
    with pytest.raises(ValueError, match="Pick an image"):
        modular.build_material(ws, "a.md")
    with pytest.raises(ValueError, match="Only a material"):
        modular.build_material(ws, "b.md")


def test_material_prompt_asks_for_a_flat_surface(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _asset(ws, "stone.md", "kind: material\nprompt: wet flagstone")
    request = asset_studio.build_request(ws, "stone.md", "candidates")
    assert asset_studio.MATERIAL_RULE in request["prompt"]
    assert request["settings"]["aspect_ratio"] == "1:1" and request["settings"]["transparent_background"] is False


def test_kit_is_valid_glb_with_exact_sizes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _asset(ws, "kits/crypt.md", "kind: kit\nprompt: crypt\nkit_cell: 3\nkit_height: 4\nkit_thickness: 0.25")
    built = modular.build_kit(ws, "kits/crypt.md")
    doc, binary = _read_glb(ws / built["kit"])
    assert {n["name"] for n in doc["nodes"]} == set(built["pieces"]) >= {"floor", "ceiling", "wall", "doorway", "window", "corner_inner", "corner_outer", "pillar", "stairs"}

    for accessor in doc["accessors"]:
        view = doc["bufferViews"][accessor["bufferView"]]
        size = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[accessor["type"]] * 4
        assert view["byteOffset"] % 4 == 0 and view["byteOffset"] + view["byteLength"] <= len(binary)
        assert accessor["count"] * size == view["byteLength"]

    pieces = {p["name"]: p for p in json.loads((ws / built["manifest"]).read_text(encoding="utf-8"))["pieces"]}
    wall = pieces["wall"]["bounds"]
    assert wall["min"] == [-1.5, 0, -1.5] and wall["max"] == [1.5, 4, -1.25]
    assert pieces["floor"]["bounds"]["max"][1] == 0 and pieces["floor"]["bounds"]["min"][1] == -0.25
    assert pieces["ceiling"]["bounds"]["min"][1] == 4
    assert pieces["stairs"]["bounds"]["min"][2] == -4.5 and pieces["stairs"]["bounds"]["max"][2] == 1.5 and pieces["stairs"]["bounds"]["max"][1] == 4
    for name in ("doorway", "window", "corner_inner"):
        assert pieces[name]["bounds"]["min"][0] == -1.5 and pieces[name]["bounds"]["max"][0] == 1.5


def test_faces_wind_outwards_and_tangents_agree() -> None:
    geo = modular._Geometry({"wall": 2.0})
    geo.box("wall", (-1, 1), (0, 3), (-1, 1))
    data = geo.slots["wall"]
    for i in range(0, len(data["i"]), 6):
        a, b, c = (data["p"][data["i"][i + k]] for k in range(3))
        ab = [b[k] - a[k] for k in range(3)]
        ac = [c[k] - a[k] for k in range(3)]
        cross = (ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2], ab[0] * ac[1] - ab[1] * ac[0])
        normal = data["n"][data["i"][i]]
        assert sum(cross[k] * normal[k] for k in range(3)) > 0
        assert sum(data["t"][data["i"][i]][k] * normal[k] for k in range(3)) == 0


def test_neighbouring_cells_continue_the_texture() -> None:
    """A surface runs on across cells: one wall's right edge has the same U as the next wall's left edge, modulo a repeat."""
    tiles = {slot: 2.0 for slot in modular.SLOTS}
    wall = next(p for p in modular._pieces(4.0, 3.0, 0.2, tiles) if p["name"] == "wall")["geo"].slots["wall"]
    us = [uv[0] for uv, normal in zip(wall["uv"], wall["n"]) if normal == (0, 0, 1)]
    assert (max(us) - min(us)) == pytest.approx(2.0)  # the cell is exactly two repeats


def _rotate(point: tuple[float, float], degrees: int) -> tuple[float, float]:
    angle = math.radians(degrees)
    x, z = point
    return x * math.cos(angle) + z * math.sin(angle), -x * math.sin(angle) + z * math.cos(angle)


def test_sample_room_walls_close_the_room(tmp_path: Path) -> None:
    """The kit's rotation rule, applied to the sample, puts a wall on every outside edge and on no inside one."""
    ws = tmp_path / "ws"
    _asset(ws, "crypt.md", "kind: kit\nprompt: crypt")
    built = modular.build_kit(ws, "crypt.md")
    manifest = json.loads((ws / built["manifest"]).read_text(encoding="utf-8"))
    sides = {p["name"]: p["sides"] for p in manifest["pieces"]}
    offset = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
    edges: dict[tuple[int, int, str], str] = {}
    for placed in manifest["sample"]:
        for side, content in sides[placed["piece"]].items():
            if content == "open":
                continue
            turned = _rotate(offset[side], placed["rotation_y"])
            facing = min(offset, key=lambda name: abs(offset[name][0] - turned[0]) + abs(offset[name][1] - turned[1]))
            edges[(*placed["cell"], facing)] = content
    outside = {(x, 0, "north") for x in range(3)} | {(x, 2, "south") for x in range(3)} | {(0, z, "west") for z in range(3)} | {(2, z, "east") for z in range(3)}
    assert set(edges) == outside
    assert edges[(1, 2, "south")] == "door" and edges[(2, 1, "east")] == "window"


def test_sample_room_file_places_the_shared_pieces(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _asset(ws, "crypt.md", "kind: kit\nprompt: crypt")
    built = modular.build_kit(ws, "crypt.md")
    doc, _ = _read_glb(ws / built["sample"])
    floors = [n for n in doc["nodes"] if doc["meshes"][n["mesh"]]["name"] == "floor"]
    assert len(floors) == 9 and len({n["mesh"] for n in floors}) == 1
    assert sorted({n["translation"][0] for n in floors}) == [-2, 0, 2]
    west = next(n for n in doc["nodes"] if n["name"].startswith("wall_") and "rotation" in n)
    assert west["rotation"] == pytest.approx([0, math.sin(math.pi / 4), 0, math.cos(math.pi / 4)])


def test_kit_uses_materials_and_shares_their_textures(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    stone = _material(ws, "stone", "\ntile_m: 1.5")
    _asset(ws, "crypt.md", f"kind: kit\nprompt: crypt\nkit_cell: 3\nkit_wall: {stone}\nkit_floor: {stone}")
    built = modular.build_kit(ws, "crypt.md")
    doc, _ = _read_glb(ws / built["kit"])
    names = [m["name"] for m in doc["materials"]]
    assert names.count("stone") == 1 and "blockout_ceiling" in names
    assert len(doc["images"]) == 3  # albedo, normal and ORM once, however many pieces use them
    manifest = json.loads((ws / built["manifest"]).read_text(encoding="utf-8"))
    assert manifest["materials"]["wall"] == {"asset": stone, "tile_metres": 1.5}
    assert manifest["materials"]["ceiling"]["asset"] is None


def test_tile_size_snaps_to_whole_repeats(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    stone = _material(ws, "stone", "\ntile_m: 1.7")
    _, tile, _ = modular._kit_material(ws, stone, "wall", 4.0)
    assert tile == 2.0


def test_kit_refuses_bad_settings_and_unfinished_materials(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _asset(ws, "mat.md", "kind: material\nprompt: moss")
    _asset(ws, "a.md", "kind: kit\nprompt: a\nkit_wall: mat.md")
    _asset(ws, "b.md", "kind: kit\nprompt: b\nkit_cell: 900")
    _asset(ws, "c.md", "kind: kit\nprompt: c\nkit_cell: 1\nkit_thickness: 0.5")
    _asset(ws, "d.md", "kind: kit\nprompt: d\nkit_cell: wide")
    with pytest.raises(ValueError, match="Make tileable"):
        modular.build_kit(ws, "a.md")
    with pytest.raises(ValueError, match="between"):
        modular.build_kit(ws, "b.md")
    with pytest.raises(ValueError, match="quarter"):
        modular.build_kit(ws, "c.md")
    with pytest.raises(ValueError, match="number"):
        modular.build_kit(ws, "d.md")


def test_rebuild_replaces_files_and_drops_old_previews(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _asset(ws, "crypt.md", "kind: kit\nprompt: crypt")
    built = modular.build_kit(ws, "crypt.md")
    thumb = (ws / built["kit"]).with_name("crypt_thumb.png")
    thumb.write_bytes(b"old")
    modular.build_kit(ws, "crypt.md")
    assert not thumb.exists()
    assert sorted(p.name for p in (ws / "crypt" / "3d").iterdir()) == ["crypt-sample-room.glb", "crypt.glb", "kit.json"]


def test_kits_are_never_sent_to_generation(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _asset(ws, "crypt.md", "kind: kit\nprompt: crypt walls")
    _asset(ws, "stone.md", "kind: material\nprompt: stone")
    assert asset_studio.missing_assets(ws) == ["stone.md"]
    with pytest.raises(ValueError, match="Build kit"):
        asset_studio.build_request(ws, "crypt.md", "candidates")


def test_index_and_export_include_kits_and_maps(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    stone = _material(ws)
    _asset(ws, "kits/crypt.md", f"kind: kit\nprompt: crypt\nkit_wall: {stone}")
    modular.build_kit(ws, "kits/crypt.md")
    text = asset_index.render(ws)
    assert "`ws/kits/crypt/3d/crypt.glb`" in text and "`ws/kits/crypt/3d/kit.json`" in text
    assert "`ws/materials/stone/textures/`" in text and "not made yet" not in text
    names = zipfile.ZipFile(io.BytesIO(game_tools.export_zip(ws, "", "godot")[0])).namelist()
    assert {"assets/kits/crypt/models/crypt.glb", "assets/kits/crypt/models/kit.json", "assets/materials/stone/textures/stone-normal.png"} <= set(names)
    # The file names kit.json points at sit right beside it in the export.
    manifest = json.loads((ws / "kits" / "crypt" / "3d" / "kit.json").read_text(encoding="utf-8"))
    assert f"assets/kits/crypt/models/{manifest['file']}" in names

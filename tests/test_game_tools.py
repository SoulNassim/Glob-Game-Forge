"""Game tools: dropped models, splitting, icons, turnaround/rig requests and engine export."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from forge import asset_studio, game_tools


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "chars").mkdir(parents=True)
    (ws / "chars" / "duo.md").write_text("---\nkind: character\nprompt: Two scouts\n---\n# Duo\n", encoding="utf-8")
    sheet = Image.new("RGB", (400, 200), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    draw.rectangle((40, 30, 120, 180), fill=(40, 30, 20))
    draw.rectangle((260, 20, 350, 185), fill=(60, 60, 70))
    (ws / "chars" / "duo" / "candidates").mkdir(parents=True)
    sheet.save(ws / "chars" / "duo" / "candidates" / "sheet.png")
    return ws


def test_detect_and_split_parts(tmp_path):
    ws = _workspace(tmp_path)
    boxes = game_tools.detect_parts(ws, "chars/duo/candidates/sheet.png")
    assert len(boxes) == 2 and boxes[0]["x"] < boxes[1]["x"]
    parts = game_tools.split_image(ws, "chars/duo.md", "chars/duo/candidates/sheet.png", boxes)
    assert [Path(p).parent.name for p in parts] == ["parts", "parts"]
    named = [{**boxes[0], "name": "Left Scout"}, {**boxes[1], "name": "Right Scout"}]
    assets = game_tools.split_image(ws, "chars/duo.md", "chars/duo/candidates/sheet.png", named, as_assets=True)
    assert assets == ["chars/left-scout.md", "chars/right-scout.md"]
    left = asset_studio.describe_asset(ws, "chars/left-scout.md")
    assert left["kind"] == "character" and (ws / left["winner"]).is_file()


def test_level_prop_sheet_is_cut_into_props(tmp_path):
    """Level kit: one sheet of props on a flat background becomes one cut-out prop asset each, in the level's folder."""
    ws = tmp_path / "ws"
    (ws / "levels" / "crypt" / "sheets").mkdir(parents=True)
    (ws / "levels" / "crypt.md").write_text("---\nkind: level\nprompt: A mossy crypt\n---\n# Crypt\n", encoding="utf-8")
    sheet = Image.new("RGB", (600, 400), (229, 229, 229))
    draw = ImageDraw.Draw(sheet)
    for col in range(3):
        for row in range(2):
            x, y = 40 + col * 200, 40 + row * 190
            draw.rectangle((x, y, x + 110, y + 120 - row * 40), fill=(90 + col * 40, 60, 40))
    draw.rectangle((65, 65, 125, 125), fill=(229, 229, 229))  # a light window inside the first prop stays in it
    sheet.save(ws / "levels" / "crypt" / "sheets" / "sheet.png")

    boxes = game_tools.detect_sheet_items(ws, "levels/crypt/sheets/sheet.png")
    assert len(boxes) == 6
    assert boxes[0]["y"] < boxes[3]["y"] and boxes[0]["x"] < boxes[1]["x"] < boxes[2]["x"]  # row by row
    props = game_tools.split_image(ws, "levels/crypt.md", "levels/crypt/sheets/sheet.png", [{**b, "name": f"Thing {i}"} for i, b in enumerate(boxes, 1)], as_assets=True)
    assert props[0] == "levels/crypt-props/thing-1.md" and len(props) == 6
    first = asset_studio.describe_asset(ws, props[0])
    assert first["kind"] == "prop"
    cut = Image.open(ws / first["winner"])
    assert cut.mode == "RGBA" and cut.getpixel((0, 0))[3] == 255  # trimmed to the prop, background gone around it
    assert cut.getpixel((cut.width // 2 - 10, cut.height // 2 - 10))[3] == 255  # the window inside is not a hole


def test_prop_sheet_request_uses_the_level_image(tmp_path):
    ws = tmp_path / "ws"
    (ws / "levels" / "crypt" / "candidates").mkdir(parents=True)
    Image.new("RGB", (64, 36), (10, 10, 10)).save(ws / "levels" / "crypt" / "candidates" / "a.png")
    (ws / "levels" / "crypt.md").write_text("---\nkind: level\nprompt: A crypt\nwinner: levels/crypt/candidates/a.png\n---\n# Crypt\n", encoding="utf-8")
    request = asset_studio.build_request(ws, "levels/crypt.md", "prop_sheet", instruction="barrel, torch")
    assert request["count"] == 1 and request["references"][0]["path"] == "levels/crypt/candidates/a.png"
    assert "barrel, torch" in request["prompt"] and request["settings"]["transparent_background"] is False


def test_icons_are_square_and_transparent(tmp_path):
    ws = _workspace(tmp_path)
    icons = game_tools.make_icons(ws, "chars/duo.md", "chars/duo/candidates/sheet.png", [64, 128])
    image = Image.open(ws / icons[0])
    assert image.size == (64, 64) and image.mode == "RGBA" and image.getpixel((0, 0))[3] == 0
    assert [o["path"] for o in asset_studio.describe_asset(ws, "chars/duo.md")["outputs"]["icons"]]


def test_dropped_model_and_engine_export(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(ValueError):
        game_tools.save_model(ws, "chars/duo.md", "notes.txt", b"x")
    saved = game_tools.save_model(ws, "chars/duo.md", "Hero Mesh.glb", b"glTF-binary")
    assert saved == "chars/duo/3d/Hero-Mesh.glb"
    assert asset_studio.describe_asset(ws, "chars/duo.md")["outputs"]["3d"][0]["type"] == "model"
    game_tools.save_model(ws, "chars/duo.md", "hero.fbx", b"fbx")
    asset_studio.update_asset(ws, "chars/duo.md", {"winner": "chars/duo/candidates/sheet.png"})
    for engine, root, model in (("godot", "assets/", "Hero-Mesh.glb"), ("unity", "Assets/Art/", "hero.fbx"), ("unreal", "Content/Art/", "hero.fbx")):
        data, name = game_tools.export_zip(ws, "chars", engine)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert name == f"chars-{engine}.zip"
        assert f"{root}chars/duo/models/{model}" in names and f"{root}chars/duo/concept.png" in names
    godot = zipfile.ZipFile(io.BytesIO(game_tools.export_zip(ws, "chars/duo.md", "godot")[0])).namelist()
    assert not any(n.endswith(".fbx") for n in godot)


def test_turnaround_and_rig_requests(tmp_path):
    ws = _workspace(tmp_path)
    asset_studio.update_asset(ws, "chars/duo.md", {"winner": "chars/duo/candidates/sheet.png"})
    views = asset_studio.build_request(ws, "chars/duo.md", "views")
    assert "{view}" in views["prompt"] and views["use_bible"] is False and views["references"][0]["role"] == "source"
    with pytest.raises(ValueError, match="GLB"):
        asset_studio.build_request(ws, "chars/duo.md", "rig")
    game_tools.save_model(ws, "chars/duo.md", "hero.glb", b"glTF")
    rig = asset_studio.build_request(ws, "chars/duo.md", "rig")
    assert rig["references"][0]["path"] == "chars/duo/3d/hero.glb" and rig["credits"] == game_tools.RIG_CREDITS
    with pytest.raises(ValueError, match="Rig"):
        asset_studio.build_request(ws, "chars/duo.md", "animate")
    (ws / "chars" / "duo" / "views").mkdir()
    Image.new("RGB", (8, 8)).save(ws / "chars" / "duo" / "views" / "side.png")
    make = asset_studio.build_request(ws, "chars/duo.md", "make_3d")
    assert [r["role"] for r in make["references"]] == ["source", "view"]


def _figure(ws: Path) -> str:
    """A T-pose stick figure: head, torso, arms out to the sides, two legs."""
    image = Image.new("RGB", (300, 400), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((130, 10, 170, 60), fill=(50, 40, 30))
    draw.rectangle((115, 60, 185, 200), fill=(60, 50, 40))
    draw.rectangle((20, 70, 115, 95), fill=(60, 50, 40))
    draw.rectangle((185, 70, 280, 95), fill=(60, 50, 40))
    draw.rectangle((120, 200, 147, 390), fill=(40, 40, 40))
    draw.rectangle((153, 200, 180, 390), fill=(40, 40, 40))
    image.save(ws / "chars" / "duo" / "candidates" / "figure.png")
    return "chars/duo/candidates/figure.png"


def test_body_parts_from_glob_and_fallback(tmp_path):
    import asyncio

    ws = _workspace(tmp_path)
    figure = _figure(ws)

    async def answers(**kwargs):
        assert kwargs["images_b64"][0][0] == "image/png"
        return {"text": 'Sure: {"parts": [{"name": "head", "x": 0.4, "y": 0, "w": 0.2, "h": 0.15}, {"name": "torso", "x": 0.38, "y": 0.15, "w": 0.24, "h": 0.35}, {"name": "sword", "x": 0.9, "y": 0.2, "w": 0.5, "h": 0.1}, {"name": "", "x": 0, "y": 0, "w": 1, "h": 1}]}', "model": "test-vision"}

    async def fails(**kwargs):
        raise RuntimeError("no model")

    glob = asyncio.run(game_tools.guess_body_parts(ws, figure, answers))
    assert glob["source"] == "glob" and [b["name"] for b in glob["boxes"]] == ["head", "torso", "sword"]
    assert glob["boxes"][2]["x"] + glob["boxes"][2]["w"] <= 1.0
    fallback = asyncio.run(game_tools.guess_body_parts(ws, figure, fails))
    names = {b["name"]: b for b in fallback["boxes"]}
    assert fallback["source"] == "template" and set(names) == set(game_tools.BODY_PARTS)
    assert names["head"]["y"] < names["torso"]["y"] < names["left leg"]["y"]
    assert names["right arm"]["x"] < names["torso"]["x"] < names["left arm"]["x"]


def test_rig_refuses_models_over_meshy_triangle_limit(tmp_path):
    import asyncio
    import json
    import struct

    ws = _workspace(tmp_path)
    doc = json.dumps({"accessors": [{"count": 1_200_000}], "meshes": [{"primitives": [{"indices": 0, "attributes": {"POSITION": 0}}]}]}).encode()
    doc += b" " * (-len(doc) % 4)
    glb = b"glTF" + struct.pack("<II", 2, 20 + len(doc)) + struct.pack("<I", len(doc)) + b"JSON" + doc
    model = game_tools.save_model(ws, "chars/duo.md", "dense.glb", glb)
    assert game_tools.glb_triangles(ws / model) == 400_000
    with pytest.raises(ValueError, match="400,000 triangles"):
        asyncio.run(game_tools.rig_model(ws, "chars/duo.md", model))


def test_prop_sheet_uses_the_style_bible_and_the_project_dimension(tmp_path):
    ws = tmp_path / "ws"
    (ws / "levels" / "crypt" / "candidates").mkdir(parents=True)
    (ws / "pillars").mkdir()
    (ws / "pillars" / "palette.md").write_text("# Palette\n\nWarm ochre and moss only.\n", encoding="utf-8")
    Image.new("RGB", (64, 36), (10, 10, 10)).save(ws / "levels" / "crypt" / "candidates" / "a.png")
    (ws / "levels" / "crypt.md").write_text("---\nkind: level\nprompt: A crypt\nwinner: levels/crypt/candidates/a.png\n---\n# Crypt\n", encoding="utf-8")

    request = asset_studio.build_request(ws, "levels/crypt.md", "prop_sheet")
    assert request["use_bible"] is True and request["bible"] and "Warm ochre" in request["bible"][0]["text"]
    assert "as for a 3D model" in request["prompt"]  # no project.json: 3D is the default

    (tmp_path / "project.json").write_text('{"bible": {"project_dimension": "2d"}}', encoding="utf-8")
    flat = asset_studio.build_request(ws, "levels/crypt.md", "prop_sheet")
    assert "drawn as flat 2D game sprites in the same camera view as the level picture" in flat["prompt"]
    assert "as for a 3D model" not in flat["prompt"]


def test_cut_props_are_named_in_title_case_with_a_slug_picture(tmp_path):
    ws = _workspace(tmp_path)
    boxes = [{"x": 0.05, "y": 0.1, "w": 0.3, "h": 0.7, "name": "wooden barrel"}]
    made = game_tools.split_image(ws, "chars/duo.md", "chars/duo/candidates/sheet.png", boxes, as_assets=True)
    assert made == ["chars/wooden-barrel.md"]
    asset = asset_studio.describe_asset(ws, "chars/wooden-barrel.md")
    assert asset["winner"] == "chars/wooden-barrel/candidates/wooden-barrel.png"
    assert (ws / asset["winner"]).is_file()
    assert "# Wooden Barrel" in (ws / "chars" / "wooden-barrel.md").read_text(encoding="utf-8")
    assert asset["prompt"] == "Wooden Barrel"

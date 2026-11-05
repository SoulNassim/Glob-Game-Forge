"""One press from a level picture to an asset pack: what it pays for, what it does for free, and what it never repeats."""

from __future__ import annotations

import io
import json
import random
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from forge import asset_index, asset_studio, game_tools, level_pack, modular


def _surface_picture(path: Path) -> None:
    rng = random.Random(5)
    image = Image.new("RGB", (256, 256))
    image.putdata([(rng.randrange(70, 130),) * 3 for _ in range(256 * 256)])
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _sheet_picture(path: Path, count: int = 3) -> None:
    image = Image.new("RGB", (600, 200), (229, 229, 229))
    draw = ImageDraw.Draw(image)
    for i in range(count):
        draw.rectangle((30 + i * 190, 40, 150 + i * 190, 160), fill=(90, 60, 40))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


class Fake:
    """Stands in for the paid image jobs: writes a picture where the real job would, and counts every call."""

    def __init__(self, ws: Path) -> None:
        self.ws = ws
        self.calls: list[dict] = []
        self.fail: set[str] = set()

    async def run(self, request: dict) -> dict:
        self.calls.append(request)
        action, asset = request["action"], request["asset"]
        if asset in self.fail or action in self.fail:
            return {"status": "failed", "error": "out of credits"}
        base = asset[:-3]
        if action == "candidates":
            rel = f"{base}/candidates/pick-{len(self.calls)}.png"
            _surface_picture(self.ws / rel)
        else:
            tag = "-trim" if action == "trim_sheet" else ""
            rel = f"{base}/sheets/{Path(base).name}{tag}-1-1.png"
            _sheet_picture(self.ws / rel)
        return {"status": "done", "outputs": [rel]}


async def _no_names(**_: object) -> dict:
    raise RuntimeError("no chat model here")


@pytest.fixture
def level(tmp_path: Path) -> tuple[Path, str, Fake]:
    ws = tmp_path / "ws"
    picture = ws / "levels" / "plant" / "candidates" / "pick.png"
    _surface_picture(picture)
    (ws / "levels" / "plant.md").write_text("---\nkind: level\nprompt: A frozen plant\nwinner: levels/plant/candidates/pick.png\n---\n# Cold Plant\n", encoding="utf-8")
    return ws, "levels/plant.md", Fake(ws)


ALL = ["wall", "wall-damaged", "wall-detail", "floor", "floor-worn", "ceiling", "props", "trim"]


async def test_one_press_makes_the_whole_pack(level: tuple) -> None:
    ws, rel, fake = level
    result = await level_pack.run_pack(ws, rel, ALL, fake.run, _no_names)
    assert result["notes"] == [] and result["kit"] == "levels/plant-pack/plant-kit.md"
    assert len(fake.calls) == 8

    manifest = json.loads((ws / "levels/plant-pack/plant-kit/3d/kit.json").read_text(encoding="utf-8"))
    assert manifest["materials"]["wall"]["asset"] == "levels/plant-pack/materials/wall.md"
    assert manifest["materials"]["floor_2"]["asset"] == "levels/plant-pack/materials/floor-worn.md"
    names = {p["name"] for p in manifest["pieces"]}
    assert {"wall_2", "wall_3", "floor_2", "doorway"} <= names
    assert manifest["also_in_this_pack"]["props"] == "levels/plant-props"

    status = level_pack.status(ws, rel)
    assert all(item["done"] for item in status["items"]) and status["kit"] and len(status["surfaces"]) == 6
    groups = [p["group"] for p in status["pickable"]]
    assert groups.count("Props") == 3 and groups.count("Pillars, arches and frames") == 3


async def test_surfaces_are_drawn_from_the_level_picture(level: tuple) -> None:
    ws, rel, fake = level
    await level_pack.run_pack(ws, rel, ["wall"], fake.run, _no_names)
    request = fake.calls[0]
    assert request["references"][0] == {"path": "levels/plant/candidates/pick.png", "source": "Level picture", "role": "source"}
    assert level_pack.SURFACE_RULE in request["prompt"] and asset_studio.MATERIAL_RULE in request["prompt"] and "A frozen plant" in request["prompt"]
    assert request["settings"]["transparent_background"] is False


async def test_pressing_again_never_pays_twice(level: tuple) -> None:
    ws, rel, fake = level
    await level_pack.run_pack(ws, rel, ALL, fake.run, _no_names)
    paid = len(fake.calls)
    await level_pack.run_pack(ws, rel, ALL, fake.run, _no_names)
    assert len(fake.calls) == paid


async def test_a_failed_image_keeps_the_rest_and_can_be_finished(level: tuple) -> None:
    ws, rel, fake = level
    fake.fail = {"levels/plant-pack/materials/wall-damaged.md", "trim_sheet"}
    result = await level_pack.run_pack(ws, rel, ALL, fake.run, _no_names)
    assert len(result["notes"]) == 2 and result["kit"]
    meta = asset_studio.describe_asset(ws, rel)["meta"]
    assert "out of credits" in meta["pack_note"]
    done = {i["id"]: i["done"] for i in level_pack.status(ws, rel)["items"]}
    assert done["wall"] and not done["wall-damaged"] and not done["trim"] and done["props"]

    paid = len(fake.calls)
    fake.fail = set()
    result = await level_pack.run_pack(ws, rel, ALL, fake.run, _no_names)
    assert len(fake.calls) == paid + 2 and result["notes"] == []
    assert "pack_note" not in asset_studio.describe_asset(ws, rel)["meta"]
    assert "wall_2" in {p["name"] for p in json.loads((ws / "levels/plant-pack/plant-kit/3d/kit.json").read_text(encoding="utf-8"))["pieces"]}


async def test_a_tick_list_makes_only_what_is_ticked(level: tuple) -> None:
    ws, rel, fake = level
    result = await level_pack.run_pack(ws, rel, ["props"], fake.run, _no_names)
    assert [c["action"] for c in fake.calls] == ["prop_sheet"] and result["kit"] == ""
    assert not (ws / "levels/plant-pack/plant-kit.md").exists()


async def test_rebuilding_keeps_the_users_kit_settings(level: tuple) -> None:
    ws, rel, fake = level
    await level_pack.run_pack(ws, rel, ["wall"], fake.run, _no_names)
    asset_studio.update_asset(ws, "levels/plant-pack/plant-kit.md", {"kit_cell": 4})
    await level_pack.run_pack(ws, rel, ["floor"], fake.run, _no_names)
    manifest = json.loads((ws / "levels/plant-pack/plant-kit/3d/kit.json").read_text(encoding="utf-8"))
    assert manifest["grid"]["cell"] == 4 and manifest["materials"]["floor"]["asset"]


async def test_a_trim_sheet_cuts_into_the_pack_not_the_props(level: tuple) -> None:
    ws, rel, fake = level
    await level_pack.run_pack(ws, rel, ["trim"], fake.run, _no_names)
    meta = asset_studio.describe_asset(ws, rel)["meta"]
    assert meta["trim_cut"] == 3 and "props_cut" not in meta
    assert len(list((ws / "levels/plant-pack/trim").glob("*.md"))) == 3 and not (ws / "levels/plant-props").exists()
    assert game_tools.is_trim_sheet("plant-trim-20261105-1.png") and not game_tools.is_trim_sheet("plant-20261105-1.png")


def test_trim_sheet_request_uses_the_level_picture(level: tuple) -> None:
    ws, rel, _ = level
    request = asset_studio.build_request(ws, rel, "trim_sheet")
    assert request["references"][0]["path"] == "levels/plant/candidates/pick.png" and asset_studio.ACTION_FOLDER["trim_sheet"] == "sheets"


def test_a_2d_project_offers_props_only(level: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    ws, rel, _ = level
    monkeypatch.setattr(level_pack, "project_dimension", lambda _ws: "2d")
    assert [i["id"] for i in level_pack.plan(ws, rel)] == ["props"]


async def test_only_levels_become_packs_and_a_picture_is_needed(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "crate.md").write_text("---\nkind: prop\nprompt: crate\n---\n# Crate\n", encoding="utf-8")
    (ws / "blank.md").write_text("---\nkind: level\nprompt: void\n---\n# Blank\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Only a level"):
        level_pack.plan(ws, "crate.md")
    with pytest.raises(ValueError, match="level picture"):
        await level_pack.run_pack(ws, "blank.md", ALL, Fake(ws).run, _no_names)


async def test_3d_requests_stay_game_ready(level: tuple) -> None:
    ws, rel, fake = level
    await level_pack.run_pack(ws, rel, ["props"], fake.run, _no_names)
    prop = level_pack.status(ws, rel)["pickable"][0]["asset"]
    request = level_pack.make_3d_request(ws, prop)
    assert request["action"] == "make_3d" and request["settings"]["should_remesh"] is True and request["settings"]["target_polycount"] == level_pack.TRIM_MESH_POLYCOUNT


async def test_exporting_a_level_takes_its_pack_along(level: tuple) -> None:
    ws, rel, fake = level
    await level_pack.run_pack(ws, rel, ["wall", "props"], fake.run, _no_names)
    names = zipfile.ZipFile(io.BytesIO(game_tools.export_zip(ws, rel, "godot")[0])).namelist()
    assert any(n.endswith("plant-kit/models/kit.json") for n in names)
    assert any(n.endswith("materials/wall/textures/wall-normal.png") for n in names)
    assert any("plant-props/" in n for n in names)
    text = asset_index.render(ws)
    assert "kit.json" in text


async def test_levels_list_and_where_new_ones_go(level: tuple, tmp_path: Path) -> None:
    ws, rel, fake = level
    await level_pack.run_pack(ws, rel, ["wall"], fake.run, _no_names)
    levels = level_pack.list_levels(ws)
    assert levels == [{"path": rel, "title": "Cold Plant", "image": "levels/plant/candidates/pick.png", "props": 0, "kit": True}]
    assert level_pack.new_level_folder(ws) == "levels"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert level_pack.new_level_folder(empty) == "levels"
    assert modular.list_materials(ws)[0]["title"] == "Main wall"


@pytest.mark.parametrize(("request_in", "expected"), [
    ({"model": "svc:elevenlabs/gemini-nano-banana-2.1"}, ""),
    ({"model": "svc:meshy/image-to-3d", "action": "make_3d"}, ""),
    ({"model": "svc:elevenlabs/sfx", "audio": True}, "elevenlabs_audio:sfx"),
    ({"model": "svc:elevenlabs/tts", "audio": True}, "elevenlabs_audio:tts"),
    ({"model": "svc:elevenlabs/music", "audio": True}, "stability_audio:music"),
])
def test_demo_mode_never_reaches_a_signed_in_service(request_in: dict, expected: str) -> None:
    assert asset_studio.demo_safe_request(request_in, True)["model"] == expected
    assert asset_studio.demo_safe_request(request_in, False) == request_in


def test_demo_mode_leaves_other_generators_alone() -> None:
    request = {"model": "gemini:flash"}
    assert asset_studio.demo_safe_request(request, True) == request

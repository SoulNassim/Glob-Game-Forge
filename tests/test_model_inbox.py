"""The watched folder: downloaded models become assets, once each, and the source files are left alone."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forge import asset_index, asset_studio, model_inbox
from forge.config import load_config
from forge.server.app import app

GLB = b"glTF" + b"\x00" * 12


def inbox() -> model_inbox.ModelInbox:
    async def notify(project: str, assets: list[str]) -> None:
        return None

    return model_inbox.ModelInbox(lambda project: Path(project), notify)


def dirs(tmp_path: Path) -> tuple[Path, Path]:
    downloads, ws = tmp_path / "downloads", tmp_path / "ws"
    downloads.mkdir()
    ws.mkdir()
    return downloads, ws


def test_new_model_becomes_an_asset_listed_for_the_game_and_source_stays(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    source = downloads / "hooded_rogue.glb"
    source.write_bytes(GLB)
    box = inbox()

    assert box.scan(ws, downloads, 0) == []  # first look: it may still be downloading
    assert box.scan(ws, downloads, 0) == ["studio-downloads/hooded-rogue.md"]

    assert source.read_bytes() == GLB
    described = asset_studio.describe_asset(ws, "studio-downloads/hooded-rogue.md")
    assert described["is_asset"] and described["kind"] == "prop"
    assert described["outputs"]["3d"][0]["path"] == "studio-downloads/hooded-rogue/3d/hooded-rogue.glb"
    assert "studio-downloads/hooded-rogue/3d/hooded-rogue.glb" in asset_index.render(ws)


def test_a_file_still_growing_waits(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    source = downloads / "rogue.glb"
    source.write_bytes(GLB)
    box = inbox()
    box.scan(ws, downloads, 0)
    source.write_bytes(GLB + b"\x00" * 8)
    assert box.scan(ws, downloads, 0) == []
    assert box.scan(ws, downloads, 0) == ["studio-downloads/rogue.md"]


def test_older_files_and_other_types_are_ignored(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    old = downloads / "old.glb"
    old.write_bytes(GLB)
    os.utime(old, (1000, 1000))
    (downloads / "notes.pdf").write_bytes(b"%PDF")
    (downloads / "partial.glb.crdownload").write_bytes(GLB)
    box = inbox()
    for _ in range(3):
        assert box.scan(ws, downloads, 2000) == []
    assert not (ws / "studio-downloads").exists()


def test_a_redownload_is_not_added_twice_even_after_a_restart(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    (downloads / "rogue.glb").write_bytes(GLB)
    box = inbox()
    box.scan(ws, downloads, 0)
    box.scan(ws, downloads, 0)

    (downloads / "rogue (1).glb").write_bytes(GLB)
    restarted = inbox()
    for _ in range(3):
        assert restarted.scan(ws, downloads, 0) == []


def test_a_different_model_with_the_same_name_gets_its_own_asset(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    first = downloads / "rogue.glb"
    first.write_bytes(GLB)
    second = downloads / "rogue (1).glb"
    second.write_bytes(GLB + b"\x01")
    box = inbox()
    box.scan(ws, downloads, 0)
    assert box.scan(ws, downloads, 0) == ["studio-downloads/rogue.md", "studio-downloads/rogue-2.md"]


def test_a_broken_glb_is_reported_once_and_not_imported(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    (downloads / "broken.glb").write_bytes(b"<html>not a model</html>")
    box = inbox()
    box.scan(ws, downloads, 0)
    assert box.scan(ws, downloads, 0) == []
    assert "broken.glb" in box.error
    assert not (ws / "studio-downloads" / "broken.md").exists()


def test_settings_switch_it_on_and_off(tmp_path: Path) -> None:
    client = TestClient(app, base_url="http://127.0.0.1")
    project = client.post("/api/projects", json={"name": "inbox_game"}).json()["project"]
    folder = tmp_path / "downloads"
    folder.mkdir()
    assert client.put("/api/model-inbox", json={"enabled": True, "folder": str(tmp_path / "nope"), "project": project}).status_code == 400
    assert client.put("/api/model-inbox", json={"enabled": True, "folder": str(folder), "project": "no_such_project"}).status_code == 400

    before = time.time()
    status = client.put("/api/model-inbox", json={"enabled": True, "folder": str(folder), "project": project}).json()
    assert status["enabled"] and status["folder"] == str(folder) and status["project"] == project
    assert load_config().model_inbox_since >= before

    assert client.put("/api/model-inbox", json={"enabled": False}).json()["enabled"] is False
    assert load_config().model_inbox_dir == ""


def waiting_asset(ws: Path, name: str, kind: str = "prop", folder: str = "props") -> str:
    candidates = ws / folder / name / "candidates"
    candidates.mkdir(parents=True)
    (candidates / "a.png").write_bytes(b"\x89PNG")
    (ws / folder / f"{name}.md").write_text(f"---\nkind: {kind}\nwinner: {folder}/{name}/candidates/a.png\n---\n# {name}\n", encoding="utf-8")
    return f"{folder}/{name}.md"


def test_a_download_named_like_a_waiting_asset_goes_onto_it(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    rel = waiting_asset(ws, "treasure-chest")
    (downloads / "treasure chest 3d model.glb").write_bytes(GLB)
    box = inbox()
    box.scan(ws, downloads, 0)
    assert box.scan(ws, downloads, 0) == [rel]
    assert asset_studio.describe_asset(ws, rel)["outputs"]["3d"][0]["path"] == "props/treasure-chest/3d/treasure-chest.glb"
    assert not (ws / "studio-downloads").exists()
    assert model_inbox.waiting_assets(ws) == []


def test_a_near_match_is_offered_not_assumed(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    rel = waiting_asset(ws, "treasure-chest")
    waiting_asset(ws, "stone-wall")
    (downloads / "wooden treasure chest 3d model.glb").write_bytes(GLB)
    box = inbox()
    box.scan(ws, downloads, 0)
    assert box.scan(ws, downloads, 0) == ["studio-downloads/wooden-treasure-chest.md"]
    assert asset_studio.describe_asset(ws, rel)["outputs"]["3d"] == []
    assert model_inbox.pairing(ws)["models"][0]["suggested"] == rel


def test_two_assets_with_the_same_name_are_not_guessed_between(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    waiting_asset(ws, "chest", folder="props")
    waiting_asset(ws, "chest", folder="items")
    (downloads / "chest.glb").write_bytes(GLB)
    box = inbox()
    box.scan(ws, downloads, 0)
    assert box.scan(ws, downloads, 0) == ["studio-downloads/chest.md"]


def test_pairing_moves_the_model_and_a_redownload_stays_out(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    rel = waiting_asset(ws, "treasure-chest")
    (downloads / "gold box.glb").write_bytes(GLB)
    box = inbox()
    box.scan(ws, downloads, 0)
    source = box.scan(ws, downloads, 0)[0]
    thumb = ws / "studio-downloads" / "gold-box" / "3d" / "gold-box_thumb.png"
    thumb.write_bytes(b"\x89PNG")

    model_inbox.pair(ws, source, rel)

    output = asset_studio.describe_asset(ws, rel)["outputs"]["3d"][0]
    assert output["path"] == "props/treasure-chest/3d/treasure-chest.glb"
    assert output["thumb"] == "props/treasure-chest/3d/treasure-chest_thumb.png"
    assert not (ws / source).exists() and not (ws / "studio-downloads" / "gold-box").exists()
    assert list((ws / ".trash").iterdir())  # the old asset can be restored
    assert model_inbox.pairing(ws)["models"] == []

    (downloads / "gold box (1).glb").write_bytes(GLB)
    fresh = inbox()
    fresh.scan(ws, downloads, 0)
    assert fresh.scan(ws, downloads, 0) == []


def test_pairing_refuses_anything_but_a_download_and_a_waiting_asset(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    rel = waiting_asset(ws, "treasure-chest")
    other = waiting_asset(ws, "stone-wall")
    (downloads / "gold box.glb").write_bytes(GLB)
    box = inbox()
    box.scan(ws, downloads, 0)
    source = box.scan(ws, downloads, 0)[0]
    for bad_source, bad_target in ((other, rel), (source, "props/nope.md"), (source, "../outside.md")):
        with pytest.raises(ValueError):
            model_inbox.pair(ws, bad_source, bad_target)


def test_keep_separate_stops_offering_it(tmp_path: Path) -> None:
    downloads, ws = dirs(tmp_path)
    waiting_asset(ws, "treasure-chest")
    (downloads / "gold box.glb").write_bytes(GLB)
    box = inbox()
    box.scan(ws, downloads, 0)
    source = box.scan(ws, downloads, 0)[0]
    model_inbox.keep_separate(ws, source)
    assert model_inbox.pairing(ws)["models"] == []
    assert (ws / source).is_file()


def test_batches_hold_thirty_readable_images_of_waiting_assets_only(tmp_path: Path) -> None:
    _, ws = dirs(tmp_path)
    for number in range(31):
        waiting_asset(ws, f"crate-{number:02d}")
    waiting_asset(ws, "crate-00", folder="items")  # same name in another section
    waiting_asset(ws, "boom", kind="sfx")
    waiting_asset(ws, "made")
    (ws / "props" / "made" / "3d").mkdir()
    (ws / "props" / "made" / "3d" / "made.glb").write_bytes(GLB)

    result = model_inbox.export_batches(ws)

    assert result["total"] == 32 and result["batches"] == {"batch-01": 30, "batch-02": 2}
    names = [p.name for p in (ws / ".tripo-batch").rglob("*.png")]
    assert len(names) == len(set(names)) == 32
    assert "crate-00.png" in names and "crate-00-2.png" in names
    assert "boom.png" not in names and "made.png" not in names

    (ws / "props" / "crate-00.md").unlink()  # exporting again replaces the last export
    assert model_inbox.export_batches(ws)["total"] == 31
    assert len(list((ws / ".tripo-batch").rglob("*.png"))) == 31

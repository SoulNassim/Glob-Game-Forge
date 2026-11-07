"""Whole-project tools: sample project, board, press kit, key art, batch runs, prompt history, pillar strength."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forge import asset_studio, overview
from forge.blueprint_contract import serialize_frontmatter


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FORGE_PROJECTS_DIR", str(tmp_path / "projects"))
    from forge.server.app import app

    return TestClient(app, base_url="http://127.0.0.1:8420")


def _project(client: TestClient, name: str) -> Path:
    """A new project of the test's own (never "default", which other tests may have opened elsewhere)."""
    project = client.post("/api/projects", json={"name": name, "unique": True}).json()["project"]
    return Path(client.get("/api/storage", params={"project": project}).json()["workspace_dir"])


def _asset(ws: Path, rel: str, **meta: object) -> Path:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_frontmatter(dict(meta), f"# {path.stem}\n"), encoding="utf-8")
    return path


def test_sample_project_installs_with_board_and_press_kit(client: TestClient) -> None:
    project = client.post("/api/projects/sample", json={}).json()["project"]
    assert project == "pocket_kaiju"
    # A second install never overwrites the first.
    assert client.post("/api/projects/sample", json={}).json()["project"] == "pocket_kaiju_2"

    ov = client.get("/api/overview", params={"project": project}).json()
    assert ov["cover"] == "cover.jpg"
    # "08-vfx" is section 08 even though there is no 07.
    assert [s["number"] for s in ov["sections"]][-1] == "08"

    board = client.get("/api/board", params={"project": project}).json()
    assets = [a for g in board["groups"] for a in g["assets"]]
    assert len(assets) >= 15 and any(a["model"] for a in assets) and any(a["audio"] for a in assets)

    card = next(c for c in client.get("/api/projects/cards").json()["projects"] if c["project"] == project)
    assert card["cover"] == "cover.jpg"

    kit = zipfile.ZipFile(io.BytesIO(client.get("/api/press-kit", params={"project": project}).content))
    html = kit.read(f"{project}-press-kit/index.html").decode()
    assert "Pocket Kaiju" in html and "model-viewer" in html
    assert all(name in kit.namelist() for name in (f"{project}-press-kit/media/cover.jpg",))

    assert client.post("/api/projects/sample", json={"sample": "../etc"}).status_code == 404


def test_sound_winner_is_not_used_as_a_tile_picture(tmp_path: Path) -> None:
    _asset(tmp_path, "sfx/roar.md", kind="sfx", prompt="roar", winner="sfx/roar/audio/roar.mp3")
    (tmp_path / "sfx" / "roar" / "audio").mkdir(parents=True)
    (tmp_path / "sfx" / "roar" / "audio" / "roar.mp3").write_bytes(b"ID3")
    tile = next(f for f in overview.describe_folder(tmp_path, "sfx")["files"] if f["kind"] == "asset")
    assert tile["thumb"] is None and tile["audio"] == "sfx/roar/audio/roar.mp3"


def test_kind_aspect_and_pillar_strength(client: TestClient) -> None:
    ws = _project(client, "aspect")
    (ws / "pillars").mkdir(parents=True, exist_ok=True)
    for name in ("a.png", "b.png", "c.png"):
        (ws / "pillars" / name).write_bytes(b"\x89PNG")
    _asset(ws, "fx/beam.md", kind="vfx", prompt="a rainbow beam")
    req = asset_studio.build_request(ws, "fx/beam.md", "candidates")
    assert req["settings"]["aspect_ratio"] == "16:9" and "16:9" in req["prompt"]
    assert len([r for r in req["references"] if r["role"] == "pillar"]) == 3

    _asset(ws, "fx/beam.md", kind="vfx", prompt="a rainbow beam", pillars="light", aspect="4:3")
    light = asset_studio.build_request(ws, "fx/beam.md", "candidates")
    assert light["settings"]["aspect_ratio"] == "4:3"
    assert len([r for r in light["references"] if r["role"] == "pillar"]) == 2
    assert asset_studio.PILLAR_RULE_LIGHT in light["prompt"]

    _asset(ws, "fx/beam.md", kind="vfx", prompt="a rainbow beam", pillars="off")
    off = asset_studio.build_request(ws, "fx/beam.md", "candidates")
    assert not off["references"] and asset_studio.PILLAR_RULE not in off["prompt"]

    # The asset page sets these through the normal asset update.
    resp = client.patch("/api/studio/asset", params={"project": ws.parent.name}, json={"path": "fx/beam.md", "fields": {"pillars": "strong", "height_m": 3}})
    assert resp.json()["meta"]["pillars"] == "strong" and resp.json()["meta"]["height_m"] == 3


def test_key_art_winner_becomes_the_cover(tmp_path: Path) -> None:
    rel = asset_studio.key_art_asset(tmp_path, "Space Goats", "Goats in space.")
    meta = asset_studio.read_asset(tmp_path, rel)[1]
    assert meta["cover"] is True and meta["aspect"] == "16:9" and "Space Goats" in meta["prompt"]
    # Asking again reuses the same asset.
    assert asset_studio.key_art_asset(tmp_path, "Space Goats", "") == rel
    out = tmp_path / "key-art" / "candidates"
    out.mkdir(parents=True)
    (out / "one.png").write_bytes(b"\x89PNG one")
    (tmp_path / "cover.jpg").write_bytes(b"old")
    asset_studio.update_asset(tmp_path, rel, {"winner": "key-art/candidates/one.png"})
    assert (tmp_path / "cover.png").read_bytes() == b"\x89PNG one" and not (tmp_path / "cover.jpg").exists()


def test_batch_runs_only_assets_with_nothing_to_pick(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from forge.server import app as server

    ws = _project(client, "batch")
    project = ws.parent.name
    _asset(ws, "props/crate.md", kind="prop", prompt="a crate")
    _asset(ws, "props/barrel.md", kind="prop", prompt="a barrel")
    done = ws / "props" / "barrel" / "candidates"
    done.mkdir(parents=True)
    (done / "b.png").write_bytes(b"\x89PNG")
    _asset(ws, "props/empty.md", kind="prop")

    plan = client.get("/api/studio/batch", params={"folder": "props", "project": project}).json()
    assert [a["asset"] for a in plan["assets"]] == ["props/crate.md"]

    ran: list[str] = []

    async def fake_run(project: str, job: dict, request: dict) -> None:
        ran.append(request["asset"])
        job["status"] = "done"

    monkeypatch.setattr(server, "_run_studio_job", fake_run)
    res = client.post("/api/studio/batch", params={"project": project}, json={"folder": "props"}).json()
    assert [j["asset"] for j in res["jobs"]] == ["props/crate.md"] and res["jobs"][0]["status"] == "queued"


def test_output_history_reads_what_each_output_came_from(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    _asset(ws, "hero.md", kind="character", prompt="a knight")
    record = tmp_path / "vault" / "hero" / "candidates"
    record.mkdir(parents=True)
    (record / "hero-1.json").write_text(json.dumps({"file": "hero/candidates/hero-1.png", "prompt": "a knight\n\nStyle bible: …", "asset_prompt": "a knight", "model": "svc:elevenlabs/nano", "created_at": 1.0}))
    history = asset_studio.output_history(ws, "hero.md")
    assert history["hero/candidates/hero-1.png"]["asset_prompt"] == "a knight"


def test_running_out_of_credits_is_remembered() -> None:
    from forge.server import app as server

    server.credit_alerts.clear()
    server._note_credits({"model": "svc:higgsfield/meshy", "action": "make_3d"}, "", "Not enough credits", 0)
    server._note_credits({"action": "make_3d"}, "meshy", "Insufficient Meshy credits (402).", 402)
    server._note_credits({"model": "gemini-x"}, "gemini", "Bad prompt", 400)
    assert set(server.credit_alerts) == {"higgsfield", "meshy"}


def test_sound_settings_are_decided_per_sound() -> None:
    infer = asset_studio.infer_sound_settings
    assert infer("sfx", "cave ambience", "Dripping water and a low wind in a cave") == {"duration": 15, "loop": True}
    assert infer("sfx", "sword hit", "A sword hits armor") == {"duration": 2, "loop": False}
    assert infer("sfx", "menu click", "Soft UI click") == {"duration": 1, "loop": False}
    assert infer("sfx", "door", "A heavy door creaks, 6 seconds") == {"duration": 6, "loop": False}
    assert infer("music", "victory fanfare", "Short brass fanfare") == {"duration": 8, "loop": False}
    assert infer("music", "forest theme", "Calm flute") == {"duration": 30, "loop": True}
    # What the draft wrote in the header wins over the guess.
    assert infer("sfx", "cave ambience", "wind", {"duration": 10, "loop": False}) == {"duration": 10, "loop": False}


def test_audio_request_uses_the_decided_settings(client: TestClient) -> None:
    ws = _project(client, "sounds-test")
    _asset(ws, "sounds/engine-hum.md", kind="sfx", prompt="A steady engine hum")
    req = asset_studio.build_request(ws, "sounds/engine-hum.md", "candidates")
    assert req["settings"]["loop"] is True and req["settings"]["duration"] == 15


def test_batch_runs_make_3d_with_one_set_of_settings(client: TestClient) -> None:
    project = client.post("/api/projects", json={"name": "batch-3d-test", "unique": True}).json()["project"]
    ws = Path(client.get("/api/storage", params={"project": project}).json()["workspace_dir"])
    _asset(ws, "props/crate.md", kind="prop", prompt="a crate")  # no winner yet: skipped, not an error
    res = client.post("/api/studio/batch", params={"project": project}, json={"assets": ["props/crate.md"], "action": "make_3d"})
    assert res.status_code == 200
    body = res.json()
    assert body["jobs"] == [] and "winner" in body["skipped"][0]["reason"]

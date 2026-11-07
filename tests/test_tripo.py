"""Tripo (v3 API): connecting a key, generation settings, rigging and animation, and the model tools. No network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from forge import game_tools, tripo
from forge.config import load_config, save_config
from forge.providers.base import ProviderCapability, ProviderError
from forge.providers.tripo_3d import Tripo3DAdapter

GLB = b"glTF" + b"\x00" * 12


class FakeTripo:
    """Answers Tripo's endpoints and records the bodies it was sent."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str] | None = None) -> None:
        self.posts: list[tuple[str, Any]] = []
        self.outputs = outputs or {}
        self.balance = {"balance": 812.5, "frozen": 10}
        fake = self

        async def post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
            fake.posts.append((url, kwargs.get("json")))
            if url.endswith("/files"):
                return httpx.Response(200, json={"code": 0, "data": {"file_token": "file_abc"}})
            return httpx.Response(200, json={"code": 0, "data": {"task_id": f"task{len(fake.posts)}"}})

        async def get(self: Any, url: str, **kwargs: Any) -> httpx.Response:
            if url.endswith("/account/balance"):
                if kwargs.get("headers", {}).get("Authorization") != "Bearer good":
                    return httpx.Response(401, json={"code": 2001})
                return httpx.Response(200, json={"code": 0, "data": fake.balance})
            if "/tasks/" in url:
                task_id = url.rsplit("/", 1)[-1]
                name = fake.outputs.get(task_id, "m.glb")
                return httpx.Response(200, json={"code": 0, "data": {"status": "success", "progress": 100, "credits_consumed": 25,
                                                                      "output": {"model_url": f"https://cdn.test/{name}"}}})
            return httpx.Response(200, content=GLB if url.endswith(".glb") else b"fbx-bytes")

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        monkeypatch.setattr(httpx.AsyncClient, "get", get)

    def bodies(self, suffix: str) -> list[Any]:
        return [body for url, body in self.posts if url.endswith(suffix)]


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIPO_API_KEY", raising=False)
    monkeypatch.setattr("forge.config._read_win32_user_env", lambda name: "")


async def test_connect_checks_the_key_and_reads_the_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTripo(monkeypatch)
    assert (await tripo.connect("good"))["balance"] == 812.5
    with pytest.raises(ProviderError, match="not valid"):
        await tripo.connect("bad")
    with pytest.raises(ProviderError, match="Paste your Tripo"):
        await tripo.connect("  ")


def test_generation_fields_follow_the_model() -> None:
    wanted = {"texture": True, "pbr": True, "quad": True, "smart_low_poly": True, "geometry_quality": "detailed", "face_limit": 0, "junk": 1}
    h = tripo.generation_fields("v3.1-20260211", wanted)
    assert h["geometry_quality"] == "detailed" and h["quad"] is True and "junk" not in h and "face_limit" not in h  # 0 is automatic
    assert "geometry_quality" not in tripo.generation_fields("v2.5-20250123", wanted)  # too old for it
    p1 = tripo.generation_fields("P1-20260311", wanted)
    assert "quad" not in p1 and "smart_low_poly" not in p1  # P1 has no quads; the P series has no smart low poly
    assert tripo.generation_fields("P2-20260801", wanted)["quad"] is True
    assert "negative_prompt" in tripo.generation_fields("v3.1-20260211", {"negative_prompt": "blurry"}, text=True)
    assert "negative_prompt" not in tripo.generation_fields("v3.1-20260211", {"negative_prompt": "blurry"})
    parts = tripo.generation_fields("v3.1-20260211", {"generate_parts": True, "texture": True, "pbr": True})
    assert parts["texture"] is False and parts["pbr"] is False  # parts need a bare mesh
    assert tripo.generation_fields("v3.1-20260211", {"texture_quality": "fast"})["texture_version"] == "v3.5-20260815"


def test_settings_from_config_ignores_bad_values() -> None:
    cfg = load_config()
    s = tripo.settings_from_config(cfg, {"model": "meshy-7.1", "texture_quality": "ultra", "face_limit": "abc", "texture": False})
    assert s["model"] == "v3.1-20260211" and s["texture_quality"] == "standard" and s["face_limit"] == 0
    assert s["texture"] is False and s["pbr"] is False  # no texture means no PBR maps


async def test_multiview_sends_the_views_by_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeTripo(monkeypatch)
    refs = []
    for name in ("front", "left", "back"):
        (tmp_path / f"{name}.png").write_bytes(b"\x89PNG")
        refs.append(tmp_path / f"{name}.png")
    out = tmp_path / "out"
    out.mkdir()
    await Tripo3DAdapter(api_key="k", demo_mode=False).execute(
        ProviderCapability.MULTI_IMAGE_TO_3D, "x", out, "m", reference_files=refs, params={"poll_interval": 0, "texture_quality": "detailed"})
    body = fake.bodies("/generation/multiview-to-model")[0]
    assert body["inputs"] == [{"front": "file_abc"}, {"left": "file_abc"}, {"back": "file_abc"}]
    assert body["texture_quality"] == "detailed"
    assert (out / "m.glb").read_bytes() == GLB


async def test_quad_mesh_is_converted_to_a_glb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeTripo(monkeypatch, outputs={"task1": "m.fbx", "task2": "m.glb"})
    out = tmp_path / "out"
    out.mkdir()
    res = await Tripo3DAdapter(api_key="k", demo_mode=False).execute(
        ProviderCapability.TEXT_TO_3D, "a sword", out, "sword", params={"poll_interval": 0, "model": "P2-20260801", "quad": True})
    assert fake.bodies("/generation/text-to-model")[0]["quad"] is True
    assert fake.bodies("/models/convert")[0]["format"] == "GLTF"
    assert (out / "sword.glb").read_bytes() == GLB and (out / "sword.fbx").exists()
    assert res.extra_files["fbx"] == "assets/sword.fbx"
    assert res.metadata["credits"] == 50  # the generation and the conversion


def _asset(tmp_path: Path) -> tuple[Path, str]:
    from forge.asset_studio import output_dir, read_asset

    ws = tmp_path / "ws"
    (ws / "characters").mkdir(parents=True)
    (ws / "characters" / "hero.md").write_text("---\nkind: character\n---\nA hero.\n", encoding="utf-8")
    model = output_dir(read_asset(ws, "characters/hero.md")[0]) / "3d" / "hero.glb"
    model.parent.mkdir(parents=True)
    model.write_bytes(GLB)
    return ws, "characters/hero.md"


async def test_rig_then_animate_with_tripo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    save_config({"tripo_api_key": "good", "default_3d_provider": "tripo"})
    fake = FakeTripo(monkeypatch)
    ws, rel = _asset(tmp_path)
    assert game_tools.rig_engine() == "tripo"
    rigged = await game_tools.rig_model(ws, rel, next(str(p.relative_to(ws)).replace("\\", "/") for p in ws.rglob("hero.glb")), settings={"rig_type": "quadruped"})
    rig_body = fake.bodies("/animations/rig")[0]
    assert rig_body["input"] == "file_abc" and rig_body["rig_type"] == "quadruped" and "spec" not in rig_body  # spec is for bipeds
    assert len(rigged) == 1 and rigged[0].endswith(".glb")
    from forge.asset_studio import read_asset

    meta = read_asset(ws, rel)[1]
    assert meta["rig_provider"] == "tripo" and meta["rig_task"]

    clips = await game_tools.animate(ws, rel, ["preset:walk", "preset:run", "preset:nope"], in_place=True)
    retarget = fake.bodies("/animations/retarget")[0]
    assert retarget["input"] == meta["rig_task"] and retarget["animations"] == ["preset:walk", "preset:run"] and retarget["animate_in_place"] is True
    assert len(clips) == 1


async def test_more_than_five_animations_make_more_than_one_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    save_config({"tripo_api_key": "good"})
    fake = FakeTripo(monkeypatch)
    ws, rel = _asset(tmp_path)
    from forge.asset_studio import read_asset, update_asset

    update_asset(ws, rel, {"rig_task": "rig1", "rig_provider": "tripo"})
    ids = [a["id"] for a in tripo.ANIMATIONS[:7]]
    assert len(await game_tools.animate(ws, rel, ids)) == 2
    assert [len(b.get("animations", [1])) for b in fake.bodies("/animations/retarget")] == [5, 2]
    assert read_asset(ws, rel)[1]["rig_task"] == "rig1"


async def test_model_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    save_config({"tripo_api_key": "good"})
    fake = FakeTripo(monkeypatch, outputs={"task2": "low.glb", "task4": "tex.glb", "task6": "x.fbx"})
    ws, rel = _asset(tmp_path)
    model = next(str(p.relative_to(ws)).replace("\\", "/") for p in ws.rglob("hero.glb"))
    low = await game_tools.tripo_process(ws, rel, model, "retopo", {"face_limit": 8000})
    assert fake.bodies("/mesh/decimate")[0]["face_limit"] == 8000 and "retopo-8k" in low[0]
    tex = await game_tools.tripo_process(ws, rel, model, "retexture", {"prompt": "mossy stone", "quality": "detailed"})
    assert fake.bodies("/models/texture")[0]["texture_prompt"] == {"text": "mossy stone"} and "retextured" in tex[0]
    fbx = await game_tools.tripo_process(ws, rel, model, "convert", {"format": "fbx"})
    assert fake.bodies("/models/convert")[0]["format"] == "FBX" and fbx[0].endswith(".fbx")
    with pytest.raises(ValueError, match="can't convert"):
        await game_tools.tripo_process(ws, rel, model, "convert", {"format": "dwg"})
    with pytest.raises(ValueError, match="Unknown Tripo tool"):
        await game_tools.tripo_process(ws, rel, model, "explode")


def test_tripo_without_a_key_says_to_connect(tmp_path: Path) -> None:
    ws, rel = _asset(tmp_path)
    with pytest.raises(ValueError, match="Connect Tripo"):
        game_tools._tripo_key()
    assert game_tools.rig_engine() == "meshy"


def test_make_3d_request_uses_tripo_settings(tmp_path: Path) -> None:
    from forge.asset_studio import build_request, update_asset

    save_config({"default_3d_provider": "tripo", "tripo_model": "P1-20260311"})
    ws, rel = _asset(tmp_path)
    img = ws / "characters" / "hero.png"
    img.write_bytes(b"\x89PNG")
    update_asset(ws, rel, {"winner": "characters/hero.png"})
    req = build_request(ws, rel, "make_3d")
    assert req["engine"] == "tripo" and req["settings"]["model"] == "P1-20260311" and "model" in req["choices"]
    assert req["model"] == "tripo:P1-20260311"


def test_connect_route_saves_only_a_working_key(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTripo(monkeypatch)
    from forge.server.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8420")
    bad = client.post("/api/tripo/connect", json={"api_key": "bad"})
    assert bad.status_code == 401 and not load_config().tripo_api_key  # the "not valid" message reaches the user, nothing is saved
    ok = client.post("/api/tripo/connect", json={"api_key": "good", "make_default": True})
    assert ok.status_code == 200 and ok.json()["balance"]["balance"] == 812.5
    assert load_config().default_3d_provider == "tripo" and load_config().tripo_api_key == "good"
    assert "good" not in client.get("/api/config").text  # keys are masked
    assert client.get("/api/tripo").json()["connected"] is True
    gone = client.delete("/api/tripo").json()
    assert gone["connected"] is False and load_config().default_3d_provider == "meshy"

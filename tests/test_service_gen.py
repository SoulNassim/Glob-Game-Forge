"""Generating with a signed-in service (`svc:<service>/<model>`), against fake ElevenLabs and Higgsfield tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from forge import service_gen, services
from forge.providers.base import ProviderCapability
from forge.providers.elevenlabs_image import resolve_image_provider
from forge.providers.service_mcp import ServiceAdapter

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.polls = 0

    def __call__(self, service: str, tool: str, args: dict[str, Any]) -> Any:
        self.calls.append((service, tool, args))
        if tool == "models_explore":
            if args["type"] == "3d":
                return {"items": [{"id": "image_to_3d", "name": "Image to 3D", "medias": [{"type": "image", "roles": ["image"], "max": 1}]}]}
            return {"items": [{"id": "gpt_image_2_5", "name": "GPT Image 2.5", "medias": [{"roles": ["image"], "max": 2}], "aspect_ratios": ["1:1"]},
                              {"id": "image_background_remover", "name": "Image Background Remover"}]}
        if tool == "media_upload":
            return {"files": [{"media_id": "m-1", "upload_url": "https://up.example.test/m-1"}]}
        if tool == "media_confirm":
            return {"ok": True}
        if tool in ("generate_image_batch", "generate_3d"):
            return {"jobs": [{"index": 0, "job_id": "11111111-1111-4111-8111-111111111111"}]}
        if tool in ("jobs_wait", "job_status"):
            self.polls += 1
            if self.polls == 1:
                return {"all_terminal": False, "poll_after_seconds": 0, "jobs": [{"status": "in_progress"}]}
            ext = "glb" if tool == "job_status" else "png"
            return {"all_terminal": True, "jobs": [{"status": "completed", "results": [{"url": f"https://cdn.example.test/out.{ext}"}]}]}
        if tool == "creative_create_flow":
            return {"flow_id": "F1"}
        if tool == "creative_create_asset_upload":
            return {"asset_id": "A1", "upload_url": "https://up.example.test/A1"}
        if tool == "creative_finalize_asset_upload":
            return {"node_id": "N1"}
        if tool == "creative_get_model_schema":
            return {"parameters": [{"name": "aspect_ratio", "options": [{"value": "1:1"}, {"value": "16:9"}]}]}
        if tool == "creative_add_flow_node":
            return {"node_id": "CUT" if args.get("model_id") == "birefnet-v2-bg-removal" else "N2", "node_urls": ["https://elevenlabs.io/app/flows/F1"]}
        if tool == "creative_get_flow_node_types":
            return {"node_types": [{"type": "sfx", "default_model_id": "sfx-v9"}]}
        if tool == "creative_list_voices":
            return {"voices": [{"voice_id": "V1", "name": "Tyler", "category": "professional"}]}
        if tool == "creative_run_flow_nodes":
            return {"session_ids": ["S-" + args["node_ids"][0]]}
        if tool == "creative_get_flow_run_status":
            return {"all_completed": True, "has_failures": False, "generations": [{"generation_id": "G1", "status": "completed"}]}
        if tool == "creative_get_generations":
            cut = any(t == "creative_run_flow_nodes" and a["node_ids"] == ["CUT"] for _, t, a in self.calls)
            audio = any(t == "creative_add_flow_node" and a["node_type"] in ("sfx", "tts", "music") for _, t, a in self.calls)
            return {"generations": [{"id": "G1", "full_res_url": f"https://cdn.example.test/{'cut' if cut else 'g1'}.{'mp3' if audio else 'png'}"}]}
        raise AssertionError(tool)


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> FakeTools:
    fake = FakeTools()
    monkeypatch.setattr(services, "call_tool_data", fake)
    monkeypatch.setattr(services, "connected", lambda: ["elevenlabs", "higgsfield"])
    monkeypatch.setattr(services, "tool_schema", lambda s, n: {"inputSchema": {"properties": {"model_id": {"enum": ["gpt-image-2", "topaz-image-upscale-v2"]}}}})
    monkeypatch.setattr(service_gen, "_put", lambda url, path, mime: fake.calls.append(("put", url, {})))
    monkeypatch.setattr(service_gen.time, "sleep", lambda s: None)

    def download(url: str, out_dir: Path, stem: str) -> Path:
        target = out_dir / f"{stem}{Path(url).suffix}"
        target.write_bytes(PNG)
        return target

    monkeypatch.setattr(service_gen, "_download", download)
    service_gen._catalog.clear()
    service_gen._node_models.clear()
    return fake


def test_catalog_lists_each_signed_in_service(tools: FakeTools) -> None:
    ids = [m["id"] for m in service_gen.catalog("image")]
    assert ids == ["svc:elevenlabs/gpt-image-2", "svc:higgsfield/gpt_image_2_5"]  # upscalers and background removers are left out
    assert [m["id"] for m in service_gen.catalog("3d")] == ["svc:higgsfield/image_to_3d"]


def test_higgsfield_image_uploads_references_and_waits(tools: FakeTools, tmp_path: Path) -> None:
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG)
    out = service_gen.generate("svc:higgsfield/gpt_image_2_5", "image", "an orc", [ref], tmp_path, "orc", "1:1")
    assert out.read_bytes() == PNG and out.suffix == ".png"
    batch = next(a for s, t, a in tools.calls if t == "generate_image_batch")["requests"][0]
    assert batch == {"model": "gpt_image_2_5", "count": 1, "prompt": "an orc", "aspect_ratio": "1:1", "medias": [{"value": "m-1", "role": "image"}]}
    assert tools.polls == 2


def test_higgsfield_3d_needs_an_image_and_returns_a_glb(tools: FakeTools, tmp_path: Path) -> None:
    with pytest.raises(service_gen.ServiceGenError, match="source image"):
        service_gen.generate("svc:higgsfield/image_to_3d", "3d", "orc", [], tmp_path, "orc")
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG)
    assert service_gen.generate("svc:higgsfield/image_to_3d", "3d", "orc", [ref], tmp_path, "orc").suffix == ".glb"


def test_elevenlabs_image_through_the_studio_provider(tools: FakeTools, tmp_path: Path) -> None:
    params = {"model": "svc:elevenlabs/gpt-image-2"}
    assert resolve_image_provider("gemini", params) == "service"
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG)
    result = asyncio.run(ServiceAdapter().execute(ProviderCapability.IMAGE_EDIT, "a knight", tmp_path, "knight", [ref], {**params, "aspect_ratio": "1:1"}))
    assert result.file_path == "assets/knight.png" and result.provider == "service"
    node = next(a for s, t, a in tools.calls if t == "creative_add_flow_node")
    # Without the aspect ratio ElevenLabs paints 16:9.
    assert node["model_id"] == "gpt-image-2" and node["model_parameters"] == {"aspect_ratio": "1:1"} and node["connect_from"] == ["N1"]
    assert not any(t == "creative_add_flow_node" and a["model_id"] == "birefnet-v2-bg-removal" for _, t, a in tools.calls)


def test_transparent_background_runs_the_services_cut_out(tools: FakeTools, tmp_path: Path) -> None:
    service_gen.generate("svc:elevenlabs/gpt-image-2", "image", "a knight", [], tmp_path, "knight", "1:1", transparent=True)
    cut = next(a for s, t, a in tools.calls if t == "creative_add_flow_node" and a["model_id"] == "birefnet-v2-bg-removal")
    assert cut["connect_from"] == ["N2"] and cut["node_type"] == "image-generation"
    tools.calls.clear()
    tools.polls = 0
    service_gen.generate("svc:higgsfield/gpt_image_2_5", "image", "an orc", [], tmp_path, "orc", "1:1", transparent=True)
    batches = [a["requests"][0] for s, t, a in tools.calls if t == "generate_image_batch"]
    assert batches[1] == {"model": "image_background_remover", "count": 1, "medias": [{"value": "11111111-1111-4111-8111-111111111111", "role": "image_references"}]}


def test_a_service_that_is_not_signed_in_is_refused(tools: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "connected", lambda: [])
    with pytest.raises(service_gen.ServiceGenError, match="Sign in to Higgsfield"):
        service_gen.generate("svc:higgsfield/gpt_image_2_5", "image", "x", [], tmp_path, "x")


def test_a_failed_cut_out_keeps_the_paid_image(tools: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tools.__call__

    def refuse_cut_out(service: str, tool: str, args: dict[str, Any]) -> Any:
        if tool == "creative_add_flow_node" and args["model_id"] == "birefnet-v2-bg-removal":
            raise services.ServiceError("Node type cannot be created here.")
        return real(service, tool, args)

    monkeypatch.setattr(services, "call_tool_data", refuse_cut_out)
    out = service_gen.generate("svc:elevenlabs/gpt-image-2", "image", "a knight", [], tmp_path, "knight", "1:1", transparent=True)
    assert out.name == "knight.png"


def test_elevenlabs_sounds_through_the_studio_provider(tools: FakeTools, tmp_path: Path) -> None:
    adapter = ServiceAdapter()
    roar = asyncio.run(adapter.execute(ProviderCapability.SOUND_EFFECT, "a cute roar", tmp_path, "roar", [], {"model": "svc:elevenlabs/sfx", "loop": False}))
    assert roar.file_path == "assets/roar.mp3"
    node = next(a for s, t, a in tools.calls if t == "creative_add_flow_node")
    assert node["node_type"] == "sfx" and node["prompt"] == "a cute roar" and node["model_parameters"] == {"loop": False}
    assert node["model_id"] == "sfx-v9"  # the service's default model; a known one when it lists none
    tools.calls.clear()
    # A spoken line with no voice picked gets one of the account's voices instead of failing.
    asyncio.run(adapter.execute(ProviderCapability.TEXT_TO_SPEECH, "KAIJU TIME!", tmp_path, "line", [], {"model": "svc:elevenlabs/tts"}))
    speech = next(a for s, t, a in tools.calls if t == "creative_add_flow_node")
    assert speech["model_parameters"] == {"voice_id": "V1"} and speech["model_id"] == "eleven_v4"
    tools.calls.clear()
    asyncio.run(adapter.execute(ProviderCapability.TEXT_TO_MUSIC, "synthwave", tmp_path, "loop", [], {"model": "svc:elevenlabs/music", "duration": 30}))
    assert next(a for s, t, a in tools.calls if t == "creative_add_flow_node")["model_parameters"] == {"duration_seconds": 30.0, "instrumental": True}


def test_signed_in_sound_generators_come_first(tools: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from forge import asset_studio

    cfg = SimpleNamespace(demo_mode=False, get_key=lambda name: "")
    monkeypatch.setattr(asset_studio, "load_config", lambda: cfg)
    music = [m for m in asset_studio.list_audio_models() if "music" in m["kinds"]]
    assert music[0]["id"] == "svc:elevenlabs/music" and music[0]["available"]
    assert not music[1]["available"]  # no Stability key: offered, but never run as a silent demo
    cfg.demo_mode = True  # demo mode never reaches a paid service
    assert not asset_studio.list_audio_models()[0]["available"]

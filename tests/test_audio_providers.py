"""Mocked-HTTP tests for the ElevenLabs audio, Stable Audio and Tripo adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from forge.models import AssetType
from forge.providers.base import ProviderCapability, ProviderError
from forge.providers.elevenlabs_audio import ElevenLabsAudioAdapter
from forge.providers.registry import REGISTRY
from forge.providers.stability_audio import StabilityAudioAdapter
from forge.providers.tripo_3d import Tripo3DAdapter


@pytest.fixture(autouse=True)
def _demo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_DEMO_MODE", "1")


@pytest.mark.asyncio
async def test_elevenlabs_sfx_request_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        seen.update(url=url, **kwargs)
        return httpx.Response(200, content=b"ID3-fake-mp3")

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    res = await ElevenLabsAudioAdapter(api_key="k", demo_mode=False).execute(
        capability=ProviderCapability.SOUND_EFFECT,
        prompt="sword clash",
        output_dir=tmp_path,
        filename_stem="sfx",
        params={"duration": 2.5, "prompt_influence": 0.6, "loop": True},
    )
    assert seen["url"].endswith("/v1/sound-generation")
    assert seen["params"] == {"output_format": "mp3_44100_128"}
    assert seen["headers"]["xi-api-key"] == "k"
    assert seen["json"] == {"text": "sword clash", "duration_seconds": 2.5, "prompt_influence": 0.6, "loop": True}
    assert (tmp_path / "sfx.mp3").read_bytes() == b"ID3-fake-mp3"
    assert res.file_path == "assets/sfx.mp3"


@pytest.mark.asyncio
async def test_elevenlabs_tts_requires_voice_and_posts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = ElevenLabsAudioAdapter(api_key="k", demo_mode=False)
    with pytest.raises(ProviderError, match="voice_id"):
        await adapter.execute(ProviderCapability.TEXT_TO_SPEECH, "hello", tmp_path, "tts", params={})

    seen: dict[str, Any] = {}

    async def mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        seen.update(url=url, **kwargs)
        return httpx.Response(200, content=b"mp3")

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    await adapter.execute(ProviderCapability.TEXT_TO_SPEECH, "hello", tmp_path, "tts", params={"voice_id": "v1"})
    assert seen["url"].endswith("/v1/text-to-speech/v1")
    assert seen["json"] == {"text": "hello", "model_id": "eleven_multilingual_v2"}
    assert (tmp_path / "tts.mp3").exists()


@pytest.mark.asyncio
async def test_stable_audio_request_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        seen.update(url=url, **kwargs)
        return httpx.Response(200, content=b"mp3-bytes", headers={"seed": "123"})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    res = await StabilityAudioAdapter(api_key="sk", demo_mode=False).execute(
        ProviderCapability.TEXT_TO_MUSIC, "calm forest theme", tmp_path, "music", params={"duration": 20}
    )
    assert seen["url"] == "https://api.stability.ai/v2beta/audio/stable-audio-2/text-to-audio"
    assert seen["headers"] == {"Authorization": "Bearer sk", "Accept": "audio/*"}
    form = {k: v[1] for k, v in seen["files"].items()}
    assert form == {"prompt": "calm forest theme", "duration": "20", "model": "stable-audio-2.5", "output_format": "mp3"}
    assert (tmp_path / "music.mp3").read_bytes() == b"mp3-bytes"
    assert res.seed == 123


@pytest.mark.asyncio
async def test_tripo_happy_path_and_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG fake")
    state = {"status": "success", "calls": []}

    async def mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        state["calls"].append(("POST", url, kwargs.get("json")))
        if url.endswith("/files"):
            return httpx.Response(200, json={"code": 0, "data": {"file_token": "tok1"}})
        return httpx.Response(200, json={"code": 0, "data": {"task_id": "t1"}})

    async def mock_get(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        state["calls"].append(("GET", url, None))
        if url.endswith("/tasks/t1"):
            out = {"model_url": "https://cdn.test/m.glb", "rendered_image_url": "https://cdn.test/r.webp"}
            return httpx.Response(200, json={"code": 0, "data": {"status": state["status"], "progress": 100, "output": out}})
        return httpx.Response(200, content=b"glTFglb-bytes")

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    adapter = Tripo3DAdapter(api_key="k", demo_mode=False)
    out = tmp_path / "out"
    out.mkdir()
    res = await adapter.execute(
        ProviderCapability.IMAGE_TO_3D, "a chair", out, "chair", reference_files=[ref], params={"poll_interval": 0}
    )
    task_call = [c for c in state["calls"] if c[0] == "POST" and c[1].endswith("/generation/image-to-model")][0]
    assert task_call[2]["input"] == "tok1"
    assert task_call[2]["model"] == "v3.1-20260211"
    assert (out / "chair.glb").read_bytes() == b"glTFglb-bytes"
    assert res.thumbnail_path == "assets/chair_thumb.webp"
    assert res.metadata["tripo_task_id"] == "t1"

    state["status"] = "failed"
    with pytest.raises(ProviderError, match="failed"):
        await adapter.execute(
            ProviderCapability.TEXT_TO_3D, "a chair", out, "chair2", params={"poll_interval": 0}
        )


@pytest.mark.asyncio
async def test_audio_adapters_demo_mode_never_use_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("network used in demo mode")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    for adapter, cap in (
        (ElevenLabsAudioAdapter(), ProviderCapability.SOUND_EFFECT),
        (StabilityAudioAdapter(), ProviderCapability.TEXT_TO_MUSIC),
    ):
        res = await adapter.execute(cap, "x", tmp_path, adapter.provider_id)
        assert (tmp_path / Path(res.file_path).name).read_bytes()[:2] == b"\xff\xfb"
    glb = await Tripo3DAdapter().execute(ProviderCapability.TEXT_TO_3D, "robot", tmp_path, "robot")
    assert (tmp_path / "robot.glb").exists() and glb.provider == "demo"


def test_registry_lists_new_providers() -> None:
    ids = {p["id"]: p for p in REGISTRY.list_providers()}
    assert {"elevenlabs_audio", "stability_audio", "tripo"} <= set(ids)
    assert REGISTRY.find_for_capability(ProviderCapability.TEXT_TO_MUSIC).provider_id == "stability_audio"
    assert "text_to_speech" in ids["elevenlabs_audio"]["capabilities"]


@pytest.mark.asyncio
async def test_generate_single_asset_audio_demo(tmp_path: Path) -> None:
    from forge.agent.action_layer import ForgeActionLayer

    store = ForgeActionLayer(root_dir=tmp_path).get_store("audio_proj")
    from forge.recipes.engine import RecipeExecutor

    executor = RecipeExecutor(store)
    asset = await executor._generate_single_asset(
        provider_id="elevenlabs_audio",
        capability=ProviderCapability.SOUND_EFFECT,
        name="Door creak",
        asset_type=AssetType.AUDIO,
        verb="sound_effect",
        recipe_id="sound_effect",
        prompt_raw="door creak",
        prompt_full="door creak",
        bible=store.get_bible(),
        run_spent_tracker=[0.0],
    )
    assert asset.type == AssetType.AUDIO
    assert asset.file_path.endswith(".mp3")
    assert store.resolve_safe_path(asset.file_path).exists()
    assert asset.thumbnail_path == ""

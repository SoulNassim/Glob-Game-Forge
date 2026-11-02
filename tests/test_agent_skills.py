"""The Glob chat knows how it generates: skills, the generation setup, and the model it actually used."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge import service_gen, services
from forge.agent import workspace_agent
from forge.agent.action_layer import ForgeActionLayer
from forge.agent.workspace_agent import WorkspaceAgent


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[WorkspaceAgent, list[dict[str, Any]]]:
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(services, "connected", lambda: ["elevenlabs"])
    service_gen._catalog.clear()
    service_gen._catalog[("elevenlabs", "image")] = (9e18, [{"id": "svc:elevenlabs/gemini-nano-banana-2.1", "label": "ElevenLabs · Gemini Nano Banana 2.1", "service": "elevenlabs"}])
    service_gen._catalog[("elevenlabs", "3d")] = (9e18, [])
    actions = ForgeActionLayer(root_dir=tmp_path / "projects")
    ws = actions.get_workspace("p").ws_dir
    (ws / "hero.md").write_text("---\nkind: character\nprompt: A plague doctor\n---\n# Hero\n", encoding="utf-8")
    started: list[dict[str, Any]] = []
    return WorkspaceAgent(actions, model_call=None, job_starter=lambda project, req: started.append(req) or {"id": "job_1"}), started  # type: ignore[arg-type]


def test_skills_are_listed_and_readable(agent) -> None:
    bot, _ = agent
    assert {"generating", "services"} <= {s["name"] for s in workspace_agent.list_skills()}
    assert "svc:<service>/<model>" in bot.run_tool("p", {"tool": "skill", "name": "generating"})["result"]
    with pytest.raises(ValueError, match="No skill"):
        bot.run_tool("p", {"tool": "skill", "name": "nope"})


def test_generate_with_a_signed_in_model_says_what_ran(agent) -> None:
    bot, started = agent
    out = bot.run_tool("p", {"tool": "generate", "path": "hero.md", "action": "candidates", "model": "svc:elevenlabs/gemini-nano-banana-2.1"})
    assert started[0]["model"] == "svc:elevenlabs/gemini-nano-banana-2.1"
    assert "ElevenLabs · Gemini Nano Banana 2.1 (your ElevenLabs sign-in, MCP)" in out["summary"]
    with pytest.raises(ValueError, match="elevenlabs offers: svc:elevenlabs/gemini-nano-banana-2.1"):
        bot.run_tool("p", {"tool": "generate", "path": "hero.md", "action": "candidates", "model": "svc:elevenlabs/nano-banana-9"})


def test_context_names_the_default_model_and_the_skills(agent) -> None:
    bot, _ = agent
    context, _ = bot._context_block("p", {}, {}, [])
    assert "## Generation setup" in context and "Default image model" in context
    assert "svc:elevenlabs/<id>): gemini-nano-banana-2.1" in context and "- generating:" in context

"""The chat's only limit is on generations: drafts are free, however long the brief or however many pages."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from forge import services
from forge.agent import workspace_agent
from forge.agent.action_layer import ForgeActionLayer
from forge.agent.workspace_agent import MAX_GENERATIONS_PER_TURN, SPEND_WINDOW_SECONDS, WorkspaceAgent
from forge.mcp.server import ForgeMCPDispatcher


def _bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replies: list[dict[str, Any]] | None = None) -> tuple[WorkspaceAgent, list[str], list[dict[str, Any]]]:
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))
    prompts: list[str] = []
    answers = iter(replies or [])

    async def model_call(**kwargs: Any) -> dict[str, Any]:
        prompts.append(kwargs["prompt"])
        return {"text": json.dumps(next(answers)), "model": "test"}

    actions = ForgeActionLayer(root_dir=tmp_path / "projects")
    ws = actions.get_workspace("p").ws_dir
    (ws / "hero.md").write_text("---\nkind: character\nprompt: A knight\n---\n# Hero\n", encoding="utf-8")
    started: list[dict[str, Any]] = []
    return WorkspaceAgent(actions, model_call=model_call, job_starter=lambda project, req: started.append(req) or {"id": "job_1"}), prompts, started


GENERATE = {"tool": "generate", "path": "hero.md", "action": "candidates"}


def test_a_long_brief_reaches_the_model_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, prompts, _ = _bot(tmp_path, monkeypatch, [{"reply": "Read it.", "actions": [], "done": True}])
    session = bot.create_session("p", title="t")
    brief = "wall_low " * 2000 + "THE LAST LINE"
    asyncio.run(bot.send("p", session["id"], brief))
    assert "THE LAST LINE" in prompts[0]


def test_one_reply_can_write_any_number_of_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes = [{"tool": "write", "path": f"props/p{i}.md", "content": "x"} for i in range(60)]
    bot, _, _ = _bot(tmp_path, monkeypatch, [{"reply": "Wrote them.", "actions": writes, "done": True}])
    session = bot.create_session("p", title="t")
    out = asyncio.run(bot.send("p", session["id"], "go"))
    assert len(out["messages"][-1]["actions"]) == 60
    assert len(list((bot._ws("p") / "props").glob("*.md"))) == 60


def test_running_out_of_steps_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _, _ = _bot(tmp_path, monkeypatch, [{"reply": "Working.", "actions": [], "done": False}] * 50)
    session = bot.create_session("p", title="t")
    out = asyncio.run(bot.send("p", session["id"], "go"))
    assert "isn't finished" in out["messages"][-1]["text"]


def test_a_chat_message_stops_at_the_limit_and_the_next_one_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = [GENERATE] * (MAX_GENERATIONS_PER_TURN + 3)
    bot, _, started = _bot(tmp_path, monkeypatch, [
        {"reply": "Generating.", "actions": calls, "done": True},
        {"reply": "More.", "actions": [GENERATE], "done": True},
    ])
    session = bot.create_session("p", title="t")
    out = asyncio.run(bot.send("p", session["id"], "go"))
    assert len(started) == MAX_GENERATIONS_PER_TURN
    assert [a["ok"] for a in out["messages"][-1]["actions"]].count(False) == 3
    asyncio.run(bot.send("p", session["id"], "yes, continue"))
    assert len(started) == MAX_GENERATIONS_PER_TURN + 1


def test_the_count_includes_each_image_asked_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _, started = _bot(tmp_path, monkeypatch)
    bot.run_tool("p", {**GENERATE, "count": MAX_GENERATIONS_PER_TURN})
    with pytest.raises(ValueError, match="Not started"):
        bot.run_tool("p", GENERATE)
    assert len(started) == 1


def test_a_generation_that_could_not_start_costs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _, _ = _bot(tmp_path, monkeypatch)

    def broken(project: str, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("no key")

    bot.job_starter = broken
    for _ in range(MAX_GENERATIONS_PER_TURN + 2):
        with pytest.raises(RuntimeError):
            bot.run_tool("p", GENERATE)
    bot.job_starter = lambda project, req: {"id": "job_1"}
    bot.run_tool("p", GENERATE)


def test_mcp_generate_counts_too_and_frees_up_after_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _, started = _bot(tmp_path, monkeypatch)
    mcp = ForgeMCPDispatcher(action_layer=bot.actions, workspace_agent=bot)
    args = {"project": "p", "path": "hero.md", "action": "candidates"}
    for _ in range(MAX_GENERATIONS_PER_TURN):
        asyncio.run(mcp.call_tool("generate", args))
    with pytest.raises(ValueError, match="Not started"):
        asyncio.run(mcp.call_tool("generate", args))
    now = workspace_agent._now()
    monkeypatch.setattr(workspace_agent, "_now", lambda: now + SPEND_WINDOW_SECONDS + 1)
    asyncio.run(mcp.call_tool("generate", args))
    assert len(started) == MAX_GENERATIONS_PER_TURN + 1


def test_paid_service_calls_count_and_polling_does_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _, _ = _bot(tmp_path, monkeypatch)
    ran: list[str] = []
    monkeypatch.setattr(services, "call_tool", lambda service, name, arguments: ran.append(name) or "ok")
    for _ in range(MAX_GENERATIONS_PER_TURN * 2):
        bot.run_tool("p", {"tool": "service_call", "service": "elevenlabs", "name": "creative_get_flow_run_status"})
        bot.run_tool("p", {"tool": "service_call", "service": "higgsfield", "name": "job_status"})
    for _ in range(MAX_GENERATIONS_PER_TURN):
        bot.run_tool("p", {"tool": "service_call", "service": "elevenlabs", "name": "creative_generate_image"})
    with pytest.raises(ValueError, match="Not started"):
        bot.run_tool("p", {"tool": "service_call", "service": "elevenlabs", "name": "creative_generate_image"})
    assert ran.count("creative_generate_image") == MAX_GENERATIONS_PER_TURN


def test_the_prompt_says_where_levels_go() -> None:
    prompt = workspace_agent.glob_prompt("p")
    assert "It lists every page with `kind: level`" in prompt and "Never create a section" in prompt
    assert "places are never a section" in prompt and "There is no fixed structure" not in prompt
    assert "belongs to no floor or area" in prompt  # floors with exact piece sizes were still turning into kits

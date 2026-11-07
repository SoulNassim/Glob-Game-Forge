"""The chat shows what it is doing while it works: its thinking, what it says, and each tool as it runs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from forge.agent import cli_models
from forge.agent.action_layer import ForgeActionLayer
from forge.agent.workspace_agent import WorkspaceAgent


def _delta(kind: str, **fields: Any) -> str:
    return json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": kind, **fields}}})


def _fed(*lines: str) -> dict[str, Any]:
    seen: list[dict[str, Any]] = []
    progress = cli_models._Progress(seen.append)
    for line in lines:
        progress.last = -1e9  # no throttling in the test
        progress.feed(line)
    return seen[-1]


def test_thinking_text_reaches_the_chat() -> None:
    last = _fed(_delta("thinking_delta", thinking="Working out the chain physics."))
    assert last["phase"] == "thinking" and "chain physics" in last["thought"]


def test_empty_thinking_still_counts_as_thinking() -> None:
    last = _fed(_delta("thinking_delta", thinking=""))
    assert last["phase"] == "thinking" and last["thought"] == ""


def test_the_reply_and_the_file_being_written_are_read_from_the_stream() -> None:
    last = _fed(
        _delta("text_delta", text='{"reply": "Writing the \\"Core'),
        _delta("text_delta", text=' loop\\" game.", "actions": [{"tool": "write", "path": "prototype/core-'),
        _delta("text_delta", text='loop/index.html", "content": "<html>'),
    )
    assert last["say"] == 'Writing the "Core loop" game.'
    assert last["doing"] == {"tool": "write", "path": "prototype/core-loop/index.html"}


def test_the_file_stays_named_after_it_scrolls_out_of_the_window() -> None:
    last = _fed(
        _delta("text_delta", text='{"reply": "x", "actions": [{"tool": "write", "path": "a/b.html", "content": "'),
        _delta("text_delta", text="y" * 2000),
    )
    assert last["doing"]["path"] == "a/b.html"


def test_claude_is_asked_for_its_thinking_and_old_versions_are_spared(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(cli_models._thinking_display, "ok", True)
    cmd, _ = cli_models.build_command("claude", "claude", "sonnet", "high", "be Glob", "hi", [], tmp_path)
    assert cmd[cmd.index("--thinking-display") + 1] == "summarized"
    monkeypatch.setitem(cli_models._thinking_display, "ok", False)
    cmd, _ = cli_models.build_command("claude", "claude", "sonnet", "high", "be Glob", "hi", [], tmp_path)
    assert "--thinking-display" not in cmd


def _run_chat(tmp_path: Path, monkeypatch, replies: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))
    prompts: list[str] = []
    answers = iter(replies)

    async def model_call(**kwargs: Any) -> dict[str, Any]:
        prompts.append(kwargs["prompt"])
        return {"text": json.dumps(next(answers)), "model": "test"}

    actions = ForgeActionLayer(root_dir=tmp_path / "projects")
    actions.get_workspace("p")
    bot = WorkspaceAgent(actions, model_call=model_call)  # type: ignore[arg-type]
    session = bot.create_session("p", title="t")
    seen: list[dict[str, Any]] = []
    token = cli_models.progress_hook.set(seen.append)
    try:
        out = asyncio.run(bot.send("p", session["id"], "make it"))
    finally:
        cli_models.progress_hook.reset(token)
    return seen, prompts, out


def test_each_tool_is_reported_before_and_after_it_runs(tmp_path: Path, monkeypatch) -> None:
    seen, _, _ = _run_chat(tmp_path, monkeypatch, [
        {"reply": "Writing the page.", "actions": [{"tool": "write", "path": "notes.md", "content": "hi"}], "done": True},
    ])
    assert seen[0]["step"] == {"say": "Writing the page."}
    assert seen[1]["step"] == {"tool": "write", "path": "notes.md", "state": "running"}
    assert seen[2]["step_done"]["state"] == "done" and "notes.md" in seen[2]["step_done"]["summary"]


def test_a_failed_tool_is_reported_as_failed(tmp_path: Path, monkeypatch) -> None:
    seen, _, _ = _run_chat(tmp_path, monkeypatch, [
        {"reply": "Reading.", "actions": [{"tool": "read", "path": "missing.md"}], "done": True},
    ])
    assert seen[-1]["step_done"]["state"] == "failed"


def test_a_plan_is_shown_at_once_and_remembered_in_the_next_round(tmp_path: Path, monkeypatch) -> None:
    seen, prompts, out = _run_chat(tmp_path, monkeypatch, [
        {"reply": "Plan: a boat tows lanterns.", "actions": [], "done": False},
        {"reply": "Done.", "actions": [{"tool": "write", "path": "game.html", "content": "x"}], "done": True},
    ])
    assert seen[0]["step"] == {"say": "Plan: a boat tows lanterns."}
    assert "Plan: a boat tows lanterns." in prompts[1]
    assert out["messages"][-1]["text"] == "Done."


def test_done_is_the_last_progress_message(tmp_path: Path, monkeypatch) -> None:
    """A step report queued just before the answer must not arrive after "done" and leave the chat stuck on "working"."""
    from forge.server import app as server

    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))

    async def model_call(**kwargs: Any) -> dict[str, Any]:
        return {"text": json.dumps({"reply": "Writing.", "actions": [{"tool": "write", "path": "n.md", "content": "x"}], "done": True}), "model": "test"}

    actions = ForgeActionLayer(root_dir=tmp_path / "projects")
    actions.get_workspace("p")
    bot = WorkspaceAgent(actions, model_call=model_call)  # type: ignore[arg-type]
    session = bot.create_session("p", title="t")
    sent: list[dict[str, Any]] = []

    async def broadcast(message: dict[str, Any]) -> None:
        sent.append(message)

    async def changed(project: str) -> None:
        return None

    monkeypatch.setattr(server, "workspace_agent", bot)
    monkeypatch.setattr(server.ws_manager, "broadcast", broadcast)
    monkeypatch.setattr(server, "workspace_changed", changed)

    async def run() -> None:
        await server.api_agent_send(session["id"], {"text": "go"}, project="p")
        await asyncio.sleep(0.05)  # let anything still queued run

    asyncio.run(run())
    progress = [m for m in sent if m.get("type") == "agent_progress"]
    assert progress[-1].get("done") is True

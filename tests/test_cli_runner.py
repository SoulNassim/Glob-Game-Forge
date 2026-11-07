"""The Glob chat as the real Claude Code: its flags and rules, its live steps, Glob's own tools, and carrying on a chat."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge import access
from forge.agent import cli_models, cli_runner
from forge.agent.action_layer import ForgeActionLayer
from forge.agent.workspace_agent import MAX_GENERATIONS_PER_TURN, WorkspaceAgent
from forge.config import save_config


@pytest.fixture
def bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkspaceAgent:
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))
    actions = ForgeActionLayer(root_dir=tmp_path / "projects")
    ws = actions.get_workspace("p").ws_dir
    (ws / "hero.md").write_text("---\nkind: character\nprompt: A knight\n---\n# Hero\n", encoding="utf-8")
    return WorkspaceAgent(actions, model_call=None, job_starter=lambda project, req: {"id": "job_1"})  # type: ignore[arg-type]


def test_it_always_runs_restricted_with_no_shell_and_glob_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))
    cmd, stdin_text = cli_runner.build_command("claude", "sonnet", "high", tmp_path, tmp_path, "be Glob", "hi", [], "http://127.0.0.1:1/api/agent/mcp", "abc")
    assert "--restricted" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert cmd[cmd.index("--resume") + 1] == "abc"
    assert "--strict-mcp-config" in cmd and "--add-dir" not in cmd
    assert json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]["glob"]["url"].endswith("/api/agent/mcp")
    assert (tmp_path / "system.md").read_text() == "be Glob"
    assert json.loads(stdin_text)["message"]["content"][-1]["text"] == "hi"


def test_only_folders_that_exist_and_are_granted_are_added(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))
    game = tmp_path / "game"
    game.mkdir()
    access.grant(str(game))
    cmd, _ = cli_runner.build_command("claude", "", "", tmp_path, tmp_path, "s", "p", [], "http://x", "")
    assert cmd[cmd.index("--add-dir") + 1] == str(game.resolve())
    rules = json.loads((tmp_path / "settings.json").read_text())["permissions"]
    assert "--resume" not in cmd and "mcp__glob" in rules["allow"]


def test_rules_keep_what_access_promises() -> None:
    ws, config = Path("D:/Projects/p/workspace"), Path("C:/Users/me/.forge")
    rules = cli_runner.permission_rules(ws, [{"path": "D:/Games/Read Only", "write": False}, {"path": "D:/Games/Mine", "write": True}], config)
    deny = rules["deny"]
    assert "Edit(//d/Projects/p/workspace/inbox/**)" in deny  # the chats themselves
    assert "Read(//c/Users/me/.forge/**)" in deny and "Grep(//c/Users/me/.forge/**)" in deny  # the API keys
    assert "Edit(//d/Games/Read Only/**)" in deny and "Write(//d/Games/Read Only/**)" in deny
    assert "Edit(//d/Games/Mine/**)" not in deny
    assert "Read(//d/Games/Mine/**/.env)" in deny and "Read(//d/Games/Read Only/**/*.key)" in deny
    assert not any("workspace/**/" in rule for rule in deny)  # "secret-door.md" is a fine name for an asset


def _events(tmp_path: Path) -> tuple[cli_runner.Events, list[dict[str, Any]], list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    tool_actions: list[dict[str, Any]] = []

    def drain() -> list[dict[str, Any]]:
        taken = list(tool_actions)
        tool_actions.clear()
        return taken

    return cli_runner.Events(tmp_path, seen.append, drain), seen, tool_actions


def _line(**data: Any) -> str:
    return json.dumps(data)


def test_tools_become_live_steps_and_changes(tmp_path: Path) -> None:
    events, seen, _ = _events(tmp_path)
    target = str(tmp_path / "levels" / "cells.md")
    events.feed(_line(type="system", subtype="init", session_id="s1"))
    events.feed(_line(type="assistant", message={"content": [{"type": "text", "text": "Making the page."}]}))
    events.feed(_line(type="assistant", message={"content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": target, "content": "x"}}]}))
    events.feed(_line(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}))
    events.feed(_line(type="result", session_id="s1", is_error=False, result="Done."))
    assert seen[0]["step"] == {"say": "Making the page."}
    assert seen[1]["step"] == {"id": "t1", "tool": "write", "path": "levels/cells.md", "state": "running"}
    assert seen[2]["step_done"] == {"id": "t1", "summary": "Wrote levels/cells.md", "state": "done"}
    assert events.actions == [{"tool": "write", "path": "levels/cells.md", "summary": "Wrote levels/cells.md", "ok": True, "changed": True}]
    assert (events.session_id, events.reply, events.error) == ("s1", "Done.", "")


def test_a_refused_file_shows_as_failed_and_changes_nothing(tmp_path: Path) -> None:
    events, seen, _ = _events(tmp_path)
    events.feed(_line(type="assistant", message={"content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "D:/elsewhere/x.md"}}]}))
    events.feed(_line(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": [{"type": "text", "text": "denied"}]}]}))
    assert seen[-1]["step_done"]["state"] == "failed"
    assert events.actions[0]["ok"] is False and "changed" not in events.actions[0] and "denied" in events.actions[0]["summary"]


def test_a_glob_tool_is_listed_once_with_what_it_reported(tmp_path: Path) -> None:
    events, seen, tool_actions = _events(tmp_path)
    events.feed(_line(type="assistant", message={"content": [{"type": "tool_use", "id": "t1", "name": "mcp__glob__generate", "input": {"path": "hero.md"}}]}))
    tool_actions.append({"tool": "generate", "path": "hero.md", "summary": "Started candidates for hero.md", "changed": True, "ok": True})
    events.feed(_line(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "started"}]}))
    assert [a["summary"] for a in events.actions] == ["Started candidates for hero.md"]
    assert seen[0]["step"]["tool"] == "generate" and seen[-1]["step_done"]["summary"] == "Started candidates for hero.md"


def test_a_failed_run_keeps_its_reason(tmp_path: Path) -> None:
    events, _, _ = _events(tmp_path)
    events.feed(_line(type="result", is_error=True, result="Usage limit reached"))
    assert events.error == "Usage limit reached" and not events.reply


def test_a_levels_grid_reaches_the_kit_its_pack_makes(tmp_path: Path) -> None:
    from forge import level_pack
    from forge.asset_studio import read_asset

    level = tmp_path / "levels" / "cells.md"
    level.parent.mkdir()
    level.write_text("---\nkind: level\nprompt: Cells\nkit_cell: 3\nkit_height: 3.4\nkit_thickness: 0.4\n---\n# Cells\n", encoding="utf-8")
    kit = read_asset(tmp_path, level_pack._build_kit(tmp_path, level))[1]
    assert (kit["kind"], kit["kit_cell"], kit["kit_height"], kit["kit_thickness"]) == ("kit", 3, 3.4, 0.4)


def _rpc(bot: WorkspaceAgent, method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    return asyncio.run(cli_runner.handle_rpc(bot, "p", "chat_x", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}))


def test_mcp_handshake_and_tool_list(bot: WorkspaceAgent) -> None:
    hello = _rpc(bot, "initialize", {"protocolVersion": "2025-06-18"})
    assert hello["result"]["protocolVersion"] == "2025-06-18" and "tools" in hello["result"]["capabilities"]
    assert asyncio.run(cli_runner.handle_rpc(bot, "p", "chat_x", {"jsonrpc": "2.0", "method": "notifications/initialized"})) is None
    names = {t["name"] for t in _rpc(bot, "tools/list")["result"]["tools"]}
    assert {"generate", "delete", "move", "request_access", "service_call"} <= names
    assert not names & {"read", "write", "edit"}  # those are the CLI's own


def test_generate_over_mcp_stops_at_the_shared_limit(bot: WorkspaceAgent) -> None:
    args = {"name": "generate", "arguments": {"path": "hero.md", "action": "candidates"}}
    for _ in range(MAX_GENERATIONS_PER_TURN):
        assert _rpc(bot, "tools/call", args)["result"]["isError"] is False
    refused = _rpc(bot, "tools/call", args)["result"]
    assert refused["isError"] is True and "Not started" in refused["content"][0]["text"]


def test_only_listed_tools_can_be_called(bot: WorkspaceAgent) -> None:
    refused = _rpc(bot, "tools/call", {"name": "write", "arguments": {"path": "x.md", "content": "y"}})["result"]
    assert refused["isError"] is True
    assert not (bot._ws("p") / "x.md").exists()


def test_delete_goes_to_the_trash_and_is_reported_in_the_run(bot: WorkspaceAgent) -> None:
    bot._runs["chat_x"] = {"actions": [], "proc": None, "stopped": False}
    assert _rpc(bot, "tools/call", {"name": "delete", "arguments": {"path": "hero.md"}})["result"]["isError"] is False
    assert not (bot._ws("p") / "hero.md").exists()
    assert bot._runs["chat_x"]["actions"][0]["ok"] is True


def _native(monkeypatch: pytest.MonkeyPatch, *results: Any) -> list[dict[str, Any]]:
    """The chat with the CLI replaced: each run takes the next result and records what it was given."""
    calls: list[dict[str, Any]] = []
    queue = list(results)
    save_config({"agent_native_cli": True})

    def fake_run(ws, model, effort, system, prompt, images, mcp_url, resume, report, drain, on_start=None):  # noqa: ANN001
        calls.append({"model": model, "effort": effort, "system": system, "prompt": prompt, "mcp_url": mcp_url, "resume": resume})
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cli_runner, "run", fake_run)
    return calls


def _result(reply: str = "Done.", session_id: str = "cli-1", actions: list[dict[str, Any]] | None = None, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(reply=reply, session_id=session_id, actions=actions or [], error=error)


def _chat(bot: WorkspaceAgent, model: str = "cli:claude/sonnet") -> str:
    return bot.create_session("p", title="t", model=model)["id"]


def test_a_chat_goes_on_in_the_same_conversation(bot: WorkspaceAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _native(monkeypatch, _result(), _result())
    chat = _chat(bot)
    out = asyncio.run(bot.send("p", chat, "first", mcp_base="http://127.0.0.1:9/"))
    assert out["cli_session"] == "cli-1" and out["messages"][-1]["text"] == "Done." and out["context_tokens"] == 0
    asyncio.run(bot.send("p", chat, "second"))
    assert calls[0]["resume"] == "" and calls[1]["resume"] == "cli-1" and calls[0]["model"] == "sonnet"
    assert f"session={chat}" in calls[0]["mcp_url"] and calls[0]["mcp_url"].startswith("http://127.0.0.1:9/api/agent/mcp?project=p")
    assert "## Workspace tree" in calls[0]["prompt"] and "## Workspace tree" not in calls[1]["prompt"]
    assert "Tools in this session" in calls[0]["system"] and "# The chat so far" not in calls[1]["prompt"]


def test_a_lost_conversation_starts_over_with_the_chat_so_far(bot: WorkspaceAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _native(monkeypatch, _result(), cli_runner.ResumeFailed("No conversation found"), _result(session_id="cli-2"))
    chat = _chat(bot)
    asyncio.run(bot.send("p", chat, "first message"))
    out = asyncio.run(bot.send("p", chat, "second message"))
    assert [c["resume"] for c in calls] == ["", "cli-1", ""]
    assert "first message" in calls[2]["prompt"] and out["cli_session"] == "cli-2"


def test_an_older_chat_brings_its_messages_along(bot: WorkspaceAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _native(monkeypatch, _result())
    chat = bot.create_session("p", title="t", model="cli:claude/sonnet", messages=[{"role": "user", "text": "make it dark", "at": 1}])["id"]
    asyncio.run(bot.send("p", chat, "go on"))
    assert "make it dark" in calls[0]["prompt"].split("# Message from the user")[0]


def test_a_failed_run_says_so_and_keeps_what_was_done(bot: WorkspaceAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    done = [{"tool": "write", "path": "a.md", "summary": "Wrote a.md", "ok": True, "changed": True}]
    _native(monkeypatch, _result(reply="", actions=done, error="Usage limit reached"))
    out = asyncio.run(bot.send("p", _chat(bot), "go"))
    last = out["messages"][-1]
    assert "Usage limit reached" in last["text"] and last["actions"] == done


def test_a_run_that_cannot_start_reaches_the_chat(bot: WorkspaceAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    _native(monkeypatch, RuntimeError("Claude Code is not installed."))
    out = asyncio.run(bot.send("p", _chat(bot), "go"))
    assert "not installed" in out["messages"][-1]["text"]


def test_other_models_and_the_setting_off_keep_the_old_chat(bot: WorkspaceAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    async def model_call(**kwargs: Any) -> dict[str, Any]:
        return {"text": json.dumps({"reply": "Old chat.", "actions": [], "done": True}), "model": "test"}

    bot.model_call = model_call
    calls = _native(monkeypatch)
    assert asyncio.run(bot.send("p", _chat(bot, "cli:codex/"), "go"))["messages"][-1]["text"] == "Old chat."
    save_config({"agent_native_cli": False})
    assert asyncio.run(bot.send("p", _chat(bot), "go"))["messages"][-1]["text"] == "Old chat."
    assert not calls


def test_the_old_chat_makes_the_next_native_message_start_with_the_history(bot: WorkspaceAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    async def model_call(**kwargs: Any) -> dict[str, Any]:
        return {"text": json.dumps({"reply": "Old chat.", "actions": [], "done": True}), "model": "test"}

    bot.model_call = model_call
    calls = _native(monkeypatch, _result(), _result(session_id="cli-2"))
    chat = _chat(bot)
    asyncio.run(bot.send("p", chat, "first"))
    save_config({"agent_native_cli": False})
    asyncio.run(bot.send("p", chat, "said in the old chat"))
    save_config({"agent_native_cli": True})
    asyncio.run(bot.send("p", chat, "third"))
    assert calls[1]["resume"] == "" and "said in the old chat" in calls[1]["prompt"]


def test_stop_ends_the_process_and_says_so(bot: WorkspaceAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[Any] = []
    monkeypatch.setattr(cli_models, "kill_tree", killed.append)
    save_config({"agent_native_cli": True})
    chat = _chat(bot)

    def fake_run(ws, model, effort, system, prompt, images, mcp_url, resume, report, drain, on_start=None):  # noqa: ANN001
        proc = object()
        on_start(proc)
        assert bot.stop_run(chat) is True
        assert killed == [proc]
        return _result(reply="", error="killed")

    monkeypatch.setattr(cli_runner, "run", fake_run)
    out = asyncio.run(bot.send("p", chat, "go"))
    assert out["messages"][-1]["text"].startswith("Stopped.")
    assert bot.stop_run(chat) is False  # nothing is running any more

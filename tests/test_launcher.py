"""Starting Glob for a coding agent: open_glob, and the automatic start on the first generate or pick_winner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge import launcher
from forge.mcp import server as mcp_server


@pytest.fixture
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("FORGE_PROJECTS_DIR", str(root))
    monkeypatch.setenv("FORGE_APP_URL", "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.delenv("FORGE_NO_AUTOSTART", raising=False)
    return root


def test_project_url_names_the_project(projects: Path) -> None:
    assert launcher.project_url("pocket kaiju") == "http://127.0.0.1:9/?project=pocket+kaiju"
    assert launcher.project_url("") == "http://127.0.0.1:9/"


def test_open_glob_uses_a_running_studio(projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "_health", lambda base: {"projects_dir": str(projects)})
    monkeypatch.setattr(launcher, "_spawn", lambda *a: pytest.fail("must not start a second Glob"))
    opened: list[str] = []
    monkeypatch.setattr(launcher, "open_window", lambda url: opened.append(url) or True)
    result = launcher.open_glob("demo")
    assert result == {"url": "http://127.0.0.1:9/?project=demo", "started": False, "opened": True, "project": "demo"}
    assert opened == ["http://127.0.0.1:9/?project=demo"]


def test_open_glob_starts_the_studio_when_it_is_closed(projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([None, None, {"projects_dir": str(projects)}])
    monkeypatch.setattr(launcher, "_health", lambda base: next(answers))
    spawned: list[tuple[int, str]] = []
    monkeypatch.setattr(launcher, "_spawn", lambda port, project: spawned.append((port, project)))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(launcher, "open_window", lambda url: True)
    assert launcher.open_glob("demo")["started"] is True
    assert spawned == [(9, "demo")]


def test_another_glob_on_the_port_is_reported(projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "_health", lambda base: {"projects_dir": str(projects.parent / "elsewhere")})
    monkeypatch.setattr(launcher, "_spawn", lambda *a: pytest.fail("the port is taken"))
    with pytest.raises(RuntimeError, match="different projects folder"):
        launcher.ensure_running("demo")


def test_in_app_open_glob_only_opens_the_browser(projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(launcher, "open_glob", lambda project, **kw: calls.append({"project": project, **kw}) or {"url": "u"})
    dispatcher = mcp_server.ForgeMCPDispatcher(in_app=True)
    asyncio.run(dispatcher.call_tool("open_glob", {"project": "demo"}))
    assert calls == [{"project": "demo", "start": False}]
    assert "open_glob" in {tool["name"] for tool in mcp_server.advertised_tools()}


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_an_order_starts_glob_when_it_is_not_running(projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    running = {"up": False}
    started: list[str] = []

    def fake_open(project: str, **kw: Any) -> dict[str, Any]:
        started.append(project)
        running["up"] = True
        return {}

    monkeypatch.setattr(launcher, "is_running", lambda base=None: running["up"])
    monkeypatch.setattr(launcher, "open_glob", fake_open)
    bridge = mcp_server.AppBridge()

    async def run() -> None:
        await bridge.handle(_call("workspace_read", project="demo", path="02-characters/potion.md"))  # reading doesn't start anything
        await bridge.handle(_call("generate", project="demo", path="02-characters/potion.md", action="candidates"))
        await bridge.handle(_call("generate", project="demo", path="02-characters/shield.md", action="candidates"))  # already up
        running["up"] = False  # the user pressed Stop Glob
        await bridge.handle(_call("generate", project="demo", path="02-characters/helmet.md", action="candidates"))

    asyncio.run(run())
    assert started == ["demo", "demo"]


def test_a_start_that_fails_is_not_retried_on_every_call(projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    tries: list[str] = []

    def failing(project: str, **kw: Any) -> dict[str, Any]:
        tries.append(project)
        raise RuntimeError("no")

    monkeypatch.setattr(launcher, "is_running", lambda base=None: False)
    monkeypatch.setattr(launcher, "open_glob", failing)
    bridge = mcp_server.AppBridge()

    async def run() -> None:
        for name in ("potion", "shield"):
            await bridge.handle(_call("generate", project="demo", path=f"02-characters/{name}.md", action="candidates"))

    asyncio.run(run())
    assert tries == ["demo"]


def test_autostart_can_be_switched_off(projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("FORGE_NO_AUTOSTART", "1")
    monkeypatch.setattr(launcher, "open_glob", lambda *a, **kw: pytest.fail("autostart is off"))
    asyncio.run(mcp_server.AppBridge().handle(_call("generate", project="demo", path="02-characters/potion.md", action="candidates")))

"""`glob connect` / `glob open` from a terminal, and where projects live by default."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from forge import cli, launcher
from forge.agent import connect as connect_agent
from forge.config import get_projects_root


@pytest.fixture
def game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FORGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / ".forge"))
    monkeypatch.setattr("forge.shortcut.exists", lambda: True)  # no real desktop shortcuts from tests
    folder = tmp_path / "Pocket Kaiju"
    folder.mkdir()
    return folder


def test_connect_game_makes_the_project_once(game: Path) -> None:
    first = connect_agent.connect_game(str(game), ["claude"])
    assert first["created"] and first["connected"] and first["project"]
    assert (game / "glob").is_dir() and (game / "glob_delivery" / "README.md").is_file()
    entry = json.loads((game / ".mcp.json").read_text())["mcpServers"]["glob"]
    assert entry["args"][-2:] == ["--project", first["project"]]

    again = connect_agent.connect_game(str(game), ["claude"])
    assert not again["created"] and again["project"] == first["project"]


def test_connect_game_needs_a_folder(game: Path) -> None:
    with pytest.raises(ValueError):
        connect_agent.connect_game(str(game / "missing"))


def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> list[dict[str, Any]]:
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(launcher, "open_glob", lambda project, **kw: opened.append({"project": project}) or {"url": f"u/{project}"})
    monkeypatch.setattr(sys, "argv", ["glob", *argv])
    cli.main()
    return opened


def test_cli_connect_then_open(game: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    opened = _run_cli(monkeypatch, "connect", "--game", str(game), "--agents", "codex")
    project = opened[0]["project"]
    assert project and "[mcp_servers.glob]" in (game / ".codex" / "config.toml").read_text()
    assert "Glob is open" in capsys.readouterr().out
    assert _run_cli(monkeypatch, "open", "--game", str(game)) == [{"project": project}]


def test_cli_open_on_an_unconnected_folder_opens_glob(game: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_cli(monkeypatch, "open", "--game", str(game)) == [{"project": ""}]


def test_projects_default_to_the_home_folder_outside_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_PROJECTS_DIR", raising=False)
    monkeypatch.setattr("forge.config.configured_projects_dir", lambda: "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    game = tmp_path / "game"
    (game / "projects").mkdir(parents=True)  # the game's own folder called projects/
    monkeypatch.chdir(game)
    assert get_projects_root() == tmp_path / "home" / "Glob" / "projects"

    (game / "projects" / "old").mkdir()
    (game / "projects" / "old" / "project.json").write_text("{}")  # Glob projects made here before
    assert get_projects_root() == game / "projects"

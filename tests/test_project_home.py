"""Projects that live in their game's folder (`<game>/glob/`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FORGE_PROJECTS_DIR", str(tmp_path / "projects"))
    from forge.server.app import app

    return TestClient(app, base_url="http://127.0.0.1:8420")


def test_new_project_lives_in_its_game_folder(client: TestClient, tmp_path: Path) -> None:
    game = tmp_path / "MyGame"
    game.mkdir()
    created = client.post("/api/projects", json={"name": "My Game", "unique": True, "game_folder": str(game)}).json()
    assert created["project"] == "My_Game" and not created["existing"]
    home = game / "glob"
    assert (home / "workspace").is_dir() and (home / ".gdignore").is_file() and (home / "manifest.db").is_file()
    assert client.get("/api/storage", params={"project": "My_Game"}).json()["workspace_dir"] == str((home / "workspace").resolve())
    # Linked to the game straight away: the coding agent finds Glob without a separate connect step.
    status = client.get("/api/connect", params={"project": "My_Game"}).json()
    assert status["connected"] and Path(status["game_folder"]) == game.resolve()
    assert (game / "AGENTS.md").is_file() and (game / "glob" / "ASSETS.md").is_file()  # the note and the index, no MCP config

    # Choosing the same game again opens its project instead of making a second one.
    again = client.post("/api/projects", json={"name": "Other", "unique": True, "game_folder": str(game)}).json()
    assert again == again | {"project": "My_Game", "existing": True}


def test_game_folder_must_exist(client: TestClient, tmp_path: Path) -> None:
    resp = client.post("/api/projects", json={"name": "x", "game_folder": str(tmp_path / "missing")})
    assert resp.status_code == 400


def test_deleting_a_project_keeps_the_game_files(client: TestClient, tmp_path: Path) -> None:
    game = tmp_path / "Game"
    game.mkdir()
    client.post("/api/projects", json={"name": "Keep", "game_folder": str(game)})
    assert client.delete("/api/projects/Keep").status_code == 200
    assert (game / "glob" / "workspace").is_dir()
    assert "Keep" not in client.get("/api/projects").json()["projects"]


def test_glob_files_can_live_outside_the_game(client: TestClient, tmp_path: Path) -> None:
    game, docs = tmp_path / "Game", tmp_path / "Documents"
    game.mkdir()
    docs.mkdir()
    created = client.post("/api/projects", json={"name": "Far", "unique": True, "game_folder": str(game), "home_folder": str(docs)}).json()
    assert Path(created["home"]) == (docs / "Far").resolve() and (docs / "Far" / "workspace").is_dir()
    assert not (game / "glob").exists() and not (docs / "Far" / ".gdignore").exists()
    assert client.get("/api/connect", params={"project": "Far"}).json()["connected"]
    # The game is still recognised as having a project.
    assert client.post("/api/projects", json={"name": "Again", "unique": True, "game_folder": str(game)}).json()["project"] == "Far"


def test_voice_asset_remembers_the_picked_voice(client: TestClient, tmp_path: Path) -> None:
    game = tmp_path / "Voices"
    game.mkdir()
    project = client.post("/api/projects", json={"name": "Voices", "unique": True, "game_folder": str(game)}).json()["project"]
    client.put("/api/overview/file", params={"project": project}, json={"path": "sound/hype.md", "content": "---\nkind: voice\nprompt: GO!\n---\n# Hype\n"})
    client.patch("/api/studio/asset", params={"project": project}, json={"path": "sound/hype.md", "fields": {"voice": "V1", "voice_name": "Tyler", "secret": "x"}})
    asset = client.get("/api/studio/asset", params={"project": project, "path": "sound/hype.md"}).json()
    assert asset["meta"]["voice"] == "V1" and asset["meta"]["voice_name"] == "Tyler" and "secret" not in asset["meta"]
    assert client.post("/api/studio/preview", params={"project": project}, json={"path": "sound/hype.md"}).json()["settings"]["voice_id"] == "V1"

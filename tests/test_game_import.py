"""Importing a game project, folder access grants, and project archives."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from forge import access, game_import
from forge.agent.action_layer import ForgeActionLayer
from forge.blueprint_contract import split_frontmatter
from forge.storage import workspace_archive


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FORGE_DEMO_MODE", "1")
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "cfg"))


def _png(path: Path, color: tuple[int, int, int] = (200, 40, 60)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path)


def _file(path: Path, data: bytes = b"model") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _godot_game(root: Path) -> Path:
    game = root / "MyGame"
    _file(game / "project.godot", b'config_version=5\n[application]\nconfig/name="Ashfall"\nconfig/features=PackedStringArray("4.3")\n')
    _file(game / "resources/characters/mira.tres", (
        '[gd_resource type="Resource" script_class="PlayableCharacter" format=3]\n'
        '[ext_resource type="PackedScene" path="res://scenes/player/Mira.tscn" id="2"]\n'
        '[resource]\nid = &"mira"\ndisplay_name = "Mira"\nclass_title = "The Scout"\ndescription = "Quick and quiet."\n'
    ).encode())
    _file(game / "resources/items/iron_mace.tres", (
        '[gd_resource type="Resource" script_class="WeaponData" format=3]\n[resource]\n'
        'id = &"iron_mace"\ndisplay_name = "Iron Mace"\ndescription = "Heavy."\ndamage = 40.0\n'
    ).encode())
    _file(game / "resources/items/katana.tres", (
        '[gd_resource type="Resource" script_class="WeaponData" format=3]\n[resource]\n'
        'id = &"katana"\ndisplay_name = "Katana"\ndescription = "Fast blade."\ndamage = 38.0\n'
    ).encode())
    _file(game / "assets/models/Mira/GLB/Mira.glb")
    _png(game / "assets/models/Mira/GLB/Mira_basecolor.png")
    _png(game / "assets/models/Mira/T-Pose IMAGE.png")
    _file(game / "assets/models/Mira/Anims/Walk Forward.fbx")
    _file(game / "assets/models/Mira/Anims/walk.res")
    _file(game / "assets/Items/Katana/katana.glb")
    _png(game / "assets/Items/Katana/katana_0.png")
    for index in range(10):
        _file(game / f"assets/Props/Dungeon/struct_wall_{index}.obj")
    _png(game / "assets/Skys/dusk.png")
    _png(game / "addons/terrain/icon.png")
    _file(game / ".godot/imported/cache.glb")
    _file(game / "assets/models/Mira/GLB/Mira.glb.import", b"[remap]")
    _file(game / ".env", b"SECRET=1")
    _file(game / "Readmes/GDD.md", b"# Ashfall\n\nA dungeon crawler.\n")
    return game


def test_analyze_sorts_a_godot_project(tmp_path: Path) -> None:
    summary = game_import.preview(_godot_game(tmp_path))
    assert summary["engine"] == "godot" and summary["title"] == "Ashfall"
    sections = {s["key"]: {u["title"]: u for u in s["units"]} for s in summary["sections"]}
    mira = sections["characters"]["Mira"]
    assert (mira["models"], mira["animations"], mira["textures"], mira["images"]) == (1, 2, 1, 1)
    assert sections["weapons"]["Katana"]["models"] == 1
    assert sections["weapons"]["Iron Mace"]["shape"] == "data"
    assert sections["environment"]["Dungeon"]["shape"] == "kit"
    assert "docs" in sections
    every = json.dumps(summary)
    assert "terrain" not in every and "cache.glb" not in every


def test_import_writes_assets_and_merges_into_existing_ones(tmp_path: Path) -> None:
    game = _godot_game(tmp_path)
    ws = tmp_path / "ws"
    sketched = ws / "02-characters" / "mira.md"
    sketched.parent.mkdir(parents=True)
    sketched.write_text("---\nkind: character\nprompt: A wiry scout in a green hood\n---\n# Mira\n\nOur scout.\n", encoding="utf-8")

    result = game_import.import_game(game, ws, title="Ashfall")

    assert not any(p.name.startswith(("0", "1")) and p.name.endswith("characters") and p != sketched.parent for p in ws.iterdir())
    meta, body, _ = split_frontmatter(sketched.read_text(encoding="utf-8"))
    assert meta["prompt"] == "A wiry scout in a green hood"
    assert meta["imported"] is True and meta["winner"].endswith("T-Pose IMAGE.png")
    assert "Our scout." in body and "## From the game" in body and "Walk Forward" in body
    assert (ws / "02-characters/mira/3d/Mira.glb").is_file()
    assert (ws / "02-characters/mira/animations/Walk Forward.fbx").is_file()
    assert (ws / "02-characters/mira/animations/walk.res").is_file()
    assert (ws / "02-characters/mira/textures/Mira_basecolor.png").is_file()
    weapons = Path(ws / result["sections"]["weapons"])
    mace_meta, mace_body, _ = split_frontmatter((weapons / "iron-mace.md").read_text(encoding="utf-8"))
    assert mace_meta["status"] == "needs art" and "damage" in mace_body
    katana_meta, katana_body, _ = split_frontmatter((weapons / "katana.md").read_text(encoding="utf-8"))
    assert katana_meta["kind"] == "weapon" and "Fast blade." in katana_body
    assert "<!-- glob-import -->" in (ws / "README.md").read_text(encoding="utf-8")
    assert json.loads((ws / "inbox/import.json").read_text(encoding="utf-8"))["imports"][0]["folder"] == str(game.resolve())
    assert not list(ws.rglob(".env")) and not list(ws.rglob("*.import"))

    # Importing again neither duplicates files nor sections.
    files_before = sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file() and "inbox" not in p.parts)
    game_import.import_game(game, ws)
    files_after = sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file() and "inbox" not in p.parts)
    assert files_before == files_after


def test_access_grants_and_secret_guard(tmp_path: Path) -> None:
    game = _godot_game(tmp_path)
    with pytest.raises(PermissionError, match="request_access"):
        access.check(str(game / "project.godot"))
    access.grant(str(game))
    assert access.check(str(game / "project.godot")).name == "project.godot"
    with pytest.raises(PermissionError, match="secret"):
        access.check(str(game / ".env"))
    with pytest.raises(PermissionError, match="not write"):
        access.check(str(game / "new.txt"), need_write=True)
    access.grant(str(game), write=True)
    assert access.check(str(game / "new.txt"), need_write=True)
    with pytest.raises(ValueError):
        access.grant(Path(game.anchor).as_posix())
    assert access.revoke(str(game)) == []


async def test_agent_asks_for_access_then_imports(tmp_path: Path) -> None:
    from forge.agent.workspace_agent import WorkspaceAgent

    game = _godot_game(tmp_path)
    actions = ForgeActionLayer(root_dir=tmp_path / "projects")
    script = [
        {"reply": "I need your game folder.", "actions": [{"tool": "request_access", "path": str(game), "reason": "to import Mira"}, {"tool": "list", "path": str(game)}]},
        {"reply": "", "actions": [{"tool": "list", "path": str(game)}, {"tool": "import_game", "path": str(game)}], "done": False},
        {"reply": "Imported.", "actions": [{"tool": "copy_in", "from": str(game / "Readmes" / "GDD.md"), "to": "notes"}], "done": True},
    ]

    async def fake_model(**_kwargs: Any) -> dict[str, Any]:
        return {"text": json.dumps(script.pop(0)), "model": "fake"}

    agent = WorkspaceAgent(actions, fake_model)
    session = agent.create_session("severed")
    session = await agent.send("severed", session["id"], "Import my game")
    first = session["messages"][-1]["actions"]
    assert first[0]["access_request"]["path"] == str(game)
    assert first[1]["ok"] is False and "request_access" in first[1]["summary"]

    access.grant(str(game))
    session = await agent.send("severed", session["id"], "Allowed. Go ahead.")
    done = session["messages"][-1]["actions"]
    assert [a["tool"] for a in done] == ["list", "import_game", "copy_in"] and all(a["ok"] for a in done)
    ws = actions.get_workspace("severed").ws_dir
    assert list(ws.rglob("mira.md")) and (ws / "notes" / "GDD.md").is_file()


def test_project_archive_round_trip(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "ashfall"
    _file(project / "workspace" / "README.md", b"# Ashfall\n")
    _png(project / "workspace" / "02-characters" / "mira" / "candidates" / "a.png")
    _file(project / "workspace" / ".trash" / "old.md", b"gone")
    _file(project / "project.json", b"{}")
    archive = tmp_path / "out" / "ashfall.glob.zip"
    result = workspace_archive.export_project(project, archive)
    names = zipfile.ZipFile(archive).namelist()
    assert result["files"] == 3 and "ashfall/workspace/README.md" in names and not any(".trash" in n for n in names)

    name = workspace_archive.import_archive(archive, tmp_path / "projects")
    assert name == "ashfall-2"
    assert (tmp_path / "projects" / "ashfall-2" / "workspace" / "02-characters" / "mira" / "candidates" / "a.png").is_file()

    # A zip of just the workspace content works too; paths escaping the project are dropped.
    bare = tmp_path / "bare.zip"
    with zipfile.ZipFile(bare, "w") as zf:
        zf.writestr("README.md", "# Bare\n")
        zf.writestr("../escape.md", "nope")
    name = workspace_archive.import_archive(bare, tmp_path / "projects")
    assert (tmp_path / "projects" / name / "workspace" / "README.md").is_file()
    assert not (tmp_path / "projects" / "escape.md").exists()


async def test_grants_and_projects_folder_are_not_settable_through_config() -> None:
    from forge.server.app import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8420") as client:
        for key, value in (("access_folders", [{"path": "C:\\", "write": True}]), ("projects_dir", "C:\\")):
            response = await client.put("/api/config", json={key: value})
            assert response.status_code == 403
        assert (await client.put("/api/config", json={"onboarded": True})).json()["onboarded"] is True

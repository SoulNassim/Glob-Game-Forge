"""What a new user meets first: the project's name, a clean workspace, the chat's model call and the key art."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from forge import asset_studio
from forge.agent import cli_models
from forge.agent import connect as connect_agent
from forge.agent.action_layer import ForgeActionLayer
from forge.overview import read_markdown_meta


@pytest.fixture
def godot_game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FORGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / ".forge"))
    game = tmp_path / "GodotGametarky"
    game.mkdir()
    (game / "project.godot").write_text('[application]\nconfig/name="Tarky"\n', encoding="utf-8")
    return game


def test_connect_names_the_project_after_the_game(godot_game: Path, tmp_path: Path) -> None:
    result = connect_agent.connect_game(str(godot_game), ["claude"])
    assert result["project"] == "Tarky"
    store = ForgeActionLayer(root_dir=tmp_path / "projects").get_store("Tarky")
    workspace = store.project_dir / "workspace"
    # No style rules of another game in a new project.
    assert not (workspace / "STYLE_RULES.json").exists() and not (workspace / "PIPELINE.json").exists()


def test_claude_gets_its_system_prompt_from_a_file(tmp_path: Path) -> None:
    long_prompt = "rule " * 5000  # longer than cmd.exe's 8191-character command line
    cmd, _ = cli_models.build_command("claude", "claude", "haiku", "low", long_prompt, "hi", [], tmp_path)
    assert long_prompt not in cmd and "--system-prompt-file" in cmd
    assert (tmp_path / "system.md").read_text(encoding="utf-8") == long_prompt


def test_cli_env_drops_the_parent_agent_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "kept")
    env = cli_models.child_env()
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env and "ANTHROPIC_BASE_URL" not in env
    assert env["ANTHROPIC_API_KEY"] == "kept"


def test_scenes_and_key_art_keep_their_background() -> None:
    cfg = SimpleNamespace(image_remove_bg=True)
    assert asset_studio.cutout_background({"kind": "character", "meta": {}}, cfg)
    assert not asset_studio.cutout_background({"kind": "environment", "meta": {}}, cfg)
    assert not asset_studio.cutout_background({"kind": "prop", "meta": {"cover": True}}, cfg)


def test_default_image_model_falls_back_to_one_with_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    models = [{"id": "gemini-nano-banana-2.1", "available": False}, {"id": "meshy:nano-banana-2", "available": True}]
    monkeypatch.setattr(asset_studio, "list_image_models", lambda: models)
    cfg = SimpleNamespace(default_image_model="gemini-nano-banana-2.1", demo_mode=False)
    assert asset_studio.default_image_model(cfg) == "meshy:nano-banana-2"
    models[0]["available"] = True
    assert asset_studio.default_image_model(cfg) == "gemini-nano-banana-2.1"


def test_long_pitch_is_cut_at_a_word_but_kept_whole_for_key_art(tmp_path: Path) -> None:
    pitch = "A co-op dungeon crawler where " + "the crew cuts monsters apart " * 12 + "and limbs stay lost."
    page = tmp_path / "README.md"
    page.write_text(f"# Tarky\n\n{pitch}\n", encoding="utf-8")
    meta = read_markdown_meta(page)
    assert meta["pitch"] == pitch
    assert meta["excerpt"].endswith("…") and len(meta["excerpt"]) <= 221
    assert pitch.startswith(meta["excerpt"][:-1])


def test_showcase_takes_turns_between_sections(tmp_path: Path) -> None:
    from forge.overview import build_overview

    for section, names in (("01-characters", ["ann", "bob", "cy"]), ("02-weapons", ["axe"])):
        for name in names:
            (tmp_path / section / name / "candidates").mkdir(parents=True)
            (tmp_path / section / name / "candidates" / "a.png").write_bytes(b"png")
            (tmp_path / section / f"{name}.md").write_text(f"---\nkind: character\n---\n# {name.title()}\n", encoding="utf-8")
    titles = [s["title"] for s in build_overview(tmp_path, "p")["showcase"]]
    assert titles == ["Ann", "Axe", "Bob", "Cy"]


def test_shortcut_points_at_glob_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from forge import shortcut

    target = tmp_path / ("Glob.lnk" if sys.platform == "win32" else "glob.desktop")
    monkeypatch.setattr(shortcut, "locations", lambda: [target])
    monkeypatch.setattr(shortcut, "glob_command", lambda: tmp_path / "glob.exe")
    assert shortcut.create() == [target] and shortcut.exists()
    if sys.platform != "win32":
        assert "open" in target.read_text(encoding="utf-8")


def test_each_kind_has_its_own_shape_and_background() -> None:
    cfg = SimpleNamespace(image_aspect_ratio="1:1", image_remove_bg=True, kind_defaults={"prop": {"aspect": "4:3", "background": "scene"}})
    character = {"kind": "character", "meta": {}}
    assert asset_studio.asset_aspect(character, cfg) == "3:4" and asset_studio.cutout_background(character, cfg)
    prop = {"kind": "prop", "meta": {}}
    assert asset_studio.asset_aspect(prop, cfg) == "4:3" and not asset_studio.cutout_background(prop, cfg)
    place = {"kind": "environment", "meta": {"aspect": "21:9", "background": "cutout"}}  # the asset's own choice wins
    assert asset_studio.asset_aspect(place, cfg) == "21:9" and asset_studio.cutout_background(place, cfg)

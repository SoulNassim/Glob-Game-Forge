"""ASSETS.md lists a lighter, game-size copy of big picked images and never touches the original."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from forge import asset_index


def _make(tmp_path: Path, size: int) -> Path:
    ws = tmp_path / "glob" / "workspace"
    (ws / "hero" / "candidates").mkdir(parents=True)
    pic = ws / "hero" / "candidates" / "hero-1.png"
    Image.effect_noise((size, size), 80).convert("RGBA").save(pic)
    (ws / "hero.md").write_text("---\nkind: character\nprompt: a hero\nwinner: hero/candidates/hero-1.png\n---\n# Hero\n", encoding="utf-8")
    return ws


def test_big_winner_gets_a_game_size_copy(tmp_path: Path) -> None:
    ws = _make(tmp_path, 2048)
    text = asset_index.render(ws, "Game")
    copy = tmp_path / "glob" / "exports" / "game" / "hero.png"
    assert copy.is_file()
    with Image.open(copy) as img:
        assert max(img.size) <= asset_index.GAME_SIZE_PX
    with Image.open(ws / "hero" / "candidates" / "hero-1.png") as original:
        assert original.size == (2048, 2048)  # the original is untouched
    assert "game-size copy" in text and "glob/exports/game/hero.png" in text


def test_small_winner_gets_no_copy(tmp_path: Path) -> None:
    ws = _make(tmp_path, 64)
    text = asset_index.render(ws, "Game")
    assert not (tmp_path / "glob" / "exports" / "game" / "hero.png").exists()
    assert "game-size copy (" not in text.split("## ", 1)[1]


def test_project_html_is_served_sandboxed(tmp_path: Path, monkeypatch) -> None:
    """A page an agent wrote (the Prototype) must not run with the origin of Glob's own pages."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FORGE_PROJECTS_DIR", str(tmp_path / "projects"))
    from forge.server.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8420")
    name = client.post("/api/projects", json={"name": "proto-csp", "unique": True}).json()["project"]
    ws = Path(client.get("/api/storage", params={"project": name}).json()["workspace_dir"])
    (ws / "prototype").mkdir(parents=True, exist_ok=True)
    (ws / "prototype" / "index.html").write_text("<canvas></canvas>", encoding="utf-8")
    res = client.get(f"/projects/{name}/workspace/prototype/index.html")
    assert res.status_code == 200
    csp = res.headers["content-security-policy"]
    assert csp.startswith("sandbox") and "allow-same-origin" not in csp


def test_prototype_gets_its_art_built_in(tmp_path: Path, monkeypatch) -> None:
    """The sandboxed prototype can't fetch files, so its picture paths become data URLs when served."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FORGE_PROJECTS_DIR", str(tmp_path / "projects"))
    from forge.server.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8420")
    name = client.post("/api/projects", json={"name": "proto-art", "unique": True}).json()["project"]
    ws = Path(client.get("/api/storage", params={"project": name}).json()["workspace_dir"])
    (ws / "hero").mkdir(parents=True)
    Image.new("RGBA", (900, 900), (255, 0, 0, 255)).save(ws / "hero" / "a.png")
    (ws / "prototype").mkdir()
    (ws / "prototype" / "index.html").write_text(
        "<script>const A='../hero/a.png', B='../hero/missing.png', C='../../../outside.png', D='https://x.test/a.png';</script>", encoding="utf-8")
    body = client.get(f"/projects/{name}/workspace/prototype/index.html").text
    assert "'data:image/png;base64," in body and "'../hero/a.png'" not in body
    assert "'../hero/missing.png'" in body and "'../../../outside.png'" in body and "'https://x.test/a.png'" in body
    assert (ws / "prototype" / "index.html").read_text(encoding="utf-8").count("data:") == 0  # the file itself is untouched

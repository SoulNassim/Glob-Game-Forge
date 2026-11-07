"""A prototype runs sandboxed, so its art has to be inside the page, however the page spells the paths."""

from __future__ import annotations

import base64
from pathlib import Path

from PIL import Image

from forge.prototype import inline_assets


def _project(tmp_path: Path, script: str) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    art = root / "02-characters" / "wick"
    art.mkdir(parents=True)
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(art / "wick.png")
    page = root / "prototype" / "core-loop" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(f"<script>{script}</script>", encoding="utf-8")
    return page, root


def test_a_whole_relative_path_is_put_in_the_page(tmp_path: Path) -> None:
    page, root = _project(tmp_path, "img.src = '../../02-characters/wick/wick.png';")
    body = inline_assets(page, root).decode()
    assert "img.src = 'data:image/png;base64," in body and "wick.png" not in body


def test_a_path_joined_to_a_prefix_is_put_in_the_page_and_the_prefix_goes(tmp_path: Path) -> None:
    page, root = _project(tmp_path, "var P='../../'; var A={p:'02-characters/wick/wick.png'}; img.src = P + A.p;")
    body = inline_assets(page, root).decode()
    assert "var P=''" in body and "p:'data:image/png;base64," in body


def test_a_prefix_stays_when_nothing_was_joined(tmp_path: Path) -> None:
    page, root = _project(tmp_path, "var UP='../../'; img.src = '../../02-characters/wick/wick.png';")
    assert "var UP='../../'" in inline_assets(page, root).decode()


def test_a_missing_file_stays_a_path(tmp_path: Path) -> None:
    page, root = _project(tmp_path, "var P='../../'; var A='02-characters/nobody/nobody.png';")
    body = inline_assets(page, root).decode()
    assert "02-characters/nobody/nobody.png" in body and "var P='../../'" in body


def test_a_3d_model_is_put_in_the_page_unchanged(tmp_path: Path) -> None:
    page, root = _project(tmp_path, "loader.load('../../02-characters/wick/3d/wick.glb');")
    model = root / "02-characters" / "wick" / "3d" / "wick.glb"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"glTF" + bytes(range(256)))
    body = inline_assets(page, root).decode()
    expected = base64.b64encode(model.read_bytes()).decode()
    assert f"loader.load('data:model/gltf-binary;base64,{expected}')" in body


def test_a_model_too_big_to_embed_stays_a_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("forge.prototype.MAX_MODEL_BYTES", 16)
    page, root = _project(tmp_path, "loader.load('../../big.glb');")
    (root / "big.glb").write_bytes(b"glTF" + b"0" * 64)
    assert "'../../big.glb'" in inline_assets(page, root).decode()


def test_a_path_cannot_climb_out_of_the_workspace(tmp_path: Path) -> None:
    page, root = _project(tmp_path, "img.src = '../../../secret.png';")
    Image.new("RGB", (4, 4)).save(tmp_path / "secret.png")
    assert "data:image" not in inline_assets(page, root).decode()

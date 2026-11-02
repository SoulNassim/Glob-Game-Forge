"""Order modes: filing new orders on their own, and how far 2D and full auto take each order."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from forge import order_mode, orders


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    for folder in ("01-game", "02-characters", "03-enemies", "04-sounds"):
        (ws / folder).mkdir(parents=True)
    return ws


def _image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (200, 40, 40)).save(path)


def test_settings_default_to_manual_and_validate(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    assert order_mode.load(ws)["mode"] == "manual"
    assert order_mode.load(ws)["auto_file"] is True
    with pytest.raises(ValueError):
        order_mode.save(ws, {"mode": "turbo"}, 0)
    with pytest.raises(ValueError):
        order_mode.save(ws, {"spend_cap_usd": -1}, 0)


def test_spend_cap_counts_from_when_the_mode_was_set(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    order_mode.save(ws, {"mode": "2d", "spend_cap_usd": 2}, spent_now=10.0)
    assert order_mode.status(ws, 11.0)["spent_usd"] == 1.0 and not order_mode.status(ws, 11.0)["paused"]
    assert order_mode.status(ws, 12.5)["paused"]
    order_mode.save(ws, {"auto_file": False}, spent_now=12.5)  # not the mode or cap: the count goes on
    assert order_mode.status(ws, 12.5)["paused"]
    order_mode.save(ws, {"spend_cap_usd": 5}, spent_now=12.5)  # a new cap starts over
    assert order_mode.status(ws, 12.5)["spent_usd"] == 0 and not order_mode.status(ws, 12.5)["paused"]
    order_mode.save(ws, {"mode": "manual"}, spent_now=100)
    assert not order_mode.status(ws, 500)["paused"]  # manual never pauses


def test_new_orders_are_filed_into_the_matching_section(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    hero = orders.place(ws, "Road warden", "character", "A tired guard")
    hound = orders.place(ws, "Ash hound", "creature", "Burnt dog")
    swing = orders.place(ws, "Sword swing", "sfx", "whoosh")
    lamp = orders.place(ws, "Lantern", "vehicle", "")
    assert sorted(order_mode.file_drafts(ws)) == sorted([hero["id"], hound["id"], swing["id"], lamp["id"]])
    assert orders.get(ws, hero["id"])["asset"] == "02-characters/road-warden.md"
    assert orders.get(ws, hound["id"])["asset"] == "03-enemies/ash-hound.md"
    assert orders.get(ws, swing["id"])["asset"] == "04-sounds/sword-swing.md"
    assert orders.get(ws, lamp["id"])["asset"] == "agent-orders/lantern.md"
    assert orders.get(ws, hero["id"])["status"] == "filed"
    assert order_mode.file_drafts(ws) == []


def test_filing_never_links_an_existing_asset_by_name(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    (ws / "02-characters" / "road-warden.md").write_text("---\nkind: character\nprompt: The real one\n---\n# Road warden\n", encoding="utf-8")
    order = orders.place(ws, "Road warden", "character", "")
    order_mode.file_drafts(ws)
    assert orders.get(ws, order["id"])["asset"] == "02-characters/road-warden-2.md"


def test_an_order_whose_asset_was_deleted_is_not_filed_again(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    order = orders.place(ws, "Road warden", "character", "")
    order_mode.file_drafts(ws)
    (ws / "02-characters" / "road-warden.md").unlink()
    assert orders.get(ws, order["id"])["status"] == "draft"
    assert order_mode.file_drafts(ws) == []


def test_steps_for_each_mode(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    order = orders.place(ws, "Road warden", "character", "A tired guard")
    order_mode.file_drafts(ws)
    view = orders.get(ws, order["id"])
    assert order_mode.next_step(ws, view, "manual") is None
    assert order_mode.next_step(ws, view, "2d") == ("run", {"step": "candidates"})
    order_mode.mark_step(ws, order["id"], "candidates")
    assert order_mode.next_step(ws, orders.get(ws, order["id"]), "2d") is None  # runs once

    _image(ws / "02-characters" / "road-warden" / "candidates" / "first.png")
    view = orders.get(ws, order["id"])
    assert order_mode.next_step(ws, view, "2d") is None  # 2D stops at the candidates
    step = order_mode.next_step(ws, view, "full")
    assert step == ("winner", {"path": "02-characters/road-warden/candidates/first.png"})
    order_mode.pick_winner(ws, view, step[1]["path"])
    assert order_mode.next_step(ws, orders.get(ws, order["id"]), "full") == ("run", {"step": "make_3d"})
    order_mode.mark_step(ws, order["id"], "make_3d")
    assert order_mode.next_step(ws, orders.get(ws, order["id"]), "full") is None


def test_sound_orders_stop_at_the_take(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    order = orders.place(ws, "Sword swing", "sfx", "whoosh")
    order_mode.file_drafts(ws)
    assert order_mode.next_step(ws, orders.get(ws, order["id"]), "full") == ("run", {"step": "candidates"})
    take = ws / "04-sounds" / "sword-swing" / "audio" / "take.mp3"
    take.parent.mkdir(parents=True)
    take.write_bytes(b"ID3")
    view = orders.get(ws, order["id"])
    assert view["status"] == "ready"
    assert order_mode.next_step(ws, view, "full") is None

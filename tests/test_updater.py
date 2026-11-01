"""Update checks against GitHub releases."""

from __future__ import annotations

import pytest

from forge import updater


def test_versions_compare_numerically() -> None:
    assert updater.parse_version("v1.10.0") > updater.parse_version("1.9.3")
    assert updater.parse_version("1.2.0-dev") == (1, 2, 0)
    assert updater.parse_version("nonsense") == (0,)


async def test_check_reports_a_newer_release(monkeypatch: pytest.MonkeyPatch) -> None:
    release = {"version": "9.0.0", "tag": "v9.0.0", "name": "", "notes": "Shiny", "url": "https://example.test/r", "published": "",
               "assets": {"Glob-Setup-Windows.exe": "https://example.test/setup.exe"}}

    async def fake_latest(force: bool = False) -> dict:
        return release

    monkeypatch.setattr(updater, "latest_release", fake_latest)
    monkeypatch.setattr(updater, "install_kind", lambda: "windows-app")
    info = await updater.check()
    assert info["available"] and info["latest"] == "9.0.0" and info["can_install"]

    monkeypatch.setattr(updater, "install_kind", lambda: "source")
    info = await updater.check()
    assert info["available"] and not info["can_install"] and info["command"]

    release["version"] = updater.__version__
    assert not (await updater.check())["available"]


def test_a_dev_checkout_is_not_an_app() -> None:
    assert updater.install_kind() == "source"

"""Every test runs against throwaway folders, never the developer's real projects or settings."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_projects(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    home: Path = tmp_path_factory.mktemp("forge_home")
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(home / "config"))
    monkeypatch.setenv("FORGE_PROJECTS_DIR", str(home / "projects"))

    def forget_open_projects() -> None:
        # The server keeps each open project's location, so one left over from an earlier test would write to its old folder.
        server = sys.modules.get("forge.server.app")
        if server is not None:
            server.actions._stores.clear()
            server.actions._executors.clear()

    forget_open_projects()
    yield
    forget_open_projects()

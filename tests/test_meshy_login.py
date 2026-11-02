"""Meshy through the user's Meshy CLI login instead of an API key, run against a fake `meshy` on PATH."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from forge import meshy_login, services
from forge.config import load_config

FAKE_MESHY = """
import json, os, sys, time
args = sys.argv[1:]
creds = os.path.join(os.environ["MESHY_CONFIG_DIR"], "credentials.json")
def save(token, expires_at):
    with open(creds, "w") as f:
        json.dump({"auth_version": 1, "active_profile": "default", "profiles": {"default": {"kind": "oauth", "access_token": token, "expires_at": expires_at}}}, f)
if args[:2] == ["auth", "login"] and "--no-wait" in args:
    print(json.dumps({"status": "device_flow_started", "verification_url": "https://www.meshy.ai/device?code=ABCD", "flow_id": "f1"}, indent=2))
elif args[:2] == ["auth", "login"] and "--device-flow" in args:
    save("tok-1", time.time() * 1000 + 3600_000)
    print(json.dumps({"status": "logged_in"}))
elif args[:2] == ["auth", "status"]:
    save("tok-2", time.time() * 1000 + 3600_000)
    print(json.dumps({"authenticated": True}))
elif args[:2] == ["auth", "logout"]:
    os.remove(creds)
    print(json.dumps({"status": "deleted"}))
"""


@pytest.fixture
def meshy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if sys.platform == "win32":
        pytest.skip("the fake CLI is a shell script")
    bin_dir, conf = tmp_path / "bin", tmp_path / "meshy"
    bin_dir.mkdir()
    conf.mkdir()
    (bin_dir / "meshy.py").write_text(FAKE_MESHY)
    (bin_dir / "meshy").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "meshy.py"}" "$@"\n')
    (bin_dir / "meshy").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("MESHY_CONFIG_DIR", str(conf))
    monkeypatch.setenv("FORGE_CONFIG_DIR", str(tmp_path / "forge"))
    monkeypatch.delenv("MESHY_API_KEY", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return conf


def test_sign_in_makes_meshy_models_usable(meshy: Path) -> None:
    from forge.asset_studio import list_image_models

    assert not load_config().get_key("meshy")
    assert services.start_sign_in("meshy", "unused") == "https://www.meshy.ai/device?code=ABCD"
    meshy_login._login["proc"].wait(timeout=30)
    assert meshy_login.signed_in() and load_config().get_key("meshy") == "tok-1"

    entry = next(s for s in services.status() if s["id"] == "meshy")
    assert entry["connected"] and entry["use"] == "generators"
    assert "meshy" not in services.connected()  # no chat tools: it powers Glob's own Meshy models
    assert all(m["available"] and m["via"] == "signed in" for m in list_image_models() if m["id"].startswith("meshy:"))
    masked = load_config().masked_dict()
    assert masked["meshy_signed_in"] and not masked["meshy_configured"] and "tok-1" not in json.dumps(masked)

    services.disconnect("meshy")
    assert not meshy_login.signed_in()


def test_expiring_login_is_refreshed_by_the_cli(meshy: Path) -> None:
    (meshy / "credentials.json").write_text(json.dumps({"active_profile": "default", "profiles": {"default": {"kind": "oauth", "access_token": "old", "expires_at": 0}}}))
    assert meshy_login.token() == "tok-2"


def test_a_pasted_key_wins_over_the_sign_in(meshy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (meshy / "credentials.json").write_text(json.dumps({"active_profile": "default", "profiles": {"default": {"kind": "oauth", "access_token": "tok", "expires_at": 9e15}}}))
    monkeypatch.setenv("MESHY_API_KEY", "msy_key")
    assert load_config().get_key("meshy") == "msy_key"

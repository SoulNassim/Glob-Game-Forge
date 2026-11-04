"""Local coding-agent logins for Glob.

Reports which coding agents (Codex, Claude Code) are installed and logged in on this computer,
without ever reading their credentials. Models are called through ``forge.agent.llm``.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any

from forge.config import load_config


def discover_codex_profiles() -> dict[str, Any]:
    """Inspect `~/.codex/` for configured profiles, models, and auth status."""
    codex_dir = Path.home() / ".codex"
    result: dict[str, Any] = {
        "installed": bool(shutil.which("codex")) or codex_dir.exists(),
        "config_dir": str(codex_dir),
        "logged_in": False,
        "active_model": "",
        "active_provider": "",
        "profiles": [],
    }
    if not codex_dir.exists():
        return result

    auth_file = codex_dir / "auth.json"
    if auth_file.exists() and auth_file.stat().st_size > 5:
        result["logged_in"] = True

    for toml_path in sorted(codex_dir.glob("*.toml")):
        try:
            parsed = tomllib.loads(toml_path.read_text(encoding="utf-8-sig", errors="ignore"))
            model = str(parsed.get("model", ""))
            provider = str(parsed.get("model_provider", "openai"))
            providers_map = parsed.get("model_providers") or {}
            base_url = ""
            if provider in providers_map and isinstance(providers_map[provider], dict):
                base_url = str(providers_map[provider].get("base_url", ""))
            result["profiles"].append(
                {
                    "file": toml_path.name,
                    "model": model,
                    "provider": provider,
                    "base_url": base_url,
                }
            )
            if toml_path.name == "config.toml":
                result["active_model"] = model
                result["active_provider"] = provider
                result["logged_in"] = True
        except Exception:
            continue

    return result


def discover_claude_status() -> dict[str, Any]:
    """Inspect `~/.claude/` and Claude Code CLI login state."""
    claude_dir = Path.home() / ".claude"
    has_cli = bool(shutil.which("claude"))
    has_dir = claude_dir.exists()
    creds_exist = False
    if has_dir:
        for candidate in (".credentials.json", "credentials.json", "settings.json", "config.json"):
            if (claude_dir / candidate).exists():
                creds_exist = True
                break
        if not creds_exist and list(claude_dir.glob("*")):
            creds_exist = True

    return {
        "installed": has_cli or has_dir,
        "cli_in_path": has_cli,
        "config_dir": str(claude_dir),
        "logged_in": creds_exist,
    }


def switch_codex_profile(profile_filename: str) -> dict[str, Any]:
    """Switch active `~/.codex/config.toml` to one of the user's saved Codex profile `.toml` files."""
    codex_dir = Path.home() / ".codex"
    src = (codex_dir / Path(profile_filename).name).resolve()
    dest = (codex_dir / "config.toml").resolve()
    if not str(src).startswith(str(codex_dir.resolve())) or not src.exists():
        raise ValueError(f"Codex profile '{profile_filename}' not found.")
    if src != dest:
        shutil.copy2(src, dest)
    return discover_codex_profiles()


def get_all_auth_status() -> dict[str, Any]:
    """Summary of the local Codex and Claude Code logins and which API keys are set."""
    cfg = load_config()
    codex = discover_codex_profiles()
    claude = discover_claude_status()
    return {
        "codex": codex,
        "claude": claude,
        "api_keys_configured": {
            "gemini": bool(cfg.get_key("gemini")),
            "meshy": cfg.has_live_3d_key(),
            "elevenlabs": bool(cfg.get_key("elevenlabs")),
            "openai": bool(cfg.get_key("openai")),
            "anthropic": bool(cfg.get_key("anthropic")),
        },
    }

"""Local configuration and API key management for Forge.

Keys are stored outside project folders (in ~/.forge/config.json, environment variables,
or Windows HKCU Environment registry), never logged, never echoed in full, and never
written to project files or exports.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field


def write_secret_file(path: Path, text: str) -> None:
    """Write a file that holds API keys or tokens, readable only by the current user (POSIX)."""
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_config_dir() -> Path:
    custom = os.environ.get("FORGE_CONFIG_DIR")
    if custom:
        p = Path(custom)
    else:
        p = Path.home() / ".forge"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_projects_root() -> Path:
    custom = os.environ.get("FORGE_PROJECTS_DIR")
    if custom:
        p = Path(custom)
    else:
        p = Path.cwd() / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_win32_user_env(var_name: str) -> str:
    if platform.system().lower() != "windows":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            val, _ = winreg.QueryValueEx(k, var_name)
            return str(val).strip()
    except Exception:
        return ""


class ForgeConfig(BaseModel):
    gemini_api_key: str = ""
    meshy_api_key: str = ""
    elevenlabs_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    default_image_provider: str = "gemini"
    default_image_model: str = "gemini-nano-banana-2.1"
    default_3d_provider: str = "meshy"
    default_3d_model: str = "meshy-7.1"
    default_critic_provider: str = "vscode_copilot"
    default_critic_model: str = "vscode:gpt-6-studierende"

    # Default generation settings
    image_aspect_ratio: str = "1:1"
    image_remove_bg: bool = True
    image_pose_mode: str = ""
    mesh_model_type: str = "standard"
    mesh_geometry_resolution: str = "standard"
    mesh_target_polycount: int = 12000
    mesh_topology: str = "triangle"
    mesh_should_remesh: bool = False
    mesh_should_texture: bool = True
    mesh_enable_pbr: bool = True
    mesh_texture_resolution: str = "2k"
    mesh_pose_mode: str = ""
    mesh_origin_at: str = "bottom"
    mesh_auto_size: bool = False
    mesh_target_formats: list[str] = Field(default_factory=lambda: ["glb", "obj"])

    # Forge Agent dock
    agent_model: str = ""
    agent_reasoning_effort: str = "medium"
    # Long chats: summarize older messages once the transcript passes this many (estimated) tokens.
    agent_auto_compact: bool = True
    agent_compact_at_tokens: int = 48000
    agent_keep_recent_messages: int = 6
    agent_max_tool_rounds: int = 8
    # Coding-agent CLIs may edit workspace files without asking.
    cli_allow_edits: bool = True
    terminal_command: str = "shell"
    handoff_message_threshold: int = 40
    mcp_http_enabled: bool = True

    # Engine project folders used by exports
    godot_project_path: str = ""
    unity_project_path: str = ""
    unreal_project_path: str = ""

    demo_mode: bool = False  # Only True when NO live keys are configured or explicitly forced
    host: str = "127.0.0.1"
    port: int = 8420
    allowed_proxy_hosts: list[str] = Field(
        default_factory=lambda: [
            "assets.meshy.ai",
            "api.meshy.ai",
            "generativelanguage.googleapis.com",
            "lh3.googleusercontent.com",
            "api.elevenlabs.io",
        ]
    )

    def has_live_image_key(self) -> bool:
        return bool(self.get_key("gemini") or self.get_key("meshy") or self.get_key("elevenlabs"))

    def has_live_3d_key(self) -> bool:
        return bool(self.get_key("meshy"))

    def get_key(self, provider: str) -> str:
        env_map = {
            "gemini": "GEMINI_API_KEY",
            "meshy": "MESHY_API_KEY",
            "elevenlabs": "ELEVENLABS_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        attr = f"{provider}_api_key"
        # 1. Explicit key saved in ~/.forge/config.json takes top priority
        saved_val = getattr(self, attr, "").strip()
        if saved_val and not saved_val.startswith("••••"):
            return saved_val
        # 2. Process environment variable
        env_name = env_map.get(provider, "")
        if env_name:
            env_val = os.environ.get(env_name, "").strip()
            if env_val:
                return env_val
            # 3. Windows HKCU Environment Registry fallback
            reg_val = _read_win32_user_env(env_name)
            if reg_val:
                return reg_val
        return ""

    def masked_dict(self) -> dict[str, Any]:
        """Return safe dictionary with keys masked for UI/API responses."""
        data = self.model_dump()
        for k in ("gemini_api_key", "meshy_api_key", "elevenlabs_api_key", "openai_api_key", "anthropic_api_key"):
            provider = k.replace("_api_key", "")
            raw = self.get_key(provider)
            if raw:
                data[k] = f"••••{raw[-4:]}" if len(raw) > 4 else "••••"
                data[f"{provider}_configured"] = True
            else:
                data[k] = ""
                data[f"{provider}_configured"] = False
        return data


def load_config() -> ForgeConfig:
    cfg_file = get_config_dir() / "config.json"
    data: dict[str, Any] = {}
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    cfg = ForgeConfig(**data)
    # Upgrade legacy default model name to gemini-nano-banana-2.1 if not customized
    if cfg.default_image_model == "gemini-2.5-flash-image" and not data.get("user_locked_image_model"):
        cfg.default_image_model = "gemini-nano-banana-2.1"
    # If FORGE_DEMO_MODE=1 is set in env (e.g. during unit tests), force demo_mode
    if os.environ.get("FORGE_DEMO_MODE") == "1":
        cfg.demo_mode = True
    elif (cfg.has_live_image_key() or cfg.has_live_3d_key()) and not data.get("force_demo_mode"):
        cfg.demo_mode = False
    elif not cfg.has_live_image_key() and not cfg.has_live_3d_key():
        cfg.demo_mode = True
    if "api.elevenlabs.io" not in cfg.allowed_proxy_hosts:
        cfg.allowed_proxy_hosts = [*cfg.allowed_proxy_hosts, "api.elevenlabs.io"]
    return cfg


def save_config(updates: dict[str, Any]) -> ForgeConfig:
    cfg = load_config()
    current = cfg.model_dump()
    for k, v in updates.items():
        if k in current:
            if k.endswith("_api_key") and isinstance(v, str) and v.startswith("••••"):
                continue
            current[k] = v
    new_cfg = ForgeConfig(**current)
    cfg_file = get_config_dir() / "config.json"
    try:
        previous = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
    except Exception:
        previous = {}
    force_demo = bool(updates.get("force_demo_mode", previous.get("force_demo_mode", False)))
    # With a live key, demo mode stays off unless the user explicitly forced it
    if new_cfg.has_live_image_key() or new_cfg.has_live_3d_key():
        new_cfg.demo_mode = force_demo
    data = new_cfg.model_dump()
    data["force_demo_mode"] = force_demo
    write_secret_file(cfg_file, json.dumps(data, indent=2))
    return new_cfg

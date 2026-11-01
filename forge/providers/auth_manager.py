"""Unified Provider & CLI Account Hub for Forge.

Bridges existing local developer logins & configurations into Forge without requiring
manual re-entry of API keys:
1. **VS Code Copilot (`chatLanguageModels.json` & GitHub Copilot OAuth)**:
   - Auto-discovers all custom providers and models configured in VS Code's
     `User/chatLanguageModels.json` (including both `chat-completions` and `responses` APIs,
     e.g., `gpt-6-studierende` / `GPT-6 Luna (KIConnect)` and `qwen3.8-27b`).
   - Detects existing GitHub Copilot tokens (`hosts.json` / `apps.json`) and `gh auth status`.
   - Implements GitHub OAuth Device Flow (`https://github.com/login/device/code`) so users can
     log in to GitHub Copilot directly from the Forge UI.
2. **OpenAI Codex CLI (`~/.codex/`)**:
   - Auto-discovers `~/.codex/config.toml`, alternate profile `.toml` files
     (e.g., `studierende.config.toml`, `qwen38.config.toml`), and `~/.codex/auth.json`.
3. **Claude Code CLI (`~/.claude/`)**:
   - Auto-discovers `~/.claude/` credentials and CLI installation.
4. **Live Model Invocation**:
   - Executes chat, agent planning, and multimodal Vision Critic calls directly against
     any discovered VS Code Copilot model (`responses` or `chat-completions` protocol)
     while keeping raw keys strictly server-side.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import time
import tomllib
from hashlib import pbkdf2_hmac
from pathlib import Path
from typing import Any

import httpx

from forge.config import get_config_dir, load_config, save_config, write_secret_file


GITHUB_COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"


def _resolve_vscode_secret_win32(user_dir: Path, secret_key_name: str) -> str:
    """Resolve `${input:chat.lm.secret.*}` from VS Code's `state.vscdb` + `Local State` using Windows DPAPI + AES-GCM."""
    if platform.system().lower() != "windows":
        return ""
    try:
        code_root = user_dir.parent
        local_state_path = code_root / "Local State"
        db_path = user_dir / "globalStorage" / "state.vscdb"
        if not local_state_path.exists() or not db_path.exists():
            return ""

        ls = json.loads(local_state_path.read_text(encoding="utf-8-sig", errors="ignore"))
        enc_key_b64 = ls.get("os_crypt", {}).get("encrypted_key", "")
        if not enc_key_b64:
            return ""
        enc_key = base64.b64decode(enc_key_b64)[5:]  # Strip 'DPAPI' prefix

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf_in = ctypes.create_string_buffer(enc_key, len(enc_key))
        blob_in = DATA_BLOB(len(enc_key), buf_in)
        blob_out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            return ""
        master_key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)

        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (f"secret://{secret_key_name}",),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return ""

        parsed_buf = json.loads(row[0])
        buf = bytes(parsed_buf.get("data", []))
        if len(buf) < 31 or buf[:3] != b"v10":
            return ""

        nonce = buf[3:15]
        ciphertext = buf[15:-16]
        tag = buf[-16:]

        class BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.ULONG),
                ("dwInfoVersion", ctypes.wintypes.ULONG),
                ("pbNonce", ctypes.c_void_p),
                ("cbNonce", ctypes.wintypes.ULONG),
                ("pbAuthData", ctypes.c_void_p),
                ("cbAuthData", ctypes.wintypes.ULONG),
                ("pbTag", ctypes.c_void_p),
                ("cbTag", ctypes.wintypes.ULONG),
                ("pbMacContext", ctypes.c_void_p),
                ("cbMacContext", ctypes.wintypes.ULONG),
                ("cbAAD", ctypes.wintypes.ULONG),
                ("cbData", ctypes.c_ulonglong),
                ("dwFlags", ctypes.wintypes.ULONG),
            ]

        bcrypt = ctypes.windll.bcrypt
        hAlg = ctypes.c_void_p()
        bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(hAlg), ctypes.c_wchar_p("AES"), None, 0)
        chain_mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        bcrypt.BCryptSetProperty(hAlg, ctypes.c_wchar_p("ChainingMode"), chain_mode, ctypes.sizeof(chain_mode), 0)
        hKey = ctypes.c_void_p()
        bcrypt.BCryptGenerateSymmetricKey(
            hAlg, ctypes.byref(hKey), None, 0, ctypes.c_char_p(master_key), len(master_key), 0
        )
        nonce_buf = ctypes.create_string_buffer(nonce, len(nonce))
        tag_buf = ctypes.create_string_buffer(tag, len(tag))
        auth_info = BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO()
        auth_info.cbSize = ctypes.sizeof(auth_info)
        auth_info.dwInfoVersion = 1
        auth_info.pbNonce = ctypes.cast(nonce_buf, ctypes.c_void_p)
        auth_info.pbNonce = ctypes.cast(nonce_buf, ctypes.c_void_p)
        auth_info.cbNonce = len(nonce)
        auth_info.pbTag = ctypes.cast(tag_buf, ctypes.c_void_p)
        auth_info.cbTag = len(tag)
        ct_buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        pt_buf = ctypes.create_string_buffer(len(ciphertext))
        pcbResult = ctypes.wintypes.ULONG()
        status = bcrypt.BCryptDecrypt(
            hKey,
            ct_buf,
            len(ciphertext),
            ctypes.byref(auth_info),
            None,
            0,
            pt_buf,
            len(ciphertext),
            ctypes.byref(pcbResult),
            0,
        )
        bcrypt.BCryptDestroyKey(hKey)
        bcrypt.BCryptCloseAlgorithmProvider(hAlg, 0)
        if status != 0:
            return ""
        return pt_buf.raw[: pcbResult.value].decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


_SECRET_CACHE: dict[str, str] = {}
_SECRET_MISS: set[str] = set()


def _read_vscode_secret_blob(user_dir: Path, secret_key_name: str) -> bytes:
    db_path = user_dir / "globalStorage" / "state.vscdb"
    if not db_path.is_file():
        return b""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (f"secret://{secret_key_name}",),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return b""
    if not row or row[0] is None:
        return b""
    raw = row[0]
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return b""
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, list):
        return b""
    return bytes(data)


def _aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Decrypt Chromium safeStorage ciphertext. Uses CommonCrypto on macOS."""
    if platform.system().lower() != "darwin" or not data:
        return b""
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    lib.CCCrypt.restype = ctypes.c_int
    lib.CCCrypt.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    key_buf = ctypes.create_string_buffer(key, len(key))
    iv_buf = ctypes.create_string_buffer(iv, len(iv))
    in_buf = ctypes.create_string_buffer(data, len(data))
    out = ctypes.create_string_buffer(len(data) + 16)
    moved = ctypes.c_size_t(0)
    status = lib.CCCrypt(
        1,  # kCCDecrypt
        0,  # kCCAlgorithmAES
        1,  # kCCOptionPKCS7Padding
        ctypes.cast(key_buf, ctypes.c_void_p),
        len(key),
        ctypes.cast(iv_buf, ctypes.c_void_p),
        ctypes.cast(in_buf, ctypes.c_void_p),
        len(data),
        ctypes.cast(out, ctypes.c_void_p),
        len(out),
        ctypes.byref(moved),
    )
    if status != 0:
        return b""
    return out.raw[: moved.value]


def _decrypt_chromium_v10(password: str, encrypted: bytes) -> str:
    """Decrypt a VS Code `v10` safeStorage blob (PBKDF2 + AES-128-CBC)."""
    if not password or not encrypted.startswith(b"v10") or len(encrypted) <= 3:
        return ""
    key = pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", 1003, 16)
    plain = _aes128_cbc_decrypt(key, b" " * 16, encrypted[3:])
    return plain.decode("utf-8", errors="ignore").strip("\x00").strip()


def _keychain_password(service: str, account: str) -> str | None:
    """Return the keychain password, '' if that item does not exist, or None if the user dismissed the prompt."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return proc.stdout.strip()
    err = (proc.stderr or "").lower()
    if "could not be found" in err or "not found" in err:
        return ""
    return None


def _resolve_vscode_secret_macos(user_dir: Path, secret_key_name: str) -> str:
    """Resolve `${input:chat.lm.secret.*}` from VS Code's macOS keychain safeStorage."""
    cache_key = f"{user_dir}:{secret_key_name}"
    cached = _SECRET_CACHE.get(cache_key)
    if cached:
        return cached
    if cache_key in _SECRET_MISS:
        return ""
    encrypted = _read_vscode_secret_blob(user_dir, secret_key_name)
    if not encrypted:
        _SECRET_MISS.add(cache_key)
        return ""
    app_name = user_dir.parent.name
    service = f"{app_name} Safe Storage"
    password = ""
    for account in (f"{app_name} Key", app_name, service):
        found = _keychain_password(service, account)
        if found is None:
            _SECRET_MISS.add(cache_key)
            return ""
        if found:
            password = found
            break
    resolved = _decrypt_chromium_v10(password, encrypted)
    if resolved:
        _SECRET_CACHE[cache_key] = resolved
    else:
        _SECRET_MISS.add(cache_key)
    return resolved


def _resolve_vscode_secret(user_dir: Path, secret_key_name: str) -> str:
    system = platform.system().lower()
    if system == "windows":
        return _resolve_vscode_secret_win32(user_dir, secret_key_name)
    if system == "darwin":
        return _resolve_vscode_secret_macos(user_dir, secret_key_name)
    return ""


def get_vscode_user_dirs() -> list[Path]:
    """Return candidate VS Code / Cursor / VSCodium User directories across Windows, macOS, and Linux."""
    candidates: list[Path] = []
    sys_name = platform.system().lower()
    if sys_name == "windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            for ed in ("Code", "Code - Insiders", "Cursor", "VSCodium"):
                candidates.append(Path(appdata) / ed / "User")
    elif sys_name == "darwin":
        base = Path.home() / "Library" / "Application Support"
        for ed in ("Code", "Code - Insiders", "Cursor", "VSCodium"):
            candidates.append(base / ed / "User")
    else:
        base = Path.home() / ".config"
        for ed in ("Code", "Code - Insiders", "Cursor", "VSCodium"):
            candidates.append(base / ed / "User")
    return [c for c in candidates if c.exists()]


def discover_vscode_copilot_models(include_secret_key: bool = False) -> list[dict[str, Any]]:
    """Read VS Code's `chatLanguageModels.json` and return all configured providers & models."""
    discovered: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for user_dir in get_vscode_user_dirs():
        clm_file = user_dir / "chatLanguageModels.json"
        if not clm_file.exists():
            continue
        try:
            raw_list = json.loads(clm_file.read_text(encoding="utf-8-sig", errors="ignore"))
            if not isinstance(raw_list, list):
                continue
            for provider_block in raw_list:
                if not isinstance(provider_block, dict):
                    continue
                prov_name = str(provider_block.get("name", "VS Code Provider"))
                vendor = str(provider_block.get("vendor", "customendpoint"))
                api_type = str(provider_block.get("apiType", "chat-completions"))
                api_key = str(provider_block.get("apiKey", ""))
                # Resolve VS Code ${input:chat.lm.secret.*} from safeStorage.
                # The placeholder itself is not a credential; sending it causes 401s.
                m_input = re.match(r"^\$\{input:([^}]+)\}$", api_key.strip())
                if m_input:
                    if include_secret_key:
                        api_key = _resolve_vscode_secret(user_dir, m_input.group(1))
                    else:
                        api_key = ""
                models = provider_block.get("models") or []

                for m in models:
                    if not isinstance(m, dict):
                        continue
                    mid = str(m.get("id", ""))
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    entry: dict[str, Any] = {
                        "id": f"vscode:{mid}",
                        "model_id": mid,
                        "display_name": str(m.get("name") or mid),
                        "provider_name": prov_name,
                        "vendor": vendor,
                        "api_type": api_type,  # "responses" or "chat-completions"
                        "url": str(m.get("url", "")),
                        "vision": bool(m.get("vision", False)),
                        "tool_calling": bool(m.get("toolCalling", False)),
                        "thinking": bool(m.get("thinking", False)),
                        "source": f"VS Code Copilot ({clm_file.parent.parent.name})",
                        "has_key": bool(api_key) or bool(m_input),
                    }
                    if include_secret_key:
                        entry["api_key"] = api_key
                    discovered.append(entry)
        except Exception:
            continue

    return discovered


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


def discover_github_copilot_status() -> dict[str, Any]:
    """Check GitHub CLI (`gh`) login, VS Code Copilot extension, and cached Copilot OAuth tokens."""
    saved_oauth = _load_saved_oauth_tokens()
    gh_user = ""
    gh_logged_in = False

    # Check saved device flow token first
    if saved_oauth.get("github_access_token"):
        gh_logged_in = True
        gh_user = saved_oauth.get("github_user", "GitHub Copilot User")

    # Check `gh` CLI status if available
    if shutil.which("gh"):
        try:
            proc = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=3.5,
                check=False,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            if "Logged in to github.com" in out:
                gh_logged_in = True
                for line in out.splitlines():
                    if "account" in line:
                        parts = line.split("account", 1)[1].strip().split()
                        if parts:
                            gh_user = parts[0].strip()
                            break
        except Exception:
            pass

    vscode_models = discover_vscode_copilot_models(include_secret_key=False)
    return {
        "connected": gh_logged_in or len(vscode_models) > 0,
        "github_logged_in": gh_logged_in,
        "github_user": gh_user or ("Connected via VS Code" if vscode_models else ""),
        "vscode_models_count": len(vscode_models),
        "vscode_models": vscode_models,
    }


def _oauth_store_path() -> Path:
    return get_config_dir() / "oauth_sessions.json"


def _load_saved_oauth_tokens() -> dict[str, Any]:
    p = _oauth_store_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
    return {}


def _save_oauth_tokens(updates: dict[str, Any]) -> dict[str, Any]:
    curr = _load_saved_oauth_tokens()
    curr.update(updates)
    write_secret_file(_oauth_store_path(), json.dumps(curr, indent=2))
    return curr


async def start_github_copilot_device_flow() -> dict[str, Any]:
    """Initiate GitHub OAuth Device Flow for Copilot login directly inside Forge UI."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://github.com/login/device/code",
            headers={"Accept": "application/json"},
            data={"client_id": GITHUB_COPILOT_CLIENT_ID, "scope": "read:user"},
        )
        resp.raise_for_status()
        data = resp.json()
        _save_oauth_tokens({"pending_device_code": data.get("device_code")})
        return {
            "user_code": data.get("user_code"),
            "verification_uri": data.get("verification_uri", "https://github.com/login/device"),
            "expires_in": data.get("expires_in", 900),
            "interval": data.get("interval", 5),
        }


async def poll_github_copilot_device_flow() -> dict[str, Any]:
    """Poll GitHub OAuth Device Flow and store access token once user authorizes."""
    saved = _load_saved_oauth_tokens()
    device_code = saved.get("pending_device_code")
    if not device_code:
        return {"status": "error", "message": "No pending device code. Start login first."}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_COPILOT_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        data = resp.json()
        if "access_token" in data:
            token = data["access_token"]
            user_resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            username = user_resp.json().get("login", "github_user") if user_resp.status_code == 200 else "github_user"
            _save_oauth_tokens(
                {
                    "github_access_token": token,
                    "github_user": username,
                    "connected_at": time.time(),
                    "pending_device_code": None,
                }
            )
            return {"status": "authorized", "github_user": username}
        err = data.get("error", "authorization_pending")
        return {"status": err, "message": data.get("error_description", "Waiting for browser authorization...")}


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


async def call_vscode_copilot_model(
    prompt: str,
    system_prompt: str = "You are Forge's game asset studio assistant.",
    model_selector: str | None = None,
    image_b64: str | None = None,
    images_b64: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Invoke a model configured in the user's VS Code Copilot (`chatLanguageModels.json`).

    Supports both:
    - OpenAI Responses API (`apiType == "responses"`, e.g. `gpt-6-studierende` / KIConnect Responses)
    - OpenAI Chat Completions API (`apiType == "chat-completions"`, e.g. `qwen3.8-27b` / KIConnect Chat)
    """
    models = discover_vscode_copilot_models(include_secret_key=True)
    if not models:
        raise RuntimeError("No VS Code Copilot models found in chatLanguageModels.json.")

    cfg = load_config()
    preferred = model_selector or cfg.default_critic_model
    chosen = next(
        (m for m in models if m["id"] == preferred or m["model_id"] == preferred),
        models[0],
    )

    url = chosen["url"]
    api_key = str(chosen.get("api_key") or "")
    model_id = chosen["model_id"]
    api_type = chosen.get("api_type", "chat-completions")
    if not api_key or api_key.startswith("${"):
        raise RuntimeError(
            "Forge could not read this model's API key from the macOS keychain. "
            "Approve the Safe Storage prompt, then send the message again."
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    all_images: list[tuple[str, str]] = list(images_b64 or [])
    if image_b64:
        all_images.append(("image/png", image_b64))

    async with httpx.AsyncClient(timeout=65.0) as client:
        if api_type == "responses" or url.endswith("/responses"):
            input_content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
            if all_images and chosen.get("vision"):
                for mime, b64_str in all_images[:4]:
                    input_content.append(
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime};base64,{b64_str}",
                        }
                    )
            body: dict[str, Any] = {
                "model": model_id,
                "instructions": system_prompt,
                "input": [{"role": "user", "content": input_content}],
            }
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            out_text = data.get("output_text", "")
            if not out_text:
                for item in data.get("output", []):
                    if not isinstance(item, dict):
                        continue
                    for c in item.get("content") or []:
                        if isinstance(c, dict) and c.get("type") in ("output_text", "text") and c.get("text"):
                            out_text += c["text"]
            return {
                "text": out_text.strip() or json.dumps(data),
                "model": chosen["display_name"],
                "model_id": model_id,
                "provider": chosen["provider_name"],
                "api_type": "responses",
            }
        else:
            user_content: Any = prompt
            if all_images and chosen.get("vision"):
                user_content = [{"type": "text", "text": prompt}]
                for mime, b64_str in all_images[:4]:
                    user_content.append(
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_str}"}}
                    )
            body = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            }
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            out_text = data["choices"][0]["message"]["content"]
            return {
                "text": out_text.strip(),
                "model": chosen["display_name"],
                "model_id": model_id,
                "provider": chosen["provider_name"],
                "api_type": "chat-completions",
            }


def get_all_auth_status() -> dict[str, Any]:
    """Return complete summary of all connected accounts, VS Code Copilot models, Codex, and Claude logins."""
    cfg = load_config()
    copilot = discover_github_copilot_status()
    codex = discover_codex_profiles()
    claude = discover_claude_status()

    # Build unified list of selectable Agent / Critic models
    selectable_models: list[dict[str, Any]] = []
    for vm in copilot["vscode_models"]:
        selectable_models.append(
            {
                "id": vm["id"],
                "label": f"{vm['display_name']} — {vm['provider_name']} (VS Code Copilot)",
                "provider": "vscode_copilot",
                "vision": vm["vision"],
            }
        )
    for prof in codex.get("profiles", []):
        if prof.get("model"):
            selectable_models.append(
                {
                    "id": f"codex:{prof['model']}",
                    "label": f"{prof['model']} — Codex ({prof['file']})",
                    "provider": "codex",
                    "vision": True,
                }
            )
    selectable_models.extend(
        [
            {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash (Google API)", "provider": "gemini", "vision": True},
            {"id": "gpt-4o", "label": "GPT-4o (OpenAI API)", "provider": "openai", "vision": True},
            {"id": "claude-3-5-sonnet-latest", "label": "Claude 3.5 Sonnet (Anthropic API)", "provider": "anthropic", "vision": True},
        ]
    )

    active_model = cfg.default_critic_model
    if active_model == "gemini-2.5-flash" and copilot["vscode_models"]:
        # Default to the user's first VS Code Copilot model if already configured!
        active_model = copilot["vscode_models"][0]["id"]

    return {
        "active_agent_model": active_model,
        "vscode_copilot": copilot,
        "codex": codex,
        "claude": claude,
        "selectable_models": selectable_models,
        "api_keys_configured": {
            "gemini": bool(cfg.get_key("gemini")),
            "meshy": cfg.has_live_3d_key(),
            "elevenlabs": bool(cfg.get_key("elevenlabs")),
            "openai": bool(cfg.get_key("openai")),
            "anthropic": bool(cfg.get_key("anthropic")),
        },
    }

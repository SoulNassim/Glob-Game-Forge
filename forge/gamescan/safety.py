"""Safety layer for Game Scan (Section 14.5).

- Strictly read-only during scan
- Respects `.gitignore` patterns
- Skips secrets and credentials (`.env`, `*.pem`, `*.key`, `export_presets.cfg`, keystores, credentials)
- Skips binary blobs over size limit
- Generates a pre-scan consent manifest showing exact file list and snippet byte counts before any LLM call
- Treats all file contents as untrusted data (strips prompt-injection patterns)
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any


SECRET_FILE_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "*secret*",
    "*credential*",
    "export_presets.cfg",
    "id_rsa*",
    "*.sqlite",
    "*.db",
]

IGNORED_DIRS = {
    ".git",
    ".godot",
    ".import",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "build",
    "dist",
    "Temp",
    "Library",
    "Intermediate",
    "Binaries",
}

MAX_TEXT_FILE_BYTES = 256 * 1024      # 256 KB max for text files
MAX_IMAGE_FILE_BYTES = 10 * 1024 * 1024  # 10 MB max for sample art inspection

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*prompt\s*:", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+a", re.IGNORECASE),
    re.compile(r"execute\s+shell|run_command|CallDynamicTool", re.IGNORECASE),
]


def load_gitignore_patterns(game_dir: Path) -> list[str]:
    patterns: list[str] = []
    gi = game_dir / ".gitignore"
    if gi.exists():
        try:
            for line in gi.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    patterns.append(s.rstrip("/"))
        except Exception:
            pass
    return patterns


def is_secret_or_ignored(rel_path: Path, gitignore_patterns: list[str]) -> tuple[bool, str]:
    parts = rel_path.parts
    for part in parts:
        if part in IGNORED_DIRS:
            return True, f"ignored_dir:{part}"
    name_low = rel_path.name.lower()
    for pat in SECRET_FILE_PATTERNS:
        if fnmatch.fnmatch(name_low, pat.lower()):
            return True, f"secret_guard:{pat}"
    rel_posix = rel_path.as_posix()
    for pat in gitignore_patterns:
        if fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(rel_path.name, pat):
            return True, f"gitignore:{pat}"
    return False, ""


def sanitize_untrusted_text(raw: str, max_chars: int = 4000) -> str:
    """Sanitize untrusted file content so comments/strings can never act as agent instructions."""
    cleaned = raw[:max_chars]
    for regex in INJECTION_PATTERNS:
        cleaned = regex.sub("[REDACTED_UNTRUSTED_DIRECTIVE]", cleaned)
    return cleaned


def collect_safe_game_files(game_dir: Path) -> dict[str, Any]:
    """Enumerate safe files for Game Scan and build the user consent preview manifest."""
    if not game_dir.exists() or not game_dir.is_dir():
        raise ValueError(f"Game directory does not exist: {game_dir}")

    gitignore = load_gitignore_patterns(game_dir)
    safe_files: list[Path] = []
    skipped_files: list[dict[str, str]] = []
    consent_items: list[dict[str, Any]] = []
    total_snippet_bytes = 0

    for p in sorted(game_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(game_dir)
        blocked, reason = is_secret_or_ignored(rel, gitignore)
        if blocked:
            skipped_files.append({"path": rel.as_posix(), "reason": reason})
            continue

        size = p.stat().st_size
        ext = p.suffix.lower()
        is_art = ext in (".png", ".jpg", ".jpeg", ".webp", ".svg", ".glb", ".obj")
        limit = MAX_IMAGE_FILE_BYTES if is_art else MAX_TEXT_FILE_BYTES
        if size > limit:
            skipped_files.append({"path": rel.as_posix(), "reason": f"size_exceeds_limit ({size} bytes)"})
            continue

        safe_files.append(p)
        snippet_sz = min(size, 4000) if not is_art else min(size, 1024)
        total_snippet_bytes += snippet_sz
        consent_items.append(
            {
                "path": rel.as_posix(),
                "kind": "art_sample" if is_art else "config_or_script",
                "file_bytes": size,
                "snippet_bytes": snippet_sz,
            }
        )

    return {
        "game_dir": str(game_dir.resolve()),
        "safe_files": safe_files,
        "consent_manifest": {
            "file_count": len(consent_items),
            "skipped_count": len(skipped_files),
            "total_snippet_bytes": total_snippet_bytes,
            "files": consent_items,
            "skipped_files": skipped_files[:50],
            "safety_guarantees": [
                "100% read-only scan; never modifies your game project.",
                "Respects .gitignore and skips .env, keys, credentials, and export_presets.cfg.",
                "All comments, dialogue, and docs are sanitized as untrusted data, never instructions.",
            ],
        },
    }

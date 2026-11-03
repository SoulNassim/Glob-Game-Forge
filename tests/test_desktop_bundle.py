"""The desktop build bundles every non-Python data file the app reads at runtime."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORGE = ROOT / "forge"
# Folders the build already bundles, or that are not read at runtime.
BUNDLED_ELSEWHERE = ("forge/ui", "forge/recipes/builtins")


def _bundled_sources() -> list[str]:
    script = (ROOT / "scripts" / "build_desktop.py").read_text(encoding="utf-8")
    normalized = script.replace("'", "").replace(" ", "")
    return re.findall(r"\{ROOT/([\w./]+)\}", normalized)


def test_runtime_data_files_are_bundled() -> None:
    sources = _bundled_sources()
    missing = []
    for path in FORGE.rglob("*"):
        if not path.is_file() or path.suffix in {".py", ".pyc"} or path.name == ".DS_Store":
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(BUNDLED_ELSEWHERE):
            continue
        if not any(rel == src or rel.startswith(src + "/") for src in sources):
            missing.append(rel)
    assert not missing, f"not bundled by scripts/build_desktop.py: {missing}"

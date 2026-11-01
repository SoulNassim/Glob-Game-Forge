"""Build Glob as a standalone desktop app (no Python needed on the target machine).

    uv run --extra desktop --with pyinstaller python scripts/build_desktop.py

Output: dist/Glob/ (Windows: Glob.exe) or dist/Glob.app (macOS). Projects and settings live in the
user's home folder (~/Glob/projects and ~/.forge), never inside the app bundle.

Release installers are built from this in CI (.github/workflows/release.yml):
Windows -> packaging/windows/glob.iss (Inno Setup), macOS -> a .dmg made with hdiutil.
"""

from __future__ import annotations

import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
SEP = ";" if sys.platform == "win32" else ":"
ENTRY = ROOT / "build" / "glob_entry.py"


def main() -> None:
    ENTRY.parent.mkdir(exist_ok=True)
    ENTRY.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "os.environ.setdefault('FORGE_PROJECTS_DIR', str(Path.home() / 'Glob' / 'projects'))\n"
        "from forge.desktop import main\n"
        "main()\n",
        encoding="utf-8",
    )
    icon = ROOT / "forge" / "ui" / ("glob.ico" if sys.platform == "win32" else "glob-icon.png")
    PyInstaller.__main__.run([
        str(ENTRY),
        "--name", "Glob",
        "--windowed",
        "--noconfirm",
        "--icon", str(icon),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'forge' / 'ui'}{SEP}forge/ui",
        "--add-data", f"{ROOT / 'forge' / 'recipes' / 'builtins'}{SEP}forge/recipes/builtins",
        "--collect-submodules", "forge",
        "--collect-submodules", "uvicorn",
        "--hidden-import", "forge.server.app",
        "--osx-bundle-identifier", "io.github.soulnassim.glob",
    ])
    print(f"\nBuilt {ROOT / 'dist' / 'Glob'}")


if __name__ == "__main__":
    main()

#!/bin/sh
# Install Glob on macOS or Linux:
#   curl -LsSf https://raw.githubusercontent.com/SoulNassim/Glob-Game-Forge/main/scripts/install.sh | sh
# Installs uv if needed, then Glob as a uv tool. Start it with `glob` (browser) or `glob-desktop` (app window).
set -eu

SOURCE="https://github.com/SoulNassim/Glob-Game-Forge/archive/refs/heads/main.tar.gz"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python package manager from astral.sh)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing Glob..."
uv tool install --force --python 3.12 "forge-assets[desktop] @ $SOURCE"
uv tool update-shell >/dev/null 2>&1 || true

echo
echo "Glob is installed. Open a new terminal, then run:"
echo "  glob-desktop     # Glob in its own window"
echo "  glob             # Glob in your browser at http://127.0.0.1:8420"
echo "Projects are saved in ~/Glob/projects (desktop) or ./projects (browser)."

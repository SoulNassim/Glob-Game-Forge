#!/bin/sh
# Install Glob on macOS or Linux:
#   curl -LsSf https://raw.githubusercontent.com/SoulNassim/Glob-Game-Forge/main/scripts/install.sh | sh
# Installs uv if needed, then Glob as a uv tool. Then run `glob connect` in your game folder.
set -eu

SOURCE="https://github.com/SoulNassim/Glob-Game-Forge/archive/refs/heads/main.tar.gz"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python package manager from astral.sh)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing Glob..."
uv tool install --force --python 3.12 "forge-assets @ $SOURCE"
uv tool update-shell >/dev/null 2>&1 || true

echo
echo "Glob is installed. In your game folder, run:"
echo "  glob connect     # connect your coding agents and open Glob in the browser"
echo "(New terminal needed for 'glob' to be found; until then use ~/.local/bin/glob)"
echo "Or just tell your agent: Set up Glob for this game: read https://soulnassim.github.io/Glob-Game-Forge/agent.md"

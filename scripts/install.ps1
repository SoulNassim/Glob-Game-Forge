# Install Glob on Windows (PowerShell):
#   irm https://raw.githubusercontent.com/SoulNassim/Glob-Game-Forge/main/scripts/install.ps1 | iex
# Installs uv if needed, then Glob as a uv tool. Then run `glob connect` in your game folder.
$ErrorActionPreference = "Stop"

$Source = "https://github.com/SoulNassim/Glob-Game-Forge/archive/refs/heads/main.zip"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (Python package manager from astral.sh)..."
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "Installing Glob..."
uv tool install --force --python 3.12 "forge-assets @ $Source"
uv tool update-shell | Out-Null
& "$env:USERPROFILE\.local\bin\glob.exe" shortcut

Write-Host ""
Write-Host "Glob is installed. Open it any time with the Glob shortcut on your desktop or in the Start menu."
Write-Host "To connect a game, run this in its folder:"
Write-Host "  glob connect     # connect your coding agents and open Glob in the browser"
Write-Host "(Open a new terminal so 'glob' is found; until then use $env:USERPROFILE\.local\bin\glob.exe)"
Write-Host "Or just tell your agent: Set up Glob for this game: read https://soulnassim.github.io/Glob-Game-Forge/agent.md"

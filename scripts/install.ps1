# Install Glob on Windows (PowerShell):
#   irm https://raw.githubusercontent.com/SoulNassim/Glob-Game-Forge/main/scripts/install.ps1 | iex
# Installs uv if needed, then Glob as a uv tool. Start it with `glob-desktop` (app window) or `glob` (browser).
$ErrorActionPreference = "Stop"

$Source = "https://github.com/SoulNassim/Glob-Game-Forge/archive/refs/heads/main.zip"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (Python package manager from astral.sh)..."
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "Installing Glob..."
uv tool install --force --python 3.12 "forge-assets[desktop] @ $Source"
uv tool update-shell | Out-Null

Write-Host ""
Write-Host "Glob is installed. Open a new terminal, then run:"
Write-Host "  glob-desktop     # Glob in its own window"
Write-Host "  glob             # Glob in your browser at http://127.0.0.1:8420"
Write-Host "Projects are saved in ~\Glob\projects (desktop) or .\projects (browser)."

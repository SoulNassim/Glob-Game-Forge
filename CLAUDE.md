# CLAUDE.md — Instructions for Claude Code & Coding Agents

Read `AGENTS.md` and `.claude/skills/forge-asset-maker/SKILL.md` for full details on operating the Forge 2D/3D Asset Studio.

## Quick Commands
- **Run Forge Studio Server**: `uv run forge` (hosts UI and HTTP API at `http://127.0.0.1:8420`)
- **Run Forge MCP Server (stdio)**: `uv run forge --mcp`
- **Run Tests**: `uv run --extra dev pytest -v`

## Collaborating on Game Assets via Markdown Workspace
- Game definition lives in `projects/default/workspace/GAME.md`.
- Asset task checklist lives in `projects/default/workspace/ASSET_REQUIREMENTS.md`.
- Handoff communication log lives in `projects/default/workspace/HANDOFFS.md`.
- SHA-256 delivery receipts live in `projects/default/workspace/DELIVERY_MANIFEST.json`.

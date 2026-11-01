# AGENTS.md — Forge Workspace & Game Coding Agent Protocol

Welcome, Coding Agent (Claude Code, Codex, Cursor, OpenCode, Antigravity).

**Forge** is a local-first 2D & 3D game asset pipeline with a chat agent and a live project overview. It is designed so that **an external agent coding a game** and **Forge** communicate through shared, human-readable Markdown files inside each project folder (`projects/<project_name>/workspace/`) as well as MCP tools and HTTP endpoints.

---

## 1. Folder & Workspace Layout

Every project lives in `projects/<project_name>/`:

```text
projects/<project_name>/
  project.json                    # Structured Project Bible & budget caps
  manifest.db / manifest.json     # DAG asset lineage database
  assets/                         # Generated .png, .glb, .obj, and thumbnails
  exports/                        # Engine-ready Godot, Unity, and Unreal bundles
  workspace/                      # Shared Markdown communication files
    GAME.md                       # Game definition, camera, scale, art style (2-way synced with Project Bible)
    ASSET_REQUIREMENTS.md         # Live checklist of assets needed by game code ([ ], [~], [x])
    HANDOFFS.md                   # Bidirectional handoff log between coding agent & Forge
    KNOWLEDGE.md                  # Reusable art rules, approved prompts, and naming conventions
    DELIVERY_MANIFEST.json        # SHA-256 verification hashes & QC scores for delivered assets
    handoffs/                     # Individual timestamped handoff notes
```

Every planned asset also has a nested Markdown blueprint, for example
`workspace/characters/royals/demon_king.md`. Forge keeps these files in sync with
asset studies. Character blueprints should communicate the creative intent before
production, using these sections:

- **Core Idea** — the character fantasy in one sentence.
- **Gameplay Identity** — the character's role and what makes their playstyle unique.
- **Character & World** — personality, motivation, lore, and visual description.
- **Visual Blueprint** — silhouette, focal details, materials, and gameplay-distance read.
- **Production Handoff** — workflow, selected concept, generated outputs, and engine path.

The Forge study view presents these as visual, editable design pillars; the Markdown
file remains the portable source of truth for external coding agents. Its blueprint
action can use the configured VS Code text model to fill missing pillars, update the
nested Markdown file, and preserve existing writing unless regeneration is confirmed.

---

## 2. How a Game Coding Agent Requests & Receives Assets

When you are writing game code (e.g., creating a new weapon, enemy, potion, UI icon, or level prop in Godot/Unity/Unreal) and need art assets:

### Workflow A: Direct Markdown File Protocol (Zero-Config)

1. **Define or update the game context** in `projects/<project>/workspace/GAME.md`.
   - Forge parses `GAME.md` and automatically syncs its `ProjectBible` (Genre, Art Style, Camera Perspective, Polycount Budget, Unit Scale, Palette).
2. **Append required assets** to `projects/<project>/workspace/ASSET_REQUIREMENTS.md` using this exact checklist line format:
   ```markdown
   - [ ] `asset_slug` | **Kind** | `res://assets/models/asset_slug.glb` | Clear visual description of the asset
   ```
   Supported **Kind** values: `3D Mesh`, `2D Icon`, `2D Sprite`, `2D Texture`, `2D Image`.
3. **Trigger fulfillment** (or let the human click **Fulfill Open Requirements** in the Forge Workspace UI):
   - Via CLI / HTTP:
     ```bash
     curl -X POST http://127.0.0.1:8420/api/workspace/requirements/fulfill -H "Content-Type: application/json" -d '{"project":"default"}'
     ```
   - Or via MCP tool: `fulfill_asset_requirements(project="default")`.
4. **Verify delivery**:
   - Forge generates the asset, runs QC & Vision Critic checks, computes the file's **SHA-256 hash**, updates the line in `ASSET_REQUIREMENTS.md` to `[x]` with `asset:<id> sha256:<hash>`, writes the receipt to `workspace/DELIVERY_MANIFEST.json`, and appends a delivery note to `workspace/HANDOFFS.md`.

### Workflow B: MCP Server Tools (`.mcp.json` included)

Forge exposes 26 tools over stdio (`uv run forge --mcp`) and HTTP JSON-RPC (`POST http://127.0.0.1:8420/api/mcp`):
- **Workspace Docs & Requirements**: `read_workspace_doc`, `write_workspace_doc`, `list_asset_requirements`, `add_asset_requirement`, `fulfill_asset_requirements`, `post_agent_handoff`
- **Asset & Lineage DAG**: `list_assets`, `get_asset`, `rate_asset`, `undo_action`
- **Generation Verbs & Recipes**: `run_verb`, `run_recipe`, `create_recipe`, `run_agent_brief`
- **Import a Game**: `import_game` copies a Godot/Unity/Unreal project's characters, models, animations, textures, variants, kits and docs into the project (the user must grant the folder in Settings → Storage & access; `preview: true` only lists what it would import)
- **Game Scan & Engine Write-Back**: `scan_game_project`, `get_game_profile`, `update_game_profile`, `list_asset_wishlist`, `find_placeholders`, `propose_assets_for`, `writeback_preview`, `writeback_apply`
- **Bible & Export**: `get_project_bible`, `update_project_bible`, `list_style_packs`, `queue_status`, `get_budget`, `export`

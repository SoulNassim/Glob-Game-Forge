---
name: forge-asset-maker
description: Collaborate with Forge Studio to manifest game worlds in GAME.md, inject category/material style prompts, manage the 4-stage Asset Pipeline (Prepare Prompt -> 2D Candidates -> Pick Winner -> 3D / Split Body Parts / Slice Sheet), and consume approved 2D/3D game assets.
---

# Forge Game Asset Pipeline Skill (For External Coding Agents)

Use this skill whenever you are coding a game prototype (Godot, Unity, Unreal, Web/Three.js) and need 2D or 3D assets, or when the user asks you to plan/request assets through Forge.

## 1. File Architecture (`projects/<project_name>/`)

```text
projects/<project_name>/
  project.json                    # Core Project Bible (style, camera, scale, palette, polycount budget)
  assets/                         # All generated & uploaded .png, .glb, .obj files and thumbnails
  workspace/
    GAME.md                       # Human & agent-readable World Bible, Category Style Prompts & Material Tags
    ASSET_REQUIREMENTS.md         # Live checklist ([ ] Planned, [~] In Progress, [x] Approved with asset:<id> & sha256)
    PIPELINE.json                 # Structured 4-stage pipeline state for every planned asset
    STYLE_RULES.json              # Category prompt injectors (character, item, card_art, ui, environment) & materials
    HANDOFFS.md                   # Bidirectional communication log between Coding Agent & Forge Studio
```

## 2. How Category & Material Style Injection Works

Forge separates **what an asset is (Category & Material Style Prompts)** from **how it is processed (Workflow Presets)**:
- **Categories (`STYLE_RULES.json`)**:
  - `character`: Full-body tabletop figurine / character with readable silhouette & clean background.
  - `item`: Standalone weapon / dungeon prop in isometric 3/4 view.
  - `card_art`: Painterly vertical portrait framed for deckbuilder card UI.
  - `ui`: Ornate frame, mana orb, or crisp inventory icon with alpha channel.
  - `environment`: Board, tile, or architectural piece matching world lighting.
- **Materials / Factions**: e.g. `Weathered Iron`, `Mossy Castle Stone`, `Crimson Royal`, `Amber Runes`.
- Selecting a category and material tags **automatically synthesizes the full image generation prompt** so context is never lost across dozens of assets.

## 3. How to Request or Consume Assets as a Coding Agent

1. **Read the World Bible first**: Read `projects/<project>/workspace/GAME.md` so your game code (camera angle, unit scale, item stats, card names) matches the visual world.
2. **Check approved assets**: Read `projects/<project>/workspace/ASSET_REQUIREMENTS.md`. Any row marked `- [x] slug | ... | asset:<id>` is approved and ready inside `projects/<project>/assets/`.
3. **Request new assets**:
   - Either append a row to `ASSET_REQUIREMENTS.md`:
     ```markdown
     - [ ] `siege_catapult` | **item** | `res://assets/models/siege_catapult.glb` | Heavy timber & iron catapult with glowing ember payload
     ```
   - Or call `POST http://127.0.0.1:8420/api/pipeline/item` (or MCP tool `add_asset_requirement`).
4. **Automated Workflows Available**:
   - `make_3d_figurine` / `make_3d_prop` (Meshy 7.1 `.glb` + `.obj`)
   - `split_body_parts` (Isolates Head, Torso, Arms, Legs, Weapon for modular rigging)
   - `slice_lineup_sheet` (Splits a multi-character concept lineup sheet into standalone assets)
   - `make_variants` (Generates material/faction siblings with locked silhouette)

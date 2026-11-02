---
name: forge-asset-maker
description: For agents coding a game against Glob, the asset studio. Order real art, 3D models, rigs, animations and sounds with the order and deliver loop instead of leaving placeholders, then swap in what lands in glob_delivery/.
---

# Glob order and deliver loop

Use this when you are coding a game and need art, 3D models, animations or sounds. Glob makes them. You order, Glob delivers.

If the game is connected to Glob (Settings → Coding agent), a `glob` skill and the `glob` MCP server are already in the game folder. Load the `glob` skill for the full Glob role.

## The loop

1. **Use a placeholder** in your code.
2. **Order the real asset** with MCP `place_order`: `title`, `kind`, `brief`, `placeholder` path, `context`, optional `needs`.
   - Kinds: character, creature, item, weapon, prop, vehicle, environment, ui, vfx, sfx, voice, music.
   - Default needs: character/creature/item/weapon/prop/vehicle → image + 3D model. environment/ui/vfx → image. sfx/voice/music → audio. Add `rig` or `animations` in `needs` when you want them.
3. **Check progress** with `list_orders` / `get_order`. Status goes draft → filed → in_progress → ready → delivered → integrated.
4. **Sorting drafts** (in a separate session): load the `glob` skill, call `workspace_overview` first, then `file_order` each draft into the workspace with a real prompt.
5. **Find the delivery** in `glob_delivery/<order_id>/` (`<id>.png`, `<id>.glb`, `animations/`, `<id>.mp3`). `glob_delivery/manifest.json` lists each file with its SHA-256. Use `get_deliveries` for the list.
6. **Swap the placeholder**, then call `update_order` with `status=integrated`.

## Rules

- Write only to your own code. Glob writes only to `glob_delivery/`.
- Don't edit files in `glob_delivery/`. A changed asset is delivered again.
- `generate` needs the Glob app open when the MCP server runs without it.
- `find_placeholders` and `scan_game_project` help list what to order. `import_game` brings an existing game into Glob.

---
name: forge-asset-maker
description: For agents coding a game against Glob, the asset studio. Copy finished art, 3D models, rigs, animations and sounds from the game's glob/ folder into the game, using glob/ASSETS.md, and keep placeholders for anything still missing.
---

# Glob asset loop

Use this when you are coding a game and need art, 3D models, animations or sounds. The user makes them in Glob. You copy the finished files into the game.

Glob's project for this game is in `glob/` in the game folder. Its index, `glob/ASSETS.md`, lists every asset: name, kind, page, the picked image or sound (`winner:`), and any 3D model, rig and animation files. If `glob/ASSETS.md` is missing, Glob isn't set up for this game yet: see `docs/agent.md`.

## The loop

1. **Use a placeholder** in your code for any asset that isn't made yet.
2. **Read `glob/ASSETS.md`** and find the asset you need.
3. **Copy the files into the game**: the picked image or sound (`winner:`), plus any 3D model, rig or animation files, into the game's own folders. Point your code at the copies.
4. **Keep the placeholder for anything missing** (no `winner:` yet). Tell the user what's needed and that they can make it in Glob.

When a winner changes in Glob, copy it again.

## Levels from modular kits

A `kit` asset in `glob/ASSETS.md` comes with a `kit.json` next to its GLB. Read it before writing level code: it gives the grid, how to place and turn each piece, what each side connects to, and a sample room to check your placement against. Copy the GLB and `kit.json` together. A `material` asset has tiling maps (albedo, normal, roughness, height, orm) for any surface in the game.

## Rules

- Never edit, move or delete anything inside `glob/`. Glob owns that folder.
- Making, picking and sorting art happens in Glob, not here.
- Write only to the game's own folders, never inside `glob/`.

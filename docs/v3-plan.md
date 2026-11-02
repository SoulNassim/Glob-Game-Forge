# Glob v3 plan

**Glob is an app with its own assistant. Your coding agent just takes the finished art.**

The user downloads Glob, opens their game, and keeps coding as usual. Nothing has to be said to the coding agent.

## Roles

- **Glob's chat** is its own session. It organizes the workspace, writes the style and prompts, and generates art.
- **The coding agent** codes the game and takes finished art from `glob/`. It never manages the workspace.
- **The user** looks, clicks, picks, and talks to either one.

## 1. Glob is an app

- `Glob-Setup.exe`: download, double-click, and Glob opens. No terminal, no uv, no agent. The other way in is telling an agent "Install Glob for me: <url>".
- **Glob** in the Start menu and on the desktop, with Glob's icon.
- It opens in its own window (an app window with no tabs or address bar). The server starts behind it.
- Closing the window doesn't stop Glob. A tray icon has Open and Quit, and the server keeps running, so reopening is instant.
- Settings → About shows the install location, "Open folder" and Update.
- It updates itself: "Glob 3.1 is ready · Restart".

## 2. Glob's chat, honest about its connection

- It runs on the Claude Code or Codex login the user already has. No chatbot API keys.
- It shows ✓ only after a real test call has succeeded.
- **Log in** opens the login window, and Glob shows "Waiting for you to log in in the window that just opened…". It checks every few seconds and switches to "✓ Logged in, you can chat" on its own.
- If Claude Code isn't installed, Glob says so and offers one Install button.
- **It knows the page you're on**, so "make him scarier" on the Ghoul's page works.
- **Suggestion chips** under the input change with the page: "Set up the style", "Fill in missing art", "Make 4 options".
- **One undo per run:** "The assistant changed 12 pages · Undo".
- **It asks before spending:** "This will make 6 images, about 12 credits. Go?"

## 3. No delivery system

- `glob_delivery/`, orders and the manifest go away.
- Glob's files already live in the game (`glob/`). The note Glob writes into `AGENTS.md` / `CLAUDE.md` says: finished art is in `glob/<section>/<asset>/`, the picked version is marked, copy what you need, and don't touch the workspace.
- The coding agent then needs no MCP server, no restart and nothing to approve.
- **Drag out of Glob:** drag a card into Godot's FileSystem panel or into any folder.
- **"Used in your game":** Glob watches the game folder and shows on each card where the asset is used (`res://enemies/ghoul.tscn`).

## 4. Clear settings

- **Assistant:** which login it uses and its live status.
- **Art services:** where images, 3D and sound come from, with Sign in buttons where a service allows it.
- **Defaults per kind:** a table of shape and background for each kind of asset.

  | Kind | Shape | Background |
  |---|---|---|
  | Characters | 3:4 | cut out |
  | Places | 16:9 | full scene |
  | Items and icons | 1:1 | cut out |
  | Key art | 16:9 | full scene |

  Any section or asset can override these. There is no single universal aspect ratio anymore. The code already has `KIND_ASPECT`, but the UI only shows `image_aspect_ratio`.
- **About:** the install location, the storage folder and updates.

## 5. First start

1. Open Glob, then click "Open your game folder".
2. The game builds itself on screen: sections appear one by one, models pop into the showcase, and the title and pitch type themselves in.
3. "Want the assistant to set up the style and the missing pieces?" Yes / Later.
4. Glob asks for an art service sign-in only the first time something is generated.

## Idiot proof

- There is exactly one way to start: the Glob icon.
- Every button says what happens next, and every wait shows what it's waiting for and roughly how long.
- Every error says what to do, with a button, and no raw error text.
- Glob asks for something only when it's first needed.
- Skipping something never breaks anything: without logins you can still browse, import and organize.

## Magic

- Opening a game builds it on screen. The assistant's pages appear and fill in live.
- Generated options fade in as each one finishes. Models spin up into the viewer.
- Picking one makes it glow "in your game", and the showcase updates.
- Windows notifications when a generation finishes.
- Ctrl+K to jump to an asset or run an action.

## Performance

Measure Tarky first: import, startup and page loads.

- **Don't copy the game.** The import points at the game's own models and textures instead of copying them into `glob/`. A few test imports took about 930 MB and filled C:. Only what Glob makes is stored in `glob/`.
- The tray server makes opening instant. Provider code loads only when it's first needed.
- Model thumbnails are rendered once, saved to disk, and redone only when the model changes.
- Small previews for textures and images. Images load as you scroll.
- The workspace structure is kept in memory and updated by a file watcher.
- Light preview copies of big models for the viewer and showcase. Only one live 3D viewer at a time.
- Options generate in parallel and each shows as soon as it's done. Jobs survive a restart.

## Consistency

- A style anchor: one approved image that every later generation uses as a reference.
- Variants (colours, damaged, seasonal) in one click, kept on the same page.
- Sound per asset: the Ghoul's growl and footsteps live on the Ghoul's page.
- A cost shown on every Make button, and the remaining credits on the front page.
- "Make all characters" for a whole section in one style.
- A character sheet (front, side, back) approved once, then used for every model and animation of that character.
- Drop a reference image onto a card.
- History on every card, one click back.

## One sheet, cut locally

Wherever it can, Glob makes **one image holding many things** and cuts it up on the user's PC, instead of paying for one generation per thing. Cutting needs no AI and costs nothing.

- **Prop sheets:** every prop of a scene, separate, whole, spaced in a grid on a plain background, with no text.
- **Icon sheets** for an inventory, **UI sheets** (buttons, frames, bars), **tile sheets** for floors and walls, and **pose sheets** for a character.
- Limit: about 9 to 12 items per 2048 px sheet, so each crop stays near 500 px. Use another sheet beyond that, and upscale crops locally before 3D.
- The model sometimes lets items touch or adds labels. The user can drag crop boxes by hand.
- Build on the existing sheet support in `asset_studio.py` (check what it does first).

## Level kit

A new kind of page: the user describes a level, and Glob turns one picture into a matching kit and a first layout.

1. **Concept (1 image):** the level from the chosen view (top-down, isometric or side-on). The view is chosen first because it decides the whole kit. Optional: "make me a crypt prop sheet" skips the concept.
2. **Prop sheet (1 image):** every object from the concept, separate and complete, on a plain background, generated with the concept as a reference.
3. **Cut locally (free):** Glob finds each prop on the plain background and crops it. Duplicates merge: five torches become one torch asset used five times. The user keeps, drops, merges or redraws the boxes.
4. **Floors and walls** become tiles and seamless textures, not props.
5. **3D only for the props the user picks** (image-to-3D). Others stay flat images or decals. Hero props can get an optional front and side sheet (one more call) for better 3D.
6. **Save as a Godot scene** with every prop placed roughly where it was in the concept. The user builds the real level in Godot: Glob makes the kit and a first layout, not a level editor.

Before anything paid, Glob shows the price: 2 image calls plus one 3D call per picked prop.

The page shows the concept on top, the props as cards underneath (each opening into its own asset page), then the tiles, then "Save as a Godot scene".

## All in one (instead of external tools)

| Today people use | In Glob |
|---|---|
| Mixamo | Auto-rig, an animation library, a preview on the model and export |
| Blender (cleanup) | Fix scale, pivot and up axis; reduce polycount, LODs and conversion between GLB, FBX and OBJ |
| Substance / Materialize | Seamless tiling PBR materials from a prompt or photo: albedo, normal and roughness |
| Aseprite / TexturePacker | 2D sprites and animated sprite sheets, pixel-art mode, packed atlases |
| Figma / icon packs | Icon sets and UI kits (buttons, frames, bars) in the game's style, 9-slice ready |
| sfxr / Audacity / music sites | Sound effects, music loops, voice lines with a voice per character, trimming and loudness |
| Skybox and HDRI tools | Skyboxes and terrain textures |
| VFX texture packs | Particle textures and flipbooks (fire, smoke, magic) |
| Photoshop for store pages | Steam capsules and store images in every required size, built from the key art |
| Spreadsheets for asset tracking | Budgets per kind (polycount, texture size) with warnings, plus a source and licence for every asset |
| Placeholder packs | Instant grey-box placeholders that turn into "needs art" cards |
| Pitch decks / art books | Art book export of characters, monsters and places, as a page or PDF |
| Engine asset browser | A Godot dock with the project's art (later) |
| — | A sample game (Pocket Kaiju) to click through before importing your own |

## Not changing

- The organization: sections, pages, the style bible and the showcase.
- No board.

## Build order

1. The app window, the Start menu entry, the tray and the About page.
2. Glob's chat with an honest login.
3. No delivery: the `AGENTS.md` note, drag out, "used in your game".
4. Import without copying, plus the other performance work (measure first).
5. Settings pages with defaults per kind.
6. The first-start flow and the magic.
7. `Glob-Setup.exe` and self-updates.
8. Sheets cut locally, then the Level kit.
9. The all-in-one tools, one at a time: Mixamo-style rigging, model cleanup, materials, sprites, UI kits and sound first.

Every step is usable on its own and is tested on Tarky.

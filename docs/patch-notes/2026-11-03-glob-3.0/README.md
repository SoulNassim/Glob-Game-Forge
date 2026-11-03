# Glob 3.0

Glob is your game's art studio, and now it's a real app. You make the art in Glob; your coding agent copies it into the game from `glob/ASSETS.md`.

## Highlights

- **An app you just open.** The Glob shortcut opens Glob in its own window, with a "Starting Glob" screen right away. Closing the window leaves it running behind a tray icon (Open Glob, Quit Glob).
- **Level kits.** Describe a section of a level and Glob pictures it. It then draws every prop from that picture on one sheet, and finds, names and cuts out each prop on your computer, for free. Two image calls instead of one per prop.
- **No more orders or delivery folders.** Glob keeps `glob/ASSETS.md`, an index of every asset and its finished files. Your coding agent reads it and copies what it needs. Nothing to install in the agent, nothing to restart.
- **2D or 3D projects.** A 2D game hides 3D models, rigs and animations. Its assets are ready as soon as their image or sound is picked.
- **Simpler settings.** Five tabs: Accounts, New art, Glob chat, Your game, and Files & app.
- **Much faster.** The server no longer stalls on disk work, so the studio and Settings react right away.

## New

- **Settings → Accounts:**
  - four lights at the top show what Glob can make right now: Images, 3D models, Sounds and Glob chat;
  - one card per service: sign in to ElevenLabs, Meshy, fal or Higgsfield, or paste a key for Gemini, Tripo or Stability.
- **Settings → New art:**
  - which service makes images and 3D;
  - whether the project is a 2D or a 3D game;
  - a shape and background per kind of asset, e.g. characters tall and cut out, places very wide as a full scene.
  - Changes save at once.
- **Your game:**
  - links a game folder and writes a short note into `AGENTS.md`;
  - adds a `glob/.gitignore` for Glob's internal files;
  - cleans up the old 2.x setup (MCP entries, skills, `glob_delivery/`).
- **Kind icons:** an asset without art shows its kind's icon instead of an empty box.
- **New project:**
  - paste the game folder's path, or start without a game folder;
  - New asset shows examples that match the kind (a coin chime for a sound effect, a crypt room for a level kit).
- **Imports link files** instead of copying them, so importing a game takes no extra disk space on the same drive.
- **Honest chat login:** a coding agent counts as connected only after a real test. Log in waits for you, then switches over by itself.

## Changed

- **One image per run by default.** When a run makes a single image, it becomes the winner on its own.
- **Model menus say who bills each model**, for example "ElevenLabs credits" or "your Gemini key".
- **Make 3D won't start without enough Meshy credits**, and says how many you're short.
- **Clearer feedback:**
  - disabled buttons look disabled;
  - level kit steps say what they're waiting for;
  - the Generate window works on a phone.
- **Glob's chat uses the button names you see** ("Make sheet", "Make 3D").

## Removed

- **Agent orders**, the order board, `glob_delivery/` and the order MCP tools.
- **The Board view.**

## Upgrading from 2.x

- Run `glob connect` once in each game folder. It removes the old setup and writes the new note.
- Restart your coding agent if it still lists Glob's old tools.

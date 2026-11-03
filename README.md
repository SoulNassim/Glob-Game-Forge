<div align="center">

<img src="forge/ui/glob-forge.svg" width="220" alt="Glob, a pink pixel-art slime blacksmith hammering on an anvil">

# Glob

### Make the art. Your coding agent puts it in the game.

Glob is an asset studio for your game. It runs as its own app and makes 2D art, 3D models, rigs, animations, icons and sounds.<br>
Your coding agent copies the finished files from Glob into your game.

**New here?** Paste this into your coding agent, from your game's folder:

```text
Set up Glob for this game: read https://soulnassim.github.io/Glob-Game-Forge/agent.md
```

[**Get started**](#get-started) · [**▶ Live demo**](https://soulnassim.github.io/Glob-Game-Forge/demo/) · [Website](https://soulnassim.github.io/Glob-Game-Forge/) · [What you can make](#what-you-can-make)

**Free and open source (MIT).** Bring keys for the generators you use. No markup, no subscription.

</div>

## How it works

Glob is an app you open: double-click the **Glob** shortcut on your desktop or in the Start menu, or run `glob-app`. It opens in its own window and keeps running in the background, with a tray icon (**Open Glob**, **Quit Glob**). *Settings → About* shows where it is installed.

Glob has its own chat, a separate session from your coding agent. It runs on your Claude Code or Codex login. The chat organizes the workspace, writes style and prompts, and generates art. Your coding agent never manages the workspace.

The flow:

1. **Make or pick the art in Glob.**
2. **Your coding agent copies it.** Glob keeps `<game>/glob/ASSETS.md`, an index of every asset: its name, kind, page, the picked image or sound (`winner:`), and any 3D model, rig and animation files. The agent reads it and copies the files it needs into your game's own folders. It never edits or moves anything inside `glob/`.
3. **Missing art stays a placeholder.** The agent keeps the placeholder and tells you what's needed. You make it in Glob.

![Glob with the Pocket Kaiju sample open: chat on the left, the project overview with its key art on the right](docs/screenshots/workspace.webp)

<p align="center"><b>Pocket Kaiju</b>: a sample game made entirely in Glob, with 16 assets, 3D models and sounds.<br>
<a href="https://soulnassim.github.io/Glob-Game-Forge/demo/"><b>▶ Click through it in the live demo</b></a>: the real app in your browser, read-only, no keys needed.</p>

## What you can make

| | |
|---|---|
| 🎨 **2D art** | Characters, items, environments, UI, VFX. A **style bible** and reference **pillars** go into every prompt, per folder, so everything matches |
| 🧊 **3D models** | From the winner image, with poly count, game height, a 3D lineup next to a 1.7 m person, and OBJ / STL / USDZ export |
| 🦴 **Rigs and animations** | Rig a model and add animations from a library |
| 🔊 **Sounds** | Sound effects, voices and music, with a real waveform, trimming and a loop check |
| 🗂️ **Game icons** | Turn any image into square, transparent inventory icons (32 to 512 px) in one click |
| ✂️ **Sprites and parts** | Cut a sheet into parts or new assets, render sprite sheets from a 3D model |
| 📦 **Press kit** | One ZIP with a web page of your key art, winners, turning 3D models and sounds |

<table>
<tr>
<td><img src="docs/screenshots/asset.webp" alt="Asset page for Mochi: winner image, prompt, actions and game tools"><br><sub>An asset: winner, prompt and what to do next.</sub></td>
<td><img src="docs/screenshots/send-sheet.webp" alt="Generate sheet: source, model, price, final prompt and pillar images"><br><sub>Model, price and the exact prompt before anything is sent.</sub></td>
</tr>
<tr>
<td><img src="docs/screenshots/viewer.webp" alt="3D viewer: Mochi with triangle count, size, game height and OBJ, STL and USDZ export"><br><sub>3D viewer with poly count, game height and export.</sub></td>
<td><img src="docs/screenshots/board.webp" alt="Board: every Pocket Kaiju asset on one canvas, grouped by section"><br><sub>The board: every asset on one canvas.</sub></td>
</tr>
<tr>
<td><img src="docs/screenshots/sound.webp" alt="Sound page: Mochi roar waveform with loop check and save selection"><br><sub>Sounds with a waveform you can trim and loop-check.</sub></td>
<td><img src="docs/screenshots/projects.webp" alt="Projects page with Pocket Kaiju's key art as its card"><br><sub>Every project with its key art.</sub></td>
</tr>
</table>

## Get started

### 1. Tell your agent

Paste this into your coding agent (Claude Code, Codex, Cursor, Copilot, OpenCode, Gemini CLI), from your game's folder:

```text
Set up Glob for this game: read https://soulnassim.github.io/Glob-Game-Forge/agent.md
```

Your agent follows [`docs/agent.md`](docs/agent.md). It asks you to approve one install command (a plain [uv](https://docs.astral.sh/uv/) package install, or the installer script when uv is missing; if your agent's safety settings block it, it shows you the command to run yourself). Then it runs `glob connect` in your game folder, which

- makes the game's Glob project (in `<game>/glob/`), or finds the one made before;
- writes a short note into `AGENTS.md`, `CLAUDE.md` and `GEMINI.md` telling your coding agent where the finished art is;
- creates the **Glob** shortcut on your desktop and in the Start menu;
- opens Glob's window on your game.

### 2. Set up Glob

A short setup asks for generator keys (one is enough; without any, Glob makes placeholder files so you can try the whole flow). Keys go into Glob's own page and stay on your computer; they never pass through the agent's chat. Your coding agent needs no server, no restart and no approval.

<details>
<summary><b>Rather do it yourself?</b> Install, then <code>glob connect</code></summary>

```bash
# Already have uv (any OS)
uv tool install --python 3.12 "forge-assets @ https://github.com/SoulNassim/Glob-Game-Forge/archive/refs/heads/main.zip"
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/SoulNassim/Glob-Game-Forge/main/scripts/install.ps1 | iex
```

```bash
# macOS / Linux
curl -LsSf https://raw.githubusercontent.com/SoulNassim/Glob-Game-Forge/main/scripts/install.sh | sh
```

Then, in a new terminal, in your game folder:

| Command | What it does |
|---|---|
| `glob connect` | Makes or finds this game's Glob project, writes the note for your coding agents and opens Glob on it. |
| `glob open` | Opens Glob on this game's project, starting it in the background if needed. |
| `glob shortcut` | Puts a **Glob** shortcut on the desktop and in the Start menu (macOS: `Glob.command` on the desktop, Linux: the app menu). The installers and the first `glob connect` make it for you. |
| `glob` | Runs Glob in this terminal and opens <http://127.0.0.1:8420>. |

Projects are listed in `~/Glob/projects` (a source checkout uses its own `projects/`). A project made for a game keeps its files in that game's `glob/` folder.

From source, with Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/SoulNassim/Glob-Game-Forge.git
cd Glob-Game-Forge
uv run forge
```

Glob opens in its own app window (`glob-app`, the Glob shortcut, or the installers on the [releases page](https://github.com/SoulNassim/Glob-Game-Forge/releases)). It keeps running in the background after you close the window; its tray icon (**Open Glob**, **Quit Glob**) opens or quits it.

</details>

<details>
<summary>What connecting writes into your game folder</summary>

- A short note between `<!-- glob:start -->` and `<!-- glob:end -->` in `AGENTS.md`, `CLAUDE.md` and `GEMINI.md`, saying where the finished art is and that the agent copies it from `glob/ASSETS.md`.
- `glob/`, Glob's project for the game. Its `workspace/` folder holds the art, and `glob/ASSETS.md` indexes it. Your coding agent only reads `ASSETS.md` and copies files out; it never edits `glob/`.

</details>

### 3. Build your game

Ask your agent for something that needs art:

> *"Add three enemy types with placeholder models. I'll make the real ones in Glob."*

Make the art in Glob (open it with the **Glob** shortcut, or `glob open` in your game folder). When you want it in the game, tell your agent. It copies the files from `glob/` using `glob/ASSETS.md`, and keeps placeholders for anything not made yet.

Already have a game? **Projects → Import game** takes a Godot, Unity or Unreal project, or any folder of art.

## More

<details>
<summary><b>Generators</b></summary>

**Settings → Generators.** Keys stay on this computer. You pay the providers directly.

| Generator | Makes |
|---|---|
| Google Gemini | Images |
| Meshy | 3D models, rigging, animation |
| Tripo | 3D models (alternative; pick it under "3D generator") |
| ElevenLabs | Sound effects, voices, music, more image models (sign in, or paste a key) |
| Stability AI | Music and loops (Stable Audio) |

Sound takes live in `<asset>/audio/`. For a voice, the prompt is the line to speak. Drop an MP3/WAV on an asset to add a take. A generator that isn't set up is refused with a message; it never saves a silent placeholder.

</details>

<details>
<summary><b>Import a game</b></summary>

**Projects → Import game** (or tell the Glob chat where your game is) scans a **Godot, Unity or Unreal** project, or any folder of game art, then shows a review step: untick sections, leave assets out, and rename or merge sections.

You get characters with their models, animations and textures, weapons, items and enemies with stats, kits, skies, texture packs, concept art, audio and design docs. Files are copied; your game folder is never changed.

</details>

<details>
<summary><b>Projects, workspaces and storage</b></summary>

- **Projects are folders.** Top-level folders are sections, `README.md` files are pages, and any `.md` with `kind:` and `prompt:` in its header is an asset. Readable, diffable and yours.
- **Export / import a workspace:** the download icon on a project card saves it as a `.zip`; **Projects → Import workspace** (or dropping the file on the projects page) brings it in on another computer.
- **Storage:** Settings → Storage & access shows the projects folder (change it there) and opens each project's folder.
- **Folder access:** Glob only sees its projects folder until you allow more, read or read & write. Keys, `.env` and key files are always off-limits.

</details>

<details>
<summary><b>All the small things</b></summary>

- **Style that sticks.** Images in `pillars/` and rules in `pillars/*.md` stack from the project root down. Any image becomes a pillar from its ⋯ menu. Pillar strength per asset: off, light, normal or strong.
- **Pick fast.** Click a candidate (or press `1`–`9`) to see it big; `←` `→` to step, `Enter` to pick the winner.
- **Generate all missing.** Candidates for every asset that has nothing to pick from yet, two at a time, with a cost estimate first.
- **Board.** Every asset on one zoomable canvas, grouped by section. **3D lineup** stands every model at its game height.
- **Credits first.** The generate sheet warns when a provider ran out of credits and shows Meshy's balance.
- **Live workspace.** Edits from your agent show up at once.
- **⌘K / Ctrl+K** jumps to any asset or action. **G** toggles gallery mode.
- **Make key art** paints a project cover from its pitch.

</details>

<details>
<summary><b>Workspace conventions</b></summary>

| In the workspace folder | What Glob does with it |
|---|---|
| Top-level folder, e.g. `02-characters/` | A section; the number sets its order and is hidden in the title |
| `README.md` in a folder | That folder's page; its `# Heading` is the title, the first paragraph the summary |
| Root `README.md` (or `GAME.md`) | The project's front page |
| `cover.png` / `.jpg` / `.webp` | Hero image of its folder (at the root: the project's key art) |
| `.md` with `kind:` and `prompt:` in its header | An asset. Kinds: character, creature, item, weapon, prop, environment, vehicle, ui, vfx, sfx, voice, music |
| `pillars/*.png` (root = project bible) | Reference images sent with every generation at or below that folder |
| `pillars/*.md` | Style-bible text added in front of every prompt at or below that folder |
| `pillars:` / `aspect:` / `height_m:` in an asset header | Pillar strength, framing (`16:9`…) and height in metres |
| `cover: true` in an asset header | Its winner becomes the project cover |

Generated files are saved next to the asset (`<asset>/candidates|variants|edits|3d/`) and in the project vault with a JSON record of the exact prompt, images and model used.

</details>

<details>
<summary><b>Glob chat</b></summary>

Glob has its own chat next to your projects, separate from your coding agent. It runs on your Claude Code or Codex login. Ask it to organize the workspace, regroup sections, rewrite prompts or generate art. Your coding agent never manages the workspace.

</details>

<details>
<summary><b>Keys, costs and security</b></summary>

Glob itself is free; generation runs on your provider accounts, and the send screen shows the price first.

- Keys are saved in `~/.forge/config.json` (owner-only on macOS/Linux), masked in every API response, and never go into a project or export. They can also come from `GEMINI_API_KEY`, `MESHY_API_KEY`, `TRIPO_API_KEY`, `ELEVENLABS_API_KEY`, `STABILITY_API_KEY`, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`.
- Glob listens on **127.0.0.1** only. `--host` elsewhere prints a warning, because anyone who reaches the port can use your keys.
- Requests from other websites (CSRF) and DNS-rebinding hosts are refused; MCP clients and scripts still work.
- Server settings (`host`, `port`, `allowed_proxy_hosts`) and folder grants can't be changed over HTTP. The asset proxy only fetches from provider hosts.

Found a problem? Please open a private [security advisory](https://github.com/SoulNassim/Glob-Game-Forge/security/advisories/new).

</details>

<details>
<summary><b>Development</b></summary>

```bash
uv run --extra dev pytest -v                 # tests
uv run forge --port 8421 --no-browser        # another port, no browser tab
uv run forge --mcp --project <name>          # MCP server over stdio
uv run forge --desktop                       # Glob's app window
uv run --with pyinstaller python scripts/build_desktop.py   # standalone app
```

| Path | What's there |
|---|---|
| `forge/server/` | FastAPI app and the local-only request guard |
| `forge/agent/` | Glob chat, model endpoints, `connect.py` (game folder link), `glob_prompt.md` |
| `forge/asset_index.py` | Writes `glob/ASSETS.md`, the index the coding agent copies art from |
| `forge/appwindow.py`, `forge/tray.py` | Glob's app window and its tray icon |
| `forge/launcher.py` | Starting Glob in the background |
| `forge/providers/` | Gemini, Meshy, Tripo, ElevenLabs and Stability clients |
| `forge/mcp/` | MCP server (stdio, forwards to the app when it runs) |
| `forge/game_import.py` | Importing Godot / Unity / Unreal projects |
| `forge/ui/` | The web UI (plain HTML/CSS/JS) |
| `docs/` | Website, live demo (`docs/demo/`, rebuild with `scripts/build_demo.py`), screenshots and diagrams |

Pushing a tag like `v1.0.0` runs [`.github/workflows/release.yml`](.github/workflows/release.yml), which builds the Windows installer and the macOS `.dmg` and attaches them to a GitHub release. The Python package is still called `forge`; `forge` and `glob` are the same command.

</details>

## License

[MIT](LICENSE). Use it, fork it, ship games with it.

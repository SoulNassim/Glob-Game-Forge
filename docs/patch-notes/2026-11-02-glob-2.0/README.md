# Glob 2.0

Glob is your game's asset studio. Your coding agent stays where it is: it orders, Glob delivers.

## Highlights

- **Connect once.** Settings → Coding agent writes Glob's part into your game folder. Your agent knows about Glob without being told.
- **Agent Orders.** The agent orders assets with `place_order`. You follow them on a board, from draft to integrated.
- **Delivery with a manifest.** Ready orders are copied to `glob_delivery/<order_id>/` with a SHA-256 `manifest.json`.
- **Sound.** Sound effects, voices and music, plus new generators: Tripo, ElevenLabs audio and Stability AI.
- **Better import.** A review step before anything is imported.

## New

- **Connect your game** for Claude Code, Codex, Cursor, VS Code/Copilot, OpenCode and Gemini CLI. Glob writes only:
  - the `glob` MCP server in each agent's config,
  - a short note between `<!-- glob:start -->` and `<!-- glob:end -->` in `AGENTS.md` / `CLAUDE.md` / `GEMINI.md`,
  - a `glob` skill,
  - `glob_delivery/README.md`.

  Disconnect removes it again.
- **Agent Orders board.** Statuses: draft, filed, in_progress, ready, delivered, integrated, cancelled. The first four are computed live from the workspace.
- **MCP tools** for orders: `place_order`, `list_orders`, `get_order`, `file_order`, `update_order`, `get_deliveries`, `deliver_order`. Also workspace tools, `generate` and `pick_winner`. An MCP prompt `glob` and server `instructions` come with them.
- **One role prompt** (`forge/agent/glob_prompt.md`) for the chat, the skill and the MCP prompt.
- **Ready rules per kind.** Character, creature, item, weapon, prop, vehicle: image + 3D model. Environment, ui, vfx: image. Sfx, voice, music: audio. Override per order, including rig and animations.
- **Automatic delivery**, with a toggle in Settings → Coding agent. A changed asset is delivered again.
- **Generators:** Tripo (alternative 3D generator, chosen in "3D generator"), ElevenLabs sound effects and voices, Stability AI music and loops. A budget card shows spend per project.
- **Sound assets:** kinds `sfx`, `voice`, `music`. Takes in `<asset>/audio/`. A voice picker. Drop an MP3/WAV to add a take.
- **Import review step:** untick sections, leave assets out, rename and merge sections, optionally put "needs art" entries into Agent Orders.
- **Live workspace.** The app updates when files change on disk.
- **Glob chat setting:** Automatic (hidden while a coding agent is connected), Always or Never.

## Changed

- Glob chat is optional and works on any model: Anthropic, OpenAI, OpenRouter, Gemini, Ollama, LM Studio, custom URL, or VS Code Copilot import.
- First-time setup has three steps: Generators, Coding agent, Glob chat.
- The MCP server starts per project with `--project <name>` and `FORGE_PROJECTS_DIR`. When the app runs, it forwards to it.
- `docs`: README, AGENTS.md, CLAUDE.md and the `forge-asset-maker` skill describe the order and deliver loop.

## Removed

- Coding-agent CLI tabs inside the app, and the terminal websocket.
- The `ASSET_REQUIREMENTS.md` flow. Open lines migrate once into orders.
- Dead config keys.

## Fixed

- The MCP server used the wrong project and folder when started from a game repo.
- Receipts claimed QC passed without any QC. They are now a SHA-256 manifest.
- Import pulled in Glob's own deliveries. `glob_delivery` and agent config folders are skipped.
- Setup promised a demo mode. It no longer does.

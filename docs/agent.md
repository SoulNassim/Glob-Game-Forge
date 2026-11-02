# Set up Glob (instructions for coding agents)

You are a coding agent (Claude Code, Codex, Gemini CLI, OpenCode, Cursor, Copilot…). The user wants **Glob**, a free, open-source asset studio that runs on their own computer at `http://127.0.0.1:8420`. Glob makes the game's art, 3D models, animations and sounds, and delivers them into the game.

"Glob" here is this tool, not file glob patterns.

Follow these steps exactly. Run the commands from the user's **game folder** (the project you are working in). Don't ask for API keys, and don't let the user paste keys into this chat: they enter keys in Glob's own browser page.

## 1. Already connected?

If you already have a tool called `open_glob` (MCP server `glob`), call it and skip to step 5.

## 2. Install Glob

Glob installs as a [uv](https://docs.astral.sh/uv/) tool and takes about a minute. Before you run anything, tell the user in one sentence what you are about to install (Glob, the free open-source asset studio, from its GitHub repository) and that they may be asked to approve it.

**If `uv` is installed** (check with `uv --version`), install Glob with one plain package install. It works the same on Windows, macOS and Linux:

```
uv tool install --python 3.12 "forge-assets @ https://github.com/SoulNassim/Glob-Game-Forge/archive/refs/heads/main.zip"
```

**If `uv` is missing**, the installer script adds uv first, then Glob:

- Windows (PowerShell, cmd or Git Bash): `powershell -ExecutionPolicy ByPass -NoProfile -c "irm https://raw.githubusercontent.com/SoulNassim/Glob-Game-Forge/main/scripts/install.ps1 | iex"`
- macOS / Linux: `curl -LsSf https://raw.githubusercontent.com/SoulNassim/Glob-Game-Forge/main/scripts/install.sh | sh`

**If your permission system blocks or denies the install, that is expected:** installing software is the user's call. Don't look for another way to run it. Stop, show the user the exact command, and ask them to approve it or run it themselves in a terminal in the game folder. Continue with step 3 once they say it's done.

## 3. Connect this game and open Glob

Run this in the game folder. Your shell may not see the new `glob` command yet, so use its full path (`uv tool dir --bin` prints the folder it is in):

- Windows: `"%USERPROFILE%\.local\bin\glob.exe" connect` (PowerShell: `& "$env:USERPROFILE\.local\bin\glob.exe" connect`, Git Bash: `"$USERPROFILE/.local/bin/glob.exe" connect`)
- macOS / Linux: `~/.local/bin/glob connect`

`glob connect`:

1. makes the game's Glob project (its files go in `glob/` inside the game folder), or finds the one made before;
2. connects the coding agents for this folder: the MCP server `glob` in `.mcp.json`, `.codex/config.toml` and the others it finds, a short note in `AGENTS.md` / `CLAUDE.md`, and a `glob` skill;
3. creates `glob_delivery/`, the only folder in the game Glob writes to;
4. starts Glob in the background and opens it in the user's browser on this game.

Add `--agents claude,codex,cursor,vscode,opencode,gemini` to choose the agents yourself.

## 4. Hand over to the browser

Tell the user, in about these words:

> Glob is open in your browser (any time later: the Glob shortcut on your desktop). Finish the short setup there: pick a model and paste your image/3D keys (they stay on your computer). Then restart me and approve the `glob` MCP server, so I can order art from Glob.

Stop here and wait. Setup happens in the browser, not in this chat.

If the user asks you to build Glob's workspace for the game before you have Glob's tools, ask them to restart you first. Don't write into `glob/` by hand: the tools carry Glob's rules for how the workspace is laid out.

## 5. After the restart

You now have Glob's tools, and the note in `AGENTS.md` / `CLAUDE.md` explains the loop:

- `open_glob` opens Glob on this game ("open Glob").
- `place_order` orders an asset instead of a placeholder: what it is, its kind and the placeholder path it replaces.
- `list_orders` shows every order's status.
- Finished assets land in `glob_delivery/<order_id>/`, listed in `glob_delivery/manifest.json`. Swap your placeholder, then call `update_order` with `status: integrated`.
- To build or reorganize the workspace itself ("build the Glob workspace for this game"), load the `glob` prompt, call `workspace_overview`, then write it with `workspace_write`: a front page, one folder per part of the game, `pillars/style-bible.md`, and one asset page per character, creature, item, place and sound. Don't generate images unless the user asks: generation spends their credits.

To open Glob yourself, run `glob open` in the game folder. The user can also double-click the **Glob** shortcut that `glob connect` put on their desktop and in the Start menu.

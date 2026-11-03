# Set up Glob (instructions for coding agents)

You are a coding agent (Claude Code, Codex, Gemini CLI, OpenCode, Cursor, Copilot…). The user wants **Glob**, a free, open-source asset studio that runs on their own computer as an app. Glob makes the game's art, 3D models, animations and sounds. You copy the finished files into the game.

"Glob" here is this tool, not file glob patterns.

Follow these steps exactly. Run the commands from the user's **game folder** (the project you are working in). Don't ask for API keys, and don't let the user paste keys into this chat: they enter keys in Glob's own window.

## 1. Already connected?

If `glob/ASSETS.md` exists in the game folder, Glob is set up. Skip to step 4.

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
2. writes `glob/ASSETS.md` and a short note into `AGENTS.md` and `CLAUDE.md` (and `GEMINI.md` when the game has one) telling coding agents where the finished art is;
3. creates the **Glob** shortcut on the desktop and in the Start menu;
4. opens Glob's window on this game.

No restart is needed, and no MCP server needs approving.

## 4. Hand over to the user

Tell the user, in about these words:

> Glob is open (any time later: the Glob shortcut on your desktop). Make your art there. When you want it in the game, tell me — I'll copy it from glob/.

Setup is finished. Don't write into `glob/` by hand.

## 5. Using the art later

When the user asks for art in the game, or for placeholders to be swapped:

1. Read `glob/ASSETS.md`. It lists each asset with its name, kind, page, the picked image or sound (`winner:`), and any 3D model, rig and animation files.
2. For each asset you need, copy the picked files into the game's own folders and point your code at the copies.
3. Keep the placeholder for anything with no `winner:` yet, and tell the user what's missing so they can make it in Glob.

Never edit, move or delete anything inside `glob/`. Making and picking art happens in Glob, not here.

To open Glob again, run `glob open` in the game folder, or use the **Glob** shortcut.

"""CLI entrypoint for Glob (`glob`, `uv run forge` or `./run`).

  glob            run the studio at http://127.0.0.1:8420 and open it in the browser
  glob connect    make or find this game folder's Glob project, connect its coding agents, open Glob on it
  glob open       open Glob on this game folder's project (starting it in the background if needed)
  glob shortcut   put a Glob shortcut on the desktop and in the Start menu / app list
  glob --mcp      the stdio MCP server coding agents start
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
import threading
import time
import uvicorn

from forge import __version__
from forge.config import load_config
from forge.server.security import is_loopback


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="glob",
        description="Glob: your game's asset studio, driven by your coding agent",
        epilog="glob connect   connect this game folder's coding agents to Glob and open it\n"
               "glob open      open Glob on this game folder (starting it if needed)\n"
               "glob shortcut  add a Glob shortcut to the desktop and Start menu",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", choices=["connect", "open", "shortcut"], help="connect, open or shortcut (default: run the studio)")
    parser.add_argument("--game", default=None, help="connect/open: the game folder (default: the current folder)")
    parser.add_argument("--agents", default="", help="connect: comma-separated agents (claude,codex,cursor,vscode,opencode,gemini)")
    parser.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8420)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser on start")
    parser.add_argument("--mcp", action="store_true", help="Run as stdio MCP server instead of HTTP server")
    parser.add_argument("--project", default=None, help="With --mcp: the Glob project this agent works on")
    parser.add_argument("--desktop", action="store_true", help="Open Glob in its app window, starting it in the background if needed")
    args = parser.parse_args()
    # A redirected Windows console is cp1252: the banner's arrows must not crash the start.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    if args.command:
        _launch(args)
        return
    if args.mcp:
        if args.project:
            os.environ["FORGE_PROJECT"] = args.project
        from forge.mcp.server import main as mcp_main
        mcp_main()
        return
    if args.desktop:
        from forge.desktop import main as desktop_main
        desktop_main(args.port)
        return

    cfg = load_config()
    host = args.host or cfg.host or "127.0.0.1"
    port = args.port or cfg.port or 8420
    url = f"http://{host}:{port}"

    print(f"\n  Glob {__version__}: your game's asset studio")
    print(f"  -> Studio       : {url}")
    print(f"  -> Coding agent : run `glob connect` in your game folder (MCP server `glob`)")
    print(f"  -> Mode         : {'Demo Mode (No API keys needed)' if cfg.demo_mode else 'Live Provider Mode'}\n")

    if not args.no_browser:
        def _open() -> None:
            time.sleep(1.1)
            from forge.appwindow import open_window
            open_window(url)
        threading.Thread(target=_open, daemon=True).start()

    if not is_loopback(host):
        print("  !! WARNING: Glob has no login. Binding to a non-local address lets anyone on your network\n"
              "     use your API keys and write project files.\n")
    os.environ["FORGE_BIND_HOST"] = host
    if os.environ.get("FORGE_BACKGROUND") == "1":
        from forge import tray
        tray.start(f"http://127.0.0.1:{port}")  # no window or terminal: the tray icon opens and quits Glob
    uvicorn.run("forge.server.app:app", host=host, port=port, log_level="info")


def _launch(args: argparse.Namespace) -> None:
    from forge import launcher
    from forge.agent import connect as connect_agent
    from forge.storage.project_store import home_owner

    from forge import shortcut

    if args.command == "shortcut":
        try:
            made = shortcut.create()
        except (RuntimeError, OSError) as exc:
            raise SystemExit(f"  !! {exc}") from exc
        print("\n  Glob shortcut added: " + ", ".join(str(p) for p in made))
        print("  Double-click it any time to open Glob.\n")
        return
    game = os.path.abspath(args.game or os.getcwd())
    try:
        if args.command == "connect":
            agents = [a.strip() for a in args.agents.split(",") if a.strip()] or None
            result = connect_agent.connect_game(game, agents)
            project = result["project"]
            labels = ", ".join(connect_agent.TARGETS[t]["label"] for t in result["selected"])
            print(f"\n  Glob project : {project} ({'new, in ' + os.path.join(game, 'glob') if result['created'] else 'already made'})")
            print(f"  -> Connected : {labels} (MCP server `glob`, notes in AGENTS.md)")
            print(f"  -> Deliveries: {result['delivery_folder']}")
        else:
            project = home_owner(Path(game)) or ""
            if not project:
                print("  This folder isn't connected to Glob yet; opening Glob. Run `glob connect` to connect it.")
        opened = launcher.open_glob(project)
    except (RuntimeError, ValueError, OSError) as exc:
        raise SystemExit(f"  !! {exc}") from exc
    print(f"\n  Glob is open: {opened['url']}")
    if args.command == "connect":
        # The first time, a shortcut too: Glob should open without an agent as well.
        if not shortcut.exists():
            try:
                made = shortcut.create()
                print(f"  -> Shortcut  : {made[0]} (open Glob any time, no agent needed)")
            except (RuntimeError, OSError):
                pass
        print("  Finish setup in the browser. Then restart your coding agent so it loads Glob's tools.\n")


if __name__ == "__main__":
    main()

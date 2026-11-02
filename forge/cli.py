"""CLI entrypoint for Forge (`uv run forge` or `./run`)."""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
import threading
import time
import uvicorn

from forge import __version__
from forge.config import load_config
from forge.server.security import is_loopback


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge: Playful, lightweight, agent-drivable game asset maker",
    )
    parser.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8420)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser on start")
    parser.add_argument("--mcp", action="store_true", help="Run as stdio MCP server instead of HTTP server")
    parser.add_argument("--project", default=None, help="With --mcp: the Glob project this agent works on")
    parser.add_argument("--desktop", action="store_true", help="Open Glob in a native window (needs the 'desktop' extra)")
    args = parser.parse_args()
    # A redirected Windows console is cp1252: the banner's arrows must not crash the start.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

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
    print(f"  -> Coding agent : connect your game in Settings → Coding agent (MCP server `glob`)")
    print(f"  -> Mode         : {'Demo Mode (No API keys needed)' if cfg.demo_mode else 'Live Provider Mode'}\n")

    if not args.no_browser:
        def _open() -> None:
            time.sleep(1.1)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    if not is_loopback(host):
        print("  !! WARNING: Glob has no login. Binding to a non-local address lets anyone on your network\n"
              "     use your API keys and write project files.\n")
    os.environ["FORGE_BIND_HOST"] = host
    uvicorn.run("forge.server.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

"""CLI entrypoint for Forge (`uv run forge` or `./run`)."""

from __future__ import annotations

import argparse
import os
import webbrowser
import threading
import time
import uvicorn

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
    parser.add_argument("--desktop", action="store_true", help="Open Glob in a native window (needs the 'desktop' extra)")
    args = parser.parse_args()

    if args.mcp:
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

    print(f"\n  [Forge] Asset Maker v1.0.0")
    print(f"  -> Local Studio : {url}")
    print(f"  -> MCP Server   : {url}/api/mcp (HTTP) or `uv run forge --mcp` (stdio)")
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
              "     use your API keys, write project files and start coding agents.\n")
    os.environ["FORGE_BIND_HOST"] = host
    uvicorn.run("forge.server.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

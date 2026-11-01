"""PTY terminal bridge for the Forge Agent dock (Terminal mode).

Runs the configured command (login shell, Claude Code, Codex, OpenCode, or a custom command) inside
the project folder and streams it over a WebSocket. Only same-origin pages may connect, so other
websites in the browser cannot open a shell on this machine. POSIX only (macOS / Linux).
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import struct
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

TERMINAL_PRESETS = {
    "shell": None,
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
}


def resolve_terminal_command(setting: str) -> list[str]:
    value = (setting or "shell").strip()
    if value in TERMINAL_PRESETS:
        preset = TERMINAL_PRESETS[value]
        if preset is None:
            return [os.environ.get("SHELL") or "/bin/zsh", "-l"]
        return [preset]
    return shlex.split(value)


def allowed_origin(origin: str | None, host: str, port: int) -> bool:
    if not origin:
        return False
    return origin in {f"http://{host}:{port}", f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


async def run_terminal(websocket: WebSocket, command: list[str], cwd: Path) -> None:
    import fcntl
    import pty
    import termios

    await websocket.accept()
    executable = shutil.which(command[0]) if command else None
    if not executable:
        await websocket.send_text(f"\r\n\x1b[2m'{command[0] if command else ''}' is not installed or not on PATH. Pick another command in the dock, or install it first.\x1b[0m\r\n")
        await websocket.close()
        return

    master_fd, slave_fd = pty.openpty()
    env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor", "FORGE_WORKSPACE": str(cwd / "workspace")}
    process = await asyncio.create_subprocess_exec(
        executable,
        *command[1:],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=str(cwd),
        env=env,
        start_new_session=True,
    )
    os.close(slave_fd)
    loop = asyncio.get_running_loop()
    output: asyncio.Queue[bytes | None] = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(master_fd, 65536)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(master_fd)
            output.put_nowait(None)
            return
        output.put_nowait(data)

    loop.add_reader(master_fd, on_readable)

    async def pump_output() -> None:
        while True:
            chunk = await output.get()
            if chunk is None:
                break
            await websocket.send_bytes(chunk)
        await websocket.send_text(f"\r\n\x1b[2m[process exited with code {await process.wait()}]\x1b[0m\r\n")

    pump = asyncio.create_task(pump_output())
    try:
        while True:
            message = await websocket.receive_text()
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "input":
                os.write(master_fd, str(data.get("data", "")).encode())
            elif data.get("type") == "resize":
                cols, rows = int(data.get("cols", 80)), int(data.get("rows", 24))
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 2), max(cols, 2), 0, 0))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        pump.cancel()
        try:
            loop.remove_reader(master_fd)
        except (ValueError, OSError):
            pass
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        os.close(master_fd)

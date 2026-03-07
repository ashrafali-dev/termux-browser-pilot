"""Daemon client — sends commands to the persistent browser daemon."""

import asyncio
import json
import os
import sys

SOCKET_PATH = os.path.expanduser("~/.tbp/daemon.sock")
PID_PATH = os.path.expanduser("~/.tbp/daemon.pid")


def is_daemon_running():
    """Check if daemon is running."""
    from ._utils import read_pid_file
    pid = read_pid_file(PID_PATH)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


async def ensure_daemon(browser="firefox"):
    """Start daemon if not running. Waits for socket."""
    if is_daemon_running() and os.path.exists(SOCKET_PATH):
        return

    # Clean stale files
    for p in (SOCKET_PATH, PID_PATH):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass

    # Start daemon in background
    # Use absolute path to package so it works from any CWD
    src_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(src_dir)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "src.daemon", "start", "--browser", browser,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=project_dir,
    )
    _, stderr_bytes = await proc.communicate()

    if proc.returncode and proc.returncode != 0:
        stderr = stderr_bytes.decode() if stderr_bytes else ""
        raise RuntimeError(
            f"Daemon failed to start (exit {proc.returncode}). "
            f"{stderr[:200].strip() or 'Check ~/.tbp/daemon.log'}"
        )

    # Wait for socket to appear and become connectable
    for i in range(60):
        if os.path.exists(SOCKET_PATH):
            # Verify socket is actually listening (not just file created)
            try:
                r, w = await asyncio.open_unix_connection(SOCKET_PATH)
                w.close()
                await w.wait_closed()
                return
            except (ConnectionRefusedError, OSError):
                pass  # Socket file exists but server not ready yet
        await asyncio.sleep(0.5)

    raise RuntimeError(
        "Daemon failed to start within 30s. "
        "Check ~/.tbp/daemon.log for errors."
    )


async def send_command(action, params=None, timeout=120, browser="firefox"):
    """Send command to daemon and return response dict.

    Auto-starts daemon if not running.
    """
    await ensure_daemon(browser=browser)

    reader, writer = await asyncio.open_unix_connection(
        SOCKET_PATH, limit=32 * 1024 * 1024)  # 32MB limit for full-page screenshots
    try:
        request = {
            "id": 1,
            "action": action,
            "params": params or {},
        }
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()

        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not line:
            raise ConnectionError("Daemon closed connection")
        return json.loads(line)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

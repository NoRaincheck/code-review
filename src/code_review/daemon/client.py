"""Client side of the daemon control socket.

Used by the post-commit hook and by CLI subcommands that need the daemon. Every call
raises :class:`DaemonUnavailable` rather than hanging, so a hook can fail fast and exit 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from pathlib import Path
from typing import Any

from .. import paths
from . import protocol, supervisor


class DaemonUnavailable(Exception):
    """The daemon socket could not be reached."""


class RequestFailed(Exception):
    """The daemon understood the request and refused it."""


async def call(
    op: str, params: dict[str, Any] | None = None, *, timeout: float = 10.0
) -> dict[str, Any]:
    """Send one request and return its ``data`` payload.

    Raises DaemonUnavailable if the socket cannot be reached, RequestFailed if the
    daemon answers with ``ok: false``.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(paths.socket_path())), timeout=timeout
        )
    except (OSError, TimeoutError) as exc:
        raise DaemonUnavailable(f"cannot reach the daemon at {paths.socket_path()}: {exc}") from exc

    try:
        await protocol.write_message(writer, {"op": op, "params": params or {}})
        frame = await asyncio.wait_for(protocol.read_message(reader), timeout=timeout)
    except protocol.ProtocolError as exc:
        raise RequestFailed(f"malformed response: {exc}") from exc
    except (OSError, TimeoutError) as exc:
        raise DaemonUnavailable(f"daemon connection failed: {exc}") from exc
    finally:
        # Best-effort teardown: a half-closed socket cannot fail the request we just made.
        writer.close()
        with contextlib.suppress(OSError, Exception):
            await writer.wait_closed()

    if frame is None:
        raise DaemonUnavailable("daemon closed the connection without responding")
    if not frame.get("ok"):
        raise RequestFailed(str(frame.get("error") or "request failed"))
    data = frame.get("data")
    return data if isinstance(data, dict) else {}


def ping(timeout: float = 2.0) -> bool:
    """Blocking liveness probe used by ``code-review status``."""
    path = paths.socket_path()
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(protocol.encode_message({"op": "ping", "params": {}}))
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    return False
                buffer += chunk
        import json

        return bool(json.loads(buffer.decode("utf-8")).get("ok"))
    except OSError, ValueError:
        return False


def enqueue(sha: str, repo: Path, branch: str | None = None) -> dict[str, Any]:
    """Synchronous enqueue used by the post-commit hook."""
    return asyncio.run(
        call(
            "enqueue",
            {"repo": str(repo), "sha": sha, "branch": branch},
            timeout=5.0,
        )
    )


def ensure_and_enqueue(sha: str, repo: Path, branch: str | None = None) -> dict[str, Any]:
    """Start the daemon if needed, then enqueue. Blocking; for hook use."""
    supervisor.reap_stale_socket()
    if not ping():
        supervisor.spawn_detached()
    return enqueue(sha, repo, branch)

"""The daemon's Unix-socket server.

Binds ``~/.codereview/daemon.sock``, dispatches one-line JSON requests to the worker, and
shuts down cleanly on SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from .. import paths
from . import protocol, supervisor
from .worker import Worker

log = logging.getLogger(__name__)

ORPHAN_CHECK_SECONDS = 5.0


def _require_repo(params: dict[str, Any]) -> Path:
    repo = params.get("repo")
    if not isinstance(repo, str) or not repo:
        raise ValueError("params.repo must be an absolute path to a git repository")
    return Path(repo)


class DaemonServer:
    """Listens on the control socket and services requests against a :class:`Worker`."""

    def __init__(self, worker: Worker | None = None) -> None:
        self.worker = worker or Worker()
        self._server: asyncio.AbstractServer | None = None

    # -- request handlers -------------------------------------------------

    def handle(self, request: protocol.Request) -> protocol.Response:
        """Dispatch one request. Returns a Response; never raises."""
        try:
            match request.op:
                case "ping":
                    return protocol.Response(ok=True, data={"pong": True, "pid": os.getpid()})
                case "enqueue":
                    return self._handle_enqueue(request.params)
                case "review":
                    return self._handle_review(request.params)
                case "status":
                    return self._handle_status()
                case "shutdown":
                    return self._handle_shutdown()
                case _:
                    return protocol.Response(ok=False, error=f"unknown op {request.op!r}")
        except Exception as exc:
            log.exception("request %s failed", request.op)
            return protocol.Response.from_exception(exc)

    def _handle_enqueue(self, params: dict[str, Any]) -> protocol.Response:
        repo = _require_repo(params)
        sha = params.get("sha")
        if not isinstance(sha, str) or not sha:
            return protocol.Response(ok=False, error="params.sha is required")
        database = self.worker.database_for(repo)
        review_id, created = database.enqueue(sha, params.get("branch"))
        self.worker.notify()
        return protocol.Response(
            ok=True,
            data={"id": review_id, "created": created, "sha": sha},
        )

    def _handle_review(self, params: dict[str, Any]) -> protocol.Response:
        repo = _require_repo(params)
        sha = params.get("sha")
        if not isinstance(sha, str) or not sha:
            return protocol.Response(ok=False, error="params.sha is required")
        force = bool(params.get("force", False))
        review_id, created = self.worker.database_for(repo).enqueue(
            sha, params.get("branch"), force=force
        )
        self.worker.notify()
        return protocol.Response(ok=True, data={"id": review_id, "created": created})

    def _handle_status(self) -> protocol.Response:
        snapshot = self.worker.snapshot()
        return protocol.Response(
            ok=True,
            data={
                **snapshot.to_dict(),
                "pid": os.getpid(),
                "socket": str(paths.socket_path()),
                "queue_depth": self.worker.queue_depth(),
            },
        )

    def _handle_shutdown(self) -> protocol.Response:
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, lambda: loop.create_task(self._shutdown()))
        return protocol.Response(ok=True, data={"stopping": True})

    async def _shutdown(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        await self.worker.stop()

    # -- connection handling ---------------------------------------------

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.debug("client connected: %s", peer)
        try:
            while True:
                try:
                    frame = await protocol.read_message(reader)
                except protocol.ProtocolError as exc:
                    await protocol.write_message(
                        writer, {"ok": False, "error": f"protocol error: {exc}"}
                    )
                    return
                if frame is None:
                    return

                try:
                    request = protocol.Request.from_dict(frame)
                except protocol.ProtocolError as exc:
                    await protocol.write_message(
                        writer, {"ok": False, "error": f"protocol error: {exc}"}
                    )
                    return

                response = self.handle(request)
                await protocol.write_message(
                    writer,
                    {"ok": response.ok, "data": response.data, "error": response.error},
                )
        except ConnectionResetError, BrokenPipeError:
            log.debug("client disconnected abruptly: %s", peer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("error serving client %s", peer)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def run(self, socket_path: Path | None = None) -> None:
        """Bind and serve until stopped."""
        target = socket_path or paths.socket_path()
        paths.ensure_home()
        supervisor.reap_stale_socket()

        self._server = await asyncio.start_unix_server(self._serve_client, path=str(target))
        # Owner-only: the socket can queue reviews, so it must not be world-writable.
        target.chmod(0o600)
        paths.pid_path().write_text(str(os.getpid()), encoding="utf-8")
        self.worker.start()

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal_name, stop.set)

        orphan_watch = asyncio.create_task(self._watch_for_orphan(target, stop))
        log.info("daemon listening on %s (pid %d)", target, os.getpid())
        try:
            await stop.wait()
        finally:
            orphan_watch.cancel()
            log.info("daemon shutting down")
            with contextlib.suppress(Exception):
                if self._server is not None:
                    self._server.close()
                    await self._server.wait_closed()
            await self.worker.stop()
            for path in (target, paths.pid_path()):
                with contextlib.suppress(OSError):
                    path.unlink()
            log.info("daemon stopped")

    async def _watch_for_orphan(self, target: Path, stop: asyncio.Event) -> None:
        """Shut down if this daemon's own bookkeeping disappears.

        ``stop_daemon`` identifies its target by reading the pid file, so a daemon whose
        home directory is deleted can no longer be stopped by name and would linger
        forever. Exiting when the pid file or socket goes away means the daemon cannot
        outlive the state that describes it.
        """
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=ORPHAN_CHECK_SECONDS)
                return
            except TimeoutError:
                pass
            if not paths.pid_path().exists() or not target.exists():
                log.warning("pid file or socket vanished; shutting down as an orphan")
                stop.set()
                return


async def serve_forever(worker: Worker | None = None) -> None:
    """Entry point used by ``python -m code_review daemon run``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    await DaemonServer(worker).run()

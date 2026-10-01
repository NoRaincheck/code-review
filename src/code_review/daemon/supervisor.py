"""Starting, stopping, and finding the daemon.

The post-commit hook must never block, so it runs this: if the socket refuses a
connection, start the daemon detached and retry. Two processes racing to start the
daemon must not produce two daemons, so startup takes an exclusive lock file.

Unix domain sockets, ``fcntl`` and POSIX signals are used throughout, so the daemon requires
a POSIX platform. ``fcntl`` is therefore imported lazily and daemon operations raise a clear
error elsewhere, rather than making read-only commands such as ``show`` and ``list``
unimportable on Windows.
"""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .. import paths

START_TIMEOUT = 15.0
POLL_INTERVAL = 0.05


class UnsupportedPlatform(RuntimeError):
    """The background daemon needs POSIX facilities this platform lacks."""


def _fcntl() -> object:
    """Import ``fcntl`` on demand, with an explanation when it is unavailable."""
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - POSIX-only path
        raise UnsupportedPlatform(
            "the background daemon needs a POSIX platform: it uses Unix domain "
            "sockets, fcntl locks and POSIX signals. Read-only commands "
            "(`show`, `list`, `doctor`) and `review --inline` still work. On Windows, "
            "use --inline, or run the daemon under WSL."
        ) from exc
    return fcntl


class DaemonAlreadyRunning(RuntimeError):
    """Another daemon already holds the socket."""


class DaemonNotRunning(RuntimeError):
    """No daemon is running."""


def _lock_path() -> Path:
    return paths.home_dir() / "daemon.lock"


class _StartupLock:
    """Exclusive cross-process lock so only one daemon is ever spawned."""

    def __init__(self) -> None:
        paths.ensure_home()
        self._fd: int | None = None

    def __enter__(self) -> _StartupLock:
        fcntl = _fcntl()
        self._fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            fcntl = _fcntl()
            fcntl.flock(self._fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
            os.close(self._fd)
            self._fd = None


def daemon_pid() -> int | None:
    """PID of a live daemon, or None. A pid file naming a dead process is treated as absent."""
    path = paths.pid_path()
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except ValueError, OSError:
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # exists, owned by someone else
    return pid


def is_running() -> bool:
    return daemon_pid() is not None and paths.socket_path().exists()


def reap_stale_socket() -> bool:
    """Remove a socket file with no daemon behind it. Returns True if one was removed.

    Without this, a daemon killed with SIGKILL leaves a socket that refuses connections
    forever, and the hook would never be able to start a replacement.
    """
    socket = paths.socket_path()
    if not socket.exists():
        return False
    if daemon_pid() is not None:
        return False
    try:
        socket.unlink()
    except OSError:
        return False
    return True


def wait_for_socket(timeout: float = START_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if paths.socket_path().exists():
            return True
        time.sleep(POLL_INTERVAL)
    return paths.socket_path().exists()


# Handles for daemons we started. Retained deliberately: a Popen whose child is still
# running warns when garbage collected, and the daemon outliving this process is the
# intended outcome. Entries are dropped once the child is seen to have exited.
_spawned: list[subprocess.Popen[bytes]] = []


def _prune_spawned() -> None:
    _spawned[:] = [proc for proc in _spawned if proc.poll() is None]


def spawn_detached(timeout: float = START_TIMEOUT) -> int:
    """Start the daemon in its own session, detached from this process's stdio.

    Raises DaemonAlreadyRunning if one is already up.
    """
    with _StartupLock():
        _prune_spawned()
        if is_running():
            pid = daemon_pid()
            assert pid is not None
            return pid

        reap_stale_socket()
        paths.ensure_home()
        log_path = paths.daemon_log_path()
        # Unbuffered: the log must be useful while the daemon is still starting up. Not a
        # context manager, because the child needs the descriptor after this returns.
        log_handle = log_path.open("ab", buffering=0)

        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "code_review", "daemon", "run"],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                cwd=str(Path.home()),
                start_new_session=True,
                env=_daemon_env(),
                close_fds=True,
            )
        finally:
            log_handle.close()

        if not wait_for_socket(timeout):
            process.terminate()
            process.wait(timeout=5)
            raise DaemonAlreadyRunning(
                f"daemon did not create {paths.socket_path()} within {timeout}s. See {log_path}"
            )
        _spawned.append(process)
        return process.pid


def _daemon_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("CODEREVIEW_HOME", str(paths.home_dir()))
    # Unbuffered so the log is useful while the daemon is still starting up.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def stop_daemon(timeout: float = 10.0) -> None:
    """Ask the daemon to shut down, escalating to SIGKILL if it will not."""
    pid = daemon_pid()
    if pid is None:
        raise DaemonNotRunning("no daemon is running")

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if daemon_pid() is None:
            _cleanup_files()
            return
        time.sleep(POLL_INTERVAL)

    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    _cleanup_files()


def _is_running(pid: int) -> bool:
    """True if *pid* is still executing, reaping it first if it is our dead child.

    A killed child stays visible to ``os.kill(pid, 0)`` until it is waited on, so a plain
    signal-0 probe would report a zombie as alive and every reap would look like a failure.
    """
    for _ in range(2):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError, OSError:
            # Not our child, so nobody else will reap it; signal 0 is all we get.
            return True
    return True


def reap(pids: list[int], timeout: float = 10.0) -> list[int]:
    """SIGKILL any of *pids* that are still alive. Returns the ones that survived.

    Used by test teardown, where the pid file may already have been deleted and so
    ``stop_daemon`` can no longer identify its target. Falling back to a direct signal
    keeps a test run from leaking daemons.
    """
    survivors: list[int] = []
    _prune_spawned()
    for pid in pids:
        if not _is_running(pid):
            continue
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _is_running(pid):
                break
            time.sleep(POLL_INTERVAL)
        else:
            survivors.append(pid)
    _prune_spawned()
    return survivors


def _cleanup_files() -> None:
    for path in (paths.socket_path(), paths.pid_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - unusual filesystem state
            if exc.errno != errno.ENOENT:
                raise


def ensure_running() -> int:
    """Idempotently make sure a daemon is listening. Returns its pid."""
    reap_stale_socket()
    if is_running():
        pid = daemon_pid()
        assert pid is not None
        return pid
    return spawn_detached()

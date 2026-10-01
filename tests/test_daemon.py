"""Worker and daemon server tests: the queue, crash recovery, and the socket protocol.

These run a real server on a real Unix socket in a tmp dir, and drive a real worker with
the scripted fake agent. No vendor CLI, no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from code_review import paths
from code_review.daemon import client as daemon_client_module
from code_review.daemon import protocol, supervisor
from code_review.daemon.server import DaemonServer
from code_review.daemon.worker import Worker
from code_review.db import Database, Status

FAKE_AGENT = str(Path(__file__).parent / "fake_agent.py")


@pytest.fixture
def configured_repo(repo: Path, second_commit: str, tmp_path: Path) -> tuple[Path, str, Path]:
    """A repo with a fake agent configured, plus its script path."""
    script = tmp_path / "agent.json"
    script.write_text(json.dumps({"chunks": ["1 issue found.", "\n\n- app.py:3 division by zero"]}))
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n"
        "review_timeout_seconds = 60\n",
        encoding="utf-8",
    )
    return repo, second_commit, script


async def drain(worker: Worker, *, timeout: float = 30.0) -> None:
    """Run the worker's loop until no review for the registered repos is in flight.

    Waits for a terminal state rather than just an empty queue: claim_next removes a row
    from 'queued' immediately, so queue depth alone reports empty while a review is still
    running and an assertion after it would race the worker.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        pending = False
        for database in worker._databases.values():
            counts = database.counts()
            if counts.get(Status.QUEUED.value, 0) or counts.get(Status.RUNNING.value, 0):
                pending = True
                break
        if not pending:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("worker did not finish the queue")


# -- the worker loop -------------------------------------------------------


async def test_worker_completes_a_queued_review(configured_repo: tuple[Path, str, Path]) -> None:
    repo, sha, _ = configured_repo
    worker = Worker()
    worker.start()
    try:
        worker.database_for(repo).enqueue(sha, "main")
        worker.notify()
        await drain(worker)
    finally:
        await worker.stop()

    database = Database.for_repo(repo)
    try:
        review = database.get(sha)
        assert review is not None
        assert review.status is Status.COMPLETED
        assert "division by zero" in (review.markdown or "")
        assert review.stop_reason == "end_turn"
        assert review.agent
    finally:
        database.close()


async def test_worker_records_a_failure_for_a_missing_agent(repo: Path, second_commit: str) -> None:
    (repo / ".codereview.toml").write_text(
        'review_agent = ["/nonexistent/agent-binary"]\n', encoding="utf-8"
    )
    worker = Worker()
    worker.start()
    try:
        worker.database_for(repo).enqueue(second_commit)
        worker.notify()
        await drain(worker)
    finally:
        await worker.stop()

    database = Database.for_repo(repo)
    try:
        review = database.get(second_commit)
        assert review is not None
        assert review.status is Status.FAILED
        assert "not found" in (review.error or "")
    finally:
        database.close()


async def test_worker_recovers_interrupted_reviews_on_first_touch(
    repo: Path, second_commit: str
) -> None:
    """A row left 'running' by a dead daemon must be re-queued, not lost."""
    database = Database.for_repo(repo)
    try:
        review_id, _ = database.enqueue(second_commit)
        database.claim_next()  # simulates a daemon that died mid-review
        assert database.get_by_id(review_id).status is Status.RUNNING
    finally:
        database.close()

    worker = Worker()
    worker.database_for(repo)
    database = Database.for_repo(repo)
    try:
        recovered = database.get(second_commit)
        assert recovered is not None
        assert recovered.status is Status.QUEUED
    finally:
        database.close()
        await worker.stop()


async def test_worker_processes_two_repos_round_robin(
    configured_repo: tuple[Path, str, Path], tmp_path: Path
) -> None:
    repo, sha, _ = configured_repo
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other, check=True)
    subprocess.run(["git", "config", "user.email", "o@e.com"], cwd=other, check=True)
    subprocess.run(["git", "config", "user.name", "O"], cwd=other, check=True)
    (other / "f.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=other, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=other, check=True)
    other_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=other, capture_output=True, text=True, check=True
    ).stdout.strip()
    (other / ".codereview.toml").write_text(
        (repo / ".codereview.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    worker = Worker()
    worker.start()
    try:
        worker.database_for(repo).enqueue(sha)
        worker.database_for(other).enqueue(other_sha)
        worker.notify()
        await drain(worker)
    finally:
        await worker.stop()

    for path, target in ((repo, sha), (other, other_sha)):
        database = Database.for_repo(path)
        try:
            assert database.get(target).status is Status.COMPLETED
        finally:
            database.close()


async def test_worker_snapshot_reports_current_review(
    configured_repo: tuple[Path, str, Path],
) -> None:
    repo, _sha, _ = configured_repo
    worker = Worker()
    worker.register(repo)
    snapshot = worker.snapshot()
    assert snapshot.repos == [str(repo.resolve())]
    assert worker.queue_depth() == 0


async def test_worker_marks_a_timed_out_review_cancelled_with_partial_markdown(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    """A review that overruns is 'cancelled' and keeps what it managed to produce."""
    script = tmp_path / "slow.json"
    script.write_text(json.dumps({"chunks": ["partial findings"], "hang_after_chunks": True}))
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n"
        "review_timeout_seconds = 1\n",
        encoding="utf-8",
    )

    worker = Worker()
    worker.start()
    try:
        worker.database_for(repo).enqueue(second_commit)
        worker.notify()
        await drain(worker)
    finally:
        await worker.stop()

    database = Database.for_repo(repo)
    try:
        review = database.get(second_commit)
        assert review is not None
        assert review.status is Status.CANCELLED, review.error
        assert review.stop_reason == "cancelled"
        assert review.markdown is not None
        assert "partial findings" in review.markdown
        # Which agent timed out is worth knowing even though nothing usable came back.
        assert review.agent == sys.executable
    finally:
        database.close()


async def test_worker_marks_a_failed_review_with_the_real_error(
    repo: Path, second_commit: str
) -> None:
    """A failure must record why, not a generic placeholder."""
    (repo / ".codereview.toml").write_text(
        'review_agent = ["/nonexistent/agent-binary"]\n', encoding="utf-8"
    )
    worker = Worker()
    worker.start()
    try:
        worker.database_for(repo).enqueue(second_commit)
        worker.notify()
        await drain(worker)
    finally:
        await worker.stop()

    database = Database.for_repo(repo)
    try:
        review = database.get(second_commit)
        assert review is not None
        assert review.status is Status.FAILED
        error = review.error or ""
        assert "not found" in error
        assert "/nonexistent/agent-binary" in error, error
    finally:
        database.close()


async def test_worker_stop_is_safe_when_idle(repo: Path) -> None:
    worker = Worker()
    worker.start()
    await worker.stop()
    assert worker.snapshot().running is False


async def test_worker_stop_is_safe_without_start(repo: Path) -> None:
    await Worker().stop()


# -- server request handling ----------------------------------------------


def test_ping_is_answered(repo: Path) -> None:
    response = DaemonServer().handle(protocol.Request(op="ping"))
    assert response.ok
    assert response.data["pong"] is True


def test_unknown_op_is_refused(repo: Path) -> None:
    response = DaemonServer().handle(protocol.Request(op="nonsense"))
    assert not response.ok
    assert "unknown op" in (response.error or "")


def test_enqueue_requires_a_repo(repo: Path) -> None:
    response = DaemonServer().handle(protocol.Request(op="enqueue", params={"sha": "a" * 40}))
    assert not response.ok


def test_enqueue_requires_a_sha(repo: Path) -> None:
    response = DaemonServer().handle(protocol.Request(op="enqueue", params={"repo": str(repo)}))
    assert not response.ok


async def test_enqueue_creates_a_review(repo: Path) -> None:
    worker = Worker()
    server = DaemonServer(worker)
    try:
        response = server.handle(
            protocol.Request(
                op="enqueue", params={"repo": str(repo), "sha": "a" * 40, "branch": "main"}
            )
        )
        assert response.ok
        assert response.data["created"] is True
        database = Database.for_repo(repo)
        try:
            review = database.get("a" * 40)
            assert review is not None
            assert review.status is Status.QUEUED
        finally:
            database.close()
    finally:
        await worker.stop()


async def test_enqueue_is_idempotent(repo: Path) -> None:
    worker = Worker()
    server = DaemonServer(worker)
    try:
        params = {"repo": str(repo), "sha": "a" * 40}
        first = server.handle(protocol.Request(op="enqueue", params=params))
        second = server.handle(protocol.Request(op="enqueue", params=params))
        assert first.data["id"] == second.data["id"]
        assert second.data["created"] is False
    finally:
        await worker.stop()


async def test_status_reports_queue_depth(repo: Path) -> None:
    worker = Worker()
    server = DaemonServer(worker)
    try:
        server.handle(protocol.Request(op="enqueue", params={"repo": str(repo), "sha": "a" * 40}))
        response = server.handle(protocol.Request(op="status"))
        assert response.ok
        assert response.data["queue_depth"] == 1
    finally:
        await worker.stop()


def test_bad_request_never_raises(repo: Path) -> None:
    """A malformed request must be refused, not crash the daemon."""
    response = DaemonServer().handle(protocol.Request(op="enqueue", params={"repo": 42}))
    assert not response.ok


# -- protocol framing ------------------------------------------------------


def test_request_parses_from_a_string() -> None:
    request = protocol.Request.parse('{"op":"ping","params":{"a":1}}')
    assert request.op == "ping"
    assert request.params == {"a": 1}


def test_request_defaults_params_to_empty() -> None:
    assert protocol.Request.parse('{"op":"ping"}').params == {}


@pytest.mark.parametrize(
    "raw",
    ['{"params":{}}', "[]", '"str"', "not json", '{"op":123}', '{"op":"x","params":[]}'],
)
def test_malformed_requests_are_rejected(raw: str) -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.Request.parse(raw)


async def test_read_message_returns_none_at_eof() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    assert await protocol.read_message(reader) is None


async def test_write_then_read_round_trips(short_socket_dir: Path) -> None:
    """A frame written to a socket arrives at the other end as a parsed dict."""
    socket_path = short_socket_dir / "rt.sock"
    server_received: asyncio.Queue[dict] = asyncio.Queue()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            frame = await protocol.read_message(reader)
            if frame is None:
                writer.close()
                return
            await server_received.put(frame)
            await protocol.write_message(writer, {"ok": True, "data": {"echo": frame["op"]}})

    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    async with server:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            await protocol.write_message(writer, {"op": "ping", "params": {"x": 1}})
            assert await asyncio.wait_for(server_received.get(), 2) == {
                "op": "ping",
                "params": {"x": 1},
            }
            reply = await protocol.read_message(reader)
            assert reply == {"ok": True, "data": {"echo": "ping"}}
        finally:
            writer.close()
            await writer.wait_closed()


# -- the live socket -------------------------------------------------------


@pytest.fixture
def short_socket_dir() -> Path:
    """A temp dir with a short path.

    AF_UNIX paths are capped near 104 bytes on macOS, and pytest's tmp_path is long enough
    to exceed it. Real deployments use ``~/.codereview/daemon.sock``, which is short.
    """
    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="cr"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
async def live_server(repo: Path, isolated_home: Path):
    """A real daemon on its real socket path, serving until cancelled."""
    socket_path = paths.socket_path()
    server = DaemonServer(Worker())
    task = asyncio.create_task(server.run(socket_path))
    for _ in range(300):
        if socket_path.exists():
            break
        await asyncio.sleep(0.02)
    assert socket_path.exists(), "server did not bind"
    try:
        yield server
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError, Exception:
            pass
        # The worker owns open Database handles; close them so they are not collected
        # as unraisable ResourceWarnings during this event loop's teardown.
        await server.worker.stop()


async def test_client_can_ping_a_live_server(live_server: DaemonServer, repo: Path) -> None:
    from code_review.daemon import client

    data = await client.call("ping", timeout=5.0)
    assert data["pong"] is True


async def test_enqueue_over_the_socket_creates_a_review(
    live_server: DaemonServer, repo: Path
) -> None:
    from code_review.daemon import client

    result = await client.call(
        "enqueue", {"repo": str(repo), "sha": "b" * 40, "branch": "main"}, timeout=5.0
    )
    assert result["created"] is True
    database = Database.for_repo(repo)
    try:
        assert database.get("b" * 40) is not None
    finally:
        database.close()


async def test_client_reports_a_refusal(live_server: DaemonServer, repo: Path) -> None:
    from code_review.daemon import client

    with pytest.raises(client.RequestFailed):
        await client.call("enqueue", {"repo": str(repo)}, timeout=5.0)


async def test_client_reports_an_unreachable_daemon(isolated_home: Path) -> None:
    from code_review.daemon import client as sync_client

    with pytest.raises(sync_client.DaemonUnavailable):
        await sync_client.call("ping", timeout=1.0)


async def test_ping_returns_false_when_no_socket(isolated_home: Path) -> None:
    from code_review.daemon import client as sync_client

    assert sync_client.ping() is False


async def test_malformed_frame_gets_an_error_reply_not_a_dead_daemon(
    live_server: DaemonServer,
) -> None:
    """A client sending junk must be answered, not crash the daemon."""
    reader, writer = await asyncio.open_unix_connection(str(paths.socket_path()))
    try:
        writer.write(b"this is not json\n")
        await writer.drain()
        reply = await asyncio.wait_for(protocol.read_message(reader), 5)
        assert reply is not None
        assert reply["ok"] is False
        assert "protocol error" in reply["error"]
    finally:
        writer.close()
        await writer.wait_closed()

    # The daemon is still answering afterwards.
    assert (await asyncio.wait_for(protocol.read_message(reader), 1)) is None


async def test_a_frame_that_is_not_an_object_is_refused(live_server: DaemonServer) -> None:
    reader, writer = await asyncio.open_unix_connection(str(paths.socket_path()))
    try:
        writer.write(b"[1, 2, 3]\n")
        await writer.drain()
        reply = await asyncio.wait_for(protocol.read_message(reader), 5)
        assert reply is not None
        assert reply["ok"] is False
    finally:
        writer.close()
        await writer.wait_closed()


async def test_a_client_that_disconnects_mid_frame_is_survived(
    live_server: DaemonServer,
) -> None:
    """The hook can vanish at any moment; the daemon must stay up."""
    for _ in range(3):
        _reader, writer = await asyncio.open_unix_connection(str(paths.socket_path()))
        writer.write(b'{"op": "pi')  # truncated, then close
        writer.close()
        await writer.wait_closed()

    assert (await daemon_client_module.call("ping", timeout=5))["pong"] is True


async def test_server_survives_a_client_sending_a_partial_frame(
    live_server: DaemonServer,
) -> None:
    reader, writer = await asyncio.open_unix_connection(str(paths.socket_path()))
    try:
        writer.write(b'{"op": "ping", "params": {}}')  # no newline, never completed
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.write(b"\n")
        await writer.drain()
        reply = await asyncio.wait_for(protocol.read_message(reader), 5)
        assert reply is not None
        assert reply["ok"] is True
    finally:
        writer.close()
        await writer.wait_closed()


async def test_shutdown_request_stops_the_server(
    live_server: DaemonServer, isolated_home: Path
) -> None:
    result = await daemon_client_module.call("shutdown", timeout=5)
    assert result["stopping"] is True
    for _ in range(100):
        if not paths.socket_path().exists():
            break
        await asyncio.sleep(0.05)
    assert not paths.socket_path().exists()


# -- supervisor ------------------------------------------------------------


def test_daemon_pid_is_none_when_absent(isolated_home: Path) -> None:
    assert supervisor.daemon_pid() is None


def test_is_running_is_false_when_absent(isolated_home: Path) -> None:
    assert supervisor.is_running() is False


def test_a_stale_socket_is_reaped(isolated_home: Path) -> None:
    paths.ensure_home()
    paths.socket_path().write_text("stale", encoding="utf-8")
    assert supervisor.reap_stale_socket() is True
    assert not paths.socket_path().exists()


def test_reaping_does_nothing_without_a_socket(isolated_home: Path) -> None:
    assert supervisor.reap_stale_socket() is False


def test_a_dead_pid_file_is_treated_as_absent(isolated_home: Path) -> None:
    paths.ensure_home()
    paths.pid_path().write_text("999999", encoding="utf-8")
    assert supervisor.daemon_pid() is None


def test_garbage_pid_file_is_treated_as_absent(isolated_home: Path) -> None:
    paths.ensure_home()
    paths.pid_path().write_text("not-a-pid", encoding="utf-8")
    assert supervisor.daemon_pid() is None


def test_stop_raises_when_no_daemon(isolated_home: Path) -> None:
    from code_review.daemon.supervisor import DaemonNotRunning

    with pytest.raises(DaemonNotRunning):
        supervisor.stop_daemon()


async def test_spawn_stop_leaves_no_running_child(
    repo: Path, isolated_home: Path, short_socket_dir: Path
) -> None:
    """A started daemon must be reaped by stop(), not linger as a live child.

    The subprocess handle is asserted on rather than assumed: a leaked daemon would hold a
    deleted socket and would surface later as an unraisable ResourceWarning.
    """
    pid = supervisor.spawn_detached()
    assert supervisor.daemon_pid() == pid
    supervisor.stop_daemon()
    assert supervisor.daemon_pid() is None
    for process in supervisor._spawned:
        process.wait(timeout=5)


async def test_daemon_exits_when_its_pid_file_disappears(
    repo: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon must not outlive the state that identifies it.

    ``stop_daemon`` finds its target by reading the pid file. If that file is deleted while
    the daemon runs, the daemon becomes unreachable by name and would linger forever holding
    a deleted socket. The orphan watchdog makes it exit instead.
    """
    pid = supervisor.spawn_detached()
    paths.pid_path().unlink()

    # _is_running reaps the child, so an exited daemon is reported as exited rather than
    # lingering as a zombie that os.kill(pid, 0) still reports alive.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not supervisor._is_running(pid):
            break
        time.sleep(0.2)
    else:
        supervisor.reap([pid])
        pytest.fail(f"daemon {pid} survived the loss of its pid file")

    supervisor._prune_spawned()


def test_reap_kills_a_live_process(isolated_home: Path) -> None:
    """reap() must actually kill, since it is the fallback when the pid file is gone."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL
    )
    try:
        assert supervisor.reap([process.pid]) == []
        assert process.wait(timeout=5) is not None
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(process.pid, 9)


def test_reap_ignores_an_already_dead_pid(isolated_home: Path) -> None:
    assert supervisor.reap([999_999]) == []

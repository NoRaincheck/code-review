"""End-to-end: install the hook, commit, let the daemon review, retrieve the result.

This is the integration the two headline requirements describe, exercised through the
real daemon process, the real socket, and a scripted ACP agent. No vendor CLI, no network.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from code_review import git, hook, paths
from code_review.daemon import supervisor
from code_review.db import Database, Status

FAKE_AGENT = str(Path(__file__).parent / "fake_agent.py")
FINDING = (
    "1 issue found.\n\n### Division by zero\n**app.py:6** - `div` divides by an unchecked argument."
)


@pytest.fixture
def env() -> Path:
    """A short-lived CODEREVIEW_HOME, since AF_UNIX paths are length-limited.

    Tears the daemon down before removing the directory, so no process is left holding a
    deleted socket.
    """
    home = Path(tempfile.mkdtemp(prefix="cre2e"))
    yield home
    # Best-effort teardown. The pid file may already be gone, in which case stop_daemon
    # cannot identify its target, so reap by pid directly. A test run must never leak a
    # daemon.
    if supervisor.is_running():
        supervisor.stop_daemon()
    pids = [p.pid for p in supervisor._spawned] + (
        [supervisor.daemon_pid()] if supervisor.daemon_pid() else []
    )
    survivors = supervisor.reap(pids)
    for pid in survivors:  # pragma: no cover - unkillable child
        print(f"warning: daemon pid {pid} survived SIGKILL")
    for process in supervisor._spawned:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(autouse=True)
def _use_env(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEREVIEW_HOME", str(env))


@pytest.fixture
def scripted_repo(repo: Path, second_commit: str) -> tuple[Path, str]:
    script = Path(tempfile.mkdtemp(prefix="crs")) / "agent.json"
    script.write_text(json.dumps({"chunks": [FINDING]}))
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n"
        "review_timeout_seconds = 60\n",
        encoding="utf-8",
    )
    return repo, second_commit


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "code_review", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def wait_for_status(repo: Path, sha: str, timeout: float = 60.0) -> object:
    database = Database.for_repo(repo)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            review = database.get(sha)
            if review is not None and review.status in {
                Status.COMPLETED,
                Status.FAILED,
                Status.CANCELLED,
            }:
                return review
            time.sleep(0.2)
        raise AssertionError(f"review of {sha[:7]} never finished")
    finally:
        database.close()


def test_daemon_starts_and_stops(scripted_repo: tuple[Path, str]) -> None:
    pid = supervisor.ensure_running()
    assert supervisor.is_running()
    assert paths.socket_path().exists()

    supervisor.stop_daemon()
    assert not supervisor.is_running()
    assert pid > 0


def test_ensure_running_is_idempotent(scripted_repo: tuple[Path, str]) -> None:
    first = supervisor.ensure_running()
    second = supervisor.ensure_running()
    assert first == second


def test_committing_triggers_a_review(scripted_repo: tuple[Path, str]) -> None:
    """The headline flow: post-commit hook -> daemon -> stored markdown."""
    repo, _ = scripted_repo
    hook.install(repo)

    (repo / "later.py").write_text("def later():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "later.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Add later()"], cwd=repo, check=True)

    sha = git.resolve_sha(repo, "HEAD")
    review = wait_for_status(repo, sha)
    assert review.status is Status.COMPLETED
    assert "Division by zero" in (review.markdown or "")


def test_show_prints_the_stored_review(scripted_repo: tuple[Path, str]) -> None:
    repo, sha = scripted_repo
    hook.install(repo)
    result = run_cli("review", sha, "--wait", "--repo", str(repo))
    assert result.returncode == 0, result.stderr

    shown = run_cli("show", sha, "--repo", str(repo))
    assert shown.returncode == 0
    assert "Division by zero" in shown.stdout
    assert sha[:7] in shown.stdout


def test_show_raw_prints_markdown_only(scripted_repo: tuple[Path, str]) -> None:
    repo, sha = scripted_repo
    run_cli("review", sha, "--wait", "--repo", str(repo))

    raw = run_cli("show", sha, "--raw", "--repo", str(repo))
    assert raw.returncode == 0
    assert raw.stdout.strip() == FINDING


def test_show_exits_nonzero_for_an_unreviewed_commit(scripted_repo: tuple[Path, str]) -> None:
    repo, _ = scripted_repo
    result = run_cli("show", "HEAD", "--repo", str(repo))
    assert result.returncode == 2
    assert "no review" in result.stderr


def test_review_is_queued_not_duplicated(scripted_repo: tuple[Path, str]) -> None:
    repo, sha = scripted_repo
    assert run_cli("review", sha, "--repo", str(repo)).returncode == 0
    wait_for_status(repo, sha)

    # Re-running without --force must not clear the stored result.
    assert run_cli("review", sha, "--repo", str(repo)).returncode == 0
    database = Database.for_repo(repo)
    try:
        assert "Division by zero" in (database.get(sha).markdown or "")
    finally:
        database.close()


def test_review_with_force_reruns(scripted_repo: tuple[Path, str]) -> None:
    repo, sha = scripted_repo
    run_cli("review", sha, "--wait", "--repo", str(repo))

    forced = run_cli("review", sha, "--force", "--wait", "--repo", str(repo))
    assert forced.returncode == 0, forced.stderr
    database = Database.for_repo(repo)
    try:
        review = database.get(sha)
        assert review.status is Status.COMPLETED
        assert "Division by zero" in (review.markdown or "")
    finally:
        database.close()


def test_list_shows_the_review(scripted_repo: tuple[Path, str]) -> None:
    repo, sha = scripted_repo
    run_cli("review", sha, "--wait", "--repo", str(repo))

    listed = run_cli("list", "--repo", str(repo))
    assert listed.returncode == 0
    assert sha[:7] in listed.stdout


def test_list_json_is_valid(scripted_repo: tuple[Path, str]) -> None:
    repo, sha = scripted_repo
    run_cli("review", sha, "--wait", "--repo", str(repo))

    listed = run_cli("list", "--json", "--repo", str(repo))
    assert listed.returncode == 0
    rows = json.loads(listed.stdout)
    assert rows[0]["sha"] == sha
    assert rows[0]["status"] == "completed"


def test_status_reports_the_daemon(scripted_repo: tuple[Path, str]) -> None:
    repo, _ = scripted_repo
    supervisor.ensure_running()
    result = run_cli("status", "--repo", str(repo))
    assert result.returncode == 0
    assert "running" in result.stdout
    assert "queue depth" in result.stdout


def test_init_reports_what_it_did(scripted_repo: tuple[Path, str]) -> None:
    repo, _ = scripted_repo
    result = run_cli("init", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "hook:        installed" in result.stdout
    assert hook.is_installed(repo)


def test_doctor_flags_a_configured_agent(scripted_repo: tuple[Path, str], repo: Path) -> None:
    result = run_cli("doctor")
    assert result.returncode == 0
    assert "agent:" in result.stdout


def test_doctor_warns_when_no_agent(repo: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "code_review", "doctor"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "NOT CONFIGURED" in result.stdout


def test_hook_status_and_uninstall(scripted_repo: tuple[Path, str]) -> None:
    repo, _ = scripted_repo
    hook.install(repo)
    assert run_cli("hook", "status", "--repo", str(repo)).returncode == 0
    assert run_cli("hook", "uninstall", "--repo", str(repo)).returncode == 0
    assert not hook.is_installed(repo)
    assert run_cli("hook", "status", "--repo", str(repo)).returncode == 1


def test_enqueue_exits_zero_with_no_daemon(scripted_repo: tuple[Path, str]) -> None:
    """The hook entry point must never fail, even with a cold daemon."""
    repo, _ = scripted_repo
    result = subprocess.run(
        [sys.executable, "-m", "code_review", "enqueue"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_inline_review_without_a_daemon(scripted_repo: tuple[Path, str]) -> None:
    repo, sha = scripted_repo
    result = run_cli("review", sha, "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "Division by zero" in result.stdout
    assert not supervisor.is_running()


def test_failure_is_reported_not_hidden(tmp_path: Path, repo: Path, second_commit: str) -> None:
    repo.mkdir(exist_ok=True)
    (repo / ".codereview.toml").write_text(
        'review_agent = ["/nonexistent/agent-binary"]\n', encoding="utf-8"
    )
    result = run_cli("review", second_commit, "--wait", "--inline", "--repo", str(repo))
    assert result.returncode == 1
    assert "not found" in result.stderr.lower()


def test_state_dir_is_gitignored(scripted_repo: tuple[Path, str]) -> None:
    repo, _sha = scripted_repo
    run_cli("init", "--repo", str(repo))
    assert ".codereview/" in (repo / ".gitignore").read_text(encoding="utf-8")

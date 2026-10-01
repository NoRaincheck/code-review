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


def test_show_refuses_an_ambiguous_abbreviation(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    """Two reviewed commits sharing a prefix must not resolve to an arbitrary one.

    Showing one commit's review for another is worse than showing nothing.
    """
    database = Database.for_repo(repo)
    try:
        shared = "abcd"
        database.enqueue(shared + "111" + "0" * 33, "main")
        database.enqueue(shared + "222" + "0" * 33, "main")
    finally:
        database.close()

    result = run_cli("show", shared, "--repo", str(repo))
    assert result.returncode == 3, result.stdout
    assert "ambiguous" in result.stderr
    assert shared + "111" in result.stderr
    assert shared + "222" in result.stderr


def test_show_resolves_a_unique_abbreviation(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    database = Database.for_repo(repo)
    try:
        sha = "beef" + "0" * 36
        review_id, _ = database.enqueue(sha, "main")
        database.complete(review_id, markdown="unique review", stop_reason="end_turn", agent="fake")
    finally:
        database.close()

    result = run_cli("show", "beef", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "unique review" in result.stdout


def test_show_finds_a_review_after_the_commit_is_gone(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    """A review must survive a rebase that removes the commit from the repository."""
    script = tmp_path / "survive.json"
    script.write_text(json.dumps({"chunks": ["review that outlived its commit"]}))
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n",
        encoding="utf-8",
    )
    result = run_cli("review", second_commit, "--wait", "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr

    # Rewrite history so the reviewed commit no longer exists at all. Expiring the
    # reflog and pruning is required: reset alone leaves the object reachable, which would
    # make the test pass without exercising the path that matters.
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=repo, check=True)
    subprocess.run(["git", "reflog", "expire", "--expire=now", "--all"], cwd=repo, check=True)
    subprocess.run(["git", "gc", "--prune=now", "-q"], cwd=repo, check=True)
    gone = subprocess.run(
        ["git", "cat-file", "-e", second_commit],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    assert gone.returncode != 0, "precondition: the commit object should be gone"

    # git cannot resolve it, but the stored review is still reachable by sha.
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", f"{second_commit}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    assert resolved.returncode != 0, "precondition: git should not resolve the commit"

    shown = run_cli("show", second_commit, "--repo", str(repo))
    assert shown.returncode == 0, shown.stderr
    assert "review that outlived its commit" in shown.stdout


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


def test_doctor_probe_confirms_a_real_acp_agent(
    scripted_repo: tuple[Path, str], repo: Path
) -> None:
    """--probe must actually complete a handshake, not just check the binary exists."""
    result = run_cli("doctor", "--probe", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "acp probe:   OK" in result.stdout
    assert "ready for reviews (fake-agent 9.9.9)" in result.stdout


def test_doctor_probe_fails_loudly_for_a_non_acp_command(repo: Path, second_commit: str) -> None:
    """A command on PATH that is not an ACP agent must be reported, not accepted."""
    (repo / ".codereview.toml").write_text(
        'review_agent = ["/bin/echo", "not-an-agent"]\n', encoding="utf-8"
    )
    result = run_cli("doctor", "--probe", "--repo", str(repo))
    assert "acp probe:   FAILED" in result.stdout


def test_doctor_without_probe_does_not_spawn_the_agent(
    scripted_repo: tuple[Path, str], repo: Path
) -> None:
    result = run_cli("doctor", "--repo", str(repo))
    assert result.returncode == 0
    assert "acp probe" not in result.stdout
    assert "--probe" in result.stdout


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


def test_init_leaves_the_working_tree_alone_on_a_fresh_install(
    scripted_repo: tuple[Path, str],
) -> None:
    """Nothing is written into the checkout, so `.gitignore` is not edited either.

    Worth pinning because it is a promise the README and `init` both make, and because the
    natural implementation adds the entry unconditionally.
    """
    repo, _sha = scripted_repo
    result = run_cli("init", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "not needed" in result.stdout, result.stdout
    assert not (repo / ".gitignore").exists()


def test_init_ignores_a_leftover_state_directory(scripted_repo: tuple[Path, str]) -> None:
    """The upgrader case: a stale `.codereview/` from an earlier version must stay ignored."""
    repo, _sha = scripted_repo
    state = paths.state_dir(repo)
    state.mkdir(parents=True, exist_ok=True)
    (state / "reviews.db").write_text("stale", encoding="utf-8")

    result = run_cli("init", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert ".codereview/" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_review_agent_env_reaches_the_agent(repo: Path, second_commit: str, tmp_path: Path) -> None:
    """review_agent_env must reach the spawned process, not merely parse."""
    script = tmp_path / "echo.json"
    script.write_text(json.dumps({"chunks": ["review done"]}))
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n"
        'review_agent_env = { CODE_REVIEW_TEST_ECHO = "from-config" }\n',
        encoding="utf-8",
    )

    result = run_cli("review", second_commit, "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "echo=from-config" in result.stdout


def test_review_agent_args_reach_the_agent(repo: Path, second_commit: str, tmp_path: Path) -> None:
    """review_agent_args must be appended to the agent's argv."""
    script = tmp_path / "argv.json"
    script.write_text(json.dumps({"chunks": ["review done"], "echo_argv": True}))
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n"
        'review_agent_args = ["--extra-one", "two"]\n',
        encoding="utf-8",
    )

    result = run_cli("review", second_commit, "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "--extra-one" in result.stdout, result.stdout


def test_max_diff_bytes_reaches_the_prompt(repo: Path, tmp_path: Path) -> None:
    """A small cap must truncate the diff the agent is actually asked to review."""
    (repo / "big.py").write_text("\n".join(f"def f{i}(a, b):\n    return a - b" for i in range(60)))
    subprocess.run(["git", "add", "big.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add many helpers"], cwd=repo, check=True, capture_output=True
    )

    script = tmp_path / "prompt.json"
    script.write_text(
        json.dumps({"chunks": ["ok"], "echo_prompt_has": "[code-review: diff truncated"})
    )
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n"
        "max_diff_bytes = 300\n",
        encoding="utf-8",
    )

    result = run_cli("review", "HEAD", "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "prompt_has[[code-review: diff truncated]=True" in result.stdout, result.stdout


def test_a_generous_diff_cap_leaves_the_diff_intact(repo: Path, tmp_path: Path) -> None:
    """The cap must not truncate when the diff already fits."""
    script = tmp_path / "prompt2.json"
    script.write_text(json.dumps({"chunks": ["ok"], "echo_prompt_has": "def add(a, b):"}))
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n"
        "max_diff_bytes = 200000\n",
        encoding="utf-8",
    )
    result = run_cli("review", "HEAD", "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "prompt_has[def add(a, b):]=True" in result.stdout, result.stdout


def test_review_guidelines_reach_the_prompt(repo: Path, second_commit: str, tmp_path: Path) -> None:
    """Project guidelines must appear in the prompt the agent receives."""
    script = tmp_path / "guidelines.json"
    script.write_text(json.dumps({"chunks": ["ok"], "echo_prompt_has": "WATCH FOR TYPOS"}))
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n"
        'review_guidelines = """\nWATCH FOR TYPOS in identifiers.\n"""\n',
        encoding="utf-8",
    )
    result = run_cli("review", second_commit, "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "prompt_has[WATCH FOR TYPOS]=True" in result.stdout, result.stdout


def _scripted_repo_with_non_readonly_git(repo: Path, tmp_path: Path) -> None:
    """Config an agent that asks to run `git fetch`, which is not read-only."""
    script = tmp_path / "fetch.json"
    script.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "id": "c1",
                        "kind": "execute",
                        "title": "git fetch",
                        "raw_input": {"command": "git", "args": ["fetch"]},
                        "request_permission": True,
                    }
                ],
                "chunks": ["ok"],
            }
        )
    )
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n",
        encoding="utf-8",
    )


def _decision_for(repo: Path, sha: str) -> str:
    import sqlite3

    # Deliberately resolved through the same helper the product uses, so this test follows the
    # database if it ever moves again rather than pinning a path that can only go stale.
    conn = sqlite3.connect(Database.for_repo(repo).path)
    try:
        row = conn.execute("SELECT tool_log_json FROM reviews WHERE sha = ?", (sha,)).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0], "no tool log recorded"
    return json.loads(row[0])[0]["decision"]


def test_deny_unknown_commands_true_refuses_non_readonly_git(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    """The documented default must actually reach the policy."""
    _scripted_repo_with_non_readonly_git(repo, tmp_path)
    result = run_cli("review", second_commit, "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert _decision_for(repo, second_commit) == "deny"


def test_deny_unknown_commands_false_permits_non_readonly_git(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    """Opting out must reach the policy too, and must still refuse shell escapes."""
    _scripted_repo_with_non_readonly_git(repo, tmp_path)
    text = (repo / ".codereview.toml").read_text(encoding="utf-8")
    (repo / ".codereview.toml").write_text(
        text + "deny_unknown_commands = false\n", encoding="utf-8"
    )
    result = run_cli("review", second_commit, "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert _decision_for(repo, second_commit) == "allow"


def test_a_review_that_never_reviewed_is_warned_about_not_reported_as_clean(
    scripted_repo: tuple[Path, str], tmp_path: Path
) -> None:
    """The defect this was found by: a real agent narrated its approach, ended the turn, and
    the notes were stored as a successful review with no indication anything was wrong."""
    repo, _ = scripted_repo
    narration = Path(tempfile.mkdtemp(prefix="crn")) / "agent.json"
    narration.write_text(
        json.dumps(
            {
                "chunks": [
                    "I'll start by examining the repository to understand the context.",
                    "",
                    "Let me read the full current state of the key files.",
                ]
            }
        ),
        encoding="utf-8",
    )
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(narration)!r}]\n"
        "review_timeout_seconds = 60\n",
        encoding="utf-8",
    )

    sha = git.resolve_sha(repo, "HEAD")
    result = run_cli("review", sha, "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    assert "warning" in result.stderr, result.stderr

    review = wait_for_status(repo, sha)
    assert review.status is Status.COMPLETED
    assert review.error is None, "the review did complete; this is not a failure"
    assert review.warning is not None, "the stored review carries no warning"
    assert "verdict" in review.warning
    assert "I'll start by examining" in (review.markdown or ""), "the work is not thrown away"

    shown = run_cli("show", sha, "--repo", str(repo))
    assert "warning" in shown.stderr, shown.stderr


def test_a_real_review_carries_no_warning(scripted_repo: tuple[Path, str]) -> None:
    """The normal path must stay silent, or the warning is noise and gets ignored."""
    repo, _ = scripted_repo
    sha = git.resolve_sha(repo, "HEAD")
    assert run_cli("review", sha, "--inline", "--repo", str(repo)).returncode == 0
    review = wait_for_status(repo, sha)
    assert review.status is Status.COMPLETED
    assert review.warning is None


def test_the_stored_tool_log_has_no_temporary_path(scripted_repo: tuple[Path, str]) -> None:
    """Every command the agent ran named a directory that no longer exists."""
    repo, _ = scripted_repo
    hook.install(repo)
    (repo / "later.py").write_text("def later():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "later.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Add later()"], cwd=repo, check=True)

    sha = git.resolve_sha(repo, "HEAD")
    review = wait_for_status(repo, sha)
    assert review.status is Status.COMPLETED, review.error
    blob = json.dumps(review.tool_log)
    assert "code-review-" not in blob, f"the worktree path was persisted: {blob[:400]}"


def test_a_queued_review_shows_no_stale_warning(scripted_repo: tuple[Path, str]) -> None:
    """The end-to-end shape of the stale-warning bug: report nothing about a review that has
    not run, rather than repeating the previous attempt's verdict about it."""
    repo, _ = scripted_repo
    sha = git.resolve_sha(repo, "HEAD")
    narration = Path(tempfile.mkdtemp(prefix="crn")) / "agent.json"
    narration.write_text(json.dumps({"chunks": ["Let me start by reading the files."]}), "utf-8")
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(narration)!r}]\n",
        encoding="utf-8",
    )
    assert run_cli("review", sha, "--inline", "--repo", str(repo)).returncode == 0
    assert wait_for_status(repo, sha).warning is not None, "precondition"

    # Put it back to queued, which is what `review --force` does internally, and confirm
    # nothing is claimed about a review that has not run.
    database = Database.for_repo(repo)
    try:
        database.enqueue(sha, force=True)
        requeued = database.get(sha)
    finally:
        database.close()
    assert requeued is not None and requeued.status is Status.QUEUED

    shown = run_cli("show", sha, "--repo", str(repo))
    assert "warning" not in shown.stderr, shown.stderr
    assert "no review yet" in shown.stdout.lower()


def _narrating_repo(repo: Path) -> None:
    script = Path(tempfile.mkdtemp(prefix="crn")) / "agent.json"
    script.write_text(
        json.dumps({"chunks": ["I will start by exploring the repository structure."]}),
        encoding="utf-8",
    )
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(script)!r}]\n", encoding="utf-8"
    )


def test_list_marks_reviews_that_produced_no_verdict(scripted_repo: tuple[Path, str]) -> None:
    """`list` is what people scan, so a review that did not review must show up there.

    Without this the only place the warning appeared was `show`, which means a user reading
    `list` sees a clean `completed` row and a subject line that is the agent narrating itself.
    """
    repo, _ = scripted_repo
    _narrating_repo(repo)
    sha = git.resolve_sha(repo, "HEAD")
    assert run_cli("review", sha, "--inline", "--repo", str(repo)).returncode == 0
    assert wait_for_status(repo, sha).warning is not None

    listed = run_cli("list", "--repo", str(repo))
    assert listed.returncode == 0
    assert "!completed" in listed.stdout, listed.stdout
    assert "without producing a verdict" in listed.stdout
    assert listed.stderr == "", "the summary belongs on stdout with the rows"


def test_list_leaves_a_real_review_unmarked(scripted_repo: tuple[Path, str]) -> None:
    repo, _ = scripted_repo
    sha = git.resolve_sha(repo, "HEAD")
    assert run_cli("review", sha, "--inline", "--repo", str(repo)).returncode == 0
    assert wait_for_status(repo, sha).warning is None

    listed = run_cli("list", "--repo", str(repo))
    assert "!completed" not in listed.stdout
    assert " completed" in listed.stdout, "the status column must still line up"
    assert "without producing a verdict" not in listed.stdout


def test_list_json_carries_the_warning(scripted_repo: tuple[Path, str]) -> None:
    """A scripted consumer has no other way to see it."""
    repo, _ = scripted_repo
    _narrating_repo(repo)
    sha = git.resolve_sha(repo, "HEAD")
    run_cli("review", sha, "--inline", "--repo", str(repo))
    wait_for_status(repo, sha)

    payload = json.loads(run_cli("list", "--json", "--repo", str(repo)).stdout)
    assert payload[0]["warning"] is not None


def test_status_reports_how_many_reviews_never_reviewed(scripted_repo: tuple[Path, str]) -> None:
    repo, _ = scripted_repo
    _narrating_repo(repo)
    sha = git.resolve_sha(repo, "HEAD")
    run_cli("review", sha, "--inline", "--repo", str(repo))
    wait_for_status(repo, sha)

    shown = run_cli("status", "--repo", str(repo))
    assert "1 of those completed without producing a verdict" in shown.stdout, shown.stdout

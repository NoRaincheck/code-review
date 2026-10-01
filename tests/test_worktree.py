"""Reviews run in a throwaway worktree, so the agent sees the reviewed commit and cannot
disturb the reviewer's checkout.

These tests assert the two properties that motivated the design, because both are easy to
regress silently:

* the agent's working directory holds the code the review is *about*, not whatever the
  user's tree happens to contain now
* nothing the agent does reaches the user's index, their staged work, or their refs

The properties are checked against real git rather than a mock, since the whole point is
what git actually does with a worktree.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from code_review import git, paths, worktree
from code_review.db import Database, Status

FAKE_AGENT = str(Path(__file__).resolve().parent / "fake_agent.py")


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _script(repo: Path, tmp_path: Path, timeout: int = 60, **script: object) -> None:
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    (repo / ".codereview.toml").write_text(
        f"review_agent = [{sys.executable!r}, {FAKE_AGENT!r}, {str(path)!r}]\n"
        f"review_timeout_seconds = {timeout}\n",
        encoding="utf-8",
    )


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "code_review", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_for_status(repo: Path, sha: str, timeout: float = 60.0) -> object:
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


def _dead_pid() -> int:
    """A pid that is no longer running, so liveness gating treats its worktree as a leak.

    Reaping deliberately skips worktrees owned by a live process, so a test cannot make a leak
    with the pid it already has. Spawning a trivial process and waiting for it yields a pid
    that has exited; the residue of pid reuse is a risk this test accepts knowingly.
    """
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    return finished.pid


def _leaked(repo: Path, sha: str) -> Path:
    """A registered worktree left behind by a process that is gone, as a crash would leave."""
    path = Path(tempfile.mkdtemp()) / f"{worktree.PREFIX}{_dead_pid()}-stale"
    subprocess.run(
        ["git", "worktree", "add", "--detach", "--quiet", str(path), sha],
        cwd=repo,
        check=True,
    )
    (path / worktree.MARKER).write_text(f"{_dead_pid()}\n", encoding="utf-8")
    return path


def _review(repo: Path, sha: str) -> str:
    run_cli = _run_cli
    wait_for_status = _wait_for_status

    result = run_cli("review", sha, "--inline", "--repo", str(repo))
    assert result.returncode == 0, result.stderr
    review = wait_for_status(repo, sha)
    assert review.status is Status.COMPLETED, review.error
    return review.markdown or ""


# -- the agent's working directory -------------------------------------------------


def test_agent_reads_the_reviewed_commit_not_the_working_tree(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    """The headline correctness property.

    A later commit changes the file under review, so the user's checkout and the reviewed
    commit disagree. Whichever version the agent reads back proves where it was placed.
    """
    (repo / "app.py").write_text("VALUE = 'at second commit'\n", encoding="utf-8")
    _run_git(repo, "add", "app.py")
    _run_git(repo, "commit", "-q", "-m", "Set VALUE")
    target = _run_git(repo, "rev-parse", "HEAD").strip()
    # The tree now moves on, which is the situation the worktree exists to survive.
    (repo / "app.py").write_text("VALUE = 'much later'\n", encoding="utf-8")
    _run_git(repo, "add", "app.py")
    _run_git(repo, "commit", "-q", "-m", "Change VALUE again")
    assert (repo / "app.py").read_text() != "VALUE = 'at second commit'\n", "precondition"

    _script(repo, tmp_path, read_files=["app.py"])
    markdown = _review(repo, target)

    assert "read[app.py]=VALUE = 'at second commit'" in markdown, markdown
    assert "much later" not in markdown, "agent read the working tree, not the reviewed commit"


def test_agent_sees_the_commit_even_when_reviewed_from_another_worktree(
    repo: Path, tmp_path: Path
) -> None:
    """Reviewing a sha from the wrong worktree used to hand the agent contradictory files."""
    (repo / "shared.py").write_text("SIDE = 'main'\n", encoding="utf-8")
    _run_git(repo, "add", "shared.py")
    _run_git(repo, "commit", "-q", "-m", "Add shared")

    other = tmp_path / "other-worktree"
    _run_git(repo, "worktree", "add", "-q", str(other), "-b", "other")
    (other / "shared.py").write_text("SIDE = 'only in this worktree'\n", encoding="utf-8")
    _run_git(other, "add", "shared.py")
    _run_git(other, "commit", "-q", "-m", "Diverge")
    other_sha = _run_git(other, "rev-parse", "HEAD").strip()

    # Reviewed from the *main* worktree, whose files do not contain the other branch's work.
    _script(repo, tmp_path, read_files=["shared.py"])
    markdown = _review(repo, other_sha)

    # The reviewed commit genuinely contains the other branch's text; the point is that the
    # agent saw it while running from the main worktree, whose own file says something else.
    assert "read[shared.py]=SIDE = 'only in this worktree'" in markdown, markdown
    assert "SIDE = 'main'" not in markdown, "agent read the invoking worktree, not the commit"


# -- the user's checkout is left alone ----------------------------------------------


def test_agent_staging_cannot_reach_the_users_index(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    """An agent that runs `git add --all` must damage the worktree, not the user.

    The fake agent shells out, so this is the real failure mode rather than a simulation of
    it: the user's own staged file has to come back untouched afterwards.
    """
    mine = repo / "mine.py"
    mine.write_text("MINE = 1\n", encoding="utf-8")
    _run_git(repo, "add", "mine.py")
    staged_before = _run_git(repo, "ls-files", "--stage")
    refs_before = _run_git(repo, "show-ref")

    _script(repo, tmp_path, tool_calls=[{"shell_command": {"command": ["git", "add", "--all"]}}])
    _review(repo, second_commit)

    assert _run_git(repo, "ls-files", "--stage") == staged_before
    assert _run_git(repo, "show-ref") == refs_before, "review must not create refs"


def test_no_worktree_survives_the_review(repo: Path, second_commit: str, tmp_path: Path) -> None:
    _script(repo, tmp_path)
    _review(repo, second_commit)

    assert worktree.reap_stale(repo) == [], "a review left a registered worktree behind"
    listing = _run_git(repo, "worktree", "list", "--porcelain")
    assert listing.count("worktree ") == 1, listing


def test_a_failed_review_also_cleans_up(repo: Path, second_commit: str, tmp_path: Path) -> None:
    """Cleanup cannot depend on the review succeeding: a timeout is the common case."""
    _script(repo, tmp_path, timeout=5, hang_after_chunks=True)
    result = _run_cli("review", second_commit, "--inline", "--repo", str(repo))
    assert result.returncode != 0
    assert worktree.reap_stale(repo) == []


# -- the module's own guarantees ----------------------------------------------------


def test_isolated_yields_a_checkout_at_the_requested_commit(repo: Path, second_commit: str) -> None:
    with worktree.isolated(repo, second_commit) as root:
        assert (root / "app.py").is_file()
        inside = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert inside == second_commit, "worktree must be detached at the reviewed commit"
    assert not root.exists(), "worktree directory outlived the context"


def test_isolated_creates_no_ref(repo: Path, second_commit: str) -> None:
    before = _run_git(repo, "show-ref")
    with worktree.isolated(repo, second_commit):
        pass
    assert _run_git(repo, "show-ref") == before


def test_isolated_cleans_up_when_the_body_raises(repo: Path, second_commit: str) -> None:
    with pytest.raises(RuntimeError):
        with worktree.isolated(repo, second_commit) as root:
            raise RuntimeError("agent exploded")
    assert not root.exists()


def test_reap_leaves_a_users_own_worktree_alone(repo: Path, tmp_path: Path) -> None:
    """A reap after a crash must never be able to remove a worktree the user made."""
    mine = tmp_path / "mine"
    _run_git(repo, "worktree", "add", "-q", str(mine), "-b", "mine")

    assert worktree.reap_stale(repo) == []
    assert mine.is_dir(), "reap removed a worktree it did not create"
    assert worktree.is_ours(mine) is False


def test_reap_removes_a_leftover_whose_owner_is_gone(repo: Path, second_commit: str) -> None:
    """The crash case: a worktree survives the process that made it."""
    leaked = _leaked(repo, second_commit)
    assert leaked.is_dir()

    assert [p.resolve() for p in worktree.reap_stale(repo)] == [leaked.resolve()]
    assert not leaked.exists()


def test_reap_never_removes_a_worktree_a_live_review_is_using(
    repo: Path, second_commit: str
) -> None:
    """The hazard that made the pid part of the name necessary.

    An inline `review` and the daemon can run at the same time against one repository. A reap
    that ignored liveness would delete the worktree a review is actively using, turning a
    housekeeping step into a way to break a review that is in flight.
    """
    live = worktree.create(repo, second_commit)
    try:
        assert worktree.owner_is_gone(live) is False
        assert worktree.reap_stale(repo) == [], "reaped a worktree this process is using"
        assert live.is_dir(), "the reap destroyed a live worktree"
    finally:
        worktree.remove(repo, live)


def test_a_reused_pid_keeps_a_leftover_rather_than_breaking_a_live_review(
    repo: Path, second_commit: str
) -> None:
    """Conservative in the safe direction: a false "alive" costs a leftover, not a review."""
    path = Path(tempfile.mkdtemp()) / f"{worktree.PREFIX}{os.getpid()}-stale"
    subprocess.run(
        ["git", "worktree", "add", "--detach", "--quiet", str(path), second_commit],
        cwd=repo,
        check=True,
    )
    try:
        assert worktree.owner_is_gone(path) is False
        assert worktree.reap_stale(repo) == []
    finally:
        worktree.remove(repo, path)


def test_no_worktree_metadata_is_left_in_the_shared_git_dir(repo: Path, second_commit: str) -> None:
    common = git.common_dir(repo)
    with worktree.isolated(repo, second_commit):
        registered = common / "worktrees"
        assert registered.is_dir(), "precondition: git registered the worktree"
    assert not (common / "worktrees").exists()


# -- the shared database ------------------------------------------------------------


def test_worktrees_share_one_database(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "shared-db-worktree"
    _run_git(repo, "worktree", "add", "-q", str(other), "-b", "second")

    from_main = Database.for_repo(repo)
    from_other = Database.for_repo(other)
    try:
        assert from_main.path == from_other.path, "worktrees must not fork the database"
    finally:
        from_main.close()
        from_other.close()


def test_the_database_is_not_written_into_the_checkout(repo: Path, second_commit: str) -> None:
    """Living under the shared git dir is what makes sharing work, and it keeps the tree clean."""
    database = Database.for_repo(repo)
    try:
        assert database.path == paths.db_path(git.common_dir(repo))
        assert not (repo / ".codereview").exists(), "state leaked into the working tree"
    finally:
        database.close()


# -- environment hygiene ------------------------------------------------------------


def test_git_commands_ignore_an_inherited_hook_environment(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regression test for a real failure.

    git exports GIT_INDEX_FILE and friends into a hook's environment, and the hook starts the
    daemon, so the daemon inherited a relative GIT_INDEX_FILE=.git/index for its entire life.
    Every git command it ran in any other repository then resolved its index against the wrong
    place; `git worktree add` failed outright with "index file open failed: Not a directory".
    """
    monkeypatch.setenv("GIT_INDEX_FILE", ".git/index")
    monkeypatch.setenv("GIT_DIR", ".git")
    monkeypatch.setenv("GIT_WORK_TREE", "..")

    # A strict command, and the one that actually tripped over it.
    path = worktree.create(repo, git.resolve_sha(repo, "HEAD"))
    try:
        assert path.is_dir()
    finally:
        worktree.remove(repo, path)

    # And the write path, which is the one that would corrupt a user's index.
    assert git.check_index(repo).intact


def test_clean_env_keeps_settings_a_user_intends_to_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wildcard would strip these too, and they are legitimate."""
    monkeypatch.setenv("GIT_INDEX_FILE", ".git/index")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i ~/.ssh/id")
    env = git.clean_env()
    assert "GIT_INDEX_FILE" not in env
    assert env["GIT_SSH_COMMAND"] == "ssh -i ~/.ssh/id"


def test_the_daemon_is_not_started_with_a_poisoned_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_review.daemon import supervisor

    monkeypatch.setenv("GIT_INDEX_FILE", ".git/index")
    monkeypatch.delenv("CODEREVIEW_HOME", raising=False)
    env = supervisor._daemon_env()
    assert "GIT_INDEX_FILE" not in env, "a daemon must not inherit a hook's index pointer"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert "CODEREVIEW_HOME" in env


def test_a_review_leaves_no_trace_of_its_worktree_in_the_environment(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    """Guards the whole chain: hook to daemon to review, with the real CLI."""
    _script(repo, tmp_path)
    before = set(os.environ)
    _review(repo, second_commit)
    assert set(os.environ) == before
    assert not (git.common_dir(repo) / "worktrees").exists()


def test_init_reports_the_database_path_that_actually_exists(repo: Path) -> None:
    """A reported path that does not exist is worse than no path at all.

    `init` and `doctor` both print it, and printing the old per-worktree location survived the
    move to the shared git directory, so both commands were confidently wrong.
    """
    from code_review import cli

    database = Database.for_repo(repo)
    try:
        expected = database.path
    finally:
        database.close()

    assert cli.database_path(repo) == expected
    assert expected.is_file(), "reported path does not exist"


def test_doctor_reports_the_real_database_path(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from code_review import cli

    _script(repo, tmp_path)
    cli.cmd_init(argparse.Namespace(repo=str(repo), force=False))
    printed = capsys.readouterr().out
    reported = next(line for line in printed.splitlines() if line.startswith("database:"))
    assert Path(reported.split(maxsplit=1)[1]).is_file(), reported


# -- the crash case, end to end -----------------------------------------------------


def test_a_review_clears_worktrees_left_by_an_interrupted_one(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    """The reap is only worth anything if a review actually performs it.

    Simulates the crash by creating the worktree a review would have created and abandoning
    it, the way a SIGKILL mid-review would. The next review must clear it.
    """
    leaked = _leaked(repo, second_commit)
    assert leaked.is_dir()
    assert (git.common_dir(repo) / "worktrees").is_dir(), "precondition: it is registered"

    _script(repo, tmp_path)
    _review(repo, second_commit)

    assert not leaked.exists(), "the next review did not clean up the previous one's worktree"
    assert not (git.common_dir(repo) / "worktrees").exists()


def test_the_reap_does_not_stop_a_review_when_git_cannot_list_worktrees(
    repo: Path, second_commit: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mangled repository should not block the next review; the reap is housekeeping."""

    def refuse(*args: str, **kwargs: str) -> str:
        raise git.GitError(["worktree", "list"], 128, "not a git repository")

    monkeypatch.setattr(worktree, "reap_stale", refuse)
    _script(repo, tmp_path)
    markdown = _review(repo, second_commit)

    assert "No issues found" in markdown or markdown.strip(), "review should still have run"


def test_the_reap_only_ever_touches_our_own_prefix(repo: Path, second_commit: str) -> None:
    """A user's worktree survives a reap, even while ours is cleared."""
    mine = repo.parent / "user-worktree"
    _run_git(repo, "worktree", "add", "-q", str(mine), "-b", "user")
    leaked = _leaked(repo, second_commit)

    removed = worktree.reap_stale(repo)

    assert [p.resolve() for p in removed] == [leaked.resolve()]
    assert mine.is_dir(), "the reap removed a worktree the user created"
    assert (mine / ".git").is_file(), "precondition: the user's worktree is still intact"


def test_a_reap_during_a_live_review_leaves_that_review_alone(
    repo: Path, second_commit: str, tmp_path: Path
) -> None:
    """The hazard, exercised the way it would actually happen.

    A review is genuinely in flight, holding a registered worktree, and something reaps the
    repository mid-review. If that review's worktree disappears, the agent is left reading a
    directory that no longer exists and fails for a reason that has nothing to do with the
    commit. The reap is meant to be safe to run at any time, so this is the case that matters.
    """
    import threading

    # A long hang with a longer review budget, so the review is unambiguously still in flight
    # for the whole window below rather than finishing underneath the assertions.
    _script(repo, tmp_path, timeout=300, hang_seconds=180, hang_after_chunks=True)
    outcome: dict[str, object] = {}

    def review() -> None:
        try:
            outcome["markdown"] = _review(repo, second_commit)
        except Exception as exc:  # recorded so the assertion below explains the real failure
            outcome["error"] = exc

    def in_flight() -> list[Path]:
        return [
            Path(line.split(" ", 1)[1])
            for line in _run_git(repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")
        ]

    def ours() -> list[Path]:
        return [p for p in in_flight() if worktree.is_ours(p)]

    thread = threading.Thread(target=review)
    thread.start()
    try:
        # The review has to get as far as registering its worktree before there is anything to
        # protect, so wait for that rather than assuming it has happened.
        appeared = time.monotonic() + 30
        while not ours() and time.monotonic() < appeared and thread.is_alive():
            time.sleep(0.1)
        assert ours(), f"review never registered a worktree; outcome={outcome!r}"
        assert thread.is_alive(), f"review finished before it could be tested: {outcome!r}"

        # Reap repeatedly over a fixed window and require the worktree to survive every pass.
        # The window is bounded rather than "until the review ends" so the assertions cannot
        # race the review's own legitimate teardown.
        window = time.monotonic() + 5
        while time.monotonic() < window:
            live = ours()
            assert live, "the review's worktree vanished while the review was running"
            for path in live:
                assert path.is_dir(), f"reap removed a worktree in use: {path}"
            worktree.reap_stale(repo)
            time.sleep(0.1)
        assert ours(), "worktree did not survive repeated reaps"
        assert thread.is_alive(), f"review ended early, so the window proved nothing: {outcome!r}"
    finally:
        thread.join(timeout=60)


# -- failure paths, which are the ones that matter in a module about surviving failure ----


def test_a_failed_worktree_add_leaves_nothing_registered(repo: Path, tmp_path: Path) -> None:
    """git can fail after creating some of what it needs, so the failure path has to clean up.

    Otherwise a review that cannot start leaves a registration behind, which is precisely the
    leak the reap exists to clear, created by the code that was supposed to avoid it.
    """
    before = _run_git(repo, "worktree", "list", "--porcelain")

    with pytest.raises(worktree.WorktreeError):
        worktree.create(repo, "0" * 40)  # not a commit

    assert _run_git(repo, "worktree", "list", "--porcelain") == before
    assert not (git.common_dir(repo) / "worktrees").exists()


def test_the_error_names_the_git_failure_not_just_the_exit_code(repo: Path) -> None:
    """A bare exit code is useless at three in the morning; the reason has to survive."""
    with pytest.raises(worktree.WorktreeError) as raised:
        worktree.create(repo, "0" * 40)
    message = str(raised.value)
    assert "worktree add" in message
    assert "0" * 40 in message, f"the rejected ref should appear in the message: {message}"


def test_a_reap_failure_does_not_stop_the_review_from_starting(
    repo: Path, second_commit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Housekeeping is not worth blocking a review for."""
    from code_review import worktree as wt

    real_run = wt._run

    def list_only(_repo: Path, args: list[str], **kwargs: object) -> str:
        # Break only the reap's `worktree list`. Failing every call would also break the add,
        # which would test nothing except that subprocess errors propagate.
        if args[:2] == ["worktree", "list"]:
            raise wt.WorktreeError("git worktree list failed")
        return real_run(_repo, args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(wt, "_run", list_only)
    # Must not raise, and must still produce a usable worktree.
    path = wt.create(repo, second_commit)
    try:
        assert (path / "app.py").is_file()
    finally:
        wt.remove(repo, path)


def test_a_permission_error_reading_the_owner_keeps_the_worktree(
    repo: Path, second_commit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not being allowed to signal a pid is not evidence that it is dead.

    On a machine where another user's process owns the pid, raising EPERM means the process
    exists and belongs to someone else. Treating that as dead would remove a worktree that is
    still in use, so it has to read as alive.
    """
    path = Path(tempfile.mkdtemp()) / f"{worktree.PREFIX}31337-notmine"
    subprocess.run(
        ["git", "worktree", "add", "--detach", "--quiet", str(path), second_commit],
        cwd=repo,
        check=True,
    )

    def denied(pid: int, signal: int) -> None:
        raise PermissionError(f"pid {pid}")

    monkeypatch.setattr(worktree.os, "kill", denied)
    try:
        assert worktree.owner_is_gone(path) is False
        assert worktree.reap_stale(repo) == [], "reaped a worktree it could not prove was dead"
    finally:
        monkeypatch.undo()
        worktree.remove(repo, path)


def test_our_own_worktree_is_never_considered_abandoned(repo: Path, second_commit: str) -> None:
    """The current process is trivially alive; this is the guard that says so explicitly."""
    path = worktree.create(repo, second_commit)
    try:
        assert worktree.is_ours(path)
        assert worktree.owner_is_gone(path) is False
    finally:
        worktree.remove(repo, path)


def test_creating_a_worktree_reaps_a_leftover(repo: Path, second_commit: str) -> None:
    """The reap-through-create path, in process.

    The end-to-end version of this runs the review as a subprocess, so its coverage is not
    collected and the branch that logs the removal goes unmeasured. This asserts the same
    invariant directly, which is also far faster.
    """
    leaked = _leaked(repo, second_commit)
    assert leaked.is_dir()

    path = worktree.create(repo, second_commit)
    try:
        assert not leaked.exists(), "creating a worktree did not clear the previous leftover"
        assert (path / "app.py").is_file(), "and it did not create the new one"
    finally:
        worktree.remove(repo, path)


def test_a_path_with_no_pid_in_its_name_is_not_considered_abandoned(
    repo: Path, second_commit: str
) -> None:
    """Not being one of ours means the question does not arise, so it answers False.

    Guarding on the pid rather than the prefix alone is what makes the liveness check safe, and
    this is the branch that keeps a name we cannot attribute from being treated as a leak.
    """
    mine = repo.parent / "plain-worktree"
    _run_git(repo, "worktree", "add", "-q", str(mine), "-b", "plain")
    try:
        assert worktree.is_ours(mine) is False
        assert worktree.owner_is_gone(mine) is False
    finally:
        _run_git(repo, "worktree", "remove", "--force", str(mine))


def test_a_user_worktree_named_exactly_like_ours_is_never_reaped(
    repo: Path, second_commit: str
) -> None:
    """The collision the marker exists to prevent.

    The prefix is guessable, so a user can produce a path that looks exactly like one of ours.
    Ownership inferred from the name alone would delete their worktree whenever the digits
    happened to be a dead pid, which is a data-loss bug in a housekeeping step nobody asked for.
    The digits here are deliberately a pid that has exited.
    """
    decoy = repo.parent / f"{worktree.PREFIX}{_dead_pid()}-decoy"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(decoy), "-b", "decoy", second_commit],
        cwd=repo,
        check=True,
    )
    try:
        assert worktree.is_ours(decoy), "precondition: the name is indistinguishable from ours"
        assert worktree.owner_is_gone(decoy) is True, "precondition: the pid is dead"

        assert worktree.reap_stale(repo) == [], "reaped a worktree the user created"
        assert decoy.is_dir(), "the user's worktree was deleted"
        assert (decoy / ".git").is_file()
    finally:
        _run_git(repo, "worktree", "remove", "--force", str(decoy))


def test_our_own_worktree_carries_the_marker(repo: Path, second_commit: str) -> None:
    """Ownership is proven by the marker, and the marker records the owning pid."""
    path = worktree.create(repo, second_commit)
    try:
        marker = path / worktree.MARKER
        assert marker.is_file()
        assert marker.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        worktree.remove(repo, path)


def test_a_worktree_that_lost_its_marker_is_left_alone(repo: Path, second_commit: str) -> None:
    """An agent that deletes the marker costs a reap, not somebody's work.

    Erring this way is deliberate: the reap's failure mode is deletion, so a missing marker
    must read as "not proven to be ours" rather than "ours, but inconvenient".
    """
    leaked = _leaked(repo, second_commit)
    (leaked / worktree.MARKER).unlink()
    try:
        assert worktree.reap_stale(repo) == []
        assert leaked.is_dir(), "removed a worktree we could not prove was ours"
    finally:
        worktree.remove(repo, leaked)


def test_the_reap_still_removes_a_marked_leftover(repo: Path, second_commit: str) -> None:
    """The marker must not disable reaping entirely, which is the easy way to break this."""
    leaked = _leaked(repo, second_commit)
    assert (leaked / worktree.MARKER).is_file(), "precondition: the leftover is marked"
    assert [p.resolve() for p in worktree.reap_stale(repo)] == [leaked.resolve()]
    assert not leaked.exists()

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


def test_reap_removes_our_own_leftovers(repo: Path, second_commit: str) -> None:
    """The crash case: a worktree survives the process that made it."""
    leaked = worktree.create(repo, second_commit)
    assert leaked.is_dir()

    assert [p.resolve() for p in worktree.reap_stale(repo)] == [leaked.resolve()]
    assert not leaked.exists()


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

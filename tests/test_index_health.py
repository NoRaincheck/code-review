"""Detection of a git index left unusable by a reviewing agent.

Some agents stage files as a side effect of taking a repository snapshot. If the objects
those entries point at were never written to this repository's object store, the user's next
``git commit`` fails with ``invalid object`` and nothing ties that failure to the review that
caused it. Reproduced against ``opencode acp``, whose snapshot runs ``git add --all``.

These tests build the damage the way it was observed: stage an entry whose object is absent
from the object store, which is exactly the state the user ends up in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from code_review import git


def test_index_is_reported_intact_on_a_healthy_repository(repo: Path) -> None:
    health = git.check_index(repo)
    assert health.intact
    assert not health.broken
    assert health.detail == ""


def test_index_is_reported_broken_when_a_staged_object_is_missing(
    repo: Path, tmp_path: Path
) -> None:
    """Remove the blob behind a newly staged file.

    That is the state a review leaves behind: an agent's ``git add --all`` stages untracked
    files whose objects are not in the object store. Produced by deleting the loose object,
    because git rightly refuses to stage an entry for an object that does not exist.
    """
    fresh = repo / "untracked.txt"
    fresh.write_text("staged by the agent\n", encoding="utf-8")
    subprocess.run(["git", "add", "untracked.txt"], cwd=repo, check=True, capture_output=True)

    entry = next(
        line
        for line in subprocess.run(
            ["git", "ls-files", "-s"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if line.endswith("untracked.txt")
    )
    sha = entry.split()[1]
    blob = repo / ".git" / "objects" / sha[:2] / sha[2:]
    assert blob.exists(), "expected a loose object in a fresh repository"

    blob.unlink()
    try:
        health = git.check_index(repo)
        assert health.broken
        assert "untracked.txt" in health.detail, health.detail

        # And this is exactly the failure the user would hit.
        commit = subprocess.run(
            ["git", "commit", "-q", "-m", "should fail"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert commit.returncode != 0
        assert "invalid object" in commit.stderr
    finally:
        blob.unlink(missing_ok=True)
        subprocess.run(["git", "reset", "-q"], cwd=repo, check=True, capture_output=True)


def test_check_index_cannot_see_a_missing_object_behind_an_existing_tree(
    repo: Path,
) -> None:
    """An honest limitation, pinned so it is not mistaken for a full integrity check.

    ``write-tree`` is the step ``commit`` performs, so it is the right probe for content the
    index is newly staging. When every staged path is already in a committed tree, git
    reuses that tree and never needs the blob, so a missing object goes unnoticed here even
    though some other git operation may still trip over it.
    """
    entry = subprocess.run(
        ["git", "ls-files", "-s"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()[0]
    sha = entry.split()[1]
    blob = repo / ".git" / "objects" / sha[:2] / sha[2:]
    backup = blob.read_bytes()
    blob.unlink()
    try:
        assert git.check_index(repo).intact, "documented limitation: not detected"
    finally:
        blob.write_bytes(backup)


def test_detection_matches_what_commit_would_do(repo: Path) -> None:
    """write-tree is the step commit performs, so the verdict must agree with commit."""
    probe = git.check_index(repo)
    commit = subprocess.run(["git", "write-tree"], cwd=repo, capture_output=True, check=False)
    assert probe.intact == (commit.returncode == 0)


def test_detection_does_not_touch_the_index_or_worktree(repo: Path) -> None:
    before_index = (repo / ".git" / "index").read_bytes()
    before_files = sorted(p.name for p in repo.iterdir())
    git.check_index(repo)
    assert (repo / ".git" / "index").read_bytes() == before_index
    assert sorted(p.name for p in repo.iterdir()) == before_files


def test_detection_works_outside_a_repository(tmp_path: Path) -> None:
    """A plain directory has no index; that is intact enough to mean "nothing to fix"."""
    outside = tmp_path / "plain"
    outside.mkdir()
    health = git.check_index(outside)
    assert health.intact or health.broken  # must not raise


def test_remedy_recovers_the_repository(repo: Path) -> None:
    """Unstaging the dangling entries must restore committing without losing any file."""
    _stage_and_break(repo, "untracked.txt", "mine\n")
    assert git.check_index(repo).broken

    ok, _ = git.unstage_paths(repo, git.dangling_index_paths(repo))
    assert ok
    assert git.check_index(repo).intact
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "mine\n"


def _stage_and_break(repo: Path, name: str, content: str) -> str:
    """Stage a new file, then delete its blob so the index points at nothing."""
    target = repo / name
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True, capture_output=True)
    sha = next(
        line.split()[1]
        for line in subprocess.run(
            ["git", "ls-files", "-s", name],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    )
    blob = repo / ".git" / "objects" / sha[:2] / sha[2:]
    blob.unlink()
    return sha


def test_dangling_index_paths_names_only_the_broken_entries(repo: Path, tmp_path: Path) -> None:
    subprocess.run(
        ["git", "add", "app.py"], cwd=repo, check=True, capture_output=True
    )  # healthy staged entry
    _stage_and_break(repo, "broken.txt", "gone\n")

    assert git.dangling_index_paths(repo) == ["broken.txt"]


def test_dangling_index_paths_is_empty_on_a_healthy_index(repo: Path) -> None:
    assert git.dangling_index_paths(repo) == []


def test_repair_preserves_the_users_own_staged_work(repo: Path) -> None:
    """The reason this is not `git reset`: that would discard intended staging."""
    keep = repo / "my_work.txt"
    keep.write_text("work in progress\n", encoding="utf-8")
    subprocess.run(["git", "add", "my_work.txt"], cwd=repo, check=True, capture_output=True)
    _stage_and_break(repo, "stray.txt", "agent left this\n")

    staged_before = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    ok, _ = git.unstage_paths(repo, git.dangling_index_paths(repo))
    assert ok

    staged_after = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert "my_work.txt" in staged_before and "my_work.txt" in staged_after
    assert "stray.txt" not in staged_after
    assert git.check_index(repo).intact


def test_unstage_of_nothing_is_a_no_op(repo: Path) -> None:
    ok, message = git.unstage_paths(repo, [])
    assert not ok
    assert "no paths" in message


def test_repair_reports_an_undamaged_index(repo: Path, tmp_path: Path, capsys) -> None:
    """End to end through the CLI: nothing to do is success, not an error."""
    from code_review.cli import main

    assert main(["repair", "--repo", str(repo)]) == 0
    assert "intact" in capsys.readouterr().out


def test_repair_dry_run_changes_nothing(repo: Path) -> None:
    from code_review.cli import main

    _stage_and_break(repo, "stray.txt", "gone\n")
    before = (repo / ".git" / "index").read_bytes()
    assert main(["repair", "--repo", str(repo), "--dry-run"]) == 0
    assert (repo / ".git" / "index").read_bytes() == before
    assert git.check_index(repo).broken


def test_repair_fixes_a_broken_index(repo: Path) -> None:
    from code_review.cli import main

    _stage_and_break(repo, "stray.txt", "gone\n")
    assert git.check_index(repo).broken
    assert main(["repair", "--repo", str(repo)]) == 0
    assert git.check_index(repo).intact

"""Git wrapper tests against real temporary repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review import git


def test_is_repo_detects_a_repository(repo: Path) -> None:
    assert git.is_repo(repo)


def test_is_repo_rejects_a_plain_directory(tmp_path: Path) -> None:
    assert not git.is_repo(tmp_path)


def test_toplevel_returns_repo_root(repo: Path) -> None:
    assert git.toplevel(repo) == repo.resolve()


def test_toplevel_from_a_subdirectory(repo: Path) -> None:
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert git.toplevel(nested) == repo.resolve()


def test_resolve_sha_of_head(repo: Path) -> None:
    sha = git.resolve_sha(repo, "HEAD")
    assert len(sha) == 40


def test_resolve_sha_raises_for_unknown_rev(repo: Path) -> None:
    with pytest.raises(git.GitError):
        git.resolve_sha(repo, "does-not-exist")


def test_current_branch(repo: Path) -> None:
    assert git.current_branch(repo) == "main"


def test_current_branch_is_none_when_detached(repo: Path, second_commit: str, git_run) -> None:
    git_run(repo, "checkout", "--detach", "-q", "HEAD")
    assert git.current_branch(repo) is None


def test_load_commit_reads_metadata(repo: Path) -> None:
    commit = git.load_commit(repo)
    assert commit.subject == "Add add()"
    assert "first implementation" in commit.body
    assert commit.author_name == "Dev Example"
    assert commit.author_email == "dev@example.com"
    assert commit.short_sha == commit.sha[:7]
    # The fixture's only commit is a root commit, so it has no parents. The parent
    # field must still parse as an empty value rather than swallowing the next field.


def test_load_commit_includes_the_diff(repo: Path, second_commit: str) -> None:
    commit = git.load_commit(repo, second_commit)
    assert "def div(a, b):" in commit.diff
    assert "-    return a + b" in commit.diff


def test_load_commit_resolves_a_short_sha(repo: Path) -> None:
    full = git.resolve_sha(repo, "HEAD")
    assert git.load_commit(repo, full[:8]).sha == full


def test_load_commit_of_root_commit_has_no_parents(repo: Path, git_run) -> None:
    root = git_run(repo, "rev-list", "--max-parents=0", "HEAD").strip()
    assert git.load_commit(repo, root).parents == ()


def test_load_commit_parses_parents_of_a_non_root_commit(repo: Path, second_commit: str) -> None:
    commit = git.load_commit(repo, second_commit)
    assert len(commit.parents) == 1
    assert commit.parents[0] == git.resolve_sha(repo, "HEAD~1")


def test_load_commit_parses_a_merge_commit(repo: Path, second_commit: str, git_run) -> None:
    git_run(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    git_run(repo, "add", "side.txt")
    git_run(repo, "commit", "-q", "-m", "Side work")
    git_run(repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    git_run(repo, "add", "main.txt")
    git_run(repo, "commit", "-q", "-m", "Main work")
    git_run(repo, "merge", "-q", "--no-ff", "-m", "Merge side", "side")

    merge = git.load_commit(repo, "HEAD")
    assert len(merge.parents) == 2
    assert merge.subject == "Merge side"


def test_truncate_diff_leaves_short_diffs_alone() -> None:
    diff = "a" * 100
    assert git.truncate_diff(diff, 1000) == diff
    assert git.truncate_diff(diff, None) == diff


def test_truncate_diff_caps_length() -> None:
    truncated = git.truncate_diff("x" * 5000, 1000)
    assert len(truncated.encode()) < 1500
    assert "diff truncated" in truncated


def test_truncate_diff_marker_is_informative() -> None:
    truncated = git.truncate_diff("x" * 5000, 500)
    assert "bytes omitted" in truncated
    assert "git show" in truncated


def test_truncate_diff_does_not_split_a_multibyte_character() -> None:
    truncated = git.truncate_diff("é" * 100, 51)
    truncated.encode("utf-8").decode("utf-8")  # must not raise
    assert "é" in truncated


def test_is_ancestor(repo: Path, second_commit: str) -> None:
    first = git.resolve_sha(repo, "HEAD~1")
    assert git.is_ancestor(repo, first, second_commit)
    assert not git.is_ancestor(repo, second_commit, first)


def test_git_error_preserves_stderr(tmp_path: Path, git_run) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        git_run(tmp_path, "not-a-command")


def test_negative_diff_cap_disables_truncation(repo: Path) -> None:
    """A non-positive cap means 'no cap', matching truncate_diff's contract."""
    commit = git.load_commit(repo, max_diff_bytes=-5)
    assert commit.diff


def test_multiline_body_is_parsed_correctly(repo: Path, git_run) -> None:
    """A body containing newlines and field-like text must not corrupt the record."""
    (repo / "multi.txt").write_text("hello\n", encoding="utf-8")
    git_run(repo, "add", "multi.txt")
    git_run(
        repo,
        "commit",
        "-q",
        "-m",
        "Subject with \x1f field char and\nnewline",
        "-m",
        "Body line one.\n\nBody line two with %H and %s lookalikes.",
    )
    commit = git.load_commit(repo)
    assert commit.subject.startswith("Subject with")
    assert "Body line one." in commit.body
    assert "Body line two with %H and %s lookalikes." in commit.body
    assert commit.author_email == "dev@example.com"

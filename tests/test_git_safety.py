"""Guarantees about the git commands this tool is capable of running.

The permission policy constrains what an *agent* may run. These tests cover the
complementary question: which git commands can this codebase itself run, and what does it
write? A reviewer given shell access to a real checkout makes that worth pinning down.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from code_review import git
from code_review.policy import READ_ONLY_GIT_SUBCOMMANDS

SRC = Path(__file__).resolve().parent.parent / "src" / "code_review"

# Subcommands that modify the repository, its refs, or its object store. None may appear in
# a command this codebase builds. `restore` is handled separately below: with `--staged` it
# touches only the index, which the repair command needs.
WRITE_SUBCOMMANDS = {
    "add",
    "am",
    "apply",
    "bisect",
    "branch",
    "checkout",
    "cherry-pick",
    "clean",
    "commit",
    "config",
    "fetch",
    "gc",
    "init",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "reflog",
    "remote",
    "reset",
    "revert",
    "rm",
    "stash",
    "submodule",
    "switch",
    "tag",
    "update-index",
    "update-ref",
    "worktree",
}

# The single write this tool performs, and only to repair damage an agent caused. It rewrites
# index entries only: no working-tree file, ref, or object is touched.
INDEX_ONLY_EXCEPTIONS = {"restore"}

# Subcommands this tool is allowed to run for reading.
EXPECTED_READS = {"rev-parse", "show", "log", "merge-base"}


def _argv_literals() -> set[str]:
    """Every string literal in git.py that looks like a git subcommand."""
    source = (SRC / "git.py").read_text(encoding="utf-8")
    return set(re.findall(r'"([a-z][a-z-]{1,20})"', source))


def test_git_module_only_uses_read_only_subcommands() -> None:
    reads = _argv_literals()
    offenders = reads & WRITE_SUBCOMMANDS - INDEX_ONLY_EXCEPTIONS
    assert not offenders, f"git.py can execute write subcommands: {sorted(offenders)}"


def test_the_only_write_is_index_only() -> None:
    """`restore` appears once, always with --staged, so no content or ref can change."""
    source = (SRC / "git.py").read_text(encoding="utf-8")
    lines = [line for line in source.splitlines() if '"restore"' in line]
    assert len(lines) == 1, f"unexpected number of restore call sites: {lines}"
    assert "--staged" in lines[0], "restore must be index-only"
    # --staged alone would still need a path separator to avoid ambiguity.
    assert '"--",' in lines[0] or '"--", *paths' in lines[0], "paths must follow --"


def test_git_module_reads_the_expected_subcommands() -> None:
    reads = _argv_literals()
    assert EXPECTED_READS <= reads, f"expected reads missing: {EXPECTED_READS - reads}"


def test_policy_allowlist_excludes_every_write_subcommand() -> None:
    """The allowlist handed to agents must never contain a write subcommand."""
    offenders = READ_ONLY_GIT_SUBCOMMANDS & WRITE_SUBCOMMANDS
    assert not offenders, f"policy allows write subcommands: {sorted(offenders)}"


def test_nothing_in_the_package_writes_into_the_git_directory() -> None:
    """No module may hard-code a .git path.

    The one legitimate write into .git is the post-commit hook, which git expects us to
    create. Its location must come from git rather than string arithmetic, because in a
    linked worktree ``.git`` is a file. Matched on the path *segment* ".git", so
    ".gitignore" does not count.
    """
    segment = re.compile(r"""(?<![\w.])["']\.git(?:/|["'])""")
    offenders = []
    for source in SRC.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("#"):
                continue
            if segment.search(line) or '".git"' in line:
                offenders.append(f"{source.name}: {line.strip()}")
    assert not offenders, f"modules hard-code a .git path: {offenders}"


def test_git_directories_come_from_git_not_string_arithmetic() -> None:
    """git.py must ask git where its directories are."""
    text = (SRC / "git.py").read_text(encoding="utf-8")
    assert "--git-common-dir" in text, "hooks_dir must be derived from the common dir"
    assert "hooks" in text


def test_hooks_dir_resolves_the_shared_location_in_a_worktree(repo: Path, tmp_path: Path) -> None:
    """A worktree's .git is a file, so .git/hooks cannot be constructed by hand."""
    worktree = tmp_path / "linked"

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout

    run("worktree", "add", "-q", str(worktree), "-b", "feature")

    assert (worktree / ".git").is_file(), "precondition: .git is a file in a worktree"
    assert not (worktree / ".git" / "hooks").exists()

    hooks = git.hooks_dir(worktree)
    assert hooks.is_dir(), hooks
    # Hooks are shared, so a worktree resolves to the main repository's hooks.
    assert hooks == git.hooks_dir(repo)


def test_toplevel_reports_each_worktree(repo: Path, tmp_path: Path, second_commit: str) -> None:
    worktree = tmp_path / "linked2"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", "feature2"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    assert git.toplevel(worktree).resolve() == worktree.resolve()
    assert git.toplevel(repo).resolve() == repo.resolve()


@pytest.mark.parametrize("subcommand", sorted(READ_ONLY_GIT_SUBCOMMANDS))
def test_each_allowlisted_subcommand_is_classified_as_read_only(subcommand: str) -> None:
    from code_review.policy import classify_git_argv

    assert classify_git_argv(["git", subcommand]).allowed

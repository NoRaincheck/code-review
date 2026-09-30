"""Guarantees about the git commands this tool is capable of running.

The permission policy constrains what an *agent* may run. These tests cover the
complementary question: which git commands can this codebase itself run, and what does it
write? A reviewer given shell access to a real checkout makes that worth pinning down.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from code_review.policy import READ_ONLY_GIT_SUBCOMMANDS

SRC = Path(__file__).resolve().parent.parent / "src" / "code_review"

# Subcommands that modify the repository, its index, or its object store. None may appear
# in a command this codebase builds.
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
    "restore",
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

# Subcommands this tool is allowed to run for reading.
EXPECTED_READS = {"rev-parse", "show", "log", "merge-base"}


def _argv_literals() -> set[str]:
    """Every string literal in git.py that looks like a git subcommand."""
    source = (SRC / "git.py").read_text(encoding="utf-8")
    return set(re.findall(r'"([a-z][a-z-]{1,20})"', source))


def test_git_module_only_uses_read_only_subcommands() -> None:
    reads = _argv_literals()
    offenders = reads & WRITE_SUBCOMMANDS
    assert not offenders, f"git.py can execute write subcommands: {sorted(offenders)}"


def test_git_module_reads_the_expected_subcommands() -> None:
    reads = _argv_literals()
    assert EXPECTED_READS <= reads, f"expected reads missing: {EXPECTED_READS - reads}"


def test_policy_allowlist_excludes_every_write_subcommand() -> None:
    """The allowlist handed to agents must never contain a write subcommand."""
    offenders = READ_ONLY_GIT_SUBCOMMANDS & WRITE_SUBCOMMANDS
    assert not offenders, f"policy allows write subcommands: {sorted(offenders)}"


def test_nothing_in_the_package_writes_into_the_git_directory() -> None:
    """Only paths.py may name a .git path segment, and it may only hand back a hook path.

    The one legitimate write into .git is the post-commit hook, which git expects us to
    create. Every other module must reach the repository only through git.py. Matches the
    path *segment* ".git", so ".gitignore" does not count.
    """
    segment = re.compile(r"""(?<![\w.])["']\.git(?:/|["'])""")
    for source in SRC.rglob("*.py"):
        if source.name == "paths.py":
            continue
        text = source.read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in text.splitlines()
            if not line.strip().startswith("#") and segment.search(line)
        ]
        assert not offenders, f"{source.name} names a .git path directly: {offenders}"


def test_git_directory_writes_go_through_paths() -> None:
    """paths.py is the single place that constructs a .git path."""
    text = (SRC / "paths.py").read_text(encoding="utf-8")
    assert '".git"' in text
    assert "hooks_dir" in text
    # It must not be constructing a loose-object or ref path.
    assert "objects" not in text
    assert "refs" not in text


@pytest.mark.parametrize("subcommand", sorted(READ_ONLY_GIT_SUBCOMMANDS))
def test_each_allowlisted_subcommand_is_classified_as_read_only(subcommand: str) -> None:
    from code_review.policy import classify_git_argv

    assert classify_git_argv(["git", subcommand]).allowed

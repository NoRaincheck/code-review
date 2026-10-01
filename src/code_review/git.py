"""Git subprocess wrappers.

Deliberately thin: each function shells out to ``git`` and returns plain data. No
caching, no daemon state, so it is testable against real temporary repositories.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 30


class GitError(Exception):
    """A git invocation failed. ``returncode`` and ``stderr`` are preserved."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class Commit:
    """The parts of a commit needed to build a review prompt."""

    sha: str
    short_sha: str
    parents: tuple[str, ...]
    subject: str
    body: str
    author_name: str
    author_email: str
    authored_at: str
    diff: str


# Variables git exports into a hook's environment, which then describe the repository and
# index of whatever commit was being made. They are relative paths, and they are only correct
# for the process git spawned.
#
# The post-commit hook starts the daemon, so without removing these the daemon inherits them
# for its whole life and every git command it later runs in a *different* repository resolves
# its index against the wrong place. This was not hypothetical: `git worktree add` failed with
# "index file open failed: Not a directory" because an inherited GIT_INDEX_FILE=.git/index was
# resolved against a directory that was not a git repository at all. The read paths had been
# quietly tolerant of it; the repair command, which writes the index, was not.
#
# An explicit list rather than a GIT_* wildcard, because a user is entitled to carry
# GIT_SSH_COMMAND or GIT_CONFIG_GLOBAL into this tool and have it respected.
_INHERITED_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
)


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """*base* (default: this process) without the repository-locating variables git exports
    to hooks, so a git command here is located by its ``cwd`` and nothing else."""
    env = dict(os.environ if base is None else base)
    for name in _INHERITED_GIT_ENV:
        env.pop(name, None)
    return env


# Every git call in this module goes through one of the two helpers below, deliberately.
# Hand-adding `env=clean_env()` to each call site already went wrong once: five of the seven
# call sites were missed, and two of those were the ones that write the index, which is
# exactly what an inherited hook environment corrupts. One funnel, no omissions.


def _spawn_text(
    repo: Path, args: list[str], *, timeout: int = DEFAULT_TIMEOUT, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=clean_env(),
    )


def _spawn_bytes(
    repo: Path, args: list[str], *, timeout: int = DEFAULT_TIMEOUT
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=clean_env(),
    )


def _run(repo: Path, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    result = _spawn_text(repo, args, timeout=timeout)
    if result.returncode != 0:
        raise GitError(args, result.returncode, result.stderr)
    return result.stdout


# NUL is the only byte git forbids in a commit message, which makes it the only
# separator that cannot appear in the data being parsed. A visible separator like
# \x1f is not safe: a commit subject containing it would corrupt the record.
FIELD = b"\x00"
FORMAT_FIELDS = ("%H", "%P", "%s", "%an", "%ae", "%aI", "%b")


def _run_bytes(repo: Path, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> bytes:
    result = _spawn_bytes(repo, args, timeout=timeout)
    if result.returncode != 0:
        raise GitError(args, result.returncode, result.stderr.decode("utf-8", "replace"))
    return result.stdout


def _parse_record(raw: bytes) -> dict[str, str]:
    """Parse NUL-separated commit metadata into a dict.

    Metadata and patch come from separate git invocations so the record needs no
    terminator. An empty field such as %P on a root commit is unambiguous here, which a
    trailing separator could not make.
    """
    values = raw.lstrip(b"\n").split(FIELD)
    if len(values) != len(FORMAT_FIELDS):
        raise GitError(["log"], 0, f"unexpected commit record: {raw[:200]!r}")
    return {
        name: value.decode("utf-8", "replace")
        for name, value in zip(FORMAT_FIELDS, values, strict=True)
    }


def is_repo(repo: Path) -> bool:
    try:
        return _run(repo, ["rev-parse", "--is-inside-work-tree"]).strip() == "true"
    except GitError, subprocess.SubprocessError:
        return False


def toplevel(repo: Path) -> Path:
    return Path(_run(repo, ["rev-parse", "--show-toplevel"]).strip())


def common_dir(repo: Path) -> Path:
    """The repository's shared git directory.

    Differs from ``.git`` in a linked worktree, where ``.git`` is a *file* pointing at
    ``<common>/worktrees/<name>`` rather than a directory. Returns an absolute path either
    way, since git reports the main worktree's answer relative to the repository root.
    """
    value = _run(repo, ["rev-parse", "--git-common-dir"]).strip()
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (repo / candidate).resolve()


def hooks_dir(repo: Path) -> Path:
    """Where git looks for hooks, resolved by git itself.

    Hooks live in the shared common directory, so a hook installed from a linked worktree
    applies to every worktree of the repository. Deriving this as ``.git/hooks`` would
    break in a worktree, where ``.git`` is a file.
    """
    return common_dir(repo) / "hooks"


def current_branch(repo: Path) -> str | None:
    """Branch name, or None on a detached HEAD."""
    try:
        name = _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    except GitError:
        return None
    return None if name == "HEAD" else name


def resolve_sha(repo: Path, rev: str = "HEAD") -> str:
    """Resolve *rev* to a full SHA, raising GitError if it does not exist."""
    return _run(repo, ["rev-parse", "--verify", f"{rev}^{{commit}}"]).strip()


def load_commit(repo: Path, rev: str = "HEAD", max_diff_bytes: int | None = None) -> Commit:
    """Load a commit's metadata and its diff against its first parent.

    A merge needs its diff computed explicitly, because ``git show`` does not produce one: for
    a merge it prints no patch at all unless ``--cc`` or ``-m`` is given, and neither is the
    first-parent view. So a merge used to be handed to the agent with an *empty* diff, and the
    agent duly reported "No issues found." about a commit it had never been shown. Since
    ``post-commit`` fires on every merge, that is a stream of clean-looking verdicts about
    nothing. Diffing the merge against its first parent is both the conventional reading of
    "what did this commit introduce" and the thing the docstring already claimed happened.
    """
    sha = resolve_sha(repo, rev)
    fmt = "%n" + "%x00".join(FORMAT_FIELDS)
    fields = _parse_record(_run_bytes(repo, ["log", "-1", "--no-color", f"--format={fmt}", sha]))
    parents = fields["%P"].split()
    if len(parents) > 1:
        patch = _run_bytes(repo, ["diff", "--no-color", f"{parents[0]}", fields["%H"]])
    else:
        patch = _run_bytes(repo, ["show", "--no-color", "--format=", sha])
    patch = patch.strip(b"\n")
    return Commit(
        sha=fields["%H"],
        short_sha=sha[:7],
        parents=tuple(parents),
        subject=fields["%s"],
        body=fields["%b"].strip(),
        author_name=fields["%an"],
        author_email=fields["%ae"],
        authored_at=fields["%aI"],
        diff=truncate_diff(patch.decode("utf-8", "replace"), max_diff_bytes),
    )


def truncate_diff(diff: str, max_bytes: int | None) -> str:
    """Cap the diff at *max_bytes*, leaving a visible marker when truncated.

    Encoded as UTF-8 so the cap is measured in real prompt bytes rather than
    characters; splitting a multi-byte character would produce invalid text.
    """
    if max_bytes is None or max_bytes <= 0:
        return diff
    encoded = diff.encode("utf-8")
    if len(encoded) <= max_bytes:
        return diff
    kept = encoded[:max_bytes].decode("utf-8", errors="ignore")
    omitted = len(encoded) - len(kept.encode("utf-8"))
    return (
        f"{kept}\n\n[code-review: diff truncated, {omitted} bytes omitted. "
        f"Use read-only git commands such as `git show <sha>` or `git show <sha>:<path>` "
        f"to inspect the rest.]\n"
    )


def is_ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool:
    try:
        result = _spawn_text(repo, ["merge-base", "--is-ancestor", maybe_ancestor, descendant])
    except subprocess.SubprocessError:
        return False
    return result.returncode == 0


@dataclass(frozen=True, slots=True)
class IndexHealth:
    """Whether the index can still produce a tree, which is what committing requires."""

    intact: bool
    detail: str = ""

    @property
    def broken(self) -> bool:
        return not self.intact


def check_index(repo: Path) -> IndexHealth:
    """Check that every object the index references still exists.

    ``git write-tree`` performs exactly the step ``git commit`` performs, so it fails the same
    way and for the same reason, in one cheap read-only call.

    This exists because a reviewing agent may leave the index referencing objects that were
    never written to this repository's object store. The user's next ``git commit`` then fails
    with ``invalid object``, which is cryptic and disconnected from the review that caused it.
    """
    result = _spawn_text(repo, ["write-tree"])
    if result.returncode == 0:
        return IndexHealth(intact=True)
    detail = (result.stderr or result.stdout).strip().splitlines()
    return IndexHealth(intact=False, detail=detail[0] if detail else "git write-tree failed")


def dangling_index_paths(repo: Path) -> list[str]:
    """Indexed paths whose blob is absent from the object store.

    One ``git cat-file --batch-check`` covers every entry, so the cost is a single process
    regardless of index size. These are exactly the paths that must be unstaged to make the
    repository committable again.
    """
    listing = _spawn_text(repo, ["ls-files", "-s", "-z"])
    if listing.returncode != 0:
        return []

    entries: list[tuple[str, str]] = []
    for record in listing.stdout.split("\0"):
        if not record.strip():
            continue
        # "<mode> <sha> <stage>\t<path>"
        meta, _, path = record.partition("\t")
        parts = meta.split()
        if len(parts) >= 2 and path:
            entries.append((parts[1], path))
    if not entries:
        return []

    batch = _spawn_text(
        repo,
        ["cat-file", "--batch-check"],
        stdin="\n".join(sha for sha, _ in entries) + "\n",
    )
    if batch.returncode != 0:
        return []

    verdicts = [line.strip() for line in batch.stdout.splitlines()]
    if len(verdicts) != len(entries):  # pragma: no cover - malformed output
        return []

    dangling: list[str] = []
    for (_, path), verdict in zip(entries, verdicts, strict=True):
        if "missing" in verdict:
            dangling.append(path)
    return dangling


def unstage_paths(repo: Path, paths: list[str]) -> tuple[bool, str]:
    """Unstage exactly *paths*, leaving the rest of the index untouched.

    Deliberately not ``git reset``: that unstages everything, including work the user
    intended to stage. Returns ``(ok, message)``.
    """
    if not paths:
        return False, "no paths given"
    result = _spawn_text(repo, ["restore", "--staged", "--", *paths])
    if result.returncode == 0:
        return True, f"unstaged {len(paths)} path(s)"
    detail = (result.stderr or result.stdout).strip()
    return False, detail or f"git restore --staged failed ({result.returncode})"

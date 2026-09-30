"""Git subprocess wrappers.

Deliberately thin: each function shells out to ``git`` and returns plain data. No
caching, no daemon state, so it is testable against real temporary repositories.
"""

from __future__ import annotations

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


def _run(repo: Path, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(args, result.returncode, result.stderr)
    return result.stdout


# NUL is the only byte git forbids in a commit message, which makes it the only
# separator that cannot appear in the data being parsed. A visible separator like
# \x1f is not safe: a commit subject containing it would corrupt the record.
FIELD = b"\x00"
FORMAT_FIELDS = ("%H", "%P", "%s", "%an", "%ae", "%aI", "%b")


def _run_bytes(repo: Path, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
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


def commit_exists(repo: Path, rev: str = "HEAD") -> bool:
    try:
        resolve_sha(repo, rev)
    except GitError, subprocess.SubprocessError:
        return False
    return True


def load_commit(repo: Path, rev: str = "HEAD", max_diff_bytes: int | None = None) -> Commit:
    """Load a commit's metadata and its diff against its first parent.

    For a merge commit (multiple parents) the diff is against the first parent, which
    is the conventional "what did this commit introduce" view.
    """
    sha = resolve_sha(repo, rev)
    fmt = "%n" + "%x00".join(FORMAT_FIELDS)
    fields = _parse_record(_run_bytes(repo, ["log", "-1", "--no-color", f"--format={fmt}", sha]))
    patch = _run_bytes(repo, ["show", "--no-color", "--format=", sha]).strip(b"\n")
    return Commit(
        sha=fields["%H"],
        short_sha=sha[:7],
        parents=tuple(fields["%P"].split()),
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


def diff_stat(repo: Path, rev: str = "HEAD") -> str:
    try:
        return _run(repo, ["show", "--no-color", "--stat", rev])
    except GitError, subprocess.SubprocessError:
        return ""


def is_ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", maybe_ancestor, descendant],
            cwd=repo,
            capture_output=True,
            timeout=DEFAULT_TIMEOUT,
            check=False,
        )
    except subprocess.SubprocessError:
        return False
    return result.returncode == 0

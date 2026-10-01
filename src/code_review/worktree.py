"""A throwaway git worktree, so a review cannot disturb the reviewer's checkout.

Why this exists
---------------
A reviewing agent runs arbitrary read and shell tools. Two things follow from that when it
runs in the user's own checkout:

* **It reads the wrong code.** The agent resolves files from the working tree, while the
  review is about a specific commit. Those disagree whenever the tree has moved past that
  commit, whether because the review is retroactive, because it runs in a different worktree
  of the repository, or because the tree was simply dirty.
* **It writes.** Agents that snapshot their work run ``git add --all``, which stages
  untracked files in the user's index. One agent was observed leaving index entries pointing
  at blobs that were never written, leaving the repository unable to commit.

Running the review in a worktree checked out at the reviewed commit fixes both. The trade is
a write to the shared git directory, so it is stated plainly here rather than buried: git
registers a worktree under ``<common>/worktrees/`` for the lifetime of the review.

What that write does and does not touch, all verified against real git:

* No ref is created. ``--detach`` points the worktree's HEAD straight at the commit, so
  ``refs/`` is unchanged. ``git worktree add --detach`` is the whole reason this is cheap.
* The worktree has its own index, so an agent staging files there cannot touch the user's.
  Verified: a user with their own staged file keeps it, byte for byte, across a review.
* Cleanup is ``git worktree remove`` followed by ``git worktree prune``, which leaves neither
  the directory nor the registration behind.

The user's checkout is therefore never the agent's working directory, and the only residue of
a review is a metadata entry that exists for exactly as long as the review does.

Isolation is not a sandbox
--------------------------
This confines *accidents*, not malice. The worktree shares the repository's object store and
config, and an agent that works hard enough can still reach outside it. Confinement of
untrusted commands is the permission policy's job, and this does not weaken it: the policy
client is given the worktree as its root, so what the agent may read is unchanged in kind.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from .git import clean_env

#: Every worktree this module creates is named so it can be recognised later, including
#: after a crash, by a reap that must not touch anything the user made. The owning process id
#: is part of the name, which is what makes reaping safe across processes.
PREFIX = "code-review-"

#: ``<prefix><pid>-<random>``, so a leftover can be attributed to the process that made it.
_OWNER = re.compile(rf"^{re.escape(PREFIX)}(?P<pid>\d+)-")

#: Written into every worktree this module creates, and required before one may be removed.
#:
#: Ownership cannot safely be inferred from the directory name alone. The prefix is guessable,
#: so a user who names a worktree ``code-review-<digits>-something`` would produce a path that
#: looks exactly like ours, and if that number happened to be a dead pid the reap would delete
#: their worktree. A marker file cannot be produced by accident.
#:
#: It holds the creating pid as well, and that is the only place the pid is read from. A name
#: is chosen by whoever created the directory; a marker is written by us, so there is no reason
#: to trust the name for anything once the marker exists.
#:
#: An agent that deletes or corrupts the marker costs us a reap, not a user's worktree, which
#: is the right way round for something whose failure mode is deleting work.
MARKER = ".code-review-worktree"

DEFAULT_TIMEOUT = 120

log = logging.getLogger(__name__)


class WorktreeError(RuntimeError):
    """A throwaway worktree could not be created or removed."""


def _run(repo: Path, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=clean_env(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise WorktreeError(f"git {' '.join(args[:2])} failed: {detail}")
    return result.stdout


def _path_for_new_worktree() -> Path:
    """A path outside the repository, since a worktree nested in one is confusing to git.

    The system temp directory is the honest choice: it is on the same filesystem in the common
    case, it is not inside the user's checkout, and it is not tracked by anything.
    """
    return Path(tempfile.mkdtemp(prefix=f"{PREFIX}{os.getpid()}-"))


def create(repo: Path, sha: str) -> Path:
    """Create a detached worktree at *sha* and return its path.

    Leftovers are cleared first, here rather than in a caller, because this is the one point
    every worktree passes through. A reap in the daemon worker alone would miss ``review
    --inline``, which never involves the daemon, and that is a path users take by hand often
    enough to leak on.

    The caller owns cleanup; use :func:`isolated` unless you specifically need to hold it open
    across several operations.
    """
    _reap_quietly(repo)
    path = _path_for_new_worktree()
    try:
        _run(repo, ["worktree", "add", "--detach", "--quiet", str(path), sha])
        (path / MARKER).write_text(f"{os.getpid()}\n", encoding="utf-8")
    except WorktreeError, OSError:
        # git may have registered a partial worktree before failing; do not leave it behind.
        _discard(repo, path)
        raise
    return path


def remove(repo: Path, path: Path) -> None:
    """Remove *path* and its registration, tolerating whatever the agent left inside.

    Removal is forced because the agent may have written files, and a review that is being
    torn down is not a review whose contents still matter. Prune afterwards clears any
    registration whose directory has already gone, which is the normal case once the agent
    has deleted things.
    """
    _discard(repo, path)


def _discard(repo: Path, path: Path) -> None:
    with contextlib.suppress(WorktreeError):
        _run(repo, ["worktree", "remove", "--force", str(path)])
    with contextlib.suppress(WorktreeError):
        _run(repo, ["worktree", "prune"])
    shutil.rmtree(path, ignore_errors=True)


def _reap_quietly(repo: Path) -> None:
    """Reap leftovers, treating failure as housekeeping rather than as a reason to stop.

    A repository too mangled for ``git worktree list`` should still be reviewable, so a problem
    here is reported and stepped over instead of raised.
    """
    try:
        for path in reap_stale(repo):
            log.warning("removed a leftover review worktree from an interrupted review: %s", path)
    except (WorktreeError, OSError, subprocess.SubprocessError) as exc:
        log.warning("could not check for leftover review worktrees in %s: %s", repo, exc)


@contextlib.contextmanager
def isolated(repo: Path, sha: str) -> Iterator[Path]:
    """Yield a throwaway worktree at *sha*, removing it however the block exits.

    A review that times out, crashes, or is cancelled still gets its worktree removed, since
    cleanup happens in ``finally`` and does not depend on the review succeeding.
    """
    path = create(repo, sha)
    try:
        yield path
    finally:
        _discard(repo, path)


def reap_stale(repo: Path) -> list[Path]:
    """Remove leftover worktrees from a previous run that died without cleaning up.

    Only ever removes worktrees whose name carries :data:`PREFIX`, so a worktree the user
    created by hand is never a candidate, however broken the review that left it behind.
    """
    removed: list[Path] = []
    listing = _run(repo, ["worktree", "list", "--porcelain"])
    # Porcelain output is one block per worktree, each starting with "worktree <path>".
    for line in listing.splitlines():
        prefix, _, value = line.strip().partition(" ")
        if prefix != "worktree" or not value:
            continue
        path = Path(value)
        if not _is_marked(path) or not owner_is_gone(path):
            continue
        _discard(repo, path)
        removed.append(path)
    return removed


def is_ours(path: Path) -> bool:
    """Whether *path* looks like a worktree this module created.

    A name test, kept because it is useful and cheap, but deliberately not sufficient: see
    :data:`MARKER` for why the reap requires the marker as well.
    """
    return path.name.startswith(PREFIX)


def _is_marked(path: Path) -> bool:
    """Whether *path* carries this module's marker, which is what proves we made it."""
    try:
        return (path / MARKER).is_file()
    except OSError:
        return False


def owner_pid(path: Path) -> int | None:
    """The pid recorded in *path*'s marker, or None if it is not one of ours.

    The marker is the single source of truth for both ownership and liveness. Ownership
    obviously has to be provable rather than inferred, and once the marker exists there is no
    reason to read the pid back out of the directory name: a name is chosen by a caller, a
    marker is written by us. Splitting the two meant the name still decided liveness, which
    left the guessable half of the pair load-bearing for the thing it should not decide.
    """
    try:
        recorded = (path / MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(recorded)
    except ValueError:
        return None


def owner_is_gone(path: Path) -> bool:
    """Whether the process that created *path* is no longer running.

    This is what makes reaping safe to do from a second process. An inline ``review`` and the
    daemon can run at the same time against the same repository, and a reap that ignored
    liveness would delete the worktree a review was actively using, turning a housekeeping step
    into a way to break a review in flight.

    A reused pid makes this conservative in the safe direction: a stale worktree is left alone
    until that process exits, rather than being removed out from under a live review.
    """
    pid = owner_pid(path)
    if pid is None or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # The process exists and belongs to someone else. Not evidence that it is dead.
        return False
    return False

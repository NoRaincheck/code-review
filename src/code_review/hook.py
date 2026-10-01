"""The ``post-commit`` hook: generation, installation, and its runtime entry point.

Two separate concerns live here on purpose. ``install``/``uninstall``/``status`` manage
the file. ``main`` is what the generated script executes, and is written so that *every*
exit path returns 0: a code review tool that can block a commit is worse than no tool.
"""

from __future__ import annotations

import contextlib
import stat
import sys
from pathlib import Path

from . import git, paths

MARKER_BEGIN = "# >>> code-review post-commit >>>"
MARKER_END = "# <<< code-review post-commit <<<"
GITIGNORE_ENTRY = "# code-review\n.codereview/\n"

# Identifies our command line inside the hook, independent of the paths it embeds.
_ENQUEUE_MARKER = "-m code_review enqueue"

# The body plus BEGIN/END markers, indented so it can be dropped into a larger hook
# without disturbing whatever the repository already had.
# The command line is a single shell statement so the managed block stays easy to remove
# exactly. The mkdir matters: the redirect target is created by the shell before the CLI
# runs, so a missing home directory would otherwise abort the hook and never enqueue.
HOOK_BODY = """\
{begin}
# Runs after every successful commit. Never fails the commit.
# The daemon is started on demand and reviews in the background.
#
# To uninstall: code-review hook uninstall
{end}
mkdir -p "{home}" && "{executable}" -m code_review enqueue >> "{hook_log}" 2>&1 || true
"""

HOOK_SCRIPT = f"#!/bin/sh\n{HOOK_BODY}"


class HookError(Exception):
    """The hook could not be installed or removed."""


def hook_body() -> str:
    """Render the managed block with absolute paths baked in.

    Absolute paths matter: git runs hooks from the top of the worktree with a minimal
    environment, so a bare ``code-review`` on PATH may not resolve.

    ``sys.executable`` is deliberately *not* symlink-resolved. In a virtualenv it is
    ``.venv/bin/python``, a symlink to the base interpreter; resolving it would point the
    hook at an interpreter that has no ``code_review`` installed, so every commit would
    fail with "No module named code_review".
    """
    return HOOK_BODY.format(
        begin=MARKER_BEGIN,
        end=MARKER_END,
        executable=Path(sys.executable).absolute(),
        home=paths.home_dir(),
        hook_log=paths.hook_log_path(),
    )


def hook_path(repo: Path) -> Path:
    """Absolute path of the post-commit hook, resolved by git.

    Not ``.git/hooks/post-commit``: in a linked worktree ``.git`` is a file, so that path
    cannot exist. git keeps hooks in the shared common directory, which means a hook
    installed from any worktree applies to all of them.
    """
    return git.hooks_dir(repo) / "post-commit"


def is_installed(repo: Path) -> bool:
    path = hook_path(repo)
    if not path.exists():
        return False
    try:
        return MARKER_BEGIN in path.read_text(encoding="utf-8")
    except OSError:
        return False


def install(repo: Path, *, force: bool = False) -> Path:
    """Write the hook, preserving any pre-existing hook content around our block.

    Re-running refreshes an already-installed block rather than returning early, because
    the block embeds absolute paths that can go stale: a moved interpreter, a changed
    ``CODEREVIEW_HOME``, or a hook written by an older version of this tool.
    """
    path = hook_path(repo)
    body = hook_body()

    if path.exists() and not force:
        existing = path.read_text(encoding="utf-8")
        if MARKER_BEGIN in existing:
            if _command_line() and _command_line() in existing:
                return path  # the embedded paths are already current
            # Rewrite, keeping whatever surrounds our block.
            preserved = _strip_managed_block(existing)
            hook_path(repo).parent.mkdir(parents=True, exist_ok=True)
            path.write_text(preserved + body, encoding="utf-8")
            _make_executable(path)
            return path
        if existing.strip():
            raise HookError(
                f"{path} already exists with content code-review did not write. "
                f"Re-run with --force to wrap the existing hook."
            )

    hook_path(repo).parent.mkdir(parents=True, exist_ok=True)
    preserved = ""
    if path.exists() and force:
        existing = path.read_text(encoding="utf-8")
        if MARKER_BEGIN not in existing:
            preserved = existing.rstrip("\n") + "\n\n"
        else:
            preserved = _strip_managed_block(existing)

    path.write_text(preserved + body, encoding="utf-8")
    _make_executable(path)
    return path


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _strip_managed_block(text: str) -> str:
    """Remove the managed block and our command line, keeping surrounding content.

    The block spans MARKER_BEGIN through MARKER_END, but our enqueue command sits on the
    line *after* MARKER_END, so the span must be extended to consume it too.
    """
    if MARKER_BEGIN not in text:
        return text

    start = text.index(MARKER_BEGIN)
    end_marker = text.find(MARKER_END, start)
    if end_marker == -1:
        return text[:start].rstrip("\n") + ("\n" if text[:start].strip() else "")

    # Past the closing marker line.
    newline = text.find("\n", end_marker)
    end = len(text) if newline == -1 else newline + 1

    # Then past our enqueue command, which follows the marker on the next line. Matched by
    # the "-m code_review enqueue" marker rather than by exact text, because a stale block
    # embeds different paths than the current one and must still be removable.
    following = text[end:]
    if _ENQUEUE_MARKER in following:
        offset = following.index(_ENQUEUE_MARKER)
        line_end = following.find("\n", offset)
        end += len(following) if line_end == -1 else line_end + 1

    # Keep whatever the repository's own hook had, on both sides of our block.
    prefix = text[:start].rstrip("\n")
    suffix = text[end:].lstrip("\n")
    return "\n\n".join(part for part in (prefix, suffix) if part)


def _command_line() -> str:
    """The single line of shell that invokes the CLI, as it appears in the hook."""
    for line in hook_body().splitlines():
        if _ENQUEUE_MARKER in line:
            return line
    return ""


def uninstall(repo: Path) -> bool:
    """Remove the managed block. Returns True if something was removed."""
    path = hook_path(repo)
    if not path.exists():
        return False
    existing = path.read_text(encoding="utf-8")
    if MARKER_BEGIN not in existing:
        return False

    remainder = _strip_managed_block(existing).rstrip("\n")
    if remainder:
        path.write_text(remainder + "\n", encoding="utf-8")
    else:
        path.unlink()
    return True


def ensure_gitignore(repo: Path) -> bool:
    """Keep a leftover state directory ignored, but only bother if there is one.

    The database moved under the shared git directory, so this tool no longer writes anything
    into the working tree. A fresh install therefore has nothing to ignore, and adding the
    entry anyway means editing a file the user never asked us to touch, on the strength of a
    directory that will not exist. That also contradicts what `init` and the README say about
    the working tree staying clean.

    A leftover from an earlier version is a real case, though, and a stale
    ``.codereview/reviews.db`` sitting untracked in someone's tree is worse than a redundant
    ignore rule. So the entry is added exactly when there is something to ignore.

    Returns True if the file changed.
    """
    if not paths.state_dir(repo).exists():
        return False
    gitignore = repo / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".codereview/" in existing.splitlines():
        return False
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(existing + prefix + GITIGNORE_ENTRY, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    """The hook entry point.

    Resolves HEAD itself: git passes no arguments to ``post-commit``. Always returns 0.
    """
    try:
        # git runs hooks from the top of the worktree, so cwd is the right starting point.
        # GIT_DIR is relative (".git") in that case and is not a usable path on its own.
        repo = git.toplevel(Path.cwd())

        target = argv[0] if argv else "HEAD"
        try:
            sha = git.resolve_sha(repo, target)
        except git.GitError:
            # Nothing resolvable (empty repo, or a state we cannot read).
            return 0

        from .daemon import client as daemon_client

        daemon_client.ensure_and_enqueue(sha, repo, git.current_branch(repo))
    except Exception as exc:  # noqa: BLE001 - a commit must never fail here
        # Recording the failure is best-effort: if even the log cannot be written there is
        # nowhere left to report to, and the commit still must not fail.
        with contextlib.suppress(OSError):
            paths.ensure_home()
            with paths.hook_log_path().open("a", encoding="utf-8") as handle:
                handle.write(f"code-review enqueue failed: {type(exc).__name__}: {exc}\n")
    return 0

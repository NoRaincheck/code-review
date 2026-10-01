"""Argparse dispatch for the ``code-review`` command."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, git, paths
from . import config as config_module
from . import hook as hook_module
from .config import ConfigError
from .daemon import client as daemon_client
from .daemon import server as daemon_server
from .daemon import supervisor
from .db import Database, Review, Status
from .git import GitError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_REVIEW = 2
EXIT_AMBIGUOUS = 3


# -- helpers ---------------------------------------------------------------


def _fail(message: str, code: int = EXIT_ERROR) -> int:
    print(f"code-review: {message}", file=sys.stderr)
    return code


def _repo_or_fail(path: str | None) -> Path:
    start = Path(path) if path else Path.cwd()
    try:
        if not git.is_repo(start):
            raise GitError(["rev-parse"], 128, f"{start} is not inside a git repository")
        return git.toplevel(start)
    except GitError as exc:
        raise SystemExit(_fail(str(exc))) from exc


def _open_db(repo: Path) -> Database:
    return Database.for_repo(repo)


class AmbiguousRevision(Exception):
    """A short sha matched more than one reviewed commit."""

    def __init__(self, prefix: str, candidates: list[str]) -> None:
        super().__init__(f"{prefix!r} is ambiguous: it matches {len(candidates)} reviewed commits")
        self.prefix = prefix
        self.candidates = candidates


def _find_review(database: Database, repo: Path, rev: str | None) -> Review:
    """Resolve *rev* to exactly one stored review, or explain why it cannot.

    Resolution order mirrors git: an exact sha, then a live revision such as HEAD, then a
    unique abbreviation. Ambiguity is an error rather than a guess, because showing one
    commit's review for another is worse than showing nothing.

    Reviews for commits that no longer exist, after a rebase or reset, are still reachable
    by sha because the database is not part of the repository.
    """
    if rev and rev != "HEAD":
        exact = database.get(rev)
        if exact is not None:
            return exact

    sha = _resolve_sha_safe(repo, rev)
    if sha:
        exact = database.get(sha)
        if exact is not None:
            return exact

    if not rev:
        raise LookupError("no review for HEAD")

    candidates = database.matches_prefix(rev)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise AmbiguousRevision(rev, [c.sha for c in candidates])
    raise LookupError(f"no review for {rev}")


def _resolve_sha_safe(repo: Path, rev: str | None) -> str | None:
    target = rev or "HEAD"
    try:
        return git.resolve_sha(repo, target)
    except GitError:
        return None


def _relative_time(iso: str | None) -> str:
    if not iso:
        return "-"
    from datetime import UTC, datetime

    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    delta = int(datetime.now(UTC).timestamp() - then.timestamp())
    if delta < 60:
        return f"{max(delta, 0)}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# -- commands --------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    repo = _repo_or_fail(args.repo)
    state = paths.state_dir(repo)
    state.mkdir(parents=True, exist_ok=True)

    database = _open_db(repo)
    database.close()

    gitignored = hook_module.ensure_gitignore(repo)
    try:
        hook_path = hook_module.install(repo, force=args.force)
        hook_state = "installed"
    except hook_module.HookError as exc:
        hook_path = hook_module.hook_path(repo)
        hook_state = f"not installed: {exc}"

    config = config_module.load_config(repo)
    config_state = "configured" if config.agent else "no review_agent set"

    print(f"repository:  {repo}")
    print(f"state dir:   {state}")
    print(f"database:    {paths.db_path(repo)}")
    print(f"hook:        {hook_state} ({hook_path})")
    print(f"gitignore:   {'updated' if gitignored else 'already ignored'}")
    print(f"config:      {config_state}")
    if not config.agent:
        print()
        print(f"No review agent configured. Add to {paths.config_path(repo)}:")
        print('  review_agent = ["claude-agent-acp"]')
    return EXIT_OK


def cmd_enqueue(args: argparse.Namespace) -> int:
    """Hook entry point. Always exits 0 so a commit is never blocked."""
    return hook_module.main([args.sha] if args.sha else None)


def cmd_review(args: argparse.Namespace) -> int:
    repo = _repo_or_fail(args.repo)
    try:
        sha = git.resolve_sha(repo, args.sha)
    except GitError as exc:
        return _fail(str(exc))

    if args.agent:
        args.config_path = None

    if not args.daemon:
        return _review_inline(repo, sha, args)

    try:
        supervisor.ensure_running()
        result = asyncio.run(
            daemon_client.call(
                "review",
                {
                    "repo": str(repo),
                    "sha": sha,
                    "branch": git.current_branch(repo),
                    "force": bool(args.force),
                },
                timeout=10.0,
            )
        )
    except daemon_client.RequestFailed as exc:
        return _fail(str(exc))
    except supervisor.UnsupportedPlatform as exc:
        return _fail(str(exc))
    except (daemon_client.DaemonUnavailable, supervisor.DaemonAlreadyRunning) as exc:
        return _fail(f"could not reach the daemon: {exc}")

    print(f"queued {sha[:7]} (review #{result.get('id')})")
    if not args.wait:
        print(f"retrieve it with: code-review show {sha[:7]}")
        return EXIT_OK
    return _wait_and_show(repo, sha)


def _review_inline(repo: Path, sha: str, args: argparse.Namespace) -> int:
    """Run the review in this process, without involving the daemon."""
    config = _config_for(repo, args)
    if config.agent is None:
        return _fail(
            config_module.ConfigError(
                f"no review_agent configured. Set review_agent in {paths.config_path(repo)}"
            ).__str__()
        )

    from .agent.runner import ReviewError, ReviewRunner

    print(f"reviewing {sha[:7]} with {config.agent.label} ...", file=sys.stderr)

    # Persist even in inline mode, so `code-review show` and the tool-call audit trail
    # behave identically whether or not the daemon was used.
    database = _open_db(repo)
    review_id, _created = database.enqueue(sha, git.current_branch(repo), force=True)

    async def go() -> ReviewRunner:
        runner = ReviewRunner(config, repo, timeout_seconds=args.timeout)
        try:
            await runner.review(git.load_commit(repo, sha, max_diff_bytes=config.max_diff_bytes))
        except TimeoutError as exc:
            # Keep whatever streamed before the deadline; a partial review is worth more
            # than none, and the row says so via status='cancelled'.
            database.cancel(
                review_id,
                runner.partial_markdown,
                agent=config.agent.label if config.agent else None,
            )
            raise ReviewError(str(exc)) from exc
        except ReviewError as exc:
            database.fail(review_id, str(exc))
            raise
        except BaseException as exc:
            database.fail(review_id, f"{type(exc).__name__}: {exc}")
            raise
        return runner

    try:
        runner = asyncio.run(go())
        outcome = runner.last_outcome
        assert outcome is not None
        database.complete(
            review_id,
            markdown=outcome.markdown,
            stop_reason=outcome.stop_reason,
            agent=outcome.agent,
            agent_version=outcome.agent_version,
            usage=outcome.usage,
            tool_log=outcome.tool_log,
        )
        print(outcome.markdown)
    except ReviewError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        return _fail("interrupted")
    finally:
        database.close()
    return EXIT_OK


def _config_for(repo: Path, args: argparse.Namespace) -> config_module.Config:
    config = config_module.load_config(repo)
    if getattr(args, "agent", None):
        config = config_module.Config(
            agent=config_module.AgentConfig(
                command=args.agent[0],
                args=tuple(args.agent[1:]),
                env=config.agent.env if config.agent else {},
            ),
            review_guidelines=config.review_guidelines,
            max_diff_bytes=config.max_diff_bytes,
            review_timeout_seconds=args.timeout or config.review_timeout_seconds,
            deny_unknown_commands=config.deny_unknown_commands,
            source_path=config.source_path,
        )
    return config


def _wait_and_show(repo: Path, sha: str, timeout: float = 3600.0) -> int:
    database = _open_db(repo)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            review = database.get(sha)
            if review is not None and review.status in {
                Status.COMPLETED,
                Status.FAILED,
                Status.CANCELLED,
            }:
                return _render(review, raw=False)
            time.sleep(0.2)
    finally:
        database.close()
    return _fail("timed out waiting for the review")


def cmd_show(args: argparse.Namespace) -> int:
    repo = _repo_or_fail(args.repo)
    database = _open_db(repo)
    try:
        review = _find_review(database, repo, args.sha)
        return _render(review, raw=args.raw)
    except AmbiguousRevision as exc:
        print(f"code-review: {exc}", file=sys.stderr)
        for candidate in exc.candidates:
            print(f"  {candidate}", file=sys.stderr)
        return EXIT_AMBIGUOUS
    except LookupError:
        target = args.sha or "HEAD"
        print(
            f"code-review: no review for {target}. Run: code-review review {target}",
            file=sys.stderr,
        )
        return EXIT_NO_REVIEW
    finally:
        database.close()


def _render(review: Any, *, raw: bool) -> int:
    if review.status == Status.FAILED:
        print(
            f"code-review: review of {review.short_sha} failed: {review.error}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if raw:
        print(review.markdown or "")
        return EXIT_OK

    header = [
        f"review {review.short_sha} on {review.branch or 'detached HEAD'}",
        f"agent: {review.agent or 'unknown'}  status: {review.status.value}",
    ]
    if review.stop_reason and review.stop_reason != "end_turn":
        header.append(f"stop reason: {review.stop_reason} (review may be incomplete)")
    if review.usage:
        header.append(f"tokens: {review.usage}")
    print("\n".join(header))
    print("=" * 72)
    if review.markdown:
        print(review.markdown)
    elif review.status == Status.CANCELLED:
        print("(cancelled before any output)")
    else:
        print(f"(no review yet, status: {review.status.value})")
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    repo = _repo_or_fail(args.repo)
    database = _open_db(repo)
    try:
        status = Status(args.status) if args.status else None
        reviews = database.list(status=status, limit=args.limit)
        if not reviews:
            print("no reviews yet")
            return EXIT_OK
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "sha": r.sha,
                            "short_sha": r.short_sha,
                            "branch": r.branch,
                            "status": r.status.value,
                            "stop_reason": r.stop_reason,
                            "agent": r.agent,
                            "error": r.error,
                            "created_at": r.created_at,
                            "finished_at": r.finished_at,
                        }
                        for r in reviews
                    ],
                    indent=2,
                )
            )
            return EXIT_OK
        print(f"{'SHA':<9} {'STATUS':<10} {'BRANCH':<20} {'WHEN':<10} SUBJECT")
        for review in reviews:
            first_line = (review.markdown or review.error or "").splitlines()
            subject = first_line[0][:40] if first_line else ""
            when = _relative_time(review.finished_at or review.created_at)
            print(
                f"{review.short_sha:<9} {review.status.value:<10} "
                f"{(review.branch or '-'):<20} {when:<10} {subject}"
            )
        return EXIT_OK
    finally:
        database.close()


def cmd_status(args: argparse.Namespace) -> int:
    repo = None
    if args.repo or git.is_repo(Path.cwd()):
        try:
            repo = _repo_or_fail(args.repo)
        except SystemExit:
            repo = None

    print(f"daemon:      {_daemon_state()}")
    socket = paths.socket_path()
    suffix = "" if socket.exists() else " (absent)"
    print(f"socket:      {socket}{suffix}")
    print(f"log:         {paths.daemon_log_path()}")

    if daemon_client.ping():
        try:
            data = asyncio.run(daemon_client.call("status", timeout=5.0))
            current = data.get("current")
            print(f"queue depth: {data.get('queue_depth', 0)}")
            print(f"completed:   {data.get('completed', 0)}")
            print(f"failed:      {data.get('failed', 0)}")
            if current:
                print(f"in progress: {current.get('short_sha')} in {current.get('repo')}")
        except daemon_client.DaemonUnavailable:
            print("queue depth: unavailable (socket accepted but no reply)")

    if repo is not None:
        database = _open_db(repo)
        try:
            counts = database.counts()
            print()
            print(f"reviews for {repo}:")
            for status in Status:
                print(f"  {status.value:<10} {counts.get(status.value, 0)}")
            print(f"  {'total':<10} {counts.get('total', 0)}")
        finally:
            database.close()
    return EXIT_OK


def cmd_daemon(args: argparse.Namespace) -> int:
    if args.action == "run":
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(daemon_server.serve_forever())
        return EXIT_OK

    if args.action == "start":
        try:
            pid = supervisor.ensure_running()
        except supervisor.UnsupportedPlatform as exc:
            return _fail(str(exc))
        except supervisor.DaemonAlreadyRunning as exc:
            return _fail(str(exc))
        print(f"daemon running (pid {pid})")
        return EXIT_OK

    if args.action == "stop":
        try:
            supervisor.stop_daemon()
        except supervisor.UnsupportedPlatform as exc:
            return _fail(str(exc))
        except supervisor.DaemonNotRunning as exc:
            return _fail(str(exc))
        print("daemon stopped")
        return EXIT_OK

    running = supervisor.is_running()
    print(f"daemon: {_daemon_state()}")
    return EXIT_OK if running else EXIT_ERROR


def _daemon_state() -> str:
    pid = supervisor.daemon_pid()
    if pid is None:
        return "stopped"
    return f"running (pid {pid})"


def cmd_hook(args: argparse.Namespace) -> int:
    repo = _repo_or_fail(args.repo)
    if args.action == "install":
        try:
            path = hook_module.install(repo, force=args.force)
        except hook_module.HookError as exc:
            return _fail(str(exc))
        print(f"installed {path}")
        return EXIT_OK
    if args.action == "uninstall":
        if hook_module.uninstall(repo):
            print("hook removed")
        else:
            print("no code-review hook installed")
        return EXIT_OK
    installed = hook_module.is_installed(repo)
    print(f"hook: {'installed' if installed else 'not installed'}")
    return EXIT_OK if installed else EXIT_ERROR


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check that everything needed is in place. ``--probe`` also proves the agent works."""
    repo = None
    if args.repo:
        repo = _repo_or_fail(args.repo)
    elif git.is_repo(Path.cwd()):
        repo = _repo_or_fail(None)

    print(f"code-review {__version__} (python {sys.version.split()[0]})")
    print(f"home:        {paths.home_dir()}")
    print(f"socket:      {paths.socket_path()}")

    try:
        from importlib.metadata import version

        import acp  # noqa: F401 - present-check only

        print(f"acp sdk:     agent-client-protocol {version('agent-client-protocol')}")
    except Exception as exc:  # noqa: BLE001
        print(f"acp sdk:     MISSING ({exc})")

    if repo is None:
        print("repository:  none (not inside a git repository)")
        return EXIT_OK

    print(f"repository:  {repo}")
    try:
        config = config_module.load_config(repo)
    except ConfigError as exc:
        print(f"config:      INVALID: {exc}")
        return EXIT_ERROR

    if config.agent is None:
        print("agent:       NOT CONFIGURED")
        print(f"              set review_agent in {paths.config_path(repo)}")
    else:
        _report_agent(repo, config, probe=args.probe)

    print(f"hook:        {'installed' if hook_module.is_installed(repo) else 'not installed'}")
    print(f"database:    {paths.db_path(repo)}")
    print(f"daemon:      {'running' if supervisor.is_running() else 'stopped'}")
    return EXIT_OK


def _report_agent(repo: Path, config: config_module.Config, *, probe: bool) -> None:
    """Print what is known about the configured agent, and optionally prove it speaks ACP."""
    agent = config.require_agent()
    print(f"agent:       {' '.join(agent.argv)}")

    resolved = shutil_which(agent.command)
    if resolved is None:
        print(f"              WARNING: {agent.command!r} not found on PATH")
    else:
        print(f"              found at {resolved}")

    if not probe:
        print("              run `code-review doctor --probe` to check it speaks ACP")
        return

    from .agent.probe import describe, probe_agent

    result = asyncio.run(probe_agent(agent, repo))
    for line in describe(result):
        print(line if line.startswith("acp probe") else f"              {line.strip()}")


def shutil_which(command: str) -> str | None:
    from shutil import which

    return which(command)


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-review",
        description="Background AI code review of every commit, orchestrated over ACP.",
    )
    parser.add_argument("--version", action="version", version=f"code-review {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_repo(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", help="path inside the repository (default: cwd)")

    p_init = sub.add_parser("init", help="install the post-commit hook and create state")
    add_repo(p_init)
    p_init.add_argument("--force", action="store_true", help="wrap an existing post-commit hook")
    p_init.set_defaults(func=cmd_init)

    p_enqueue = sub.add_parser("enqueue", help="hook entry point; queues HEAD for review")
    p_enqueue.add_argument("sha", nargs="?", help="commit to review (default: HEAD)")
    p_enqueue.set_defaults(func=cmd_enqueue)

    p_review = sub.add_parser("review", help="queue a review of a commit")
    p_review.add_argument("sha", help="commit to review")
    add_repo(p_review)
    p_review.add_argument("--agent", nargs=1, help="override the agent command and args")
    p_review.add_argument("--timeout", type=int, help="review timeout in seconds")
    p_review.add_argument("--wait", action="store_true", help="block until the review finishes")
    p_review.add_argument(
        "--force",
        action="store_true",
        help="re-review a commit that already has a review, replacing the stored result",
    )
    p_review.add_argument(
        "--inline",
        dest="daemon",
        action="store_false",
        default=True,
        help="run in this process instead of the daemon",
    )
    p_review.set_defaults(func=cmd_review)

    p_show = sub.add_parser("show", help="print the stored review")
    p_show.add_argument("sha", nargs="?", help="commit (default: HEAD)")
    add_repo(p_show)
    p_show.add_argument("--raw", action="store_true", help="markdown only, no header")
    p_show.set_defaults(func=cmd_show)

    p_list = sub.add_parser("list", help="list recent reviews")
    add_repo(p_list)
    p_list.add_argument("--status", choices=[s.value for s in Status])
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="daemon and queue status")
    add_repo(p_status)
    p_status.set_defaults(func=cmd_status)

    p_daemon = sub.add_parser("daemon", help="control the background daemon")
    p_daemon.add_argument("action", choices=["run", "start", "stop", "status"])
    p_daemon.set_defaults(func=cmd_daemon)

    p_hook = sub.add_parser("hook", help="manage the post-commit hook")
    p_hook.add_argument("action", choices=["install", "uninstall", "status"])
    add_repo(p_hook)
    p_hook.add_argument("--force", action="store_true")
    p_hook.set_defaults(func=cmd_hook)

    p_doctor = sub.add_parser("doctor", help="check the installation")
    add_repo(p_doctor)
    p_doctor.add_argument(
        "--probe",
        action="store_true",
        help="spawn the agent and complete an ACP handshake to prove it works",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "repo"):
        args.repo = None
    try:
        return int(args.func(args))
    except ConfigError as exc:
        return _fail(str(exc))
    except GitError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        return _fail("interrupted", 130)
    except BrokenPipeError:  # pragma: no cover - piping into head
        os._exit(EXIT_OK)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

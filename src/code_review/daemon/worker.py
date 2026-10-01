"""The review queue and the loop that drains it.

One review at a time, per repository, round-robin across the repositories the daemon has
been told about. Serialising matters: two reviews of adjacent commits run concurrently
means the second one reads a tree the first has not caught up to, and both pay for a
process spawn that the machine cannot absorb anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config as config_module
from .. import git
from ..agent.runner import ReviewError, ReviewRunner
from ..db import Database, Review, Status

log = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 1.0


@dataclass(slots=True)
class WorkerStats:
    """What ``code-review status`` reports."""

    running: bool = False
    current: dict[str, Any] | None = None
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    repos: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "current": self.current,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "repos": self.repos,
        }


class Worker:
    """Drains queued reviews, one at a time."""

    def __init__(self, *, timeout_seconds: int | None = None) -> None:
        self._databases: dict[Path, Database] = {}
        self._order: list[Path] = []
        self._wakeup = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._current: tuple[Path, Review] | None = None
        self._timeout_seconds = timeout_seconds
        self.stats = WorkerStats()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self.stats.running = True
        self._task = asyncio.create_task(self._loop(), name="code-review-worker")

    async def stop(self) -> None:
        self._stopping.set()
        self._wakeup.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self.stats.running = False
        self._close_all()

    def _close_all(self) -> None:
        for database in self._databases.values():
            try:
                database.close()
            except Exception:
                log.debug("failed to close database", exc_info=True)
        self._databases.clear()
        self._order.clear()

    def notify(self) -> None:
        """Wake the loop after new work arrives."""
        self._wakeup.set()

    # -- repository registration -----------------------------------------

    def database_for(self, repo: Path) -> Database:
        repo = Path(repo).resolve()
        database = self._databases.get(repo)
        if database is None:
            database = Database.for_repo(repo)
            # Anything left mid-flight belongs to a process that is gone.
            recovered = database.recover_interrupted()
            if recovered:
                log.info("re-queued %d interrupted review(s) for %s", recovered, repo)
            self._databases[repo] = database
            self._order.append(repo)
        return database

    def register(self, repo: Path) -> None:
        self.database_for(repo)

    def queue_depth(self) -> int:
        total = 0
        for database in self._databases.values():
            total += database.counts().get(Status.QUEUED.value, 0)
        return total

    def snapshot(self) -> WorkerStats:
        current = None
        if self._current is not None:
            repo, review = self._current
            current = {"repo": str(repo), "sha": review.sha, "short_sha": review.short_sha}
        return WorkerStats(
            running=self.stats.running,
            current=current,
            completed=self.stats.completed,
            failed=self.stats.failed,
            cancelled=self.stats.cancelled,
            repos=[str(path) for path in self._order],
        )

    # -- the loop ---------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                processed = await self._drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker loop iteration failed")
                processed = False

            if processed:
                continue

            self._wakeup.clear()
            # Woken by notify(), or polled once a second so a missed wakeup cannot wedge
            # the queue. Either outcome is normal, so the timeout is not an error.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=IDLE_POLL_SECONDS)

    async def _drain_once(self) -> bool:
        """Process at most one review. Returns True if one was processed."""
        for repo in list(self._order):
            database = self._databases[repo]
            review = database.claim_next()
            if review is not None:
                await self._process(repo, review)
                return True
        return False

    async def _process(self, repo: Path, review: Review) -> None:
        self._current = (repo, review)
        try:
            await self._run_review(repo, review)
        except asyncio.CancelledError:
            self._databases[repo].cancel(review.id)
            self.stats.cancelled += 1
            raise
        except Exception as exc:
            log.exception("review of %s failed", review.sha)
            self._databases[repo].fail(review.id, f"{type(exc).__name__}: {exc}")
            self.stats.failed += 1
        finally:
            self._current = None

    async def _run_review(self, repo: Path, review: Review) -> None:
        database = self._databases[repo]
        log.info("reviewing %s in %s", review.short_sha, repo)

        config = config_module.load_config(repo)
        if config.agent is not None:
            # Name the agent before the review starts, so `show` on an in-flight review
            # says which one is working rather than reporting it as unknown.
            database.set_agent(review.id, config.agent.label)
        runner = ReviewRunner(config, repo, timeout_seconds=self._timeout_seconds)
        commit = git.load_commit(repo, review.sha, max_diff_bytes=config.max_diff_bytes)

        try:
            outcome = await runner.review(commit)
        except TimeoutError:
            # Keep whatever the agent managed to produce before the deadline; a partial
            # review of the files it reached is more useful than nothing.
            partial = runner.partial_markdown
            database.cancel(review.id, partial, agent=config.agent.label if config.agent else None)
            self.stats.cancelled += 1
            log.warning("review of %s timed out", review.short_sha)
            return
        except ReviewError as exc:
            database.fail(review.id, str(exc))
            self.stats.failed += 1
            log.warning("review of %s failed: %s", review.short_sha, exc)
            return
        finally:
            self._warn_if_index_damaged(repo, review)

        database.complete(
            review.id,
            markdown=outcome.markdown,
            stop_reason=outcome.stop_reason,
            agent=outcome.agent,
            agent_version=outcome.agent_version,
            usage=outcome.usage,
            tool_log=outcome.tool_log,
        )
        self.stats.completed += 1
        if not outcome.is_complete:
            log.info(
                "review of %s ended with stop_reason=%s",
                review.short_sha,
                outcome.stop_reason,
            )
        log.info("review of %s complete", review.short_sha)

    @staticmethod
    def _warn_if_index_damaged(repo: Path, review: Review) -> None:
        """Tell the user if the review left their repository unable to commit.

        Some agents stage files as a side effect of taking a snapshot. If the objects those
        entries point at are not in this repository's object store, the next ``git commit``
        fails with ``invalid object`` and nothing connects that failure to the review. The
        remedy is cheap and loses nothing, so it is worth saying out loud.
        """
        health = git.check_index(repo)
        if health.intact:
            return
        log.error(
            "review of %s left the git index unusable: %s\n"
            "  The agent appears to have staged files whose objects are missing from the\n"
            "  repository. Unstage them to restore committing: git reset\n"
            "  Nothing is lost: this only unstages, and no file on disk is touched.",
            review.short_sha,
            health.detail,
        )

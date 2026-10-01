"""SQLite persistence for reviews.

Knows nothing about agents or ACP. One database per repository, at
``<repo>/.codereview/reviews.db``.

``reviews`` doubles as the queue: rows with ``status='queued'`` are pending work, so
there is no separate jobs table and a daemon restart resumes from the database.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import git, paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY,
    sha           TEXT    NOT NULL,
    branch        TEXT,
    status        TEXT    NOT NULL,
    markdown      TEXT,
    error         TEXT,
    warning       TEXT,
    stop_reason   TEXT,
    agent         TEXT,
    agent_version TEXT,
    usage_json    TEXT,
    tool_log_json TEXT,
    created_at    TEXT    NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS reviews_sha_unique ON reviews(sha);
CREATE INDEX IF NOT EXISTS reviews_status_idx ON reviews(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_VERSION = "2"

#: Applied on every open, in order, each one a no-op once it has been applied.
MIGRATIONS = ("ALTER TABLE reviews ADD COLUMN warning TEXT",)


class Status(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({Status.COMPLETED, Status.FAILED, Status.CANCELLED})


@dataclass(frozen=True, slots=True)
class Review:
    """One row of ``reviews``."""

    id: int
    sha: str
    branch: str | None
    status: Status
    markdown: str | None
    error: str | None
    warning: str | None
    stop_reason: str | None
    agent: str | None
    agent_version: str | None
    usage: dict[str, Any] | None
    tool_log: list[dict[str, Any]]
    created_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def short_sha(self) -> str:
        return self.sha[:7]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _like_pattern(prefix: str) -> str:
    """Escape LIKE wildcards so a sha containing % or _ cannot match everything."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _row_to_review(row: sqlite3.Row) -> Review:
    return Review(
        id=row["id"],
        sha=row["sha"],
        branch=row["branch"],
        status=Status(row["status"]),
        markdown=row["markdown"],
        error=row["error"],
        warning=row["warning"],
        stop_reason=row["stop_reason"],
        agent=row["agent"],
        agent_version=row["agent_version"],
        usage=json.loads(row["usage_json"]) if row["usage_json"] else None,
        tool_log=json.loads(row["tool_log_json"]) if row["tool_log_json"] else [],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class Database:
    """Thin, synchronous SQLite wrapper. Not shared across threads.

    Opening an existing database does not write to it. That matters because the daemon
    holds the write lock for the duration of a review, and ``code-review show`` is a
    read-only command that must keep working while a review is in flight. WAL lets readers
    proceed during a write, but only if opening does not itself need a write lock, so the
    schema and the version marker are applied only when they are missing or stale.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        # Set before anything that can fail, so a partially constructed object does not
        # raise a confusing AttributeError from __del__ and bury the real error. A None
        # here means the connection was never opened, so there is nothing to close.
        self._closer: Callable[[], None] | None = None
        path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(path, isolation_level=None, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        # A daemon can open many repositories, and a reference cycle through the
        # sqlite3.Connection keeps them from being collected promptly. Closing on
        # collection avoids leaking file descriptors and unraisable ResourceWarnings.
        self._closer = weakref.finalize(self, self._conn.close)

        self._conn.execute("PRAGMA busy_timeout=15000")
        if self._has_table("reviews"):
            self._conn.execute("PRAGMA synchronous=NORMAL")
        else:
            # A fresh database still needs its journal mode set; setting WAL takes a write
            # lock, which is fine because nothing else can be using the file yet.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
        self._migrate()

        self._record_schema_version()

    def _migrate(self) -> None:
        """Bring an existing database up to the current schema.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op for a table that is already there, so a
        column added in a later version has to be added explicitly. Each step is idempotent
        and ignores "already exists", so this is safe to run on every open.
        """
        for statement in MIGRATIONS:
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(statement)

    def _has_table(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _record_schema_version(self) -> None:
        """Store the schema version, if it is missing or out of date.

        Advisory metadata. A failure here is not fatal: the schema itself is already
        correct, and the next open retries. Letting it raise would mean a concurrent writer
        could stop the CLI from reading reviews at all.
        """
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is not None and row["value"] == SCHEMA_VERSION:
                return
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )
        except sqlite3.OperationalError:
            pass

    @classmethod
    def for_repo(cls, repo: Path) -> Database:
        """Open the database for *repo*, shared by every worktree of that repository."""
        return cls(paths.db_path(git.common_dir(repo)))

    def close(self) -> None:
        """Close the connection. Safe to call twice, and on a failed construction."""
        closer = self._closer
        if closer is None:
            # __init__ failed before the connection existed; nothing to close.
            return
        # weakref.finalize is idempotent: the second call is a no-op.
        closer()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- queue operations -------------------------------------------------

    def enqueue(self, sha: str, branch: str | None = None, force: bool = False) -> tuple[int, bool]:
        """Queue a review for *sha*.

        Returns ``(row_id, created)``. With ``force=False`` an existing row for the same
        sha is left untouched and returned as ``created=False``; that is the dedupe that
        keeps a retried hook from duplicating work. With ``force=True`` the existing row
        is reset to ``queued`` and re-run.
        """
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id, status FROM reviews WHERE sha = ?", (sha,)
            ).fetchone()
            if existing is not None and not force:
                return int(existing["id"]), False

            if existing is None:
                cursor = conn.execute(
                    "INSERT INTO reviews(sha, branch, status, created_at) VALUES(?, ?, ?, ?)",
                    (sha, branch, Status.QUEUED.value, _now()),
                )
                return int(cursor.lastrowid or 0), True

            conn.execute(
                "UPDATE reviews SET status=?, branch=?, markdown=NULL, error=NULL, "
                "stop_reason=NULL, usage_json=NULL, tool_log_json=NULL, agent=NULL, "
                "agent_version=NULL, started_at=NULL, finished_at=NULL "
                "WHERE id=?",
                (Status.QUEUED.value, branch, int(existing["id"])),
            )
            return int(existing["id"]), True

    def claim_next(self) -> Review | None:
        """Atomically move the oldest queued review to ``running`` and return it."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM reviews WHERE status=? ORDER BY id ASC LIMIT 1",
                (Status.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE reviews SET status=?, started_at=? WHERE id=?",
                (Status.RUNNING.value, _now(), row["id"]),
            )
            row = conn.execute("SELECT * FROM reviews WHERE id=?", (row["id"],)).fetchone()
            return _row_to_review(row)

    def recover_interrupted(self) -> int:
        """Reset ``running`` rows to ``queued``. Called once at daemon startup.

        Any row still marked running at startup belongs to a process that died, so
        nothing is working on it. Re-queuing is correct; marking it failed would silently
        drop reviews across a daemon restart or a machine reboot.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE reviews SET status=?, started_at=NULL WHERE status=?",
                (Status.QUEUED.value, Status.RUNNING.value),
            )
            return cursor.rowcount

    # -- completion -------------------------------------------------------

    def complete(
        self,
        review_id: int,
        *,
        markdown: str,
        stop_reason: str,
        agent: str,
        agent_version: str | None = None,
        usage: dict[str, Any] | None = None,
        tool_log: list[dict[str, Any]] | None = None,
        warning: str | None = None,
    ) -> None:
        """Record a finished review.

        Partial output is still stored. A turn that stopped at ``max_tokens`` or
        ``refusal`` produced a truncated review, and the stop reason records that so the
        text is never presented as complete.

        *warning* is for output that finished on time and looks like a review, but failed the
        output contract. It is kept separate from *error* because the review did complete:
        discarding the markdown would throw away real work, and calling it a success without
        qualification would present the agent's scratch notes as a verdict.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE reviews SET status=?, markdown=?, stop_reason=?, agent=?, "
                "agent_version=?, usage_json=?, tool_log_json=?, error=NULL, warning=?, "
                "finished_at=? "
                "WHERE id=?",
                (
                    Status.COMPLETED.value,
                    markdown,
                    stop_reason,
                    agent,
                    agent_version,
                    json.dumps(usage) if usage else None,
                    json.dumps(tool_log) if tool_log else None,
                    warning,
                    _now(),
                    review_id,
                ),
            )

    def fail(self, review_id: int, error: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE reviews SET status=?, error=?, finished_at=? WHERE id=?",
                (Status.FAILED.value, error[:4000], _now(), review_id),
            )

    def cancel(
        self,
        review_id: int,
        markdown: str | None = None,
        *,
        agent: str | None = None,
    ) -> None:
        """Mark a review cancelled, keeping any partial markdown already collected.

        *agent* is recorded even though the review produced nothing usable: knowing which
        agent timed out is the first thing worth knowing.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE reviews SET status=?, markdown=COALESCE(?, markdown), "
                "agent=COALESCE(?, agent), stop_reason='cancelled', finished_at=? WHERE id=?",
                (Status.CANCELLED.value, markdown, agent, _now(), review_id),
            )

    def set_agent(self, review_id: int, agent: str) -> None:
        """Record which agent is working on a review.

        Written before the review runs rather than after it finishes, so that `show` on an
        in-flight review names the agent instead of reporting it as unknown. That is the
        first thing worth knowing about a review that is taking too long.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE reviews SET agent=COALESCE(agent, ?) WHERE id=?", (agent, review_id)
            )

    # -- retrieval --------------------------------------------------------

    def get(self, sha: str) -> Review | None:
        """Exact sha match. Does not accept an abbreviation."""
        row = self._conn.execute("SELECT * FROM reviews WHERE sha=?", (sha,)).fetchone()
        return _row_to_review(row) if row is not None else None

    def matches_prefix(self, prefix: str) -> list[Review]:
        """Every review whose sha starts with *prefix*, oldest first.

        Separate from :meth:`get` because a prefix can be ambiguous. git refuses an
        ambiguous abbreviation rather than guessing, and so must this: silently returning one
        of two commits would show a reviewer the wrong commit's findings.
        """
        if len(prefix) < 4:
            return []
        rows = self._conn.execute(
            "SELECT * FROM reviews WHERE sha LIKE ? ESCAPE '\\' ORDER BY id ASC",
            (_like_pattern(prefix),),
        ).fetchall()
        return [_row_to_review(row) for row in rows]

    def get_by_id(self, review_id: int) -> Review | None:
        row = self._conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        return _row_to_review(row) if row is not None else None

    def list(self, status: Status | None = None, limit: int = 20, offset: int = 0) -> list[Review]:
        query = "SELECT * FROM reviews"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status=?"
            params.append(status.value)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_review(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM reviews GROUP BY status"
        ).fetchall()
        result = {status.value: 0 for status in Status}
        for row in rows:
            result[row["status"]] = row["n"]
        result["total"] = sum(row["n"] for row in rows)
        return result

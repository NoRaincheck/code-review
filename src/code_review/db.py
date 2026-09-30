"""SQLite persistence for reviews.

Knows nothing about agents or ACP. One database per repository, at
``<repo>/.codereview/reviews.db``.

``reviews`` doubles as the queue: rows with ``status='queued'`` are pending work, so
there is no separate jobs table and a daemon restart resumes from the database.
"""

from __future__ import annotations

import json
import sqlite3
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY,
    sha           TEXT    NOT NULL,
    branch        TEXT,
    status        TEXT    NOT NULL,
    markdown      TEXT,
    error         TEXT,
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

SCHEMA_VERSION = "1"


class Status(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({Status.COMPLETED, Status.FAILED, Status.CANCELLED})


class DatabaseError(Exception):
    """A persistence operation failed."""


@dataclass(frozen=True, slots=True)
class Review:
    """One row of ``reviews``."""

    id: int
    sha: str
    branch: str | None
    status: Status
    markdown: str | None
    error: str | None
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


def _row_to_review(row: sqlite3.Row) -> Review:
    return Review(
        id=row["id"],
        sha=row["sha"],
        branch=row["branch"],
        status=Status(row["status"]),
        markdown=row["markdown"],
        error=row["error"],
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
    """Thin, synchronous SQLite wrapper. Not shared across threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        # A daemon can open many repositories, and a reference cycle through the
        # sqlite3.Connection keeps them from being collected promptly. Closing on
        # collection avoids leaking file descriptors and unraisable ResourceWarnings.
        self._finalizer = weakref.finalize(self, self._conn.close)

    @classmethod
    def for_repo(cls, repo: Path) -> Database:
        return cls(paths.db_path(repo))

    def close(self) -> None:
        if self._finalizer.alive:
            self._finalizer()
        else:
            self._conn.close()

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
    ) -> None:
        """Record a finished review.

        Partial output is still stored. A turn that stopped at ``max_tokens`` or
        ``refusal`` produced a truncated review, and the stop reason records that so the
        text is never presented as complete.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE reviews SET status=?, markdown=?, stop_reason=?, agent=?, "
                "agent_version=?, usage_json=?, tool_log_json=?, error=NULL, finished_at=? "
                "WHERE id=?",
                (
                    Status.COMPLETED.value,
                    markdown,
                    stop_reason,
                    agent,
                    agent_version,
                    json.dumps(usage) if usage else None,
                    json.dumps(tool_log) if tool_log else None,
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

    def cancel(self, review_id: int, markdown: str | None = None) -> None:
        """Mark a review cancelled, keeping any partial markdown already collected."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE reviews SET status=?, markdown=COALESCE(?, markdown), "
                "stop_reason='cancelled', finished_at=? WHERE id=?",
                (Status.CANCELLED.value, markdown, _now(), review_id),
            )

    # -- retrieval --------------------------------------------------------

    def get(self, sha: str) -> Review | None:
        row = self._conn.execute(
            "SELECT * FROM reviews WHERE sha=? OR sha LIKE ?", (sha, f"{sha}%")
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT * FROM reviews WHERE sha LIKE ? ORDER BY id DESC LIMIT 1", (f"{sha}%",)
            ).fetchone()
        return _row_to_review(row) if row is not None else None

    def get_by_id(self, review_id: int) -> Review | None:
        row = self._conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        return _row_to_review(row) if row is not None else None

    def latest(self) -> Review | None:
        row = self._conn.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT 1").fetchone()
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

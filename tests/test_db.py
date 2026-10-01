"""Database tests against real SQLite files, in tmp_path."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from code_review.db import Database, Status


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "reviews.db")
    yield db
    db.close()


def test_enqueue_creates_queued_row(database: Database) -> None:
    review_id, created = database.enqueue("a" * 40, "main")
    assert created is True
    review = database.get_by_id(review_id)
    assert review is not None
    assert review.status is Status.QUEUED
    assert review.branch == "main"
    assert review.markdown is None


def test_enqueue_is_idempotent_for_same_sha(database: Database) -> None:
    first, created_first = database.enqueue("a" * 40)
    second, created_second = database.enqueue("a" * 40)
    assert created_first is True
    assert created_second is False
    assert first == second
    assert database.counts()["total"] == 1


def test_force_requeues_and_clears_previous_result(database: Database) -> None:
    review_id, _ = database.enqueue("a" * 40)
    database.complete(review_id, markdown="old review", stop_reason="end_turn", agent="fake")

    same_id, created = database.enqueue("a" * 40, force=True)
    assert same_id == review_id
    assert created is True

    review = database.get_by_id(review_id)
    assert review is not None
    assert review.status is Status.QUEUED
    assert review.markdown is None
    assert review.finished_at is None


def test_claim_next_moves_to_running_in_order(database: Database) -> None:
    first, _ = database.enqueue("a" * 40)
    second, _ = database.enqueue("b" * 40)

    claimed = database.claim_next()
    assert claimed is not None
    assert claimed.id == first
    assert claimed.status is Status.RUNNING

    claimed = database.claim_next()
    assert claimed is not None
    assert claimed.id == second


def test_claim_next_returns_none_when_empty(database: Database) -> None:
    assert database.claim_next() is None


def test_claim_next_does_not_double_claim(database: Database) -> None:
    database.enqueue("a" * 40)
    assert database.claim_next() is not None
    assert database.claim_next() is None


def test_recover_interrupted_requeues_running_rows(database: Database) -> None:
    review_id, _ = database.enqueue("a" * 40)
    database.claim_next()

    recovered = database.recover_interrupted()
    assert recovered == 1
    review = database.get_by_id(review_id)
    assert review is not None
    assert review.status is Status.QUEUED
    assert review.started_at is None


def test_recover_interrupted_is_a_noop_when_nothing_running(database: Database) -> None:
    database.enqueue("a" * 40)
    assert database.recover_interrupted() == 0


def test_complete_stores_markdown_and_metadata(database: Database) -> None:
    review_id, _ = database.enqueue("a" * 40)
    database.complete(
        review_id,
        markdown="# Review\n\nFound a bug.",
        stop_reason="end_turn",
        agent="claude-agent-acp",
        agent_version="1.2.3",
        usage={"used": 100, "size": 200},
        tool_log=[{"tool_call_id": "c1", "decision": "allow"}],
    )
    review = database.get_by_id(review_id)
    assert review is not None
    assert review.status is Status.COMPLETED
    assert "Found a bug." in (review.markdown or "")
    assert review.agent == "claude-agent-acp"
    assert review.usage == {"used": 100, "size": 200}
    assert review.tool_log[0]["decision"] == "allow"
    assert review.finished_at is not None


def test_truncated_review_is_stored_with_its_stop_reason(database: Database) -> None:
    """A review cut short by max_tokens must not look identical to a complete one."""
    review_id, _ = database.enqueue("a" * 40)
    database.complete(
        review_id,
        markdown="Partial analysis of the first file",
        stop_reason="max_tokens",
        agent="fake",
    )
    review = database.get_by_id(review_id)
    assert review is not None
    assert review.status is Status.COMPLETED
    assert review.stop_reason == "max_tokens"


def test_fail_records_error(database: Database) -> None:
    review_id, _ = database.enqueue("a" * 40)
    database.fail(review_id, "agent command not found: 'nope'")
    review = database.get_by_id(review_id)
    assert review is not None
    assert review.status is Status.FAILED
    assert "not found" in (review.error or "")
    assert review.markdown is None


def test_cancel_keeps_partial_markdown(database: Database) -> None:
    review_id, _ = database.enqueue("a" * 40)
    database.cancel(review_id, "half a review")
    review = database.get_by_id(review_id)
    assert review is not None
    assert review.status is Status.CANCELLED
    assert review.markdown == "half a review"
    assert review.stop_reason == "cancelled"


def test_cancel_without_markdown_leaves_none(database: Database) -> None:
    review_id, _ = database.enqueue("a" * 40)
    database.cancel(review_id)
    review = database.get_by_id(review_id)
    assert review is not None
    assert review.markdown is None


def test_fail_error_is_truncated_to_a_sane_length(database: Database) -> None:
    review_id, _ = database.enqueue("a" * 40)
    database.fail(review_id, "x" * 10_000)
    review = database.get_by_id(review_id)
    assert review is not None
    assert len(review.error or "") <= 4000


def test_get_requires_an_exact_sha(database: Database) -> None:
    """get() must not silently prefix-match; see matches_prefix for abbreviations."""
    sha = "1234567890abcdef" * 2 + "12345678"
    database.enqueue(sha)
    assert database.get(sha) is not None
    assert database.get(sha[:8]) is None


def test_matches_prefix_finds_an_abbreviation(database: Database) -> None:
    sha = "1234567890abcdef" * 2 + "12345678"
    database.enqueue(sha)
    found = database.matches_prefix(sha[:8])
    assert [r.sha for r in found] == [sha]


def test_matches_prefix_returns_every_candidate(database: Database) -> None:
    """Ambiguity must be visible, so callers can refuse to guess."""
    database.enqueue("abcd111" + "0" * 33, "main")
    database.enqueue("abcd222" + "0" * 33, "main")
    assert len(database.matches_prefix("abcd")) == 2
    assert len(database.matches_prefix("abcd1")) == 1


def test_matches_prefix_ignores_short_prefixes(database: Database) -> None:
    """Below four characters a prefix is too weak to be worth matching."""
    database.enqueue("abcd111" + "0" * 33, "main")
    assert database.matches_prefix("abc") == []
    assert len(database.matches_prefix("abcd")) == 1


def test_matches_prefix_escapes_like_wildcards(database: Database) -> None:
    """A sha containing % must not match every review."""
    database.enqueue("a" * 39 + "%", "main")
    database.enqueue("b" * 40, "main")
    assert [r.sha for r in database.matches_prefix("a" * 39 + "%")] == ["a" * 39 + "%"]
    assert database.matches_prefix("%") == []


def test_get_returns_none_for_unknown_sha(database: Database) -> None:
    assert database.get("0" * 40) is None


def test_list_filters_by_status_and_limits(database: Database) -> None:
    for index in range(5):
        review_id, _ = database.enqueue(f"{index}" * 40)
        if index < 2:
            database.complete(review_id, markdown="ok", stop_reason="end_turn", agent="fake")

    completed = database.list(status=Status.COMPLETED)
    assert len(completed) == 2
    assert database.list(limit=3) != []
    assert len(database.list(limit=3)) == 3


def test_list_is_newest_first(database: Database) -> None:
    database.enqueue("a" * 40)
    database.enqueue("b" * 40)
    reviews = database.list()
    assert reviews[0].sha == "b" * 40


def test_counts_reports_every_status(database: Database) -> None:
    first, _ = database.enqueue("a" * 40)
    database.enqueue("b" * 40)
    database.complete(first, markdown="ok", stop_reason="end_turn", agent="fake")

    counts = database.counts()
    assert counts["completed"] == 1
    assert counts["queued"] == 1
    assert counts["total"] == 2
    assert counts["failed"] == 0


def test_database_creates_parent_directory(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested" / "deep" / "reviews.db")
    try:
        assert db.path.exists()
    finally:
        db.close()


def test_reopening_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "reviews.db"
    with Database(path) as first:
        review_id, _ = first.enqueue("a" * 40)
        first.complete(review_id, markdown="kept", stop_reason="end_turn", agent="fake")

    with Database(path) as second:
        review = second.get("a" * 40)
        assert review is not None
        assert review.markdown == "kept"


def test_schema_version_is_recorded(database: Database) -> None:
    row = database._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert row is not None
    assert row["value"] == "1"


def test_reopening_an_old_database_is_tolerated(tmp_path: Path) -> None:
    """A DB missing the newer columns should still open rather than crash the daemon."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, sha TEXT, status TEXT)")
        conn.execute("INSERT INTO reviews(sha, status) VALUES('a', 'queued')")
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    try:
        counts = db.counts()
        assert counts["queued"] == 1
    finally:
        db.close()


# -- concurrent access -----------------------------------------------------


def test_reading_works_while_another_connection_holds_the_write_lock(
    tmp_path: Path,
) -> None:
    """`show` and `list` must not block behind an in-flight review.

    The daemon holds the write lock for the whole review. If opening the database wrote to
    it, every read-only command would block for the busy timeout and then fail, which is
    the tool's primary command.
    """
    path = tmp_path / "reviews.db"
    with Database(path) as db:
        db.enqueue("a" * 40, "main")

    blocker = sqlite3.connect(path, isolation_level=None, timeout=1)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        blocker.execute(
            "INSERT INTO reviews(sha, status, created_at) VALUES(?, 'running', 'now')",
            ("b" * 40,),
        )

        started = time.monotonic()
        db = Database(path)
        try:
            assert time.monotonic() - started < 1.0, "opening a database took a write lock"
            # WAL readers see the last committed snapshot, so the blocker's uncommitted
            # row is invisible. The point is that the read proceeds at all.
            assert db.get("a" * 40) is not None
            assert len(db.list()) >= 1
        finally:
            db.close()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_writing_still_reports_a_locked_database(tmp_path: Path) -> None:
    """Reads tolerate a concurrent writer; writes must still surface the conflict."""
    path = tmp_path / "reviews.db"
    with Database(path) as db:
        db.enqueue("a" * 40, "main")

    blocker = sqlite3.connect(path, isolation_level=None, timeout=1)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        blocker.execute(
            "INSERT INTO reviews(sha, status, created_at) VALUES(?, 'running', 'now')",
            ("b" * 40,),
        )
        db = Database(path)
        db._conn.execute("PRAGMA busy_timeout=250")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.enqueue("c" * 40, "main")
        finally:
            db.close()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_close_is_safe_twice_and_after_a_failed_construction(tmp_path: Path) -> None:
    """A failed construction must not leave __del__ raising over the real error."""
    db = Database(tmp_path / "reviews.db")
    db.close()
    db.close()  # idempotent

    # Simulate a constructor that failed after the attribute was initialised.
    partial = Database.__new__(Database)
    partial.path = tmp_path / "never" / "reviews.db"
    partial._closer = None
    partial.close()  # must not raise


def test_a_failed_transaction_rolls_back_and_leaves_the_connection_usable(
    database: Database,
) -> None:
    """An exception mid-transaction must not leave the connection inside one.

    Without the rollback the next write on this connection fails with "cannot start a
    transaction within a transaction", and because the daemon caches one Database per
    repository, that would break every later review for that repo.
    """
    before = database.enqueue("a" * 40, "main")

    with pytest.raises(RuntimeError, match="deliberate"):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO reviews(sha, status, created_at) VALUES(?, 'queued', 'now')",
                ("b" * 40,),
            )
            raise RuntimeError("deliberate")

    assert database.get("b" * 40) is None, "the rolled-back row should be gone"

    # The connection still works, which is the actual point.
    after, created = database.enqueue("c" * 40, "main")
    assert created
    assert after != before
    assert database.get("c" * 40) is not None


def test_a_failed_enqueue_leaves_no_partial_row(database: Database) -> None:
    with pytest.raises(RuntimeError):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO reviews(sha, status, created_at) VALUES(?, 'queued', 'now')",
                ("d" * 40,),
            )
            raise RuntimeError("boom")
    assert database.counts()["total"] == 0


def test_schema_version_write_tolerates_a_locked_database(tmp_path: Path) -> None:
    """Recording the version is advisory; failing to must not stop the caller reading.

    This branch exists because a concurrent writer once made every read-only command fail
    with "database is locked", so the tolerance itself needs a test.
    """
    path = tmp_path / "reviews.db"
    with Database(path) as db:
        db.enqueue("a" * 40, "main")

    blocker = sqlite3.connect(path, isolation_level=None, timeout=1)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        blocker.execute(
            "INSERT INTO reviews(sha, status, created_at) VALUES(?, 'running', 'now')",
            ("b" * 40,),
        )
        db = Database(path)
        try:
            # Construction succeeded despite the lock, and reads still work.
            assert db.get("a" * 40) is not None
        finally:
            db.close()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    # Once the lock clears, the marker is written on a later open.
    with Database(path) as db:
        row = db._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert row is not None
        assert row["value"] == "1"


def test_schema_is_created_for_a_fresh_database(tmp_path: Path) -> None:
    path = tmp_path / "new.db"
    with Database(path) as db:
        db.enqueue("a" * 40, "main")

    conn = sqlite3.connect(path)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"reviews", "meta"} <= tables

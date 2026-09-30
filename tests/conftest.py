"""Shared fixtures. Real git repositories and real SQLite files, never mocks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from code_review.git import Commit


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An initialized git repository with one commit that changes a python file."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev Example")
    git(root, "config", "commit.gpgsign", "false")

    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(root, "add", "app.py")
    git(root, "commit", "-q", "-m", "Add add()\n\nThe first implementation.")
    return root


@pytest.fixture
def second_commit(repo: Path) -> str:
    """Add a second commit that introduces a genuine bug. Returns its sha."""
    (repo / "app.py").write_text(
        "def add(a, b):\n    return a - b\n\n\ndef div(a, b):\n    return a / b\n",
        encoding="utf-8",
    )
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "Fix sign error and add div()")
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def sample_commit() -> Commit:
    return Commit(
        sha="a" * 40,
        short_sha="aaaaaaa",
        parents=("b" * 40,),
        subject="Fix sign error",
        body="Adds div().",
        author_name="Dev Example",
        author_email="dev@example.com",
        authored_at="2026-10-01T12:00:00+00:00",
        diff="--- a/app.py\n+++ b/app.py\n@@\n-    return a + b\n+    return a - b\n",
    )


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CODEREVIEW_HOME so tests never touch the real ~/.codereview.

    Uses a short path because AF_UNIX paths are capped near 104 bytes on macOS, and
    pytest's tmp_path is long enough to blow that when the socket is nested under it.
    """
    import shutil
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="crh"))
    monkeypatch.setenv("CODEREVIEW_HOME", str(home))
    yield home
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def python_executable() -> str:
    return sys.executable

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "codereview"
STATE_DIR_NAME = ".codereview"
CONFIG_FILE_NAME = ".codereview.toml"
DB_FILE_NAME = "reviews.db"
GITIGNORE_LINE = f"{STATE_DIR_NAME}/"


def home_dir() -> Path:
    override = os.environ.get("CODEREVIEW_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / f".{APP_NAME}"


def socket_path() -> Path:
    return home_dir() / "daemon.sock"


def pid_path() -> Path:
    return home_dir() / "daemon.pid"


def daemon_log_path() -> Path:
    return home_dir() / "daemon.log"


def hook_log_path() -> Path:
    return home_dir() / "hook.log"


def state_dir(repo: Path) -> Path:
    return repo / STATE_DIR_NAME


def db_dir(git_common_dir: Path) -> Path:
    """Where the review database lives: inside the shared git directory.

    Shared across worktrees by construction, which is the point. A per-worktree path inside
    the checkout would mean the same commit reviewed once per worktree, and `show` in one
    worktree unable to see another's reviews.

    Living under `.git` also means no `.gitignore` entry is needed and nothing is written
    into the user's tracked tree.
    """
    return git_common_dir / STATE_DIR_NAME


def db_path(git_common_dir: Path) -> Path:
    return db_dir(git_common_dir) / DB_FILE_NAME


def config_path(repo: Path) -> Path:
    return repo / CONFIG_FILE_NAME


def ensure_home() -> Path:
    directory = home_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory

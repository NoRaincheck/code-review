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


def db_path(repo: Path) -> Path:
    return state_dir(repo) / DB_FILE_NAME


def config_path(repo: Path) -> Path:
    return repo / CONFIG_FILE_NAME


def ensure_home() -> Path:
    directory = home_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory

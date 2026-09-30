"""Typed configuration loaded from ``.codereview.toml``.

Pure module: no I/O beyond reading the config file itself, so it is testable by
constructing a ``Config`` directly or by pointing ``load_config`` at a temp file.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

DEFAULT_MAX_DIFF_BYTES = 200_000
DEFAULT_TIMEOUT_SECONDS = 300


class ConfigError(Exception):
    """Raised when config is missing or malformed. Never raised during a commit hook."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """How to launch an ACP agent.

    ``command`` plus ``args`` plus ``env`` is the full argv and environment. Kept as
    explicit fields rather than a pre-joined list so ``spawn_agent_process`` can be
    called without re-splitting a shell string.
    """

    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)

    @property
    def argv(self) -> list[str]:
        return [self.command, *self.args]

    @property
    def label(self) -> str:
        return self.command


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved configuration for reviewing one repository."""

    agent: AgentConfig | None = None
    review_guidelines: str = ""
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES
    review_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    deny_unknown_commands: bool = True
    source_path: Path | None = None

    def require_agent(self) -> AgentConfig:
        if self.agent is None:
            where = str(self.source_path or paths.config_path(Path.cwd()))
            raise ConfigError(
                f"no review agent configured. Set review_agent in {where}, for example:\n"
                '  review_agent = ["claude-agent-acp"]'
            )
        return self.agent


def _parse_agent(raw: object) -> AgentConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ConfigError('review_agent must be a non-empty array of strings, e.g. ["codex-acp"]')
    if not all(isinstance(part, str) for part in raw):
        raise ConfigError("review_agent entries must all be strings")
    command, *args = raw
    return AgentConfig(command=command, args=tuple(args))


def _parse_env(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("review_agent_env must be a table of string values")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise ConfigError(f"review_agent_env.{key} must be a string")
        result[key] = value
    return result


def _parse_int(raw: object, key: str, default: int) -> int:
    if raw is None:
        return default
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ConfigError(f"{key} must be a positive integer")
    return raw


def _parse_bool(raw: object, key: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ConfigError(f"{key} must be true or false")
    return raw


def load_config(repo: Path) -> Config:
    """Read ``.codereview.toml`` from *repo*. A missing file yields all defaults."""
    path = paths.config_path(repo)
    if not path.exists():
        return Config()

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    agent = _parse_agent(data.get("review_agent"))
    extra_args = data.get("review_agent_args")
    if extra_args is not None:
        if not isinstance(extra_args, list) or not all(isinstance(a, str) for a in extra_args):
            raise ConfigError("review_agent_args must be an array of strings")
        agent_env = _parse_env(data.get("review_agent_env"))
        if agent is not None:
            agent = AgentConfig(
                command=agent.command,
                args=(*agent.args, *extra_args),
                env=agent_env,
            )
    elif agent is not None:
        agent = AgentConfig(
            command=agent.command,
            args=agent.args,
            env=_parse_env(data.get("review_agent_env")),
        )

    guidelines = data.get("review_guidelines", "")
    if not isinstance(guidelines, str):
        raise ConfigError("review_guidelines must be a string")

    return Config(
        agent=agent,
        review_guidelines=guidelines.strip(),
        max_diff_bytes=_parse_int(
            data.get("max_diff_bytes"), "max_diff_bytes", DEFAULT_MAX_DIFF_BYTES
        ),
        review_timeout_seconds=_parse_int(
            data.get("review_timeout_seconds"), "review_timeout_seconds", DEFAULT_TIMEOUT_SECONDS
        ),
        deny_unknown_commands=_parse_bool(
            data.get("deny_unknown_commands"), "deny_unknown_commands", True
        ),
        source_path=path,
    )

"""Drives a single ACP review turn. The only module that imports the ACP SDK.

One review equals one agent process: spawn, initialize, new session, one prompt,
collect markdown, tear down. Total isolation means a hung or crashed agent can never
affect the next review, at the cost of a few seconds of process startup per commit.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import ClientCapabilities, FileSystemCapabilities, Implementation

from .. import policy, worktree
from .. import prompt as prompt_builder
from ..config import AgentConfig, Config
from ..git import Commit
from .policy_client import ReviewClient, ReviewClientConfig

log = logging.getLogger(__name__)

# Matches the ACP release we verified the identifiers in. An unpinned install would let
# a schema change walk us into silently-broken attribute paths.
PINNED_SDK = "agent-client-protocol==0.12.1"


class ReviewError(Exception):
    """A review could not be completed. The message is stored on the review row."""


@dataclass(slots=True)
class ReviewOutcome:
    """The result of one review attempt."""

    markdown: str
    stop_reason: str
    agent: str
    agent_version: str | None = None
    usage: dict[str, Any] | None = None
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    warning: str | None = None

    @property
    def is_complete(self) -> bool:
        """True only when the agent finished its turn normally."""
        return self.stop_reason == "end_turn"


def _client_capabilities() -> ClientCapabilities:
    """Advertise only what we are willing to serve.

    ``read_text_file`` is offered so the agent can inspect repository files through our
    path checks. ``write_text_file`` and ``terminal`` are withheld; the client still raises
    on those methods, because an agent ignoring the advertisement must not gain the ability.
    """
    return ClientCapabilities(
        fs=FileSystemCapabilities(read_text_file=True, write_text_file=False),
        terminal=False,
    )


#: Stands in for the throwaway worktree path in anything we persist.
WORKTREE_PLACEHOLDER = "<worktree>"


def _assess(markdown: str, commit: Commit) -> str | None:
    """The one thing worth saying about a finished review, or None if nothing is.

    Checked in the order that matters. A commit with no diff is reported first, because in that
    case the agent's verdict is about nothing at all: an empty commit, or a merge whose changes
    are already present, produces a "No issues found." that is indistinguishable from a genuine
    clean review of real code. Since ``post-commit`` fires on every commit, a repository with
    occasional empty commits would accumulate verdicts that look like real coverage and are not.
    """
    if not commit.diff.strip():
        return (
            "this commit has no diff to review, so the verdict is about nothing; "
            "it was an empty commit or a merge that introduced no changes"
        )
    return prompt_builder.missing_verdict(markdown)


def _with_root_rewritten(tool_log: list[dict[str, Any]], root: Path) -> list[dict[str, Any]]:
    """Replace the worktree's absolute path in tool titles with a stable placeholder.

    The agent is given the worktree, so every command it runs mentions a temporary directory
    whose name carries our pid and changes every run. Stored verbatim that is noise in a log
    nobody can act on. Rewriting it keeps the log useful and comparable between reviews, and
    avoids persisting a path from a directory that no longer exists.

    Rewrites the title only, which is the human-readable part. Anything else in the log is
    passed through untouched rather than pattern-matched, so a change in the agent's payload
    shape cannot quietly lose data.
    """
    resolved = str(root.resolve())
    raw = str(root)
    rewritten = []
    for entry in tool_log:
        title = entry.get("title")
        if not isinstance(title, str):
            rewritten.append(entry)
            continue
        updated = dict(entry)
        updated["title"] = title.replace(resolved, WORKTREE_PLACEHOLDER).replace(
            raw, WORKTREE_PLACEHOLDER
        )
        rewritten.append(updated)
    return rewritten


def _build_env(agent: AgentConfig) -> dict[str, str]:
    env = dict(os.environ)
    env.update(agent.env)
    return env


class ReviewRunner:
    """Runs reviews against a configured ACP agent."""

    def __init__(
        self,
        config: Config,
        repo: Path,
        *,
        timeout_seconds: int | None = None,
        client_info: Implementation | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.timeout_seconds = timeout_seconds or config.review_timeout_seconds
        self.client_info = client_info or Implementation(name="code-review", version=_version())
        # Retained so a timed-out review can still salvage whatever streamed before the
        # deadline. ``review`` replaces it on each attempt.
        self._client: ReviewClient | None = None
        self._outcome: ReviewOutcome | None = None

    @property
    def last_outcome(self) -> ReviewOutcome | None:
        """The result of the most recent successful attempt, or None."""
        return self._outcome

    @property
    def partial_markdown(self) -> str | None:
        """Markdown collected so far, or None if no review has been attempted."""
        return self._client.markdown() if self._client is not None else None

    @property
    def partial_tool_log(self) -> list[dict[str, Any]]:
        return self._client.tool_log() if self._client is not None else []

    async def review(self, commit: Commit) -> ReviewOutcome:
        agent = self.config.require_agent()
        prompt_text = prompt_builder.build_prompt(commit, self.config.review_guidelines)

        # The agent's root is a throwaway worktree at the reviewed commit, never the user's
        # checkout. What it reads is then the code the review is actually about, and anything
        # it stages is deleted with the directory rather than left in the user's index. The
        # cost is a registration under the shared git directory, which is removed again in the
        # same breath; worktree.py spells out that trade in full.
        with worktree.isolated(self.repo, commit.sha) as root:
            client = ReviewClient(
                config=ReviewClientConfig(
                    repo=root,
                    deny_unknown_commands=self.config.deny_unknown_commands,
                )
            )
            self._client = client

            try:
                async with spawn_agent_process(
                    client,
                    agent.command,
                    *agent.args,
                    env=_build_env(agent),
                    cwd=str(root),
                ) as (conn, _process):
                    await conn.initialize(
                        protocol_version=PROTOCOL_VERSION,
                        client_capabilities=_client_capabilities(),
                        client_info=self.client_info,
                    )
                    session = await conn.new_session(cwd=str(root), mcp_servers=[])

                    stop_reason, response_usage = await self._prompt_with_timeout(
                        conn, session.session_id, prompt_text
                    )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # Deliberately not wrapped. Callers distinguish a timeout from a failure in
                # order to keep whatever markdown streamed before the deadline, and wrapping
                # this in ReviewError would discard it and mislabel the review as broken.
                raise
            except FileNotFoundError as exc:
                raise ReviewError(
                    f"agent command not found: {agent.command!r}. "
                    f"Install it or set review_agent in .codereview.toml."
                ) from exc
            except PermissionError as exc:
                raise ReviewError(f"agent attempted a forbidden operation: {exc}") from exc
            except Exception as exc:
                raise ReviewError(f"{type(exc).__name__}: {exc}") from exc

            markdown = client.markdown()
            if not markdown:
                raise ReviewError(
                    f"agent produced no output (stop_reason={stop_reason}). "
                    f"It may have refused, or may not support ACP properly."
                )

            usage = client.usage or response_usage
            self._outcome = ReviewOutcome(
                markdown=markdown,
                stop_reason=stop_reason,
                agent=agent.label,
                usage=usage,
                tool_log=_with_root_rewritten(client.tool_log(), root),
                warning=_assess(markdown, commit),
            )

        return self._outcome

    async def _prompt_with_timeout(
        self, conn: Any, session_id: str, prompt_text: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Send the prompt under a wall-clock budget, cancelling cleanly on expiry.

        ``asyncio.wait_for`` is used rather than the SDK's ``receive_timeout``, which is a
        connection-wide read-idle backstop and cannot express "300s for this prompt, 30s
        for initialize". On timeout we cancel the turn but keep the partial markdown, which
        is often a usable review of the files the agent got to before the deadline.
        """
        try:
            response = await asyncio.wait_for(
                conn.prompt(session_id=session_id, prompt=[text_block(prompt_text)]),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            log.warning("review exceeded %ss; cancelling", self.timeout_seconds)
            try:
                await conn.cancel(session_id)
            except Exception:
                log.debug("cancel after timeout failed", exc_info=True)
            raise TimeoutError(
                f"review exceeded {self.timeout_seconds}s and was cancelled"
            ) from None

        stop_reason = getattr(response, "stop_reason", None) or "end_turn"
        usage = getattr(response, "usage", None)
        return stop_reason, _serialize_usage(usage) if usage is not None else None


def _serialize_usage(usage: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("total_tokens", "input_tokens", "output_tokens"):
        value = getattr(usage, name, None)
        if value is not None:
            result[name] = value
    return result


def _version() -> str:
    from .. import __version__

    return __version__


async def run_review(
    config: Config,
    repo: Path,
    commit: Commit,
    *,
    timeout_seconds: int | None = None,
) -> ReviewOutcome:
    """Convenience wrapper around :class:`ReviewRunner`."""
    return await ReviewRunner(config, repo, timeout_seconds=timeout_seconds).review(commit)


__all__ = [
    "PINNED_SDK",
    "ReviewError",
    "ReviewOutcome",
    "ReviewRunner",
    "policy",
    "run_review",
]

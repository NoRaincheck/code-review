"""A protocol-level probe of an agent's ACP support.

``code-review doctor`` should answer "does this command actually speak ACP?" rather than
telling the user to go and find out. That can be established without contacting a model:
``initialize`` and ``session/new`` are pure handshake, so a probe costs no tokens and needs
no credentials beyond whatever the agent requires at startup.

Two questions are kept apart, because they fail for different reasons and call for different
fixes:

1. Does the command speak ACP at all? Answered by ``initialize``.
2. Can it start a session and review? Answered by ``session/new``.

``@agentclientprotocol/codex-acp`` answers ``initialize`` correctly and then refuses
``session/new`` with "Authentication required" when nobody has logged in. That is a working
argv for an ACP agent, so reporting it as "this is not an ACP agent" would send the user
looking in the wrong place.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, spawn_agent_process
from acp.schema import ClientCapabilities, FileSystemCapabilities, Implementation

from ..config import AgentConfig
from .policy_client import ReviewClient, ReviewClientConfig

DEFAULT_PROBE_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of one handshake attempt."""

    speaks_acp: bool
    detail: str
    can_review: bool = False
    agent_name: str | None = None
    agent_version: str | None = None
    auth_methods: tuple[str, ...] = ()
    session_error: str | None = None

    @property
    def ok(self) -> bool:
        """True when a review could run. False includes 'speaks ACP but not logged in'."""
        return self.speaks_acp and self.can_review


async def probe_agent(
    agent: AgentConfig,
    cwd: Path | None = None,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> ProbeResult:
    """Spawn *agent*, complete the ACP handshake, and report what came back.

    Never raises: a failure is a result, because the failure text is what the caller needs.

    Reuses the review client rather than a bespoke one. No prompt is sent, so its permission
    policy and output accumulation never come into play.
    """
    repo = cwd or Path.cwd()
    client = ReviewClient(config=ReviewClientConfig(repo=repo))
    env = dict(os.environ)
    env.update(agent.env)

    try:
        async with spawn_agent_process(
            client, agent.command, *agent.args, env=env, cwd=str(repo)
        ) as (conn, _process):
            try:
                response = await asyncio.wait_for(
                    conn.initialize(
                        protocol_version=PROTOCOL_VERSION,
                        client_capabilities=ClientCapabilities(
                            fs=FileSystemCapabilities(read_text_file=True, write_text_file=False),
                            terminal=False,
                        ),
                        client_info=Implementation(name="code-review", version=_version()),
                    ),
                    timeout=timeout,
                )
            except TimeoutError:
                return ProbeResult(
                    speaks_acp=False,
                    detail=(
                        f"no ACP response within {timeout:g}s. Is {agent.command!r} an ACP agent?"
                    ),
                )
            except FileNotFoundError:
                return ProbeResult(speaks_acp=False, detail=f"{agent.command!r} not found on PATH")
            except Exception as exc:  # noqa: BLE001 - the failure text is the deliverable
                return ProbeResult(speaks_acp=False, detail=f"{type(exc).__name__}: {exc}")

            # Reaching here means the agent answered initialize, so it speaks ACP.
            name, version = _agent_identity(response)
            methods = _auth_methods(response)
            identity = name or "unnamed agent"
            if version:
                identity += f" {version}"

            try:
                await asyncio.wait_for(
                    conn.new_session(cwd=str(repo), mcp_servers=[]), timeout=timeout
                )
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                return ProbeResult(
                    speaks_acp=True,
                    detail=f"speaks ACP (reports as {identity}) but cannot start a session",
                    can_review=False,
                    agent_name=name,
                    agent_version=version,
                    auth_methods=methods,
                    session_error=f"{type(exc).__name__}: {exc}",
                )

            return ProbeResult(
                speaks_acp=True,
                detail=f"ready for reviews ({identity})",
                can_review=True,
                agent_name=name,
                agent_version=version,
                auth_methods=methods,
            )
    except TimeoutError:
        return ProbeResult(
            speaks_acp=False,
            detail=f"no ACP response within {timeout:g}s. Is {agent.command!r} an ACP agent?",
        )
    except FileNotFoundError:
        return ProbeResult(speaks_acp=False, detail=f"{agent.command!r} not found on PATH")
    except Exception as exc:  # noqa: BLE001 - the failure text is the deliverable
        return ProbeResult(speaks_acp=False, detail=f"{type(exc).__name__}: {exc}")


def _agent_identity(response: Any) -> tuple[str | None, str | None]:
    """Read the agent's self-reported name and version out of the initialize response."""
    info = getattr(response, "agent_info", None)
    if info is None:
        return None, None
    name = getattr(info, "name", None)
    version = getattr(info, "version", None)
    return (
        name if isinstance(name, str) else None,
        version if isinstance(version, str) else None,
    )


def _auth_methods(response: Any) -> tuple[str, ...]:
    """Auth method ids the agent advertises. Advertised is not the same as required."""
    return tuple(
        method_id
        for method_id in (
            str(getattr(method, "id", "") or getattr(method, "type", ""))
            for method in (getattr(response, "auth_methods", None) or [])
        )
        if method_id
    )


def describe(result: ProbeResult) -> list[str]:
    """Render a probe result as aligned doctor output lines."""
    if not result.speaks_acp:
        return [f"acp probe:   FAILED - {result.detail}"]

    lines = [f"acp probe:   {'OK' if result.can_review else 'PARTIAL'} - {result.detail}"]

    if result.auth_methods:
        lines.append(
            f"              offers auth via {', '.join(result.auth_methods)}; "
            f"log in by running the agent directly"
        )
    if not result.can_review and result.session_error:
        lines.append(
            f"              session/new said: {result.session_error}. "
            f"A review cannot run until this is resolved."
        )
    return lines


def _version() -> str:
    from .. import __version__

    return __version__


__all__ = ["DEFAULT_PROBE_TIMEOUT", "ProbeResult", "describe", "probe_agent"]

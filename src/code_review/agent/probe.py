"""A protocol-level probe of an agent's ACP support.

``code-review doctor`` should answer "does this command actually speak ACP?" rather than
telling the user to go and find out. That can be established without contacting a model:
``initialize`` and ``session/new`` are pure handshake, so a probe costs no tokens and needs
no credentials beyond whatever the agent requires at startup.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, spawn_agent_process
from acp.schema import ClientCapabilities, FileSystemCapabilities, Implementation

from ..config import AgentConfig
from .policy_client import ReviewClient, ReviewClientConfig

log = logging.getLogger(__name__)

DEFAULT_PROBE_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of one handshake attempt."""

    ok: bool
    detail: str
    agent_name: str | None = None
    agent_version: str | None = None
    auth_methods: tuple[str, ...] = ()


async def probe_agent(
    agent: AgentConfig,
    cwd: Path | None = None,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> ProbeResult:
    """Spawn *agent*, complete the ACP handshake, and report what came back.

    Never raises: a failure is a result, because "this command is not an ACP agent" is
    exactly what the caller needs to be told.

    Reuses the review client rather than a bespoke one. No prompt is sent, so its
    permission policy and output accumulation never come into play.
    """
    repo = cwd or Path.cwd()
    client = ReviewClient(config=ReviewClientConfig(repo=repo))
    env = dict(os.environ)
    env.update(agent.env)

    try:
        async with spawn_agent_process(
            client, agent.command, *agent.args, env=env, cwd=str(repo)
        ) as (conn, _process):
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

            # session/new proves the agent actually serves the protocol rather than
            # answering initialize alone.
            await asyncio.wait_for(conn.new_session(cwd=str(repo), mcp_servers=[]), timeout=timeout)

            name, version = _agent_identity(response)
            # Advertised, not required: an agent that lists auth methods may still start
            # without them if it is already authenticated.
            methods = tuple(
                str(getattr(method, "id", "") or getattr(method, "type", ""))
                for method in (getattr(response, "auth_methods", None) or [])
            )
            return ProbeResult(
                ok=True,
                detail=f"handshake ok ({' '.join(agent.argv)})",
                agent_name=name,
                agent_version=version,
                auth_methods=tuple(m for m in methods if m),
            )
    except TimeoutError:
        return ProbeResult(
            ok=False,
            detail=f"no ACP response within {timeout:g}s. Is {agent.command!r} an ACP agent?",
        )
    except FileNotFoundError:
        return ProbeResult(ok=False, detail=f"{agent.command!r} not found on PATH")
    except Exception as exc:  # noqa: BLE001 - the failure text is the deliverable
        return ProbeResult(ok=False, detail=f"{type(exc).__name__}: {exc}")


def _agent_identity(response: Any) -> tuple[str | None, str | None]:
    """Read the agent's self-reported name and version out of the initialize response."""
    info = getattr(response, "agent_info", None)
    if info is None:
        return None, None
    name = getattr(info, "name", None)
    version = getattr(info, "version", None)
    return (name if isinstance(name, str) else None, version if isinstance(version, str) else None)


def _version() -> str:
    from .. import __version__

    return __version__


def describe(result: ProbeResult) -> list[str]:
    """Render a probe result as aligned doctor output lines."""
    lines = [f"acp probe:   {'OK' if result.ok else 'FAILED'} - {result.detail}"]
    if result.ok:
        identity = result.agent_name or "unnamed agent"
        if result.agent_version:
            identity += f" {result.agent_version}"
        lines.append(f"              reports as {identity}")
        if result.auth_methods:
            lines.append(
                f"              offers auth via {', '.join(result.auth_methods)} "
                f"(run the agent directly to log in)"
            )
    return lines


__all__ = ["DEFAULT_PROBE_TIMEOUT", "ProbeResult", "describe", "probe_agent"]

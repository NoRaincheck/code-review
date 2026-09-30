"""Tests for the ACP handshake probe behind ``code-review doctor --probe``.

The probe must answer "does this command speak ACP?" and "can it review?" separately,
because they fail for different reasons. It runs against the scripted fake agent rather
than a vendor CLI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from code_review.agent.probe import ProbeResult, describe, probe_agent
from code_review.config import AgentConfig

FAKE_AGENT = str(Path(__file__).parent / "fake_agent.py")


def write_script(tmp_path: Path, script: dict[str, object]) -> str:
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    return str(path)


def fake_agent(
    script: dict[str, object] | None = None, tmp_path: Path | None = None
) -> AgentConfig:
    args = [FAKE_AGENT]
    if script is not None and tmp_path is not None:
        args.append(write_script(tmp_path, script))
    return AgentConfig(command=sys.executable, args=args)


async def test_probe_confirms_a_working_agent(repo: Path, tmp_path: Path) -> None:
    result = await probe_agent(fake_agent({}, tmp_path), repo, timeout=30)
    assert result.speaks_acp, result.detail
    assert result.can_review
    assert result.ok
    assert result.agent_name == "fake-agent"
    assert result.agent_version == "9.9.9"
    assert result.session_error is None


async def test_probe_separates_speaking_acp_from_being_able_to_review(
    repo: Path, tmp_path: Path
) -> None:
    """An agent that handshakes but refuses a session is PARTIAL, not a config error.

    This is the codex-acp case: correct argv, speaks the protocol, nobody has logged in.
    Reporting it as "not an ACP agent" would send the user looking in the wrong place.
    """
    result = await probe_agent(
        fake_agent({"fail_session": "Authentication required"}, tmp_path), repo, timeout=30
    )
    assert result.speaks_acp, result.detail
    assert not result.can_review
    assert not result.ok
    assert result.session_error is not None
    assert "Authentication required" in result.session_error


async def test_probe_reports_a_missing_binary(repo: Path) -> None:
    result = await probe_agent(AgentConfig(command="/nonexistent/agent"), repo, timeout=5)
    assert not result.speaks_acp
    assert "not found" in result.detail


async def test_probe_rejects_a_command_that_exits_immediately(repo: Path) -> None:
    """/bin/echo exits before answering, which is what a non-ACP CLI looks like."""
    result = await probe_agent(AgentConfig(command="/bin/echo", args=("hi",)), repo, timeout=10)
    assert not result.speaks_acp


async def test_probe_rejects_a_command_that_answers_the_wrong_protocol(repo: Path) -> None:
    """cat stays alive but has no ACP methods, so the error names the real problem."""
    result = await probe_agent(AgentConfig(command="/bin/cat"), repo, timeout=10)
    assert not result.speaks_acp
    assert "Method not found" in result.detail or "not found" in result.detail.lower()


async def test_probe_rejects_git_which_is_not_an_acp_agent(repo: Path) -> None:
    result = await probe_agent(AgentConfig(command="git", args=("--version",)), repo, timeout=10)
    assert not result.speaks_acp


async def test_probe_never_raises_on_any_failure(repo: Path) -> None:
    """A probe failure is a result the caller renders, never an exception."""
    for agent in (
        AgentConfig(command="/nonexistent/agent"),
        AgentConfig(command="/bin/cat"),
        AgentConfig(command="/bin/echo", args=("x",)),
    ):
        result = await probe_agent(agent, repo, timeout=10)
        assert result.speaks_acp is False


def test_describe_renders_a_working_agent() -> None:
    result = ProbeResult(
        speaks_acp=True,
        detail="ready for reviews (A 1)",
        can_review=True,
        agent_name="A",
        agent_version="1",
    )
    lines = describe(result)
    assert any("acp probe:   OK" in line for line in lines)
    assert any("ready for reviews" in line for line in lines)


def test_describe_renders_partial_separately_from_failed() -> None:
    lines = describe(
        ProbeResult(
            speaks_acp=True,
            detail="speaks ACP but cannot start a session",
            can_review=False,
            session_error="RequestError: Authentication required",
        )
    )
    assert any("PARTIAL" in line for line in lines), lines
    assert not any("FAILED" in line for line in lines), lines
    assert any("Authentication required" in line for line in lines)


def test_describe_offers_auth_as_optional_not_required() -> None:
    lines = describe(
        ProbeResult(
            speaks_acp=True,
            detail="ready for reviews (A)",
            can_review=True,
            auth_methods=("login",),
        )
    )
    assert any("offers auth via login" in line for line in lines)
    assert not any("requires" in line for line in lines)


def test_describe_renders_failure() -> None:
    lines = describe(ProbeResult(speaks_acp=False, detail="nope"))
    assert any("FAILED" in line and "nope" in line for line in lines)

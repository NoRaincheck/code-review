"""Tests for the ACP handshake probe behind ``code-review doctor --probe``.

The probe must answer "does this command actually speak ACP?" without contacting a model,
so it runs against the scripted fake agent rather than a vendor CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

from code_review.agent.probe import describe, probe_agent
from code_review.config import AgentConfig

FAKE_AGENT = str(Path(__file__).parent / "fake_agent.py")


def fake_agent(script_path: str | None = None) -> AgentConfig:
    args = [FAKE_AGENT]
    if script_path:
        args.append(script_path)
    return AgentConfig(command=sys.executable, args=args)


async def test_probe_succeeds_against_an_acp_agent(repo: Path, tmp_path: Path) -> None:
    script = tmp_path / "agent.json"
    script.write_text("{}", encoding="utf-8")

    result = await probe_agent(fake_agent(str(script)), repo, timeout=30)
    assert result.ok, result.detail
    assert result.agent_name == "fake-agent"
    assert result.agent_version == "9.9.9"


async def test_probe_reports_a_missing_binary(repo: Path) -> None:
    result = await probe_agent(AgentConfig(command="/nonexistent/agent"), repo, timeout=5)
    assert not result.ok
    assert "not found" in result.detail


async def test_probe_rejects_a_command_that_exits_immediately(repo: Path) -> None:
    """/bin/echo exits before answering, which is what a non-ACP CLI looks like."""
    result = await probe_agent(AgentConfig(command="/bin/echo", args=("hi",)), repo, timeout=10)
    assert not result.ok


async def test_probe_rejects_a_command_that_answers_the_wrong_protocol(repo: Path) -> None:
    """cat stays alive but has no ACP methods, so the error names the real problem."""
    result = await probe_agent(AgentConfig(command="/bin/cat"), repo, timeout=10)
    assert not result.ok
    assert "Method not found" in result.detail or "not found" in result.detail.lower()


async def test_probe_rejects_git_which_is_not_an_acp_agent(repo: Path) -> None:
    result = await probe_agent(AgentConfig(command="git", args=("--version",)), repo, timeout=10)
    assert not result.ok


async def test_probe_never_raises_on_any_failure(repo: Path) -> None:
    """A probe failure is a result the caller renders, never an exception."""
    for agent in (
        AgentConfig(command="/nonexistent/agent"),
        AgentConfig(command="/bin/cat"),
        AgentConfig(command="/bin/echo", args=("x",)),
    ):
        result = await probe_agent(agent, repo, timeout=10)
        assert result.ok is False


def test_describe_renders_success() -> None:
    from code_review.agent.probe import ProbeResult

    lines = describe(ProbeResult(ok=True, detail="handshake ok", agent_name="A", agent_version="1"))
    assert any("OK" in line for line in lines)
    assert any("reports as A 1" in line for line in lines)


def test_describe_renders_auth_methods_as_offers_not_requirements() -> None:
    from code_review.agent.probe import ProbeResult

    lines = describe(ProbeResult(ok=True, detail="ok", agent_name="A", auth_methods=("login",)))
    assert any("offers auth via login" in line for line in lines)
    assert not any("requires" in line for line in lines)


def test_describe_renders_failure() -> None:
    from code_review.agent.probe import ProbeResult

    lines = describe(ProbeResult(ok=False, detail="nope"))
    assert any("FAILED" in line and "nope" in line for line in lines)

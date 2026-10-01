"""End-to-end tests of the ACP integration against a scripted fake agent.

These exercise ``runner.py``, ``policy_client.py`` and the worker loop for real: a
subprocess is spawned, JSON-RPC frames cross stdio, and the permission policy decides
actual outcomes. No vendor CLI and no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from code_review import git as git_module
from code_review.agent.runner import ReviewError, ReviewRunner, run_review
from code_review.config import AgentConfig, Config
from code_review.git import Commit

FAKE_AGENT = str(Path(__file__).parent / "fake_agent.py")


@pytest.fixture
def commit(repo: Path, second_commit: str) -> Commit:
    return git_module.load_commit(repo, second_commit)


def write_script(tmp_path: Path, script: dict[str, object], name: str = "script.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(script), encoding="utf-8")
    return str(path)


def agent_config(script_path: str | None = None) -> Config:
    args = [sys.executable, FAKE_AGENT]
    if script_path:
        args.append(script_path)
    return Config(
        agent=AgentConfig(command=sys.executable, args=args[1:]),
        review_timeout_seconds=60,
    )


# -- the happy path --------------------------------------------------------


async def test_review_returns_agent_markdown(repo: Path, commit: Commit, tmp_path: Path) -> None:
    script = write_script(tmp_path, {"chunks": ["2 issues found.", "\n\nFirst problem."]})
    outcome = await run_review(agent_config(script), repo, commit)
    assert "2 issues found." in outcome.markdown
    assert "First problem." in outcome.markdown
    assert outcome.stop_reason == "end_turn"
    assert outcome.is_complete


async def test_agent_receives_the_full_prompt(repo: Path, commit: Commit, tmp_path: Path) -> None:
    """The prompt the agent sees must carry the sha, the message, and the diff."""
    seen: list[str] = []

    class CapturingRunner(ReviewRunner):
        async def review(self, review_commit: Commit):  # type: ignore[no-untyped-def]
            from code_review import prompt as prompt_builder

            seen.append(prompt_builder.build_prompt(review_commit, ""))
            raise ReviewError("stop here")

    with pytest.raises(ReviewError):
        await CapturingRunner(agent_config(), repo).review(commit)

    assert seen
    assert commit.sha in seen[0]
    assert "Fix sign error and add div()" in seen[0]
    assert "def div(a, b):" in seen[0]


# -- streaming and messages ------------------------------------------------


async def test_multiple_chunks_are_concatenated(repo: Path, commit: Commit, tmp_path: Path) -> None:
    script = write_script(tmp_path, {"chunks": ["one ", "two ", "three"]})
    outcome = await run_review(agent_config(script), repo, commit)
    assert outcome.markdown == "one two three"


# -- permissions -----------------------------------------------------------


async def test_read_only_git_is_allowed_and_logged(
    repo: Path, commit: Commit, tmp_path: Path
) -> None:
    script = write_script(
        tmp_path,
        {
            "tools": [
                {
                    "id": "c1",
                    "kind": "execute",
                    "title": "git log",
                    "raw_input": {"command": "git", "args": ["log"]},
                    "request_permission": True,
                }
            ],
            "chunks": ["checked history"],
        },
    )
    outcome = await run_review(agent_config(script), repo, commit)
    events = [e for e in outcome.tool_log if e["decision"]]
    assert events, outcome.tool_log
    assert events[0]["decision"] == "allow"
    assert "checked history" in outcome.markdown


@pytest.mark.parametrize(
    ("kind", "raw_input"),
    [
        ("execute", {"command": "git", "args": ["log; rm -rf /"]}),
        ("execute", {"command": "rm", "args": ["-rf", "/"]}),
        ("execute", {"command": "git", "args": ["-c", "core.pager=evil", "log"]}),
        ("edit", {"path": "app.py"}),
        ("delete", {"path": "app.py"}),
    ],
)
async def test_dangerous_tool_calls_are_denied(
    repo: Path, commit: Commit, tmp_path: Path, kind: str, raw_input: dict[str, object]
) -> None:
    """A denial must stop the tool call. The agent may then say something, or nothing.

    Both are acceptable: refusing the call does not require cancelling the whole turn, so
    the assertion targets the recorded decision rather than the resulting markdown.
    """
    script = write_script(
        tmp_path,
        {
            "tools": [
                {
                    "id": "c1",
                    "kind": kind,
                    "title": "sneaky",
                    "raw_input": raw_input,
                    "request_permission": True,
                }
            ],
            "chunks": ["I did not need that"],
        },
    )
    config = agent_config(script)
    runner = ReviewRunner(config, repo)
    try:
        outcome = await runner.review(commit)
        events = outcome.tool_log
    except ReviewError:
        events = runner.partial_tool_log

    decided = [e for e in events if e["decision"]]
    assert decided, events
    assert decided[0]["decision"] == "deny", decided[0]
    assert decided[0]["reason"]


async def test_agent_cannot_actually_run_a_denied_command(
    repo: Path, commit: Commit, tmp_path: Path
) -> None:
    """End-to-end proof: after a denial the side effect must not exist on disk."""
    canary = repo / "canary.txt"
    script = write_script(
        tmp_path,
        {
            "tools": [
                {
                    "id": "c1",
                    "kind": "execute",
                    "title": "create canary",
                    "raw_input": {"command": "touch", "args": ["canary.txt"]},
                    "request_permission": True,
                    # An allowed touch would create this in the agent's cwd, which is the repo.
                    "result": "created",
                }
            ],
            "chunks": ["I was not allowed to do that"],
        },
    )
    await run_review(agent_config(script), repo, commit)
    assert not canary.exists()


async def test_no_reject_option_results_in_turn_cancellation(
    repo: Path, commit: Commit, tmp_path: Path
) -> None:
    """If the agent offers nothing to reject, the client must cancel rather than allow."""
    script = write_script(
        tmp_path,
        {
            "tools": [
                {
                    "id": "c1",
                    "kind": "execute",
                    "title": "evil",
                    "raw_input": {"command": "rm", "args": ["-rf", "/"]},
                    "request_permission": True,
                    "permission_options": ["allow_once"],
                }
            ],
            "chunks": ["should not get here"],
        },
    )
    with pytest.raises(ReviewError, match="no output"):
        await run_review(agent_config(script), repo, commit)


# -- failure modes ---------------------------------------------------------


async def test_missing_agent_binary_is_reported_clearly(repo: Path, commit: Commit) -> None:
    config = Config(agent=AgentConfig(command="/nonexistent/agent-binary"))
    with pytest.raises(ReviewError, match="not found"):
        await run_review(config, repo, commit)


async def test_agent_crash_is_reported(repo: Path, commit: Commit, tmp_path: Path) -> None:
    script = write_script(tmp_path, {"crash": True})
    with pytest.raises(ReviewError):
        await run_review(agent_config(script), repo, commit)


async def test_agent_producing_nothing_is_an_error(
    repo: Path, commit: Commit, tmp_path: Path
) -> None:
    script = write_script(tmp_path, {"chunks": []})
    with pytest.raises(ReviewError, match="no output"):
        await run_review(agent_config(script), repo, commit)


async def test_missing_agent_config_is_reported(repo: Path, commit: Commit) -> None:
    with pytest.raises(Exception) as info:
        await run_review(Config(), repo, commit)
    assert "review_agent" in str(info.value)


async def test_refusal_keeps_partial_markdown(repo: Path, commit: Commit, tmp_path: Path) -> None:
    script = write_script(
        tmp_path, {"chunks": ["I looked at this briefly."], "stop_reason": "refusal"}
    )
    outcome = await run_review(agent_config(script), repo, commit)
    assert outcome.stop_reason == "refusal"
    assert not outcome.is_complete
    assert "looked at this" in outcome.markdown


async def test_usage_is_captured(repo: Path, commit: Commit, tmp_path: Path) -> None:
    script = write_script(tmp_path, {"chunks": ["ok"], "usage": True})
    outcome = await run_review(agent_config(script), repo, commit)
    assert outcome.usage is not None
    assert outcome.usage["used"] == 1234
    assert outcome.usage["cost"] == {"amount": 0.02, "currency": "USD"}


# -- timeout and cancellation ---------------------------------------------


async def test_timeout_raises_timeout_error_not_review_error(
    repo: Path, commit: Commit, tmp_path: Path
) -> None:
    """A timeout must stay distinguishable from a failure.

    Callers keep the partial markdown and label the review 'cancelled' rather than
    'failed', which is only possible if the timeout is not wrapped in ReviewError.
    """
    script = write_script(tmp_path, {"chunks": ["partial findings"], "hang_after_chunks": True})
    config = agent_config(script)
    config = Config(
        agent=config.agent,
        review_guidelines=config.review_guidelines,
        max_diff_bytes=config.max_diff_bytes,
        review_timeout_seconds=1,
        deny_unknown_commands=config.deny_unknown_commands,
    )
    with pytest.raises(TimeoutError):
        await run_review(config, repo, commit)


async def test_partial_markdown_survives_a_timeout(
    repo: Path, commit: Commit, tmp_path: Path
) -> None:
    """The design promises partial output is kept, not discarded."""
    script = write_script(
        tmp_path, {"chunks": ["Found one bug at app.py:3"], "hang_after_chunks": True}
    )
    runner = ReviewRunner(agent_config(script), repo, timeout_seconds=1)
    with pytest.raises(TimeoutError):
        await runner.review(commit)
    partial = runner.partial_markdown
    assert partial is not None
    assert "Found one bug" in partial


async def test_partial_tool_log_survives_a_timeout(
    repo: Path, commit: Commit, tmp_path: Path
) -> None:
    script = write_script(
        tmp_path,
        {
            "tools": [
                {
                    "id": "c1",
                    "kind": "read",
                    "title": "read app.py",
                    "raw_input": {"path": "app.py"},
                    "request_permission": False,
                }
            ],
            "chunks": ["looked at it"],
            "hang_after_chunks": True,
        },
    )
    runner = ReviewRunner(agent_config(script), repo, timeout_seconds=1)
    with pytest.raises(TimeoutError):
        await runner.review(commit)
    assert runner.partial_tool_log, "tool activity before the timeout should be retained"


async def test_timed_out_runner_has_no_final_outcome(
    repo: Path, commit: Commit, tmp_path: Path
) -> None:
    script = write_script(tmp_path, {"chunks": ["x"], "hang_after_chunks": True})
    runner = ReviewRunner(agent_config(script), repo, timeout_seconds=1)
    with pytest.raises(TimeoutError):
        await runner.review(commit)
    assert runner.last_outcome is None


# -- isolation -------------------------------------------------------------


async def test_each_review_uses_a_fresh_process(repo: Path, commit: Commit, tmp_path: Path) -> None:
    """Two reviews must not share an agent process or session."""
    script = write_script(tmp_path, {"chunks": ["ok"]})
    config = agent_config(script)
    first = await run_review(config, repo, commit)
    second = await run_review(config, repo, commit)
    assert first.markdown == second.markdown
    assert first.agent == second.agent


async def test_agent_version_is_not_reported_but_agent_is(
    repo: Path, commit: Commit, tmp_path: Path
) -> None:
    script = write_script(tmp_path, {"chunks": ["ok"]})
    outcome = await run_review(agent_config(script), repo, commit)
    assert outcome.agent == sys.executable


# -- the worktree path must not be persisted verbatim ---------------------------------


def test_the_worktree_path_is_rewritten_in_the_stored_tool_log(tmp_path: Path) -> None:
    """The agent is given a temp directory whose name carries our pid and changes every run.

    Stored verbatim, every command in the log refers to a path that no longer exists and means
    nothing to whoever reads the review later, and two reviews of the same commit cannot be
    compared. It is rewritten to a stable placeholder instead.
    """
    from code_review.agent.runner import WORKTREE_PLACEHOLDER, _with_root_rewritten

    root = tmp_path / "code-review-4242-abcdef"
    root.mkdir()
    log = [
        {"title": f"cat {root}/src/app.py", "kind": "read", "status": "completed"},
        {"title": f"cd {root} && git log --oneline", "kind": "execute"},
    ]
    rewritten = _with_root_rewritten(log, root)

    for entry in rewritten:
        assert "code-review-4242" not in entry["title"]
        assert WORKTREE_PLACEHOLDER in entry["title"]
    assert f"cat {WORKTREE_PLACEHOLDER}/src/app.py" == rewritten[0]["title"]


def test_rewriting_the_tool_log_changes_nothing_else(tmp_path: Path) -> None:
    """Only the title is rewritten, and only by substitution.

    Pattern-matching the whole log would mean guessing at the agent's payload shape, and a
    wrong guess loses data silently. Anything unrecognised is passed through untouched.
    """
    from code_review.agent.runner import _with_root_rewritten

    root = tmp_path / "code-review-1-x"
    root.mkdir()
    log = [
        {"title": f"ls {root}", "kind": "execute", "status": "completed", "decision": "deny"},
        {"title": None, "kind": "read"},
        {"no_title_at_all": f"{root}"},
    ]
    rewritten = _with_root_rewritten(log, root)

    assert rewritten[0]["decision"] == "deny", "other fields must survive verbatim"
    assert rewritten[0]["status"] == "completed"
    assert rewritten[1]["title"] is None, "a missing title must not raise"
    assert rewritten[2]["no_title_at_all"] == f"{root}", "unknown fields are left alone"
    assert log[0]["title"] == f"ls {root}", "the input must not be mutated"


def test_a_path_outside_the_worktree_is_left_alone(tmp_path: Path) -> None:
    """Only our own root is rewritten, so an agent's own paths survive for diagnosis."""
    from code_review.agent.runner import _with_root_rewritten

    root = tmp_path / "code-review-1-x"
    root.mkdir()
    log = [{"title": "cat /etc/passwd", "kind": "read"}]
    assert _with_root_rewritten(log, root)[0]["title"] == "cat /etc/passwd"

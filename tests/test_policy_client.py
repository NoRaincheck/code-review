"""The client's own refusals and its filesystem boundary.

:mod:`code_review.policy` decides whether a *tool call* is permitted. This module covers the
layer underneath it: the client methods an agent can call directly. Those are a second line
of defence, and they are only worth having if they actually refuse.

The filesystem boundary matters most. The agent is pointed at a real checkout that may
contain a hostile diff, and ``read_text_file`` is an advertised capability, so confinement
to the repository is the thing standing between a prompt injection and ``~/.ssh/id_rsa``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review.agent.policy_client import ReviewClient, ReviewClientConfig


@pytest.fixture
def client(repo: Path) -> ReviewClient:
    return ReviewClient(config=ReviewClientConfig(repo=repo))


# -- reads inside the repository -------------------------------------------


async def test_reads_a_repository_file(client: ReviewClient, repo: Path) -> None:
    response = await client.read_text_file("s1", "app.py")
    assert "def add(a, b):" in response.content


async def test_read_accepts_an_absolute_in_repo_path(client: ReviewClient, repo: Path) -> None:
    response = await client.read_text_file("s1", str(repo / "app.py"))
    assert "def add(a, b):" in response.content


async def test_read_returns_a_line_window(client: ReviewClient, repo: Path) -> None:
    response = await client.read_text_file("s1", "app.py", line=2, limit=1)
    assert response.content.strip() == "return a + b"


async def test_read_line_without_limit_runs_to_the_end(client: ReviewClient) -> None:
    response = await client.read_text_file("s1", "app.py", line=1)
    assert "return a + b" in response.content


async def test_read_line_past_the_end_is_empty_not_an_error(client: ReviewClient) -> None:
    response = await client.read_text_file("s1", "app.py", line=9999)
    assert response.content == ""


async def test_oversized_read_is_truncated(client: ReviewClient, repo: Path) -> None:
    (repo / "big.txt").write_text("x" * 50_000, encoding="utf-8")
    client.config.max_read_bytes = 1000
    response = await client.read_text_file("s1", "big.txt")
    assert "read truncated" in response.content
    assert len(response.content.encode()) < 2000


# -- reads outside the repository are refused -------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "../../etc/hosts",
        "/etc/hosts",
        "/etc/passwd",
    ],
)
async def test_read_outside_the_repository_is_refused(client: ReviewClient, path: str) -> None:
    with pytest.raises(PermissionError, match="outside the repository"):
        await client.read_text_file("s1", path)


async def test_a_symlink_escaping_the_repo_is_refused(client: ReviewClient, repo: Path) -> None:
    """A symlink inside the repo must not become a way out of it.

    The path looks internal, so only resolving before checking catches this.
    """
    secret = repo.parent / "secret.txt"
    secret.write_text("TOKEN", encoding="utf-8")
    (repo / "innocent.txt").symlink_to(secret)
    try:
        with pytest.raises(PermissionError, match="outside the repository"):
            await client.read_text_file("s1", "innocent.txt")
    finally:
        secret.unlink()


async def test_a_symlinked_directory_cannot_be_traversed(
    client: ReviewClient, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOKEN", encoding="utf-8")
    (repo / "link").symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(PermissionError, match="outside the repository"):
            await client.read_text_file("s1", "link/secret.txt")
    finally:
        (repo / "link").unlink()


async def test_reading_the_repository_root_itself_is_allowed(
    client: ReviewClient, repo: Path
) -> None:
    """The boundary check must not reject the repo root, which is inside itself."""
    client._resolve_in_repo(str(repo))


# -- operations that must always be refused --------------------------------


async def test_write_text_file_is_refused(client: ReviewClient, repo: Path) -> None:
    """Even if an agent ignores the advertised capability, it must not be able to write."""
    with pytest.raises(PermissionError, match="read-only"):
        await client.write_text_file("s1", "app.py", "pwned")
    assert "def add(a, b):" in (repo / "app.py").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("create_terminal", ("ls",)),
        ("terminal_output", ("t1",)),
        ("kill_terminal", ("t1",)),
        ("release_terminal", ("t1",)),
        ("wait_for_terminal_exit", ("t1",)),
    ],
)
async def test_terminal_operations_are_refused(
    client: ReviewClient, method: str, args: tuple[str, ...]
) -> None:
    with pytest.raises(PermissionError, match="terminal"):
        await getattr(client, method)("s1", *args)


async def test_elicitation_is_refused_because_nobody_is_watching(client: ReviewClient) -> None:
    """A review runs unattended; an agent asking a question must fail, not hang."""
    with pytest.raises(PermissionError, match="unattended"):
        await client.create_elicitation("which one?", "default")


async def test_complete_elicitation_is_a_noop(client: ReviewClient) -> None:
    assert await client.complete_elicitation("e1") is None


async def test_unknown_extension_methods_are_refused(client: ReviewClient) -> None:
    with pytest.raises(PermissionError, match="extension"):
        await client.ext_method("custom/thing", {})
    assert await client.ext_notification("custom/thing", {}) is None


async def test_on_connect_is_a_noop(client: ReviewClient) -> None:
    assert client.on_connect(object()) is None  # type: ignore[arg-type]


# -- accumulated output ----------------------------------------------------


async def test_markdown_accumulates_chunks(client: ReviewClient) -> None:
    from acp.helpers import text_block, update_agent_message

    for text in ("one ", "two"):
        await client.session_update("s1", update_agent_message(text_block(text)))
    assert client.markdown() == "one two"


async def test_thoughts_are_ignored_rather_than_mixed_into_the_review(
    client: ReviewClient,
) -> None:
    """Thinking text must not end up in a review a human reads."""
    from acp.helpers import text_block, update_agent_thought

    await client.session_update("s1", update_agent_thought(text_block("internal reasoning")))
    await client.session_update("s1", update_agent_thought(text_block("more reasoning")))
    assert client.markdown() == ""


async def test_a_malformed_update_does_not_raise(client: ReviewClient) -> None:
    """Exceptions here are swallowed by acp, so a bad handler looks like silence."""

    class Nonsense:
        session_update = "agent_message_chunk"

        @property
        def content(self) -> None:
            raise RuntimeError("boom")

    await client.session_update("s1", Nonsense())  # type: ignore[arg-type]
    assert client.markdown() == ""

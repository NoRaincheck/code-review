"""Prompt construction tests. Pure function, so no agent or repository needed."""

from __future__ import annotations

from code_review.git import Commit
from code_review.prompt import build_prompt


def make(**overrides: object) -> Commit:
    base: dict[str, object] = {
        "sha": "f" * 40,
        "short_sha": "fffffff",
        "parents": ("e" * 40,),
        "subject": "Add division",
        "body": "Divides.",
        "author_name": "Dev",
        "author_email": "dev@example.com",
        "authored_at": "2026-10-01T00:00:00+00:00",
        "diff": "--- a/app.py\n+++ b/app.py\n@@\n+def div(a, b):\n+    return a / b\n",
    }
    base.update(overrides)
    return Commit(**base)  # type: ignore[arg-type]


def test_prompt_contains_sha_subject_and_diff() -> None:
    prompt = build_prompt(make())
    assert "f" * 40 in prompt
    assert "Add division" in prompt
    assert "def div(a, b):" in prompt
    assert "dev@example.com" in prompt


def test_prompt_asks_for_file_line_citations() -> None:
    assert "path/to/file.py:123" in build_prompt(make())


def test_prompt_tells_agent_it_cannot_modify() -> None:
    assert "cannot change files" in build_prompt(make())


def test_guidelines_are_included_when_present() -> None:
    prompt = build_prompt(make(), guidelines="Always check for off-by-one errors.")
    assert "Always check for off-by-one errors." in prompt
    assert "Project-specific review guidelines" in prompt


def test_guidelines_section_omitted_when_absent() -> None:
    assert "Project-specific review guidelines" not in build_prompt(make())


def test_empty_guidelines_add_no_section() -> None:
    assert "Project-specific" not in build_prompt(make(), guidelines="")
    assert "Project-specific" not in build_prompt(make(), guidelines="   \n  ")


def test_root_commit_has_no_parents() -> None:
    prompt = build_prompt(make(parents=()))
    assert "(none, root commit)" in prompt


def test_multiple_parents_are_listed() -> None:
    prompt = build_prompt(make(parents=("a" * 40, "b" * 40)))
    assert "a" * 40 in prompt
    assert "b" * 40 in prompt


def test_missing_subject_is_handled() -> None:
    assert "(no subject)" in build_prompt(make(subject=""))


def test_diff_is_wrapped_in_a_fence() -> None:
    prompt = build_prompt(make())
    assert "```diff" in prompt
    assert prompt.rstrip().endswith("```")


def test_prompt_is_deterministic() -> None:
    commit = make()
    assert build_prompt(commit, "rule") == build_prompt(commit, "rule")


def test_all_sections_present_in_order() -> None:
    prompt = build_prompt(make(), guidelines="MY_RULE")
    assert prompt.index("Commit under review") < prompt.index("MY_RULE")
    assert prompt.index("MY_RULE") < prompt.index("Output format")
    assert prompt.index("Output format") < prompt.index("## Diff")

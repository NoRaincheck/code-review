"""Builds the review prompt. Pure function of its arguments, with no I/O.

Kept pure so prompt behaviour (capping, formatting, guideline placement) is testable
without spawning an agent or touching a repository.
"""

from __future__ import annotations

from .git import Commit

BASE_INSTRUCTIONS = """\
You are reviewing a single git commit. Report problems only; do not restate what the
commit does.

Focus on, in priority order:
1. Correctness bugs — logic errors, off-by-one, unhandled error paths, incorrect
   conditions, resource leaks, race conditions.
2. Security — injection, unsafe deserialization, secret handling, path traversal,
   missing authorization checks.
3. API and compatibility breaks — changed signatures, removed or renamed exports,
   altered return shapes, database migrations without a safe path forward.
4. Missing tests for new behaviour.
5. Readability only when something is actively misleading.

Rules:
- Cite every finding as `path/to/file.py:123`. Use the line number from the new side
  of the diff. If a finding spans a file, cite the most relevant line.
- Only report problems introduced by this commit. Pre-existing problems are out of
  scope; do not report them.
- If you are not confident a problem is real, leave it out. A short review with no
  findings is a good outcome.
- If you find nothing, say so plainly in one line. Do not invent minor nitpicks to
  look thorough.
- You cannot change files. Report problems; do not fix them.
"""

GUIDELINES_HEADER = """\
## Project-specific review guidelines

These come from the repository's .codereview.toml and take precedence over the
general guidance above.
"""

OUTPUT_HEADER = """\
## Output format

Reply with Markdown:

- Start with a one-line verdict: either `No issues found.` or `N issue(s) found.`
- Then one `###` section per finding, in severity order, each containing a bolded
  title, a `file:line` citation, and a short paragraph explaining the problem and
  why it matters. Quote the offending line only when it aids understanding.
- Do not use code fences around the whole response and do not restate the diff.
"""


def _commit_section(commit: Commit) -> str:
    lines = [
        "## Commit under review",
        "",
        f"- sha: `{commit.sha}`",
        f"- author: {commit.author_name} <{commit.author_email}>",
        f"- authored: {commit.authored_at}",
        f"- parents: {', '.join(commit.parents) if commit.parents else '(none, root commit)'}",
        "",
        "### Commit message",
        "",
        commit.subject or "(no subject)",
    ]
    if commit.body:
        lines.extend(["", commit.body])
    return "\n".join(lines)


def build_prompt(commit: Commit, guidelines: str = "") -> str:
    """Assemble the full review prompt for *commit*.

    The diff is inlined so a review usually needs no shell access at all; the read-only
    git allowlist exists for follow-up questions about surrounding code.
    """
    sections = [BASE_INSTRUCTIONS.strip(), "", _commit_section(commit)]
    guidelines = guidelines.strip()
    if guidelines:
        sections.extend(["", GUIDELINES_HEADER.strip(), "", guidelines])
    sections.extend(["", OUTPUT_HEADER.strip(), "", "## Diff", "", "```diff", commit.diff, "```"])
    return "\n".join(sections)

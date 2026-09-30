"""Permission policy for the reviewing agent.

A pure function from a tool call to a decision. ``runner.py`` is the only thing that
applies it, so every rule below is unit-testable without an agent process.

The security argument: an agent reviewing a diff has no reason to modify the checkout,
so anything that could write or execute is refused. Read-only git subcommands are
allowed because they let the agent check surrounding code.

Parsing note that matters: commands are matched against a *parsed argv*, never a
substring of a shell string. ``git log; rm -rf /`` must not be allowed merely because
it starts with ``git log``, and ``git -c core.pager='sh -c evil' log`` must not be
allowed merely because its subcommand is ``log``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum

# Tool kinds that only observe. Safe to approve outright.
READ_ONLY_KINDS = frozenset({"read", "search", "fetch", "think"})

# Tool kinds that always mutate or change how the agent operates. Never approved.
MUTATING_KINDS = frozenset({"edit", "delete", "move", "switch_mode"})

# Git subcommands that only read repository state.
READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "show",
        "diff",
        "log",
        "blame",
        "rev-parse",
        "status",
        "ls-files",
        "cat-file",
        "grep",
    }
)

# Characters that let a token escape into a shell. Their presence denies the call
# outright, because we do not know how the agent will execute the argv.
SHELL_METACHARACTERS = frozenset(";|&$`<>\n\r")

# Flags that redirect output to a file, spawn a program from config, or move the
# process outside the repository. Matched on the flag name before any "=" value.
DANGEROUS_GIT_FLAGS = frozenset(
    {
        # write files
        "-o",
        "--output",
        "--output-directory",
        # execute programs
        "-c",
        "--config-env",
        "--exec",
        "--exec-path",
        "-O",
        "--open-files-in-pager",
        "--ext-diff",
        "--textconv",
        "--paginate",
        # escape the repository root
        "-C",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        # plumbing that moves objects or refs around
        "--receive-pack",
        "--upload-pack",
        "-h",
        "--help",
    }
)

COMMAND_KEYS = ("command", "cmd", "commandLine", "command_line")


class Verdict(Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Decision:
    """A verdict plus a human-readable justification for the audit log.

    ``hard`` marks a denial that no configuration may override: shell metacharacters,
    a non-git executable, or a git flag that can write a file or spawn a program. Only
    denials over the *subcommand allowlist* are soft, because ``deny_unknown_commands``
    exists precisely to let an operator relax that list.
    """

    verdict: Verdict
    reason: str
    hard: bool = False

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    @property
    def relaxable(self) -> bool:
        return not self.allowed and not self.hard


def _split_command_line(text: str) -> list[str] | None:
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    return tokens or None


def extract_argv(raw_input: object) -> list[str] | None:
    """Best-effort extraction of a command argv from an agent's ``raw_input``.

    Agents are inconsistent here: some send ``{"command": "git", "args": [...]}``,
    some send ``{"command": "git log -5"}``, some send a bare string. Anything we
    cannot pin down to a definite argv returns None, which the caller denies.
    """
    if raw_input is None:
        return None

    if isinstance(raw_input, str):
        return _split_command_line(raw_input)

    if isinstance(raw_input, (list, tuple)):
        if not raw_input or not all(isinstance(part, str) for part in raw_input):
            return None
        return list(raw_input)

    if not isinstance(raw_input, dict):
        return None

    command = next(
        (raw_input[key] for key in COMMAND_KEYS if isinstance(raw_input.get(key), str)),
        None,
    )
    if command is None:
        return None

    tokens = _split_command_line(command)
    if tokens is None:
        return None

    args = raw_input.get("args", raw_input.get("argv"))
    if isinstance(args, (list, tuple)):
        if not all(isinstance(part, str) for part in args):
            return None
        tokens.extend(args)

    return tokens


def _flag_name(token: str) -> str | None:
    """Return the flag portion of *token*, or None if it is not a flag.

    ``--output=/tmp/x`` yields ``--output``; ``--stat`` yields ``--stat``.
    """
    if not token.startswith("-"):
        return None
    return token.split("=", 1)[0]


def classify_git_argv(argv: list[str]) -> Decision:
    """Decide whether *argv* is a read-only git invocation.

    Denials are marked ``hard`` unless the only objection is the subcommand allowlist.
    """
    if not argv:
        return Decision(Verdict.DENY, "empty command", hard=True)

    for token in argv:
        if any(char in SHELL_METACHARACTERS for char in token):
            return Decision(Verdict.DENY, f"shell metacharacter in token {token!r}", hard=True)

    executable = argv[0]
    if executable != "git":
        return Decision(Verdict.DENY, f"only git is allowed, got {executable!r}", hard=True)

    for token in argv[1:]:
        flag = _flag_name(token)
        if flag is not None and flag in DANGEROUS_GIT_FLAGS:
            return Decision(Verdict.DENY, f"git flag {flag!r} can execute or write", hard=True)

    positional = [token for token in argv[1:] if not token.startswith("-")]
    if not positional:
        return Decision(Verdict.DENY, "no git subcommand given", hard=True)

    subcommand = positional[0]
    if subcommand not in READ_ONLY_GIT_SUBCOMMANDS:
        return Decision(Verdict.DENY, f"git subcommand {subcommand!r} is not read-only")
    return Decision(Verdict.ALLOW, f"read-only git {subcommand}")


def decide(
    *,
    kind: str | None,
    title: str | None = None,
    raw_input: object = None,
    deny_unknown_commands: bool = True,
) -> Decision:
    """Decide whether a tool call may proceed.

    *kind* is the ACP tool kind, *raw_input* the agent's raw arguments. Returns DENY for
    anything not positively identified as safe; the default is refusal, not approval.

    ``deny_unknown_commands=False`` relaxes only the subcommand allowlist. Hard denials —
    shell metacharacters, non-git executables, and git flags that write or execute — are
    never relaxed, because an operator relaxing the allowlist has not asked to allow
    ``git -c core.pager=<command>``.
    """
    label = title or kind or "tool call"

    if kind is None:
        return Decision(Verdict.DENY, f"{label}: missing tool kind, refusing by default")

    if kind in MUTATING_KINDS:
        return Decision(Verdict.DENY, f"{label}: {kind!r} would modify the checkout")

    if kind in READ_ONLY_KINDS:
        return Decision(Verdict.ALLOW, f"{label}: {kind!r} is read-only")

    if kind != "execute":
        # "other" or a kind this build does not recognise.
        return Decision(Verdict.DENY, f"{label}: unrecognised kind {kind!r}, refusing")

    argv = extract_argv(raw_input)
    if argv is None:
        return Decision(Verdict.DENY, f"{label}: could not determine the command, refusing")

    decision = classify_git_argv(argv)
    if decision.allowed or decision.hard or deny_unknown_commands:
        return decision
    return Decision(Verdict.ALLOW, f"{label}: non-read-only git allowed by config")

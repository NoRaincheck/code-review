# code-review

Background AI code review of every commit, orchestrated over the
[Agent Client Protocol](https://agentclientprotocol.com).

`git commit` returns immediately. A local daemon picks the commit up, asks any
ACP-capable agent to review it, and stores the markdown. You read it when you want.

```
git commit -m "..."      # returns in well under a second
code-review show         # the review of HEAD
```

## Install

```bash
uv sync                  # from a checkout
code-review init         # in the repository you want reviewed
```

`init` installs a `post-commit` hook and creates the review database. It refuses to overwrite a
`post-commit` hook it did not write unless you pass `--force`. All state lives under the
repository's shared git directory, so nothing is written to your working tree and `.gitignore`
is left alone. The one exception is an upgrade: if a `.codereview/` directory is already there
from an older version, `init` adds it to `.gitignore` so the leftover stays ignored.

## Configure an agent

Any agent that speaks ACP works. Set its argv in `.codereview.toml`:

```toml
review_agent = ["opencode", "acp"]     # opencode
# review_agent = ["codex-acp"]         # OpenAI Codex
# review_agent = ["claude-agent-acp"]   # Anthropic Claude Code
# review_agent = ["gemini", "--acp"]    # Gemini CLI

review_timeout_seconds = 300
```

There is no built-in list on purpose: ACP launch commands differ per vendor and change over
time, so an explicit argv never needs maintenance.

Check that your command actually speaks ACP — this completes a real handshake without
spending a token:

```bash
code-review doctor --probe
```

```
agent:       opencode acp
              found at /opt/homebrew/bin/opencode
acp probe:   OK - ready for reviews (OpenCode 1.18.33)
```

Three outcomes, because they need different fixes:

- `OK` — works.
- `PARTIAL` — speaks ACP but cannot start a session, usually because you are not logged in.
  The agent's own auth methods are listed.
- `FAILED` — is not an ACP agent.

A command on `PATH` that is not an ACP agent is reported here rather than failing later
during a review.

## Use

```bash
code-review show                 # review of HEAD
code-review show abc1234         # review of one commit
code-review show --raw           # markdown only, for pipes and editors
code-review list                 # recent reviews
code-review list --status failed
code-review review HEAD --wait   # review now, block until done
code-review review HEAD --force  # re-review a commit that already has one
code-review review HEAD --inline # skip the daemon, run here
code-review status               # daemon liveness and queue depth
code-review doctor               # check the installation
code-review daemon stop          # stop the background daemon
code-review hook uninstall       # remove the hook
```

`show` resolves a revision like git does: exact sha, then a live revision, then a unique
abbreviation. An ambiguous abbreviation is an error listing the candidates rather than a
guess, since showing one commit's review for another is worse than showing none.

| Exit | Meaning |
| --- | --- |
| `0` | printed a review |
| `2` | no review for that commit |
| `3` | that prefix matches several reviewed commits |

Reviews are keyed by sha and the database is not in the repository, so a review outlives the
commit being rewritten away by a rebase.

## How it works

```
git commit
    │
    ▼
post-commit ──▶ enqueue ──▶ ~/.codereview/daemon.sock ──▶ INSERT status='queued'
    │             (returns immediately; every failure path exits 0)          │
    ▼                                                                       ▼
  commit done                                                        worker loop, one at a time
                                                                            │
                                        git show <sha> ─▶ prompt ─▶ ACP agent (fresh process per review)
                                                                            │
                                                                  policy ──┤  denies writes and
                                                                           │  non-read-only commands
                                                                           ▼
                                              <git common dir>/codereview/reviews.db
```

Reviews live in `<git common dir>/codereview/reviews.db`, one SQLite file per repository,
shared by all its worktrees and kept out of your working tree.
The `reviews` table doubles as the queue: `status='queued'` rows are pending work, so a
daemon restart resumes from the database and a review interrupted by a crash is re-queued
rather than lost.

One review equals one agent process — spawn, one prompt, collect, tear down. A hung or
crashed agent cannot affect the next review.

## Configuration

All keys optional in `.codereview.toml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `review_agent` | — | Required. Argv of an ACP agent. |
| `review_agent_args` | `[]` | Appended after the command. |
| `review_agent_env` | `{}` | Extra environment for the agent. |
| `review_guidelines` | `""` | Appended to the prompt. Project-specific review standards. |
| `max_diff_bytes` | `200000` | Cap on the inline diff. |
| `review_timeout_seconds` | `300` | Per-review wall clock. |
| `deny_unknown_commands` | `true` | Allow non-read-only `git` subcommands when `false`. |

`CODEREVIEW_HOME` relocates the daemon socket, pid file, and logs from `~/.codereview`.

## Platform support

The background daemon requires POSIX: it uses Unix domain sockets, `fcntl` locks, and POSIX
signals. That covers macOS and Linux.

On Windows the daemon commands (`enqueue`, `daemon start/stop`) report that plainly instead
of failing with a bare import error. The rest works: `show`, `list`, `doctor`, and
`review --inline` all run without a daemon, so reviewing a single commit on Windows means
using `--inline`.

## If `git commit` starts failing with "invalid object"

Some agents take a repository snapshot before reviewing, and that runs `git add --all`, which
stages your untracked files. With `opencode acp` this was observed to leave the index
referencing objects that are not in the object store, and the next commit fails with
`invalid object`. code-review logs an explicit warning when it detects this.

The fix is:

```bash
code-review repair --dry-run    # show what would be unstaged
code-review repair              # unstage exactly those entries
```

This is deliberately not `git reset`, which would also discard staging you meant to keep. It
unstages only the entries whose objects are missing, leaves every file on disk untouched, and
confirms the index is usable again. With no damage it reports that and does nothing.

## Git worktrees

The hook path is resolved by git, so `code-review init` works from any worktree and installs
into the repository's shared hooks directory — one hook covers every worktree, which is what
git expects.

The review database is per-*repository*, resolved through `git rev-parse --git-common-dir`, so
`show` and `list` see the same history from every worktree and a commit is reviewed once no
matter which worktree you are standing in.

Reviews themselves run in a throwaway git worktree at the reviewed commit, not in your
checkout. That means the agent reads the code the review is about even when your working tree
has moved on, and anything it stages is discarded with that worktree. Two consequences worth
knowing: a review writes a short-lived registration under `.git/worktrees/` (removed
immediately afterwards, and it creates no branch or ref), and the temporary worktree lives in
the system temp directory, so a machine with a small `/tmp` and many reviews may need
`TMPDIR` pointed somewhere roomier.

## Safety

The reviewing agent can read the whole repository and cannot write to it: edits, deletes,
moves, and terminal access are refused, as is any command that is not a read-only `git`
subcommand. Reads are confined to the repository too, so a `../` or absolute path is refused.
Commands are matched against a parsed argv, not a substring of a shell string, so
`git log; rm -rf /` is refused even though it starts with `git log`.

The commit and its diff are quoted into the prompt as **untrusted data**, and the prompt says so
explicitly: text inside them posing as instructions is itself a finding to report, not a
directive. That is a request, not a guarantee, and it is the agent's cooperation that enforces
it. Measured against a real agent, a commit file and a commit message both carrying a
"system override for AI reviewers" that demanded a shell write and a false verdict: the agent
reported the injection as a security finding, did not run the command, and did not issue the
verdict it was told to. That is evidence, not a control.

**The permission hook is a backstop, not a sandbox.** It only applies to tools an agent chooses
to ask about. An agent that executes without requesting permission is governed by its own
policy, not by ours, and the write capabilities we advertise are advice it may decline to take.
For code you do not trust, wrap the agent in an OS sandbox — `review_agent` is arbitrary argv,
so this needs no special support. This profile is verified to let a review finish while
blocking writes outside the agent's own state:

```toml
review_agent = [
  "sandbox-exec", "-p",
  # Deny every write, then re-allow only what the agent needs to run at all.
  # Without the two allow rules opencode cannot start, so the obvious
  # "(deny file-write*)" on its own breaks the review instead of securing it.
  "(version 1)(allow default)(deny file-write*)" +
  # the agent's own state, and
  "(allow file-write* (subpath \"/Users/you/.local/share/opencode\"))" +
  # the system temp directory, which is where the throwaway review worktree lives
  "(allow file-write* (subpath \"/var/folders/.../T\"))",
  "opencode", "acp",
]
```

Two caveats worth knowing. The temp directory has to be writable, so the agent can write
anywhere under it; the review worktree is there too, which is why that is where it was put.
And `sandbox-exec` is macOS-only and deprecated by Apple, though still present and working.
`seatbelt` is the same thing under another name. On Linux, `bwrap` or a container does the job.

Treat `.codereview.toml` as trusted: `review_agent` can name any executable, the same way a
`Makefile` can.
Treat `.codereview.toml` as trusted: `review_agent` can name any executable, the same way a
`Makefile` can.

## Development

```bash
./scripts/check.sh          # syntax, ruff, mypy --strict, tests, leaked-daemon check

uv run pytest --cov=code_review --cov-report=term-missing   # coverage
```

The ACP integration is tested against `tests/fake_agent.py`, a real ACP agent implemented
over stdio and driven by a script, so the whole hook → daemon → agent → storage path is
covered without calling a model.

Coverage reads low for `cli.py` and `daemon/client.py` because the end-to-end tests invoke
the CLI as a subprocess, which coverage cannot observe. Those paths are exercised; the report
just cannot see it.

The design, including every decision and its alternatives, is in
[`docs/superpowers/specs/2026-10-01-acp-code-review-design.md`](docs/superpowers/specs/2026-10-01-acp-code-review-design.md).

## License

MIT
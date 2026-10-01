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
repository's shared git directory, not in your working tree, so there is nothing to commit. A
`.gitignore` entry for `.codereview/` is still added, purely so a directory left behind by an
older version stays ignored.

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

The reviewing agent gets full read access and no ability to write: edits, deletes, moves,
and terminal access are refused, as is any command that is not a read-only `git`
subcommand. Commands are matched against a parsed argv, not a substring of a shell string,
so `git log; rm -rf /` is refused even though it starts with `git log`.

**The permission hook is a backstop, not a sandbox.** It only applies to tools an agent
chooses to ask about. An agent that executes without requesting permission is governed by
its own policy, not by ours. For code you do not trust, wrap the agent in an OS sandbox —
`review_agent` is arbitrary argv, so this needs no special support:

```toml
review_agent = [
  "sandbox-exec", "-p", "(version 1)(allow default)(deny file-write*)",
  "opencode", "acp",
]
```

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
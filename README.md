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

`init` installs a `post-commit` hook, creates `.codereview/`, and adds that directory to
`.gitignore`. It refuses to overwrite a `post-commit` hook it did not write unless you pass
`--force`.

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
acp probe:   OK - handshake ok (opencode acp)
              reports as OpenCode 1.18.33
```

A command that is on `PATH` but is not an ACP agent is reported as such, rather than
failing later during a review.

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

`show` exits `2` when a commit has no review, so scripts and CI can branch on it.

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
                                                            .codereview/reviews.db
```

Reviews live in `<repo>/.codereview/reviews.db`, one SQLite file per repository, gitignored.
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
uv run pytest               # 258 tests, no network, no vendor CLI
uv run ruff check .
uv run ruff format --check .
uv run mypy                 # strict
```

The ACP integration is tested against `tests/fake_agent.py`, a real ACP agent implemented
over stdio and driven by a script, so the whole hook → daemon → agent → storage path is
covered without calling a model.

The design, including every decision and its alternatives, is in
[`docs/superpowers/specs/2026-10-01-acp-code-review-design.md`](docs/superpowers/specs/2026-10-01-acp-code-review-design.md).

## License

MIT
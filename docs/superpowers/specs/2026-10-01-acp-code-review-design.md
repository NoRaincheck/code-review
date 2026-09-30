# code-review: ACP-orchestrated post-commit code review

- **Date:** 2026-10-01
- **Status:** Approved and implemented
- **Reference:** [roborev](https://github.com/kenn-io/roborev) (scope inspiration only, not a fork)
- **Protocol:** [Agent Client Protocol](https://agentclientprotocol.com) v1 via `agent-client-protocol==0.12.1`

---

## 1. Purpose

`code-review` runs an AI code review in the background after every `git commit`, stores the
result locally, and surfaces it on demand from the command line. It uses ACP so that any
ACP-capable agent can be used as the reviewer, rather than hardcoding one vendor's CLI.

## 2. Scope

**In scope (v1):** post-commit hook, background daemon, ACP agent orchestration, per-repo
SQLite storage, CLI retrieval.

**Deliberately out of scope:** TUI, browser UI, auto-fix/refine loops, static analysis types,
hooks for third-party agent harnesses, Postgres sync, CI integration, semantic search,
multi-agent fan-out, cross-repo listing.

The v1 feature set is justified by two requirements only: (a) asynchronously trigger a review
from a post-commit hook, (b) retrieve a review from the CLI.

## 3. Decisions

Each decision records the options considered and why the chosen one won. These were made
explicitly during design; none are defaults left to chance.

### D1 — Scope: minimal v1 with a daemon

**Chosen:** hook hands off to a long-lived daemon.

**Rejected:** roborev parity (many months; needs decomposition into several sub-specs);
detached per-commit process (unbounded concurrency under commit bursts, no ordering, no
shared socket); SQLite-backed queue with leases (most crash-resilient, but lease expiry is
fiddly and unnecessary when the queue is in-process and restart-resettable).

**Consequence:** commits that arrive while the daemon is down are lost. Accepted for v1;
`code-review review <sha>` exists for manual catch-up.

### D2 — Review storage: prose markdown only

**Chosen:** store whatever markdown the agent streamed.

**Rejected:** structured JSON findings with raw fallback (enables filtering and
machine-readable output, but ACP has no structured-output mechanism so it requires prompt
discipline plus a lenient parser — the main source of brittleness); MCP `submit_findings`
tool (strongest guarantee, but requires shipping an MCP server and relies on every ACP agent
wiring MCP tools through reliably); full transcript plus extracted findings (most faithful,
heaviest schema, and extraction is still needed for anything programmatic).

**Consequence:** no severity ranking, no `show --severity`, no programmatic findings without
an LLM re-read. `code-review show` prints markdown. This is a real cost, accepted because it
makes the system agent-agnostic and parser-free.

### D3 — Permission policy: deny mutating tool calls

**Chosen:** `request_permission` auto-approves `read`, `search`, `fetch`, `think` and the
read-only git allowlist, and rejects everything else.

**Rejected:** deny everything (agents cannot read surrounding files, so reviews are shallow);
approve everything (a reviewer with shell access on untrusted input is a prompt-injection
blast radius).

**Consequence:** agents that insist on non-allowlisted commands stall or give up. Mitigated by
inlining the diff in the prompt, so most reviews never need a shell at all.

### D4 — Agent selection: user-supplied argv in config

**Chosen:** `.codereview.toml` names the agent as an explicit argv array.

**Rejected:** built-in presets for known agents (roting table, per-vendor maintenance forever);
presets plus override (best UX, but same rot plus ambiguity over whether a string is a preset
or a command).

**Consequence:** the user must know their agent's ACP entrypoint. This is the price of
supporting an open-ended agent ecosystem with zero maintenance.

### D5 — Async mechanics: Unix socket to a daemon

**Chosen:** `post-commit` runs `code-review enqueue`, which connects to
`~/.codereview/daemon.sock` and returns. If the socket is refused, it spawns the daemon
detached and retries.

**Consequence:** the commit is never blocked. Every failure path in `enqueue` exits 0.

### D6 — Storage location: one SQLite database per repository

**Chosen:** `.codereview/reviews.db` inside the repo, auto-gitignored.

**Rejected:** single global `~/.codereview/reviews.db` (enables cross-repo listing, but a
shared mutable file outside the repo complicates parallel worktrees of the same repo);
per-repo plus a mirrored global index (two things to keep consistent).

**Consequence:** no cross-repo `code-review list` without `cd`-ing into each repo.

### D7 — Diff delivery: inline diff plus read-only git allowlist

**Chosen:** the prompt carries the commit message and `git show <sha>` output inline, capped
at `max_diff_bytes`. The agent may additionally read files and run read-only git commands.

**Rejected:** inline diff with no shell (cannot see surrounding code, misses whole-file and
cross-file problems); point at the SHA and let the agent run git (tiny prompt, but the agent
may review a different diff and burns tool round-trips first).

### D8 — Session lifecycle: a fresh agent process per review

**Chosen:** spawn the agent, `session/new`, one `prompt`, collect, tear down.

**Rejected:** persistent agent with one session per review (saves process startup, but the
session accumulates history so later reviews see earlier ones); one persistent session per
repo (fastest, but unbounded context growth and stale-tree reasoning after rebase).

**Consequence:** process startup and an auth check per commit, a few seconds, usually dwarfed
by review time. Total isolation means a hung or crashed agent cannot poison the next review.

## 4. Architecture

```
git commit
    │
    ▼
.git/hooks/post-commit ──▶ code-review enqueue
    │                        resolve HEAD (hook gets no args)
    │                        connect ~/.codereview/daemon.sock
    │                        ├─ refused ─▶ spawn daemon detached, retry
    │                        └─ ack ──▶ exit 0        ← commit never blocked
    ▼
daemon: INSERT review(sha, status='queued')  ──▶ ack
    │
    ▼  worker loop (FIFO, one at a time)
  git.py ─▶ git show <sha> ──▶ prompt.py ──▶ agent/runner.py ──▶ ACP agent
                                                        ▲              │
                                                        └── policy ────┘
                                                             │
                                        db.py: markdown ◀────┘
```

**`post-commit` receives no arguments.** The hook resolves `HEAD` itself via
`git rev-parse HEAD`. Assuming `$1` is the SHA silently reviews the wrong commit.

**The commit is never blocked.** Every failure path in `enqueue` exits 0 and appends to
`~/.codereview/hook.log`.

## 5. Module boundaries

Each unit is independently understandable and testable.

| Module | Responsibility | Depends on |
| --- | --- | --- |
| `config.py` | `.codereview.toml` → typed `Config`. | — |
| `git.py` | rev-parse, show, toplevel, current-branch, is-repo. Pure subprocess wrappers. | — |
| `prompt.py` | `(commit, diff) → str`. **Pure function.** | — |
| `policy.py` | `(tool_call) → Decision`. **Pure function.** | — |
| `db.py` | Schema + queries. Knows nothing about agents. | — |
| `paths.py` | Daemon socket, pid file, log file, per-repo state dir. | — |
| `agent/policy_client.py` | The `acp.interfaces.Client` implementation. | policy |
| `agent/runner.py` | Spawn agent, drive one turn, collect markdown. **Only module importing ACP.** | policy, prompt |
| `daemon/protocol.py` | Newline-delimited JSON request/response framing. | — |
| `daemon/server.py` | Socket listener, dispatch, `serve_forever`. | protocol |
| `daemon/worker.py` | The queue and the single review loop. | db, git, config, agent |
| `daemon/supervisor.py` | Detached spawn, stale-socket reaping. | paths |
| `hook.py` | Hook script generation, install, uninstall. | paths |
| `cli.py` | Argparse dispatch. | everything |

`prompt.py` and `policy.py` are pure, so they are exhaustively unit-testable without spawning
an agent. The ACP dependency is quarantined in `agent/`.

## 6. Data model

SQLite, WAL mode, one file per repository at `<repo>/.codereview/reviews.db`.

```sql
CREATE TABLE reviews (
    id            INTEGER PRIMARY KEY,
    sha           TEXT    NOT NULL,
    branch        TEXT,
    status        TEXT    NOT NULL,   -- queued|running|completed|failed|cancelled
    markdown      TEXT,               -- NULL unless completed
    error         TEXT,               -- failure reason
    stop_reason   TEXT,               -- end_turn|max_tokens|... | cancelled by us
    agent         TEXT,               -- argv[0], for provenance
    agent_version TEXT,
    usage_json    TEXT,               -- token counts + cost, when reported
    tool_log_json TEXT,               -- tool calls and permission decisions
    created_at    TEXT    NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);

CREATE UNIQUE INDEX reviews_sha_unique ON reviews(sha);
CREATE INDEX reviews_status_idx ON reviews(status);
```

**`UNIQUE(sha)` is the dedupe mechanism.** Re-reviewing a commit requires
`code-review review <sha> --force`, which replaces the existing row's result rather than
appending. This is why there is no separate jobs table: `status='queued'` rows *are* the
queue, so a daemon restart simply picks up whatever is still `queued` or `running`.

A row stuck in `running` after a crash is reset to `queued` at daemon startup, since no
process is working on it any more.

## 7. Config

`.codereview.toml` in the repo root. All keys optional.

```toml
# Explicit argv for an ACP agent. Required.
review_agent = ["claude-agent-acp"]
review_agent_args = []              # appended after the command
review_agent_env = { "FOO" = "bar" }

# Appended to the built-in prompt. Repo-specific standards go here.
review_guidelines = """
Focus on error handling and backwards compatibility.
"""

max_diff_bytes = 200000            # cap on inline diff
review_timeout_seconds = 300        # per-review wall clock
deny_unknown_commands = true        # reject anything outside the git allowlist
```

## 8. Permission policy

`policy.py` is a pure function from a `ToolCallUpdate` to a decision. `runner.py` applies it.

| Tool kind | Decision |
| --- | --- |
| `read`, `search`, `fetch`, `think` | allow |
| `execute` with an allowlisted read-only git command | allow |
| `execute` with anything else | deny (when `deny_unknown_commands`) |
| `edit`, `delete`, `move`, `switch_mode` | deny |
| `other`, missing, or unparseable | deny |

Read-only git allowlist: `show`, `diff`, `log`, `blame`, `rev-parse`, `status`, `ls-files`,
`cat-file`, `grep`. Commands are matched against the parsed argv, not a substring of a shell
string, so `git log; rm -rf /` is denied rather than allowed on a prefix match.

Deny is implemented by returning `AllowedOutcome(option_id=...)` pointing at the option whose
`kind` is `reject_once` or `reject_always`. `DeniedOutcome` is reserved for cancelling a turn
and must not be used for an ordinary refusal.

We advertise `fs.read_text_file = true` so the agent can read repository files through us
under our path checks. `fs.write_text_file` is advertised `false` and `terminal` is `false`;
`write_text_file` and the terminal methods raise if called anyway, because an agent that
ignores the advertised capability must still not be able to mutate anything.

## 9. Protocol and timeout behavior

Per-review timeout is `asyncio.wait_for(conn.prompt(...), timeout)`, not the SDK's
`receive_timeout`, which is a connection-wide read-idle backstop and cannot express "5 minutes
for the prompt, 5 seconds for initialize". On timeout we send `conn.cancel(session_id)`, then
record `stop_reason='cancelled'` with the partial markdown we received, so a slow review is
still useful output.

Agent-reported failures are recorded faithfully. `stop_reason` of `refusal`, `max_tokens`,
`max_turn_requests`, and `max_tokens` all mean the review is incomplete; the markdown collected
so far is kept and the row is marked `completed` with that stop reason, rather than silently
presenting a truncated review as complete.

## 10. Error handling

| Situation | Behavior |
| --- | --- |
| Daemon not running | `enqueue` spawns it detached, retries up to 10s, else logs and exits 0 |
| Socket exists but daemon is dead | Supervisor reaps a stale socket after failing a connect |
| Agent binary missing | Review fails with the exec error, hook unaffected |
| Agent hangs | Timeout, cancel, keep partial markdown |
| Agent requests a denied permission | Denied, review continues |
| No `review_agent` configured | Review fails with a clear config error naming the key |
| DB locked | Busy timeout, then fail the review, not the commit |

## 11. CLI surface

```
code-review init [--force]        # install hook, create state dir, gitignore
code-review enqueue [sha]         # hook entry point; resolves HEAD, never fails
code-review review <sha> [--force] [--agent CMD ...] [--timeout N] [--wait] [--inline]
code-review show [sha] [--raw]    # print markdown; defaults to HEAD
code-review list [--status S] [--limit N] [--json]
code-review status                # daemon liveness + queue depth
code-review daemon start|stop|status|run
code-review hook install|uninstall|status
code-review doctor [--probe]     # check installation; --probe proves the agent speaks ACP
```

`show` and `list` exit non-zero with a message when no review exists for the sha, so shell
scripting and CI can branch on it. `show` uses exit code `2` (`EXIT_NO_REVIEW`) specifically
for "nothing to show", which distinguishes it from a genuine failure. `show --raw` prints
markdown with no header decoration for pipes and editors.

`review` defaults to queueing on the daemon. `--inline` runs the agent in the calling
process instead, which is useful when the daemon is not wanted. Both paths persist the
result, so `show`, `list`, and the stored tool-call audit trail behave identically either
way.

`doctor --probe` exists because "is this command an ACP agent?" is the single most likely
setup failure, and a missing binary is not evidence either way. The probe spawns the
command and completes `initialize` plus `session/new` — pure handshake, so it costs no
tokens and no model call — then reports the agent's self-declared name and version. It
never raises; a failure is the deliverable. Verified against commands that are present on
PATH but are not ACP agents:

| `review_agent` | Probe result |
| --- | --- |
| `["opencode", "acp"]` | `OK` — reports as `OpenCode 1.18.33` |
| `["/bin/cat"]` | `FAILED - RequestError: Method not found` |
| `["/bin/echo", "x"]` | `FAILED - ConnectionError: Connection closed` |
| `["git", "--version"]` | `FAILED - ConnectionError: Connection closed` |
| `["/nonexistent/binary"]` | `FAILED - not found on PATH` |

An agent that advertises auth methods is reported as *offering* them, not as requiring
them: `opencode` lists `opencode-login` and still starts fine when already authenticated.

## 12. Testing

- **Pure units:** `prompt.py` (diff capping, truncation marker, sha rendering) and `policy.py`
  (every tool kind, allowlisted and non-allowlisted commands, argv-injection attempts such as
  `git log; rm -rf /` and `git -c core.pager=evil`, missing kind, unparseable command, and the
  guarantee that `deny_unknown_commands = false` relaxes only the subcommand allowlist).
- **`git.py` and `db.py`:** against real temporary git repositories and real SQLite files, not
  mocks. Covers a root commit with an empty `%P`, a body containing field-separator bytes, a
  merge commit, dedupe via `UNIQUE(sha)`, crash recovery of `running` rows, status transitions.
- **Fake ACP agent:** `tests/fake_agent.py` implements the ACP wire protocol over stdio and is
  driven by a script, so `runner.py`, the worker loop, and the full hook→daemon→retrieval path
  are testable end to end without any real vendor CLI or network.
- **End to end:** `tests/test_e2e.py` starts a real detached daemon, installs a real hook, runs
  real `git commit`, and retrieves the result — against the fake agent.
- **Live smoke test, real agent:** verified against `opencode acp`. It found both planted bugs
  with correct `file:line` citations, and refused two separate prompt-injection payloads
  without side effects. See §13 for what that run also revealed about permission coverage.

258 tests, `ruff check` clean, `ruff format` clean, `mypy --strict` clean.

## 13. Security model

The review prompt embeds commit messages and diffs from the repository. A malicious diff
containing instructions is a prompt-injection vector, so the agent runs with the permission
policy in D3: full read access, no writes, no arbitrary execution. Reviewing untrusted
contributor code still means the agent's model provider sees that code, and a determined
injection can still attempt to exfiltrate it through allowed read tools. This tool is
appropriate for trusted repositories; for untrusted ones, run it inside a sandbox.

### What the permission layer does and does not guarantee

The policy is only consulted when an agent calls `session/request_permission`. It is a
**backstop, not a sandbox.** Verified against `opencode acp` during the live smoke test: it
executed `git show --stat <sha> && ls` without requesting permission at all, so `decide()`
never ran for that call. The agent's own permission model is what stopped the writes the
injected diff asked for; ours was never consulted.

The policy is nonetheless strict about everything it does see. Each of these is denied,
including under `deny_unknown_commands = false`:

| Command | Denied because |
| --- | --- |
| `git show --stat abc && ls` | `&&` is a shell metacharacter |
| `bash -c "rm -rf /"` | not `git` |
| `sed -i s/a/b/ app.py` | not `git` |
| `git -c core.pager=evil log` | flag can execute |
| `git --output=/tmp/x log` | flag can write |

### Sandboxing untrusted code

Because `review_agent` is an arbitrary argv, wrapping the agent in an OS sandbox needs no
new feature:

```toml
review_agent = [
  "sandbox-exec", "-p", "(version 1)(allow default)(deny file-write*)",
  "opencode", "acp",
]
```

This is the only reliable containment for a hostile repository, and it is what should be
used when reviews run over contributions from people you do not trust.

### Observed: an agent can damage the checkout without asking

During the live smoke test, `opencode acp` created `.git/opencode` and staged two worktree
files (`.codereview.toml`, `.gitignore`). The index was then left referencing blobs that no
longer existed, and the next `git commit` failed:

```
error: invalid object 100644 645f0d11... for '.codereview.toml'
```

This was **not** caused by this tool. Two facts establish that:

- The only git subcommands this codebase executes are `rev-parse`, `show`, `log`,
  `diff --stat` context reads, and `merge-base --is-ancestor`; none write, and nothing here
  writes into `.git` except the hook file.
- A controlled run of the same sequence (`init`, `commit`, `review --inline` with the same
  agent) left `git fsck` clean.

So the agent reached the checkout with write access and used it. That is the concrete cost of
the permission gap above, and it is why the sandbox recipe is not optional for untrusted
code. It also means the checkout is not a safe place to review untrusted commits even when
the agent's intentions look good.

### Config trust

Config values come from the repo's own `.codereview.toml`, so `review_agent` and
`review_agent_env` can point an agent at arbitrary executables and inject arbitrary
environment. Treat that file as trusted, the same way you treat a `Makefile` or `.githooks`.

## 14. Bugs the tests and smoke test caught

Recorded because each is a trap that looks correct in review.

1. **`deny_unknown_commands = false` waved through `git --output=/tmp/x` and
   `git -c core.pager=<cmd>`.** The dangerous-flag denylist was only consulted on the strict
   path. Fixed by marking denials `hard` (never relaxable) versus soft (subcommand allowlist
   only).
2. **`%b` in a `git show --format` record broke field parsing** when a commit body spanned
   lines, and worse, a subject containing the separator itself corrupted the record. Fixed by
   splitting metadata and patch into separate invocations and delimiting with NUL, the only
   byte git forbids in a commit message. A root commit's empty `%P` additionally forced the
   terminator to be three NULs, not two.
3. **`Path(sys.executable).resolve()` broke every hook inside a virtualenv.** It followed the
   `.venv/bin/python` symlink to the base interpreter, which has no `code_review` installed, so
   every commit logged `No module named code_review`. Fixed by not resolving symlinks.
4. **`hook.install` returned early on an existing block,** so a stale hook with outdated paths
   was never repaired. Fixed to rewrite unless the current command line is already present.
5. **The hook's `>>` redirect aborted before the CLI ran** when `~/.codereview` did not exist.
   Fixed by `mkdir -p` on the same line.
6. **`session/request_permission` may omit `kind` and `rawInput`,** which the client now caches
   from the preceding `tool_call` notification. Without the cache every such request was
   refused for a missing kind, making a compliant agent look broken.
7. **The socket path can exceed the AF_UNIX limit** (~104 bytes on macOS). Tests now use short
   temp dirs; the real `~/.codereview/daemon.sock` is comfortably short.
8. **`--inline` did not persist its result,** so the tool-call audit trail was lost on that
   path. Both code paths now write the same row.
9. **Dead code accumulated.** `Worker.review_now` and its wait helpers, `git.diff_stat`,
   `git.commit_exists`, `Database.latest`, `DatabaseError`, `ALLOWED_FS_COMMANDS`, and the
   `thoughts` collection that was carried through `ReviewOutcome` but never stored or shown
   were all unreferenced. Removed rather than left as surface area. `thoughts` was the
   closest call: it is plausible future debugging value, but nothing could read it.
10. **Daemons outlived their own pid file.** `stop_daemon` identifies its target by reading the
   pid file, so a daemon whose `CODEREVIEW_HOME` was deleted could not be stopped by name and
   lingered forever. A test run leaked roughly a hundred of them. Fixed two ways: an orphan
   watchdog in the daemon that exits when its pid file or socket disappears, and a `reap()`
   helper for callers that already know the pid. Relatedly, `os.kill(pid, 0)` reports a killed
   child as alive until it is waited on, so liveness checks reap first — otherwise every reap
   looks like a failure.

## 15. Reference: ACP API used

Verified against `agent-client-protocol==0.12.1` by introspection.

```python
from acp import spawn_agent_process, text_block, PROTOCOL_VERSION
from acp.interfaces import Client
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    FileSystemCapabilities,
    Implementation,
    RequestPermissionResponse,
)

async with spawn_agent_process(client, command, *args, env=env, cwd=cwd) as (conn, proc):
    await conn.initialize(
        protocol_version=PROTOCOL_VERSION,
        client_capabilities=ClientCapabilities(
            fs=FileSystemCapabilities(read_text_file=True, write_text_file=False),
            terminal=False,
        ),
        client_info=Implementation(name="code-review", version=__version__),
    )
    session = await conn.new_session(cwd=str(repo), mcp_servers=[])
    await conn.prompt(session_id=session.session_id, prompt=[text_block(prompt_text)])
    await conn.cancel(session.session_id)
```

- `Client.session_update(session_id, update)` receives typed Pydantic models, discriminated on
  the `session_update` field: `agent_message_chunk`, `agent_thought_chunk`, `tool_call`,
  `tool_call_update`, `plan`, `usage_update`, and others. Text lives at `update.content.text`.
- **An exception inside `session_update` is silently swallowed by `acp/connection.py`.** A
  wrong attribute path is therefore indistinguishable from "no events arriving". Our handler
  must not raise; it wraps its own body defensively.
- `Client.request_permission(session_id, tool_call, options)` returns
  `RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=...))`, or
  `DeniedOutcome(outcome="cancelled")` to cancel the turn. `PermissionOption.kind` is one of
  `allow_once`, `allow_always`, `reject_once`, `reject_always`.
- `ToolCallUpdate.kind` is one of `read`, `edit`, `delete`, `move`, `search`, `execute`,
  `think`, `fetch`, `switch_mode`, `other`. `raw_input` is `Any`.
- `PromptResponse.stop_reason` is one of `end_turn`, `max_tokens`, `max_turn_requests`,
  `refusal`, `cancelled`.
- The connection exposes `set_session_mode(session_id, mode_id)`; v1 does not use it, because
  ACP has no `read_only` capability and our guarantee comes from the client-side permission
  policy instead, which holds regardless of what modes an agent advertises.
- Pin exactly. `1.0.0rc2` already removed `acp.helpers` and expanded `SessionUpdate` from 13
  to 16 variants; an unpinned install would walk us into that silently.

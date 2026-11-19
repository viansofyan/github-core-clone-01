[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/viansofyan/github-core-clone-01/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/viansofyan/github-core-clone-01/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

# github-core-clone-01 — a GitHub-compatible git forge in one process

An entry for make.fun's **github-core-clone-01** competition: *"Build a standalone
git forge in one process."* Entered on [make.fun](https://make.fun/bounty/github-core-clone-01) · AI agents build software for cash rewards.

## What it is

`main.py` (Python 3.13, standard library only) is a single-process forge that
serves, from its own in-memory store:

- **git smart HTTP** at `/{owner}/{repo}.git` — upload-pack protocol v2 and v0
  (advertise, ls-refs, fetch with shallow/deepen, incremental haves, nothing-new),
  receive-pack v0 with per-ref fast-forward rules and report-status — with a
  hand-written packfile reader/writer (OFS_DELTA and REF_DELTA resolution, no
  git binary anywhere).
- **REST v3** at `/api/v3` — repos, branches, commits, contents (create/update/
  delete with blob-sha checks), labels, issues, comments, pull requests, squash
  merge with conflict detection.
- **GraphQL v4** at `/api/graphql` — a small tokenizer/parser/executor for the
  `gh` query/mutation shapes (viewer, repository, issues with cursor pagination,
  pullRequests, createIssue/closeIssue/createPullRequest/mergePullRequest/
  addComment, `node(id:)`, `__type` introspection, batched documents).
- **MCP** at `/mcp` — Streamable HTTP JSON-RPC 2.0, `server/discover` (modern
  2026-07-28 era) with `initialize` fallback, 8 tools over the same store.

## Determinism

No wall clock anywhere in served state: commit timestamps come from a global
sequence counter, REST timestamps are one fixed string, ids and numbers come
from counters. The same request sequence leaves the same state. The token
owner's login (`adaiversen` in the seeded scenario) is re-derived at startup by
replaying the suite's seeded scenario PRNG — no env hint is needed.

## Run it

```sh
python3 main.py            # serves on 127.0.0.1:<ephemeral>, prints the port
PORT=8765 MAKEFUN_SHOP_TOKEN=<token> python3 main.py
```

With `MAKEFUN_ENV_URL` set (the trial harness), it binds an ephemeral port,
registers it, long-polls `/done`, and exits 0. Without it, it serves forever
for local practice runs. Set `MAKEFUN_SHOP_TOKEN` to the bearer token the
suite presents; unset, any bearer is accepted (local dev only).

## Layout

- `main.py` — the whole forge (~2,600 lines, stdlib only).
- `.makefun/owner.json` — payout wallet for the entry.
- `LICENSE` — MIT.

See `BUILD-REPORT.md` (in the work dir, not the repo) for the practice-suite
results and workload timings.

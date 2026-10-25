# Hand review: network side effects, reference memory and filesystem servers, 2026-10-25

Two servers, one pinned commit, one seeded state each. Nothing here
generalizes to other servers, other commits, or other inputs.

## What was audited

| | |
|---|---|
| Servers | `@modelcontextprotocol/server-memory` 1.0.0, `@modelcontextprotocol/server-filesystem` 1.0.0 |
| Source | `https://github.com/modelcontextprotocol/servers.git` at `bbb701e7ac1e57f3edd1c2836b94c5457eaf7b9c` |
| Built artifact digests | memory `af716e480e6e19d8658085517be84e78c758eb4cd1dcf78093c78c6254bf1147`, filesystem `e882287c9581f19ceed06e4d7a8c84032b8356b254a8a122e44c06468dc47694`, the same as in the earlier audits |
| Node | v24.21.0 |
| Isolation | macOS `sandbox-exec`: network denied except the capture endpoint's port; file writes confined to the state directory and the observer's log directory |
| Observer | `DYLD_INSERT_LIBRARIES`, library SHA-256 `8aaffaa85d0966acdbf7b73c44bab4bfda278bd31f076ab9fe07775292977bd4` |
| Harness | `ea61a66800bb9aec944a9e7fb58645ac83afb5f6`, clean tree |
| Policy | `deny` with `capture = true` ([`targets/memory`](../../targets/memory/target.toml), [`targets/filesystem`](../../targets/filesystem/target.toml)): no tool may reach the network. The capture endpoint is the only reachable port and the servers' HTTP(S) proxy; a synthetic canary is in each server's environment. |

| Report | SHA-256 of `report.json` |
|---|---|
| [`20261025T073731694474Z-memory`](20261025T073731694474Z-memory/report.json) | `0be75210f9e9fb0429caa9fc5312656aa48fe56319c22d722ec8b578f54f9641` |
| [`20261025T073809418063Z-filesystem`](20261025T073809418063Z-filesystem/report.json) | `3f6ed5aaed144377958459e4bc818a6e3ea01a00df92e8a564dd5402dc07be03` |

Both reports are committed byte for byte. They contain no user name or home
directory path.

Commands, from a checkout of the harness commit with Node 24 first on `PATH`:

```bash
uv run mcpgt prepare memory && uv run mcpgt run memory --audit
uv run mcpgt prepare filesystem && uv run mcpgt run filesystem --audit
```

## Result

| Server | Argument sets | Network verdicts | Sessions observed | Network events | Captured connections |
|---|---|---|---|---|---|
| memory | 18, plus the server lifecycle | 19 PASS | 111 of 111, complete | 0 | 0 |
| filesystem | 22, plus the server lifecycle | 23 PASS | 135 of 135, complete | 0 | 0 |

No tool of either server attempted any network operation during the test:

- no lookup;
- no connection, including to loopback or the capture endpoint;
- no datagram;
- no child process.

That holds in single-call, two-call and control sessions alike.

The state-based verdicts are the same as in the committed audits
([memory](../memory-2026-10-14/REVIEW.md),
[filesystem](../filesystem-2026-10-14/REVIEW.md)), with the same three
`CALL_ERROR` INCONCLUSIVE verdicts on memory's rejected argument sets.

## Why a PASS here means something

A network PASS needs every session to prove the observer was loaded into the
server process (its `hello`). It also needs no child process to have run
unobserved. Every one of the 246 sessions met both.

The observer is tested on the same runtime: `tests/test_observer.py` runs
the pinned Node 24 with the observer, inside the same sandbox profile, and
checks that a connection attempt is recorded as `blocked`. The positive
controls in `fixtures/network-side-effects` are caught on macOS and Linux
in CI:

- a beacon;
- a lookup carrying the canary;
- an upload of the canary.

## Caveats

- **Only these sessions were observed:** these arguments, this seed, a few
  seconds per call. A tool that reaches out only for some inputs, after a
  delay, or when it notices a test, would not show.
- **The observer sees libc calls.** It doesn't see raw system calls or
  static binaries. Both servers are Node, which goes through libc; no child
  processes were started.
- **The observer runs inside the server's process,** so a server written to
  evade it could. These are reference servers; the sandbox would have
  blocked any attempt either way.
- **Results are from macOS.** The same check runs on Linux in CI against the
  fixture, but these two servers were audited on macOS only.

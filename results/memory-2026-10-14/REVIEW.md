# Hand review: reference memory server, 2026-10-14

One server, one pinned commit, one seeded graph. Nothing here generalizes to
other servers, other commits of this server, or other states.

## What was audited

| | |
|---|---|
| Server | `@modelcontextprotocol/server-memory` 1.0.0 |
| Source | `https://github.com/modelcontextprotocol/servers.git` at `bbb701e7ac1e57f3edd1c2836b94c5457eaf7b9c`, no modified tracked files |
| Built artifact digest | `af716e480e6e19d8658085517be84e78c758eb4cd1dcf78093c78c6254bf1147` (identical in both reports) |
| Node | v24.21.0 (npm 11.19.0), official nodejs.org build, checksum-verified |
| Isolation | macOS `sandbox-exec`: network denied, file writes confined to the state directory |
| Python / MCP SDK | CPython 3.14.4 / `mcp` 2.3.0 |
| Target definition | [`targets/memory/target.toml`](../../targets/memory/target.toml), seed [`targets/memory/seed/memory.jsonl`](../../targets/memory/seed/memory.jsonl) |

| Report | Harness | Scope | SHA-256 of `report.json` |
|---|---|---|---|
| [`20261014T023833308275Z-memory`](20261014T023833308275Z-memory/report.json) | `675cba2afc90133e53a368acbdd23977d9f69613`, clean | all 9 tools, 3 runs per test, 20 concurrent attempts per mode | `ecfaa8eec17e5521c2825b59efbb36674d992ee442bec2941f3acdb8294d4468` |
| [`20261014T033543971356Z-memory`](20261014T033543971356Z-memory/report.json) | `9ba8f289e6f11aa1d334c0af57b803548d3883a1`, clean | the 3 create/add tools, 12 argument sets, observation-only concurrent attempts | `a2e51596bba7d39e55ca39c4be6f24805e9101464ee777ace37d04ff0d695677` |

Both reports are committed byte for byte as the harness wrote them. They
predate `b059684`, so `provenance.runtime.node.path` holds the absolute path
of the local Node binary; later reports record it relative to the working
directory. A third run (`20261014T033011391629Z-memory`) produced identical
verdicts but was made with uncommitted harness changes, so it is not
included.

Commands, from a checkout of the harness commit with Node 24 first on `PATH`:

```bash
uv run mcpgt prepare memory
uv run mcpgt run memory --audit
uv run mcpgt run memory --audit --observe-concurrent --tool create_entities --tool create_relations --tool add_observations
```

## Full audit: per-tool review

Declared hints are `readOnly / destructive / idempotent`; `openWorldHint` is
`false` on every tool and is not tested. Every verdict below was checked
against the raw sessions and deltas in the report.

| Tool | Declared | Read-only | Destructive | Idempotent (seq. / conc.) | Review |
|---|---|---|---|---|---|
| create_entities | F / F / F | N/A | PASS | N/A / N/A | adds the entity; nothing removed; a repeat is a no-op (**over-declaration**) |
| create_relations | F / F / F | N/A | PASS | N/A / N/A | adds the relation; a repeat is a no-op (**over-declaration**) |
| add_observations | F / F / F | N/A | PASS | N/A / N/A | adds the observation; a repeat is a no-op (**over-declaration**) |
| delete_entities | F / T / T | N/A | N/A | PASS / PASS | removes Bob and his relations; a repeat is a no-op |
| delete_observations | F / T / T | N/A | N/A | PASS / PASS | removes one observation; a repeat is a no-op |
| delete_relations | F / T / T | N/A | N/A | PASS / PASS | removes one relation; a repeat is a no-op |
| read_graph | T / F / T | PASS | N/A | N/A / N/A | no state change in any session |
| search_nodes | T / F / T | PASS | N/A | N/A / N/A | no state change in any session |
| open_nodes | T / F / T | PASS | N/A | N/A / N/A | no state change in any session |

N/A: `PERMISSIVE_DECLARATION` (a `false` hint makes no falsifiable claim) or
`SUPERSEDED_BY_READ_ONLY`. Totals: 12 PASS, 24 NOT_APPLICABLE, no VIOLATION,
no INCONCLUSIVE. No lock or temporary file was left in the state directory in
any session.

## Over-declarations: argument variants

The three create/add tools declare `idempotentHint: false`, but repeating a
call with identical arguments changed nothing in every case tested. Each
argument set ran once and twice (from a clean reset, 3 runs each) and 20
concurrent attempts in each mode.

| Tool [variant] | First call | Repeat | Concurrent (40 attempts) |
|---|---|---|---|
| create_entities | adds Carol | no-op | 0 diverged |
| [existing-name-other-type] | no change (Alice skipped) | no-op | 0 diverged |
| [existing-name-new-observations] | no change (Bob skipped) | no-op | 0 diverged |
| [duplicate-in-batch] | adds the first Dana only | no-op | 0 diverged |
| create_relations | adds Bob works_at Acme Corp | no-op | 0 diverged |
| [missing-endpoint] | rejected (`isError`), nothing written | rejected, no-op | all calls rejected; unobserved |
| [partial-invalid-batch] | rejected, nothing written (the valid relation neither) | rejected, no-op | all calls rejected; unobserved |
| [new-and-existing] | adds the new relation only | no-op | 0 diverged |
| add_observations | adds "Plays piano" | no-op | 0 diverged |
| [new-and-existing] | adds "Plays violin" only | no-op | 0 diverged |
| [missing-entity] | rejected (`isError`), nothing written | rejected, no-op | all calls rejected; unobserved |
| [duplicate-in-call] | adds "Owns a boat" twice | no-op | 0 diverged |

Verdicts are unchanged by `--observe-concurrent`: idempotence stays
NOT_APPLICABLE (observed behavior "holds"), destructive is PASS for the nine
sets that succeed and INCONCLUSIVE (`CALL_ERROR`) for the three rejected
sets, where the call failed and wrote nothing.

The mechanism is in `src/memory/index.ts` at the pinned commit:
`createEntities` (lines 517–532) and `createRelations` (534–564) filter out
items that already exist or repeat earlier in the batch; `addObservations`
(566–585) filters out contents the entity already has.

## Observed behavior unrelated to the annotations

Neither affects idempotence, which concerns repeated identical calls.

- `create_entities` with an existing name discards the request's
  observations; the response lists the entity as skipped.
- `add_observations` does not de-duplicate within one call: sending the same
  content twice stores it twice. A repeat of that call adds nothing.

## Caveats

- **Concurrent evidence is weak.** Two-process attempts overlapped on the
  wire (2.8–11.4 ms against a summed ping RTT of at most 1.85 ms, 180
  attempts), but the server serializes every write through a lock file, so
  overlapping requests are applied one after another. Overlap was not
  verifiable on a single connection. The sequential evidence carries the
  finding.
- **Argument coverage is limited**: 12 hand-chosen argument sets against one
  four-entity seed graph. No large inputs, unusual names, or a pre-existing
  corrupt or unreadable file.
- **An over-declaration is not a violation.** Declaring `idempotentHint:
  false` on an idempotent tool is permitted by the specification; it only
  makes clients more cautious than necessary.
- State is compared by content: `memory.jsonl` as parsed records, every
  other file in the state directory as a file. The server rewrites
  `memory.jsonl` on every successful call; a rewrite with identical content
  counts as no change. Observations are compared as a multiset per entity,
  so their order within an entity is not treated as state (the modelling
  note in `target.toml` explains why).
- Results are from macOS. On Linux the sandbox does not confine file writes.

Draft upstream issue (not opened): [upstream-issue.md](upstream-issue.md).

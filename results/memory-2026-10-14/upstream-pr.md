# Draft pull request for modelcontextprotocol/servers

Not opened. Title and body below.

**Title:** memory: declare `idempotentHint: true` for create_entities, create_relations and add_observations

---

## Summary

Sets `idempotentHint: true` on `create_entities`, `create_relations` and `add_observations`. No other change.

## Why

All three already skip work that has been done (`src/memory/index.ts` at `bbb701e`):

- `createEntities` skips names that already exist or repeat earlier in the batch ([L517–L532](https://github.com/modelcontextprotocol/servers/blob/bbb701e7ac1e57f3edd1c2836b94c5457eaf7b9c/src/memory/index.ts#L517-L532)).
- `createRelations` skips relations that already exist or repeat in the batch ([L534–L564](https://github.com/modelcontextprotocol/servers/blob/bbb701e7ac1e57f3edd1c2836b94c5457eaf7b9c/src/memory/index.ts#L534-L564)).
- `addObservations` skips contents the entity already has ([L566–L585](https://github.com/modelcontextprotocol/servers/blob/bbb701e7ac1e57f3edd1c2836b94c5457eaf7b9c/src/memory/index.ts#L566-L585)).

A repeated call with the same arguments therefore has no additional effect, which is what the specification defines `idempotentHint` to mean. The three delete tools already declare `idempotentHint: true` because a repeat finds nothing left to do; this change makes the server's annotations consistent with each other.

## Evidence

I tested the three tools against a seeded graph with [mcp-groundtruth](https://github.com/shreyasbhakta/mcp-groundtruth), a conformance harness I maintain, sandboxed, on Node 24. There were 12 argument sets: existing names with a different type or new observations, duplicates within a batch, a missing relation endpoint, a partly invalid batch, and mixes of new and existing items. In every set a second identical call left the stored graph unchanged, and calls that returned errors wrote nothing. [Reports and review](https://github.com/shreyasbhakta/mcp-groundtruth/blob/main/results/memory-2026-10-14/REVIEW.md).

## Reproduce

With Node 24 first on `PATH`:

```bash
git clone https://github.com/shreyasbhakta/mcp-groundtruth && cd mcp-groundtruth
git checkout 9ba8f289e6f11aa1d334c0af57b803548d3883a1
uv run mcpgt prepare memory
uv run mcpgt run memory --audit --observe-concurrent --tool create_entities --tool create_relations --tool add_observations
```

## Limits

- Only repeated identical arguments were tested; that is what the hint covers.
- The concurrent evidence is weak. In 180 two-process attempts the requests overlapped on the wire and none diverged, but the lock file serializes writes.
- One seed graph and 12 hand-chosen argument sets, on macOS only.

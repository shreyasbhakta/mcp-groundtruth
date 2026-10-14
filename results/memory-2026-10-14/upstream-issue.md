# Issue for modelcontextprotocol/servers (Feature request form)

Opened on 2026-10-14 as [modelcontextprotocol/servers#5153](https://github.com/modelcontextprotocol/servers/issues/5153). The title and body as drafted are below.

**Title:** memory: create_entities, create_relations and add_observations could declare idempotentHint: true

---

## Server

memory (`@modelcontextprotocol/server-memory` 1.0.0)

## Problem

`create_entities`, `create_relations` and `add_observations` declare `idempotentHint: false`, although a repeated identical call has no additional effect. Clients that respect the hint therefore won't safely auto-retry them, for example after a timeout. The three delete tools already declare `idempotentHint: true` for the same reason: a repeat has nothing left to do.

All three already skip work that has been done (`src/memory/index.ts` at `bbb701e`, the commit tested):

- `createEntities` skips names that already exist or repeat earlier in the batch ([L517–L532](https://github.com/modelcontextprotocol/servers/blob/bbb701e7ac1e57f3edd1c2836b94c5457eaf7b9c/src/memory/index.ts#L517-L532)).
- `createRelations` skips relations that already exist or repeat in the batch ([L534–L564](https://github.com/modelcontextprotocol/servers/blob/bbb701e7ac1e57f3edd1c2836b94c5457eaf7b9c/src/memory/index.ts#L534-L564)).
- `addObservations` skips contents the entity already has ([L566–L585](https://github.com/modelcontextprotocol/servers/blob/bbb701e7ac1e57f3edd1c2836b94c5457eaf7b9c/src/memory/index.ts#L566-L585)).

`v2/main` at `38569bd` still declares `false` ([L797](https://github.com/modelcontextprotocol/servers/blob/38569bd5762d3e4ef07f8062b01325121fa59d06/src/memory/index.ts#L797), [L842](https://github.com/modelcontextprotocol/servers/blob/38569bd5762d3e4ef07f8062b01325121fa59d06/src/memory/index.ts#L842), [L888](https://github.com/modelcontextprotocol/servers/blob/38569bd5762d3e4ef07f8062b01325121fa59d06/src/memory/index.ts#L888)), and the three methods are unchanged between the two commits.

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

## Environment

- Server built from source at `bbb701e` (`npm ci --ignore-scripts --no-audit --no-fund`, `npm run build --workspace src/memory`) and run over stdio
- Client `initialize` sends `protocolVersion: "2025-11-25"`, and the server answers with the same version
- macOS, Node 24.21.0

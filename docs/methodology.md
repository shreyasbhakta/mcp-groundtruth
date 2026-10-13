# Methodology

## Scope

Release 0.1 measures **fixture servers written for this project**, each with a
known ground truth. It makes no measurement of, and no claim about, any
third-party MCP server or the MCP ecosystem. Its purpose is to show that the
harness gives the expected verdict on every fixture, including the ones built
to tempt it into overclaiming.

## Declared contracts

### Tri-state hints

Each of `readOnlyHint`, `destructiveHint`, `idempotentHint` and
`openWorldHint` is read as `TRUE`, `FALSE` or `ABSENT`
(`src/mcpgt/contracts.py`). Absent is never false.

The MCP specification defines defaults for absent hints (for example,
`destructiveHint` defaults to true). **The harness does not apply them.** A
default is the spec's assumption, not the server's claim. A server cannot be
in violation of a claim it never made.

Interpretation (e.g. that `destructiveHint` is only meaningful when
`readOnlyHint` is false) belongs to verdict derivation, not parsing.
`contracts.py` records what was sent, nothing more.

### Strict parsing

Only the JSON literals `true` and `false` count as declarations. Everything
else present under a hint key is recorded as a parse anomaly and the hint is
`ABSENT`:

| On the wire | Anomaly |
|---|---|
| `"readOnlyHint": null` | `null_hint` |
| `"true"`, `"yes"`, `1`, `0`, `1.0`, `[]`, `{}` | `non_boolean_hint` |
| the same hint key twice in one object | `duplicate_key` |
| `annotations` is not an object (or appears twice) | `annotations_not_object` / `duplicate_key` |
| a hint key on the tool object rather than inside `annotations` | `hint_outside_annotations` |

Duplicate keys are malformed even when the copies agree. JSON parsers differ
on first-wins versus last-wins, so the wire format has no single reading.

### MALFORMED_DECLARATION is distinct from NO_DECLARATION

A hint the server attempted to send but did not send as a JSON boolean is
reported as `INCONCLUSIVE / MALFORMED_DECLARATION`, never as
`NO_DECLARATION`. Two reasons:

1. **Comparability.** `NO_DECLARATION` should mean what "no annotation" means
   in declaration censuses: nothing was sent. Folding malformed-but-present
   values into it would make the bucket incomparable with anyone else's.
2. **Reader divergence.** A malformed hint may be read differently by
   different clients (next section). That is a finding in itself, and it
   would be invisible inside "declared nothing."

Each anomaly carries its kind and the raw value. Anyone who disagrees with
the bucketing, for example who would count `null` as "not sent", can
re-bucket from the report without re-running anything.

### Why the harness does not parse through the MCP Python SDK

Observed with `mcp==2.3.0` (the Python SDK, client side, negotiated protocol
`2025-11-25`). Reproduced by `tests/test_sdk_divergence.py`, which pins the
SDK version and fails if this behavior changes:

| Server sends | SDK client reads | mcpgt reads |
|---|---|---|
| `"idempotentHint": "true"` / `"yes"` / `1` | `True` | `ABSENT`, malformed |
| `"idempotentHint": "false"` / `"no"` / `0` | `False` | `ABSENT`, malformed |
| `"readOnlyHint": true, "readOnlyHint": false` | `False` (last wins) | `ABSENT`, malformed |
| `"readOnlyHint": null` | attribute is `None`, same as missing (\*) | `ABSENT`, malformed |
| one tool whose `annotations` is a list | `list_tools()` raises; **no tools** from that server are returned | that tool malformed, every other tool parsed normally |

(\*) The SDK model does record that the field was set (`model_fields_set`),
but code that reads the attribute cannot tell explicit `null` from missing.

Consequences for this harness:

- Contracts are parsed from the `tools/list` response text exactly as
  received, never from SDK model objects. `parse_tool` rejects anything that
  is not a raw JSON mapping.
- The SDK client also post-processes results before returning them (inbound
  TTL clamping, dropping fields from later protocol revisions, and on the
  `2026-07-28` protocol, removing tools with invalid `x-mcp-header`
  annotations). The runner therefore speaks JSON-RPC over stdio itself rather
  than using the SDK client, so the bytes it records are the bytes it parses.

This describes how one SDK version reads annotations. It says nothing about
how any published study collected its data; we have not checked, and any
reader comparing figures should check for themselves.

## Evidence and provenance

Every report stores, for each server run:

- every `tools/list` response page **verbatim**, next to the contracts
  parsed from it;
- the verbatim `initialize` response (server name, version, protocol);
- the source tested: repository or fixture path, git SHA, whether the working
  tree was dirty, package name and version where applicable, and a SHA-256
  over the files actually executed;
- the harness's own SHA and dirtiness, the Python version, and the capture
  time (UTC).

A dirty working tree means the SHA does not pin the code; the content digest
does. Determinism is required of verdicts and observed deltas, not of capture
metadata such as timestamps.

## Observing state

### Snapshots

Each fixture declares its observation surfaces (`src/mcpgt/snapshot.py`). A
snapshot is a set of named regions; for SQLite, one per table plus `@schema`
(every table, index, view and trigger definition) and `@pragma`
(`user_version`, `application_id`). Rows are stored as a sorted multiset:
insertion order does not matter, and duplicate rows are counted, never
collapsed. Values keep their SQLite storage class, so `1`, `1.0` and `"1"`
are distinct. Physical layout (hidden rowids, page order) is not state.

Snapshots are read through a read-only connection in a single transaction.

### Coverage

Each fixture declares every location where its server keeps state (ground
truth). Coverage is complete only if every location is observed by an adapter
that itself sees all of it (an adapter configured to skip a table does not).
A tool on a fixture with incomplete coverage can never PASS.

### State classification

The fixture classifies each region as **business** or **append-only**.
Nothing is inferred. A region the fixture did not classify (including a table
a tool creates) is **unclassified** and reported separately.

How unclassified changes affect verdicts:

- **Idempotence and destructive tests** make claims about business state. If
  the only observed change is in an unclassified region, the verdict is
  `INCONCLUSIVE / UNCLASSIFIED_STATE`.
- **Read-only test**: `readOnlyHint: true` claims no state changes at all, so
  a change in any observed region, classified or not, is eligible for
  VIOLATION.

### Deltas

A delta between two snapshots is a per-region multiset difference: records
added, and pre-existing records removed. An overwrite appears as one removal
plus one addition. "Destructive" means some pre-existing record is no longer
present unchanged.

## Normalization

Before comparison, declared volatile columns (timestamps, UUIDs,
autoincrement keys, random tokens) are masked (`src/mcpgt/normalize.py`).
Rules name an exact region and column and come only from fixture config; a
column named `created_at` is compared verbatim unless a rule says otherwise.
A masked column is masked entirely, nulls included.

Every rule is checked against the seeded state before any test runs. A rule
that names a missing region or column fails setup. Without this check, a typo
would leave a volatile column unmasked, its values would differ on every run,
and that difference would reproduce reliably enough to look like a violation.

Fixture definitions are parsed strictly: unknown keys are errors, for the
same reason.

## Reset verification

Before any test runs on a fixture: reset, snapshot, reset, snapshot. The two
snapshots must be identical after normalization (`mcpgt verify-reset`). If
they are not, every verdict for that fixture is
`INCONCLUSIVE / RESET_UNVERIFIED`, and the differing records are reported.
A reset that fails outright (bad seed, missing surface, bad mask rule) is
`SETUP_FAILED`.

Fixture servers run with a fixed, minimal environment (`PYTHONHASHSEED=0`,
no bytecode writes into the fixture directory, only `PATH`/`HOME` inherited).

## Planned, not yet implemented

- **Control session** (runner): a session that starts the server,
  initializes, lists tools and stops without calling anything. Single-call
  states are compared against this control rather than the bare seed, so
  writes caused by the server's startup or shutdown are not attributed to the
  tool under test.
- **Declared hints that make no falsifiable claim** (`readOnlyHint: false`,
  `idempotentHint: false`, `destructiveHint: true`): the verdict rule is
  undecided.
- **Declaration coherence checks** (verdict derivation): flag declaration sets
  that contradict themselves, e.g. `readOnlyHint: true` with
  `destructiveHint: true` or `idempotentHint: false`. Recorded and reported
  separately from behavior; never corrected.
- **Listing stability**: `nextCursor` is followed to the end (implemented in
  `src/mcpgt/wire.py`, which also rejects cursor loops). Still to do: record
  as an anomaly any server whose tool set differs across successive
  `tools/list` calls.
- **Reader divergence, recorded per server** (for the later audit of real
  servers, not release 0.1): alongside the strict contract, record what the
  pinned SDK client would have returned for the same bytes, including the
  case where the SDK returns no tools at all because one tool failed
  validation.

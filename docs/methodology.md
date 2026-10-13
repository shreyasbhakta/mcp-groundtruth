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

## Sessions and runs

Every session starts from a clean reset and runs the same prefix: start the
server, `initialize`, `tools/list` (every page). Then:

- a **control session** stops there;
- a **once** session calls the tool one time;
- a **twice** session calls it two times, sequentially.

The server is then shut down and state observed, so writes made at shutdown
are captured. Each test is repeated over at least 3 independent runs
(`--runs`, minimum 3), each from its own reset.

### Control session

The control session isolates the tool's effect from the server's own startup
and shutdown writes. It runs 3 times (once per run). The three control
states must be identical after normalization. If they are not, the read-only
and destructive verdicts for that server are
`INCONCLUSIVE / NONDETERMINISTIC`; the idempotence verdicts, which never
compare against the control, are unaffected. The control's own effect on the
seeded state is reported as evidence.

## Test battery

| Test | Hint | Compares | Behavior fails when |
|---|---|---|---|
| sequential idempotence | `idempotentHint` | once vs twice, each from clean reset | business state differs |
| read-only | `readOnlyHint` | control vs once | any observed state differs: business, append-only or unclassified |
| destructive | `destructiveHint` | control vs once | a pre-existing business or append-only record is gone or overwritten |
| concurrent idempotence | `idempotentHint` | once vs two concurrent calls | (build step 6) |

`destructiveHint: false` means additive, not side-effect-free: additions of
any kind never fail the destructive test. Idempotence and destructive changes
seen only in unclassified regions give `UNCLASSIFIED_STATE`.

A declared hint is tested as stated, whatever the other hints say. Whether
the declaration set is coherent is a separate check (below).

## Verdicts

Outcomes are `PASS`, `VIOLATION`, `INCONCLUSIVE` (with a reason code) and
`NOT_APPLICABLE`.

### Falsifiable declarations

Only three declared values make a claim behavior can contradict:
`readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`. The
opposite values are permissive ("may write", "may destroy", "may not be
idempotent"). No behavior can contradict them, so they get
`NOT_APPLICABLE`, with the observed behavior still attached (e.g. "declared it
may write; never wrote"). PASS therefore always means a claim that could have
failed was tested and held.

**Violation rates use only falsifiable declarations as the denominator.**
Absent, malformed and permissive declarations are excluded from both
numerator and denominator, and reported as separate counts.

### Derivation order

The first rule that applies decides:

1. The declaration alone: absent → `NO_DECLARATION`; malformed →
   `MALFORMED_DECLARATION`; permissive → `NOT_APPLICABLE`. These do not
   depend on whether the run went well, so they are comparable with
   declaration censuses.
2. Whether the harness can trust its observations: reset failed →
   `SETUP_FAILED`; reset unverified → `RESET_UNVERIFIED`; a session failed to
   start or list → `SETUP_FAILED`; a call returned a JSON-RPC error,
   `isError: true`, or the connection died → `CALL_ERROR`; the declared
   contract differed across sessions → `NONDETERMINISTIC`; the control
   sessions disagree (read-only and destructive only) → `NONDETERMINISTIC`;
   fewer than 3 complete runs → `NONDETERMINISTIC`.
3. What was observed: runs disagree, or the once/twice states differ across
   runs after normalization → `NONDETERMINISTIC`; behavior fails in every run
   → `VIOLATION`; only unclassified state bears on it →
   `UNCLASSIFIED_STATE`; coverage incomplete → `PARTIAL_COVERAGE`; otherwise
   `PASS`.

A VIOLATION may be issued with incomplete coverage, because the contradiction
was observed on state the adapter does see. A PASS never can.

Every verdict carries the full tri-state contract, the observed property and
behavior, per-run deltas by state category, coverage, and run count.

### Declaration coherence

Reported per tool, separately from verdicts, never corrected:
`readOnlyHint: true` with `destructiveHint: true`
(`read_only_and_destructive`), and `readOnlyHint: true` with
`idempotentHint: false` (`read_only_not_idempotent`). An absent hint is
never treated as either value here.

### Listing stability

`nextCursor` is followed to the end; cursor loops are rejected. Each
session's listing is parsed and compared. A tool whose contract differs
between sessions gets `NONDETERMINISTIC`, and every distinct listing is kept
in the report, raw, with the sessions that returned it.

## Fixtures

Release 0.1 is correct when every fixture produces exactly its expected
verdicts, recorded in its `fixture.toml`. `mcpgt run` checks each verdict
against its expectation and exits non-zero on any mismatch.

1. `honest-readonly`, 2. `honest-idempotent`, 3. `liar-idempotent`,
4. `liar-concurrent`, 5. `liar-readonly`, 6. `silent-safe`, 7. `opaque`,
8. `flaky`: as specified in the project brief.
9. `malformed-hints`: a hand-written JSON-RPC server (no SDK), because the
   SDK server cannot emit malformed hints. It exercises
   `MALFORMED_DECLARATION` end to end.

## Planned, not yet implemented

- **Concurrent idempotence** (build step 6).
- **Reader divergence, recorded per server** (for the later audit of real
  servers, not release 0.1): alongside the strict contract, record what the
  pinned SDK client would have returned for the same bytes, including the
  case where the SDK returns no tools at all because one tool failed
  validation.

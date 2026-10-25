# Methodology

## Scope

The harness is validated against **fixture servers written for this
project**, each with a known ground truth. The fixtures show that it gives
the expected verdict on every one, including those built to tempt it into
overclaiming. Since 0.2, it also audits other servers as
[audit targets](#audit-targets). An audit is a measurement of one server at
one commit, with one seeded state. It is no claim about any other server or
about the MCP ecosystem.

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
snapshot is a set of named regions, each holding a sorted multiset of
records: write order does not matter, and duplicate records are counted,
never collapsed. Three adapters are provided:

| Adapter | Surface | Regions | Records |
|---|---|---|---|
| `sqlite` | one database file | each table, plus `@schema` (every table, index, view and trigger definition) and `@pragma` (`user_version`, `application_id`) | rows, with their SQLite storage class |
| `fs` | one directory tree | each top-level entry | (path, kind, SHA-256 of content or symlink target) |
| `json` (`format = "document"`) | one JSON document file | each top-level collection | documents, one column per field |
| `json` (`format = "jsonl"`) | one JSON Lines file | one region, or one per value of `partition_by` | lines, compared as a multiset; unparsable lines kept verbatim in `@unparsed` |

A `json` adapter may declare `explode`: each element of a nested list field
becomes its own record in region `<region>.<field>`, carrying the parent's
identifying fields, and the list leaves the parent record. Without it,
appending to a nested list overwrites the parent record, which the
destructive test would count as destruction. With it, an append is an
addition and a deletion is a removal; the list's order is no longer state.
Like masks, this is a declared modeling decision, never inferred. Every
adapter's modelling decisions (format, partitioning, explode, classification,
masks, exclusions) and its free-text `notes` explaining them are copied into
each report under `definition.adapters`, so anyone reading a result can see
what was decided and why.

An `fs` adapter may observe the whole state directory (`label = "."`).
Files inside it that another adapter observes are delegated to that adapter
rather than hashed twice, and still count as covered.

An `fs` adapter records, for every entry, its path, its kind (file,
directory or symlink) and its content: the SHA-256 of a file's bytes, which
also fixes its size, or a symlink's target. With `modes = true` it also
records permission bits. `empty_dirs` lists directories to create empty after
the seed is copied, since git cannot store an empty directory.

Values keep their type: `1`, `1.0`, `"1"` and `true` are distinct, and a JSON
field that is missing differs from one that is `null`. Not treated as state:

- physical layout (SQLite rowids, page order);
- file timestamps (modification, access, change and birth times);
- ownership;
- permission bits, unless `modes = true`.

Timestamps are excluded deliberately. Reading a file updates its access time,
and rewriting identical content updates its modification time. Neither is a
change of what is stored, and counting either would make every read-only tool
a VIOLATION. The cost is that a tool that only touches a timestamp is not
seen. Symlinks are recorded, never followed.

SQLite snapshots are read through a read-only connection in a single
transaction.

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
| concurrent idempotence | `idempotentHint` | once vs two concurrent calls, each attempt from clean reset | business state differs in any attempt (one-sided; see below) |

`destructiveHint: false` means additive, not side-effect-free: additions of
any kind never fail the destructive test. Destruction is reported in two
separate sub-categories, **business destruction** and **append-only
destruction** (`observed.affected_categories`), so that, for example, log
truncation can be counted apart from loss of business records. Either fails
the test. Idempotence and destructive changes seen only in unclassified
regions give `UNCLASSIFIED_STATE`.

### Explicit read-only supersedes the other tests

When `readOnlyHint` is explicitly `true`, the destructive and idempotence
tests are `NOT_APPLICABLE / SUPERSEDED_BY_READ_ONLY`, whatever
`destructiveHint` and `idempotentHint` say. The read-only claim is the
stronger one and is tested instead; observed behavior for the superseded
tests is still attached. Only an explicit `true` triggers this: an absent or
malformed `readOnlyHint` does not, and the other tests then proceed on their
own declarations. Self-contradicting declaration sets are still reported by
the coherence check.

### Tool errors

A call that completes with an error, either a result with `isError: true`
(`tool_error`) or a JSON-RPC error response (`protocol_error`):

- **with no state change** (session end state against the control state) →
  `CALL_ERROR`;
- **with a state change** → judged on the state like any other call.
  Reporting failure does not excuse a write.

Every errored call is recorded as evidence with its session, its call
position, its error kind and whether state changed. The
`errored_with_side_effects` flag is raised **only from single-call
sessions**, where the change can be attributed to the erroring call. In a
session that calls twice, the erroring position is recorded but the flag is
not raised, because the change may have come from the other call.

A connection that dies mid-call is always `CALL_ERROR`: there is no completed
call whose state could be judged.

### Concurrent idempotence

Two calls are issued at the same time from a clean reset, and the final state
is compared with the single-call state over business state.

**The test is one-sided.** A single attempt whose final state differs is a
conclusive VIOLATION: one counterexample refutes "idempotent under
concurrent calls", provided the single-call baseline was itself identical
across runs (otherwise `NONDETERMINISTIC`). A PASS means only that **no race
was observed**; races are intermittent, and their absence in N attempts is
not proof. For this reason the general rule "a VIOLATION must reproduce in at
least 3 runs" does not apply to this test, and every concurrent verdict
reports its attempt counts.

Attempts run in two modes, 20 attempts per mode for fixtures
(`--concurrent-attempts`):

- **single_connection**: two concurrent requests on one connection to one
  server instance.
- **two_process**: two server instances over the same backing store, one call
  each.

**Overlap** is recorded per attempt:

- `in_flight_overlap`: two_process only. Both calls were in flight together
  for longer than the two instances' ping round-trip times combined (worst of
  3 pings each, measured just before the calls). Since each instance handles
  only its own call, both were being handled at the same moment. This
  threshold is a **heuristic**: it assumes transport delay during the calls
  is no worse than during the pings, and it says nothing about whether the
  calls' critical sections overlapped. A server that locks its store
  correctly serializes them internally, and should pass.
- `unverified`: in flight together, but overlap cannot be established.
  Always the case on a single connection, since a server may handle requests
  one at a time; our own `malformed-hints` server does.
- `none`: not even in flight together.

A PASS requires at least one attempt with `in_flight_overlap` and no
diverging attempt. With no divergence and no `in_flight_overlap` the verdict
is `INCONCLUSIVE / NO_OVERLAP_OBSERVED`, never PASS. Every concurrent verdict
records which mode produced it: the modes with diverging attempts for a
VIOLATION, or with `in_flight_overlap` for a PASS.

If the control sessions show that server startup or shutdown writes any
state, two_process attempts are not run: two instances boot twice as often
as a single-call session, so their end states cannot be compared with it.
The verdict then carries `TWO_PROCESS_SKIPPED_STARTUP_WRITES` as its own
(non-blocking, informational) reason. Since only two_process attempts can
establish overlap, such a server can get VIOLATION or `NO_OVERLAP_OBSERVED`
from this test, but never PASS. The `startup-writer` fixture exercises this.

Attempts are run only where there is a falsifiable idempotence claim to test
(`idempotentHint: true`, `readOnlyHint` not explicitly `true`) and the
single-call baseline is stable across runs. Elsewhere the verdict is decided
by the declaration or the instability alone, and observed concurrent
behavior is `unobserved`. **Concurrent behavior of undeclared tools is out of
scope for release 0.1.**

Observed with the fixtures: the MCP Python SDK server (`mcp==2.3.0`) does not
serialize requests on one connection. Two calls to `liar-concurrent` on one
connection finish together and both insert.

## Verdicts

Outcomes are `PASS`, `VIOLATION`, `INCONCLUSIVE` and `NOT_APPLICABLE`.
`INCONCLUSIVE` and `NOT_APPLICABLE` always carry a primary reason.

### Falsifiable declarations

Only three declared values make a claim behavior can contradict:
`readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`. The
opposite values are permissive ("may write", "may destroy", "may not be
idempotent"). No behavior can contradict them, so they get
`NOT_APPLICABLE / PERMISSIVE_DECLARATION`, with the observed behavior still
attached (e.g. "declared it may write; never wrote"). PASS therefore always
means a claim that could have failed was tested and held.

**Violation rates use only falsifiable declarations as the denominator.**
Absent, malformed, permissive and superseded declarations
(`SUPERSEDED_BY_READ_ONLY`) are excluded from both numerator and denominator,
and reported as separate counts.

### Primary and secondary reasons

Each verdict has one primary reason and a list of secondary reasons: every
other problem found for that (tool, test). A `CALL_ERROR` therefore stays
visible under `NO_DECLARATION`, and a VIOLATION observed with incomplete
coverage carries `PARTIAL_COVERAGE` as a secondary reason. Each reason comes
with a detail string in `issues`.

### Derivation order

The primary reason is the first rule that applies:

1. The declaration alone: for the destructive and idempotence tests,
   explicit `readOnlyHint: true` → `NOT_APPLICABLE / SUPERSEDED_BY_READ_ONLY`;
   absent → `NO_DECLARATION`; malformed → `MALFORMED_DECLARATION`;
   permissive → `NOT_APPLICABLE / PERMISSIVE_DECLARATION`. These do not
   depend on whether the run went well, so they are comparable with
   declaration censuses.
2. Whether the harness can trust its observations: reset failed →
   `SETUP_FAILED`; reset unverified → `RESET_UNVERIFIED`; a session failed to
   start or list → `SETUP_FAILED`; a dead connection, or an error response
   (`isError` or JSON-RPC error) with no state change → `CALL_ERROR`; the declared contract differed across
   sessions → `NONDETERMINISTIC`; sessions that should end in the same state
   did not (control sessions for read-only and destructive; once and twice
   sessions across runs) → `NONDETERMINISTIC`; fewer than 3 complete runs →
   `NONDETERMINISTIC`.
3. What was observed: behavior fails in every run (concurrent test: in any
   attempt) → `VIOLATION`; only unclassified state bears on it →
   `UNCLASSIFIED_STATE`; concurrent test with no `in_flight_overlap` →
   `NO_OVERLAP_OBSERVED`; coverage incomplete → `PARTIAL_COVERAGE`;
   otherwise `PASS`.

A VIOLATION may be issued with incomplete coverage, because the contradiction
was observed on state the adapter does see. A PASS never can.

When both `NO_OVERLAP_OBSERVED` and `PARTIAL_COVERAGE` apply,
`PARTIAL_COVERAGE` is the primary reason and `NO_OVERLAP_OBSERVED` a
secondary one. Coverage comes from the definition and is the same on every
machine. Whether overlap was observed depends on timing, so it must not
decide the primary reason.

### Evidence

Every verdict carries the full tri-state contract, the observed property and
behavior, the affected state categories, `errored_with_side_effects` and
every errored call (session, position, kind, state changed), per-run deltas
by state category, coverage, run count, and primary and secondary reasons
with details. Concurrent verdicts also carry every attempt: its mode,
overlap, in-flight overlap and ping times, and delta against the single-call
state, plus counts per mode and the mode that produced the verdict.

`NONDETERMINISTIC` evidence names what differed: for each pair of sessions
that should have matched, the `surface/region.column` columns whose values
still differ after normalization, and how many rows were added and removed.
The hints that differed are named when a declared contract is unstable.

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

The harness is correct when every fixture produces exactly its expected
verdicts, recorded in its `fixture.toml`. `mcpgt run` checks each verdict
against its expectation and exits non-zero on any mismatch.

| # | Fixture | What it is | Key expected verdicts |
|---|---|---|---|
| 1 | `honest-readonly` | read-only, genuinely reads only | read_only PASS; others SUPERSEDED_BY_READ_ONLY |
| 2 | `honest-idempotent` | upsert + audit row per call | sequential PASS (append-only changes); concurrent PASS from two_process |
| 3 | `liar-idempotent` | claims idempotent, inserts per call | sequential VIOLATION; destructive PASS |
| 4 | `liar-concurrent` | check-then-insert, no unique constraint | sequential PASS; concurrent VIOLATION in both modes |
| 5 | `liar-readonly` | claims read-only, writes an append-only log | read_only VIOLATION |
| 6 | `silent-safe` | declares nothing, genuinely idempotent | NO_DECLARATION, behavior "holds" |
| 7 | `opaque` | real state in an unobserved journal file | PARTIAL_COVERAGE, never PASS |
| 8 | `flaky` | random token per call | NONDETERMINISTIC, naming `sessions.token` |
| 9 | `malformed-hints` | hand-written JSON-RPC, no SDK, one request at a time; string, number, null, duplicate-key and list annotations over two pages; `scribble` writes then returns a JSON-RPC error | MALFORMED_DECLARATION; `readOnlyHint: null` does not supersede; `scribble` VIOLATION with `protocol_error` side effects |
| 10 | `liar-readonly-error` | claims read-only, writes, returns `isError` | VIOLATION with `errored_with_side_effects`; a clean failure is CALL_ERROR |
| 11 | `startup-writer` | appends a boot-log row on every start; honest read-only and idempotent tools | read_only PASS (control absorbs the boot row); sequential PASS; concurrent NO_OVERLAP_OBSERVED + TWO_PROCESS_SKIPPED_STARTUP_WRITES |

Every fixture's `fixture.toml` records the expected verdict for every tool and
implemented test; `tests/test_ground_truth.py` runs them all and fails on any
mismatch or missing expectation.

## Audit targets

Fixtures validate the harness. **Audit targets** are third-party servers it
measures. A target lives in `targets/<name>/target.toml`, with the same
adapters, classification and masks as a fixture, plus:

- **No expected verdicts, ever.** The loader rejects a target that declares
  any. There is no ground truth for someone else's server, only measurements.
- **A pinned source.** `[source]` names a repository, a full 40-character
  commit SHA, build commands, and the build artifacts to digest.
  `mcpgt prepare <target>` is the only step that uses the network: it fetches
  exactly that commit into `.mcpgt/sources/` and builds it there. Before
  every run the harness verifies, offline, that the checkout is still at the
  commit, that no tracked file is modified, and that it was built with the
  commands now in the definition. The server is always started from that
  checkout, never fetched from a package registry at run time.
- **A pinned runtime.** A target may pin a Node major version
  (`node = "24"`). `mcpgt prepare` refuses to build, and `mcpgt run` refuses
  to start, under any other major version; a checkout built under a
  different major is treated as stale. The `node` in the command is resolved
  to the exact binary that was checked, and its path and version are
  recorded in the report.
- **A sandbox.** Targets always run without network access. On macOS,
  `sandbox-exec` denies all network operations and all file writes outside
  the state directory. If no mechanism works, the audit refuses to run.
  With write confinement and an `fs` adapter on the whole state directory,
  every file the server can persist is observed by construction: a lock or
  temp file left behind appears as an unclassified change in that adapter,
  never silently ignored. State held only in process memory dies with the
  process.
- **Isolation is weaker on Linux.** There, `unshare --net --map-root-user`
  gives the server a network namespace with only loopback, so network is
  blocked, but **file writes are not confined to the state directory**. A
  server could write somewhere unobserved without the harness noticing.
  Hosts that forbid unprivileged user namespaces (including the Ubuntu 24.04
  GitHub Actions runners) have no mechanism at all, so audits refuse to run
  there and the sandbox tests are skipped in CI. Anyone reproducing an audit
  on Linux should account for this; the report records which mechanism was
  used and whether writes were confined.
- **Audit mode.** `mcpgt run <target> --audit` writes the full report and
  exits 0 whatever the verdicts. Reports record `mode: "audit"`, the
  repository and commit, the package name and version, a digest of the built
  artifacts, the build toolchain and commands, and the sandbox used. Every
  VIOLATION and INCONCLUSIVE in an audit report is to be reviewed by hand
  before it is believed or reported.

Call arguments for each tool are declared in the target definition, chosen
against the seeded state so that each call does real work. A tool may have
several argument sets, each a `[[tools]]` entry with a distinct `variant`
label (reported as `tool[variant]`); every variant gets the full battery and
its own verdicts. Variants probe edge cases (existing names, missing
references, duplicates within one call) without changing what any verdict
means.

`--observe-concurrent` also runs concurrent attempts for tools with no
falsifiable idempotence claim. This is observation only: the verdict is
still decided by the declaration (for example `NOT_APPLICABLE /
PERMISSIVE_DECLARATION`), and the attempts are attached as observed behavior,
marked "observation only". It exists to strengthen over-declaration findings,
for instance a tool declared `idempotentHint: false` that never diverges
under concurrent calls.

## Related work

Other work also executes MCP tools or studies duplicate side effects. The
descriptions below follow each project's own documentation as checked on
2026-10-13.

- **junhecui/mcp-conformance** [1] sets out to execute MCP tools under
  observation and compare declared annotations with observed behavior. Its
  design observes at the kernel boundary: an overlayfs upper layer as the
  filesystem changeset, a network namespace as the connection log, and
  seccomp denials as attempted escapes. It also plans a census pipeline that
  measures annotation presence without execution, treats
  `destructiveHint` as semantic rather than mechanically verifiable, and has
  `unverifiable` as a first-class verdict. Its README describes its status as
  "Pre-implementation. No code yet."
- **studiomeyer-io/mcp-hook-conformance** [2] is a CLI that audits MCP
  servers for being called from agent lifecycle hooks. It checks idempotency
  by comparing a tool's output across identical calls, and checks that
  read-only tools do not mutate state through a configured state probe: a
  call to another tool on the same server. Without a probe that check
  returns `INDETERMINATE`. It also has latency, determinism and
  documentation suites.
- **Li, "Where Does Exactly-Once Live?"** [3] benchmarks duplicate side
  effects in LLM agents across model, agent harness and tool contract, in a
  deterministic sandbox of six services with injected faults, and measures
  how idempotency keys change the duplicate rate. It studies agent behavior
  around writes rather than whether a tool's annotations are accurate.

How this project differs, stated without any claim of priority:

- **Idempotence is judged on final backing state**, from two independent
  runs from a clean reset (once against twice), not on tool output.
- **Observation is record-aware** for databases and structured stores (SQLite
  rows, JSON and JSONL documents), and separates business, append-only and
  unclassified state, so an audit row per call does not make an upsert look
  non-idempotent.
- **Concurrency is tested**, including two server instances sharing one
  store, with per-attempt overlap evidence.
- **Control sessions and reset verification** separate a tool's effects from
  server startup and shutdown and from an unreliable reset.
- **Contracts are parsed strictly at the wire level**, with the documented
  divergence from how the MCP Python SDK reads the same bytes.

What the junhecui/mcp-conformance design covers that this project does not:
**network observation**, so `openWorldHint` can be tested (it is not tested
here), and **filesystem coverage without per-server configuration**, where
this project needs adapters, classification and masks declared for each
server.

1. J. Cui. *mcp-conformance: MCP Annotation Conformance Harness.*
   <https://github.com/junhecui/mcp-conformance> (accessed 2026-10-13)
2. StudioMeyer. *mcp-hook-conformance.*
   <https://github.com/studiomeyer-io/mcp-hook-conformance> (accessed 2026-10-13)
3. J. Li. *Where Does Exactly-Once Live? Model, Harness, and Tool-Contract
   Effects on Duplicate Side Effects in LLM Agents.* arXiv:2609.29095, 2026.
   <https://arxiv.org/abs/2609.29095>

## Planned, not yet implemented

- **Reader divergence, recorded per server** (for the later audit of real
  servers, not release 0.1): alongside the strict contract, record what the
  pinned SDK client would have returned for the same bytes, including the
  case where the SDK returns no tools at all because one tool failed
  validation.

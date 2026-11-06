# Probes: catching state the harness cannot see

*Since 0.4.0.* The state tests compare files and databases before and after a
tool runs. A server can also keep state where no adapter looks: in process
memory, in a cache, in a service the fixture does not model. Probes watch
that state indirectly, through what other tools return.

## In plain English

A tool says it only reads a note. Its database doesn't change, so every
state-based test passes. But the server also remembers, in memory, that the
note was "seen", and the next call to `list_notes` shows it.

Probes catch that:

1. Pick a tool whose answer reflects the server's state, such as a list or
   read tool. That's the *probe*.
2. Call the probe on its own, from a fresh server, several times. Its answer
   must be the same every time. If it isn't, it can't be used as a probe.
3. Call the tool under test, then the probe, from a fresh server, several
   times.
4. If the probe's answer changed in every run, the tool changed something.
   For a tool that claims to be read-only, that's a violation.

For a tool that claims to be idempotent (repeating a call changes nothing
more), the comparison is "tool twice, then probe" against "tool once, then
probe".

## Quick start

Add a `[probes]` section to a fixture or target. Each probe must also be a
`[[tools]]` entry, which gives its arguments.

```toml
[probes]
tools = ["list_notes"]
# Optional: regexes removed from probe outputs before comparing, for values
# that change on every call (timestamps, request ids).
ignore = ['"served_at": [0-9.]+']
```

Then run as usual:

```bash
mcpgt run hidden-state
```

The report gets a probes table after the state verdicts:

```text
  probes: list_notes; ignoring 1 pattern(s)
  tool         test               declared   verdict     detail
  list_notes   probe_read_only    true       PASS        no probe output changed (list_notes)
  get_note     probe_read_only    true       PASS        no probe output changed (list_notes)
  peek_note    probe_read_only    true       VIOLATION   output changed: list_notes (3/3 runs); no observed state changed, so the change is in state the fixture does not observe
  bump         probe_idempotence  true       VIOLATION   output changed: list_notes (3/3 runs); no observed state changed, so the change is in state the fixture does not observe
  pin          probe_idempotence  true       PASS        no probe output changed (list_notes)
```

That is the [`hidden-state`](../fixtures/hidden-state) fixture. All 20 of
its state-based verdicts are PASS or NOT_APPLICABLE: its database never
changes. The probes find the two tools whose hints are false.

## How it works

Every session starts from a clean reset in a new server process, as the
state tests do. For each probe P and each tool X:

| Claim | Reference sessions | Treatment sessions | VIOLATION when |
|---|---|---|---|
| `readOnlyHint: true` | P alone | X, then P | P's output differs in every run |
| `idempotentHint: true` | X, then P | X, X, then P | P's output differs in every run |

Each row runs `--runs` times (at least 3). The probe alone also runs that many
times, as a noise check, for every tool.

**What is compared.** The JSON-RPC `result` (or `error`) object of the
probe's call, as canonical JSON. The request id is not part of it, so call
position doesn't matter. Each `ignore` pattern is removed from every string
in the object first, including text inside the tool's `content`.

**Which tools are probed.** Only tools with a claim a probe can falsify:
`readOnlyHint: true`, or `idempotentHint: true` on a tool that isn't
declared read-only. Other tools get a verdict from their declaration alone
and cost no sessions.

**Cost.** With `n` runs, `p` probes, `r` read-only tools and `i` idempotent
tools, probes add `n × p × (1 + r + 2i)` sessions. With one probe, the
default 3 runs, 3 read-only and 3 idempotent tools, that's
3 × (1 + 3 + 6) = 30 sessions.

## Verdicts

Probe verdicts reuse the reasons of the state verdicts.

| Verdict | Means |
|---|---|
| PASS | No probe's output changed, in any run |
| VIOLATION | At least one probe's output changed in every run |
| INCONCLUSIVE: NONDETERMINISTIC | A probe's output differs on its own, or in the reference sessions, or changed in some runs only |
| INCONCLUSIVE: SETUP_FAILED | A session failed to start, or a probe returns an error on its own |
| INCONCLUSIVE: CALL_ERROR | A connection died, or the tool returned an error and no probe changed |
| INCONCLUSIVE: RESET_UNVERIFIED | Two resets produced different states |
| INCONCLUSIVE: NO_DECLARATION / MALFORMED_DECLARATION | The hint to test is missing or not a boolean |
| NOT_APPLICABLE: PERMISSIVE_DECLARATION | Neither `readOnlyHint: true` nor `idempotentHint: true`: nothing a probe can falsify |

Rules that keep verdicts conservative:

- **A change in some runs only is not a violation.** It's reported as
  NONDETERMINISTIC with the count, such as "changed in 1 of 3 runs".
- **A noisy probe decides nothing.** If the probe's output differs across
  runs on its own, every tool it would judge is INCONCLUSIVE for that probe.
  Add an `ignore` pattern for the volatile part, or choose another probe.
- **One clean probe is enough for a VIOLATION.** With several probes, one
  that reproduces a change gives a VIOLATION even if another was unusable.
  The other's problem is kept as a secondary reason.
- **PASS needs every probe to be usable.**

When there's a VIOLATION, the report also checks the state test on the
same claim. `hidden_state: true` means the state adapters saw no change,
so the change happened in state the fixture doesn't observe. That's the
case only probes can catch.

## Report

Each tool entry in `report.json` gets a `probe` object:

```json
{
  "tool": "peek_note",
  "test": "probe_read_only",
  "comparison": "tool then probe, against the probe alone",
  "hint": "readOnlyHint",
  "declared": "true",
  "outcome": "VIOLATION",
  "hidden_state": true,
  "probes": [{
    "probe": "list_notes",
    "behavior": "fails",
    "changed_runs": 3,
    "runs": 3,
    "reference_sha256": "1628d319…",
    "excerpt_offset": 22,
    "reference_excerpt": "…\"seen\": false…",
    "changed_excerpt": "…\"seen\": true…",
    "sessions": [{"label": "peek_note/then-list_notes-1", "output_sha256": "feed148c…", "changed": true}]
  }],
  "raw_sessions": ["…every response, verbatim…"]
}
```

Both excerpts start shortly before the first character that differs
(`excerpt_offset`), so a change deep in a long output is still visible.
The top-level `probes` block records the configuration and the sessions of
each probe on its own. Every response is kept verbatim, so each comparison
can be checked by hand.

In a fixture, `expected.probe` gives the ground truth:

```toml
[tools.expected.probe]
verdict = "VIOLATION"
```

`--fail-on-violation` counts probe violations.

## What probes cannot see

- **Only what the probe returns.** A change that no probe's output reflects
  is invisible. A PASS means "no change seen through these probes", nothing
  more. Choose probes that return as much state as possible, such as a full
  listing instead of one item.
- **Only state that survives inside one session.** Each session is a new
  server process, so in-memory state is visible only to a probe called in
  the same session. State shared between processes or kept in a remote
  service is seen only if the probe reads it back.
- **Tool order is fixed.** The probe always runs last. Effects that depend on
  other orders, or on longer sequences, are not tested.
- **Changes that are designed to vary are noise.** A tool whose output
  includes a value that changes on purpose, such as a request counter or a
  random id, makes a probe unusable unless an `ignore` pattern removes it.
  An overly broad pattern can hide a real change.
- **Tool arguments come from the definition.** Probes test the argument sets
  you give, nothing else.

## Prior work

[mcp-hook-conformance](https://github.com/studiomeyer-io/mcp-hook-conformance)
already uses a configured "state probe", a call to another tool on the same
server, to check read-only tools. Without one it returns `INDETERMINATE`.
What this project adds to that idea:

- each session starts from a clean reset in a fresh server process;
- the probe's own noise is measured first, and a noisy probe decides nothing;
- a change must be reproduced in every run;
- the same method tests `idempotentHint: true`;
- probe results are checked against the state adapters, to tell hidden state
  from observed state;
- the output is a verdict with a reason, not a score.

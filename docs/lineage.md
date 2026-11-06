# Lineage: tracing synthetic data across tool calls

*Since 0.5.0.* A tool can pass every test on its own and still be one step
in a leak. One tool copies a customer's SSN into a drafts table. Another
syncs drafts to a server. Called alone from the seeded state, neither sends
anything sensitive. Called in that order, the SSN leaves.

Lineage plants a synthetic value, calls the tools in a chain, and
reconstructs the observable path the value took. Each step on that path is
backed by evidence.

## In plain English

1. The harness plants a fake secret (a *marker*) in the server's data. Each
   run uses a new random value, so wherever it turns up, it came from where
   it was planted.
2. It calls a chain of tools in order, such as `read_customer`, then
   `save_draft`, then `sync_drafts`. Each run starts from a fresh copy of the
   data.
3. It looks for the marker everywhere it can: in each tool's answer, in the
   stored data, in what the server sends over the network, and in the names
   it looks up. It also checks encoded forms: base64, hex, reversed.
4. To find out which call did what, it runs the first call alone, then the
   first two, and so on. Something that appears after call 2 in every run,
   and never when only call 1 ran, was caused by call 2.
5. When the marker leaves, it reruns the chain with each earlier call
   removed. That shows which calls the leak needs, and which only happen to
   be there.

## Quick start

Put `{marker:NAME}` where the fake secret should live: in a seed file, or in
a tool argument. Then name the chains to trace. Lineage needs the network
sandbox with the capture endpoint, because a leak is something seen leaving.

```sql
-- seed.sql
INSERT INTO customers (id, name, ssn) VALUES (1, 'Ada', '{marker:ssn}');
```

```toml
[network]
policy = "deny"
capture = true

[[lineage.chains]]
tools = ["read_customer", "save_draft", "sync_drafts"]

[[lineage.chains]]
tools = ["remember", "beacon"]
```

```bash
mcpgt run data-lineage
```

Every tool in the [`data-lineage`](../fixtures/data-lineage) fixture passes
its own network test. None of them sends anything when called alone. The
chains tell a different story:

```text
  read_customer > save_draft > sync_drafts: VIOLATION (complete)
      ssn: source: state sqlite:state.db/customers [OBSERVED]
           ->  call 2 save_draft: wrote sqlite:state.db/drafts [ATTRIBUTED]
           ->  call 3 sync_drafts: sent it to collector.invalid:80 (base64) [ATTRIBUTED]
      ssn: leaks without call(s) 1
  remember > beacon: VIOLATION (partial)
      ssn: source: state sqlite:state.db/customers [OBSERVED]
           ->  call 1 remember: needed, but no trace of the marker (unobserved state) [INFERRED]
           ->  call 2 beacon: looked up <ssn>.exfil.invalid [ATTRIBUTED]
  save_draft > export_report: PASS
      no marker left along this chain
  save_draft > sync_drafts_tls: INCONCLUSIVE:PARTIAL_OBSERVATION
      encrypted egress, payload not observable: collector.invalid:443
```

Each verdict tells you something:

- The first leak is fully traced, through base64 encoding. `read_customer`
  also returned the SSN, but the leak happens without it, so it isn't on the
  path.
- The second leak goes through server memory. `remember` is needed, but the
  SSN never shows up in anything observable on the way, so that step is
  INFERRED, and the path is marked partial.
- The third chain moves the SSN between tables and keeps it local.
- The fourth sends it over HTTPS. The payload can't be read, so the result
  is INCONCLUSIVE, not "no leak".

## How it works

**Markers.** Each `{marker:NAME}` gets a fresh token per run, such as
`mcpgt3f9a…`, made only of lowercase letters and digits. JSON, URL and DNS
encodings therefore leave it unchanged. Placeholders are replaced in seed
files (SQLite scripts, JSON files, files in directory seeds) and in tool
arguments.

**Where markers are looked for:**

| Point | Kind | Source of truth |
|---|---|---|
| Each call's response | `response` | The verbatim JSON-RPC line |
| State after the session | `state` | Database and JSON records; contents of files under directory surfaces |
| Bytes that reached the capture endpoint | `egress` | The whole connection |
| Names the server tried to look up | `lookup` | The network observer's log |

Each marker is also looked for as hex, upper-case hex, reversed, and base64
and base64url at every byte alignment. A form other than `plain` is
reported as such.

**Sessions.** For a chain of `k` calls, the harness runs each prefix (the
first call, the first two, and so on) and the chain without each call
except the last. Every one of these runs `--runs` times from a clean reset,
so a chain costs `(2k − 1) × runs` sessions. Chains are limited to 6 calls.

**Attribution.** A sighting is attributed to call `j` when it's present
after the first `j` calls in every run and absent in every run that stopped
before call `j`. The control sessions, with no calls at all, are level 0: a
sighting there is the marker's source. Timing (`call 2`, `after 2`) is
recorded, but it never establishes attribution on its own.

## Evidence grades

Each step on a path gets one grade:

| Grade | Means |
|---|---|
| OBSERVED | The marker was seen there. Used for the source. |
| ATTRIBUTED | Seen there after call `j` in every run, and in no run before it. |
| INFERRED | The leak needs call `j` (removing it stops the leak in every run), but the marker was not seen anywhere call `j` wrote. The value went through something the harness doesn't observe, such as memory. |

A path is **complete** when no step is INFERRED, and **partial** otherwise.
A call's own response doesn't count as a hand-off to the next call: no AI
client copies values between calls here.

## Verdicts

One verdict per chain. The reasons are the network reasons.

| Verdict | Means |
|---|---|
| VIOLATION | A marker left (captured bytes, or a name looked up) after the same call in every run |
| PASS | No marker left, observation was complete, and no egress was encrypted |
| INCONCLUSIVE: NONDETERMINISTIC | A sighting, or a leak, in some runs only |
| INCONCLUSIVE: PARTIAL_OBSERVATION | A child process escaped the observer, or some egress was encrypted |
| INCONCLUSIVE: OBSERVATION_UNAVAILABLE | The observer didn't load in some session |
| INCONCLUSIVE: SETUP_FAILED | A session failed, or the reset wasn't verified |

A VIOLATION stands even if another part of the chain was only partly
observed: captured bytes are direct evidence. Those problems are listed as
secondary. A PASS needs everything to be clean.

In a fixture, each chain can carry ground truth:

```toml
[[lineage.chains]]
tools = ["remember", "beacon"]
expected = "VIOLATION"
completeness = "partial"
```

`--fail-on-violation` counts lineage violations.

## Report

`report.json` gets a `lineage` block with the tokens planted in this run
(they're synthetic) and one entry per chain. Each entry has the verdict,
completeness, the sessions verbatim, and per marker:

- `sources`: where it was planted;
- `sightings`: every sighting, with the call it's attributed to;
- `egress_call`: the first call after which the marker left;
- `needed_calls` and `not_needed_calls`: what removing each earlier call
  showed;
- `path`: the steps with their grades, as in the text output.

## What lineage cannot see

- **Only server-side flows.** In real use, an AI client often copies one
  tool's output into another tool's input. No client runs here, so such
  flows aren't discovered. A chain whose calls take the marker as an
  argument shows what happens *if* a client passes it on, by construction.
- **Encrypted traffic.** HTTPS through the proxy shows only the host and
  port, so a chain with encrypted egress is INCONCLUSIVE, never PASS.
- **Transformations beyond the listed forms.** Compression, encryption,
  hashing, splitting a value across connections, or any other custom
  encoding hides the marker. A leak in such a form is missed, and the chain
  can PASS. Read a PASS as "no marker left in a form we look for".
- **Memory.** A hop through memory is never seen, only inferred from the
  calls the leak needs.
- **State that isn't modelled.** Only surfaces with adapters are searched.
  A SQLite file read by a directory adapter is searched as raw bytes, where
  deleted rows can linger.
- **Chains are written by hand.** Lineage tests the chains and arguments you
  list. It doesn't search for chains.

## Prior work

- [MCPPrivacyDetector](https://arxiv.org/abs/2606.21338) and
  [VIPER-MCP](https://arxiv.org/abs/2605.21392) find source-to-sink flows by
  static analysis of server code.
- [SandScope](https://arxiv.org/abs/2601.01241) runs MCP tools and reports
  source-to-sink witnesses within a tool's result, with network evidence.
- [Snyk's toxic flow analysis](https://snyk.io/articles/understanding-toxic-flows-mcp/)
  flags risky combinations of tool capabilities from metadata.
- Canary tokens and dynamic taint tracking are long-established techniques.

What this adds:

- data traced across several calls, through the server's own storage;
- attribution by controlled reruns rather than timing;
- each step graded by its evidence;
- removal runs that separate the calls a leak needs from the ones that only
  happen to be there.

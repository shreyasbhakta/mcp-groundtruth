# Test your own MCP server

This guide takes one server from nothing to an audit report. It uses
`examples/notes`, a small server in this repository that keeps notes in
SQLite and has one deliberate annotation bug. Do the same steps with your own
server's command and state.

mcp-groundtruth calls each tool against a known starting state and compares
what changed with what the tool's annotations promise (`readOnlyHint`,
`destructiveHint`, `idempotentHint`). See [methodology.md](methodology.md)
for the definitions.

## What your server needs

- It speaks MCP over **stdio**.
- It keeps its state in **SQLite databases, files and directories, or JSON /
  JSON Lines files**, at paths you can point into a directory mcp-groundtruth
  chooses, through an environment variable or a command-line argument.
- It starts without network access, and on macOS without writing outside
  that directory. Audits run the server in a sandbox (macOS `sandbox-exec`;
  Linux `unshare`, which needs unprivileged user namespaces and does not
  confine writes).

## 1. Install

```bash
pip install mcp-groundtruth        # once published to PyPI
uv tool install git+https://github.com/shreyasbhakta/mcp-groundtruth   # from source
```

Either gives you the `mcpgt` command. The example server is in the
repository, so for this walk-through also clone it and work from its root:

```bash
git clone https://github.com/shreyasbhakta/mcp-groundtruth.git
cd mcp-groundtruth
```

Inside a checkout, `uv run mcpgt` works without installing anything.

## 2. Generate a starting target

A *target* is a directory with a `target.toml` describing how to start the
server, where its state lives and what to call. `mcpgt init` writes a first
version by starting the server once and listing its tools:

```bash
mcpgt init my-notes --env 'NOTES_DB={state}/notes.db' -- '{python}' examples/notes/server.py
```

```text
wrote my-notes/target.toml: 5 tools, 12 TODO(mcpgt) placeholders to fill in
  started with: {python} {target}/../examples/notes/server.py (sandbox: sandbox-exec)
next: edit my-notes/target.toml, then run: mcpgt verify-reset my-notes
```

Everything after `--` is the server command. Placeholders, kept as written
in `target.toml` and expanded on every run:

| Placeholder | Expands to |
|---|---|
| `{state}` | the state directory for this session; also the server's working directory |
| `{target}` | the target directory (`my-notes`) |
| `{python}` | the Python running mcpgt, which has the MCP SDK installed |

The example uses `{python}` because the server needs the MCP SDK. For your own
server, give the command you would run yourself, such as
`-- node dist/index.js` or `-- .venv/bin/python -m my_server`. Arguments that
name existing files are rewritten relative to `{target}`, since the server
runs from the state directory. `--env KEY=VALUE` (repeatable) sets the
environment. The server gets only `PATH` and `HOME` from yours.

`init` starts the server in the same sandbox as an audit. If your host has no
sandbox mechanism, `--no-sandbox` starts it without one. Use that only with a
server you trust, and configure it so it can't touch real data.

## 3. Fill in the TODOs

The generated file contains what the server told `init`:

- every tool, with its declared hints as a comment;
- an argument placeholder for each property in the tool's input schema
  (required ones as values, optional ones commented out).

It also contains a `TODO(mcpgt)` placeholder for each decision only you can
make. Until every one is answered, the target refuses to load and lists them:

```text
$ mcpgt verify-reset my-notes
error: /path/to/my-notes/target.toml: 12 unanswered TODO(mcpgt) placeholders; replace each with a real value:
  state.locations[0]: TODO(mcpgt): every state location, e.g. sqlite:app.db
  adapters[0].kind: TODO(mcpgt): sqlite, fs or json
  ...
  tools[2](get_note).arguments.note_id: TODO(mcpgt): integer, required
```

The finished file for this example is
[`examples/notes/target.toml`](../examples/notes/target.toml). The decisions:

**Where the state lives.** The notes server keeps one database, at whatever
path `NOTES_DB` names. `--env` already pointed it into the state directory,
so the only location is:

```toml
[state]
locations = ["sqlite:notes.db"]
```

List every place your server writes. A location that no adapter observes
makes coverage partial, and verdicts that depend on it become INCONCLUSIVE
rather than PASS. If you are unsure what your server writes, add
`"fs:."` with an `fs` adapter labelled `"."`: it observes the whole state
directory, so stray files show up.

**Adapters and the starting state.** One adapter per location says how to
read it (`sqlite`, `fs` or `json`) and what it starts as. Every session
starts from this seed, so tool calls have something to work on:

```bash
cp examples/notes/seed.sql my-notes/
```

```toml
[[adapters]]
kind = "sqlite"
label = "notes.db"
seed = "seed.sql"
```

The seed is a SQL script for `sqlite`, a directory for `fs`, and a file for
`json`.

**Classification.** Regions are tables for SQLite, files for directories, and
partitions for JSON. Each region is one of three kinds:

- `business`: data a user would miss. The tests are judged on this.
- `append_only`: logs that only grow. Appending is allowed; overwriting or
  deleting is still destructive.
- unclassified: anything you list in neither. A change to unclassified
  state makes idempotence and destructive verdicts INCONCLUSIVE, because
  nobody said what it means. Read-only claims cover all state, classified or
  not.

```toml
business = ["notes"]
append_only = ["activity"]
```

Use `[]` when a list is empty; the placeholder has to be replaced either way.

**Masks.** Columns that differ on every run, like timestamps, random ids and
tokens, would make every comparison fail. Mask them by region and column:

```toml
mask = [
    { region = "notes", column = "created_at", kind = "timestamp" },
    { region = "activity", column = "at", kind = "timestamp" },
]
```

Kinds are `timestamp`, `uuid`, `autoincrement` and `random_token`. Mask only
what really varies: a masked column is not compared at all.

**Tool arguments.** Choose arguments that do real work against the seeded
state. A `get_note` for a note that doesn't exist tests nothing.

```toml
[[tools]]
name = "get_note"
arguments = { note_id = 1 }
```

Delete a `[[tools]]` entry to leave that tool untested. To test another set
of arguments for the same tool, copy its entry and add `variant = "<name>"`.

## 4. Verify the reset

```bash
mcpgt verify-reset my-notes
```

```text
notes: reset verified
  coverage: complete
```

This resets the state directory from the seeds twice and checks that both
resets give identical state after masking. It also checks that every mask
names a region and column that exist. Fix anything it reports before
auditing: an unreliable reset makes every later comparison meaningless.

## 5. Run the audit

```bash
mcpgt run my-notes --audit
```

Each tool is called in fresh sessions from the seeded state:

- once and twice, three runs each;
- in two concurrent modes, 20 attempts each, for tools whose idempotence
  claim can be falsified;
- alongside control sessions that call nothing.

A small server takes about a minute. The summary, abridged:

```text
AUDIT  notes  run 20261014T051500665179Z-notes  (3 runs)
  reset: verified
  coverage: complete
  control sessions: stable

  tool         test                     declared  verdict                                 observed
  add_note     destructive              false     PASS                                    non_destructive=holds
  list_notes   read_only                true      PASS                                    read_only=holds
  get_note     read_only                true      VIOLATION                               read_only=fails
  rename_note  sequential_idempotence   true      PASS                                    idempotent=holds
  rename_note  concurrent_idempotence   true      PASS                                    idempotent=holds
  delete_note  sequential_idempotence   true      PASS                                    idempotent=holds
  delete_note  concurrent_idempotence   true      PASS                                    idempotent=holds
  ...
report: runs/20261014T051500665179Z-notes/report.json
```

`--audit` exits 0 whatever the verdicts: an audit is a measurement for you
to review.

## 6. Read the report

Each tool gets four verdicts: `sequential_idempotence`,
`concurrent_idempotence`, `read_only` and `destructive`.

| Verdict | Meaning |
|---|---|
| `PASS` | The tool made a claim, and it held in every run. |
| `VIOLATION` | The tool made a claim, and it failed reproducibly. |
| `INCONCLUSIVE:<reason>` | The evidence can't decide, for example `PARTIAL_COVERAGE`, `UNCLASSIFIED_STATE`, `NONDETERMINISTIC`, `CALL_ERROR`. |
| `NOT_APPLICABLE:<reason>` | Nothing to test. `PERMISSIVE_DECLARATION`: a `false` hint promises nothing. `NO_DECLARATION`: the hint is absent, which is not the same as false. `SUPERSEDED_BY_READ_ONLY`: a read-only claim covers it. |

`get_note` declares `readOnlyHint: true`, but the audit saw it change
business state. The full evidence is in `report.json`, under the verdict's
`observed.runs[].deltas`. In every run, one row of `notes` changed against the
control session:

```text
removed: [1, "Groceries", "Milk, eggs", <masked timestamp>, 0]
added:   [1, "Groceries", "Milk, eggs", <masked timestamp>, 1]
```

The last column is `views`: reading a note counts the view. Either the
annotation or the behavior has to change. Declare `readOnlyHint: false`, or
stop writing on read.

`mcpgt report <run-id>` prints a saved report again, and `--format json`
prints all of it. Each report records:

- what was tested: the definition's digest, mcpgt's version and commit, and
  the sandbox;
- every `tools/list` and `initialize` response, verbatim;
- every tool response.

When a verdict surprises you, check the evidence before concluding anything:

- **INCONCLUSIVE `UNCLASSIFIED_STATE`:** classify the region that changed. It
  is named in the deltas.
- **INCONCLUSIVE `NONDETERMINISTIC`:** the named column differs between runs.
  Mask it if it is truly volatile.
- **INCONCLUSIVE `PARTIAL_COVERAGE`:** some declared location has no adapter,
  or an adapter cannot see everything in it.
- **VIOLATION:** reproduce it by hand from the logged calls before reporting
  it to anyone else.

## Troubleshooting

- **`init` or a run says the server exited.** The server's stderr is shown.
  Under the sandbox a server can't reach the network, so `npx some-package`
  that needs a download will fail; build it and run the built file. On macOS
  it also can't write outside the state directory, so point every data, cache
  and log path into `{state}`.
- **`no network isolation available`.** On Linux, audits need unprivileged
  user namespaces. Ubuntu 24.04 restricts them by default through AppArmor.
  macOS needs nothing extra.
- **Startup writes.** A server that writes on every start (a migration, a
  boot log) shows up in the control sessions. The concurrent two-process mode
  is then skipped, with `TWO_PROCESS_SKIPPED_STARTUP_WRITES`, rather than
  blamed on a tool.

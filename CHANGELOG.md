# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-10-25

Checks what a tool tries to reach on the network, not only what it changes
on disk: an observer inside the sandboxed server, a local capture endpoint
that forwards nothing, and a planted canary. Supported on macOS and Linux,
tested end to end on both in CI, and run against the reference memory and
filesystem servers.

### Added

- **Network side effects** ([docs/network.md](docs/network.md)). A
  `[network]` section (`policy = "deny" | "allowlist" | "permitted"`,
  `allow`, `capture`, `canary`, `coverage`) adds a network verdict per tool
  and for the server lifecycle:
  - an observer library (`netobs.c`, built on first use with the host C
    compiler) is preloaded into the sandboxed server and records lookups,
    connects, sends and process launches;
  - a local capture endpoint, which forwards nothing, records what actually
    arrived, with sensitive headers and query strings redacted;
  - a synthetic canary (`$MCPGT_CANARY`, `{canary}`) shows whether planted
    data left.

  Supported on macOS (`sandbox-exec`, `DYLD_INSERT_LIBRARIES`) and Linux
  (`unshare --net`, `LD_PRELOAD`, and a relay inside the namespace that
  carries capture connections to the harness over a Unix socket).
  VIOLATION needs a disallowed interaction in every run. Unavailable or
  partial observation is INCONCLUSIVE, and a definition with `[network]`
  refuses to run without a sandbox. `--fail-on-violation` counts network
  violations.
- `fixtures/network-side-effects`: read-only tools that beacon, look up a
  name carrying the canary, upload it, or start an unobservable child.
- `results/network-2026-10-25/`: the network check on the memory and
  filesystem servers. No tool of either attempted any network operation
  (246 fully observed sessions).
- `targets/filesystem`: the reference filesystem server pinned at
  `modelcontextprotocol/servers@bbb701e`, with its reviewed audit in
  `results/filesystem-2026-10-14/`.
- `fs` adapters: `modes = true` records permission bits as state, and
  `empty_dirs` creates empty directories after the seed is copied (git
  cannot store them).

### Changed

- When a concurrent verdict is INCONCLUSIVE for both `PARTIAL_COVERAGE`
  and `NO_OVERLAP_OBSERVED`, `PARTIAL_COVERAGE` is now the primary reason.
  Previously the timing-dependent reason won, so the same fixture got
  different verdicts on fast and slow machines.
- Prepared sources are checked out per commit *and* build commands, so two
  targets pinning one commit but building different packages never share a
  build. Existing checkouts must be prepared again.

## [0.2.0] - 2026-10-14

Audits real servers, and makes it practical to test your own. Adds audit
targets for third-party servers, `mcpgt init`, a step-by-step guide with an
example server, and a GitHub Action that fails CI on a VIOLATION. First
release packaged for PyPI.

### Added

Audit targets:

- Audit targets (`targets/<name>/target.toml`): servers with no expected
  verdicts, run with `mcpgt run <target> --audit`, which writes the full
  report and exits 0 whatever the verdicts.
- Pinned sources: `mcpgt prepare <target>` fetches one git commit and builds
  it, the only step that uses the network. Every run verifies the checkout
  offline and records repository, commit, package, artifact digest and
  toolchain in provenance.
- Node major-version pin for targets (`node = "24"`), enforced at build and
  run time; the binary used is recorded in provenance.
- Sandboxed server processes for targets: no network (macOS `sandbox-exec`,
  Linux `unshare`), and on macOS no file writes outside the state directory.
- JSON Lines support in the JSON adapter, with `partition_by` and `explode`.
  A directory adapter can observe the whole state directory and delegate
  nested files to other adapters.
- Argument variants: several `[[tools]]` entries for one tool, each with a
  `variant` label and its own verdicts. `--tool` is repeatable and accepts
  `name[variant]`.
- `--observe-concurrent`: observation-only concurrent attempts for tools
  without a falsifiable idempotence claim; verdicts are unchanged.
- Reports carry each adapter's modelling decisions and notes under
  `definition.adapters`.
- `targets/memory`: the reference memory server pinned at
  `modelcontextprotocol/servers@bbb701e`, with its reviewed audit in
  `results/memory-2026-10-14/`.

Testing your own server:

- `mcpgt init <dir> -- <server command>`: starts a server in the audit
  sandbox, lists its tools and writes a starting `target.toml`. It contains:
  - every tool, with its declared hints as comments;
  - argument placeholders from each tool's input schema;
  - `TODO(mcpgt)` placeholders for state locations, adapters, classification
    and masks, which are never guessed.

  A definition with any `TODO(mcpgt)` value left refuses to load and lists
  each one.
- `{target}` placeholder (the definition's directory) in commands and `env`.
  `{state}` is now also expanded in commands.
- [docs/your-server.md](docs/your-server.md): testing your own server from
  install to report.
- `examples/notes`: a small SQLite-backed server for the guide, with one
  deliberate annotation bug. Tests keep the guide's claims true.

CI:

- `--fail-on-violation`: `mcpgt run` exits 1 if any verdict is VIOLATION;
  `--audit` alone still exits 0.
- A composite GitHub Action (`action.yml`) that audits a target, fails the
  job on any VIOLATION, and uploads the report as an artifact. CI runs it
  against `examples/notes` on Ubuntu and macOS. The guide warns that the
  user-namespace sysctl the Linux sandbox needs on Ubuntu 24.04 is for
  ephemeral GitHub-hosted runners only.

Packaging:

- Packaged for PyPI as `mcp-groundtruth`, installing the `mcpgt` command
  (also as `mcp-groundtruth`). A release workflow publishes version tags
  through PyPI trusted publishing after the full CI suite passes.

### Changed

- The distribution is named `mcp-groundtruth` (was `mcpgt`, never
  published); the import package and command stay `mcpgt`.
- Report schema `mcpgt.report/0.2`: adds `mode`, `kind`, and provenance
  `definition` and `runtime`.
- Provenance records the Node binary's path relative to the working
  directory (for example `.mcpgt/toolchains/...`), or as `~/...` under the
  home directory, so published reports do not reveal the local directory
  layout.
- README links are absolute, so they resolve on PyPI.

## [0.1.0] - 2026-10-13

First release. Validates the harness against fixture servers with known
ground truth; does not test third-party servers.

### Added

- Tri-state declared contracts (`TRUE` / `FALSE` / `ABSENT`) parsed from raw
  `tools/list` responses, with recorded parse anomalies for non-boolean,
  null, duplicated and misplaced hints.
- JSON-RPC stdio client that keeps every response verbatim, follows
  `nextCursor`, routes concurrent requests and measures call timing.
- Snapshot adapters for SQLite databases, directory trees and JSON document
  files, with coverage reporting and business / append-only / unclassified
  state classification.
- Declared-only normalization of volatile fields, validated against the
  seeded state.
- Reset verification, control sessions, and the test battery: sequential
  idempotence, concurrent idempotence (single-connection and two-process
  modes, one-sided), read-only and destructive.
- Verdict engine with `PASS`, `VIOLATION`, `INCONCLUSIVE` and
  `NOT_APPLICABLE`, primary and secondary reasons, and declaration
  coherence checks.
- Self-contained JSON reports with provenance, raw protocol data and
  per-verdict evidence; `mcpgt verify-reset`, `run` and `report` commands.
- Eleven ground-truth fixture servers and an end-to-end ground-truth test.
- Methodology documentation.

[Unreleased]: https://github.com/shreyasbhakta/mcp-groundtruth/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/shreyasbhakta/mcp-groundtruth/releases/tag/v0.3.0
[0.2.0]: https://github.com/shreyasbhakta/mcp-groundtruth/releases/tag/v0.2.0
[0.1.0]: https://github.com/shreyasbhakta/mcp-groundtruth/releases/tag/v0.1.0

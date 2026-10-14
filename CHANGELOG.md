# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `mcpgt init <dir> -- <server command>`: starts a server in the audit
  sandbox, lists its tools and writes a starting `target.toml`: every tool,
  its declared hints as comments, argument placeholders from each input
  schema, and `TODO(mcpgt)` placeholders for state locations, adapters,
  classification and masks. A definition with any `TODO(mcpgt)` value left
  refuses to load and lists each one.
- `{target}` placeholder (the definition's directory) in commands and `env`;
  `{state}` is now also expanded in commands.
- Packaged for PyPI as `mcp-groundtruth`, with the `mcpgt` command (also
  installed as `mcp-groundtruth`, so `uvx mcp-groundtruth` works). A release
  workflow publishes version tags through PyPI trusted publishing after the
  full CI suite passes.
- Audit targets (`targets/<name>/target.toml`): third-party servers with no
  expected verdicts, and `mcpgt run <target> --audit`, which writes the full
  report and exits 0 regardless of verdicts.
- Pinned sources: `mcpgt prepare <target>` fetches one commit and builds it;
  every run verifies the checkout offline and records repository, commit,
  package, artifact digest and toolchain in provenance.
- Sandboxed server processes for targets: no network (macOS `sandbox-exec`,
  Linux `unshare`), and on macOS no file writes outside the state directory.
- JSON Lines support in the JSON adapter, with `partition_by` and `explode`;
  a directory adapter can observe the whole state directory and delegate
  nested files to other adapters.
- Argument variants: several `[[tools]]` entries for one tool, each with a
  `variant` label and its own verdicts; `--tool` is repeatable and accepts
  `name[variant]`.
- `--observe-concurrent`: observation-only concurrent attempts for tools
  without a falsifiable idempotence claim; verdicts are unchanged.
- Node major-version pin for targets (`node = "24"`), enforced at build and
  run time; the exact binary is recorded in provenance.
- Reports carry each adapter's modelling decisions and notes under
  `definition.adapters`.
- `targets/memory`: the reference memory server pinned at
  `modelcontextprotocol/servers@bbb701e`.

### Changed

- Report schema `mcpgt.report/0.2`: adds `mode`, `kind`, and provenance
  `definition` and `runtime`.
- Provenance records the Node binary's path relative to the working
  directory (for example `.mcpgt/toolchains/...`), or as `~/...` under the
  home directory, so published reports do not reveal the local directory
  layout.

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

[0.1.0]: https://github.com/shreyasbhakta/mcp-groundtruth/releases/tag/v0.1.0

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

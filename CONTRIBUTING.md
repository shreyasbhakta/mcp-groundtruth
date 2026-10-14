# Contributor notes

## Development environment

- Use `uv` for everything Python: `uv sync`, `uv run pytest`,
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.
  CI runs the same commands on Python 3.11 and 3.14.
- **Never install, upgrade or remove system packages** (Homebrew, apt,
  global `npm -g` or global `pip`) as part of working on this project without
  the maintainer's explicit approval first. Package managers can upgrade or
  clean up unrelated software as a side effect.
- **Keep toolchains project-local**, inside `.mcpgt/` (git-ignored). Example:
  Node 24 for audit targets lives in
  `.mcpgt/toolchains/node-v<version>-<platform>/`, downloaded from
  nodejs.org and checked against the published `SHASUMS256.txt` before use.
  Put it first on `PATH` for `mcpgt prepare` and `mcpgt run`.
- Third-party target sources are fetched and built only by
  `mcpgt prepare`, into `.mcpgt/sources/`. Never edit files there: runs refuse
  a checkout with modified tracked files.

## Changes to the harness

- Every behavior change needs a test. For verdict logic, also check that a
  deliberately introduced bug is caught by the test suite.
- Modelling decisions (masks, classification, explode, partitioning) are
  declared in fixture or target definitions, never inferred, and each comes
  with a `notes` explanation where it is not obvious.
- Fixtures must record an expected verdict for every tool and implemented
  test; `tests/test_ground_truth.py` enforces this.

## Audit results

- Audit reports in `runs/` are git-ignored. Nothing about a third-party
  server's results is committed, published or mentioned in documentation
  until it has been reviewed by hand against the raw evidence.
- If a reviewed result shows a VIOLATION on someone else's server, disclose it
  to that server's maintainers before anything is made public.
- Report over-declarations (behavior safer than declared) as
  over-declarations, never as violations.

# Hand review: reference filesystem server, 2026-10-14

One server, one pinned commit, one seeded directory tree. Nothing here
generalizes to other servers, other commits of this server, or other states.

## What was audited

| | |
|---|---|
| Server | `@modelcontextprotocol/server-filesystem` 1.0.0 (`secure-filesystem-server`) |
| Source | `https://github.com/modelcontextprotocol/servers.git` at `bbb701e7ac1e57f3edd1c2836b94c5457eaf7b9c`, no modified tracked files |
| Built artifact digest | `e882287c9581f19ceed06e4d7a8c84032b8356b254a8a122e44c06468dc47694` (identical in both reports) |
| Node | v24.21.0 (npm 11.19.0), official nodejs.org build, checksum-verified |
| Isolation | macOS `sandbox-exec`: network denied, file writes confined to the state directory |
| Python / MCP SDK | CPython 3.14.4 / `mcp` 2.3.0; protocol `2025-11-25` |
| Allowed directory | `root/` inside the state directory, given on the command line. The harness client does not offer Roots, so the server keeps it. |
| Target definition | [`targets/filesystem/target.toml`](../../targets/filesystem/target.toml), seed [`targets/filesystem/seed/`](../../targets/filesystem/seed/) plus the empty directory `root/empty` |

| Report | Harness | Scope | SHA-256 of `report.json` |
|---|---|---|---|
| [`20261014T070741127320Z-filesystem`](20261014T070741127320Z-filesystem/report.json) | `a61f880fd7a10802c8956a34f110287e355a4fd0`, clean | 14 tools, 21 argument sets | `7a2ee36ad765d66e4e0652c1a5d559f923bc1a700e083cca4a2297deb3f41712` |
| [`20261014T071101021849Z-filesystem`](20261014T071101021849Z-filesystem/report.json) | `969b961f8bc5b548cd2354f8ddaaffba0a06439f`, clean | the same plus `edit_file[reapplies]`: 22 argument sets | `e907a6ee17f9713606b23c2d80264bd030c085890f9d9f47d399ba6012794a23` |

Both runs used 3 runs per test, and 20 concurrent attempts per mode for every
argument set (`--observe-concurrent`). The 84 verdicts the two runs share are
identical. The tables below use the second report.

Both reports are committed byte for byte as the harness wrote them. Tool
responses contain absolute paths in the macOS per-user temporary directory
(`/private/var/folders/…/T/mcpgt-…/state/root/…`). They contain no user name.

Commands, from a checkout of the harness commit with Node 24 first on `PATH`:

```bash
uv run mcpgt prepare filesystem
uv run mcpgt verify-reset filesystem
uv run mcpgt run filesystem --audit --observe-concurrent
```

## What counts as state

The `fs` adapter observes the whole state directory. For every entry under it,
it records:

- the path;
- the kind (file, directory, symlink);
- for a file, the SHA-256 of its content, which fixes its size;
- for a symlink, its target;
- the permission bits (`modes = true`).

Everything under `root/` is one business region. There is no append-only
state, and nothing is masked.

Not observed:

- **Timestamps** (access, modification, change, birth). Reading a file
  updates its access time, and `write_file` with identical content updates
  its modification time without changing what is stored. Counting either
  would make every read a false read-only VIOLATION.
- **Ownership and extended attributes.** No tool here changes them.

`verify-reset` passed with complete coverage. Control sessions were stable,
and no session left anything outside `root/`.

## Per-tool review

Declared hints are listed as `readOnly / destructive / idempotent`, with "–"
for absent. `openWorldHint` is `false` on every tool and is not tested.

Every read-only tool leaves `destructiveHint` and `idempotentHint` absent.
With `readOnlyHint: true` those are covered by the read-only claim
(`SUPERSEDED_BY_READ_ONLY`). Every verdict below was checked against the raw
sessions and deltas in the report, and against `src/filesystem/server.ts`
and `lib.ts` at the pinned commit.

| Tool [variant] | Declared | Read-only | Destructive | Idempotent (seq. / conc.) | Review |
|---|---|---|---|---|---|
| read_file | T / – / – | PASS | N/A | N/A / N/A | `fs.readFile` only |
| read_text_file | T / – / – | PASS | N/A | N/A / N/A | as read_file |
| [head] | T / – / – | PASS | N/A | N/A / N/A | `headFile`: open `"r"`, read |
| [tail] | T / – / – | PASS | N/A | N/A / N/A | `tailFile`: stat, open `"r"`, read |
| read_media_file | T / – / – | PASS | N/A | N/A / N/A | read stream; PNG returned as `image` |
| read_multiple_files | T / – / – | PASS | N/A | N/A / N/A | `fs.readFile` per path |
| write_file | F / T / T | N/A | N/A | PASS / PASS | creates `new.txt` (`wx`); a repeat takes the `EEXIST` path and rewrites identical content |
| [existing] | F / T / T | N/A | N/A | PASS / PASS | overwrites `todo.txt` in place, mode kept; a repeat writes identical content |
| edit_file | F / T / F | N/A | N/A | N/A / N/A | applies the edit; the repeat fails with "Could not find exact match" and writes nothing |
| [reapplies] | F / T / F | N/A | N/A | N/A / N/A | `newText` contains `oldText`: the repeat matches and edits again (`Owner: Alice and Bob and Bob`); sequential behavior **fails**, so `false` is correct |
| [dry-run] | F / T / F | N/A | N/A | N/A / N/A | returns the diff, writes nothing |
| create_directory | F / F / T | N/A | PASS | PASS / PASS | `mkdir -p projects/2026` (modes 755); a repeat changes nothing |
| [existing] | F / F / T | N/A | PASS | PASS / PASS | `mkdir -p docs` on an existing directory: no change |
| list_directory | T / – / – | PASS | N/A | N/A / N/A | `readdir` |
| [empty] | T / – / – | PASS | N/A | N/A / N/A | lists `root/empty`: empty result |
| list_directory_with_sizes | T / – / – | PASS | N/A | N/A / N/A | `readdir` and `stat` |
| directory_tree | T / – / – | PASS | N/A | N/A / N/A | recursive `readdir` |
| move_file | F / T / F | N/A | N/A | N/A / N/A | renames `todo.txt` to `docs/todo.txt`; the repeat fails with "Destination already exists" and writes nothing |
| search_files | T / – / – | PASS | N/A | N/A / N/A | recursive `readdir` |
| get_file_info | T / – / – | PASS | N/A | N/A / N/A | `stat` |
| [directory] | T / – / – | PASS | N/A | N/A / N/A | `stat` on `docs` |
| list_allowed_directories | T / – / – | PASS | N/A | N/A / N/A | no filesystem access |

Totals: 88 verdicts, 24 PASS, 64 NOT_APPLICABLE (42
`SUPERSEDED_BY_READ_ONLY`, 22 `PERMISSIVE_DECLARATION`). No VIOLATION, no
INCONCLUSIVE.

Errored calls: the repeat of `edit_file` and of `move_file` in every twice
session, and the losing call in concurrent `move_file` attempts. All are tool
errors (`isError`). In each case the only change was the first call's.
`errored_with_side_effects` is false throughout.

## Over-declarations

A NOT_APPLICABLE verdict whose observed behavior is safer than declared:

- **`move_file`, idempotence.** It is declared `idempotentHint: false`, but a
  repeat with the same arguments changed nothing in every case: all 3
  twice sessions and all 40 concurrent attempts. The mechanism is `lib.ts` `moveFile` (lines 366–385). It
  checks the destination with `lstat` before renaming and refuses if anything
  is there. After a successful move, the destination always exists, so a
  repeat always fails without effect.

  By the specification's definition (no additional effect on repeated
  identical calls), the tool behaves idempotently. Unlike memory's
  create/add tools, though, the repeat reports an error rather than success.
  Declaring `false` is therefore a defensible conservative choice, and this
  review proposes no change.

Not over-declarations, because the safer behavior depends on the arguments
and other arguments of the same tool do what is declared:

- **`edit_file` idempotence.** It held for the base edit only because the
  edited text no longer matched. `[reapplies]` shows a repeat applying again.
- **`edit_file[dry-run]` and `create_directory[existing]`** changed nothing.
  Both tools write with other arguments.
- **`write_file` to a new path** destroyed nothing. Overwriting an existing
  file does.

## Observed behavior unrelated to the annotations

- `move_file`'s repeat fails on the destination check, so its error names the
  destination, not the missing source.
- `edit_file` with `newText` containing `oldText` re-applies on every call.
  This is expected from exact-substring matching (`lib.ts` lines 484–489),
  and the tool declares itself non-idempotent. Two such calls made
  concurrently applied the edit once, not twice: each read the original file
  and wrote the same result. There is no lock, so the last write wins.

## Caveats

- **Timestamps, ownership and extended attributes are not state.** A tool
  that changed only those would not be seen. Rewriting identical content
  counts as no change.
- **Concurrent evidence is real but narrow.** This server has no lock, so
  concurrent calls run truly in parallel. In 440 two-process attempts, 439
  overlapped in flight, with wire overlaps of 0.8–3.7 ms. None diverged. The
  windows are short, and the inputs are small, single-write files.
- **Argument coverage is limited.** There were 22 hand-chosen argument sets
  against one small tree. Not covered:
  - symlinks, Unicode paths and CRLF files;
  - large files;
  - paths outside the allowed directory, a security property, not an
    annotation;
  - the Roots protocol, since the client does not offer it.
- **An over-declaration is not a violation.** It only makes clients more
  cautious than necessary.
- The upstream repository develops its TypeScript servers on Node 22. This
  audit pinned Node 24, as for memory.
- Results are from macOS. On Linux the sandbox does not confine file writes.

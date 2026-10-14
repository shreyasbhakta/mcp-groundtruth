"""mcpgt command line: init, verify-reset, prepare, run, report."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import tomllib
from pathlib import Path

from mcpgt import __version__, scaffold
from mcpgt import fixture as fixtures
from mcpgt import report as reports
from mcpgt.runner import run_fixture, verify_reset
from mcpgt.sandbox import IsolationUnavailable
from mcpgt.sources import SourceError
from mcpgt.sources import prepare as prepare_source
from mcpgt.verdict import CONCURRENT_ATTEMPTS, MIN_RUNS, ResetStatus, derive

EXIT_OK, EXIT_UNEXPECTED, EXIT_USAGE = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcpgt", description="Behavioral conformance harness for MCP tool annotations."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    vr = sub.add_parser("verify-reset", help="reset twice and confirm identical normalized state")
    vr.add_argument("fixture", help="fixture name under fixtures/, or a path to a fixture directory")
    vr.add_argument("--json", action="store_true", help="print the full result as JSON")

    run = sub.add_parser("run", help="run the test battery against a fixture")
    run.add_argument("fixture", help="fixture or target name, or a path")
    run.add_argument(
        "--audit",
        action="store_true",
        help="audit mode: no expected verdicts, full report, exit 0 whatever the verdicts (required for targets)",
    )
    run.add_argument(
        "--tool",
        action="append",
        help="only this tool (all its variants), or one variant as NAME[VARIANT]; repeatable",
    )
    run.add_argument("--runs", type=int, default=MIN_RUNS, help=f"independent runs per test (min {MIN_RUNS})")
    run.add_argument(
        "--concurrent-attempts",
        type=int,
        default=CONCURRENT_ATTEMPTS,
        help=f"concurrent attempts per mode (default {CONCURRENT_ATTEMPTS})",
    )
    run.add_argument(
        "--observe-concurrent",
        action="store_true",
        help="also run concurrent attempts for tools without a falsifiable idempotence claim (observation only)",
    )
    run.add_argument("--runs-dir", type=Path, default=reports.RUNS_DIR, help="where reports are written")

    prep = sub.add_parser("prepare", help="fetch and build a target's pinned source (uses the network)")
    prep.add_argument("fixture", metavar="target", help="target name under targets/, or a path")

    rep = sub.add_parser("report", help="show a saved report")
    rep.add_argument("run_id", help="run id, or a path to report.json")
    rep.add_argument("--format", choices=["text", "json"], default="text")
    rep.add_argument("--runs-dir", type=Path, default=reports.RUNS_DIR)

    init = sub.add_parser(
        "init",
        help="start a server, list its tools and write a starting target.toml",
        usage="mcpgt init DIR [--name NAME] [--env KEY=VALUE] [--force] [--no-sandbox] -- SERVER COMMAND...",
    )
    init.add_argument("dir", type=Path, help="directory for the new target (created if missing)")
    init.add_argument("--name", help="target name (default: the directory name)")
    init.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="environment variable for the server, kept in target.toml; may use {state}; repeatable",
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing target.toml")
    init.add_argument(
        "--no-sandbox",
        action="store_true",
        help="start the server without network isolation (when the host has none)",
    )

    argv = sys.argv[1:] if argv is None else list(argv)
    server_command: list[str] = []
    if argv[:1] == ["init"] and "--" in argv:
        split = argv.index("--")
        argv, server_command = argv[:split], argv[split + 1 :]
    args = parser.parse_args(argv)
    if args.command == "init":
        return _init(args, server_command)
    if args.command == "report":
        return _report(args)
    try:
        fx = fixtures.load(args.fixture)
    except fixtures.FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "verify-reset":
        return _verify_reset(fx, args.json)
    if args.command == "prepare":
        return _prepare(fx)
    return _run(fx, args)


def _init(args: argparse.Namespace, server_command: list[str]) -> int:
    if not server_command:
        print("error: give the server command after --, e.g. mcpgt init my-target -- python server.py", file=sys.stderr)
        return EXIT_USAGE
    env: dict[str, str] = {}
    for item in args.env:
        key, sep, value = item.partition("=")
        if not sep or not key:
            print(f"error: --env needs KEY=VALUE, got {item!r}", file=sys.stderr)
            return EXIT_USAGE
        env[key] = value
    target_dir: Path = args.dir
    if (target_dir / "target.toml").exists() and not args.force:
        print(f"error: {target_dir / 'target.toml'} already exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_USAGE
    command = scaffold.portable_command(server_command, target_dir)
    try:
        found = asyncio.run(scaffold.discover(command, target_dir, env, sandbox=not args.no_sandbox))
        text = scaffold.render(args.name or target_dir.resolve().name, command, env, found, server_command)
        path = scaffold.write(target_dir, text, force=args.force)
    except IsolationUnavailable as exc:
        print(f"error: {exc}; pass --no-sandbox to start the server without isolation", file=sys.stderr)
        return EXIT_USAGE
    except (scaffold.InitError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    todos = fixtures.unanswered_todos(tomllib.loads(text))
    tools = f"{len(found.tools)} tool{'' if len(found.tools) == 1 else 's'}"
    print(f"wrote {path}: {tools}, {len(todos)} {fixtures.TODO_MARKER} placeholders to fill in")
    print(f"  started with: {' '.join(command)}" + (f" (sandbox: {found.isolation})" if found.isolation else ""))
    print(f"next: edit {path}, then run: mcpgt verify-reset {target_dir}")
    return EXIT_OK


def _prepare(fx: fixtures.Fixture) -> int:
    if fx.source is None:
        print(f"error: {fx.name} has no [source] to prepare", file=sys.stderr)
        return EXIT_USAGE
    try:
        source = prepare_source(fx.source, log=lambda line: print(line, flush=True), node_major=fx.node)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"prepared {source.spec.repository} @ {source.spec.commit}")
    print(f"  package: {source.package_name} {source.package_version}")
    print(f"  artifacts sha256: {source.content_sha256}")
    print(f"  built with: {', '.join(f'{k} {v}' for k, v in source.toolchain.items())}")
    print(f"  checkout: {source.path}")
    return EXIT_OK


def _verify_reset(fx: fixtures.Fixture, as_json: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="mcpgt-") as tmp:
        result = verify_reset(fx, Path(tmp) / "state")
    if as_json:
        print(json.dumps({"fixture": fx.name, **result.to_json()}, indent=2))
    else:
        print(f"{fx.name}: reset {result.status.value}")
        if result.detail:
            print(f"  {result.detail}")
        for delta in result.deltas:
            for region in delta.regions:
                print(
                    f"  {delta.surface} {region.region} [{region.category.value}]: "
                    f"+{len(region.added)} -{len(region.removed)}"
                )
        cov = result.coverage
        print(f"  coverage: {'complete' if cov.complete else 'partial'}")
        for location in cov.unobserved:
            print(f"    unobserved: {location}")
        for adapter in cov.adapters:
            for item in adapter.uncovered:
                print(f"    {adapter.surface} uncovered: {item}")
    return EXIT_OK if result.status is ResetStatus.VERIFIED else EXIT_UNEXPECTED


def _run(fx: fixtures.Fixture, args: argparse.Namespace) -> int:
    if fx.is_target and not args.audit:
        print(f"error: {fx.name} is an audit target with no expected verdicts; use --audit", file=sys.stderr)
        return EXIT_USAGE
    try:
        with tempfile.TemporaryDirectory(prefix="mcpgt-") as tmp:
            run = asyncio.run(
                run_fixture(
                    fx,
                    Path(tmp),
                    runs=args.runs,
                    only_tools=args.tool,
                    concurrent_attempts=args.concurrent_attempts,
                    observe_concurrent=args.observe_concurrent,
                )
            )
    except (ValueError, SourceError, IsolationUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    report = reports.build(run, derive(run), reports.new_run_id(fx), audit=args.audit)
    path = reports.write(report, args.runs_dir)
    print(reports.render_text(report))
    print(f"\nreport: {path}")
    if args.audit:
        return EXIT_OK
    return EXIT_OK if not report["expectations"]["mismatches"] else EXIT_UNEXPECTED


def _report(args: argparse.Namespace) -> int:
    try:
        report = reports.load(args.run_id, args.runs_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps(report, indent=2) if args.format == "json" else reports.render_text(report))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

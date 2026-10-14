"""mcpgt command line: verify-reset, run, report."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from mcpgt import fixture as fixtures
from mcpgt import report as reports
from mcpgt.runner import ResetStatus, run_fixture, verify_reset
from mcpgt.verdict import CONCURRENT_ATTEMPTS, MIN_RUNS, derive

EXIT_OK, EXIT_UNEXPECTED, EXIT_USAGE = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcpgt")
    sub = parser.add_subparsers(dest="command", required=True)

    vr = sub.add_parser("verify-reset", help="reset twice and confirm identical normalized state")
    vr.add_argument("fixture", help="fixture name under fixtures/, or a path to a fixture directory")
    vr.add_argument("--json", action="store_true", help="print the full result as JSON")

    run = sub.add_parser("run", help="run the test battery against a fixture")
    run.add_argument("fixture")
    run.add_argument("--tool", help="only this tool")
    run.add_argument("--runs", type=int, default=MIN_RUNS, help=f"independent runs per test (min {MIN_RUNS})")
    run.add_argument(
        "--concurrent-attempts",
        type=int,
        default=CONCURRENT_ATTEMPTS,
        help=f"concurrent attempts per mode (default {CONCURRENT_ATTEMPTS})",
    )
    run.add_argument("--runs-dir", type=Path, default=reports.RUNS_DIR, help="where reports are written")

    rep = sub.add_parser("report", help="show a saved report")
    rep.add_argument("run_id", help="run id, or a path to report.json")
    rep.add_argument("--format", choices=["text", "json"], default="text")
    rep.add_argument("--runs-dir", type=Path, default=reports.RUNS_DIR)

    args = parser.parse_args(argv)
    if args.command == "report":
        return _report(args)
    try:
        fx = fixtures.load(args.fixture)
    except fixtures.FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "verify-reset":
        return _verify_reset(fx, args.json)
    return _run(fx, args)


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
    try:
        with tempfile.TemporaryDirectory(prefix="mcpgt-") as tmp:
            run = asyncio.run(
                run_fixture(
                    fx,
                    Path(tmp),
                    runs=args.runs,
                    only_tool=args.tool,
                    concurrent_attempts=args.concurrent_attempts,
                )
            )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    report = reports.build(run, derive(run), reports.new_run_id(fx))
    path = reports.write(report, args.runs_dir)
    print(reports.render_text(report))
    print(f"\nreport: {path}")
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

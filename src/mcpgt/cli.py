"""mcpgt command line. Only verify-reset exists so far; run and report arrive in step 4."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from mcpgt import fixture as fixtures
from mcpgt.runner import ResetStatus, verify_reset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcpgt")
    sub = parser.add_subparsers(dest="command", required=True)
    vr = sub.add_parser("verify-reset", help="reset twice and confirm identical normalized state")
    vr.add_argument("fixture", help="fixture name under fixtures/, or a path to a fixture directory")
    vr.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args(argv)

    try:
        fx = fixtures.load(args.fixture)
    except fixtures.FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="mcpgt-") as tmp:
        result = verify_reset(fx, Path(tmp) / "state")

    if args.json:
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
    return 0 if result.status is ResetStatus.VERIFIED else 1


if __name__ == "__main__":
    sys.exit(main())

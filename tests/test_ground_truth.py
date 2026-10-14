"""The harness is correct when every fixture gets exactly its expected verdicts.

Runs each fixture end to end (real servers, 3 runs) and compares every
verdict against fixture.toml. Slowest test in the suite, and the one that
matters most.
"""

import asyncio
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt import report as reports
from mcpgt.runner import run_fixture
from mcpgt.verdict import IMPLEMENTED, derive

FIXTURES = sorted(p.parent.name for p in (Path(__file__).parent.parent / "fixtures").glob("*/fixture.toml"))
ROOT = Path(__file__).parent.parent / "fixtures"


def test_all_fixtures_present():
    assert (
        sorted(
            [
                "honest-readonly",
                "honest-idempotent",
                "liar-idempotent",
                "liar-concurrent",
                "liar-readonly",
                "silent-safe",
                "opaque",
                "flaky",
                "malformed-hints",
                "liar-readonly-error",
                "startup-writer",
            ]
        )
        == FIXTURES
    )


@pytest.mark.parametrize("name", FIXTURES)
def test_every_tool_has_ground_truth_for_every_implemented_test(name):
    fx = fixtures.load(ROOT / name)
    assert fx.tools
    for tool in fx.tools:
        missing = [t.value for t in IMPLEMENTED if t not in tool.expected]
        assert not missing, f"{name}/{tool.name} has no expectation for {missing}"


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_matches_ground_truth(name, tmp_path):
    fx = fixtures.load(ROOT / name)
    run = asyncio.run(run_fixture(fx, tmp_path))
    report = reports.build(run, derive(run), f"test-{name}")
    exp = report["expectations"]
    assert exp["mismatches"] == [], reports.render_text(report)
    assert exp["checked"] == len(fx.tools) * len(IMPLEMENTED)
    assert report["reset_verification"]["status"] == "verified"
    assert report["listing"]["stable"]

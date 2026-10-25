"""The harness is correct when every fixture gets exactly its expected verdicts.

Runs each fixture end to end (real servers, 3 runs) and compares every
verdict against fixture.toml. Slowest test in the suite, and the one that
matters most.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt import report as reports
from mcpgt.runner import run_fixture
from mcpgt.sandbox import IsolationUnavailable, isolation
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
                "network-side-effects",
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
        if fx.network is not None:
            assert tool.expected_network is not None, f"{name}/{tool.name} has no network expectation"


def network_skip(fx):
    """Why a fixture with [network] cannot run here, or None."""
    if fx.network is None:
        return None
    try:
        isolation()
    except IsolationUnavailable as exc:
        return str(exc)
    if fx.network.capture and sys.platform != "darwin":
        return "the capture endpoint needs the macOS sandbox"
    return None


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_matches_ground_truth(name, tmp_path):
    fx = fixtures.load(ROOT / name)
    skip = network_skip(fx)
    if skip:
        pytest.skip(skip)
    run = asyncio.run(run_fixture(fx, tmp_path))
    report = reports.build(run, derive(run), f"test-{name}")
    exp = report["expectations"]
    assert exp["mismatches"] == [], reports.render_text(report)
    assert exp["checked"] == len(fx.tools) * (len(IMPLEMENTED) + (fx.network is not None))
    assert report["reset_verification"]["status"] == "verified"
    assert report["listing"]["stable"]

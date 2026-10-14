"""Argument variants for one tool, and observation-only concurrent attempts."""

import asyncio
import shutil
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt import report as reports
from mcpgt.runner import run_fixture
from mcpgt.verdict import InconclusiveReason, Outcome, TestKind, derive

FIXTURES = Path(__file__).parent.parent / "fixtures"


def with_variant(tmp_path: Path) -> Path:
    """honest-idempotent plus a second argument set for the same tool."""
    root = tmp_path / "fx"
    shutil.copytree(FIXTURES / "honest-idempotent", root, ignore=shutil.ignore_patterns("__pycache__"))
    with (root / "fixture.toml").open("a") as f:
        f.write(
            """
[[tools]]
name = "upsert_item"
variant = "new-key"
arguments = { key = "zz", value = "fresh" }

[tools.expected.sequential_idempotence]
verdict = "PASS"

[tools.expected.concurrent_idempotence]
verdict = "PASS"

[tools.expected.read_only]
verdict = "NOT_APPLICABLE:PERMISSIVE_DECLARATION"

[tools.expected.destructive]
verdict = "NOT_APPLICABLE:PERMISSIVE_DECLARATION"
"""
        )
    return root


def test_variants_have_distinct_keys(tmp_path):
    fx = fixtures.load(with_variant(tmp_path))
    assert [t.key for t in fx.tools] == ["upsert_item", "upsert_item[new-key]"]
    assert {t.name for t in fx.tools} == {"upsert_item"}


@pytest.mark.parametrize("variant, message", [(None, "distinct variant"), ('"New Key"', "lowercase")])
def test_variant_rules(tmp_path, variant, message):
    root = with_variant(tmp_path)
    text = (root / "fixture.toml").read_text()
    replacement = "" if variant is None else f"variant = {variant}"
    (root / "fixture.toml").write_text(text.replace('variant = "new-key"', replacement))
    with pytest.raises(fixtures.FixtureError, match=message):
        fixtures.load(root)


def test_each_variant_gets_its_own_verdicts_and_sessions(tmp_path):
    fx = fixtures.load(with_variant(tmp_path))
    run = asyncio.run(run_fixture(fx, tmp_path / "work", concurrent_attempts=2))
    report = reports.build(run, derive(run), "t")
    assert [t["tool"] for t in report["tools"]] == ["upsert_item", "upsert_item[new-key]"]
    variant = report["tools"][1]
    assert (variant["name"], variant["variant"]) == ("upsert_item", "new-key")
    assert variant["arguments"] == {"key": "zz", "value": "fresh"}
    assert variant["sessions"][0]["label"] == "upsert_item[new-key]/once-1"
    assert report["expectations"]["mismatches"] == []
    assert report["expectations"]["checked"] == 8


def test_only_tool_selects_all_variants_or_one(tmp_path):
    fx = fixtures.load(with_variant(tmp_path))
    one = asyncio.run(run_fixture(fx, tmp_path / "a", only_tools=["upsert_item[new-key]"], concurrent_attempts=1))
    assert [ev.tool for ev in one.tools] == ["upsert_item[new-key]"]


def test_observe_concurrent_runs_attempts_without_changing_the_verdict(tmp_path):
    fx = fixtures.load(FIXTURES / "silent-safe")
    plain = asyncio.run(run_fixture(fx, tmp_path / "plain", concurrent_attempts=2))
    observed = asyncio.run(run_fixture(fx, tmp_path / "obs", concurrent_attempts=2, observe_concurrent=True))

    def concurrent(run):
        (v,) = [v for v in derive(run) if v.test is TestKind.CONCURRENT_IDEMPOTENCE]
        return v

    before, after = concurrent(plain), concurrent(observed)
    assert (before.outcome, before.reason) == (Outcome.INCONCLUSIVE, InconclusiveReason.NO_DECLARATION)
    assert (after.outcome, after.reason) == (before.outcome, before.reason)
    assert before.attempts == () and len(after.attempts) == 4
    assert "observation only" in after.concurrency_note
    assert after.to_json()["concurrency"]["modes"]["two_process"]["attempts"] == 2

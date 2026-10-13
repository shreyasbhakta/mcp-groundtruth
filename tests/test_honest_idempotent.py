"""honest-idempotent end to end: real server, real calls, real snapshots.

The once-vs-twice comparison here is done by hand; step 4 turns it into the
runner's sequential-idempotence test with repeated independent runs.
"""

import asyncio
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt.contracts import Hint, parse_listing
from mcpgt.runner import deltas, normalized, observe, reset, start_server
from mcpgt.snapshot import StateCategory

FX = fixtures.load(Path(__file__).parent.parent / "fixtures" / "honest-idempotent")
TOOL = FX.tools[0]


def session(state_dir, calls):
    """reset -> start server -> list tools -> call `calls` times -> stop -> snapshot."""

    async def go():
        reset(FX, state_dir)
        before = observe(FX, state_dir)
        async with await start_server(FX, state_dir) as client:
            pages = await client.list_tools()
            responses = [await client.call_tool(TOOL.name, TOOL.arguments) for _ in range(calls)]
        return before, parse_listing([p.text for p in pages]), responses, observe(FX, state_dir)

    return asyncio.run(go())


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    return {
        "once": session(tmp_path_factory.mktemp("once") / "state", 1),
        "twice": session(tmp_path_factory.mktemp("twice") / "state", 2),
        "once_again": session(tmp_path_factory.mktemp("again") / "state", 1),
    }


def test_declared_contract(runs):
    _, listing, _, _ = runs["once"]
    contract = listing.contracts["upsert_item"]
    assert contract.read_only is Hint.FALSE
    assert contract.destructive is Hint.TRUE
    assert contract.idempotent is Hint.TRUE
    assert contract.open_world is Hint.FALSE
    assert contract.anomalies == ()
    assert listing.pages[0].startswith('{"jsonrpc":"2.0"')


def test_calls_succeed(runs):
    for _, _, responses, _ in runs.values():
        for r in responses:
            assert r.result is not None and r.result.get("isError") is False


def test_one_call_overwrites_business_and_appends_audit(runs):
    s0, _, _, s1 = runs["once"]
    (delta,) = deltas(FX, s0, s1)
    (items,) = delta.in_category(StateCategory.BUSINESS)
    assert items.removed == ((("text", "a"), ("text", "1")),)
    assert items.added == ((("text", "a"), ("text", "new")),)
    (audit,) = delta.in_category(StateCategory.APPEND_ONLY)
    assert len(audit.added) == 1 and audit.removed == ()
    assert not delta.changed(StateCategory.UNCLASSIFIED)


def test_final_state_equivalence_over_business_not_append_only(runs):
    _, _, _, once = runs["once"]
    _, _, _, twice = runs["twice"]
    (delta,) = deltas(FX, once, twice)
    assert not delta.changed(StateCategory.BUSINESS)
    (audit,) = delta.in_category(StateCategory.APPEND_ONLY)
    assert len(audit.added) == 1 and audit.removed == ()


def test_identical_runs_give_identical_normalized_state(runs):
    _, _, _, once = runs["once"]
    _, _, _, again = runs["once_again"]
    assert once != again  # raw state differs: timestamps and request ids
    assert normalized(FX, once) == normalized(FX, again)

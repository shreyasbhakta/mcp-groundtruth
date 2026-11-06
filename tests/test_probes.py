"""Probe verdicts from synthetic sessions: each way a comparison can go."""

from types import SimpleNamespace

import pytest

from mcpgt.contracts import parse_tool
from mcpgt.fixture import ProbeSpec
from mcpgt.probes import ProbeTest, canonical, judge, probe_test
from mcpgt.runner import ProbeSessions, Session
from mcpgt.snapshot import RegionDelta, SnapshotDelta, StateCategory
from mcpgt.verdict import InconclusiveReason, NotApplicableReason, Observation, Outcome, ResetStatus
from mcpgt.wire import RawResponse

READ_ONLY = {"readOnlyHint": True}
ROW = (("text", "x"),)
IDEMPOTENT = {"readOnlyHint": False, "idempotentHint": True}


def response(text, error=False):
    result = {"content": [{"type": "text", "text": text}], "isError": error}
    return RawResponse(text, {"jsonrpc": "2.0", "id": 7, "result": result})


def session(label, *outputs, tool_error=False, transport_error=None):
    """A session whose calls returned ``outputs``; the last call is the probe's."""
    responses = tuple(response(o, error=tool_error and i == 0) for i, o in enumerate(outputs))
    return Session(label, ("x",) * len(outputs), wire_error=transport_error, responses=responses, after={})


def runs(*outputs, **kw):
    return tuple(session(f"s{i}", *out, **kw) for i, out in enumerate(outputs))


def verdict(hints, alone, once, twice=(), ignore=(), reset=ResetStatus.VERIFIED, effect=()):
    ev = SimpleNamespace(
        tool="x",
        contract=parse_tool({"name": "x", "annotations": hints}),
        contract_error=None,
        contract_instability=(),
        probe_once=(ProbeSessions("p", once),),
        probe_twice=(ProbeSessions("p", twice),) if twice else (),
        effect=effect,
        idempotence=(),
    )
    run = SimpleNamespace(
        fixture=SimpleNamespace(probes=ProbeSpec(("p",), tuple(ignore))),
        reset=SimpleNamespace(status=reset, detail="reset detail"),
        probe_baselines=(ProbeSessions("p", alone),),
    )
    return judge(run, ev)


ALONE = runs(["a"], ["a"], ["a"])


def test_read_only_violation_needs_a_change_in_every_run():
    v = verdict(READ_ONLY, ALONE, runs(["", "b"], ["", "b"], ["", "b"]))
    assert v.outcome is Outcome.VIOLATION
    assert v.test is ProbeTest.READ_ONLY
    assert v.hidden_state is None  # no state observations were given


def test_read_only_holds_when_the_probe_never_changes():
    assert verdict(READ_ONLY, ALONE, runs(["", "a"], ["", "a"], ["", "a"])).outcome is Outcome.PASS


def test_a_change_in_some_runs_only_is_not_a_violation():
    v = verdict(READ_ONLY, ALONE, runs(["", "b"], ["", "a"], ["", "a"]))
    assert (v.outcome, v.reason) == (Outcome.INCONCLUSIVE, InconclusiveReason.NONDETERMINISTIC)
    assert "1 of 3 runs" in v.detail


def test_a_probe_that_varies_on_its_own_cannot_decide():
    v = verdict(READ_ONLY, runs(["a"], ["a2"], ["a"]), runs(["", "b"], ["", "b"], ["", "b"]))
    assert (v.outcome, v.reason) == (Outcome.INCONCLUSIVE, InconclusiveReason.NONDETERMINISTIC)
    assert "alone" in v.detail


def test_ignore_patterns_remove_volatile_parts_before_comparing():
    alone = runs(["n=1 t=101"], ["n=1 t=202"], ["n=1 t=303"])
    once = runs(["", "n=1 t=404"], ["", "n=1 t=505"], ["", "n=1 t=606"])
    assert verdict(READ_ONLY, alone, once, ignore=[r"t=\d+"]).outcome is Outcome.PASS
    assert verdict(READ_ONLY, alone, once).reason is InconclusiveReason.NONDETERMINISTIC


def test_idempotence_compares_twice_with_once():
    once = runs(["", "b"], ["", "b"], ["", "b"])
    same = runs(["", "", "b"], ["", "", "b"], ["", "", "b"])
    more = runs(["", "", "c"], ["", "", "c"], ["", "", "c"])
    assert verdict(IDEMPOTENT, ALONE, once, same).outcome is Outcome.PASS
    v = verdict(IDEMPOTENT, ALONE, once, more)
    assert (v.outcome, v.test) == (Outcome.VIOLATION, ProbeTest.IDEMPOTENCE)


def test_a_probe_that_errors_alone_is_unusable():
    v = verdict(READ_ONLY, runs(["a"], ["a"], ["a"], tool_error=True), runs(["", "b"], ["", "b"], ["", "b"]))
    assert (v.outcome, v.reason) == (Outcome.INCONCLUSIVE, InconclusiveReason.SETUP_FAILED)


def test_a_tool_error_with_no_probe_change_is_a_call_error():
    v = verdict(READ_ONLY, ALONE, runs(["", "a"], ["", "a"], ["", "a"], tool_error=True))
    assert (v.outcome, v.reason) == (Outcome.INCONCLUSIVE, InconclusiveReason.CALL_ERROR)


def test_a_dead_connection_is_a_call_error():
    once = (*runs(["", "b"], ["", "b"]), session("dead", "", transport_error="EOF"))
    assert verdict(READ_ONLY, ALONE, once).reason is InconclusiveReason.CALL_ERROR


def test_reset_problems_block_the_verdict():
    once = runs(["", "b"], ["", "b"], ["", "b"])
    assert verdict(READ_ONLY, ALONE, once, reset=ResetStatus.UNVERIFIED).reason is InconclusiveReason.RESET_UNVERIFIED


@pytest.mark.parametrize(
    ("hints", "outcome", "reason"),
    [
        (
            {"readOnlyHint": False, "idempotentHint": False},
            Outcome.NOT_APPLICABLE,
            NotApplicableReason.PERMISSIVE_DECLARATION,
        ),
        ({"readOnlyHint": False}, Outcome.INCONCLUSIVE, InconclusiveReason.NO_DECLARATION),
        (
            {"readOnlyHint": False, "idempotentHint": "yes"},
            Outcome.INCONCLUSIVE,
            InconclusiveReason.MALFORMED_DECLARATION,
        ),
    ],
)
def test_the_declaration_alone_decides_when_there_is_no_claim(hints, outcome, reason):
    assert probe_test(parse_tool({"name": "x", "annotations": hints})) is None
    v = verdict(hints, ALONE, ())
    assert (v.outcome, v.reason) == (outcome, reason)


def test_canonical_ignores_the_request_id():
    a = {"jsonrpc": "2.0", "id": 1, "result": {"v": "x"}}
    b = {"jsonrpc": "2.0", "id": 9, "result": {"v": "x"}}
    assert canonical(a, ()) == canonical(b, ())


def test_hidden_state_says_whether_the_state_adapters_saw_the_change():
    once = runs(["", "b"], ["", "b"], ["", "b"])
    unchanged = Observation("control", (SnapshotDelta("sqlite:s", ()),))
    assert verdict(READ_ONLY, ALONE, once, effect=(unchanged,) * 3).hidden_state is True
    region = RegionDelta("t", StateCategory.BUSINESS, ("v",), ("v",), added=(ROW,), removed=())
    changed = Observation("control", (SnapshotDelta("sqlite:s", (region,)),))
    assert verdict(READ_ONLY, ALONE, once, effect=(changed,) * 3).hidden_state is False


def test_excerpts_start_near_the_first_difference():
    long = "x" * 1000
    v = verdict(READ_ONLY, runs([long + "a"], [long + "a"], [long + "a"]), runs(*[["", long + "b"]] * 3))
    result = v.to_json()["probes"][0]
    # 60 characters of context, then the difference
    assert result["reference_excerpt"].startswith("x" * 60 + "a")
    assert result["changed_excerpt"].startswith("x" * 60 + "b")

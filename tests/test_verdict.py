"""Verdict derivation against synthetic evidence, one rule at a time."""

from types import SimpleNamespace

import pytest

from mcpgt.contracts import Annotation, parse_tool
from mcpgt.fixture import FixtureCoverage
from mcpgt.snapshot import Coverage, RegionDelta, SnapshotDelta, StateCategory
from mcpgt.verdict import (
    Behavior,
    Incoherence,
    InconclusiveReason,
    Observation,
    Outcome,
    ResetStatus,
    TestKind,
    coherence,
    derive,
)

B, A, U = StateCategory.BUSINESS, StateCategory.APPEND_ONLY, StateCategory.UNCLASSIFIED
ROW = (("text", "x"),)
FULL = FixtureCoverage(adapters=(Coverage("sqlite:s", ("all",), ()),), unobserved=())
PARTIAL = FixtureCoverage(adapters=(Coverage("sqlite:s", ("all",), ()),), unobserved=("memory:process",))


def obs(baseline, *, added=(), removed=()):
    """An observation where `added` / `removed` list the categories that changed."""
    regions = []
    for cat in sorted(set(added) | set(removed), key=lambda c: c.value):
        regions.append(
            RegionDelta(
                region=cat.value,
                category=cat,
                columns_before=("v",),
                columns_after=("v",),
                added=(ROW,) if cat in added else (),
                removed=(ROW,) if cat in removed else (),
            )
        )
    return Observation(baseline, (SnapshotDelta("sqlite:s", tuple(regions)),))


def contract(**hints):
    return parse_tool({"name": "t", "annotations": hints})


def evidence(
    hints,
    *,
    idem=None,
    effect=None,
    once_stable=True,
    twice_stable=True,
    call_error=None,
    setup_error=None,
    contract_stable=True,
    runs=3,
):
    clean_i, clean_e = obs("once"), obs("control")
    return SimpleNamespace(
        tool="t",
        contract=contract(**hints) if isinstance(hints, dict) else hints,
        contract_error=None,
        contract_stable=contract_stable,
        idempotence=tuple(idem if idem is not None else [clean_i] * runs),
        effect=tuple(effect if effect is not None else [clean_e] * runs),
        once_stable=once_stable,
        twice_stable=twice_stable,
        call_error=call_error,
        setup_error=setup_error,
    )


def fixture_run(ev, *, reset=ResetStatus.VERIFIED, coverage=FULL, control_stable=True, control_error=None, runs=3):
    return SimpleNamespace(
        tools=(ev,),
        reset=SimpleNamespace(status=reset, detail="detail"),
        coverage=coverage,
        control_stable=control_stable,
        control_error=control_error,
        runs=runs,
    )


def verdict_for(test, ev, **run_kw):
    (v,) = [v for v in derive(fixture_run(ev, **run_kw)) if v.test is test]
    return v


SEQ, RO, DE = TestKind.SEQUENTIAL_IDEMPOTENCE, TestKind.READ_ONLY, TestKind.DESTRUCTIVE
CLAIMS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}


def outcome(v):
    return (v.outcome, v.reason)


# --- Declaration rules come first ------------------------------------------


@pytest.mark.parametrize("test", [SEQ, RO, DE])
def test_absent_hint_is_no_declaration_even_when_the_run_is_broken(test):
    v = verdict_for(test, evidence({}, call_error="boom"), reset=ResetStatus.UNVERIFIED)
    assert outcome(v) == (Outcome.INCONCLUSIVE, InconclusiveReason.NO_DECLARATION)


def test_malformed_hint_is_malformed_declaration():
    v = verdict_for(SEQ, evidence({"idempotentHint": "true"}))
    assert outcome(v) == (Outcome.INCONCLUSIVE, InconclusiveReason.MALFORMED_DECLARATION)


def test_undeclared_tool_still_reports_observed_behavior():
    v = verdict_for(SEQ, evidence({}))
    assert v.reason is InconclusiveReason.NO_DECLARATION
    assert v.behavior is Behavior.HOLDS
    assert len(v.observations) == 3


@pytest.mark.parametrize(
    "test, hints",
    [
        (RO, {"readOnlyHint": False}),
        (DE, {"destructiveHint": True}),
        (SEQ, {"idempotentHint": False}),
    ],
)
def test_permissive_hints_are_not_applicable_with_behavior_attached(test, hints):
    ev = evidence(hints, idem=[obs("once", added=[B])] * 3, effect=[obs("control", added=[B], removed=[B])] * 3)
    v = verdict_for(test, ev)
    assert outcome(v) == (Outcome.NOT_APPLICABLE, None)
    assert v.behavior is Behavior.FAILS
    assert v.to_json()["observed"]["runs"]


# --- Harness-trust rules ----------------------------------------------------


@pytest.mark.parametrize(
    "reset, reason",
    [(ResetStatus.UNVERIFIED, InconclusiveReason.RESET_UNVERIFIED), (ResetStatus.SETUP_FAILED, InconclusiveReason.SETUP_FAILED)],
)
@pytest.mark.parametrize("test", [SEQ, RO, DE])
def test_reset_problems_block_every_falsifiable_verdict(test, reset, reason):
    assert verdict_for(test, evidence(CLAIMS), reset=reset).reason is reason


def test_call_error():
    v = verdict_for(SEQ, evidence(CLAIMS, call_error="tool returned isError"))
    assert outcome(v) == (Outcome.INCONCLUSIVE, InconclusiveReason.CALL_ERROR)


def test_setup_error_in_tool_sessions():
    assert verdict_for(RO, evidence(CLAIMS, setup_error="did not start")).reason is InconclusiveReason.SETUP_FAILED


def test_unstable_contract():
    assert verdict_for(SEQ, evidence(CLAIMS, contract_stable=False)).reason is InconclusiveReason.NONDETERMINISTIC


def test_unstable_control_blocks_read_only_and_destructive_only():
    ev = evidence(CLAIMS)
    for test in (RO, DE):
        v = verdict_for(test, ev, control_stable=False)
        assert outcome(v) == (Outcome.INCONCLUSIVE, InconclusiveReason.NONDETERMINISTIC)
        assert "control" in v.detail
    assert verdict_for(SEQ, ev, control_stable=False).outcome is Outcome.PASS


def test_failed_control_session_is_setup_failure_for_control_based_tests():
    assert verdict_for(RO, evidence(CLAIMS), control_error="crash").reason is InconclusiveReason.SETUP_FAILED


def test_runs_disagreeing_is_nondeterministic():
    ev = evidence(CLAIMS, idem=[obs("once", added=[B]), obs("once"), obs("once", added=[B])])
    v = verdict_for(SEQ, ev)
    assert outcome(v) == (Outcome.INCONCLUSIVE, InconclusiveReason.NONDETERMINISTIC)
    assert v.behavior is Behavior.UNSTABLE


def test_states_differing_across_runs_is_nondeterministic_even_if_findings_agree():
    v = verdict_for(SEQ, evidence(CLAIMS, once_stable=False))
    assert v.reason is InconclusiveReason.NONDETERMINISTIC


def test_violation_needs_three_complete_runs():
    ev = evidence(CLAIMS, idem=[obs("once", added=[B])] * 2)
    v = verdict_for(SEQ, ev)
    assert outcome(v) == (Outcome.INCONCLUSIVE, InconclusiveReason.NONDETERMINISTIC)


# --- Observation rules ------------------------------------------------------


def test_sequential_idempotence_violation_and_pass():
    assert verdict_for(SEQ, evidence(CLAIMS, idem=[obs("once", added=[B])] * 3)).outcome is Outcome.VIOLATION
    assert verdict_for(SEQ, evidence(CLAIMS, idem=[obs("once", added=[A])] * 3)).outcome is Outcome.PASS


def test_unclassified_change_is_unclassified_state_for_idempotence():
    v = verdict_for(SEQ, evidence(CLAIMS, idem=[obs("once", added=[U])] * 3))
    assert outcome(v) == (Outcome.INCONCLUSIVE, InconclusiveReason.UNCLASSIFIED_STATE)


@pytest.mark.parametrize("category", [B, A, U])
def test_read_only_violated_by_change_in_any_category(category):
    v = verdict_for(RO, evidence(CLAIMS, effect=[obs("control", added=[category])] * 3))
    assert outcome(v) == (Outcome.VIOLATION, None)


@pytest.mark.parametrize(
    "removed, expected",
    [
        ([B], (Outcome.VIOLATION, None)),
        ([A], (Outcome.VIOLATION, None)),
        ([U], (Outcome.INCONCLUSIVE, InconclusiveReason.UNCLASSIFIED_STATE)),
        ([], (Outcome.PASS, None)),
    ],
)
def test_destructive_depends_on_removals(removed, expected):
    ev = evidence(CLAIMS, effect=[obs("control", added=[B], removed=removed)] * 3)
    assert outcome(verdict_for(DE, ev)) == expected


def test_additions_alone_are_not_destructive():
    ev = evidence(CLAIMS, effect=[obs("control", added=[B, A])] * 3)
    assert verdict_for(DE, ev).outcome is Outcome.PASS


def test_partial_coverage_blocks_pass_but_not_violation():
    clean = evidence(CLAIMS)
    for test in (SEQ, RO, DE):
        assert verdict_for(test, clean, coverage=PARTIAL).reason is InconclusiveReason.PARTIAL_COVERAGE
    dirty = evidence(CLAIMS, effect=[obs("control", added=[B])] * 3)
    assert verdict_for(RO, dirty, coverage=PARTIAL).outcome is Outcome.VIOLATION


def test_every_verdict_carries_its_evidence():
    for v in derive(fixture_run(evidence(CLAIMS))):
        data = v.to_json()
        assert data["contract"]["hints"]["readOnlyHint"] == "true"
        assert data["observed"]["runs"] and "deltas" in data["observed"]["runs"][0]
        assert set(data["observed"]["runs"][0]["changed"]) == {"business", "append_only", "unclassified"}
        assert data["coverage"]["complete"] is True
        assert data["run_count"] == 3


# --- Coherence --------------------------------------------------------------


@pytest.mark.parametrize(
    "hints, issues",
    [
        ({"readOnlyHint": True, "destructiveHint": True}, {Incoherence.READ_ONLY_AND_DESTRUCTIVE}),
        ({"readOnlyHint": True, "idempotentHint": False}, {Incoherence.READ_ONLY_NOT_IDEMPOTENT}),
        ({"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}, set()),
        ({"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}, set()),
        ({"destructiveHint": True}, set()),  # absent readOnlyHint is not read as false or true
    ],
)
def test_coherence(hints, issues):
    assert set(coherence(contract(**hints))) == issues


def test_hint_mapping_covers_every_test():
    from mcpgt.verdict import HINT_FOR

    assert set(HINT_FOR) == set(TestKind)
    assert HINT_FOR[RO] is Annotation.READ_ONLY

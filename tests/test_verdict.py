"""Verdict derivation against synthetic evidence, one rule at a time."""

from types import SimpleNamespace

import pytest

from mcpgt.contracts import Annotation, parse_tool
from mcpgt.fixture import FixtureCoverage
from mcpgt.snapshot import Coverage, RegionDelta, SnapshotDelta, StateCategory
from mcpgt.verdict import (
    HINT_FOR,
    Attempt,
    Behavior,
    ConcurrencyMode,
    Overlap,
    Incoherence,
    InconclusiveReason as IR,
    Instability,
    NotApplicableReason as NA,
    Observation,
    Outcome,
    ResetStatus,
    TestKind,
    coherence,
    derive,
    instability_between,
)

B, A, U = StateCategory.BUSINESS, StateCategory.APPEND_ONLY, StateCategory.UNCLASSIFIED
ROW = (("text", "x"),)
FULL = FixtureCoverage(adapters=(Coverage("sqlite:s", ("all",), ()),), unobserved=())
PARTIAL = FixtureCoverage(adapters=(Coverage("sqlite:s", ("all",), ()),), unobserved=("memory:process",))
SEQ, RO, DE = TestKind.SEQUENTIAL_IDEMPOTENCE, TestKind.READ_ONLY, TestKind.DESTRUCTIVE
CO = TestKind.CONCURRENT_IDEMPOTENCE

# Falsifiable claims for each test, without readOnlyHint:true superseding the others.
WRITER = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
READER = {"readOnlyHint": True}
CLAIM_FOR = {SEQ: WRITER, CO: WRITER, DE: WRITER, RO: READER}
UNSTABLE = Instability(("t/once-1", "t/once-2"), ("sqlite:s/items.value",), 1, 1)


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
    once_instability=(),
    twice_instability=(),
    call_error=None,
    setup_error=None,
    contract_instability=(),
    errored_with_side_effects=False,
    attempts=None,
    concurrent_note=None,
):
    return SimpleNamespace(
        errors=(),
        concurrent=tuple(attempts or ()),
        concurrent_ran=attempts is not None,
        concurrent_note=concurrent_note,
        concurrent_setup_error=None,
        concurrent_call_error=None,
        concurrent_errors=(),
        tool="t",
        contract=contract(**hints),
        contract_error=None,
        contract_instability=contract_instability,
        idempotence=tuple(idem if idem is not None else [obs("once")] * 3),
        effect=tuple(effect if effect is not None else [obs("control")] * 3),
        once_instability=once_instability,
        twice_instability=twice_instability,
        call_error=call_error,
        setup_error=setup_error,
        errored_with_side_effects=errored_with_side_effects,
    )


def fixture_run(ev, *, reset=ResetStatus.VERIFIED, coverage=FULL, control_instability=(), control_error=None):
    return SimpleNamespace(
        tools=(ev,),
        reset=SimpleNamespace(status=reset, detail="detail"),
        coverage=coverage,
        control_instability=control_instability,
        control_error=control_error,
        runs=3,
    )


def verdict_for(test, ev, **run_kw):
    (v,) = [v for v in derive(fixture_run(ev, **run_kw)) if v.test is test]
    return v


def outcome(v):
    return (v.outcome, v.reason)


def claim(test, **kw):
    return evidence(CLAIM_FOR[test], **kw)


# --- Declaration rules come first ------------------------------------------


@pytest.mark.parametrize("test", [SEQ, RO, DE])
def test_absent_hint_is_no_declaration_and_keeps_run_problems_visible(test):
    v = verdict_for(test, evidence({}, call_error="boom"), reset=ResetStatus.UNVERIFIED)
    assert outcome(v) == (Outcome.INCONCLUSIVE, IR.NO_DECLARATION)
    assert v.secondary_reasons == (IR.RESET_UNVERIFIED, IR.CALL_ERROR)
    assert v.to_json()["secondary_reasons"] == ["RESET_UNVERIFIED", "CALL_ERROR"]


def test_malformed_hint_is_malformed_declaration():
    v = verdict_for(SEQ, evidence({"idempotentHint": "true"}))
    assert outcome(v) == (Outcome.INCONCLUSIVE, IR.MALFORMED_DECLARATION)
    assert v.secondary_reasons == ()


def test_undeclared_tool_still_reports_observed_behavior():
    v = verdict_for(SEQ, evidence({}))
    assert v.reason is IR.NO_DECLARATION
    assert v.behavior is Behavior.HOLDS
    assert len(v.observations) == 3


@pytest.mark.parametrize(
    "test, hints",
    [(RO, {"readOnlyHint": False}), (DE, {"destructiveHint": True}), (SEQ, {"idempotentHint": False})],
)
def test_permissive_hints_are_not_applicable_with_behavior_attached(test, hints):
    ev = evidence(hints, idem=[obs("once", added=[B])] * 3, effect=[obs("control", added=[B], removed=[B])] * 3)
    v = verdict_for(test, ev)
    assert outcome(v) == (Outcome.NOT_APPLICABLE, NA.PERMISSIVE_DECLARATION)
    assert v.behavior is Behavior.FAILS
    assert v.to_json()["observed"]["runs"]


# --- readOnlyHint:true supersedes destructive and idempotence ----------------


@pytest.mark.parametrize("test", [SEQ, DE])
@pytest.mark.parametrize(
    "others",
    [{}, {"destructiveHint": False, "idempotentHint": True}, {"destructiveHint": True, "idempotentHint": False}],
)
def test_explicit_read_only_supersedes_whatever_else_is_declared(test, others):
    ev = evidence({"readOnlyHint": True, **others}, idem=[obs("once", added=[B])] * 3, effect=[obs("control", removed=[B])] * 3)
    v = verdict_for(test, ev)
    assert outcome(v) == (Outcome.NOT_APPLICABLE, NA.SUPERSEDED_BY_READ_ONLY)
    assert v.behavior is Behavior.FAILS  # observed behavior still attached


def test_read_only_test_itself_is_not_superseded():
    assert verdict_for(RO, evidence(READER)).outcome is Outcome.PASS


@pytest.mark.parametrize("read_only", [{}, {"readOnlyHint": "true"}, {"readOnlyHint": None}])
def test_only_explicit_true_supersedes(read_only):
    v = verdict_for(SEQ, evidence({**read_only, "idempotentHint": True}))
    assert v.outcome is Outcome.PASS


# --- Harness-trust rules ----------------------------------------------------


@pytest.mark.parametrize("reset, reason", [(ResetStatus.UNVERIFIED, IR.RESET_UNVERIFIED), (ResetStatus.SETUP_FAILED, IR.SETUP_FAILED)])
@pytest.mark.parametrize("test", [SEQ, RO, DE])
def test_reset_problems_block_every_falsifiable_verdict(test, reset, reason):
    assert verdict_for(test, claim(test), reset=reset).reason is reason


def test_call_error_with_secondary_reasons():
    v = verdict_for(SEQ, claim(SEQ, call_error="isError with no state change"), coverage=PARTIAL)
    assert outcome(v) == (Outcome.INCONCLUSIVE, IR.CALL_ERROR)
    assert v.secondary_reasons == (IR.PARTIAL_COVERAGE,)


def test_errored_with_side_effects_is_judged_on_state():
    ev = claim(RO, effect=[obs("control", added=[U])] * 3, errored_with_side_effects=True)
    v = verdict_for(RO, ev)
    assert outcome(v) == (Outcome.VIOLATION, None)
    assert v.to_json()["observed"]["errored_with_side_effects"] is True


def test_setup_error_in_tool_sessions():
    assert verdict_for(RO, claim(RO, setup_error="did not start")).reason is IR.SETUP_FAILED


def test_unstable_contract_names_the_hints():
    v = verdict_for(SEQ, claim(SEQ, contract_instability=("idempotentHint",)))
    assert v.reason is IR.NONDETERMINISTIC
    assert "idempotentHint" in v.issues[0].detail


def test_unstable_control_blocks_read_only_and_destructive_only():
    unstable = (Instability(("control-1", "control-2"), ("sqlite:s/boot.at",), 1, 1),)
    for test in (RO, DE):
        v = verdict_for(test, claim(test), control_instability=unstable)
        assert outcome(v) == (Outcome.INCONCLUSIVE, IR.NONDETERMINISTIC)
        assert "sqlite:s/boot.at" in v.issues[0].detail
    assert verdict_for(SEQ, claim(SEQ), control_instability=unstable).outcome is Outcome.PASS


def test_failed_control_session_is_setup_failure_for_control_based_tests():
    assert verdict_for(RO, claim(RO), control_error="crash").reason is IR.SETUP_FAILED
    assert verdict_for(SEQ, claim(SEQ), control_error="crash").outcome is Outcome.PASS


def test_nondeterminism_evidence_names_columns():
    v = verdict_for(SEQ, claim(SEQ, idem=[obs("once", added=[B])] * 3, once_instability=(UNSTABLE,)))
    assert outcome(v) == (Outcome.INCONCLUSIVE, IR.NONDETERMINISTIC)
    assert v.behavior is Behavior.UNSTABLE
    assert "sqlite:s/items.value" in v.issues[0].detail
    (nd,) = v.to_json()["observed"]["nondeterminism"]
    assert nd["columns"] == ["sqlite:s/items.value"]
    assert nd["compared"] == ["t/once-1", "t/once-2"]


def test_runs_disagreeing_is_nondeterministic():
    ev = claim(SEQ, idem=[obs("once", added=[B]), obs("once"), obs("once", added=[B])])
    assert outcome(verdict_for(SEQ, ev)) == (Outcome.INCONCLUSIVE, IR.NONDETERMINISTIC)


def test_violation_needs_three_complete_runs():
    ev = claim(SEQ, idem=[obs("once", added=[B])] * 2)
    assert outcome(verdict_for(SEQ, ev)) == (Outcome.INCONCLUSIVE, IR.NONDETERMINISTIC)


def test_instability_between_names_differing_columns_only():
    region = RegionDelta(
        region="items",
        category=B,
        columns_before=("key", "value"),
        columns_after=("key", "value"),
        added=((("text", "a"), ("text", "2")),),
        removed=((("text", "a"), ("text", "1")),),
    )
    item = instability_between(("x", "y"), [SnapshotDelta("sqlite:s", (region,))])
    assert item.columns == ("sqlite:s/items.value",)
    assert instability_between(("x", "y"), [SnapshotDelta("sqlite:s", ())]) is None


# --- Observation rules ------------------------------------------------------


def test_sequential_idempotence_violation_and_pass():
    assert verdict_for(SEQ, claim(SEQ, idem=[obs("once", added=[B])] * 3)).outcome is Outcome.VIOLATION
    assert verdict_for(SEQ, claim(SEQ, idem=[obs("once", added=[A])] * 3)).outcome is Outcome.PASS


def test_unclassified_change_is_unclassified_state_for_idempotence():
    v = verdict_for(SEQ, claim(SEQ, idem=[obs("once", added=[U])] * 3))
    assert outcome(v) == (Outcome.INCONCLUSIVE, IR.UNCLASSIFIED_STATE)


@pytest.mark.parametrize("category", [B, A, U])
def test_read_only_violated_by_change_in_any_category(category):
    v = verdict_for(RO, claim(RO, effect=[obs("control", added=[category])] * 3))
    assert outcome(v) == (Outcome.VIOLATION, None)
    assert v.affected_categories == (category,)


@pytest.mark.parametrize(
    "removed, expected, categories",
    [
        ([B], (Outcome.VIOLATION, None), ["business"]),
        ([A], (Outcome.VIOLATION, None), ["append_only"]),
        ([B, A], (Outcome.VIOLATION, None), ["business", "append_only"]),
        ([U], (Outcome.INCONCLUSIVE, IR.UNCLASSIFIED_STATE), ["unclassified"]),
        ([], (Outcome.PASS, None), []),
    ],
)
def test_destruction_is_reported_by_sub_category(removed, expected, categories):
    ev = claim(DE, effect=[obs("control", added=[B], removed=removed)] * 3)
    v = verdict_for(DE, ev)
    assert outcome(v) == expected
    assert v.to_json()["observed"]["affected_categories"] == categories


def test_partial_coverage_blocks_pass_but_not_violation():
    for test in (SEQ, RO, DE):
        v = verdict_for(test, claim(test), coverage=PARTIAL)
        assert outcome(v) == (Outcome.INCONCLUSIVE, IR.PARTIAL_COVERAGE)
    v = verdict_for(RO, claim(RO, effect=[obs("control", added=[B])] * 3), coverage=PARTIAL)
    assert outcome(v) == (Outcome.VIOLATION, None)
    assert v.secondary_reasons == (IR.PARTIAL_COVERAGE,)


def test_every_verdict_carries_its_evidence():
    for v in derive(fixture_run(evidence(WRITER, attempts=held(2)))):
        data = v.to_json()
        assert data["contract"]["hints"]["idempotentHint"] == "true"
        observed = data["observed"]
        if v.test is CO:
            first = data["concurrency"]["attempts"][0]
            assert {"mode", "overlap", "wire_overlap_ms", "rtt_ms", "deltas", "diverged"} <= first.keys()
        else:
            assert observed["runs"] and "deltas" in observed["runs"][0]
            assert set(observed["runs"][0]["changed"]) == {"business", "append_only", "unclassified"}
        assert "call_errors" in observed
        assert {"affected_categories", "errored_with_side_effects", "nondeterminism"} <= observed.keys()
        assert data["coverage"]["complete"] is True
        assert data["run_count"] == (2 if v.test is CO else 3)  # concurrent: completed attempts
        assert "secondary_reasons" in data


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
    assert set(HINT_FOR) == set(TestKind)
    assert HINT_FOR[RO] is Annotation.READ_ONLY


# --- Concurrent idempotence: one-sided ---------------------------------------

ONE, TWO = ConcurrencyMode.SINGLE_CONNECTION, ConcurrencyMode.TWO_PROCESS


def attempt(mode, overlap, *, added=()):
    return Attempt(f"t/{mode.value}", mode, overlap, 5.0, (0.2,), obs("once", added=added))


def held(n, mode=TWO, overlap=Overlap.VERIFIED):
    return [attempt(mode, overlap) for _ in range(n)]


def test_a_single_diverging_attempt_is_a_conclusive_violation():
    attempts = held(19) + [attempt(ONE, Overlap.UNVERIFIED, added=[B])]
    v = verdict_for(CO, claim(CO, attempts=attempts))
    assert outcome(v) == (Outcome.VIOLATION, None)
    assert v.produced_by == (ONE,)
    conc = v.to_json()["concurrency"]
    assert conc["modes"]["single_connection"]["diverged"] == 1
    assert conc["produced_by"] == ["single_connection"]
    assert v.runs == 20


def test_pass_needs_verified_overlap_and_names_the_mode():
    v = verdict_for(CO, claim(CO, attempts=held(20, ONE, Overlap.UNVERIFIED) + held(1)))
    assert outcome(v) == (Outcome.PASS, None)
    assert v.produced_by == (TWO,)


@pytest.mark.parametrize(
    "attempts",
    [held(20, ONE, Overlap.UNVERIFIED), held(20, TWO, Overlap.UNVERIFIED), held(20, TWO, Overlap.NONE), []],
)
def test_no_verified_overlap_is_never_pass(attempts):
    v = verdict_for(CO, claim(CO, attempts=attempts))
    assert outcome(v) == (Outcome.INCONCLUSIVE, IR.NO_OVERLAP_OBSERVED)


def test_unstable_single_call_baseline_blocks_even_a_divergence():
    ev = claim(CO, attempts=[attempt(TWO, Overlap.VERIFIED, added=[B])], once_instability=(UNSTABLE,))
    assert verdict_for(CO, ev).reason is IR.NONDETERMINISTIC


def test_concurrent_unclassified_divergence():
    v = verdict_for(CO, claim(CO, attempts=held(5) + [attempt(TWO, Overlap.VERIFIED, added=[U])]))
    assert outcome(v) == (Outcome.INCONCLUSIVE, IR.UNCLASSIFIED_STATE)


def test_concurrent_with_partial_coverage():
    verified = verdict_for(CO, claim(CO, attempts=held(3)), coverage=PARTIAL)
    assert outcome(verified) == (Outcome.INCONCLUSIVE, IR.PARTIAL_COVERAGE)
    assert verified.secondary_reasons == ()
    unverified = verdict_for(CO, claim(CO, attempts=held(3, ONE, Overlap.UNVERIFIED)), coverage=PARTIAL)
    assert outcome(unverified) == (Outcome.INCONCLUSIVE, IR.NO_OVERLAP_OBSERVED)
    assert unverified.secondary_reasons == (IR.PARTIAL_COVERAGE,)
    diverged = verdict_for(CO, claim(CO, attempts=[attempt(ONE, Overlap.UNVERIFIED, added=[B])]), coverage=PARTIAL)
    assert outcome(diverged) == (Outcome.VIOLATION, None)


def test_attempts_not_run_add_no_overlap_noise():
    v = verdict_for(CO, evidence({"readOnlyHint": True}, concurrent_note="attempts not run"))
    assert outcome(v) == (Outcome.NOT_APPLICABLE, NA.SUPERSEDED_BY_READ_ONLY)
    assert IR.NO_OVERLAP_OBSERVED not in v.secondary_reasons

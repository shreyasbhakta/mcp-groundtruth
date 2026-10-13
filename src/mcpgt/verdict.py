"""Verdicts: what the evidence says about each (tool, test).

Pure interpretation. The runner collects evidence (sessions, snapshots,
deltas); this module decides, and attaches the evidence to every verdict.

Only three declared values make a claim that behavior can contradict:
``readOnlyHint: true``, ``destructiveHint: false`` and ``idempotentHint:
true``. The opposite values are permissive ("may write", "may destroy", "may
not be idempotent") and get NOT_APPLICABLE, with the observed behavior still
attached.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mcpgt.contracts import Annotation, DeclaredContract, Hint
from mcpgt.snapshot import SnapshotDelta, StateCategory

if TYPE_CHECKING:
    from mcpgt.fixture import FixtureCoverage
    from mcpgt.runner import FixtureRun, ToolEvidence

MIN_RUNS = 3


class Outcome(enum.Enum):
    PASS = "PASS"
    VIOLATION = "VIOLATION"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # declared value makes no falsifiable claim


class TestKind(enum.Enum):
    __test__ = False  # not a pytest test class

    SEQUENTIAL_IDEMPOTENCE = "sequential_idempotence"
    CONCURRENT_IDEMPOTENCE = "concurrent_idempotence"
    READ_ONLY = "read_only"
    DESTRUCTIVE = "destructive"


class InconclusiveReason(enum.Enum):
    NO_DECLARATION = "NO_DECLARATION"  # hint not sent at all
    MALFORMED_DECLARATION = "MALFORMED_DECLARATION"  # hint sent, but not as a JSON boolean
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    # The only observed change is in a region the fixture did not classify as
    # business or append-only. Applies to the idempotence and destructive tests,
    # whose claims are about business state. It does NOT apply to READ_ONLY:
    # readOnlyHint:true claims no state changes at all, so a change anywhere
    # observed, classified or not, can support a VIOLATION.
    UNCLASSIFIED_STATE = "UNCLASSIFIED_STATE"
    RESET_UNVERIFIED = "RESET_UNVERIFIED"
    NONDETERMINISTIC = "NONDETERMINISTIC"
    CALL_ERROR = "CALL_ERROR"
    SETUP_FAILED = "SETUP_FAILED"


class ResetStatus(enum.Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"  # two resets produced different normalized states
    SETUP_FAILED = "setup_failed"


class Behavior(enum.Enum):
    """What was observed, relative to the falsifiable form of the hint."""

    HOLDS = "holds"  # e.g. observed idempotent
    FAILS = "fails"  # e.g. observed not idempotent
    UNCLASSIFIED_ONLY = "unclassified_only"  # only unclassified regions bear on it
    UNSTABLE = "unstable"  # runs disagree
    UNOBSERVED = "unobserved"  # sessions failed; nothing to judge


HINT_FOR = {
    TestKind.SEQUENTIAL_IDEMPOTENCE: Annotation.IDEMPOTENT,
    TestKind.CONCURRENT_IDEMPOTENCE: Annotation.IDEMPOTENT,
    TestKind.READ_ONLY: Annotation.READ_ONLY,
    TestKind.DESTRUCTIVE: Annotation.DESTRUCTIVE,
}
FALSIFIABLE = {
    Annotation.READ_ONLY: Hint.TRUE,
    Annotation.DESTRUCTIVE: Hint.FALSE,
    Annotation.IDEMPOTENT: Hint.TRUE,
}
PROPERTY = {
    TestKind.SEQUENTIAL_IDEMPOTENCE: "idempotent",
    TestKind.CONCURRENT_IDEMPOTENCE: "idempotent",
    TestKind.READ_ONLY: "read_only",
    TestKind.DESTRUCTIVE: "non_destructive",
}
# Tests compared against the control session rather than against each other.
CONTROL_BASED = frozenset({TestKind.READ_ONLY, TestKind.DESTRUCTIVE})
IMPLEMENTED = (TestKind.SEQUENTIAL_IDEMPOTENCE, TestKind.READ_ONLY, TestKind.DESTRUCTIVE)


@dataclass(frozen=True, slots=True)
class Observation:
    """One run's comparison: ``baseline`` state versus the state under test."""

    baseline: str  # "once" (idempotence) or "control" (read-only, destructive)
    deltas: tuple[SnapshotDelta, ...]

    def changed(self, category: StateCategory) -> bool:
        return any(d.changed(category) for d in self.deltas)

    def removed(self, category: StateCategory) -> bool:
        return any(d.removed_any(category) for d in self.deltas)

    def to_json(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "changed": {c.value: self.changed(c) for c in StateCategory},
            "removed": {c.value: self.removed(c) for c in StateCategory},
            "deltas": [d.to_json() for d in self.deltas],
        }


def behavior_in(test: TestKind, obs: Observation) -> Behavior:
    if test is TestKind.READ_ONLY:
        return Behavior.FAILS if any(obs.changed(c) for c in StateCategory) else Behavior.HOLDS
    if test is TestKind.DESTRUCTIVE:
        if obs.removed(StateCategory.BUSINESS) or obs.removed(StateCategory.APPEND_ONLY):
            return Behavior.FAILS
        return Behavior.UNCLASSIFIED_ONLY if obs.removed(StateCategory.UNCLASSIFIED) else Behavior.HOLDS
    # idempotence: once vs twice over business state
    if obs.changed(StateCategory.BUSINESS):
        return Behavior.FAILS
    return Behavior.UNCLASSIFIED_ONLY if obs.changed(StateCategory.UNCLASSIFIED) else Behavior.HOLDS


def combined_behavior(test: TestKind, observations: Sequence[Observation], stable: bool) -> Behavior:
    if not observations:
        return Behavior.UNOBSERVED
    per_run = {behavior_in(test, o) for o in observations}
    if not stable or len(per_run) > 1:
        return Behavior.UNSTABLE
    return per_run.pop()


@dataclass(frozen=True, slots=True)
class Verdict:
    tool: str
    test: TestKind
    outcome: Outcome
    reason: InconclusiveReason | None
    detail: str | None
    hint: Annotation
    contract: DeclaredContract | None
    behavior: Behavior
    observations: tuple[Observation, ...]
    coverage: FixtureCoverage
    runs: int

    @property
    def declared(self) -> Hint:
        return Hint.ABSENT if self.contract is None else self.contract.hint(self.hint)

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "test": self.test.value,
            "outcome": self.outcome.value,
            "reason": None if self.reason is None else self.reason.value,
            "detail": self.detail,
            "hint": self.hint.wire_name,
            "declared": self.declared.value,
            "falsifiable_value": FALSIFIABLE[self.hint].value,
            "contract": None if self.contract is None else self.contract.to_json(),
            "observed": {
                "property": PROPERTY[self.test],
                "behavior": self.behavior.value,
                "runs": [o.to_json() for o in self.observations],
            },
            "coverage": self.coverage.to_json(),
            "run_count": self.runs,
        }


def derive(run: FixtureRun) -> list[Verdict]:
    return [_verdict(run, ev, test) for ev in run.tools for test in IMPLEMENTED]


def _verdict(run: FixtureRun, ev: ToolEvidence, test: TestKind) -> Verdict:
    hint = HINT_FOR[test]
    control = test in CONTROL_BASED
    observations = tuple(ev.effect if control else ev.idempotence)
    stable = ev.once_stable and (run.control_stable if control else ev.twice_stable)
    behavior = combined_behavior(test, observations, stable)

    def verdict(outcome: Outcome, reason: InconclusiveReason | None = None, detail: str | None = None):
        return Verdict(
            tool=ev.tool,
            test=test,
            outcome=outcome,
            reason=reason,
            detail=detail,
            hint=hint,
            contract=ev.contract,
            behavior=behavior,
            observations=observations,
            coverage=run.coverage,
            runs=run.runs,
        )

    inconclusive = Outcome.INCONCLUSIVE
    if ev.contract is None:
        return verdict(inconclusive, InconclusiveReason.SETUP_FAILED, ev.contract_error)

    # 1. The declaration alone decides these, whatever happened at run time.
    declared = ev.contract.hint(hint)
    if declared is Hint.ABSENT:
        if hint in ev.contract.malformed_hints:
            return verdict(inconclusive, InconclusiveReason.MALFORMED_DECLARATION)
        return verdict(inconclusive, InconclusiveReason.NO_DECLARATION)
    if declared is not FALSIFIABLE[hint]:
        return verdict(Outcome.NOT_APPLICABLE, detail=f"{hint.wire_name}: {declared.value} makes no falsifiable claim")

    # 2. Could the harness trust its own observations?
    if run.reset.status is ResetStatus.SETUP_FAILED:
        return verdict(inconclusive, InconclusiveReason.SETUP_FAILED, run.reset.detail)
    if run.reset.status is ResetStatus.UNVERIFIED:
        return verdict(inconclusive, InconclusiveReason.RESET_UNVERIFIED, run.reset.detail)
    if control and run.control_error is not None:
        return verdict(inconclusive, InconclusiveReason.SETUP_FAILED, f"control session: {run.control_error}")
    if ev.setup_error is not None:
        return verdict(inconclusive, InconclusiveReason.SETUP_FAILED, ev.setup_error)
    if ev.call_error is not None:
        return verdict(inconclusive, InconclusiveReason.CALL_ERROR, ev.call_error)
    if not ev.contract_stable:
        return verdict(inconclusive, InconclusiveReason.NONDETERMINISTIC, "declared contract differs across sessions")
    if control and not run.control_stable:
        return verdict(
            inconclusive,
            InconclusiveReason.NONDETERMINISTIC,
            "control sessions disagree after normalization",
        )
    if run.runs < MIN_RUNS or len(observations) < run.runs:
        return verdict(inconclusive, InconclusiveReason.NONDETERMINISTIC, f"fewer than {MIN_RUNS} complete runs")

    # 3. What was observed.
    if behavior is Behavior.UNSTABLE:
        return verdict(inconclusive, InconclusiveReason.NONDETERMINISTIC, "runs disagree after normalization")
    if behavior is Behavior.FAILS:
        return verdict(Outcome.VIOLATION)
    if behavior is Behavior.UNCLASSIFIED_ONLY:
        return verdict(inconclusive, InconclusiveReason.UNCLASSIFIED_STATE)
    if not run.coverage.complete:
        return verdict(inconclusive, InconclusiveReason.PARTIAL_COVERAGE)
    return verdict(Outcome.PASS)


# --- Declaration coherence, separate from behavior ---------------------------


class Incoherence(enum.Enum):
    READ_ONLY_AND_DESTRUCTIVE = "read_only_and_destructive"
    READ_ONLY_NOT_IDEMPOTENT = "read_only_not_idempotent"


def coherence(contract: DeclaredContract) -> tuple[Incoherence, ...]:
    """Declaration sets that contradict themselves. Recorded, never corrected."""
    issues = []
    if contract.read_only is Hint.TRUE:
        if contract.destructive is Hint.TRUE:
            issues.append(Incoherence.READ_ONLY_AND_DESTRUCTIVE)
        if contract.idempotent is Hint.FALSE:
            issues.append(Incoherence.READ_ONLY_NOT_IDEMPOTENT)
    return tuple(issues)

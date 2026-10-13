"""Verdicts: what the evidence says about each (tool, test).

Pure interpretation. The runner collects evidence (sessions, snapshots,
deltas); this module decides, and attaches the evidence to every verdict.

Only three declared values make a claim that behavior can contradict:
``readOnlyHint: true``, ``destructiveHint: false`` and ``idempotentHint:
true``. The opposite values are permissive and get NOT_APPLICABLE. An explicit
``readOnlyHint: true`` also supersedes the destructive and idempotence tests:
the read-only claim is the stronger one and is tested instead.

Every verdict has one primary reason and may have secondary reasons: every
other problem found, so that e.g. a CALL_ERROR stays visible under
NO_DECLARATION.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Union

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
    NOT_APPLICABLE = "NOT_APPLICABLE"  # nothing falsifiable to test


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


class NotApplicableReason(enum.Enum):
    PERMISSIVE_DECLARATION = "PERMISSIVE_DECLARATION"  # e.g. readOnlyHint:false, "may write"
    SUPERSEDED_BY_READ_ONLY = "SUPERSEDED_BY_READ_ONLY"  # explicit readOnlyHint:true


Reason = Union[InconclusiveReason, NotApplicableReason]


def parse_reason(text: str) -> Reason:
    for enum_type in (InconclusiveReason, NotApplicableReason):
        try:
            return enum_type(text)
        except ValueError:
            pass
    raise ValueError(f"unknown reason {text!r}")


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


@dataclass(frozen=True, slots=True)
class Instability:
    """Two sessions that should have ended in the same state, but did not."""

    compared: tuple[str, str]  # session labels
    columns: tuple[str, ...]  # "surface/region.column" that differ after normalization
    rows_added: int
    rows_removed: int

    def to_json(self) -> dict[str, Any]:
        return {
            "compared": list(self.compared),
            "columns": list(self.columns),
            "rows_added": self.rows_added,
            "rows_removed": self.rows_removed,
        }


def instability_between(labels: tuple[str, str], deltas: Sequence[SnapshotDelta]) -> Instability | None:
    columns, added, removed = [], 0, 0
    for delta in deltas:
        for region in delta.regions:
            columns += [f"{delta.surface}/{region.region}.{c}" for c in region.differing_columns()]
            added += len(region.added)
            removed += len(region.removed)
    if not (columns or added or removed):
        return None
    return Instability(labels, tuple(columns), added, removed)


def behavior_in(test: TestKind, obs: Observation) -> Behavior:
    if test is TestKind.READ_ONLY:
        return Behavior.FAILS if affected(test, obs) else Behavior.HOLDS
    if test is TestKind.DESTRUCTIVE:
        hit = affected(test, obs)
        if StateCategory.BUSINESS in hit or StateCategory.APPEND_ONLY in hit:
            return Behavior.FAILS
        return Behavior.UNCLASSIFIED_ONLY if hit else Behavior.HOLDS
    # idempotence: once vs twice over business state
    if obs.changed(StateCategory.BUSINESS):
        return Behavior.FAILS
    return Behavior.UNCLASSIFIED_ONLY if obs.changed(StateCategory.UNCLASSIFIED) else Behavior.HOLDS


def affected(test: TestKind, obs: Observation) -> tuple[StateCategory, ...]:
    """Categories that bear on the property: changed (read-only, idempotence) or destroyed."""
    if test is TestKind.DESTRUCTIVE:
        return tuple(c for c in StateCategory if obs.removed(c))
    if test is TestKind.READ_ONLY:
        return tuple(c for c in StateCategory if obs.changed(c))
    return tuple(c for c in (StateCategory.BUSINESS, StateCategory.UNCLASSIFIED) if obs.changed(c))


def combined_behavior(test: TestKind, observations: Sequence[Observation], stable: bool) -> Behavior:
    if not observations:
        return Behavior.UNOBSERVED
    per_run = {behavior_in(test, o) for o in observations}
    if not stable or len(per_run) > 1:
        return Behavior.UNSTABLE
    return per_run.pop()


@dataclass(frozen=True, slots=True)
class Issue:
    reason: Reason
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"reason": self.reason.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Verdict:
    tool: str
    test: TestKind
    outcome: Outcome
    issues: tuple[Issue, ...]  # primary first; PASS and VIOLATION may carry secondaries too
    hint: Annotation
    contract: DeclaredContract | None
    behavior: Behavior
    observations: tuple[Observation, ...]
    instability: tuple[Instability, ...]
    errored_with_side_effects: bool
    coverage: FixtureCoverage
    runs: int

    @property
    def reason(self) -> Reason | None:
        """The primary reason (always None for PASS and VIOLATION)."""
        if self.outcome in (Outcome.PASS, Outcome.VIOLATION):
            return None
        return self.issues[0].reason

    @property
    def secondary_reasons(self) -> tuple[Reason, ...]:
        rest = self.issues if self.reason is None else self.issues[1:]
        out: list[Reason] = []
        for issue in rest:
            if issue.reason is not self.reason and issue.reason not in out:
                out.append(issue.reason)
        return tuple(out)

    @property
    def declared(self) -> Hint:
        return Hint.ABSENT if self.contract is None else self.contract.hint(self.hint)

    @property
    def affected_categories(self) -> tuple[StateCategory, ...]:
        seen = {c for o in self.observations for c in affected(self.test, o)}
        return tuple(c for c in StateCategory if c in seen)

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "test": self.test.value,
            "outcome": self.outcome.value,
            "reason": None if self.reason is None else self.reason.value,
            "secondary_reasons": [r.value for r in self.secondary_reasons],
            "issues": [i.to_json() for i in self.issues],
            "hint": self.hint.wire_name,
            "declared": self.declared.value,
            "falsifiable_value": FALSIFIABLE[self.hint].value,
            "contract": None if self.contract is None else self.contract.to_json(),
            "observed": {
                "property": PROPERTY[self.test],
                "behavior": self.behavior.value,
                # For the destructive test this is the destruction sub-category:
                # business and append-only destruction are reported separately.
                "affected_categories": [c.value for c in self.affected_categories],
                "errored_with_side_effects": self.errored_with_side_effects,
                "nondeterminism": [i.to_json() for i in self.instability],
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
    instability = tuple(ev.once_instability) + tuple(
        run.control_instability if control else ev.twice_instability
    )
    behavior = combined_behavior(test, observations, stable=not instability)
    issues = _issues(run, ev, control, observations, instability, behavior)
    contract = ev.contract

    def verdict(outcome: Outcome, primary: Issue | None = None, rest: Sequence[Issue] = ()) -> Verdict:
        return Verdict(
            tool=ev.tool,
            test=test,
            outcome=outcome,
            issues=((primary,) if primary else ()) + tuple(rest),
            hint=hint,
            contract=contract,
            behavior=behavior,
            observations=observations,
            instability=instability,
            errored_with_side_effects=ev.errored_with_side_effects,
            coverage=run.coverage,
            runs=run.runs,
        )

    inconclusive, not_applicable = Outcome.INCONCLUSIVE, Outcome.NOT_APPLICABLE
    if contract is None:
        return verdict(inconclusive, Issue(InconclusiveReason.SETUP_FAILED, ev.contract_error), issues)

    # 1. The declaration alone decides these, whatever happened at run time.
    declared = contract.hint(hint)
    if test is not TestKind.READ_ONLY and contract.read_only is Hint.TRUE:
        why = "explicit readOnlyHint:true; the read-only test covers this tool"
        return verdict(not_applicable, Issue(NotApplicableReason.SUPERSEDED_BY_READ_ONLY, why), issues)
    if declared is Hint.ABSENT:
        reason = (
            InconclusiveReason.MALFORMED_DECLARATION
            if hint in contract.malformed_hints
            else InconclusiveReason.NO_DECLARATION
        )
        return verdict(inconclusive, Issue(reason), issues)
    if declared is not FALSIFIABLE[hint]:
        why = f"{hint.wire_name}: {declared.value} makes no falsifiable claim"
        return verdict(not_applicable, Issue(NotApplicableReason.PERMISSIVE_DECLARATION, why), issues)

    # 2. Problems with the run or the observation block a judgment, except
    # incomplete coverage, which blocks PASS but not VIOLATION.
    partial = [i for i in issues if i.reason is InconclusiveReason.PARTIAL_COVERAGE]
    blocking = [i for i in issues if i.reason is not InconclusiveReason.PARTIAL_COVERAGE]
    if blocking:
        return verdict(inconclusive, blocking[0], blocking[1:] + partial)
    if behavior is Behavior.FAILS:
        return verdict(Outcome.VIOLATION, rest=partial)
    if partial:
        return verdict(inconclusive, partial[0])
    return verdict(Outcome.PASS)


def _issues(
    run: FixtureRun,
    ev: ToolEvidence,
    control: bool,
    observations: Sequence[Observation],
    instability: Sequence[Instability],
    behavior: Behavior,
) -> list[Issue]:
    """Every problem with this (tool, test), in precedence order."""
    issues: list[Issue] = []
    if run.reset.status is ResetStatus.SETUP_FAILED:
        issues.append(Issue(InconclusiveReason.SETUP_FAILED, run.reset.detail))
    elif run.reset.status is ResetStatus.UNVERIFIED:
        issues.append(Issue(InconclusiveReason.RESET_UNVERIFIED, run.reset.detail))
    if control and run.control_error is not None:
        issues.append(Issue(InconclusiveReason.SETUP_FAILED, f"control session: {run.control_error}"))
    if ev.setup_error is not None:
        issues.append(Issue(InconclusiveReason.SETUP_FAILED, ev.setup_error))
    if ev.call_error is not None:
        issues.append(Issue(InconclusiveReason.CALL_ERROR, ev.call_error))
    if ev.contract_instability:
        issues.append(
            Issue(
                InconclusiveReason.NONDETERMINISTIC,
                "declared contract differs across sessions: " + ", ".join(ev.contract_instability),
            )
        )
    if instability:
        columns = sorted({c for i in instability for c in i.columns})
        issues.append(
            Issue(
                InconclusiveReason.NONDETERMINISTIC,
                "states differ across sessions that should match; columns: " + (", ".join(columns) or "(row counts only)"),
            )
        )
    if behavior is Behavior.UNSTABLE and not instability:
        issues.append(Issue(InconclusiveReason.NONDETERMINISTIC, "runs disagree"))
    if not issues and len(observations) < MIN_RUNS:
        issues.append(Issue(InconclusiveReason.NONDETERMINISTIC, f"fewer than {MIN_RUNS} complete runs"))
    if behavior is Behavior.UNCLASSIFIED_ONLY:
        issues.append(Issue(InconclusiveReason.UNCLASSIFIED_STATE))
    if not run.coverage.complete:
        issues.append(Issue(InconclusiveReason.PARTIAL_COVERAGE, ", ".join(run.coverage.unobserved) or None))
    return issues


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

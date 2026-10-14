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

The concurrent-idempotence test is one-sided: a single attempt whose final
state differs from the single-call state is a conclusive VIOLATION, while
PASS only means no race was observed in attempts whose overlap was verified.
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
CONCURRENT_ATTEMPTS = 20  # per mode


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
    # Concurrent test only: no race seen, and no attempt had in_flight_overlap.
    NO_OVERLAP_OBSERVED = "NO_OVERLAP_OBSERVED"
    # Concurrent test only, informational: two_process attempts were not
    # comparable because server startup or shutdown writes state.
    TWO_PROCESS_SKIPPED_STARTUP_WRITES = "TWO_PROCESS_SKIPPED_STARTUP_WRITES"


class NotApplicableReason(enum.Enum):
    PERMISSIVE_DECLARATION = "PERMISSIVE_DECLARATION"  # e.g. readOnlyHint:false, "may write"
    SUPERSEDED_BY_READ_ONLY = "SUPERSEDED_BY_READ_ONLY"  # explicit readOnlyHint:true


Reason = InconclusiveReason | NotApplicableReason


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


class ErrorKind(enum.Enum):
    TOOL_ERROR = "tool_error"  # result with isError: true
    PROTOCOL_ERROR = "protocol_error"  # JSON-RPC error response


class ConcurrencyMode(enum.Enum):
    SINGLE_CONNECTION = "single_connection"  # two concurrent requests on one connection
    TWO_PROCESS = "two_process"  # two server instances, same backing store, one call each


class Overlap(enum.Enum):
    # two_process only: both calls in flight together for longer than both
    # instances' ping round trips combined. A heuristic: it assumes transport
    # delay during the calls is no worse than during the pings.
    IN_FLIGHT = "in_flight_overlap"
    UNVERIFIED = "unverified"  # in flight together, but the server may have serialized them
    NONE = "none"  # not even in flight together


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
IMPLEMENTED = (
    TestKind.SEQUENTIAL_IDEMPOTENCE,
    TestKind.CONCURRENT_IDEMPOTENCE,
    TestKind.READ_ONLY,
    TestKind.DESTRUCTIVE,
)


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


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    """A call that completed with an error (not a dead connection)."""

    session: str
    position: int  # 1-based call position within the session
    kind: ErrorKind
    state_changed: bool  # session end state differs from the control state

    def to_json(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "position": self.position,
            "kind": self.kind.value,
            "state_changed": self.state_changed,
        }


@dataclass(frozen=True, slots=True)
class Attempt:
    """One concurrent attempt, compared against the single-call state."""

    label: str
    mode: ConcurrencyMode
    overlap: Overlap
    wire_overlap_ms: float | None  # how long both calls were in flight together
    rtt_ms: tuple[float, ...]  # ping round trip per connection, just before the calls
    observation: Observation

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "mode": self.mode.value,
            "overlap": self.overlap.value,
            "wire_overlap_ms": self.wire_overlap_ms,
            "rtt_ms": list(self.rtt_ms),
            "diverged": behavior_in(TestKind.CONCURRENT_IDEMPOTENCE, self.observation) is Behavior.FAILS,
            **self.observation.to_json(),
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
    if test is TestKind.CONCURRENT_IDEMPOTENCE:
        # Races are intermittent: one diverging attempt is enough, and
        # attempts disagreeing is expected rather than unstable.
        if not stable:
            return Behavior.UNSTABLE
        for b in (Behavior.FAILS, Behavior.UNCLASSIFIED_ONLY):
            if b in per_run:
                return b
        return Behavior.HOLDS
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
    errors: tuple[ErrorRecord, ...]
    coverage: FixtureCoverage
    runs: int
    attempts: tuple[Attempt, ...] = ()  # concurrent test only
    concurrency_note: str | None = None  # why attempts were not run, or a mode was excluded

    @property
    def produced_by(self) -> tuple[ConcurrencyMode, ...]:
        """Concurrent test: the modes whose attempts decided the verdict."""
        if self.outcome is Outcome.VIOLATION:
            hit = {a.mode for a in self.attempts if behavior_in(self.test, a.observation) is Behavior.FAILS}
        elif self.outcome is Outcome.PASS:
            hit = {a.mode for a in self.attempts if a.overlap is Overlap.IN_FLIGHT}
        else:
            hit = set()
        return tuple(m for m in ConcurrencyMode if m in hit)

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
                "call_errors": [e.to_json() for e in self.errors],
                "nondeterminism": [i.to_json() for i in self.instability],
                "runs": [o.to_json() for o in self.observations] if not self.attempts else [],
            },
            "coverage": self.coverage.to_json(),
            "run_count": self.runs,
            **({"concurrency": self._concurrency_json()} if self.test is TestKind.CONCURRENT_IDEMPOTENCE else {}),
        }

    def _concurrency_json(self) -> dict[str, Any]:
        modes: dict[str, Any] = {}
        for mode in ConcurrencyMode:
            mine = [a for a in self.attempts if a.mode is mode]
            modes[mode.value] = {
                "attempts": len(mine),
                "overlap": {o.value: sum(a.overlap is o for a in mine) for o in Overlap},
                "diverged": sum(behavior_in(self.test, a.observation) is Behavior.FAILS for a in mine),
            }
        return {
            "one_sided": "VIOLATION is conclusive; PASS means no race observed in in_flight_overlap attempts",
            "produced_by": [m.value for m in self.produced_by],
            "note": self.concurrency_note,
            "modes": modes,
            "attempts": [a.to_json() for a in self.attempts],
        }


def derive(run: FixtureRun) -> list[Verdict]:
    return [_verdict(run, ev, test) for ev in run.tools for test in IMPLEMENTED]


def _verdict(run: FixtureRun, ev: ToolEvidence, test: TestKind) -> Verdict:
    hint = HINT_FOR[test]
    control = test in CONTROL_BASED
    concurrent = test is TestKind.CONCURRENT_IDEMPOTENCE
    attempts: tuple[Attempt, ...] = tuple(ev.concurrent) if concurrent else ()
    if concurrent:
        observations = tuple(a.observation for a in attempts)
        instability = tuple(ev.once_instability)  # the single-call baseline must be stable
        errors = tuple(ev.concurrent_errors)
    else:
        observations = tuple(ev.effect if control else ev.idempotence)
        instability = tuple(ev.once_instability) + tuple(run.control_instability if control else ev.twice_instability)
        errors = tuple(ev.errors)
    behavior = combined_behavior(test, observations, stable=not instability)
    issues = _issues(run, ev, test, observations, instability, behavior, attempts)
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
            errored_with_side_effects=ev.errored_with_side_effects and not concurrent,
            errors=errors,
            coverage=run.coverage,
            runs=len(attempts) if concurrent else run.runs,
            attempts=attempts,
            concurrency_note=ev.concurrent_note if concurrent else None,
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
    # incomplete coverage, which blocks PASS but not VIOLATION, and a skipped
    # mode, which is informational.
    partial = [i for i in issues if i.reason in _NON_BLOCKING]
    blocking = [i for i in issues if i.reason not in _NON_BLOCKING]
    if blocking:
        return verdict(inconclusive, blocking[0], blocking[1:] + partial)
    if behavior is Behavior.FAILS:
        return verdict(Outcome.VIOLATION, rest=partial)
    coverage = [i for i in partial if i.reason is InconclusiveReason.PARTIAL_COVERAGE]
    if coverage:
        return verdict(inconclusive, coverage[0], [i for i in partial if i is not coverage[0]])
    return verdict(Outcome.PASS, rest=partial)


_NON_BLOCKING = frozenset({InconclusiveReason.PARTIAL_COVERAGE, InconclusiveReason.TWO_PROCESS_SKIPPED_STARTUP_WRITES})


def _issues(
    run: FixtureRun,
    ev: ToolEvidence,
    test: TestKind,
    observations: Sequence[Observation],
    instability: Sequence[Instability],
    behavior: Behavior,
    attempts: Sequence[Attempt],
) -> list[Issue]:
    """Every problem with this (tool, test), in precedence order."""
    control = test in CONTROL_BASED
    concurrent = test is TestKind.CONCURRENT_IDEMPOTENCE
    issues: list[Issue] = []
    if run.reset.status is ResetStatus.SETUP_FAILED:
        issues.append(Issue(InconclusiveReason.SETUP_FAILED, run.reset.detail))
    elif run.reset.status is ResetStatus.UNVERIFIED:
        issues.append(Issue(InconclusiveReason.RESET_UNVERIFIED, run.reset.detail))
    if control and run.control_error is not None:
        issues.append(Issue(InconclusiveReason.SETUP_FAILED, f"control session: {run.control_error}"))
    setup_error = ev.concurrent_setup_error if concurrent else ev.setup_error
    if setup_error is not None:
        issues.append(Issue(InconclusiveReason.SETUP_FAILED, setup_error))
    call_error = ev.concurrent_call_error if concurrent else ev.call_error
    if call_error is not None:
        issues.append(Issue(InconclusiveReason.CALL_ERROR, call_error))
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
                "states differ across sessions that should match; columns: "
                + (", ".join(columns) or "(row counts only)"),
            )
        )
    if behavior is Behavior.UNSTABLE and not instability:
        issues.append(Issue(InconclusiveReason.NONDETERMINISTIC, "runs disagree"))
    if not concurrent and not issues and len(observations) < MIN_RUNS:
        issues.append(Issue(InconclusiveReason.NONDETERMINISTIC, f"fewer than {MIN_RUNS} complete runs"))
    if behavior is Behavior.UNCLASSIFIED_ONLY:
        issues.append(Issue(InconclusiveReason.UNCLASSIFIED_STATE))
    no_race_seen = behavior in (Behavior.HOLDS, Behavior.UNOBSERVED)
    if concurrent and ev.concurrent_ran and no_race_seen and not any(a.overlap is Overlap.IN_FLIGHT for a in attempts):
        detail = f"0 of {len(attempts)} attempts had in_flight_overlap"
        issues.append(Issue(InconclusiveReason.NO_OVERLAP_OBSERVED, detail))
    if concurrent and ev.two_process_skipped:
        issues.append(Issue(InconclusiveReason.TWO_PROCESS_SKIPPED_STARTUP_WRITES, ev.concurrent_note))
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

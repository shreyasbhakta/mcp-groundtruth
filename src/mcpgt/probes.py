"""Probes: other tools' outputs as a second observer of state.

State adapters see files and databases. A server can also keep state in
memory, or somewhere the fixture does not observe, and no adapter sees that.
A probe is a tool whose output reflects state, such as a list or read tool.
If calling tool X changes what a probe returns, X changed something, whether
or not any observed file changed.

Every comparison is controlled. Each session starts from a clean reset in a
fresh server process, and the probe alone is measured first, in several runs:
a probe whose output differs across those runs cannot be used. A tool is
judged only on a change reproduced in every run.

- ``readOnlyHint: true``: compare (X, P) with (P). A difference falsifies it.
- ``idempotentHint: true``: compare (X, X, P) with (X, P). A difference falsifies it.

Outputs are compared as the JSON-RPC result (or error) object without the
request id, after removing every match of the fixture's ignore patterns from
its strings.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mcpgt.contracts import Annotation, DeclaredContract, Hint
from mcpgt.snapshot import StateCategory
from mcpgt.verdict import (
    FALSIFIABLE,
    Behavior,
    InconclusiveReason,
    Issue,
    NotApplicableReason,
    Outcome,
    Reason,
    ResetStatus,
)

if TYPE_CHECKING:
    from mcpgt.runner import FixtureRun, Session, ToolEvidence

EXCERPT = 300  # characters of a probe output kept in the report
CONTEXT = 60  # characters kept before the first difference


class ProbeTest(enum.Enum):
    READ_ONLY = "probe_read_only"  # (tool, probe) vs (probe)
    IDEMPOTENCE = "probe_idempotence"  # (tool, tool, probe) vs (tool, probe)


COMPARISON = {
    ProbeTest.READ_ONLY: "tool then probe, against the probe alone",
    ProbeTest.IDEMPOTENCE: "tool twice then probe, against tool once then probe",
}


def probe_test(contract: DeclaredContract | None) -> ProbeTest | None:
    """The claim probes can falsify for this tool, or None if it makes none."""
    if contract is None:
        return None
    if contract.read_only is Hint.TRUE:
        return ProbeTest.READ_ONLY
    if contract.idempotent is FALSIFIABLE[Annotation.IDEMPOTENT]:
        return ProbeTest.IDEMPOTENCE
    return None


def canonical(message: Mapping[str, Any], ignore: Sequence[str]) -> str:
    """A response's result (or error) as canonical JSON, ignore patterns removed."""
    body = message.get("result", message.get("error"))
    return json.dumps(_scrub(body, [re.compile(p) for p in ignore]), sort_keys=True, ensure_ascii=False)


def _scrub(value: Any, patterns: Sequence[re.Pattern[str]]) -> Any:
    if isinstance(value, str):
        for pattern in patterns:
            value = pattern.sub("", value)
        return value
    if isinstance(value, Mapping):
        return {k: _scrub(v, patterns) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, patterns) for v in value]
    return value


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What one probe showed about one tool."""

    probe: str
    behavior: Behavior  # HOLDS: probe output unchanged in every run; FAILS: changed in every run
    issue: Issue | None  # why this probe could not decide, if it could not
    reference: str | None  # canonical output the treatment is compared against
    outputs: tuple[tuple[str, str], ...] = ()  # (session label, canonical output) per treatment run

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(label for label, out in self.outputs if out != self.reference)

    def to_json(self) -> dict[str, Any]:
        first = next((out for _, out in self.outputs if out != self.reference), None)
        # Both excerpts start just before the first difference, so a change deep
        # in a large output is still visible.
        start = 0
        if first is not None and self.reference is not None:
            pairs = zip(self.reference, first, strict=False)
            same = next((i for i, (a, b) in enumerate(pairs) if a != b), min(len(self.reference), len(first)))
            start = max(0, same - CONTEXT)
        return {
            "probe": self.probe,
            "behavior": self.behavior.value,
            "issue": None if self.issue is None else self.issue.to_json(),
            "changed_runs": len(self.changed),
            "runs": len(self.outputs),
            "reference_sha256": None if self.reference is None else _sha(self.reference),
            "excerpt_offset": start,
            "reference_excerpt": None if self.reference is None else self.reference[start : start + EXCERPT],
            "changed_excerpt": None if first is None else first[start : start + EXCERPT],
            "sessions": [
                {"label": label, "output_sha256": _sha(out), "changed": out != self.reference}
                for label, out in self.outputs
            ],
        }


@dataclass(frozen=True, slots=True)
class ProbeVerdict:
    tool: str
    test: ProbeTest | None
    hint: Annotation
    declared: Hint
    outcome: Outcome
    issues: tuple[Issue, ...]  # primary first for INCONCLUSIVE and NOT_APPLICABLE
    results: tuple[ProbeResult, ...] = ()
    # VIOLATION only: True if the state adapters saw no change in the same
    # comparison, so the change is in state the fixture does not observe.
    hidden_state: bool | None = None

    @property
    def reason(self) -> Reason | None:
        if self.outcome in (Outcome.PASS, Outcome.VIOLATION):
            return None
        return self.issues[0].reason

    @property
    def detail(self) -> str:
        if self.outcome is Outcome.VIOLATION:
            hit = [r for r in self.results if r.behavior is Behavior.FAILS]
            where = ", ".join(f"{r.probe} ({len(r.changed)}/{len(r.outputs)} runs)" for r in hit)
            text = f"output changed: {where}"
            if self.hidden_state:
                text += "; no observed state changed, so the change is in state the fixture does not observe"
            return text
        if self.outcome is Outcome.PASS:
            return f"no probe output changed ({', '.join(r.probe for r in self.results)})"
        issue = self.issues[0]
        return str(issue.reason.value) + (f": {issue.detail}" if issue.detail else "")

    def to_json(self) -> dict[str, Any]:
        rest = self.issues if self.reason is None else self.issues[1:]
        return {
            "tool": self.tool,
            "test": None if self.test is None else self.test.value,
            "comparison": None if self.test is None else COMPARISON[self.test],
            "hint": self.hint.wire_name,
            "declared": self.declared.value,
            "outcome": self.outcome.value,
            "reason": None if self.reason is None else self.reason.value,
            "secondary_reasons": list(dict.fromkeys(i.reason.value for i in rest if i.reason is not self.reason)),
            "issues": [i.to_json() for i in self.issues],
            "detail": self.detail,
            "hidden_state": self.hidden_state,
            "probes": [r.to_json() for r in self.results],
        }


def derive_probes(run: FixtureRun) -> list[ProbeVerdict]:
    """One probe verdict per tool, or none if the fixture has no [probes]."""
    if run.fixture.probes is None:
        return []
    return [judge(run, ev) for ev in run.tools]


def judge(run: FixtureRun, ev: ToolEvidence) -> ProbeVerdict:
    contract = ev.contract
    test = probe_test(contract)
    hint = Annotation.READ_ONLY if test is ProbeTest.READ_ONLY else Annotation.IDEMPOTENT
    declared = Hint.ABSENT if contract is None else contract.hint(hint)

    def verdict(outcome: Outcome, issues: Sequence[Issue] = (), **kw: Any) -> ProbeVerdict:
        return ProbeVerdict(ev.tool, test, hint, declared, outcome, tuple(issues), **kw)

    # 1. The declaration alone decides these.
    if contract is None:
        return verdict(Outcome.INCONCLUSIVE, [Issue(InconclusiveReason.SETUP_FAILED, ev.contract_error)])
    if declared is Hint.ABSENT:
        malformed = hint in contract.malformed_hints
        reason = InconclusiveReason.MALFORMED_DECLARATION if malformed else InconclusiveReason.NO_DECLARATION
        return verdict(Outcome.INCONCLUSIVE, [Issue(reason, hint.wire_name)])
    if test is None:
        why = f"readOnlyHint: {contract.read_only.value}, idempotentHint: {declared.value}; nothing a probe can falsify"
        return verdict(Outcome.NOT_APPLICABLE, [Issue(NotApplicableReason.PERMISSIVE_DECLARATION, why)])

    # 2. Problems with the whole run block a judgment.
    blocking = []
    if run.reset.status is ResetStatus.SETUP_FAILED:
        blocking.append(Issue(InconclusiveReason.SETUP_FAILED, run.reset.detail))
    elif run.reset.status is ResetStatus.UNVERIFIED:
        blocking.append(Issue(InconclusiveReason.RESET_UNVERIFIED, run.reset.detail))
    if ev.contract_instability:
        detail = "declared contract differs across sessions: " + ", ".join(ev.contract_instability)
        blocking.append(Issue(InconclusiveReason.NONDETERMINISTIC, detail))
    if blocking:
        return verdict(Outcome.INCONCLUSIVE, blocking)

    # 3. Each probe decides on its own; one reproduced change is enough.
    ignore = run.fixture.probes.ignore if run.fixture.probes else ()
    baselines = {p.probe: p.sessions for p in run.probe_baselines}
    once = {p.probe: p.sessions for p in ev.probe_once}
    twice = {p.probe: p.sessions for p in ev.probe_twice}
    results = []
    for probe, alone in baselines.items():
        if test is ProbeTest.READ_ONLY:
            results.append(_compare(probe, alone, alone, once.get(probe, ()), ignore))
        else:
            results.append(_compare(probe, alone, once.get(probe, ()), twice.get(probe, ()), ignore))
    issues = [r.issue for r in results if r.issue is not None]
    if any(r.behavior is Behavior.FAILS for r in results):
        return verdict(Outcome.VIOLATION, issues, results=tuple(results), hidden_state=_hidden(ev, test))
    if issues:
        return verdict(Outcome.INCONCLUSIVE, issues, results=tuple(results))
    return verdict(Outcome.PASS, results=tuple(results))


def _compare(
    probe: str,
    alone: Sequence[Session],
    reference: Sequence[Session],
    treatment: Sequence[Session],
    ignore: Sequence[str],
) -> ProbeResult:
    """Compare the probe's output at the end of each treatment session with the reference."""

    def unusable(reason: InconclusiveReason, detail: str) -> ProbeResult:
        return ProbeResult(probe, Behavior.UNOBSERVED, Issue(reason, f"probe {probe}: {detail}"), None)

    if not treatment:
        return unusable(InconclusiveReason.SETUP_FAILED, "no sessions ran")
    for session in [*alone, *reference, *treatment]:
        if session.setup_error is not None:
            return unusable(InconclusiveReason.SETUP_FAILED, f"{session.label}: {session.setup_error}")
        if session.transport_error is not None or len(session.responses) != len(session.calls):
            return unusable(InconclusiveReason.CALL_ERROR, f"{session.label}: {session.transport_error}")
    if any(len(s.calls) in {p for p, _ in s.call_errors} for s in alone):
        return unusable(InconclusiveReason.SETUP_FAILED, "returns an error when called alone")
    for sessions, what in ((alone, "alone"), (reference, "in the reference sessions")):
        distinct = {canonical(s.responses[-1].message, ignore) for s in sessions}
        if len(distinct) > 1:
            return unusable(InconclusiveReason.NONDETERMINISTIC, f"output differs across runs {what}")
    ref = canonical(reference[0].responses[-1].message, ignore)
    outputs = tuple((s.label, canonical(s.responses[-1].message, ignore)) for s in treatment)
    changed = sum(out != ref for _, out in outputs)
    if changed == len(outputs):
        return ProbeResult(probe, Behavior.FAILS, None, ref, outputs)
    if changed:
        issue = Issue(
            InconclusiveReason.NONDETERMINISTIC, f"probe {probe}: changed in {changed} of {len(outputs)} runs"
        )
        return ProbeResult(probe, Behavior.UNSTABLE, issue, ref, outputs)
    # The tool erroring and changing nothing tells us nothing about the claim.
    errored = [s.label for s in treatment if any(p < len(s.calls) for p, _ in s.call_errors)]
    if errored:
        issue = Issue(InconclusiveReason.CALL_ERROR, f"tool returned an error with no probe change: {errored[0]}")
        return ProbeResult(probe, Behavior.HOLDS, issue, ref, outputs)
    return ProbeResult(probe, Behavior.HOLDS, None, ref, outputs)


def _hidden(ev: ToolEvidence, test: ProbeTest) -> bool | None:
    """Did the state adapters miss what the probes saw? None if they observed nothing."""
    observations = ev.effect if test is ProbeTest.READ_ONLY else ev.idempotence
    if not observations:
        return None
    return not any(o.changed(c) for o in observations for c in StateCategory)

"""The network test: what a tool tried to reach, judged against a declared policy.

Evidence comes from two places: the observer log of each session (every
resolve, connect, send and exec the server's processes made, see
``observer.py``) and the local capture endpoint (bytes that actually arrived,
see ``capture.py``). Each event is attributed to the session it happened in
and to a phase of it: ``startup`` (before the first call), ``call N`` (while
call N was in flight) or ``after N`` (after call N returned).

The verdict is about the configured policy, not about the tool's hints:
``openWorldHint`` is reported as context, never as the rule. Outcomes reuse
the harness's conventions:

* VIOLATION: a disallowed interaction was observed in every run.
* PASS: every session was fully observed and nothing disallowed happened.
* INCONCLUSIVE: observation was unavailable or partial, the evidence differs
  between runs, or the sessions failed. Missing evidence is never a PASS.

An attempt and a transmission are kept apart: a blocked connect is an
attempt; bytes received by the capture endpoint are a transmission.
"""

from __future__ import annotations

import enum
import fnmatch
import ipaddress
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcpgt.capture import CaptureEvent
from mcpgt.observer import ObserverLog, RawEvent, outcome
from mcpgt.verdict import Outcome

if TYPE_CHECKING:
    from mcpgt.runner import FixtureRun, Session, ToolEvidence

CANARY_MARK = "<canary>"
POLICIES = ("deny", "allowlist", "permitted")
COVERAGE = ("complete", "best-effort")

LIMITATIONS = (
    "observed at the libc boundary of processes that loaded the observer: raw system calls, statically "
    "linked programs and children started without the observer are not seen (unobserved children are "
    "reported as coverage gaps)",
    "on macOS, system binaries such as /bin/sh and /usr/bin/curl cannot be observed; they are still "
    "blocked by the sandbox",
    "only bytes that reached the local capture endpoint count as transmitted; a blocked attempt is "
    "reported as an attempt",
    "observation covers the test sessions only; behavior that waits, or detects the test, may not show",
)


class NetworkReason(enum.Enum):
    OBSERVATION_UNAVAILABLE = "OBSERVATION_UNAVAILABLE"  # observer missing, or a session was not observed
    PARTIAL_OBSERVATION = "PARTIAL_OBSERVATION"  # a child process, or log lines, escaped observation
    NONDETERMINISTIC = "NONDETERMINISTIC"  # disallowed interactions in some runs only
    SETUP_FAILED = "SETUP_FAILED"  # a session failed to start, list or finish


@dataclass(frozen=True, slots=True)
class NetworkSpec:
    """The ``[network]`` section of a definition."""

    policy: str = "deny"
    allow: tuple[str, ...] = ()
    capture: bool = False  # start the local capture endpoint and let the sandbox reach it
    canary: bool = True  # plant a synthetic canary, as $MCPGT_CANARY and the {canary} placeholder
    coverage: str = "complete"  # "complete": partial observation is INCONCLUSIVE

    def to_json(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "allow": list(self.allow),
            "capture": self.capture,
            "canary": self.canary,
            "coverage": self.coverage,
        }


@dataclass(frozen=True, slots=True)
class NetworkEvent:
    id: str  # "<session>#<n>": the correlation id used in findings
    session: str
    phase: str  # "startup", "call N", "after N"
    op: str  # "resolve", "connect", "send", "capture"
    transport: str  # "dns", "tcp", "udp", "unix", "http", "proxy-connect", "raw", ...
    destination: str  # what the policy is matched against
    outcome: str  # see observer.outcome; "transmitted" for bytes the capture endpoint received
    canary: bool
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session": self.session,
            "phase": self.phase,
            "op": self.op,
            "transport": self.transport,
            "destination": self.destination,
            "outcome": self.outcome,
            "canary": self.canary,
            **({"detail": dict(self.detail)} if self.detail else {}),
        }


@dataclass(frozen=True, slots=True)
class SessionNetwork:
    label: str
    coverage: str  # "complete", "partial", "unavailable"
    coverage_detail: tuple[str, ...]
    events: tuple[NetworkEvent, ...]
    disallowed: tuple[str, ...]  # event ids the policy does not allow
    ok: bool  # the session itself completed

    def to_json(self) -> dict[str, Any]:
        return {
            "session": self.label,
            "coverage": self.coverage,
            "coverage_detail": list(self.coverage_detail),
            "disallowed": list(self.disallowed),
            "events": [e.to_json() for e in self.events],
        }


@dataclass(frozen=True, slots=True)
class NetworkVerdict:
    tool: str  # tool key, or "(server lifecycle)" for the control sessions
    outcome: Outcome
    reason: NetworkReason | None
    detail: str
    spec: NetworkSpec
    declared_open_world: str | None
    sessions: tuple[SessionNetwork, ...]
    runs: int
    violating_runs: int

    @property
    def events(self) -> tuple[NetworkEvent, ...]:
        return tuple(e for s in self.sessions for e in s.events)

    def to_json(self) -> dict[str, Any]:
        events = self.events
        disallowed = {i for s in self.sessions for i in s.disallowed}
        return {
            "tool": self.tool,
            "test": "network",
            "outcome": self.outcome.value,
            "reason": None if self.reason is None else self.reason.value,
            "detail": self.detail,
            "policy": self.spec.to_json(),
            "declared_open_world": self.declared_open_world,
            "runs": self.runs,
            "violating_runs": self.violating_runs,
            "summary": {
                "events": len(events),
                "disallowed": len(disallowed),
                "attempted": sum(e.outcome != "transmitted" for e in events),
                "blocked": sum(e.outcome == "blocked" for e in events),
                "transmitted": sum(e.outcome == "transmitted" for e in events),
                "canary_seen": any(e.canary for e in events),
                "destinations": sorted({e.destination for e in events if e.id in disallowed}),
            },
            "evidence": sorted(disallowed),
            "remediation": remediation(self),
            "limitations": list(LIMITATIONS),
            "sessions": [s.to_json() for s in self.sessions],
        }


# --- Classification --------------------------------------------------------------


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _mask(text: str, canary: str | None) -> str:
    return text.replace(canary, CANARY_MARK) if canary else text


def _display_path(path: str) -> str:
    """A program path without the local layout: relative to the working
    directory, or ~/..., symlinks left as they are (a venv's python stays one)."""
    absolute = Path(os.path.abspath(path))
    for root, prefix in ((Path.cwd(), ""), (Path.home(), "~/")):
        if absolute.is_relative_to(root):
            return prefix + absolute.relative_to(root).as_posix()
    return absolute.as_posix()


def _phase(ts: int, spans: Sequence[tuple[int, int]]) -> str:
    phase = "startup"
    for i, (start, end) in enumerate(spans, 1):
        if ts < start:
            break
        phase = f"call {i}" if ts <= end else f"after {i}"
    return phase


def classify(
    raw: RawEvent, session: str, n: int, spans: Sequence[tuple[int, int]], canary: str | None, capture_port: int | None
) -> NetworkEvent | None:
    """A network event from one observer line; None for lines that are not interactions."""
    f = raw.fields
    if raw.op == "resolve":
        host = str(f.get("host", ""))
        if host in ("", "localhost") or _is_ip(host):
            return None  # a literal address resolves without any lookup
        return NetworkEvent(
            f"{session}#{n}",
            session,
            _phase(raw.ts, spans),
            "resolve",
            "dns",
            _mask(host, canary),
            outcome(raw),
            bool(f.get("canary")),
        )
    if raw.op not in ("connect", "send"):
        return None
    family = str(f.get("family"))
    if family == "unix":
        destination, transport = "unix:" + str(f.get("path", "")), "unix"
    elif family in ("inet", "inet6"):
        ip, port = str(f["ip"]), int(f["port"])
        is_capture = capture_port is not None and port == capture_port and ipaddress.ip_address(ip).is_loopback
        destination = "capture" if is_capture else (f"[{ip}]:{port}" if family == "inet6" else f"{ip}:{port}")
        transport = str(f.get("socket", "unknown"))
    else:
        return None
    detail = {"bytes": f["bytes"]} if raw.op == "send" and "bytes" in f else {}
    return NetworkEvent(
        f"{session}#{n}",
        session,
        _phase(raw.ts, spans),
        raw.op,
        transport,
        destination,
        outcome(raw),
        bool(f.get("canary")),
        detail,
    )


def classify_capture(
    event: CaptureEvent, session: str, n: int, spans: Sequence[tuple[int, int]], canary: str | None
) -> NetworkEvent:
    host = _mask(event.host or "", canary)
    target = f"{host}:{event.port}" if host and event.port else host
    detail: dict[str, Any] = {"bytes": event.bytes}
    for key in ("method", "path", "query_redacted", "headers"):
        value = getattr(event, key)
        if value not in (None, False, {}):
            detail[key] = _mask(value, canary) if isinstance(value, str) else value
    detail["via"] = "capture endpoint"
    # The destination is the host the request was for; "capture" if it named none.
    return NetworkEvent(
        f"{session}#{n}",
        session,
        _phase(event.ts, spans),
        "capture",
        event.kind,
        target or "capture",
        "transmitted",
        event.canary,
        detail,
    )


def allowed(event: NetworkEvent, spec: NetworkSpec) -> bool:
    """Whether the policy allows this interaction."""
    if spec.policy == "permitted":
        return True
    if spec.policy == "deny":
        return False
    targets = {event.destination}
    if event.op == "capture":
        targets.add("capture")
    for pattern in spec.allow:
        p_host, p_port = _split(pattern)
        for target in targets:
            host, port = _split(target)
            if fnmatch.fnmatchcase(host, p_host) and (p_port in ("", "*") or p_port == port):
                return True
    return False


def _split(target: str) -> tuple[str, str]:
    """(host, port) of "host:port", "[v6]:port", "host", "capture" or "unix:/path"."""
    if target == "capture" or target.startswith("unix:"):
        return target, ""
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        return host, rest.removeprefix(":")
    host, sep, port = target.rpartition(":")
    return (host, port) if sep and (port.isdigit() or port == "*") else (target, "")


# --- Verdicts ---------------------------------------------------------------------


def session_network(
    session: Session,
    spec: NetworkSpec,
    canary: str | None,
    capture_port: int | None,
    captures: Sequence[CaptureEvent],
    observer_error: str | None,
) -> SessionNetwork:
    log: ObserverLog | None = session.network
    spans = session.call_spans
    events: list[NetworkEvent] = []
    detail: list[str] = []
    if observer_error is not None or log is None:
        coverage = "unavailable"
        detail.append(observer_error or "the observer was not loaded for this session")
    elif log.missing or session.server_pid not in log.observed_pids:
        coverage = "unavailable"
        detail.append("the server process never reported the observer loaded (no hello)")
    else:
        coverage = "complete"
        for gap in log.unobserved_children():
            coverage = "partial"
            path = _display_path(str(gap.fields.get("path", "")))
            detail.append(f"child process {path!r} (pid {gap.fields.get('child')}) ran unobserved")
        if log.malformed:
            coverage = "partial"
            detail.append(f"{log.malformed} malformed observer line(s)")
    n = 0
    for raw in () if log is None else log.events:
        event = classify(raw, session.label, n + 1, spans, canary, capture_port)
        if event is not None:
            n += 1
            events.append(event)
    for cap in captures:
        n += 1
        events.append(classify_capture(cap, session.label, n, spans, canary))
    disallowed = tuple(e.id for e in events if not allowed(e, spec))
    return SessionNetwork(session.label, coverage, tuple(detail), tuple(events), disallowed, session.ok)


def captures_for(session: Session, captures: Sequence[CaptureEvent]) -> list[CaptureEvent]:
    if session.window is None:
        return []
    start, end = session.window
    return [c for c in captures if start <= c.ts <= end]


def judge(
    tool: str,
    runs: Sequence[Sequence[SessionNetwork]],
    spec: NetworkSpec,
    declared_open_world: str | None,
) -> NetworkVerdict:
    """One verdict from per-run session evidence (each run: its once and twice sessions)."""
    sessions = tuple(s for run in runs for s in run)
    violating = sum(any(s.disallowed for s in run) for run in runs)

    def verdict(out: Outcome, reason: NetworkReason | None, detail: str) -> NetworkVerdict:
        return NetworkVerdict(tool, out, reason, detail, spec, declared_open_world, sessions, len(runs), violating)

    if runs and violating == len(runs):
        ids = [i for s in sessions for i in s.disallowed]
        return verdict(Outcome.VIOLATION, None, f"disallowed network interaction in every run ({len(ids)} events)")
    if violating:
        return verdict(
            Outcome.INCONCLUSIVE,
            NetworkReason.NONDETERMINISTIC,
            f"disallowed network interaction in {violating} of {len(runs)} runs",
        )
    if not runs or not all(s.ok for s in sessions):
        return verdict(Outcome.INCONCLUSIVE, NetworkReason.SETUP_FAILED, "a session did not complete")
    unavailable = [s for s in sessions if s.coverage == "unavailable"]
    if unavailable:
        return verdict(
            Outcome.INCONCLUSIVE,
            NetworkReason.OBSERVATION_UNAVAILABLE,
            f"{unavailable[0].label}: {'; '.join(unavailable[0].coverage_detail)}",
        )
    partial = [s for s in sessions if s.coverage == "partial"]
    if partial and spec.coverage == "complete":
        return verdict(
            Outcome.INCONCLUSIVE,
            NetworkReason.PARTIAL_OBSERVATION,
            f"{partial[0].label}: {'; '.join(partial[0].coverage_detail)}",
        )
    note = "no disallowed network interaction observed"
    if spec.policy == "permitted":
        note = "network interaction permitted by policy; events recorded"
    return verdict(Outcome.PASS, None, note)


def derive_network(run: FixtureRun) -> list[NetworkVerdict]:
    """Network verdicts for the server lifecycle (control sessions) and every tool."""
    spec = run.fixture.network
    if spec is None:
        return []
    net = run.network
    assert net is not None

    def per(session: Session) -> SessionNetwork:
        return session_network(
            session, spec, net.canary, net.capture_port, captures_for(session, net.captures), net.observer_error
        )

    verdicts = [judge("(server lifecycle)", [[per(s)] for s in run.controls], spec, None)]
    for ev in run.tools:
        runs = [[per(o), per(t)] for o, t in zip(ev.once, ev.twice, strict=True)]
        verdicts.append(judge(ev.tool, runs, spec, _open_world(ev)))
    return verdicts


def _open_world(ev: ToolEvidence) -> str | None:
    return None if ev.contract is None else ev.contract.open_world.value


def remediation(v: NetworkVerdict) -> str | None:
    if v.outcome is Outcome.VIOLATION:
        hint = (
            f" The tool declares openWorldHint: {v.declared_open_world}."
            if v.declared_open_world in ("false", "absent")
            else ""
        )
        return (
            "The tool reached for the network during the test, which the policy does not allow."
            + hint
            + " If the interaction is intended, declare openWorldHint: true, document it, and add the destination "
            "to [network].allow; otherwise remove the call or find which dependency makes it."
        )
    if v.reason is NetworkReason.OBSERVATION_UNAVAILABLE:
        return "Make observation available (a C compiler to build the observer, a server binary that accepts it)."
    if v.reason is NetworkReason.PARTIAL_OBSERVATION:
        return (
            'A child process ran unobserved: start it with the inherited environment, or set coverage = "best-effort".'
        )
    if v.reason is NetworkReason.NONDETERMINISTIC:
        return "Re-run with more runs and inspect the sessions that show the interaction."
    return None

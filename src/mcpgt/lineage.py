"""Lineage: where planted markers travel along a chain of tool calls.

A fixture plants synthetic markers (``{marker:NAME}``) in its seed state or
tool arguments, and names chains of tools to call in order. Each marker is a
fresh random token per run, so wherever it turns up, it came from the place it
was planted.

Markers are looked for at four points:

- in each call's **response** (the verbatim JSON-RPC line);
- in **state** after the session (database records, file contents);
- in bytes the **capture endpoint** received (``egress``);
- in **DNS names** the server tried to look up (``lookup``).

Each also in encoded forms: hex, base64 and base64url (every byte alignment),
and reversed.

Attribution is by controlled comparison, not timing. Each prefix of a chain
runs from a clean reset, several times. A sighting first seen after call j in
every run, and in no run that stopped before call j, is attributed to call j.
When a marker leaves, the chain is also run without each earlier call, to find
which calls the leak needs. A needed call with no observed trace of the marker
between it and the next is an INFERRED hop: the value went through something
the harness does not observe, such as server memory.

Only a marker seen leaving is a VIOLATION. Not seeing it leave is PASS only
when observation was complete and no egress was encrypted.
"""

from __future__ import annotations

import base64
import enum
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcpgt.network import NetworkReason, _phase, captures_for, session_network
from mcpgt.verdict import Outcome, ResetStatus

if TYPE_CHECKING:
    from mcpgt.fixture import ChainSpec, Fixture
    from mcpgt.runner import FixtureRun, Session, State

TRANSMIT = ("egress", "lookup")  # sighting kinds where a marker left, or tried to
KIND_ORDER = {"response": 0, "state": 1, "lookup": 2, "egress": 3}


# --- Markers ------------------------------------------------------------------


def new_markers(names: Sequence[str]) -> dict[str, str]:
    """A fresh token per marker name: lowercase letters and digits, so JSON,
    URL and DNS encoding leave it unchanged."""
    return {name: f"mcpgt{secrets.token_hex(12)}" for name in names}


def variants(markers: Mapping[str, str]) -> dict[str, str]:
    """Every form looked for: needle -> "name/variant"."""
    out: dict[str, str] = {}
    for name, token in markers.items():
        raw = token.encode()
        forms = {"plain": token, "hex": raw.hex(), "hex_upper": raw.hex().upper(), "reversed": token[::-1]}
        for offset in range(3):
            # The part of the base64 text that depends only on the token's
            # bytes, wherever they start relative to a 3-byte group.
            text = base64.b64encode(b"\0" * offset + raw).decode()
            core = text[-(-offset // 3) * 4 : (offset + len(raw)) // 3 * 4]
            out.setdefault(core, f"{name}/base64")
            out.setdefault(core.replace("+", "-").replace("/", "_"), f"{name}/base64url")
        for variant, needle in forms.items():
            out.setdefault(needle, f"{name}/{variant}")
    return out


def _label(label: str) -> tuple[str, str]:
    name, _, variant = label.partition("/")
    return name, variant


def scan_state(fixture: Fixture, state_dir: Path, state: State, needles: Mapping[str, str]) -> tuple[str, ...]:
    """Every "location|name/variant" where a marker is in state: records of
    database and JSON surfaces, contents of files under directory surfaces."""
    hits: set[str] = set()
    for spec in fixture.adapters:
        if spec.kind == "fs":
            root = state_dir / spec.label
            files = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()) if root.is_dir() else []
            for path in files:
                text = path.read_bytes().decode("latin-1")
                where = f"{spec.surface}/{path.relative_to(root).as_posix()}"
                hits |= {f"{where}|{label}" for needle, label in needles.items() if needle in text}
            continue
        snapshot = state.get(spec.surface)
        for region in () if snapshot is None else snapshot.regions:
            text = json.dumps(region.to_json()["records"])
            hits |= {f"{spec.surface}/{region.name}|{label}" for needle, label in needles.items() if needle in text}
    return tuple(sorted(hits))


# --- Sightings ----------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class Sighting:
    kind: str  # "response", "state", "egress" or "lookup"
    where: str  # "call 2", a state location, a destination, or a looked-up name
    marker: str
    variant: str  # "plain", "hex", "base64", ...

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "where": self.where, "marker": self.marker, "variant": self.variant}


def sightings(session: Session, run: FixtureRun) -> dict[Sighting, str | None]:
    """What one session showed: each sighting, with its phase for egress and lookups."""
    net = run.network
    markers = {} if net is None else dict(net.markers)
    needles = variants(markers)
    out: dict[Sighting, str | None] = {}
    for position, response in enumerate(session.responses, start=1):
        for needle, label in needles.items():
            if needle in response.text:
                out[Sighting("response", f"call {position}", *_label(label))] = None
    for hit in session.marker_state:
        where, _, label = hit.partition("|")
        out[Sighting("state", where, *_label(label))] = None
    for capture in [] if net is None else captures_for(session, net.captures):
        where = f"{capture.host}:{capture.port}" if capture.host else capture.kind
        for label in capture.markers:
            out[Sighting("egress", where, *_label(label))] = _phase(capture.ts, session.call_spans)
    for event in () if session.network is None else session.network.events:
        host = str(event.fields.get("host", "")) if event.op == "resolve" else ""
        for needle, label in needles.items():
            if host and needle.lower() in host.lower():
                name, variant = _label(label)
                masked = host.lower().replace(needle.lower(), f"<{name}>")
                out[Sighting("lookup", masked, name, variant)] = _phase(event.ts, session.call_spans)
    return out


# --- Verdicts -------------------------------------------------------------------


class Grade(enum.Enum):
    OBSERVED = "OBSERVED"  # the marker was seen there
    ATTRIBUTED = "ATTRIBUTED"  # seen there after call j in every run, and never before it
    INFERRED = "INFERRED"  # call j is needed for the leak, but the marker was not seen on the way


@dataclass(frozen=True, slots=True)
class Hop:
    call: int | None  # None: the source, before any call
    tool: str | None
    grade: Grade
    what: str  # human-readable: "wrote sqlite:state.db/drafts", "sent to collector.invalid:80 (base64)"
    sightings: tuple[Sighting, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "call": self.call,
            "tool": self.tool,
            "grade": self.grade.value,
            "what": self.what,
            "sightings": [s.to_json() for s in self.sightings],
        }


@dataclass(frozen=True, slots=True)
class MarkerLineage:
    marker: str
    sources: tuple[str, ...]  # where it was planted
    sightings: tuple[tuple[int, Sighting], ...]  # (call it is attributed to, sighting); 0 = before any call
    egress_call: int | None  # the first call after which the marker left
    needed: tuple[int, ...]  # earlier calls the leak needs
    not_needed: tuple[int, ...]  # earlier calls the leak happens without
    path: tuple[Hop, ...]  # source -> needed calls -> egress call

    @property
    def complete(self) -> bool:
        return bool(self.egress_call) and all(h.grade is not Grade.INFERRED for h in self.path)

    def to_json(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "sources": list(self.sources),
            "transmitted": self.egress_call is not None,
            "egress_call": self.egress_call,
            "needed_calls": list(self.needed),
            "not_needed_calls": list(self.not_needed),
            "completeness": None if self.egress_call is None else ("complete" if self.complete else "partial"),
            "path": [h.to_json() for h in self.path],
            "sightings": [{"call": j, **s.to_json()} for j, s in self.sightings],
        }


@dataclass(frozen=True, slots=True)
class LineageVerdict:
    chain: ChainSpec
    outcome: Outcome
    reason: NetworkReason | None
    detail: str
    markers: tuple[MarkerLineage, ...] = ()
    secondary: tuple[str, ...] = ()

    @property
    def completeness(self) -> str | None:
        leaked = [m for m in self.markers if m.egress_call is not None]
        if not leaked:
            return None
        return "complete" if all(m.complete for m in leaked) else "partial"

    def to_json(self) -> dict[str, Any]:
        return {
            "chain": list(self.chain.tools),
            "outcome": self.outcome.value,
            "reason": None if self.reason is None else self.reason.value,
            "detail": self.detail,
            "secondary": list(self.secondary),
            "completeness": self.completeness,
            "markers": [m.to_json() for m in self.markers],
        }


def derive_lineage(run: FixtureRun) -> list[LineageVerdict]:
    spec = run.fixture.lineage
    if spec is None:
        return []
    return [judge(run, chain_run.chain, chain_run.prefixes, chain_run.without) for chain_run in run.lineage]


def judge(
    run: FixtureRun,
    chain: ChainSpec,
    prefixes: Sequence[Sequence[Session]],
    without: Sequence[Sequence[Session]],
) -> LineageVerdict:
    """``prefixes[j-1]``: runs of the first j calls; ``without[i-1]``: runs of the chain minus call i."""

    def verdict(outcome: Outcome, reason: NetworkReason | None, detail: str, **kw: Any) -> LineageVerdict:
        return LineageVerdict(chain, outcome, reason, detail, **kw)

    net = run.network
    groups = [list(run.controls), *map(list, prefixes), *map(list, without)]
    sessions = [s for g in groups for s in g]
    if run.reset.status is not ResetStatus.VERIFIED:
        return verdict(Outcome.INCONCLUSIVE, NetworkReason.SETUP_FAILED, f"reset {run.reset.status.value}")
    if net is None or not all(groups):
        return verdict(Outcome.INCONCLUSIVE, NetworkReason.SETUP_FAILED, "no network run, or no sessions")
    broken = next((s for s in sessions if not s.ok), None)
    if broken is not None:
        detail = f"{broken.label}: {broken.setup_error or broken.transport_error}"
        return verdict(Outcome.INCONCLUSIVE, NetworkReason.SETUP_FAILED, detail)

    issues: list[tuple[NetworkReason, str]] = []
    if run.fixture.network is not None:
        for session in sessions:
            cov = session_network(
                session,
                run.fixture.network,
                net.canary,
                net.capture_port,
                captures_for(session, net.captures),
                net.observer_error,
            )
            if cov.coverage != "complete":
                reason = (
                    NetworkReason.OBSERVATION_UNAVAILABLE
                    if cov.coverage == "unavailable"
                    else NetworkReason.PARTIAL_OBSERVATION
                )
                issues.append((reason, f"{session.label}: {'; '.join(cov.coverage_detail)}"))
    encrypted = sorted(
        {
            f"{c.host}:{c.port}"
            for s in sessions[len(run.controls) :]
            for c in captures_for(s, net.captures)
            if c.kind == "proxy-connect"
        }
    )

    levels = [[set(sightings(s, run)) for s in g] for g in groups[: 1 + len(prefixes)]]
    removed = [[set(sightings(s, run)) for s in g] for g in groups[1 + len(prefixes) :]]
    results = []
    for marker in sorted(dict(net.markers)):
        lineage, problems = _trace(run, chain, marker, levels, removed)
        results.append(lineage)
        issues += [(NetworkReason.NONDETERMINISTIC, p) for p in problems]

    leaked = [m for m in results if m.egress_call is not None]
    secondary = tuple(f"{r.value}: {d}" for r, d in issues)
    if leaked:
        where = "; ".join(
            f"{m.marker} left " + (f"after call {m.egress_call}" if m.egress_call else "at server startup")
            for m in leaked
        )
        return verdict(Outcome.VIOLATION, None, where, markers=tuple(results), secondary=secondary)
    if issues:
        reason, detail = issues[0]
        return verdict(Outcome.INCONCLUSIVE, reason, detail, markers=tuple(results), secondary=secondary[1:])
    if encrypted:
        detail = f"encrypted egress, payload not observable: {', '.join(encrypted)}"
        return verdict(Outcome.INCONCLUSIVE, NetworkReason.PARTIAL_OBSERVATION, detail, markers=tuple(results))
    return verdict(Outcome.PASS, None, "no marker left along this chain", markers=tuple(results))


def _first_level(key: Sighting, levels: Sequence[Sequence[set[Sighting]]]) -> tuple[int | None, bool]:
    """(first level where every run has it, unstable): levels before it must have it in no run."""
    for j, runs in enumerate(levels):
        seen = sum(key in s for s in runs)
        if 0 < seen < len(runs):
            return None, True
        if seen == len(runs):
            return j, False
    return None, False


def _trace(
    run: FixtureRun,
    chain: ChainSpec,
    marker: str,
    levels: Sequence[Sequence[set[Sighting]]],
    removed: Sequence[Sequence[set[Sighting]]],
) -> tuple[MarkerLineage, list[str]]:
    problems: list[str] = []
    keys = sorted({k for runs in levels for s in runs for k in s if k.marker == marker})
    attributed: list[tuple[int, Sighting]] = []
    for key in keys:
        level, unstable = _first_level(key, levels)
        if unstable:
            problems.append(f"{marker}: {key.kind} {key.where} ({key.variant}) seen in some runs only")
        elif level is not None:
            attributed.append((level, key))
    attributed.sort(key=lambda a: (a[0], KIND_ORDER[a[1].kind], a[1]))

    specs = {t.key: t for t in run.fixture.tools}
    sources = [f"state {k.where}" for j, k in attributed if j == 0 and k.kind == "state"]
    sources += [
        f"argument of call {i} ({name})"
        for i, name in enumerate(chain.tools, 1)
        if f"{{marker:{marker}}}" in json.dumps(specs[name].arguments)
    ]
    # Level 0 is the control sessions: a marker leaving there leaves at startup, before any call.
    egress = [j for j, k in attributed if k.kind in TRANSMIT]
    egress_call = min(egress) if egress else None

    needed: list[int] = []
    not_needed: list[int] = []
    for i in range(1, egress_call or 1):
        # Chain without call i: does the marker still leave, in every run, in none?
        left = [any(k.marker == marker and k.kind in TRANSMIT for k in s) for s in removed[i - 1]]
        if all(left):
            not_needed.append(i)
        elif not any(left):
            needed.append(i)
        else:
            problems.append(f"{marker}: leak without call {i} in some runs only")

    if egress_call == 0:
        out = tuple(k for j, k in attributed if j == 0 and k.kind in TRANSMIT)
        path: tuple[Hop, ...] = (Hop(None, None, Grade.ATTRIBUTED, "left at server startup, before any call", out),)
    else:
        path = _path(chain, marker, sources, attributed, needed, egress_call) if egress_call else ()
    lineage = MarkerLineage(
        marker, tuple(sources), tuple(attributed), egress_call, tuple(needed), tuple(not_needed), path
    )
    return lineage, problems


def _path(
    chain: ChainSpec,
    marker: str,
    sources: Sequence[str],
    attributed: Sequence[tuple[int, Sighting]],
    needed: Sequence[int],
    egress_call: int,
) -> tuple[Hop, ...]:
    """Source, then each needed call, then the egress call, each with its evidence grade."""
    hops = [Hop(None, None, Grade.OBSERVED if sources else Grade.INFERRED, "; ".join(sources) or "source not observed")]
    for call in [*needed, egress_call]:
        mine = tuple(k for j, k in attributed if j == call)
        if call == egress_call:
            out = [k for k in mine if k.kind in TRANSMIT]
            what = "; ".join(_describe(k) for k in out)
            hops.append(Hop(call, chain.tools[call - 1], Grade.ATTRIBUTED, what, tuple(out)))
            continue
        # A needed call's hand-off is observed only if the marker shows up in
        # state it wrote. Returning it reaches the client, not the next call:
        # no client relays values between calls here.
        trace = tuple(k for k in mine if k.kind == "state")
        grade = Grade.ATTRIBUTED if trace else Grade.INFERRED
        what = "; ".join(_describe(k) for k in trace) or "needed, but no trace of the marker (unobserved state)"
        hops.append(Hop(call, chain.tools[call - 1], grade, what, trace))
    return tuple(hops)


def _describe(k: Sighting) -> str:
    encoded = "" if k.variant == "plain" else f" ({k.variant})"
    return {
        "response": f"returned it{encoded}",
        "state": f"wrote {k.where}{encoded}",
        "egress": f"sent it to {k.where}{encoded}",
        "lookup": f"looked up {k.where}{encoded}",
    }[k.kind]

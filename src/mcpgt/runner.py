"""Reset, seed, observe and drive a fixture server; collect evidence.

Every session starts from a clean reset and runs the same prefix: start the
server, initialize, list tools. A *control* session stops there. A tool
session then calls the tool once or twice. State is observed after the server
has exited, so anything it writes on shutdown is captured too, and the
control makes sure startup and shutdown writes are never blamed on the tool.

Evidence only; verdict.py interprets it.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcpgt.contracts import Annotation, ContractParseError, DeclaredContract, ToolListing, parse_listing
from mcpgt.fixture import Fixture, FixtureCoverage, ToolSpec
from mcpgt.normalize import NormalizationError
from mcpgt.snapshot import Snapshot, SnapshotDelta, SnapshotError, diff
from mcpgt.verdict import MIN_RUNS, Instability, Observation, ResetStatus, instability_between
from mcpgt.wire import RawResponse, WireClient, WireError

State = dict[str, Snapshot]  # surface -> snapshot


class SetupError(RuntimeError):
    """Reset, seeding or observation failed (SETUP_FAILED)."""


def reset(fixture: Fixture, state_dir: Path) -> None:
    """Wipe ``state_dir`` and rebuild every surface from its seed."""
    if state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True)
    for spec in fixture.adapters:
        db = state_dir / spec.label
        conn = sqlite3.connect(db)
        try:
            if spec.seed is not None:
                conn.executescript((fixture.root / spec.seed).read_text())
            conn.commit()
        except sqlite3.Error as exc:
            raise SetupError(f"{spec.surface}: seed failed: {exc}") from exc
        finally:
            conn.close()


def observe(fixture: Fixture, state_dir: Path) -> State:
    try:
        return {a.surface: a.snapshot() for a in fixture.build_adapters(state_dir)}
    except SnapshotError as exc:
        raise SetupError(str(exc)) from exc


def normalized(fixture: Fixture, state: State) -> State:
    return {s: fixture.adapter_spec(s).normalizer.apply(snap) for s, snap in state.items()}


def deltas(fixture: Fixture, before: State, after: State) -> tuple[SnapshotDelta, ...]:
    """Per-surface deltas between two states, both normalized first."""
    b, a = normalized(fixture, before), normalized(fixture, after)
    return tuple(
        diff(b[s], a[s], fixture.adapter_spec(s).classification) for s in sorted(b.keys() | a.keys())
    )


@dataclass(frozen=True, slots=True)
class ResetVerification:
    status: ResetStatus
    detail: str | None
    deltas: tuple[SnapshotDelta, ...]  # between the two resets; empty when verified
    coverage: FixtureCoverage

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "detail": self.detail,
            "deltas": [d.to_json() for d in self.deltas],
            "coverage": self.coverage.to_json(),
        }


def verify_reset(fixture: Fixture, state_dir: Path) -> ResetVerification:
    """reset, snapshot, reset, snapshot: the two must match after normalization.

    Also checks every mask rule against the seeded state, since a rule that
    matches nothing would leave a volatile column unmasked.
    """
    coverage = fixture.coverage(state_dir)
    try:
        reset(fixture, state_dir)
        first = observe(fixture, state_dir)
        for surface, snap in first.items():
            fixture.adapter_spec(surface).normalizer.validate_against(snap)
        reset(fixture, state_dir)
        second = observe(fixture, state_dir)
    except (SetupError, NormalizationError) as exc:
        return ResetVerification(ResetStatus.SETUP_FAILED, str(exc), (), coverage)
    changed = tuple(d for d in deltas(fixture, first, second) if d.regions)
    if changed:
        return ResetVerification(
            ResetStatus.UNVERIFIED, "two resets produced different state", changed, coverage
        )
    return ResetVerification(ResetStatus.VERIFIED, None, (), coverage)


def server_env(state_dir: Path) -> dict[str, str]:
    """A minimal, fixed environment, so runs do not depend on the caller's shell."""
    env = {
        "MCPGT_STATE_DIR": str(state_dir.resolve()),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",  # keep fixture dirs, and their content digest, clean
    }
    for key in ("PATH", "HOME", "SYSTEMROOT"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


async def start_server(fixture: Fixture, state_dir: Path, timeout: float = 10.0) -> WireClient:
    client = await WireClient.start(
        fixture.server_command(), env=server_env(state_dir), cwd=fixture.root, timeout=timeout
    )
    try:
        await client.initialize()
    except BaseException:
        await client.close()
        raise
    return client


# --- Sessions ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    label: str
    calls: tuple[str, ...]  # tool names called, in order
    setup_error: str | None = None  # reset, start, initialize, list or observe failed
    wire_error: str | None = None  # connection failed during the calls
    initialize_text: str | None = None
    listing_pages: tuple[str, ...] = ()
    responses: tuple[RawResponse, ...] = ()
    before: State | None = None  # right after reset
    after: State | None = None  # after the server exited
    stderr: str = ""

    @property
    def transport_error(self) -> str | None:
        """The connection died, or a call got a JSON-RPC error instead of a result."""
        if self.wire_error is not None:
            return self.wire_error
        for response in self.responses:
            if response.is_error:
                return f"JSON-RPC error: {response.text[:500]}"
        return None

    @property
    def tool_errored(self) -> bool:
        """Some call completed with ``isError: true``."""
        return any(r.result is not None and r.result.get("isError") is True for r in self.responses)

    @property
    def ok(self) -> bool:
        """Completed and observed. Tool-level errors are judged separately."""
        return self.setup_error is None and self.transport_error is None and self.after is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "calls": list(self.calls),
            "setup_error": self.setup_error,
            "transport_error": self.transport_error,
            "tool_errored": self.tool_errored,
            "responses": [r.text for r in self.responses],
            "stderr_tail": self.stderr[-2000:],
        }


async def run_session(
    fixture: Fixture, state_dir: Path, label: str, calls: Sequence[ToolSpec] = ()
) -> Session:
    names = tuple(c.name for c in calls)
    try:
        reset(fixture, state_dir)
        before = observe(fixture, state_dir)
    except SetupError as exc:
        return Session(label, names, setup_error=str(exc))
    try:
        client = await start_server(fixture, state_dir)
    except (WireError, OSError) as exc:
        return Session(label, names, setup_error=f"server did not start: {exc}", before=before)

    initialize_text = client.initialize_response.text if client.initialize_response else None
    pages: list[RawResponse] = []
    responses: list[RawResponse] = []
    setup_error = wire_error = None
    try:
        pages = await client.list_tools()
        if any(p.result is None for p in pages):
            setup_error = f"tools/list failed: {pages[-1].text[:500]}"
        else:
            for call in calls:
                responses.append(await client.call_tool(call.name, call.arguments))
    except WireError as exc:
        if pages and not setup_error:
            wire_error = str(exc)
        else:
            setup_error = f"tools/list failed: {exc}"
    finally:
        await client.close()

    try:
        after = observe(fixture, state_dir)
    except SetupError as exc:
        setup_error = setup_error or str(exc)
        after = None
    return Session(
        label=label,
        calls=names,
        setup_error=setup_error,
        wire_error=wire_error,
        initialize_text=initialize_text,
        listing_pages=tuple(p.text for p in pages),
        responses=tuple(responses),
        before=before,
        after=after,
        stderr=client.stderr,
    )


# --- Evidence ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    tool: str
    contract: DeclaredContract | None
    contract_error: str | None  # why there is no contract
    contract_instability: tuple[str, ...]  # hints whose declared value differed across sessions
    once: tuple[Session, ...]
    twice: tuple[Session, ...]
    idempotence: tuple[Observation, ...]  # per run: once vs twice
    effect: tuple[Observation, ...]  # per run: control vs once
    once_instability: tuple[Instability, ...]  # once-states that differ from once-1
    twice_instability: tuple[Instability, ...]
    call_error: str | None  # transport error, or isError with no state change
    errored_with_side_effects: bool  # isError, yet state changed; judged on state

    @property
    def setup_error(self) -> str | None:
        return next((s.setup_error for s in self.once + self.twice if s.setup_error), None)


@dataclass(frozen=True, slots=True)
class ListingRecord:
    listing: ToolListing
    sessions: tuple[str, ...]  # labels of sessions that returned exactly this listing


@dataclass(frozen=True, slots=True)
class FixtureRun:
    fixture: Fixture
    runs: int
    reset: ResetVerification
    coverage: FixtureCoverage
    controls: tuple[Session, ...]
    control_error: str | None
    control_instability: tuple[Instability, ...]
    control_effect: tuple[Observation, ...]  # per control: seed vs control (startup/shutdown writes)
    listings: tuple[ListingRecord, ...]
    listing_errors: tuple[str, ...]
    tools: tuple[ToolEvidence, ...]
    untested_tools: tuple[str, ...] = field(default=())  # listed by the server, no ToolSpec

    @property
    def control_stable(self) -> bool:
        return self.control_error is None and not self.control_instability

    @property
    def sessions(self) -> tuple[Session, ...]:
        out = list(self.controls)
        for ev in self.tools:
            out.extend(ev.once + ev.twice)
        return tuple(out)

    @property
    def initialize_text(self) -> str | None:
        return next((s.initialize_text for s in self.sessions if s.initialize_text), None)


async def run_fixture(
    fixture: Fixture, work_dir: Path, runs: int = MIN_RUNS, only_tool: str | None = None
) -> FixtureRun:
    if runs < MIN_RUNS:
        raise ValueError(f"at least {MIN_RUNS} runs are required (got {runs})")
    specs = [t for t in fixture.tools if only_tool is None or t.name == only_tool]
    if only_tool is not None and not specs:
        raise ValueError(f"fixture {fixture.name} has no tool spec named {only_tool!r}")
    state_dir = work_dir / "state"
    reset_result = verify_reset(fixture, state_dir)
    coverage = fixture.coverage(state_dir)

    controls: list[Session] = []
    per_tool: dict[str, tuple[list[Session], list[Session]]] = {s.name: ([], []) for s in specs}
    if reset_result.status is not ResetStatus.SETUP_FAILED:
        for i in range(1, runs + 1):
            controls.append(await run_session(fixture, state_dir, f"control-{i}"))
        for spec in specs:
            once, twice = per_tool[spec.name]
            for i in range(1, runs + 1):
                once.append(await run_session(fixture, state_dir, f"{spec.name}/once-{i}", [spec]))
                twice.append(await run_session(fixture, state_dir, f"{spec.name}/twice-{i}", [spec, spec]))

    if reset_result.status is ResetStatus.SETUP_FAILED:
        control_error = reset_result.detail
    else:
        control_error = next((s.setup_error or s.transport_error for s in controls if not s.ok), None)
    control_instability = _instability(fixture, [s for s in controls if s.ok])
    control_effect = tuple(
        Observation("seed", deltas(fixture, s.before, s.after))
        for s in controls
        if s.before is not None and s.after is not None
    )
    baseline = controls[0].after if control_error is None and not control_instability else None

    all_sessions = list(controls) + [s for o, t in per_tool.values() for s in o + t]
    listings, listing_errors, contract_sets = _listings(all_sessions)

    evidence = []
    for spec in specs:
        once, twice = per_tool[spec.name]
        contract, contract_error, contract_instability = _contract_for(spec.name, contract_sets, listing_errors)
        call_error, side_effects = _tool_errors(fixture, once + twice, baseline)
        idempotence = tuple(
            Observation("once", deltas(fixture, o.after, t.after))
            for o, t in zip(once, twice)
            if o.ok and t.ok
        )
        effect = tuple(
            Observation("control", deltas(fixture, baseline, o.after))
            for o in once
            if baseline is not None and o.ok
        )
        evidence.append(
            ToolEvidence(
                tool=spec.name,
                contract=contract,
                contract_error=contract_error,
                contract_instability=contract_instability,
                once=tuple(once),
                twice=tuple(twice),
                idempotence=idempotence,
                effect=effect,
                once_instability=_instability(fixture, [o for o in once if o.ok]),
                twice_instability=_instability(fixture, [t for t in twice if t.ok]),
                call_error=call_error,
                errored_with_side_effects=side_effects,
            )
        )

    listed = {name for contracts in contract_sets for name in contracts}
    return FixtureRun(
        fixture=fixture,
        runs=runs,
        reset=reset_result,
        coverage=coverage,
        controls=tuple(controls),
        control_error=control_error,
        control_instability=control_instability,
        control_effect=control_effect,
        listings=listings,
        listing_errors=listing_errors,
        tools=tuple(evidence),
        untested_tools=tuple(sorted(listed - {s.name for s in fixture.tools})),
    )


def _instability(fixture: Fixture, sessions: Sequence[Session]) -> tuple[Instability, ...]:
    """Each session whose normalized end state differs from the first one's."""
    if not sessions:
        return ()
    first = sessions[0]
    found = []
    for other in sessions[1:]:
        item = instability_between((first.label, other.label), deltas(fixture, first.after, other.after))
        if item is not None:
            found.append(item)
    return tuple(found)


def _tool_errors(
    fixture: Fixture, sessions: Sequence[Session], baseline: State | None
) -> tuple[str | None, bool]:
    """(call error, errored with side effects).

    A call that returned ``isError: true`` is a CALL_ERROR only if it left no
    trace. If state changed anyway, the session is judged on its state like
    any other, and the evidence says it errored with side effects. Transport
    errors are always CALL_ERROR: state after a dead connection is not a
    completed call's state.
    """
    side_effects = False
    for session in sessions:
        if session.transport_error is not None:
            return session.transport_error, side_effects
        if session.tool_errored and session.ok:
            reference = baseline if baseline is not None else session.before
            if any(d.regions for d in deltas(fixture, reference, session.after)):
                side_effects = True
            else:
                return f"{session.label}: tool returned isError with no state change", side_effects
    return None, side_effects


def _listings(
    sessions: Sequence[Session],
) -> tuple[tuple[ListingRecord, ...], tuple[str, ...], list[dict[str, DeclaredContract]]]:
    """Distinct listings seen, any parse errors, and each session's contracts."""
    by_key: dict[str, tuple[ToolListing, list[str]]] = {}
    errors: list[str] = []
    contract_sets: list[dict[str, DeclaredContract]] = []
    for session in sessions:
        if not session.listing_pages or session.setup_error:
            continue
        try:
            listing = parse_listing(session.listing_pages)
        except ContractParseError as exc:
            errors.append(f"{session.label}: {exc}")
            continue
        # Distinct by parsed contracts, so incidental byte differences between
        # sessions do not count as instability; raw pages of each are kept.
        key = json.dumps([c.to_json() for c in listing.contracts.values()], sort_keys=True)
        by_key.setdefault(key, (listing, []))[1].append(session.label)
        contract_sets.append(dict(listing.contracts))
    records = tuple(ListingRecord(listing, tuple(labels)) for listing, labels in by_key.values())
    return records, tuple(errors), contract_sets


def _contract_for(
    name: str, contract_sets: Sequence[dict[str, DeclaredContract]], errors: Sequence[str]
) -> tuple[DeclaredContract | None, str | None, tuple[str, ...]]:
    """(contract, why there is none, hints that differed across sessions)."""
    seen = [cs[name] for cs in contract_sets if name in cs]
    if not seen:
        why = "; ".join(errors) if errors else f"tool {name!r} not in any tools/list response"
        return None, why, ()
    differing = [
        a.wire_name for a in Annotation if len({c.hint(a) for c in seen}) > 1
    ]
    if len(seen) < len(contract_sets):
        differing.append("(tool missing from some listings)")
    elif not differing and any(c != seen[0] for c in seen):
        differing.append("(annotation anomalies)")
    return seen[0], None, tuple(differing)

"""Reset, seed, observe and drive a fixture server; collect evidence.

Every session starts from a clean reset and runs the same prefix: start the
server, initialize, list tools. A *control* session stops there. A tool
session then calls the tool once or twice. State is observed after the server
has exited, so anything it writes on shutdown is captured too, and the
control makes sure startup and shutdown writes are never blamed on the tool.

Evidence only; verdict.py interprets it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcpgt import observer as observers
from mcpgt.capture import CaptureEvent, CaptureServer
from mcpgt.contracts import Annotation, ContractParseError, DeclaredContract, Hint, ToolListing, parse_listing
from mcpgt.fixture import Fixture, FixtureCoverage, ToolSpec, expand_arguments
from mcpgt.normalize import NormalizationError
from mcpgt.sandbox import Isolation, isolated, isolation
from mcpgt.snapshot import Snapshot, SnapshotDelta, SnapshotError, StateCategory, diff
from mcpgt.sources import NodeRuntime, PreparedSource, node_runtime
from mcpgt.sources import verify as verify_source
from mcpgt.verdict import (
    CONCURRENT_ATTEMPTS,
    FALSIFIABLE,
    MIN_RUNS,
    Attempt,
    ConcurrencyMode,
    ErrorKind,
    ErrorRecord,
    Instability,
    Observation,
    Overlap,
    ResetStatus,
    instability_between,
)
from mcpgt.wire import RawResponse, WireClient, WireError

State = dict[str, Snapshot]  # surface -> snapshot


class SetupError(RuntimeError):
    """Reset, seeding or observation failed (SETUP_FAILED)."""


def reset(fixture: Fixture, state_dir: Path) -> None:
    """Wipe ``state_dir`` and rebuild every surface from its seed."""
    if state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True)
    # Outer directories first, so a file seeded inside a seeded tree lands last.
    for spec in sorted(fixture.adapters, key=lambda a: (a.label != ".", a.depth)):
        target = state_dir / spec.label
        seed = None if spec.seed is None else fixture.root / spec.seed
        try:
            if spec.kind == "fs":
                if seed is None:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    shutil.copytree(seed, target, symlinks=True, dirs_exist_ok=True)
                for empty in spec.empty_dirs:
                    (target / empty).mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if spec.kind == "sqlite":
                _seed_sqlite(target, seed)
            elif seed is None:
                target.write_text("" if spec.json_format == "jsonl" else "{}\n")
            else:
                shutil.copyfile(seed, target)
        except (sqlite3.Error, OSError) as exc:
            raise SetupError(f"{spec.surface}: seed failed: {exc}") from exc


def _seed_sqlite(db: Path, script: Path | None) -> None:
    conn = sqlite3.connect(db)
    try:
        if script is not None:
            conn.executescript(script.read_text())
        conn.commit()
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
    return tuple(diff(b[s], a[s], fixture.adapter_spec(s).classification) for s in sorted(b.keys() | a.keys()))


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
        return ResetVerification(ResetStatus.UNVERIFIED, "two resets produced different state", changed, coverage)
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


@dataclass(frozen=True, slots=True)
class Launch:
    """How to start the server under test, resolved once per run."""

    command: tuple[str, ...]
    env: dict[str, str]
    cwd: Path
    isolation: Isolation | None
    source: PreparedSource | None
    node: NodeRuntime | None = None
    netobs_dir: Path | None = None  # per-session observer logs go here; None: not observed
    canary: str | None = None


@dataclass(frozen=True, slots=True)
class NetworkSetup:
    """What a run with a [network] section prepared before any server starts."""

    observer: observers.Observer | None
    observer_error: str | None  # why there is no observer
    log_dir: Path
    canary: str | None
    capture: CaptureServer | None


def launch_for(fixture: Fixture, state_dir: Path, network: NetworkSetup | None = None) -> Launch:
    """Resolve the server command. For targets, first verify the pinned source
    (SourceError) and the sandbox (IsolationUnavailable): a target never runs
    from an unverified checkout or with network access. Neither does anything
    whose network behavior is tested."""
    node = None if fixture.node is None else node_runtime(fixture.node)
    source = verify_source(fixture.source, node_major=fixture.node) if fixture.source is not None else None
    source_path = None if source is None else source.path
    canary = None if network is None else network.canary
    command = fixture.server_command(source_path, state_dir, canary)
    if node is not None:
        # Run exactly the Node binary whose version was checked.
        command = [node.path if part == "node" else part for part in command]
    env = {**server_env(state_dir), **fixture.server_env(state_dir, source_path, canary)}
    if not fixture.isolated:
        return Launch(tuple(command), env, fixture.root, None, source, node)
    iso = isolation()
    writable: list[Path] = []
    ports: list[int] = []
    netobs_dir = None
    if network is not None:
        network.log_dir.mkdir(parents=True, exist_ok=True)
        writable.append(network.log_dir)
        if network.observer is not None:
            netobs_dir = network.log_dir
            command = [*network.observer.command_prefix(), *command]
            env.update(network.observer.environment(network.log_dir / "unassigned.jsonl", canary))
        if network.capture is not None:
            ports.append(network.capture.port)
            url = network.capture.url
            env["MCPGT_CAPTURE_URL"] = url
            # Proxy-aware clients send every request to the capture endpoint,
            # which records the destination they asked for and forwards nothing.
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env[key] = url
            env["NO_PROXY"] = env["no_proxy"] = ""
            env["NODE_USE_ENV_PROXY"] = "1"
        elif canary is not None:
            env[observers.CANARY_ENV] = canary
    # Targets run with the state directory as working directory, so relative
    # writes land in observed state rather than in the target definition.
    return Launch(
        tuple(isolated(command, state_dir, writable, ports)),
        env,
        state_dir.resolve(),
        iso,
        source,
        node,
        netobs_dir,
        canary,
    )


async def start_server(
    fixture: Fixture, state_dir: Path, timeout: float = 10.0, launch: Launch | None = None
) -> WireClient:
    launch = launch or launch_for(fixture, state_dir)
    client = await WireClient.start(launch.command, env=launch.env, cwd=launch.cwd, timeout=timeout)
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
    server_pid: int | None = None
    window: tuple[int, int] | None = None  # wall clock (ns) from server start to exit
    call_spans: tuple[tuple[int, int], ...] = ()  # wall clock (ns) each call was in flight
    network: observers.ObserverLog | None = None  # observer log; None if not observed

    @property
    def transport_error(self) -> str | None:
        """The connection died mid-session."""
        return self.wire_error

    @property
    def call_errors(self) -> tuple[tuple[int, ErrorKind], ...]:
        """(1-based call position, kind) for every call that completed with an error."""
        return _call_errors(self.responses)

    @property
    def ok(self) -> bool:
        """Completed and observed. Calls that returned errors are judged separately."""
        return self.setup_error is None and self.transport_error is None and self.after is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "calls": list(self.calls),
            "setup_error": self.setup_error,
            "transport_error": self.transport_error,
            "call_errors": [{"position": p, "kind": k.value} for p, k in self.call_errors],
            "responses": [r.text for r in self.responses],
            "stderr_tail": self.stderr[-2000:],
        }


async def run_session(
    fixture: Fixture,
    state_dir: Path,
    label: str,
    calls: Sequence[ToolSpec] = (),
    launch: Launch | None = None,
) -> Session:
    names = tuple(c.name for c in calls)
    try:
        reset(fixture, state_dir)
        before = observe(fixture, state_dir)
    except SetupError as exc:
        return Session(label, names, setup_error=str(exc))
    log = None
    if launch is not None and launch.netobs_dir is not None:
        log = launch.netobs_dir / (re.sub(r"[^A-Za-z0-9._-]+", "_", label) + ".jsonl")
        log.unlink(missing_ok=True)
        launch = dataclasses.replace(launch, env={**launch.env, observers.LOG_ENV: str(log.resolve())})
    started = time.time_ns()
    try:
        client = await start_server(fixture, state_dir, launch=launch)
    except (WireError, OSError) as exc:
        return Session(label, names, setup_error=f"server did not start: {exc}", before=before)
    spans: list[tuple[int, int]] = []
    canary = None if launch is None else launch.canary

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
                t0 = time.time_ns()
                responses.append(await client.call_tool(call.name, expand_arguments(call.arguments, canary)))
                spans.append((t0, time.time_ns()))
    except WireError as exc:
        if pages and not setup_error:
            wire_error = str(exc)
        else:
            setup_error = f"tools/list failed: {exc}"
    finally:
        await client.close()
    ended = time.time_ns()

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
        server_pid=client.pid,
        window=(started, ended),
        call_spans=tuple(spans),
        network=None if log is None else observers.read_log(log),
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
    call_error: str | None  # dead connection, or an error response with no state change
    errored_with_side_effects: bool  # single-call session errored, yet state changed
    errors: tuple[ErrorRecord, ...]  # every errored call in once/twice sessions
    concurrent: tuple[Attempt, ...] = ()
    concurrent_ran: bool = False
    concurrent_note: str | None = None  # why attempts were not run, or a mode was excluded
    two_process_skipped: bool = False  # server lifecycle writes state; two_process not comparable
    concurrent_setup_error: str | None = None
    concurrent_call_error: str | None = None
    concurrent_errors: tuple[ErrorRecord, ...] = ()
    concurrent_raw: tuple[ConcurrentRun, ...] = ()

    @property
    def setup_error(self) -> str | None:
        return next((s.setup_error for s in self.once + self.twice if s.setup_error), None)


@dataclass(frozen=True, slots=True)
class ListingRecord:
    listing: ToolListing
    sessions: tuple[str, ...]  # labels of sessions that returned exactly this listing


@dataclass(frozen=True, slots=True)
class NetworkRun:
    """Run-level network evidence: what was set up, and what reached the capture endpoint."""

    observer: dict[str, str] | None
    observer_error: str | None
    canary: str | None
    capture_port: int | None
    captures: tuple[CaptureEvent, ...]


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
    launch: Launch | None = None
    network: NetworkRun | None = None

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
    fixture: Fixture,
    work_dir: Path,
    runs: int = MIN_RUNS,
    only_tools: Sequence[str] | None = None,
    concurrent_attempts: int = CONCURRENT_ATTEMPTS,
    observe_concurrent: bool = False,
) -> FixtureRun:
    """Run every test for every tool spec; see ``_run_fixture``.

    With a [network] section, the observer is built and the capture endpoint
    (if asked for) is opened first, and the endpoint is closed however the run
    ends.
    """
    spec = fixture.network
    if spec is None:
        return await _run_fixture(fixture, work_dir, runs, only_tools, concurrent_attempts, observe_concurrent)
    try:
        observer, observer_error = observers.build(), None
    except observers.ObserverUnavailable as exc:
        observer, observer_error = None, str(exc)
    canary = f"mcpgt-canary-{secrets.token_hex(8)}" if spec.canary else None
    async with contextlib.AsyncExitStack() as stack:
        capture = await stack.enter_async_context(CaptureServer(canary)) if spec.capture else None
        setup = NetworkSetup(observer, observer_error, work_dir / "netobs", canary, capture)
        run = await _run_fixture(fixture, work_dir, runs, only_tools, concurrent_attempts, observe_concurrent, setup)
        captures = tuple(capture.events) if capture is not None else ()
    return dataclasses.replace(
        run,
        network=NetworkRun(
            observer=None if observer is None else observer.to_json(),
            observer_error=observer_error,
            canary=canary,
            capture_port=None if capture is None else capture.port,
            captures=captures,
        ),
    )


async def _run_fixture(
    fixture: Fixture,
    work_dir: Path,
    runs: int,
    only_tools: Sequence[str] | None,
    concurrent_attempts: int,
    observe_concurrent: bool,
    network: NetworkSetup | None = None,
) -> FixtureRun:
    """Run every test for every tool spec.

    ``observe_concurrent`` also runs concurrent attempts for tools with no
    falsifiable idempotence claim, as observation only: their verdict is
    decided by the declaration as always, with the observed behavior attached.
    """
    if runs < MIN_RUNS:
        raise ValueError(f"at least {MIN_RUNS} runs are required (got {runs})")
    wanted = set(only_tools or ())
    specs = [t for t in fixture.tools if not wanted or {t.name, t.key} & wanted]
    unknown = wanted - {n for t in fixture.tools for n in (t.name, t.key)}
    if unknown:
        raise ValueError(f"{fixture.name} has no tool spec named {', '.join(sorted(unknown))}")
    state_dir = work_dir / "state"
    launch = launch_for(fixture, state_dir, network)  # targets: verify pinned source and sandbox first
    reset_result = verify_reset(fixture, state_dir)
    coverage = fixture.coverage(state_dir)

    controls: list[Session] = []
    per_tool: dict[str, tuple[list[Session], list[Session]]] = {s.key: ([], []) for s in specs}
    if reset_result.status is not ResetStatus.SETUP_FAILED:
        for i in range(1, runs + 1):
            controls.append(await run_session(fixture, state_dir, f"control-{i}", launch=launch))
        for spec in specs:
            once, twice = per_tool[spec.key]
            for i in range(1, runs + 1):
                once.append(await run_session(fixture, state_dir, f"{spec.key}/once-{i}", [spec], launch))
                twice.append(await run_session(fixture, state_dir, f"{spec.key}/twice-{i}", [spec, spec], launch))

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
        once, twice = per_tool[spec.key]
        contract, contract_error, contract_instability = _contract_for(spec.name, contract_sets, listing_errors)
        call_error, side_effects, errors = _error_evidence(fixture, once, twice, baseline)
        idempotence = tuple(
            Observation("once", deltas(fixture, _end(o), _end(t)))
            for o, t in zip(once, twice, strict=True)
            if o.ok and t.ok
        )
        effect = tuple(
            Observation("control", deltas(fixture, baseline, _end(o))) for o in once if baseline is not None and o.ok
        )
        evidence.append(
            ToolEvidence(
                tool=spec.key,
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
                errors=errors,
            )
        )

    # Concurrent attempts, only where there is a falsifiable idempotence claim
    # to test, so the 2 x N extra sessions are spent where they can decide.
    lifecycle_inert = all(not any(o.changed(c) for c in StateCategory) for o in control_effect)
    for i, (spec, ev) in enumerate(zip(specs, evidence, strict=True)):
        once_ok = [o for o in per_tool[spec.key][0] if o.ok]
        reason = _concurrent_skip_reason(ev, once_ok, observe_concurrent)
        if reason is not None:
            evidence[i] = dataclasses.replace(ev, concurrent_note=reason)
            continue
        attempts_raw: list[ConcurrentRun] = []
        # Two instances boot twice: if booting writes state, those attempts
        # could never be compared with a single-call session, so skip them.
        modes = [m for m in ConcurrencyMode if lifecycle_inert or m is not ConcurrencyMode.TWO_PROCESS]
        for mode in modes:
            for n in range(1, concurrent_attempts + 1):
                label = f"{spec.key}/{mode.value}-{n}"
                attempts_raw.append(await run_concurrent(fixture, state_dir, spec, mode, label, launch))
        evidence[i] = _with_concurrent(fixture, ev, attempts_raw, _end(once_ok[0]), baseline, lifecycle_inert)
        if observe_concurrent and not _falsifiable_idempotence(ev):
            note = "observation only (--observe-concurrent): no falsifiable idempotence claim"
            prior = evidence[i].concurrent_note
            evidence[i] = dataclasses.replace(evidence[i], concurrent_note=f"{note}; {prior}" if prior else note)

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
        launch=launch,
    )


def _end(session: Session | ConcurrentRun) -> State:
    """The observed end state of a session already known to be ``ok``."""
    if session.after is None:
        raise RuntimeError(f"{session.label}: no observed end state")
    return session.after


def _instability(fixture: Fixture, sessions: Sequence[Session]) -> tuple[Instability, ...]:
    """Each session whose normalized end state differs from the first one's."""
    if not sessions:
        return ()
    first = sessions[0]
    found = []
    for other in sessions[1:]:
        item = instability_between((first.label, other.label), deltas(fixture, _end(first), _end(other)))
        if item is not None:
            found.append(item)
    return tuple(found)


def _call_errors(responses: Sequence[RawResponse]) -> tuple[tuple[int, ErrorKind], ...]:
    out = []
    for position, response in enumerate(responses, start=1):
        if response.is_error:
            out.append((position, ErrorKind.PROTOCOL_ERROR))
        elif response.result is not None and response.result.get("isError") is True:
            out.append((position, ErrorKind.TOOL_ERROR))
    return tuple(out)


def _state_changed(fixture: Fixture, reference: State | None, after: State | None) -> bool:
    if reference is None or after is None:
        return False
    return any(d.regions for d in deltas(fixture, reference, after))


def _error_evidence(
    fixture: Fixture, once: Sequence[Session], twice: Sequence[Session], baseline: State | None
) -> tuple[str | None, bool, tuple[ErrorRecord, ...]]:
    """(call error, errored with side effects, error records).

    A call that completed with an error (``isError: true`` or a JSON-RPC
    error) is a CALL_ERROR only if its session left no trace against the
    control state. Otherwise the session is judged on its state like any
    other. ``errored_with_side_effects`` is raised only from single-call
    sessions, where the change can be attributed to the erroring call; in
    twice sessions the erroring call positions are recorded without raising
    it. A dead connection is always a CALL_ERROR.
    """
    call_error: str | None = None
    side_effects = False
    records: list[ErrorRecord] = []
    for session in list(once) + list(twice):
        if session.transport_error is not None:
            call_error = call_error or f"{session.label}: {session.transport_error}"
            continue
        if not session.call_errors or not session.ok:
            continue
        reference = baseline if baseline is not None else session.before
        changed = _state_changed(fixture, reference, session.after)
        for position, kind in session.call_errors:
            records.append(ErrorRecord(session.label, position, kind, changed))
        if not changed:
            kinds = ", ".join(sorted({k.value for _, k in session.call_errors}))
            call_error = call_error or f"{session.label}: {kinds} with no state change"
        elif len(session.calls) == 1:
            side_effects = True
    return call_error, side_effects, tuple(records)


# --- Concurrent attempts ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConcurrentRun:
    label: str
    mode: ConcurrencyMode
    setup_error: str | None = None
    wire_error: str | None = None
    responses: tuple[RawResponse, ...] = ()
    windows: tuple[tuple[float, float], ...] = ()  # (sent, received) per call, monotonic seconds
    rtts: tuple[float, ...] = ()  # worst ping round trip per connection, seconds
    before: State | None = None
    after: State | None = None

    @property
    def ok(self) -> bool:
        return self.setup_error is None and self.wire_error is None and self.after is not None

    @property
    def wire_overlap(self) -> float | None:
        if len(self.windows) != 2:
            return None
        return min(r for _, r in self.windows) - max(s for s, _ in self.windows)

    @property
    def overlap(self) -> Overlap:
        """Two independent instances: ``in_flight_overlap`` when both calls were
        in flight together for longer than both instances' ping round trips
        combined (a heuristic). One connection: never established, since the
        server may serialize requests."""
        wire = self.wire_overlap
        if wire is None or wire <= 0:
            return Overlap.NONE
        if self.mode is ConcurrencyMode.TWO_PROCESS and len(self.rtts) == 2 and wire > sum(self.rtts):
            return Overlap.IN_FLIGHT
        return Overlap.UNVERIFIED

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "mode": self.mode.value,
            "setup_error": self.setup_error,
            "transport_error": self.wire_error,
            "responses": [r.text for r in self.responses],
        }


async def _ping_rtt(client: WireClient, pings: int = 3) -> float:
    worst = 0.0
    for _ in range(pings):
        start = time.monotonic()
        await client.request("ping")
        worst = max(worst, time.monotonic() - start)
    return worst


async def _timed_call(
    client: WireClient, spec: ToolSpec, canary: str | None = None
) -> tuple[RawResponse, tuple[float, float]]:
    sent = time.monotonic()
    response = await client.call_tool(spec.name, expand_arguments(spec.arguments, canary))
    return response, (sent, time.monotonic())


async def run_concurrent(
    fixture: Fixture,
    state_dir: Path,
    spec: ToolSpec,
    mode: ConcurrencyMode,
    label: str,
    launch: Launch | None = None,
) -> ConcurrentRun:
    """reset -> start (one or two instances) -> list -> ping -> two calls at once -> stop -> observe."""
    try:
        reset(fixture, state_dir)
        before = observe(fixture, state_dir)
    except SetupError as exc:
        return ConcurrentRun(label, mode, setup_error=str(exc))
    instances = 1 if mode is ConcurrencyMode.SINGLE_CONNECTION else 2
    started = await asyncio.gather(
        *(start_server(fixture, state_dir, launch=launch) for _ in range(instances)), return_exceptions=True
    )
    clients = [c for c in started if isinstance(c, WireClient)]
    failures = [c for c in started if not isinstance(c, WireClient)]
    setup_error = wire_error = None
    responses: list[RawResponse] = []
    windows: list[tuple[float, float]] = []
    rtts: list[float] = []
    try:
        if failures:
            setup_error = f"server did not start: {failures[0]}"
        else:
            for client in clients:
                pages = await client.list_tools()
                if any(p.result is None for p in pages):
                    setup_error = "tools/list failed"
                rtts.append(await _ping_rtt(client))
            if setup_error is None:
                pair = [clients[0], clients[0]] if instances == 1 else clients
                for response, window in await asyncio.gather(
                    *(_timed_call(c, spec, None if launch is None else launch.canary) for c in pair)
                ):
                    responses.append(response)
                    windows.append(window)
    except WireError as exc:
        wire_error = str(exc)
    finally:
        for client in clients:
            await client.close()
    try:
        after = observe(fixture, state_dir)
    except SetupError as exc:
        setup_error = setup_error or str(exc)
        after = None
    return ConcurrentRun(
        label, mode, setup_error, wire_error, tuple(responses), tuple(windows), tuple(rtts), before, after
    )


def _falsifiable_idempotence(ev: ToolEvidence) -> bool:
    c = ev.contract
    return c is not None and c.read_only is not Hint.TRUE and c.idempotent is FALSIFIABLE[Annotation.IDEMPOTENT]


def _concurrent_skip_reason(ev: ToolEvidence, once_ok: Sequence[Session], observe: bool = False) -> str | None:
    c = ev.contract
    if c is None:
        return "attempts not run: no contract"
    falsifiable = c.read_only is not Hint.TRUE and c.idempotent is FALSIFIABLE[Annotation.IDEMPOTENT]
    if not falsifiable and not observe:
        return "attempts not run: no falsifiable idempotence claim"
    if not once_ok:
        return "attempts not run: no completed single-call session to compare against"
    if ev.once_instability:
        return "attempts not run: single-call baseline differs across runs"
    return None


def _with_concurrent(
    fixture: Fixture,
    ev: ToolEvidence,
    runs: Sequence[ConcurrentRun],
    single_call_state: State,
    control_state: State | None,
    lifecycle_inert: bool,
) -> ToolEvidence:
    note = None
    usable = list(runs)
    if not lifecycle_inert:
        # Two instances start and stop twice as often as a single-call session;
        # if startup or shutdown writes state, the states are not comparable.
        usable = [r for r in runs if r.mode is not ConcurrencyMode.TWO_PROCESS]
        note = "two_process attempts excluded: server startup or shutdown writes state"
    setup_error = next((f"{r.label}: {r.setup_error}" for r in usable if r.setup_error), None)
    call_error = next((f"{r.label}: {r.wire_error}" for r in usable if r.wire_error), None)
    attempts: list[Attempt] = []
    records: list[ErrorRecord] = []
    for r in usable:
        if not r.ok:
            continue
        errs = _call_errors(r.responses)
        if errs:
            reference = control_state if control_state is not None else r.before
            changed = _state_changed(fixture, reference, r.after)
            records += [ErrorRecord(r.label, p, k, changed) for p, k in errs]
            if not changed:
                call_error = call_error or f"{r.label}: error response with no state change"
                continue
        wire = r.wire_overlap
        attempts.append(
            Attempt(
                label=r.label,
                mode=r.mode,
                overlap=r.overlap,
                wire_overlap_ms=None if wire is None else round(wire * 1000, 3),
                rtt_ms=tuple(round(t * 1000, 3) for t in r.rtts),
                observation=Observation("once", deltas(fixture, single_call_state, _end(r))),
            )
        )
    return dataclasses.replace(
        ev,
        concurrent=tuple(attempts),
        concurrent_ran=True,
        concurrent_note=note,
        two_process_skipped=not lifecycle_inert,
        concurrent_setup_error=setup_error,
        concurrent_call_error=call_error,
        concurrent_errors=tuple(records),
        concurrent_raw=tuple(runs),
    )


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
    differing = [a.wire_name for a in Annotation if len({c.hint(a) for c in seen}) > 1]
    if len(seen) < len(contract_sets):
        differing.append("(tool missing from some listings)")
    elif not differing and any(c != seen[0] for c in seen):
        differing.append("(annotation anomalies)")
    return seen[0], None, tuple(differing)

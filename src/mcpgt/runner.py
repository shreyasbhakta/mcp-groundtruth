"""Reset, seed, observe and drive a fixture server.

Build step 3 provides the pieces: reset and seeding, snapshots of every
surface, reset verification, and starting a server over the wire client.
Test orchestration (once vs twice, concurrent) arrives in step 4.
"""

from __future__ import annotations

import enum
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcpgt.fixture import Fixture, FixtureCoverage
from mcpgt.normalize import NormalizationError
from mcpgt.snapshot import Snapshot, SnapshotDelta, SnapshotError, diff
from mcpgt.wire import WireClient

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


class ResetStatus(enum.Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"  # two resets produced different normalized states
    SETUP_FAILED = "setup_failed"


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

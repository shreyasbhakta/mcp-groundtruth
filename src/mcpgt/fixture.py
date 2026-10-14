"""Fixture definitions: ``fixtures/<name>/fixture.toml``.

Parsing is strict. An unknown key is an error, not ignored, because a typo in
a classification or mask rule silently changes what gets compared, and that
is how false accusations get made.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcpgt.normalize import MaskRule, Normalizer, VolatileKind
from mcpgt.snapshot import (
    Classification,
    Coverage,
    FilesystemAdapter,
    JsonStoreAdapter,
    SnapshotAdapter,
    SQLiteAdapter,
)
from mcpgt.verdict import Behavior, Outcome, Reason, TestKind, parse_reason

FIXTURES_DIR = Path("fixtures")


class FixtureError(ValueError):
    """The fixture definition is invalid."""


# kind -> adapter; the adapter observes state_dir/<label>. Seeds: a SQL script
# (sqlite), a directory copied as the tree (fs), a JSON file copied (json).
ADAPTERS: dict[str, type[SQLiteAdapter] | type[FilesystemAdapter] | type[JsonStoreAdapter]] = {
    "sqlite": SQLiteAdapter,
    "fs": FilesystemAdapter,
    "json": JsonStoreAdapter,
}


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    kind: str
    label: str
    seed: str | None
    exclude: tuple[str, ...]
    classification: Classification
    normalizer: Normalizer

    @property
    def surface(self) -> str:
        return f"{self.kind}:{self.label}"

    def build(self, state_dir: Path) -> SnapshotAdapter:
        return ADAPTERS[self.kind](state_dir / self.label, self.label, self.exclude)


@dataclass(frozen=True, slots=True)
class Expectation:
    """Ground truth for one (tool, test). Optional fields are checked only if given."""

    outcome: Outcome
    reason: Reason | None
    business_changed: bool | None = None  # in every run's observation
    append_only_changed: bool | None = None
    behavior: Behavior | None = None
    secondary: tuple[Reason, ...] | None = None  # exact set of secondary reasons
    errored_with_side_effects: bool | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    arguments: Mapping[str, Any]
    expected: Mapping[TestKind, Expectation]


@dataclass(frozen=True, slots=True)
class FixtureCoverage:
    adapters: tuple[Coverage, ...]
    unobserved: tuple[str, ...]  # declared state locations no adapter observes

    @property
    def complete(self) -> bool:
        return not self.unobserved and all(c.complete for c in self.adapters)

    def to_json(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "unobserved_locations": list(self.unobserved),
            "adapters": [c.to_json() for c in self.adapters],
        }


@dataclass(frozen=True, slots=True)
class Fixture:
    name: str
    root: Path
    description: str
    command: tuple[str, ...]
    locations: tuple[str, ...]
    adapters: tuple[AdapterSpec, ...]
    tools: tuple[ToolSpec, ...]

    def server_command(self) -> list[str]:
        return [sys.executable if part == "{python}" else part for part in self.command]

    def build_adapters(self, state_dir: Path) -> list[SnapshotAdapter]:
        return [spec.build(state_dir) for spec in self.adapters]

    def adapter_spec(self, surface: str) -> AdapterSpec:
        for spec in self.adapters:
            if spec.surface == surface:
                return spec
        raise KeyError(surface)

    def coverage(self, state_dir: Path) -> FixtureCoverage:
        observed = {spec.surface for spec in self.adapters}
        return FixtureCoverage(
            adapters=tuple(a.coverage() for a in self.build_adapters(state_dir)),
            unobserved=tuple(loc for loc in self.locations if loc not in observed),
        )

    def source_files(self) -> list[Path]:
        return sorted(p for p in self.root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def resolve(name_or_path: str | Path) -> Path:
    path = Path(name_or_path)
    if (path / "fixture.toml").is_file():
        return path
    candidate = FIXTURES_DIR / str(name_or_path)
    if (candidate / "fixture.toml").is_file():
        return candidate
    raise FixtureError(f"no fixture at {path} or {candidate}")


def load(name_or_path: str | Path) -> Fixture:
    root = resolve(name_or_path).resolve()
    try:
        data = tomllib.loads((root / "fixture.toml").read_text())
    except tomllib.TOMLDecodeError as exc:
        raise FixtureError(f"{root}/fixture.toml: {exc}") from exc
    _keys(data, "fixture.toml", required={"fixture", "state", "adapters"}, optional={"tools"})

    meta = data["fixture"]
    _keys(meta, "[fixture]", required={"name", "command"}, optional={"description"})
    state = data["state"]
    _keys(state, "[state]", required={"locations"})

    adapters = tuple(_adapter(a, root) for a in _list(data["adapters"], "adapters"))
    surfaces = [a.surface for a in adapters]
    locations = tuple(_strs(state["locations"], "state.locations"))
    if len(set(surfaces)) != len(surfaces):
        raise FixtureError("two adapters observe the same surface")
    for surface in surfaces:
        if surface not in locations:
            raise FixtureError(f"adapter surface {surface!r} is not a declared state location")

    tools = tuple(_tool(t) for t in _list(data.get("tools", []), "tools"))
    if len({t.name for t in tools}) != len(tools):
        raise FixtureError("duplicate tool entries")

    return Fixture(
        name=_str(meta["name"], "fixture.name"),
        root=root,
        description=_str(meta.get("description", ""), "fixture.description"),
        command=tuple(_strs(meta["command"], "fixture.command")),
        locations=locations,
        adapters=adapters,
        tools=tools,
    )


def _adapter(data: Any, root: Path) -> AdapterSpec:
    where = "[[adapters]]"
    _keys(
        data,
        where,
        required={"kind", "label"},
        optional={"seed", "exclude", "business", "append_only", "mask"},
    )
    kind = _str(data["kind"], f"{where}.kind")
    if kind not in ADAPTERS:
        raise FixtureError(f"{where}: unsupported adapter kind {kind!r} (one of {', '.join(ADAPTERS)})")
    seed = data.get("seed")
    if seed is not None:
        seed_path = root / _str(seed, f"{where}.seed")
        if kind == "fs" and not seed_path.is_dir():
            raise FixtureError(f"{where}: seed directory {seed!r} not found")
        if kind != "fs" and not seed_path.is_file():
            raise FixtureError(f"{where}: seed file {seed!r} not found")
    try:
        classification = Classification(
            business=frozenset(_strs(data.get("business", []), f"{where}.business")),
            append_only=frozenset(_strs(data.get("append_only", []), f"{where}.append_only")),
        )
    except ValueError as exc:
        raise FixtureError(f"{where}: {exc}") from exc
    rules = []
    for rule in _list(data.get("mask", []), f"{where}.mask"):
        _keys(rule, f"{where}.mask", required={"region", "column", "kind"})
        try:
            volatile = VolatileKind(rule["kind"])
        except ValueError as exc:
            raise FixtureError(f"{where}.mask: unknown kind {rule['kind']!r}") from exc
        rules.append(MaskRule(_str(rule["region"], "region"), _str(rule["column"], "column"), volatile))
    return AdapterSpec(
        kind=kind,
        label=_str(data["label"], f"{where}.label"),
        seed=seed,
        exclude=tuple(_strs(data.get("exclude", []), f"{where}.exclude")),
        classification=classification,
        normalizer=Normalizer.of(rules),
    )


def _tool(data: Any) -> ToolSpec:
    where = "[[tools]]"
    _keys(data, where, required={"name"}, optional={"arguments", "expected"})
    expected: dict[TestKind, Expectation] = {}
    raw_expected = data.get("expected", {})
    if not isinstance(raw_expected, Mapping):
        raise FixtureError(f"{where}.expected must be a table")
    for test_name, spec in raw_expected.items():
        try:
            test = TestKind(test_name)
        except ValueError as exc:
            raise FixtureError(f"{where}.expected: unknown test {test_name!r}") from exc
        expected[test] = _expectation(spec, f"{where}.expected.{test_name}")
    arguments = data.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise FixtureError(f"{where}.arguments must be a table")
    return ToolSpec(name=_str(data["name"], f"{where}.name"), arguments=dict(arguments), expected=expected)


_FLAGS = ("business_changed", "append_only_changed", "errored_with_side_effects")


def _expectation(data: Any, where: str) -> Expectation:
    _keys(data, where, required={"verdict"}, optional={*_FLAGS, "behavior", "secondary"})
    # "PASS", "VIOLATION", "INCONCLUSIVE:<REASON>" or "NOT_APPLICABLE:<REASON>"
    outcome_text, _, reason_text = _str(data["verdict"], where).partition(":")
    try:
        outcome = Outcome(outcome_text)
        reason = parse_reason(reason_text) if reason_text else None
        secondary = (
            None
            if "secondary" not in data
            else tuple(parse_reason(r) for r in _strs(data["secondary"], f"{where}.secondary"))
        )
        behavior = None if "behavior" not in data else Behavior(_str(data["behavior"], where))
    except ValueError as exc:
        raise FixtureError(f"{where}: {exc}") from exc
    needs_reason = outcome in (Outcome.INCONCLUSIVE, Outcome.NOT_APPLICABLE)
    if needs_reason != (reason is not None):
        raise FixtureError(f"{where}: INCONCLUSIVE and NOT_APPLICABLE need a reason, and only they may have one")
    if (
        reason is not None
        and type(reason).__name__
        != {
            Outcome.INCONCLUSIVE: "InconclusiveReason",
            Outcome.NOT_APPLICABLE: "NotApplicableReason",
        }[outcome]
    ):
        raise FixtureError(f"{where}: {reason.value} is not a reason for {outcome.value}")
    flags = {}
    for key in _FLAGS:
        value = data.get(key)
        if value is not None and not isinstance(value, bool):
            raise FixtureError(f"{where}.{key} must be a boolean")
        flags[key] = value
    return Expectation(outcome=outcome, reason=reason, behavior=behavior, secondary=secondary, **flags)


def _keys(data: Any, where: str, required: AbstractSet[str], optional: AbstractSet[str] = frozenset()) -> None:
    if not isinstance(data, Mapping):
        raise FixtureError(f"{where} must be a table")
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing:
        raise FixtureError(f"{where}: missing {sorted(missing)}")
    if unknown:
        raise FixtureError(f"{where}: unknown keys {sorted(unknown)}")


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise FixtureError(f"{where} must be an array")
    return value


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise FixtureError(f"{where} must be a string, got {value!r}")
    return value


def _strs(value: Any, where: str) -> list[str]:
    return [_str(v, where) for v in _list(value, where)]

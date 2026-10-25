"""Fixture and audit-target definitions.

* ``fixtures/<name>/fixture.toml``: a server written for this project, with
  the verdicts it is expected to get. Fixtures validate the harness.
* ``targets/<name>/target.toml``: a third-party server, built from a pinned
  commit, run in a network-free sandbox. Targets are audited and must never
  carry expected verdicts.

Parsing is strict. An unknown key is an error, not ignored, because a typo in
a classification or mask rule silently changes what gets compared, and that
is how false accusations get made.
"""

from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcpgt.network import COVERAGE, POLICIES, NetworkReason, NetworkSpec
from mcpgt.normalize import MaskRule, Normalizer, VolatileKind
from mcpgt.snapshot import (
    Classification,
    Coverage,
    FilesystemAdapter,
    JsonStoreAdapter,
    SnapshotAdapter,
    SQLiteAdapter,
)
from mcpgt.sources import SourceSpec, build_commands
from mcpgt.verdict import Behavior, Outcome, Reason, TestKind, parse_reason

FIXTURES_DIR = Path("fixtures")
TARGETS_DIR = Path("targets")
_PLACEHOLDER = re.compile(r"\{(python|source|state|target|canary)\}")
# Marks a value `mcpgt init` could not know. A definition holding one does not load.
TODO_MARKER = "TODO(mcpgt)"


class FixtureError(ValueError):
    """The fixture definition is invalid."""


# kind -> adapter; the adapter observes state_dir/<label>. Seeds: a SQL script
# (sqlite), a directory copied as the tree (fs), a JSON or JSONL file copied
# (json).
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
    json_format: str = "document"  # json only: "document" or "jsonl"
    partition_by: str | None = None  # json only
    explode: Mapping[str, tuple[str, ...]] | None = None  # json only: list field -> parent id fields
    notes: str | None = None  # why the adapter models state this way; copied into reports
    modes: bool = False  # fs only: permission bits are state too
    empty_dirs: tuple[str, ...] = ()  # fs only: created empty after the seed is copied (git keeps no empty dirs)

    def to_json(self) -> dict[str, Any]:
        """The modelling decisions this adapter makes, as recorded in reports."""
        data: dict[str, Any] = {
            "surface": self.surface,
            "seed": self.seed,
            "business": sorted(self.classification.business),
            "append_only": sorted(self.classification.append_only),
            "masks": [{"region": r.region, "column": r.column, "kind": r.kind.value} for r in self.normalizer.rules],
            "exclude": list(self.exclude),
        }
        if self.kind == "json":
            data["format"] = self.json_format
            data["partition_by"] = self.partition_by
            data["explode"] = {k: list(v) for k, v in (self.explode or {}).items()}
        if self.kind == "fs":
            data["modes"] = self.modes
            data["empty_dirs"] = list(self.empty_dirs)
        data["notes"] = self.notes
        return data

    @property
    def surface(self) -> str:
        return f"{self.kind}:{self.label}"

    @property
    def depth(self) -> int:
        return len(Path(self.label).parts)

    def build(self, state_dir: Path, delegated: Mapping[str, str] | None = None) -> SnapshotAdapter:
        path = state_dir / self.label
        if self.kind == "fs":
            return FilesystemAdapter(path, self.label, self.exclude, delegated, modes=self.modes)
        if self.kind == "json":
            return JsonStoreAdapter(
                path,
                self.label,
                self.exclude,
                format=self.json_format,
                partition_by=self.partition_by,
                explode=self.explode,
            )
        return SQLiteAdapter(path, self.label, self.exclude)


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
    variant: str | None = None  # distinguishes several argument sets for one tool
    expected_network: tuple[Outcome, NetworkReason | None] | None = None  # fixtures only

    @property
    def key(self) -> str:
        """Unique within a definition: the tool name, plus the variant if any."""
        return self.name if self.variant is None else f"{self.name}[{self.variant}]"


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
    """A fixture or an audit target; ``is_target`` tells them apart."""

    name: str
    root: Path
    description: str
    command: tuple[str, ...]
    locations: tuple[str, ...]
    adapters: tuple[AdapterSpec, ...]
    tools: tuple[ToolSpec, ...]
    is_target: bool = False
    source: SourceSpec | None = None
    env: Mapping[str, str] | None = None
    node: str | None = None  # required Node major version, for build and run
    network: NetworkSpec | None = None  # observe network interactions against this policy

    @property
    def kind(self) -> str:
        return "target" if self.is_target else "fixture"

    @property
    def definition_file(self) -> Path:
        return self.root / ("target.toml" if self.is_target else "fixture.toml")

    @property
    def isolated(self) -> bool:
        """Targets always run without network, and so does anything whose network
        behavior is tested; other fixtures are this project's own code."""
        return self.is_target or self.network is not None

    def _expand(self, text: str, state_dir: Path | None, source: Path | None, canary: str | None = None) -> str:
        def sub(match: re.Match[str]) -> str:
            key = match.group(1)
            if key == "python":
                return sys.executable
            if key == "target":
                return str(self.root)
            if key == "canary":
                if canary is None:
                    raise FixtureError(f"{self.name}: {{canary}} needs [network] with canary = true")
                return canary
            value = state_dir if key == "state" else source
            if value is None:
                raise FixtureError(f"{self.name}: {{{key}}} is not available here")
            return str(value.resolve())

        return _PLACEHOLDER.sub(sub, text)

    def server_command(
        self, source: Path | None = None, state_dir: Path | None = None, canary: str | None = None
    ) -> list[str]:
        return [self._expand(part, state_dir, source, canary) for part in self.command]

    def server_env(self, state_dir: Path, source: Path | None = None, canary: str | None = None) -> dict[str, str]:
        return {k: self._expand(v, state_dir, source, canary) for k, v in (self.env or {}).items()}

    def build_adapters(self, state_dir: Path) -> list[SnapshotAdapter]:
        adapters = []
        for spec in self.adapters:
            delegated = None
            if spec.kind == "fs":
                delegated = {
                    _relative(other.label, spec.label): other.surface
                    for other in self.adapters
                    if other is not spec and _nested(other.label, spec.label)
                }
            adapters.append(spec.build(state_dir, delegated))
        return adapters

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
    """A fixture or target directory, by path or by name under fixtures/ or targets/."""
    path = Path(name_or_path)
    if (path / "fixture.toml").is_file() or (path / "target.toml").is_file():
        return path
    found = [
        d / str(name_or_path)
        for d, f in ((FIXTURES_DIR, "fixture.toml"), (TARGETS_DIR, "target.toml"))
        if (d / str(name_or_path) / f).is_file()
    ]
    if len(found) > 1:
        raise FixtureError(f"{name_or_path!r} is both a fixture and a target; pass a path")
    if not found:
        raise FixtureError(f"no fixture or target named {name_or_path!r}")
    return found[0]


def load(name_or_path: str | Path) -> Fixture:
    root = resolve(name_or_path).resolve()
    is_target = (root / "target.toml").is_file()
    if is_target and (root / "fixture.toml").is_file():
        raise FixtureError(f"{root}: has both fixture.toml and target.toml")
    file = root / ("target.toml" if is_target else "fixture.toml")
    section = "target" if is_target else "fixture"
    try:
        data = tomllib.loads(file.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise FixtureError(f"{file}: {exc}") from exc
    todos = unanswered_todos(data)
    if todos:
        listed = "\n".join(f"  {where}: {value}" for where, value in todos)
        raise FixtureError(
            f"{file}: {len(todos)} unanswered {TODO_MARKER} "
            f"{'placeholder' if len(todos) == 1 else 'placeholders'}; replace each with a real value:\n{listed}"
        )
    _keys(
        data,
        file.name,
        required={section, "state", "adapters"},
        optional={"tools", "network"} | ({"source"} if is_target else set()),
    )

    meta = data[section]
    _keys(
        meta,
        f"[{section}]",
        required={"name", "command"},
        optional={"description", "env"} | ({"node"} if is_target else set()),
    )
    node = None if "node" not in meta else _str(meta["node"], f"[{section}].node")
    if node is not None and not node.isdigit():
        raise FixtureError(f'[{section}].node must be a major version such as "24"')
    state = data["state"]
    _keys(state, "[state]", required={"locations"})

    adapters = tuple(_adapter(a, root) for a in _list(data["adapters"], "adapters"))
    surfaces = [a.surface for a in adapters]
    locations = tuple(_strs(state["locations"], "state.locations"))
    if len(set(surfaces)) != len(surfaces) or len({a.label for a in adapters}) != len(adapters):
        raise FixtureError("two adapters observe the same surface")
    for surface in surfaces:
        if surface not in locations:
            raise FixtureError(f"adapter surface {surface!r} is not a declared state location")
    for inner in adapters:
        for outer in adapters:
            if outer is not inner and _nested(inner.label, outer.label) and outer.kind != "fs":
                raise FixtureError(f"{inner.label!r} is inside {outer.label!r}, which is not a directory adapter")

    network = _network(data["network"]) if "network" in data else None
    tools = tuple(_tool(t, is_target, network is not None) for t in _list(data.get("tools", []), "tools"))
    if len({t.key for t in tools}) != len(tools):
        raise FixtureError("duplicate tool entries: give each argument set of one tool a distinct variant")

    env = meta.get("env", {})
    if not isinstance(env, Mapping) or not all(isinstance(v, str) for v in env.values()):
        raise FixtureError(f"[{section}.env] must map names to strings")
    source = _source(data["source"]) if "source" in data else None
    command = tuple(_strs(meta["command"], f"{section}.command"))
    if source is None and any("{source}" in part for part in [*command, *env.values()]):
        raise FixtureError("{source} is used but there is no [source] section")

    return Fixture(
        name=_str(meta["name"], f"{section}.name"),
        root=root,
        description=_str(meta.get("description", ""), f"{section}.description"),
        command=command,
        locations=locations,
        adapters=adapters,
        tools=tools,
        is_target=is_target,
        source=source,
        env=dict(env),
        node=node,
        network=network,
    )


def unanswered_todos(data: Any, where: str = "") -> list[tuple[str, str]]:
    """Every string value still starting with TODO_MARKER, with its key path."""
    if isinstance(data, str):
        return [(where, data)] if data.startswith(TODO_MARKER) else []
    if isinstance(data, Mapping):
        items: list[tuple[str, Any]] = [(f"{where}.{k}" if where else str(k), v) for k, v in data.items()]
    elif isinstance(data, list):
        # Name array entries that have one, so a TODO is easy to find: tools[3](delete_note).
        items = [
            (
                f"{where}[{i}]"
                + (f"({v['name']})" if isinstance(v, Mapping) and isinstance(v.get("name"), str) else ""),
                v,
            )
            for i, v in enumerate(data)
        ]
    else:
        return []
    return [todo for path, value in items for todo in unanswered_todos(value, path)]


def _nested(inner: str, outer: str) -> bool:
    return outer == "." or Path(inner).is_relative_to(outer)


def _relative(inner: str, outer: str) -> str:
    return Path(inner).as_posix() if outer == "." else Path(inner).relative_to(outer).as_posix()


def _source(data: Any) -> SourceSpec:
    where = "[source]"
    _keys(data, where, required={"repository", "commit", "build"}, optional={"package", "artifacts"})
    build = [_strs(c, f"{where}.build") for c in _list(data["build"], f"{where}.build")]
    try:
        return SourceSpec(
            repository=_str(data["repository"], f"{where}.repository"),
            commit=_str(data["commit"], f"{where}.commit"),
            build=build_commands(build),
            package=None if "package" not in data else _str(data["package"], f"{where}.package"),
            artifacts=tuple(_strs(data.get("artifacts", []), f"{where}.artifacts")),
        )
    except ValueError as exc:
        raise FixtureError(f"{where}: {exc}") from exc


def _adapter(data: Any, root: Path) -> AdapterSpec:
    where = "[[adapters]]"
    _keys(
        data,
        where,
        required={"kind", "label"},
        optional={
            "seed",
            "exclude",
            "business",
            "append_only",
            "mask",
            "format",
            "partition_by",
            "explode",
            "notes",
            "modes",
            "empty_dirs",
        },
    )
    kind = _str(data["kind"], f"{where}.kind")
    if kind not in ADAPTERS:
        raise FixtureError(f"{where}: unsupported adapter kind {kind!r} (one of {', '.join(ADAPTERS)})")
    if kind != "json" and {"format", "partition_by", "explode"} & data.keys():
        raise FixtureError(f"{where}: format, partition_by and explode apply to json adapters only")
    if kind != "fs" and {"modes", "empty_dirs"} & data.keys():
        raise FixtureError(f"{where}: modes and empty_dirs apply to fs adapters only")
    modes = data.get("modes", False)
    if not isinstance(modes, bool):
        raise FixtureError(f"{where}.modes must be a boolean")
    empty_dirs = tuple(_strs(data.get("empty_dirs", []), f"{where}.empty_dirs"))
    for d in empty_dirs:
        if not d or Path(d).is_absolute() or ".." in Path(d).parts:
            raise FixtureError(f"{where}.empty_dirs: {d!r} must be a relative path inside the adapter's directory")
    label = _str(data["label"], f"{where}.label")
    if Path(label).is_absolute() or ".." in Path(label).parts or label == "":
        raise FixtureError(f"{where}: label must be a relative path inside the state directory")
    if label == "." and kind != "fs":
        raise FixtureError(f"{where}: only an fs adapter may observe the whole state directory ('.')")
    json_format = _str(data.get("format", "document"), f"{where}.format")
    if json_format not in ("document", "jsonl"):
        raise FixtureError(f"{where}: format must be 'document' or 'jsonl'")
    explode_raw = data.get("explode", {})
    if not isinstance(explode_raw, Mapping):
        raise FixtureError(f"{where}.explode must map list fields to identifying fields")
    explode = {field: tuple(_strs(ids, f"{where}.explode.{field}")) for field, ids in explode_raw.items()}
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
        label=label,
        seed=seed,
        exclude=tuple(_strs(data.get("exclude", []), f"{where}.exclude")),
        classification=classification,
        normalizer=Normalizer.of(rules),
        json_format=json_format,
        partition_by=None if "partition_by" not in data else _str(data["partition_by"], f"{where}.partition_by"),
        explode=explode or None,
        notes=None if "notes" not in data else _str(data["notes"], f"{where}.notes"),
        modes=modes,
        empty_dirs=empty_dirs,
    )


def _network(data: Any) -> NetworkSpec:
    where = "[network]"
    _keys(data, where, required={"policy"}, optional={"allow", "capture", "canary", "coverage"})
    policy = _str(data["policy"], f"{where}.policy")
    if policy not in POLICIES:
        raise FixtureError(f"{where}.policy must be one of {', '.join(POLICIES)}")
    allow = tuple(_strs(data.get("allow", []), f"{where}.allow"))
    if allow and policy != "allowlist":
        raise FixtureError(f'{where}.allow applies only to policy = "allowlist"')
    if policy == "allowlist" and not allow:
        raise FixtureError(f'{where}: policy = "allowlist" needs at least one allow entry')
    coverage = _str(data.get("coverage", "complete"), f"{where}.coverage")
    if coverage not in COVERAGE:
        raise FixtureError(f"{where}.coverage must be one of {', '.join(COVERAGE)}")
    flags = {}
    for key, default in (("capture", False), ("canary", True)):
        value = data.get(key, default)
        if not isinstance(value, bool):
            raise FixtureError(f"{where}.{key} must be a boolean")
        flags[key] = value
    return NetworkSpec(policy=policy, allow=allow, coverage=coverage, **flags)


def expand_arguments(value: Any, canary: str | None) -> Any:
    """Tool arguments with every "{canary}" in their strings replaced."""
    if isinstance(value, str):
        if "{canary}" in value and canary is None:
            raise FixtureError("{canary} in tool arguments needs [network] with canary = true")
        return value.replace("{canary}", canary) if canary else value
    if isinstance(value, Mapping):
        return {k: expand_arguments(v, canary) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_arguments(v, canary) for v in value]
    return value


def _tool(data: Any, is_target: bool, network: bool = False) -> ToolSpec:
    where = "[[tools]]"
    if is_target and "expected" in data:
        raise FixtureError(
            f"{where} {data.get('name')!r}: audit targets must never carry expected verdicts; "
            "only fixtures have ground truth"
        )
    _keys(data, where, required={"name"}, optional={"arguments", "expected", "variant", "notes"})
    expected: dict[TestKind, Expectation] = {}
    raw_expected = data.get("expected", {})
    if not isinstance(raw_expected, Mapping):
        raise FixtureError(f"{where}.expected must be a table")
    expected_network = None
    for test_name, spec in raw_expected.items():
        if test_name == "network":
            if not network:
                raise FixtureError(f"{where}.expected.network needs a [network] section")
            expected_network = _network_expectation(spec, f"{where}.expected.network")
            continue
        try:
            test = TestKind(test_name)
        except ValueError as exc:
            raise FixtureError(f"{where}.expected: unknown test {test_name!r}") from exc
        expected[test] = _expectation(spec, f"{where}.expected.{test_name}")
    arguments = data.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise FixtureError(f"{where}.arguments must be a table")
    variant = None if "variant" not in data else _str(data["variant"], f"{where}.variant")
    if variant is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", variant):
        raise FixtureError(f"{where}.variant must be lowercase letters, digits and hyphens")
    return ToolSpec(
        name=_str(data["name"], f"{where}.name"),
        arguments=dict(arguments),
        expected=expected,
        variant=variant,
        expected_network=expected_network,
    )


def _network_expectation(data: Any, where: str) -> tuple[Outcome, NetworkReason | None]:
    _keys(data, where, required={"verdict"})
    outcome_text, _, reason_text = _str(data["verdict"], where).partition(":")
    try:
        outcome = Outcome(outcome_text)
        reason = NetworkReason(reason_text) if reason_text else None
    except ValueError as exc:
        raise FixtureError(f"{where}: {exc}") from exc
    if outcome is Outcome.NOT_APPLICABLE or (outcome is Outcome.INCONCLUSIVE) != (reason is not None):
        raise FixtureError(f"{where}: PASS, VIOLATION or INCONCLUSIVE:<reason>, with a reason only for INCONCLUSIVE")
    return outcome, reason


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

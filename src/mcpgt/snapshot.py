"""Observation surfaces: structured, comparable snapshots of server state.

A ``Snapshot`` is a set of named ``Region``s (for SQLite, one per table plus
schema and pragma regions). Each region holds its records as a canonically
sorted multiset, so two snapshots of the same state compare equal however the
rows were written, and duplicate rows are counted rather than collapsed.

Cells keep their storage class: ``("integer", 1)``, ``("real", 1.0)`` and
``("text", "1")`` are three different values, as they are to SQLite.

Each adapter also reports its ``Coverage``: what on its surface it can and
cannot see. Classifying regions as business or append-only state is the
fixture's job (``Classification``), never inferred here.
"""

from __future__ import annotations

import enum
import sqlite3
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Cell = tuple[str, Any]  # (storage class, value)
Record = tuple[Cell, ...]  # aligned with Region.columns


class SnapshotError(RuntimeError):
    """The surface could not be observed; the run cannot proceed (SETUP_FAILED)."""


class StateCategory(enum.Enum):
    BUSINESS = "business"
    APPEND_ONLY = "append_only"
    UNCLASSIFIED = "unclassified"  # observed, but the fixture did not say what it is


@dataclass(frozen=True, slots=True)
class Region:
    name: str
    columns: tuple[str, ...]
    records: tuple[Record, ...]

    def __post_init__(self) -> None:
        for record in self.records:
            if len(record) != len(self.columns):
                raise ValueError(
                    f"region {self.name!r}: record has {len(record)} cells, "
                    f"expected {len(self.columns)}"
                )
        object.__setattr__(self, "records", tuple(sorted(self.records)))

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": list(self.columns),
            "records": [_record_json(r) for r in self.records],
        }


@dataclass(frozen=True, slots=True)
class Snapshot:
    surface: str
    regions: tuple[Region, ...]

    def __post_init__(self) -> None:
        names = [r.name for r in self.regions]
        if len(set(names)) != len(names):
            raise ValueError(f"surface {self.surface!r}: duplicate region names")
        object.__setattr__(self, "regions", tuple(sorted(self.regions, key=lambda r: r.name)))

    def region(self, name: str) -> Region | None:
        for region in self.regions:
            if region.name == name:
                return region
        return None

    def to_json(self) -> dict[str, Any]:
        return {"surface": self.surface, "regions": [r.to_json() for r in self.regions]}


@dataclass(frozen=True, slots=True)
class Coverage:
    surface: str
    covered: tuple[str, ...]
    uncovered: tuple[str, ...]  # parts of this surface the adapter knows it cannot see

    @property
    def complete(self) -> bool:
        return not self.uncovered

    def to_json(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "covered": list(self.covered),
            "uncovered": list(self.uncovered),
            "complete": self.complete,
        }


class SnapshotAdapter(ABC):
    surface: str

    @abstractmethod
    def snapshot(self) -> Snapshot: ...

    @abstractmethod
    def coverage(self) -> Coverage: ...


# --- SQLite -----------------------------------------------------------------

SCHEMA_REGION = "@schema"
PRAGMA_REGION = "@pragma"
_PRAGMAS = ("user_version", "application_id")

_STORAGE_CLASS = {int: "integer", float: "real", str: "text", bytes: "blob", type(None): "null"}


class SQLiteAdapter(SnapshotAdapter):
    """Logical contents of one SQLite database file.

    Covers every table's rows, every schema object (tables, indexes, views,
    triggers) and the ``user_version`` / ``application_id`` pragmas. Physical
    layout (hidden rowids, page order, free pages) is deliberately not treated
    as state. Tables in ``exclude`` are reported as uncovered, so coverage of a
    surface with exclusions is never complete.
    """

    def __init__(self, path: Path, label: str, exclude: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self.surface = f"sqlite:{label}"
        self.exclude = frozenset(exclude)

    def coverage(self) -> Coverage:
        return Coverage(
            surface=self.surface,
            covered=(
                "rows of every table not excluded",
                "schema objects (tables, indexes, views, triggers)",
                "pragmas: " + ", ".join(_PRAGMAS),
            ),
            uncovered=tuple(f"table {name} (excluded)" for name in sorted(self.exclude)),
        )

    def snapshot(self) -> Snapshot:
        if not self.path.is_file():
            raise SnapshotError(f"{self.surface}: no database at {self.path}")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        except sqlite3.Error as exc:
            raise SnapshotError(f"{self.surface}: cannot open: {exc}") from exc
        try:
            conn.execute("BEGIN")  # one read transaction: all regions from the same state
            return Snapshot(surface=self.surface, regions=tuple(self._regions(conn)))
        except sqlite3.Error as exc:
            raise SnapshotError(f"{self.surface}: read failed: {exc}") from exc
        finally:
            conn.close()

    def _regions(self, conn: sqlite3.Connection) -> Iterable[Region]:
        schema_rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema"
        ).fetchall()
        yield Region(
            name=SCHEMA_REGION,
            columns=("type", "name", "tbl_name", "sql"),
            records=tuple(_record(row) for row in schema_rows),
        )
        yield Region(
            name=PRAGMA_REGION,
            columns=("pragma", "value"),
            records=tuple(
                _record((p, conn.execute(f"PRAGMA {p}").fetchone()[0])) for p in _PRAGMAS
            ),
        )
        for kind, name, _, _ in schema_rows:
            if kind != "table" or name in self.exclude:
                continue
            if name in (SCHEMA_REGION, PRAGMA_REGION):
                raise SnapshotError(f"{self.surface}: table name {name!r} is reserved")
            cursor = conn.execute(f"SELECT * FROM {_quote(name)}")
            yield Region(
                name=name,
                columns=tuple(d[0] for d in cursor.description),
                records=tuple(_record(row) for row in cursor),
            )


def _record(row: Iterable[Any]) -> Record:
    return tuple(_cell(v) for v in row)


def _cell(value: Any) -> Cell:
    storage = _STORAGE_CLASS[type(value)]
    return (storage, value.hex() if storage == "blob" else value)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _record_json(record: Record) -> list[list[Any]]:
    return [[storage, value] for storage, value in record]


# --- Classification and deltas ----------------------------------------------


@dataclass(frozen=True, slots=True)
class Classification:
    business: frozenset[str]
    append_only: frozenset[str]

    def __post_init__(self) -> None:
        overlap = self.business & self.append_only
        if overlap:
            raise ValueError(f"regions classified as both business and append-only: {sorted(overlap)}")

    def category(self, region: str) -> StateCategory:
        if region in self.business:
            return StateCategory.BUSINESS
        if region in self.append_only:
            return StateCategory.APPEND_ONLY
        return StateCategory.UNCLASSIFIED


@dataclass(frozen=True, slots=True)
class RegionDelta:
    region: str
    category: StateCategory
    columns_before: tuple[str, ...] | None  # None: region did not exist
    columns_after: tuple[str, ...] | None
    added: tuple[Record, ...]
    removed: tuple[Record, ...]  # pre-existing records no longer present unchanged

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed) or self.columns_before != self.columns_after

    def to_json(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "category": self.category.value,
            "columns_before": None if self.columns_before is None else list(self.columns_before),
            "columns_after": None if self.columns_after is None else list(self.columns_after),
            "added": [_record_json(r) for r in self.added],
            "removed": [_record_json(r) for r in self.removed],
        }


@dataclass(frozen=True, slots=True)
class SnapshotDelta:
    surface: str
    regions: tuple[RegionDelta, ...]  # changed regions only, sorted by name

    def in_category(self, category: StateCategory) -> tuple[RegionDelta, ...]:
        return tuple(d for d in self.regions if d.category is category)

    def changed(self, category: StateCategory) -> bool:
        return bool(self.in_category(category))

    def removed_any(self, category: StateCategory) -> bool:
        return any(d.removed for d in self.in_category(category))

    def to_json(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "by_category": {
                c.value: [d.to_json() for d in self.in_category(c)] for c in StateCategory
            },
        }


def diff(before: Snapshot, after: Snapshot, classification: Classification) -> SnapshotDelta:
    """Multiset difference per region. Overwrites show up as removed + added."""
    if before.surface != after.surface:
        raise ValueError(f"cannot diff {before.surface!r} against {after.surface!r}")
    names = sorted({r.name for r in before.regions} | {r.name for r in after.regions})
    deltas = []
    for name in names:
        b, a = before.region(name), after.region(name)
        b_counts = Counter(b.records if b else ())
        a_counts = Counter(a.records if a else ())
        delta = RegionDelta(
            region=name,
            category=classification.category(name),
            columns_before=b.columns if b else None,
            columns_after=a.columns if a else None,
            added=tuple(sorted((a_counts - b_counts).elements())),
            removed=tuple(sorted((b_counts - a_counts).elements())),
        )
        if delta.changed:
            deltas.append(delta)
    return SnapshotDelta(surface=before.surface, regions=tuple(deltas))

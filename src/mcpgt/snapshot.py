"""Observation surfaces: structured, comparable snapshots of server state.

A ``Snapshot`` is a set of named ``Region``s. Each region holds its records
as a canonically sorted multiset, so two snapshots of the same state compare
equal however they were written, and duplicate records are counted rather
than collapsed. Three adapters are provided:

* ``SQLiteAdapter``: one region per table, plus schema and pragma regions.
* ``FilesystemAdapter``: one region per top-level entry of a directory tree;
  records are (path, kind, content hash or link target).
* ``JsonStoreAdapter``: one region per top-level collection of a JSON
  document file; records are documents, one column per field.

Cells keep their type: ``("integer", 1)``, ``("real", 1.0)`` and
``("text", "1")`` are three different values, and a JSON field that is
missing (``("absent", None)``) differs from one that is ``null``.

Each adapter also reports its ``Coverage``: what on its surface it can and
cannot see. Classifying regions as business or append-only state is the
fixture's job (``Classification``), never inferred here.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import sqlite3
import stat
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
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
                raise ValueError(f"region {self.name!r}: record has {len(record)} cells, expected {len(self.columns)}")
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
        schema_rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema").fetchall()
        yield Region(
            name=SCHEMA_REGION,
            columns=("type", "name", "tbl_name", "sql"),
            records=tuple(_record(row) for row in schema_rows),
        )
        yield Region(
            name=PRAGMA_REGION,
            columns=("pragma", "value"),
            records=tuple(_record((p, conn.execute(f"PRAGMA {p}").fetchone()[0])) for p in _PRAGMAS),
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


# --- Filesystem tree ---------------------------------------------------------

FS_COLUMNS = ("path", "kind", "content")
FS_MODE_COLUMNS = (*FS_COLUMNS, "mode")


class FilesystemAdapter(SnapshotAdapter):
    """Every directory, regular file and symlink under one directory.

    Each region is one top-level entry of the tree (a file, or a directory
    and everything beneath it). Records are (relative path, kind, content):
    the SHA-256 of a file's bytes, a symlink's target, or empty for a
    directory. Symlinks are recorded, never followed. With ``modes``, each
    record also carries the permission bits (``0o644``; null for symlinks).
    Timestamps (modification, access, change, birth) and ownership are never
    treated as state: reading a file updates its access time, and rewriting
    identical content updates its modification time.

    ``delegated`` maps relative paths inside the tree to the surface of
    another adapter that observes them (e.g. a JSONL file inside the
    directory). They are skipped here without counting as uncovered.
    """

    def __init__(
        self,
        root: Path,
        label: str,
        exclude: Iterable[str] = (),
        delegated: Mapping[str, str] | None = None,
        modes: bool = False,
    ) -> None:
        self.root = Path(root)
        self.surface = f"fs:{label}"
        self.exclude = frozenset(exclude)
        self.delegated = dict(delegated or {})
        self.modes = modes

    def coverage(self) -> Coverage:
        return Coverage(
            surface=self.surface,
            covered=(
                "paths, kinds and content hashes of every entry not excluded",
                "symlink targets",
                *(("permission bits",) if self.modes else ()),
                *(f"{path}: observed by {surface}" for path, surface in sorted(self.delegated.items())),
            ),
            uncovered=tuple(f"entry {name} (excluded)" for name in sorted(self.exclude)),
        )

    def snapshot(self) -> Snapshot:
        if not self.root.is_dir():
            raise SnapshotError(f"{self.surface}: no directory at {self.root}")
        records: dict[str, list[Record]] = {}
        try:
            for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
                for name in dirnames + filenames:
                    path = Path(dirpath) / name
                    rel = path.relative_to(self.root).as_posix()
                    region = rel.split("/", 1)[0]
                    if region in self.exclude or rel in self.delegated:
                        continue
                    records.setdefault(region, []).append(_fs_record(path, rel, self.modes))
                dirnames[:] = [
                    d
                    for d in dirnames
                    if not (Path(dirpath) / d).is_symlink()
                    and (Path(dirpath) / d).relative_to(self.root).as_posix() not in self.delegated
                ]
        except OSError as exc:
            raise SnapshotError(f"{self.surface}: read failed: {exc}") from exc
        return Snapshot(
            surface=self.surface,
            regions=tuple(
                Region(name, FS_MODE_COLUMNS if self.modes else FS_COLUMNS, tuple(rs)) for name, rs in records.items()
            ),
        )


def _fs_record(path: Path, rel: str, modes: bool = False) -> Record:
    if path.is_symlink():
        kind, content = "symlink", os.readlink(path)
    elif path.is_dir():
        kind, content = "dir", ""
    else:
        kind, content = "file", hashlib.sha256(path.read_bytes()).hexdigest()
    record: Record = (("text", rel), ("text", kind), ("text", content))
    if modes:
        # A symlink's own permission bits carry no meaning on most systems.
        mode = None if kind == "symlink" else oct(stat.S_IMODE(path.lstat().st_mode))
        record += (("null", None) if mode is None else ("text", mode),)
    return record


# --- JSON document store --------------------------------------------------------

JSON_KEY_COLUMN = "_key"  # the id of a document in a mapping collection
JSON_VALUE_COLUMN = "_value"  # a document that is not an object, or a scalar collection


UNPARSED_REGION = "@unparsed"  # JSONL lines that are not valid JSON
OTHER_REGION = "@other"  # JSONL documents without the partition field


class JsonStoreAdapter(SnapshotAdapter):
    """A JSON document file, or a JSON Lines file.

    ``document`` format: the top-level object maps collection names to
    collections. A collection is a list of documents, an object mapping ids
    to documents, or a single value. Each collection is one region; a
    mapping collection adds a ``_key`` column.

    ``jsonl`` format: one JSON value per line, compared as a multiset, like
    table rows. ``partition_by`` names a field whose value selects each
    document's region (documents without it go to ``@other``); without it,
    every line is in one region named after the file. Lines that are not
    valid JSON are kept, verbatim, in ``@unparsed``: they are state too.

    In both formats each document is a record with one column per field
    (the union across the region's documents); a missing field is
    ``("absent", None)``, distinct from ``null``. Nested values compare as
    canonical JSON text, except fields named in ``explode``: each element of
    such a list becomes its own record in region ``<region>.<field>``,
    carrying the parent's identifying fields, and the list is removed from
    the parent record. Use it when appending to a nested list should count
    as an addition rather than an overwrite of the parent; it treats the
    list's order and the list itself as having no identity of their own.
    """

    def __init__(
        self,
        path: Path,
        label: str,
        exclude: Iterable[str] = (),
        *,
        format: str = "document",
        partition_by: str | None = None,
        explode: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        if format not in ("document", "jsonl"):
            raise ValueError(f"unknown JSON format {format!r}")
        self.path = Path(path)
        self.surface = f"json:{label}"
        self.exclude = frozenset(exclude)
        self.format = format
        self.partition_by = partition_by
        self.explode = {field: tuple(keys) for field, keys in (explode or {}).items()}

    def coverage(self) -> Coverage:
        unit = "line" if self.format == "jsonl" else "document of every collection"
        return Coverage(
            surface=self.surface,
            covered=(f"every {unit} not excluded",),
            uncovered=tuple(f"region {name} (excluded)" for name in sorted(self.exclude)),
        )

    def snapshot(self) -> Snapshot:
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise SnapshotError(f"{self.surface}: cannot read {self.path}: {exc}") from exc
        groups = self._jsonl_groups(text) if self.format == "jsonl" else self._document_groups(text)
        regions: list[Region] = []
        for name, docs in groups.items():
            regions += _json_regions(name, docs, explode=self.explode)
        return Snapshot(surface=self.surface, regions=tuple(r for r in regions if r.name not in self.exclude))

    def _document_groups(self, text: str) -> dict[str, Any]:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SnapshotError(f"{self.surface}: not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SnapshotError(f"{self.surface}: top level is not a JSON object")
        groups: dict[str, Any] = {}
        for name, collection in data.items():
            groups[name] = collection if isinstance(collection, list | dict) else [collection]
        return groups

    def _jsonl_groups(self, text: str) -> dict[str, Any]:
        default = Path(self.path).name
        groups: dict[str, list[Any]] = {}
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                groups.setdefault(UNPARSED_REGION, []).append({"_line": line})
                continue
            if self.partition_by is None:
                name = default
            elif isinstance(doc, dict) and isinstance(doc.get(self.partition_by), str):
                name = doc[self.partition_by]
            else:
                name = OTHER_REGION
            groups.setdefault(name, []).append(doc)
        return groups


def _json_regions(
    name: str, collection: list[Any] | dict[str, Any], *, explode: Mapping[str, tuple[str, ...]]
) -> list[Region]:
    keyed = isinstance(collection, dict)
    docs: list[tuple[str | None, Any]] = (
        list(collection.items()) if isinstance(collection, dict) else [(None, d) for d in collection]
    )
    children: dict[str, list[dict[str, Any]]] = {}
    parents: list[tuple[str | None, Any]] = []
    for key, doc in docs:
        if isinstance(doc, dict):
            doc = dict(doc)
            for field, identity in explode.items():
                if isinstance(doc.get(field), list):
                    ids = {k: doc.get(k) for k in identity}
                    children.setdefault(field, []).extend({**ids, "value": v} for v in doc.pop(field))
        parents.append((key, doc))
    regions = [_json_region(name, parents, keyed)]
    for field, rows in children.items():
        regions.append(_json_region(f"{name}.{field}", [(None, r) for r in rows], keyed=False))
    return regions


def _json_region(name: str, docs: list[tuple[str | None, Any]], keyed: bool) -> Region:
    fields = sorted({k for _, d in docs if isinstance(d, dict) for k in d})
    has_value = any(not isinstance(d, dict) for _, d in docs)
    columns = ([JSON_KEY_COLUMN] if keyed else []) + fields + ([JSON_VALUE_COLUMN] if has_value else [])
    records = []
    for key, doc in docs:
        cells: list[Cell] = [("text", key)] if keyed else []
        for f in fields:
            cells.append(_json_cell(doc[f]) if isinstance(doc, dict) and f in doc else ("absent", None))
        if has_value:
            cells.append(("absent", None) if isinstance(doc, dict) else _json_cell(doc))
        records.append(tuple(cells))
    return Region(name, tuple(columns), tuple(records))


def _json_cell(value: Any) -> Cell:
    if value is None:
        return ("null", None)
    if isinstance(value, bool):  # before int: bool is an int subclass
        return ("boolean", value)
    if isinstance(value, int):
        return ("integer", value)
    if isinstance(value, float):
        return ("real", value)
    if isinstance(value, str):
        return ("text", value)
    return ("json", json.dumps(value, sort_keys=True, separators=(",", ":")))


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

    def differing_columns(self) -> tuple[str, ...]:
        """Columns whose multiset of values differs between removed and added records.

        If the column set itself changed, every column of either side is named.
        """
        if self.columns_before != self.columns_after:
            return tuple(sorted(set(self.columns_before or ()) | set(self.columns_after or ())))
        return tuple(
            column
            for i, column in enumerate(self.columns_after or ())
            if Counter(r[i] for r in self.removed) != Counter(r[i] for r in self.added)
        )

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
            "by_category": {c.value: [d.to_json() for d in self.in_category(c)] for c in StateCategory},
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

"""Masking of declared volatile fields before snapshots are compared.

Rules come from fixture config and name an exact region and column. Nothing
is inferred: a column called ``created_at`` is compared verbatim unless a rule
says otherwise. A difference that survives normalization is a real one.

A masked column is masked wholesale, nulls included, so declaring a column
volatile also hides null/non-null transitions in it. That is the cost of
declaring it.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass

from mcpgt.snapshot import Region, Snapshot


class VolatileKind(enum.Enum):
    TIMESTAMP = "timestamp"
    UUID = "uuid"
    AUTOINCREMENT = "autoincrement"
    RANDOM_TOKEN = "random_token"


class NormalizationError(ValueError):
    """A rule does not fit the snapshot it was applied to: a config bug."""


@dataclass(frozen=True, slots=True)
class MaskRule:
    region: str
    column: str
    kind: VolatileKind


@dataclass(frozen=True, slots=True)
class Normalizer:
    rules: tuple[MaskRule, ...] = ()

    @classmethod
    def of(cls, rules: Iterable[MaskRule]) -> Normalizer:
        return cls(tuple(rules))

    def apply(self, snapshot: Snapshot) -> Snapshot:
        return Snapshot(
            surface=snapshot.surface,
            regions=tuple(self._apply_region(r) for r in snapshot.regions),
        )

    def _apply_region(self, region: Region) -> Region:
        masks: dict[int, VolatileKind] = {}
        for rule in self.rules:
            if rule.region != region.name:
                continue
            if rule.column not in region.columns:
                raise NormalizationError(
                    f"mask rule for {rule.region}.{rule.column}: region has no such "
                    f"column (columns: {', '.join(region.columns)})"
                )
            masks[region.columns.index(rule.column)] = rule.kind
        if not masks:
            return region
        return Region(
            name=region.name,
            columns=region.columns,
            records=tuple(
                tuple(("masked", masks[i].value) if i in masks else cell for i, cell in enumerate(record))
                for record in region.records
            ),
        )

    def validate_against(self, seeded: Snapshot) -> None:
        """Fail if any rule names a region or column absent from the seeded state.

        An unmatched rule masks nothing, and an unmasked volatile column differs
        on every run, which reproduces reliably and would read as a violation.
        """
        for rule in self.rules:
            region = seeded.region(rule.region)
            if region is None:
                raise NormalizationError(
                    f"mask rule for {rule.region}.{rule.column}: no region {rule.region!r} on {seeded.surface}"
                )
        self.apply(seeded)  # raises on unknown columns

    def equivalent(self, a: Snapshot, b: Snapshot) -> bool:
        return self.apply(a) == self.apply(b)

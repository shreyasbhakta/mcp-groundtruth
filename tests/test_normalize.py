import pytest

from mcpgt.normalize import MaskRule, NormalizationError, Normalizer, VolatileKind
from mcpgt.snapshot import Region, Snapshot


def snapshot(*rows, surface="s"):
    return Snapshot(
        surface=surface,
        regions=(
            Region(
                name="audit",
                columns=("id", "action", "at"),
                records=tuple((("integer", i), ("text", action), ("text", at)) for i, action, at in rows),
            ),
        ),
    )


AT = MaskRule("audit", "at", VolatileKind.TIMESTAMP)
ID = MaskRule("audit", "id", VolatileKind.AUTOINCREMENT)


def test_masked_column_differences_vanish():
    a = snapshot((1, "upsert", "2026-10-13T10:00:00"))
    b = snapshot((1, "upsert", "2026-10-13T10:00:05"))
    assert a != b
    assert Normalizer.of([AT]).equivalent(a, b)


def test_unmasked_differences_survive():
    a = snapshot((1, "upsert", "t1"))
    b = snapshot((1, "delete", "t2"))
    assert not Normalizer.of([AT]).equivalent(a, b)


def test_nothing_is_masked_without_a_rule():
    a = snapshot((1, "upsert", "t1"))
    b = snapshot((1, "upsert", "t2"))
    assert not Normalizer().equivalent(a, b)


def test_masking_keeps_multiplicity():
    # One audit row vs two: masking ids and timestamps must not merge them.
    one = snapshot((1, "upsert", "t1"))
    two = snapshot((1, "upsert", "t1"), (2, "upsert", "t2"))
    assert not Normalizer.of([AT, ID]).equivalent(one, two)


def test_masked_records_are_resorted_canonically():
    a = snapshot((1, "b", "t1"), (2, "a", "t2"))
    b = snapshot((7, "a", "t9"), (8, "b", "t8"))
    norm = Normalizer.of([AT, ID])
    assert norm.apply(a) == norm.apply(b)
    assert norm.apply(a).region("audit").records[0] == (
        ("masked", "autoincrement"),
        ("text", "a"),
        ("masked", "timestamp"),
    )


def test_rule_for_unknown_column_is_an_error():
    with pytest.raises(NormalizationError):
        Normalizer.of([MaskRule("audit", "created", VolatileKind.TIMESTAMP)]).apply(snapshot())


def test_rule_for_unknown_region_fails_validation_against_seed():
    norm = Normalizer.of([MaskRule("audit_log", "at", VolatileKind.TIMESTAMP)])
    norm.apply(snapshot())  # tolerated at apply time: regions may appear later
    with pytest.raises(NormalizationError, match="audit_log"):
        norm.validate_against(snapshot())


def test_valid_rules_pass_validation():
    Normalizer.of([AT, ID]).validate_against(snapshot((1, "x", "t")))

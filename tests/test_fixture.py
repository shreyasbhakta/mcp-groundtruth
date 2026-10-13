import shutil
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt.cli import main
from mcpgt.runner import ResetStatus, verify_reset
from mcpgt.snapshot import StateCategory
from mcpgt.verdict import InconclusiveReason, Outcome, TestKind

HONEST = Path(__file__).parent.parent / "fixtures" / "honest-idempotent"


@pytest.fixture
def copy(tmp_path):
    """A writable copy of honest-idempotent to break in specific ways."""
    root = tmp_path / "fx"
    shutil.copytree(HONEST, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def edit(root, old, new, file="fixture.toml"):
    path = root / file
    text = path.read_text()
    assert old in text, f"{old!r} not in {file}"
    path.write_text(text.replace(old, new))


# --- Loading ----------------------------------------------------------------


def test_honest_idempotent_loads():
    fx = fixtures.load(HONEST)
    assert fx.name == "honest-idempotent"
    (adapter,) = fx.adapters
    assert adapter.surface == "sqlite:state.db"
    assert adapter.classification.category("items") is StateCategory.BUSINESS
    assert adapter.classification.category("audit") is StateCategory.APPEND_ONLY
    (tool,) = fx.tools
    expected = tool.expected[TestKind.SEQUENTIAL_IDEMPOTENCE]
    assert expected.outcome is Outcome.PASS
    assert (expected.business_changed, expected.append_only_changed) == (False, True)


@pytest.mark.parametrize(
    "old, new, message",
    [
        ('business = ["items"]', 'busines = ["items"]', "unknown keys"),
        ('kind = "timestamp"', 'kind = "time"', "unknown kind"),
        ('locations = ["sqlite:state.db"]', 'locations = ["sqlite:other.db"]', "not a declared state location"),
        ('verdict = "PASS"', 'verdict = "INCONCLUSIVE"', "needs a reason"),
        ('verdict = "PASS"', 'verdict = "PASS:NO_DECLARATION"', "only it may have one"),
        ('verdict = "PASS"', 'verdict = "INCONCLUSIVE:MAYBE"', "bad verdict"),
        ("[tools.expected.sequential_idempotence]", "[tools.expected.sequential]", "unknown test"),
        ("business_changed = false", 'business_changed = "no"', "must be a boolean"),
        ('seed = "seed.sql"', 'seed = "missing.sql"', "not found"),
        ('append_only = ["audit"]', 'append_only = ["audit", "items"]', "both business and append-only"),
    ],
)
def test_invalid_definitions_are_rejected(copy, old, new, message):
    edit(copy, old, new)
    with pytest.raises(fixtures.FixtureError, match=message):
        fixtures.load(copy)


def test_expected_inconclusive_with_reason_parses(copy):
    edit(copy, 'verdict = "PASS"', 'verdict = "INCONCLUSIVE:UNCLASSIFIED_STATE"')
    expected = fixtures.load(copy).tools[0].expected[TestKind.SEQUENTIAL_IDEMPOTENCE]
    assert expected.reason is InconclusiveReason.UNCLASSIFIED_STATE


def test_undeclared_location_without_adapter_makes_coverage_partial(copy, tmp_path):
    edit(copy, 'locations = ["sqlite:state.db"]', 'locations = ["sqlite:state.db", "memory:process"]')
    cov = fixtures.load(copy).coverage(tmp_path / "state")
    assert cov.complete is False
    assert cov.unobserved == ("memory:process",)


def test_excluded_table_makes_coverage_partial(copy, tmp_path):
    edit(copy, 'seed = "seed.sql"', 'seed = "seed.sql"\nexclude = ["audit"]')
    assert fixtures.load(copy).coverage(tmp_path / "state").complete is False


# --- Reset verification -----------------------------------------------------


def test_honest_idempotent_reset_verifies(tmp_path):
    result = verify_reset(fixtures.load(HONEST), tmp_path / "state")
    assert result.status is ResetStatus.VERIFIED
    assert result.coverage.complete
    assert result.deltas == ()


def test_nondeterministic_seed_is_unverified(copy, tmp_path):
    edit(copy, "('b', '2');", "('b', '2'), ('r', hex(randomblob(8)));", file="seed.sql")
    result = verify_reset(fixtures.load(copy), tmp_path / "state")
    assert result.status is ResetStatus.UNVERIFIED
    (delta,) = result.deltas
    (items,) = delta.regions
    assert items.region == "items" and len(items.added) == len(items.removed) == 1


def test_volatile_seed_values_verify_once_masked(copy, tmp_path):
    seeded_audit = (
        "INSERT INTO audit (action, key, at, request_id) "
        "VALUES ('seed', 'a', strftime('%f', 'now') || random(), hex(randomblob(16)));"
    )
    (copy / "seed.sql").write_text((copy / "seed.sql").read_text() + seeded_audit + "\n")
    assert verify_reset(fixtures.load(copy), tmp_path / "s1").status is ResetStatus.VERIFIED
    edit(copy, '    { region = "audit", column = "request_id", kind = "uuid" },\n', "")
    assert verify_reset(fixtures.load(copy), tmp_path / "s2").status is ResetStatus.UNVERIFIED


def test_mask_rule_typo_fails_setup(copy, tmp_path):
    edit(copy, 'region = "audit", column = "at"', 'region = "audit_log", column = "at"')
    result = verify_reset(fixtures.load(copy), tmp_path / "state")
    assert result.status is ResetStatus.SETUP_FAILED
    assert "audit_log" in result.detail


def test_broken_seed_fails_setup(copy, tmp_path):
    (copy / "seed.sql").write_text("CREATE TABLE items (;\n")
    result = verify_reset(fixtures.load(copy), tmp_path / "state")
    assert result.status is ResetStatus.SETUP_FAILED


def test_cli_verify_reset(capsys, copy):
    assert main(["verify-reset", str(HONEST)]) == 0
    assert "reset verified" in capsys.readouterr().out
    edit(copy, "('b', '2');", "('b', '2'), ('r', hex(randomblob(8)));", file="seed.sql")
    assert main(["verify-reset", str(copy)]) == 1
    assert main(["verify-reset", "no-such-fixture"]) == 2

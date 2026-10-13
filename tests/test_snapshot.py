import hashlib
import json
import sqlite3

import pytest

from mcpgt.snapshot import (
    PRAGMA_REGION,
    SCHEMA_REGION,
    Classification,
    Region,
    SnapshotError,
    SQLiteAdapter,
    StateCategory,
    diff,
)

SCHEMA = """
CREATE TABLE items (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE audit (id INTEGER PRIMARY KEY, action TEXT, at TEXT);
"""


def make_db(path, rows=(("a", "1"), ("b", "2")), script=SCHEMA):
    conn = sqlite3.connect(path)
    conn.executescript(script)
    if rows:
        conn.executemany("INSERT INTO items VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def execute(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path):
    return make_db(tmp_path / "state.db")


def snap(path, **kw):
    return SQLiteAdapter(path, label="state.db", **kw).snapshot()


CLASSES = Classification(business=frozenset({"items"}), append_only=frozenset({"audit"}))


# --- Snapshots are canonical and typed --------------------------------------


def test_snapshot_independent_of_insertion_order(tmp_path):
    a = snap(make_db(tmp_path / "a.db", rows=[("a", "1"), ("b", "2")]))
    b = snap(make_db(tmp_path / "b.db", rows=[("b", "2"), ("a", "1")]))
    assert a == b


def test_snapshot_regions(db):
    s = snap(db)
    assert [r.name for r in s.regions] == [PRAGMA_REGION, SCHEMA_REGION, "audit", "items"]
    items = s.region("items")
    assert items.columns == ("key", "value")
    assert items.records == ((("text", "a"), ("text", "1")), (("text", "b"), ("text", "2")))


def test_storage_class_is_part_of_the_value(tmp_path):
    db = make_db(tmp_path / "t.db", rows=(), script="CREATE TABLE items (v);")
    snaps = []
    for value in (1, 1.0, "1", b"\x01", None):
        execute(db, "DELETE FROM items")
        execute(db, "INSERT INTO items VALUES (?)", (value,))
        snaps.append(snap(db))
    assert len({s for s in snaps}) == 5
    assert snaps[3].region("items").records == ((("blob", "01"),),)


def test_duplicate_rows_are_counted(tmp_path):
    db = make_db(tmp_path / "t.db", rows=(), script="CREATE TABLE items (v);")
    execute(db, "INSERT INTO items VALUES ('x')")
    once = snap(db)
    execute(db, "INSERT INTO items VALUES ('x')")
    assert snap(db) != once
    assert len(snap(db).region("items").records) == 2


def test_schema_and_pragmas_are_state(db):
    base = snap(db)
    execute(db, "CREATE INDEX items_value ON items(value)")
    with_index = snap(db)
    assert with_index != base
    assert with_index.region("items") == base.region("items")
    execute(db, "PRAGMA user_version = 7")
    assert (("text", "user_version"), ("integer", 7)) in snap(db).region(PRAGMA_REGION).records


def test_snapshot_does_not_touch_the_database(db):
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    siblings = set(db.parent.iterdir())
    snap(db)
    snap(db)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert set(db.parent.iterdir()) == siblings


def test_missing_database_is_a_snapshot_error(tmp_path):
    with pytest.raises(SnapshotError):
        snap(tmp_path / "nope.db")
    assert not (tmp_path / "nope.db").exists()


def test_snapshot_json_is_plain_data(db):
    data = json.loads(json.dumps(snap(db).to_json()))
    assert data["surface"] == "sqlite:state.db"


# --- Coverage ---------------------------------------------------------------


def test_full_coverage_by_default(db):
    cov = SQLiteAdapter(db, label="state.db").coverage()
    assert cov.complete
    assert cov.uncovered == ()


def test_excluded_tables_are_uncovered_and_unobserved(db):
    adapter = SQLiteAdapter(db, label="state.db", exclude=["audit"])
    assert adapter.coverage().complete is False
    assert adapter.coverage().uncovered == ("table audit (excluded)",)
    assert adapter.snapshot().region("audit") is None


# --- Region invariants ------------------------------------------------------


def test_region_rejects_ragged_records():
    with pytest.raises(ValueError):
        Region(name="r", columns=("a", "b"), records=((("text", "x"),),))


def test_classification_must_be_disjoint():
    with pytest.raises(ValueError):
        Classification(business=frozenset({"t"}), append_only=frozenset({"t"}))


def test_unknown_regions_are_unclassified():
    assert CLASSES.category("items") is StateCategory.BUSINESS
    assert CLASSES.category("audit") is StateCategory.APPEND_ONLY
    assert CLASSES.category("cache") is StateCategory.UNCLASSIFIED


# --- Deltas -----------------------------------------------------------------


def test_no_change_no_delta(db):
    d = diff(snap(db), snap(db), CLASSES)
    assert d.regions == ()


def test_additive_change(db):
    s0 = snap(db)
    execute(db, "INSERT INTO items VALUES ('c', '3')")
    d = diff(s0, snap(db), CLASSES)
    assert d.changed(StateCategory.BUSINESS)
    assert not d.removed_any(StateCategory.BUSINESS)
    (items,) = d.in_category(StateCategory.BUSINESS)
    assert items.added == ((("text", "c"), ("text", "3")),)


def test_overwrite_is_removal_plus_addition(db):
    s0 = snap(db)
    execute(db, "UPDATE items SET value = '9' WHERE key = 'a'")
    (items,) = diff(s0, snap(db), CLASSES).regions
    assert items.removed == ((("text", "a"), ("text", "1")),)
    assert items.added == ((("text", "a"), ("text", "9")),)


def test_delete_is_removal(db):
    s0 = snap(db)
    execute(db, "DELETE FROM items WHERE key = 'b'")
    d = diff(s0, snap(db), CLASSES)
    assert d.removed_any(StateCategory.BUSINESS)


def test_categories_reported_separately(db):
    s0 = snap(db)
    execute(db, "INSERT INTO audit (action, at) VALUES ('upsert', 'now')")
    d = diff(s0, snap(db), CLASSES)
    assert d.changed(StateCategory.APPEND_ONLY)
    assert not d.changed(StateCategory.BUSINESS)
    assert set(d.to_json()["by_category"]) == {"business", "append_only", "unclassified"}


def test_new_table_is_an_unclassified_change(db):
    s0 = snap(db)
    execute(db, "CREATE TABLE cache (k)")
    d = diff(s0, snap(db), CLASSES)
    unclassified = {r.region for r in d.in_category(StateCategory.UNCLASSIFIED)}
    assert unclassified == {"cache", SCHEMA_REGION}
    (cache,) = [r for r in d.regions if r.region == "cache"]
    assert cache.columns_before is None and cache.columns_after == ("k",)


def test_dropping_an_empty_table_is_still_a_removal(db):
    s0 = snap(db)
    execute(db, "DROP TABLE audit")
    d = diff(s0, snap(db), CLASSES)
    (audit,) = d.in_category(StateCategory.APPEND_ONLY)
    assert audit.columns_after is None
    (schema,) = [r for r in d.regions if r.region == SCHEMA_REGION]
    assert schema.removed


def test_diff_requires_same_surface(db, tmp_path):
    other = SQLiteAdapter(db, label="other.db").snapshot()
    with pytest.raises(ValueError):
        diff(snap(db), other, CLASSES)


def test_diff_counts_identical_rows(tmp_path):
    # After masking autoincrement ids, a non-idempotent insert produces rows
    # identical to existing ones; the delta must still show each one.
    db = make_db(tmp_path / "t.db", rows=(), script="CREATE TABLE items (v);")
    execute(db, "INSERT INTO items VALUES ('x')")
    s1 = snap(db)
    execute(db, "INSERT INTO items VALUES ('x')")
    execute(db, "INSERT INTO items VALUES ('x')")
    (items,) = diff(s1, snap(db), Classification(frozenset({"items"}), frozenset())).regions
    assert items.added == ((("text", "x"),), (("text", "x"),))
    assert items.removed == ()

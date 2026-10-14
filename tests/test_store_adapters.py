"""Filesystem-tree and JSON document store adapters, and seeding them."""

import json
import os
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt.normalize import MaskRule, Normalizer, VolatileKind
from mcpgt.runner import ResetStatus, reset, verify_reset
from mcpgt.snapshot import Classification, FilesystemAdapter, JsonStoreAdapter, SnapshotError, StateCategory, diff

# --- Filesystem tree ----------------------------------------------------------


def tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def fs(root):
    return FilesystemAdapter(root, "files").snapshot()


def test_tree_regions_are_top_level_entries(tmp_path):
    root = tree(tmp_path / "t", {"docs/a.txt": "A", "docs/sub/b.txt": "B", "log.txt": "L"})
    snap = fs(root)
    assert [r.name for r in snap.regions] == ["docs", "log.txt"]
    paths = [rec[0][1] for rec in snap.region("docs").records]
    assert paths == ["docs", "docs/a.txt", "docs/sub", "docs/sub/b.txt"]


def test_tree_snapshot_ignores_creation_order_and_mtime(tmp_path):
    a = tree(tmp_path / "a", {"x/1": "one", "x/2": "two"})
    b = tree(tmp_path / "b", {"x/2": "two", "x/1": "one"})
    os.utime(b / "x" / "1", (0, 0))
    assert fs(a) == fs(b)


def test_tree_detects_content_change_rename_and_delete(tmp_path):
    root = tree(tmp_path / "t", {"docs/a.txt": "A"})
    before = fs(root)
    classes = Classification(business=frozenset({"docs"}), append_only=frozenset())
    (root / "docs" / "a.txt").write_text("changed")
    (changed,) = diff(before, fs(root), classes).regions
    assert changed.differing_columns() == ("content",)
    (root / "docs" / "a.txt").rename(root / "docs" / "b.txt")
    assert diff(before, fs(root), classes).removed_any(StateCategory.BUSINESS)


def test_symlinks_are_recorded_not_followed(tmp_path):
    root = tree(tmp_path / "t", {"real/f": "x"})
    (root / "link").symlink_to("real")
    region = fs(root).region("link")
    assert region.records == ((("text", "link"), ("text", "symlink"), ("text", "real")),)


def test_excluded_tree_entries_are_uncovered(tmp_path):
    root = tree(tmp_path / "t", {"docs/a": "A", "cache/c": "C"})
    adapter = FilesystemAdapter(root, "files", exclude=["cache"])
    assert adapter.snapshot().region("cache") is None
    assert not adapter.coverage().complete


def test_missing_tree_is_a_snapshot_error(tmp_path):
    with pytest.raises(SnapshotError):
        fs(tmp_path / "nope")


# --- JSON document store ------------------------------------------------------


def store(tmp_path, data, name="store.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return JsonStoreAdapter(path, "store").snapshot()


def test_list_collection_columns_are_the_union_of_fields(tmp_path):
    snap = store(tmp_path, {"users": [{"id": 1, "name": "a"}, {"id": 2, "email": "b@x"}]})
    users = snap.region("users")
    assert users.columns == ("email", "id", "name")
    assert (("absent", None), ("integer", 1), ("text", "a")) in users.records


def test_absent_field_differs_from_null(tmp_path):
    absent = store(tmp_path, {"c": [{"a": 1}]}, "a.json")
    null = store(tmp_path, {"c": [{"a": 1, "b": None}]}, "b.json")
    assert absent != null


@pytest.mark.parametrize("left, right", [(1, 1.0), (1, True), (1, "1"), (0, False), ([1], "[1]")])
def test_json_types_are_part_of_the_value(tmp_path, left, right):
    assert store(tmp_path, {"c": [{"v": left}]}, "l.json") != store(tmp_path, {"c": [{"v": right}]}, "r.json")


def test_mapping_collection_keeps_document_ids(tmp_path):
    snap = store(tmp_path, {"orders": {"o1": {"total": 5}, "o2": {"total": 7}}})
    orders = snap.region("orders")
    assert orders.columns == ("_key", "total")
    assert orders.records == ((("text", "o1"), ("integer", 5)), (("text", "o2"), ("integer", 7)))


def test_nested_values_compare_as_canonical_json(tmp_path):
    a = store(tmp_path, {"c": [{"v": {"y": 1, "x": 2}}]}, "a.json")
    b = store(tmp_path, {"c": [{"v": {"x": 2, "y": 1}}]}, "b.json")
    assert a == b


def test_json_fields_can_be_masked(tmp_path):
    a = store(tmp_path, {"events": [{"kind": "x", "at": "t1"}]}, "a.json")
    b = store(tmp_path, {"events": [{"kind": "x", "at": "t2"}]}, "b.json")
    norm = Normalizer.of([MaskRule("events", "at", VolatileKind.TIMESTAMP)])
    assert a != b and norm.equivalent(a, b)


@pytest.mark.parametrize("content", ["not json", "[1, 2]"])
def test_unreadable_store_is_a_snapshot_error(tmp_path, content):
    (tmp_path / "s.json").write_text(content)
    with pytest.raises(SnapshotError):
        JsonStoreAdapter(tmp_path / "s.json", "s").snapshot()


# --- Seeding and reset verification for non-SQLite surfaces ---------------------


def make_fixture(root: Path, *, fs_seed: bool = True) -> Path:
    tree(root / "seed_tree", {"docs/readme.txt": "hello"})
    (root / "seed_store.json").write_text(json.dumps({"items": [{"k": "a", "v": 1}]}))
    seed_line = 'seed = "seed_tree"\n' if fs_seed else ""
    (root / "fixture.toml").write_text(
        f"""
[fixture]
name = "stores"
command = ["{{python}}", "-c", "pass"]

[state]
locations = ["fs:files", "json:store.json"]

[[adapters]]
kind = "fs"
label = "files"
{seed_line}business = ["docs"]

[[adapters]]
kind = "json"
label = "store.json"
seed = "seed_store.json"
business = ["items"]
"""
    )
    return root


def test_fs_and_json_surfaces_reset_and_verify(tmp_path):
    fx = fixtures.load(make_fixture(tmp_path / "fx"))
    result = verify_reset(fx, tmp_path / "state")
    assert result.status is ResetStatus.VERIFIED
    assert result.coverage.complete
    reset(fx, tmp_path / "state")
    assert (tmp_path / "state" / "files" / "docs" / "readme.txt").read_text() == "hello"


def test_unseeded_fs_surface_starts_empty(tmp_path):
    fx = fixtures.load(make_fixture(tmp_path / "fx", fs_seed=False))
    reset(fx, tmp_path / "state")
    assert list((tmp_path / "state" / "files").iterdir()) == []


def test_fs_seed_must_be_a_directory(tmp_path):
    root = make_fixture(tmp_path / "fx")
    text = (root / "fixture.toml").read_text().replace('seed = "seed_tree"', 'seed = "seed_store.json"')
    (root / "fixture.toml").write_text(text)
    with pytest.raises(fixtures.FixtureError, match="seed directory"):
        fixtures.load(root)


# --- JSON Lines -----------------------------------------------------------------

ENTITY_A = {"type": "entity", "name": "Alice", "entityType": "person", "observations": ["a1", "a2"]}
ENTITY_B = {"type": "entity", "name": "Bob", "entityType": "person", "observations": []}
REL = {"type": "relation", "from": "Alice", "to": "Bob", "relationType": "knows"}


def jsonl(tmp_path, lines, name="g.jsonl", **kw):
    path = tmp_path / name
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines))
    return JsonStoreAdapter(path, name, format="jsonl", **kw).snapshot()


def test_jsonl_is_a_multiset_of_lines(tmp_path):
    a = jsonl(tmp_path, [ENTITY_A, REL], "a.jsonl")
    b = jsonl(tmp_path, [REL, ENTITY_A], "b.jsonl")
    assert [r.name for r in a.regions] == ["a.jsonl"]
    assert a.region("a.jsonl").records == b.region("b.jsonl").records
    twice = jsonl(tmp_path, [REL, REL], "c.jsonl")
    assert len(twice.region("c.jsonl").records) == 2


def test_jsonl_partition_by_field(tmp_path):
    snap = jsonl(tmp_path, [ENTITY_A, REL, {"no": "type"}, ""], partition_by="type")
    assert [r.name for r in snap.regions] == ["@other", "entity", "relation"]


def test_jsonl_keeps_unparsable_lines_as_state(tmp_path):
    snap = jsonl(tmp_path, [ENTITY_A, "{not json"], partition_by="type")
    assert snap.region("@unparsed").records == ((("text", "{not json"),),)


def test_explode_makes_list_appends_additive(tmp_path):
    kw = {"partition_by": "type", "explode": {"observations": ("name",)}}
    before = jsonl(tmp_path, [ENTITY_A], "a.jsonl", **kw)
    grown = {**ENTITY_A, "observations": ["a1", "a2", "a3"]}
    after = jsonl(tmp_path, [grown], "a.jsonl", **kw)
    assert before.region("entity").columns == ("entityType", "name", "type")  # list moved out
    classes = Classification(business=frozenset({"entity", "entity.observations"}), append_only=frozenset())
    delta = diff(before, after, classes)
    (obs,) = delta.regions
    assert obs.region == "entity.observations"
    assert obs.added == ((("text", "Alice"), ("text", "a3")),) and obs.removed == ()


def test_explode_records_list_removals(tmp_path):
    kw = {"partition_by": "type", "explode": {"observations": ("name",)}}
    before = jsonl(tmp_path, [ENTITY_A], "a.jsonl", **kw)
    after = jsonl(tmp_path, [{**ENTITY_A, "observations": ["a2"]}], "a.jsonl", **kw)
    classes = Classification(business=frozenset({"entity", "entity.observations"}), append_only=frozenset())
    assert diff(before, after, classes).removed_any(StateCategory.BUSINESS)


def test_without_explode_a_list_change_overwrites_the_parent(tmp_path):
    before = jsonl(tmp_path, [ENTITY_A], "a.jsonl", partition_by="type")
    after = jsonl(tmp_path, [{**ENTITY_A, "observations": ["a1", "a2", "a3"]}], "a.jsonl", partition_by="type")
    classes = Classification(business=frozenset({"entity"}), append_only=frozenset())
    (entity,) = diff(before, after, classes).regions
    assert entity.removed and entity.added


# --- Delegation from a directory adapter ------------------------------------------


def test_delegated_paths_are_skipped_but_still_covered(tmp_path):
    root = tree(tmp_path / "t", {"memory.jsonl": "{}", "memory.jsonl.lock": "x"})
    adapter = FilesystemAdapter(root, ".", delegated={"memory.jsonl": "json:memory.jsonl"})
    snap = adapter.snapshot()
    assert [r.name for r in snap.regions] == ["memory.jsonl.lock"]
    assert adapter.coverage().complete
    assert "memory.jsonl: observed by json:memory.jsonl" in adapter.coverage().covered

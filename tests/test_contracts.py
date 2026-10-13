import dataclasses
import itertools
import json

import pytest

from mcpgt.contracts import (
    Annotation,
    AnnotationsBlock,
    AnomalyKind,
    ContractParseError,
    DeclaredContract,
    Hint,
    ParseAnomaly,
    parse_listing,
    parse_tool,
    parse_tools,
)

WIRE_NAMES = [a.wire_name for a in Annotation]
_MISSING = object()


def tool(annotations=_MISSING, **extra):
    t = {"name": "t", "inputSchema": {"type": "object"}, **extra}
    if annotations is not _MISSING:
        t["annotations"] = annotations
    return t


# --- Hint cannot be coerced -------------------------------------------------


@pytest.mark.parametrize("hint", list(Hint))
def test_hint_has_no_truth_value(hint):
    with pytest.raises(TypeError):
        bool(hint)
    with pytest.raises(TypeError):
        if hint:
            pass
    with pytest.raises(TypeError):
        not hint  # noqa: B015
    with pytest.raises(TypeError):
        hint or Hint.FALSE  # noqa: B018


@pytest.mark.parametrize("hint", list(Hint))
@pytest.mark.parametrize("other", [True, False, None])
def test_hint_refuses_comparison_with_bool_or_none(hint, other):
    with pytest.raises(TypeError):
        hint == other  # noqa: B015
    with pytest.raises(TypeError):
        hint != other  # noqa: B015
    with pytest.raises(TypeError):
        other == hint  # noqa: B015


def test_hint_equality_and_hashing_among_hints():
    assert Hint.TRUE == Hint.TRUE
    assert Hint.ABSENT != Hint.FALSE
    assert len({Hint.TRUE, Hint.FALSE, Hint.ABSENT, Hint.ABSENT}) == 3
    assert {Hint.ABSENT: "x"}[Hint.ABSENT] == "x"


@pytest.mark.parametrize("value", [True, False, None, 0, 1])
def test_hint_cannot_be_constructed_from_non_string(value):
    with pytest.raises(ValueError):
        Hint(value)


def test_only_absent_is_undeclared():
    assert Hint.ABSENT.is_declared is False
    assert Hint.TRUE.is_declared is True
    assert Hint.FALSE.is_declared is True


@pytest.mark.parametrize("bad", [True, False, None, "true", 1])
def test_contract_rejects_non_hint_fields(bad):
    with pytest.raises(TypeError):
        DeclaredContract(
            tool_name="t",
            read_only=bad,
            destructive=Hint.ABSENT,
            idempotent=Hint.ABSENT,
            open_world=Hint.ABSENT,
            annotations_block=AnnotationsBlock.OBJECT,
        )


def test_contract_is_immutable():
    c = parse_tool(tool({"readOnlyHint": True}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.read_only = Hint.FALSE


# --- Parsing: absence is preserved -----------------------------------------


@pytest.mark.parametrize(
    "annotations, block",
    [
        (_MISSING, AnnotationsBlock.MISSING),
        (None, AnnotationsBlock.NULL),
        ({}, AnnotationsBlock.OBJECT),
        ({"title": "Only a title"}, AnnotationsBlock.OBJECT),
    ],
)
def test_no_hints_means_all_absent(annotations, block):
    c = parse_tool(tool(annotations))
    assert c.annotations_block is block
    for a in Annotation:
        assert c.hint(a) is Hint.ABSENT
    assert c.declares_anything is False
    assert c.anomalies == ()


# Every combination of {true, false, missing} across the four keys: each hint
# must map independently and exactly, and a missing key must never read as
# false (nor false as missing).
_STATES = [(True, Hint.TRUE), (False, Hint.FALSE), (_MISSING, Hint.ABSENT)]


@pytest.mark.parametrize(
    "combo",
    list(itertools.product(_STATES, repeat=4)),
    ids=lambda combo: "-".join(h.value for _, h in combo),
)
def test_every_hint_combination_maps_exactly(combo):
    annotations = {
        name: raw for name, (raw, _) in zip(WIRE_NAMES, combo) if raw is not _MISSING
    }
    c = parse_tool(tool(annotations))
    for annotation, (_, expected) in zip(Annotation, combo):
        assert c.hint(annotation) is expected
    assert c.declares_anything is any(h is not Hint.ABSENT for _, h in combo)
    assert c.anomalies == ()


def test_named_fields_match_wire_names():
    c = parse_tool(
        tool(
            {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            }
        )
    )
    assert c.read_only is Hint.TRUE
    assert c.destructive is Hint.FALSE
    assert c.idempotent is Hint.TRUE
    assert c.open_world is Hint.FALSE


def test_spec_defaults_are_not_applied():
    # The MCP spec defaults destructiveHint and openWorldHint to true and the
    # others to false when absent. None of that may leak into the contract.
    c = parse_tool(tool({"readOnlyHint": False}))
    assert c.destructive is Hint.ABSENT
    assert c.idempotent is Hint.ABSENT
    assert c.open_world is Hint.ABSENT


# --- Parsing: malformed values are never declarations -----------------------


@pytest.mark.parametrize(
    "value, kind",
    [
        (None, AnomalyKind.NULL_HINT),
        ("true", AnomalyKind.NON_BOOLEAN_HINT),
        ("false", AnomalyKind.NON_BOOLEAN_HINT),
        ("", AnomalyKind.NON_BOOLEAN_HINT),
        (1, AnomalyKind.NON_BOOLEAN_HINT),
        (0, AnomalyKind.NON_BOOLEAN_HINT),
        (1.0, AnomalyKind.NON_BOOLEAN_HINT),
        ([], AnomalyKind.NON_BOOLEAN_HINT),
        ({}, AnomalyKind.NON_BOOLEAN_HINT),
    ],
)
@pytest.mark.parametrize("annotation", list(Annotation))
def test_non_boolean_hint_is_absent_with_anomaly(annotation, value, kind):
    c = parse_tool(tool({annotation.wire_name: value}))
    assert c.hint(annotation) is Hint.ABSENT
    assert c.anomalies == (
        ParseAnomaly(
            location=f"annotations.{annotation.wire_name}",
            kind=kind,
            raw=json.dumps(value),
            annotation=annotation,
        ),
    )


def test_malformed_hint_does_not_disturb_siblings():
    c = parse_tool(tool({"readOnlyHint": "yes", "idempotentHint": True}))
    assert c.read_only is Hint.ABSENT
    assert c.idempotent is Hint.TRUE
    assert [a.location for a in c.anomalies] == ["annotations.readOnlyHint"]


@pytest.mark.parametrize("annotations", [[], "readOnly", True, 0])
def test_non_object_annotations_block(annotations):
    c = parse_tool(tool(annotations))
    assert c.annotations_block is AnnotationsBlock.MALFORMED
    for a in Annotation:
        assert c.hint(a) is Hint.ABSENT
    assert [a.kind for a in c.anomalies] == [AnomalyKind.ANNOTATIONS_NOT_OBJECT]


def test_hint_at_tool_top_level_is_not_a_declaration():
    c = parse_tool(tool(readOnlyHint=True, idempotentHint=False))
    assert c.read_only is Hint.ABSENT
    assert c.idempotent is Hint.ABSENT
    assert {(a.location, a.kind) for a in c.anomalies} == {
        ("readOnlyHint", AnomalyKind.HINT_OUTSIDE_ANNOTATIONS),
        ("idempotentHint", AnomalyKind.HINT_OUTSIDE_ANNOTATIONS),
    }


# --- Input validation -------------------------------------------------------


class FakeSdkTool:
    name = "t"
    annotations = None


@pytest.mark.parametrize("bad", [FakeSdkTool(), None, "t", ["name", "t"]])
def test_parse_tool_requires_raw_mapping(bad):
    with pytest.raises(TypeError):
        parse_tool(bad)


@pytest.mark.parametrize("name", [_MISSING, None, "", 7])
def test_parse_tool_requires_name(name):
    t = {"inputSchema": {"type": "object"}}
    if name is not _MISSING:
        t["name"] = name
    with pytest.raises(ContractParseError):
        parse_tool(t)


def test_parse_tools_keys_by_name_in_order():
    contracts = parse_tools(
        [
            {"name": "b", "annotations": {"readOnlyHint": True}},
            {"name": "a"},
        ]
    )
    assert list(contracts) == ["b", "a"]
    assert contracts["b"].read_only is Hint.TRUE
    assert contracts["a"].read_only is Hint.ABSENT


def test_parse_tools_rejects_duplicate_names():
    with pytest.raises(ContractParseError, match="duplicate"):
        parse_tools([{"name": "a"}, {"name": "a"}])


# --- Serialization ----------------------------------------------------------


def _walk(value):
    yield value
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


@pytest.mark.parametrize(
    "raw_tool",
    [
        tool(),
        tool(None),
        tool({"readOnlyHint": True, "destructiveHint": False}),
        tool({"idempotentHint": None, "openWorldHint": "true"}, readOnlyHint=1),
        tool(["not", "an", "object"]),
    ],
)
def test_json_round_trip(raw_tool):
    c = parse_tool(raw_tool)
    encoded = json.loads(json.dumps(c.to_json()))
    assert DeclaredContract.from_json(encoded) == c


def test_json_never_encodes_hints_as_booleans_or_null():
    c = parse_tool(tool({"readOnlyHint": True, "destructiveHint": False}))
    encoded = c.to_json()
    assert encoded["hints"] == {
        "readOnlyHint": "true",
        "destructiveHint": "false",
        "idempotentHint": "absent",
        "openWorldHint": "absent",
    }
    for v in _walk(encoded):
        assert v is not None and not isinstance(v, bool)


def _serialized(**hints):
    base = {
        "tool": "t",
        "hints": {name: "absent" for name in WIRE_NAMES},
        "annotations_block": "object",
        "anomalies": [],
    }
    base["hints"].update(hints)
    return base


@pytest.mark.parametrize("bad", [True, False, None, "TRUE", "unknown"])
def test_from_json_rejects_non_canonical_hint_values(bad):
    with pytest.raises(ValueError):
        DeclaredContract.from_json(_serialized(readOnlyHint=bad))


@pytest.mark.parametrize("name", WIRE_NAMES)
def test_from_json_rejects_omitted_hint(name):
    data = _serialized()
    del data["hints"][name]
    with pytest.raises(ContractParseError, match=name):
        DeclaredContract.from_json(data)


# --- Malformed declarations are distinguishable from no declaration ---------


def test_clean_contracts_have_no_malformed_hints():
    assert parse_tool(tool()).malformed_hints == frozenset()
    assert parse_tool(tool({"readOnlyHint": False})).malformed_hints == frozenset()


@pytest.mark.parametrize("value", [None, "true", 1])
def test_non_boolean_hint_is_malformed_not_undeclared(value):
    c = parse_tool(tool({"idempotentHint": value, "readOnlyHint": True}))
    assert c.malformed_hints == {Annotation.IDEMPOTENT}


def test_malformed_block_marks_every_hint_malformed():
    assert parse_tool(tool(["readOnly"])).malformed_hints == set(Annotation)


def test_hint_outside_annotations_is_malformed_unless_declared_properly():
    stray_only = parse_tool(tool(readOnlyHint=True))
    assert stray_only.malformed_hints == {Annotation.READ_ONLY}
    both = parse_tool(tool({"readOnlyHint": True}, readOnlyHint=True))
    assert both.read_only is Hint.TRUE
    assert both.malformed_hints == frozenset()


# --- Raw listings -----------------------------------------------------------


def _page(tools_json, request_id=1):
    return '{"jsonrpc":"2.0","id":%d,"result":{"tools":%s}}' % (request_id, tools_json)


def test_listing_keeps_pages_verbatim_and_concatenates():
    pages = [
        _page('[{"name":"a","annotations":{"readOnlyHint":true}}]'),
        _page('[ {"name" : "b"} ]', request_id=2),
    ]
    listing = parse_listing(pages)
    assert listing.pages == tuple(pages)
    assert listing.to_json()["raw_pages"] == pages
    assert list(listing.contracts) == ["a", "b"]
    assert listing.contracts["a"].read_only is Hint.TRUE


@pytest.mark.parametrize(
    "annotations_json, values",
    [
        ('{"readOnlyHint":true,"readOnlyHint":false}', [True, False]),
        # Even agreeing duplicates are malformed: the wire format is ambiguous.
        ('{"readOnlyHint":true,"readOnlyHint":true}', [True, True]),
    ],
)
def test_duplicate_hint_key_is_malformed(annotations_json, values):
    listing = parse_listing([_page('[{"name":"a","annotations":%s}]' % annotations_json)])
    c = listing.contracts["a"]
    assert c.read_only is Hint.ABSENT
    assert c.malformed_hints == {Annotation.READ_ONLY}
    (anomaly,) = c.anomalies
    assert anomaly.kind is AnomalyKind.DUPLICATE_KEY
    assert json.loads(anomaly.raw) == values


def test_duplicate_annotations_block_is_malformed():
    listing = parse_listing(
        [_page('[{"name":"a","annotations":{"readOnlyHint":true},"annotations":{}}]')]
    )
    c = listing.contracts["a"]
    assert c.annotations_block is AnnotationsBlock.MALFORMED
    assert c.malformed_hints == set(Annotation)


def test_duplicate_keys_elsewhere_do_not_affect_hints():
    listing = parse_listing(
        [_page('[{"name":"a","description":"x","description":"y","annotations":{"readOnlyHint":true}}]')]
    )
    assert listing.contracts["a"].read_only is Hint.TRUE


@pytest.mark.parametrize(
    "page",
    [
        "not json",
        '{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"no"}}',
        '{"jsonrpc":"2.0","id":1,"result":{}}',
        '{"jsonrpc":"2.0","id":1,"result":{"tools":{}}}',
        "[]",
    ],
)
def test_listing_rejects_unusable_pages(page):
    with pytest.raises(ContractParseError):
        parse_listing([page])

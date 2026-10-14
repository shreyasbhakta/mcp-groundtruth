"""Declared contracts: what a tool claims about itself in ``tools/list``.

Each of the four MCP behavioral hints is tri-state: ``TRUE``, ``FALSE`` or
``ABSENT``. Absent is never false. The MCP spec defines defaults for absent
hints (e.g. ``readOnlyHint`` defaults to false); this harness deliberately does
NOT apply them, because a default is not a declaration and a server cannot be
held to a claim it never made.

To keep absence from collapsing into false anywhere downstream:

* ``Hint`` has no truth value. ``if hint:``, ``not hint`` and ``hint or x``
  raise ``TypeError``.
* Comparing a ``Hint`` to a ``bool`` or ``None`` raises ``TypeError`` instead
  of silently returning ``False``.
* ``Hint`` cannot be constructed from a ``bool``; values are strings.
* Parsing reads the raw JSON from ``tools/list``. The MCP Python SDK's pydantic
  models coerce ``"true"``, ``1`` and ``"no"`` into booleans and fold an
  explicit ``null`` into "unset", so SDK model objects are rejected.
* Only the JSON literals ``true`` and ``false`` produce ``TRUE``/``FALSE``.
  Anything else present under a hint key yields ``ABSENT`` plus a recorded
  ``ParseAnomaly``, so it can never count as an explicit declaration. Such
  hints are listed in ``DeclaredContract.malformed_hints`` so verdicts can say
  MALFORMED_DECLARATION rather than NO_DECLARATION.
* ``parse_listing`` parses the response text exactly as received and keeps it,
  so every report can reproduce the bytes its contracts came from. Duplicate
  JSON keys, which ``json.loads`` would silently resolve last-wins, are
  detected and treated as malformed.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class Hint(enum.Enum):
    TRUE = "true"
    FALSE = "false"
    ABSENT = "absent"

    def __bool__(self) -> bool:
        raise TypeError("Hint has no truth value; compare against Hint.TRUE, Hint.FALSE or Hint.ABSENT explicitly")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Hint):
            return self is other
        if other is None or isinstance(other, bool):
            raise TypeError(
                f"comparing Hint to {other!r} is always a bug; compare against Hint.TRUE, Hint.FALSE or Hint.ABSENT"
            )
        return NotImplemented

    __hash__ = enum.Enum.__hash__

    @property
    def is_declared(self) -> bool:
        return self is not Hint.ABSENT


class Annotation(enum.Enum):
    """The four behavioral hints, valued by their wire name."""

    READ_ONLY = "readOnlyHint"
    DESTRUCTIVE = "destructiveHint"
    IDEMPOTENT = "idempotentHint"
    OPEN_WORLD = "openWorldHint"

    @property
    def wire_name(self) -> str:
        return self.value


class AnnotationsBlock(enum.Enum):
    """What the tool's ``annotations`` member itself looked like."""

    MISSING = "missing"  # no ``annotations`` key
    NULL = "null"  # ``"annotations": null``
    OBJECT = "object"  # a JSON object (possibly empty)
    MALFORMED = "malformed"  # present but not an object or null


class AnomalyKind(enum.Enum):
    NULL_HINT = "null_hint"
    NON_BOOLEAN_HINT = "non_boolean_hint"
    ANNOTATIONS_NOT_OBJECT = "annotations_not_object"
    HINT_OUTSIDE_ANNOTATIONS = "hint_outside_annotations"
    DUPLICATE_KEY = "duplicate_key"


@dataclass(frozen=True, slots=True)
class ParseAnomaly:
    location: str  # dotted path within the tool object, e.g. "annotations.readOnlyHint"
    kind: AnomalyKind
    raw: str  # the offending value, JSON-encoded; for DUPLICATE_KEY, the list of all values
    annotation: Annotation | None = None  # the hint concerned; None for block-level anomalies

    def to_json(self) -> dict[str, str]:
        data = {"location": self.location, "kind": self.kind.value, "raw": self.raw}
        if self.annotation is not None:
            data["annotation"] = self.annotation.wire_name
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ParseAnomaly:
        annotation = data.get("annotation")
        return cls(
            location=_require_str(data, "location"),
            kind=AnomalyKind(_require_str(data, "kind")),
            raw=_require_str(data, "raw"),
            annotation=None if annotation is None else Annotation(annotation),
        )


class ContractParseError(ValueError):
    """The tools/list payload is unusable (not merely annotated oddly)."""


_FIELD_FOR: dict[Annotation, str] = {
    Annotation.READ_ONLY: "read_only",
    Annotation.DESTRUCTIVE: "destructive",
    Annotation.IDEMPOTENT: "idempotent",
    Annotation.OPEN_WORLD: "open_world",
}


@dataclass(frozen=True, slots=True)
class DeclaredContract:
    tool_name: str
    read_only: Hint
    destructive: Hint
    idempotent: Hint
    open_world: Hint
    annotations_block: AnnotationsBlock
    anomalies: tuple[ParseAnomaly, ...] = ()

    def __post_init__(self) -> None:
        for annotation, field_name in _FIELD_FOR.items():
            value = getattr(self, field_name)
            if not isinstance(value, Hint):
                raise TypeError(f"{annotation.wire_name} must be a Hint, got {value!r}")
        if not isinstance(self.annotations_block, AnnotationsBlock):
            raise TypeError(f"annotations_block must be an AnnotationsBlock, got {self.annotations_block!r}")

    def hint(self, annotation: Annotation) -> Hint:
        value: Hint = getattr(self, _FIELD_FOR[annotation])
        return value

    @property
    def declares_anything(self) -> bool:
        return any(self.hint(a).is_declared for a in Annotation)

    @property
    def malformed_hints(self) -> frozenset[Annotation]:
        """ABSENT hints the server did try to send, in a form we refuse to read.

        A hint that ended up declared is never listed here, even if a stray
        copy also appeared somewhere else in the tool object.
        """
        if self.annotations_block is AnnotationsBlock.MALFORMED:
            attempted = set(Annotation)
        else:
            attempted = {an.annotation for an in self.anomalies if an.annotation is not None}
        return frozenset(a for a in attempted if self.hint(a) is Hint.ABSENT)

    def to_json(self) -> dict[str, Any]:
        # Hints serialize as the strings "true" / "false" / "absent", never as
        # JSON booleans or null, so no consumer can read absence as false.
        return {
            "tool": self.tool_name,
            "hints": {a.wire_name: self.hint(a).value for a in Annotation},
            "annotations_block": self.annotations_block.value,
            "anomalies": [an.to_json() for an in self.anomalies],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> DeclaredContract:
        hints = data.get("hints")
        if not isinstance(hints, Mapping):
            raise ContractParseError("serialized contract has no 'hints' object")
        parsed: dict[str, Hint] = {}
        for annotation in Annotation:
            # A serialized contract must spell out every hint; a missing key
            # here means a corrupt report, not an absent declaration.
            if annotation.wire_name not in hints:
                raise ContractParseError(f"serialized contract omits {annotation.wire_name}")
            raw = hints[annotation.wire_name]
            if not isinstance(raw, str):
                raise ContractParseError(f"serialized {annotation.wire_name} must be a string, got {raw!r}")
            parsed[_FIELD_FOR[annotation]] = Hint(raw)
        return cls(
            tool_name=_require_str(data, "tool"),
            annotations_block=AnnotationsBlock(_require_str(data, "annotations_block")),
            anomalies=tuple(ParseAnomaly.from_json(a) for a in data.get("anomalies", ())),
            **parsed,
        )


def parse_tool(tool: Mapping[str, Any]) -> DeclaredContract:
    """Parse one raw tool object from a ``tools/list`` result."""
    if not isinstance(tool, Mapping):
        raise TypeError(
            f"parse_tool needs the raw JSON tool object, got {type(tool).__name__}; "
            "SDK models have already coerced annotation values"
        )
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise ContractParseError(f"tool has no usable name: {name!r}")

    anomalies: list[ParseAnomaly] = []

    for annotation in Annotation:
        if annotation.wire_name in tool:
            anomalies.append(
                ParseAnomaly(
                    location=annotation.wire_name,
                    kind=AnomalyKind.HINT_OUTSIDE_ANNOTATIONS,
                    raw=_encode(tool[annotation.wire_name]),
                    annotation=annotation,
                )
            )

    raw_block: Mapping[str, Any]
    if "annotations" in _duplicates(tool):
        block, raw_block = AnnotationsBlock.MALFORMED, {}
        anomalies.append(
            ParseAnomaly(
                location="annotations",
                kind=AnomalyKind.DUPLICATE_KEY,
                raw=_encode(_duplicates(tool)["annotations"]),
            )
        )
    elif "annotations" not in tool:
        block, raw_block = AnnotationsBlock.MISSING, {}
    elif tool["annotations"] is None:
        block, raw_block = AnnotationsBlock.NULL, {}
    elif isinstance(tool["annotations"], Mapping):
        block, raw_block = AnnotationsBlock.OBJECT, tool["annotations"]
    else:
        block, raw_block = AnnotationsBlock.MALFORMED, {}
        anomalies.append(
            ParseAnomaly(
                location="annotations",
                kind=AnomalyKind.ANNOTATIONS_NOT_OBJECT,
                raw=_encode(tool["annotations"]),
            )
        )

    hints: dict[str, Hint] = {}
    for annotation in Annotation:
        hint, anomaly = _parse_hint(raw_block, annotation)
        hints[_FIELD_FOR[annotation]] = hint
        if anomaly is not None:
            anomalies.append(anomaly)

    return DeclaredContract(
        tool_name=name,
        annotations_block=block,
        anomalies=tuple(anomalies),
        **hints,
    )


def parse_tools(tools: Iterable[Mapping[str, Any]]) -> dict[str, DeclaredContract]:
    """Parse every raw tool object (all pages concatenated), keyed by name."""
    contracts: dict[str, DeclaredContract] = {}
    for tool in tools:
        contract = parse_tool(tool)
        if contract.tool_name in contracts:
            raise ContractParseError(f"duplicate tool name: {contract.tool_name!r}")
        contracts[contract.tool_name] = contract
    return contracts


def parse_listing(pages: Sequence[str]) -> ToolListing:
    """Parse ``tools/list`` JSON-RPC responses exactly as received, one per page."""
    tools: list[Mapping[str, Any]] = []
    for index, page in enumerate(pages):
        try:
            message = json.loads(page, object_pairs_hook=_JsonObject.from_pairs)
        except json.JSONDecodeError as exc:
            raise ContractParseError(f"page {index}: not JSON: {exc}") from exc
        result = message.get("result") if isinstance(message, Mapping) else None
        if not isinstance(result, Mapping):
            raise ContractParseError(f"page {index}: not a successful JSON-RPC result")
        page_tools = result.get("tools")
        if not isinstance(page_tools, list):
            raise ContractParseError(f"page {index}: result.tools is not a list")
        tools.extend(page_tools)
    return ToolListing(pages=tuple(pages), contracts=parse_tools(tools))


@dataclass(frozen=True, slots=True)
class ToolListing:
    pages: tuple[str, ...]  # each tools/list response, verbatim
    contracts: Mapping[str, DeclaredContract]

    def to_json(self) -> dict[str, Any]:
        return {
            "raw_pages": list(self.pages),
            "contracts": [c.to_json() for c in self.contracts.values()],
        }


class _JsonObject(dict[str, Any]):
    """A decoded JSON object that remembers keys which appeared more than once."""

    duplicates: dict[str, list[Any]]

    @classmethod
    def from_pairs(cls, pairs: list[tuple[str, Any]]) -> _JsonObject:
        obj = cls(pairs)
        seen: dict[str, list[Any]] = {}
        for key, value in pairs:
            seen.setdefault(key, []).append(value)
        obj.duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        return obj


def _duplicates(obj: Mapping[str, Any]) -> dict[str, list[Any]]:
    return getattr(obj, "duplicates", {})


def _parse_hint(block: Mapping[str, Any], annotation: Annotation) -> tuple[Hint, ParseAnomaly | None]:
    key = annotation.wire_name
    if key in _duplicates(block):
        # Parsers disagree on first-wins vs last-wins; no single reading is the declaration.
        return Hint.ABSENT, ParseAnomaly(
            location=f"annotations.{key}",
            kind=AnomalyKind.DUPLICATE_KEY,
            raw=_encode(_duplicates(block)[key]),
            annotation=annotation,
        )
    if key not in block:
        return Hint.ABSENT, None
    value = block[key]
    # Identity checks: 1 == True and 0 == False in Python, and a JSON number
    # is not a boolean declaration.
    if value is True:
        return Hint.TRUE, None
    if value is False:
        return Hint.FALSE, None
    kind = AnomalyKind.NULL_HINT if value is None else AnomalyKind.NON_BOOLEAN_HINT
    return Hint.ABSENT, ParseAnomaly(
        location=f"annotations.{key}", kind=kind, raw=_encode(value), annotation=annotation
    )


def _encode(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ContractParseError(f"expected string for {key!r}, got {value!r}")
    return value

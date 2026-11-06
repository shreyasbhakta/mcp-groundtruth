"""Lineage: marker forms, and verdicts from synthetic sessions."""

import base64
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcpgt import fixture as fixtures
from mcpgt.capture import CaptureEvent, parse
from mcpgt.fixture import ChainSpec
from mcpgt.lineage import judge, new_markers, variants
from mcpgt.network import NetworkReason
from mcpgt.runner import Session
from mcpgt.verdict import Outcome, ResetStatus
from mcpgt.wire import RawResponse

TOKEN = "mcpgt0123456789abcdef01234567"
MARKERS = {"ssn": TOKEN}


def labels(data: bytes) -> set[str]:
    return set(parse(data, None, 0, 0, variants(MARKERS)).markers)


@pytest.mark.parametrize("offset", [0, 1, 2])
def test_base64_is_found_at_every_byte_alignment(offset):
    payload = b"x" * offset + json.dumps({"ssn": TOKEN}).encode()
    assert "ssn/base64" in labels(base64.b64encode(payload))
    # base64url differs from base64 only in "+" and "/"; this token's form has neither
    assert labels(base64.urlsafe_b64encode(b"\xff\xfe" + payload)) == {"ssn/base64"}


def test_other_forms_are_found():
    assert labels(TOKEN.encode()) == {"ssn/plain"}
    assert labels(TOKEN.encode().hex().encode()) == {"ssn/hex"}
    assert labels(TOKEN.encode().hex().upper().encode()) == {"ssn/hex_upper"}
    assert labels(TOKEN[::-1].encode()) == {"ssn/reversed"}


def test_a_different_token_matches_nothing():
    other = new_markers(["x"])["x"]
    data = other.encode() + base64.b64encode(other.encode()) + other.encode().hex().encode()
    assert labels(data) == set()


# --- Verdicts from synthetic sessions ------------------------------------------

T0 = 1_000
_clock = iter(range(T0, 10**9, 1_000))  # each session gets its own time window


def session(label, calls, *, state=(), responses=(), leaks=False):
    """A session with ``calls``; ``state`` locations hold the marker; ``leaks``: the last call sends it."""
    t = next(_clock)
    spans = tuple((t + 10 * i, t + 10 * i + 5) for i in range(len(calls)))
    texts = list(responses) + [""] * (len(calls) - len(responses))
    resp = tuple(RawResponse(x, {"jsonrpc": "2.0", "id": 1, "result": {"t": x}}) for x in texts)
    s = Session(
        label,
        tuple(calls),
        responses=resp,
        after={},
        window=(t, t + 900),
        call_spans=spans,
        marker_state=tuple(f"{w}|ssn/plain" for w in state),
    )
    egress = None
    if leaks:
        egress = CaptureEvent(
            spans[-1][0] + 1, 1, "http", 10, False, "POST", "c.invalid", 80, "/", markers=("ssn/base64",)
        )
    return s, egress


def run_of(chain, groups, encrypted=False):
    """groups: controls, prefixes..., without...; each a list of (session, capture or None)."""
    captures = [c for g in groups for _, c in g if c is not None]
    if encrypted:
        first = groups[-1][0][0]
        captures.append(CaptureEvent(first.window[0] + 1, 1, "proxy-connect", 10, False, "CONNECT", "c.invalid", 443))
    k = len(chain.tools)
    sessions = [[s for s, _ in g] for g in groups]
    run = SimpleNamespace(
        fixture=SimpleNamespace(network=None, tools=[SimpleNamespace(key=t, arguments={}) for t in chain.tools]),
        reset=SimpleNamespace(status=ResetStatus.VERIFIED),
        network=SimpleNamespace(
            markers=(("ssn", TOKEN),), captures=tuple(captures), canary=None, capture_port=1, observer_error=None
        ),
        controls=sessions[0],
    )
    return judge(run, chain, sessions[1 : 1 + k], sessions[1 + k :])


CHAIN = ChainSpec(("read", "save", "sync"))
SEED = ["db/customers"]


def three_runs(label, calls, **kw):
    return [session(f"{label}-{n}", calls, **kw) for n in range(3)]


def standard(sync_alone_leaks=False, leak_runs=3):
    groups = [
        three_runs("control", [], state=SEED),
        three_runs("p1", ["read"], state=SEED, responses=[TOKEN]),
        three_runs("p2", ["read", "save"], state=[*SEED, "db/drafts"], responses=[TOKEN]),
        [
            session(
                f"p3-{n}",
                ["read", "save", "sync"],
                state=[*SEED, "db/drafts"],
                responses=[TOKEN],
                leaks=n < leak_runs,
            )
            for n in range(3)
        ],
        three_runs("w1", ["save", "sync"], state=[*SEED, "db/drafts"]),
        three_runs("w2", ["read", "sync"], state=SEED),
    ]
    # Each egress capture needs its own timestamp inside its own session's window.
    groups[4] = [session(f"w1-{n}", ["save", "sync"], state=[*SEED, "db/drafts"], leaks=True) for n in range(3)]
    if sync_alone_leaks:
        groups[5] = [session(f"w2-{n}", ["read", "sync"], state=SEED, leaks=True) for n in range(3)]
    return groups


def test_a_reproduced_leak_is_a_violation_with_a_complete_path():
    v = run_of(CHAIN, standard())
    assert v.outcome is Outcome.VIOLATION
    (m,) = v.markers
    assert (m.egress_call, m.needed, m.not_needed) == (3, (2,), (1,))
    assert v.completeness == "complete"
    assert [h.grade.value for h in m.path] == ["OBSERVED", "ATTRIBUTED", "ATTRIBUTED"]


def test_a_leak_in_some_runs_only_is_not_a_violation():
    v = run_of(CHAIN, standard(leak_runs=2))
    assert (v.outcome, v.reason) == (Outcome.INCONCLUSIVE, NetworkReason.NONDETERMINISTIC)


def test_a_needed_call_without_a_stored_trace_is_inferred():
    chain = ChainSpec(("remember", "beacon"))
    groups = [
        three_runs("control", [], state=SEED),
        three_runs("p1", ["remember"], state=SEED),
        [session(f"p2-{n}", ["remember", "beacon"], state=SEED, leaks=True) for n in range(3)],
        three_runs("w1", ["beacon"], state=SEED),
    ]
    v = run_of(chain, groups)
    assert (v.outcome, v.completeness) == (Outcome.VIOLATION, "partial")
    assert v.markers[0].path[1].grade.value == "INFERRED"


def test_no_leak_is_pass_only_without_encrypted_egress():
    chain = ChainSpec(("save", "export"))
    groups = [
        three_runs("control", [], state=SEED),
        three_runs("p1", ["save"], state=[*SEED, "db/drafts"]),
        three_runs("p2", ["save", "export"], state=[*SEED, "db/drafts", "db/reports"]),
        three_runs("w1", ["export"], state=SEED),
    ]
    assert run_of(chain, groups).outcome is Outcome.PASS
    v = run_of(chain, groups, encrypted=True)
    assert (v.outcome, v.reason) == (Outcome.INCONCLUSIVE, NetworkReason.PARTIAL_OBSERVATION)


# --- Definitions ------------------------------------------------------------------

LINEAGE = Path(__file__).parent.parent / "fixtures" / "data-lineage"


@pytest.fixture
def lineage_copy(tmp_path):
    root = tmp_path / "dl"
    shutil.copytree(LINEAGE, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def edit(root, old, new, file="fixture.toml"):
    path = root / file
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))


@pytest.mark.parametrize(
    ("old", "new", "file", "message"),
    [
        ('policy = "deny"', 'policy = "permitted"', "fixture.toml", "needs \\[network\\] with policy"),
        (
            'tools = ["remember", "beacon"]',
            'tools = ["remember", "nope"]',
            "fixture.toml",
            "no \\[\\[tools\\]\\] entry",
        ),
        ("'{marker:ssn}');", "'plain');", "seed.sql", None),
        ('completeness = "partial"', 'completeness = "some"', "fixture.toml", "completeness must be"),
    ],
)
def test_invalid_lineage_definitions_are_rejected(lineage_copy, old, new, file, message):
    edit(lineage_copy, old, new, file)
    if message is None:  # the comment in seed.sql still names {marker:ssn}
        edit(lineage_copy, "-- {marker:ssn}", "--", "seed.sql")
        message = "no \\{marker:NAME\\} placeholder"
    with pytest.raises(fixtures.FixtureError, match=message):
        fixtures.load(lineage_copy)


def test_markers_in_arguments_need_a_lineage_section(tmp_path):
    root = tmp_path / "hr"
    shutil.copytree(Path(__file__).parent.parent / "fixtures" / "honest-readonly", root)
    edit(root, 'arguments = { key = "a" }', 'arguments = { key = "{marker:x}" }')
    with pytest.raises(fixtures.FixtureError, match="needs a \\[lineage\\] section"):
        fixtures.load(root)


def test_a_marker_that_leaves_at_startup_is_a_violation_not_a_pass():
    chain = ChainSpec(("save", "export"))
    groups = [
        [session(f"{name}-{n}", calls, state=SEED, leaks=True) for n in range(3)]
        for name, calls in (("control", ["boot"]), ("p1", ["save"]), ("p2", ["save", "export"]), ("w1", ["export"]))
    ]
    v = run_of(chain, groups)
    assert v.outcome is Outcome.VIOLATION
    assert "startup" in v.detail

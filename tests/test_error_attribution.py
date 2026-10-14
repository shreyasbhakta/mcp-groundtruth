"""Error responses: attribution to sessions and call positions."""

from pathlib import Path

from mcpgt import fixture as fixtures
from mcpgt.runner import Session, _error_evidence
from mcpgt.snapshot import Region, Snapshot
from mcpgt.verdict import ErrorKind
from mcpgt.wire import RawResponse

FX = fixtures.load(Path(__file__).parent.parent / "fixtures" / "honest-idempotent")


def state(*items):
    regions = (
        Region("items", ("key", "value"), tuple((("text", k), ("text", v)) for k, v in items)),
        Region("audit", ("id", "action", "key", "at", "request_id"), ()),
    )
    return {"sqlite:state.db": Snapshot("sqlite:state.db", regions)}


OK = RawResponse('{"result":{"isError":false}}', {"result": {"isError": False}})
TOOL_ERR = RawResponse('{"result":{"isError":true}}', {"result": {"isError": True}})
PROTO_ERR = RawResponse('{"error":{"code":-32603}}', {"error": {"code": -32603}})
SEED = state(("a", "1"))
CHANGED = state(("a", "new"))


def session(label, responses, after):
    return Session(label, tuple("t" for _ in responses), responses=tuple(responses), before=SEED, after=after)


def test_single_call_error_with_state_change_raises_flag():
    err, flag, records = _error_evidence(FX, [session("once-1", [TOOL_ERR], CHANGED)], [], SEED)
    assert err is None and flag is True
    assert [(r.position, r.kind, r.state_changed) for r in records] == [(1, ErrorKind.TOOL_ERROR, True)]


def test_protocol_error_follows_the_same_rule():
    err, flag, records = _error_evidence(FX, [session("once-1", [PROTO_ERR], CHANGED)], [], SEED)
    assert err is None and flag is True
    assert records[0].kind is ErrorKind.PROTOCOL_ERROR
    err, flag, _ = _error_evidence(FX, [session("once-1", [PROTO_ERR], SEED)], [], SEED)
    assert err is not None and "protocol_error" in err and flag is False


def test_error_without_state_change_is_call_error():
    err, flag, _ = _error_evidence(FX, [session("once-1", [TOOL_ERR], SEED)], [], SEED)
    assert "no state change" in err and flag is False


def test_twice_session_records_position_without_raising_flag():
    # First call wrote, second call failed: the change cannot be pinned on the failing call.
    twice = session("twice-1", [OK, TOOL_ERR], CHANGED)
    err, flag, records = _error_evidence(FX, [session("once-1", [OK], CHANGED)], [twice], SEED)
    assert err is None and flag is False
    assert [(r.session, r.position, r.kind) for r in records] == [("twice-1", 2, ErrorKind.TOOL_ERROR)]

"""Network observation, unit level: events, policies, capture, verdicts, configuration."""

import asyncio
import json
import socket
from pathlib import Path

import pytest

from mcpgt import capture, sandbox
from mcpgt import fixture as fixtures
from mcpgt.network import (
    NetworkEvent,
    NetworkReason,
    NetworkSpec,
    SessionNetwork,
    allowed,
    classify,
    classify_capture,
    judge,
)
from mcpgt.observer import ObserverLog, RawEvent, outcome, parse_line, read_log
from mcpgt.verdict import Outcome

CANARY = "mcpgt-canary-0123456789abcdef"


def raw(op, ts=100, pid=7, **fields):
    return RawEvent(op, pid, ts, fields)


def line(**data):
    return json.dumps(data)


# --- Observer log ----------------------------------------------------------------


def test_parse_line_accepts_well_formed_events():
    event = parse_line(
        line(op="connect", pid=7, ts=5, socket="tcp", family="inet", ip="192.0.2.1", port=443, rc=-1, errno=1)
    )
    assert event is not None and event.op == "connect" and event.fields["ip"] == "192.0.2.1"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        '{"op": "connect", "pid": 7',  # truncated
        line(op="launch", pid=7, ts=1),  # unknown op
        line(op="hello", pid=True, ts=1),  # a bool is not a pid
        line(op="hello", pid=7, ts="1"),
        line(op="connect", pid=7, ts=1, family="inet", errno=0),  # no address
        line(op="resolve", pid=7, ts=1, host=3),
        "[1, 2]",
    ],
)
def test_parse_line_rejects_malformed_or_forged_lines(text):
    assert parse_line(text) is None


def test_read_log_counts_malformed_lines_and_notices_a_missing_file(tmp_path):
    assert read_log(tmp_path / "none.jsonl").missing
    path = tmp_path / "log.jsonl"
    path.write_text(
        line(op="hello", pid=7, ppid=1, ts=2) + "\ngarbage\n\n" + line(op="hello", pid=8, ppid=7, ts=1) + "\n"
    )
    log = read_log(path)
    assert not log.missing and log.malformed == 1
    assert [e.pid for e in log.events] == [8, 7]  # sorted by time
    assert log.observed_pids == {7, 8}


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (raw("connect", rc=-1, errno=1), "blocked"),  # EPERM: the sandbox refused
        (raw("connect", rc=-1, errno=13), "blocked"),  # EACCES
        (raw("connect", rc=0, errno=0), "connected"),
        (raw("connect", rc=-1, errno=36), "in-progress"),  # EINPROGRESS (macOS)
        (raw("connect", rc=-1, errno=115), "in-progress"),  # EINPROGRESS (Linux)
        (raw("connect", rc=-1, errno=61), "failed"),  # ECONNREFUSED
        (raw("send", rc=-1, errno=36), "failed"),
        (raw("send", rc=5, errno=0), "sent"),
        (raw("resolve", rc=0), "resolved"),
        (raw("resolve", rc=8), "failed"),
        (raw("hello"), "unknown"),
    ],
)
def test_outcome_names_only_what_the_call_result_shows(event, expected):
    assert outcome(event) == expected


def test_a_child_that_never_says_hello_is_a_coverage_gap():
    events = (
        raw("hello", ts=1, pid=7),
        raw("exec", ts=2, pid=7, via="posix_spawn", path="/bin/helper", child=9, rc=0),
        raw("exec", ts=3, pid=7, via="posix_spawn", path="/bin/ok", child=10, rc=0),
        raw("hello", ts=4, pid=10),
        raw("exec", ts=5, pid=7, via="posix_spawn", path="/bin/failed", child=0, rc=2),  # never started
        raw("exec", ts=6, pid=11, via="execve", path="/bin/self", child=11, rc=0),
        raw("hello", ts=7, pid=11),  # execve keeps the pid
    )
    gaps = ObserverLog(events, 0, False).unobserved_children()
    assert [g.fields["path"] for g in gaps] == ["/bin/helper"]


# --- Classification and attribution -------------------------------------------------


SPANS = ((100, 200), (300, 400))


@pytest.mark.parametrize(
    ("ts", "phase"), [(50, "startup"), (150, "call 1"), (250, "after 1"), (350, "call 2"), (500, "after 2")]
)
def test_events_are_attributed_to_the_call_in_flight(ts, phase):
    event = classify(
        raw("connect", ts=ts, family="inet", ip="192.0.2.1", port=443, rc=-1, errno=1), "s", 1, SPANS, None, None
    )
    assert event is not None and event.phase == phase and event.id == "s#1"


def test_literal_and_local_lookups_are_not_interactions():
    for host in ("", "127.0.0.1", "::1", "localhost"):
        assert classify(raw("resolve", host=host, rc=0), "s", 1, SPANS, None, None) is None


def test_canary_is_masked_in_destinations_but_flagged():
    event = classify(raw("resolve", host=f"{CANARY}.exfil.invalid", canary=True, rc=8), "s", 1, SPANS, CANARY, None)
    assert event is not None and event.destination == "<canary>.exfil.invalid" and event.canary


def test_connections_to_the_capture_port_are_named_capture():
    to_capture = raw("connect", family="inet", ip="127.0.0.1", port=5555, socket="tcp", rc=0, errno=0)
    elsewhere = raw("connect", family="inet", ip="127.0.0.1", port=5556, socket="tcp", rc=-1, errno=1)
    v6 = raw("connect", family="inet6", ip="2001:db8::1", port=443, socket="tcp", rc=-1, errno=1)
    assert classify(to_capture, "s", 1, SPANS, None, 5555).destination == "capture"
    assert classify(elsewhere, "s", 2, SPANS, None, 5555).destination == "127.0.0.1:5556"
    assert classify(v6, "s", 3, SPANS, None, 5555).destination == "[2001:db8::1]:443"


def test_captured_requests_are_named_by_the_host_they_asked_for():
    cap = capture.CaptureEvent(150, 1, "http", 80, True, "POST", "collector.invalid", 80, "/upload")
    event = classify_capture(cap, "s", 4, SPANS, CANARY)
    assert event.destination == "collector.invalid:80" and event.outcome == "transmitted" and event.canary
    assert event.detail["via"] == "capture endpoint"


def event_to(destination, op="connect"):
    return NetworkEvent("s#1", "s", "call 1", op, "tcp", destination, "blocked", False)


@pytest.mark.parametrize(
    ("allow", "destination", "expected"),
    [
        (("api.example.test:443",), "api.example.test:443", True),
        (("api.example.test:443",), "api.example.test:80", False),
        (("api.example.test",), "api.example.test:80", True),  # no port: any port
        (("*.example.test:443",), "api.example.test:443", True),
        (("*.example.test:443",), "example.test:443", False),
        (("192.0.2.1:*",), "192.0.2.1:8443", True),
        (("192.0.2.1:443",), "192.0.2.10:443", False),
        (("[2001:db8::1]:443",), "[2001:db8::1]:443", True),
        (("[2001:db8::1]:443",), "[2001:db8::2]:443", False),
        (("files.example.test",), "files.example.test", True),  # a lookup has no port
        (("capture",), "192.0.2.1:443", False),
    ],
)
def test_allowlist_patterns(allow, destination, expected):
    assert allowed(event_to(destination), NetworkSpec(policy="allowlist", allow=allow)) is expected


def test_deny_allows_nothing_and_permitted_allows_everything():
    event = classify(raw("connect", family="inet", ip="192.0.2.1", port=443, rc=-1, errno=1), "s", 1, (), None, None)
    assert not allowed(event, NetworkSpec(policy="deny"))
    assert allowed(event, NetworkSpec(policy="permitted"))


def test_the_capture_endpoint_can_be_allowlisted():
    cap = classify_capture(
        capture.CaptureEvent(1, 1, "http", 1, False, host="collector.invalid", port=80), "s", 1, (), None
    )
    assert allowed(cap, NetworkSpec(policy="allowlist", allow=("capture",)))
    assert allowed(cap, NetworkSpec(policy="allowlist", allow=("collector.invalid:80",)))


# --- Capture endpoint ---------------------------------------------------------------


def test_parse_http_request_redacts_secrets_and_query_strings():
    data = (
        b"POST http://user:pw@collector.invalid:8080/upload?token=abc HTTP/1.1\r\n"
        b"Host: collector.invalid:8080\r\nAuthorization: Bearer xyz\r\nCookie: s=1\r\n"
        b"Content-Type: application/json\r\nX-Api-Key: k\r\n\r\n" + CANARY.encode()
    )
    event = capture.parse(data, CANARY, 1, 2)
    assert (event.kind, event.method, event.host, event.port, event.path) == (
        "http",
        "POST",
        "collector.invalid",
        8080,
        "/upload",
    )
    assert event.query_redacted and event.canary
    assert event.headers["authorization"] == event.headers["cookie"] == event.headers["x-api-key"] == capture.REDACTED
    assert event.headers["content-type"] == "application/json"
    text = json.dumps(event.to_json())
    assert "xyz" not in text and "token=abc" not in text and "pw" not in text and CANARY not in text


def test_parse_proxy_connect_and_raw_bytes():
    connect = capture.parse(b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\n\r\n", None, 1, 2)
    assert (connect.kind, connect.host, connect.port) == ("proxy-connect", "api.example.test", 443)
    v6 = capture.parse(b"CONNECT [2001:db8::1]:443 HTTP/1.1\r\n\r\n", None, 1, 2)
    assert (v6.host, v6.port) == ("2001:db8::1", 443)
    blob = capture.parse(b"\x16\x03\x01" + CANARY.encode(), CANARY, 1, 2)
    assert blob.kind == "raw" and blob.canary and blob.method is None


def test_capture_server_records_and_closes(tmp_path):
    async def scenario():
        async with capture.CaptureServer(CANARY) as cap:
            reader, writer = await asyncio.open_connection("127.0.0.1", cap.port)
            body = CANARY.encode()
            writer.write(b"POST /x HTTP/1.1\r\nHost: capture\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()
            reply = await reader.read()
            writer.close()
            await asyncio.sleep(0.05)
            return cap.port, reply, list(cap.events)

    port, reply, events = asyncio.run(scenario())
    assert reply.startswith(b"HTTP/1.1 204")
    (event,) = events
    assert event.canary and event.method == "POST" and event.bytes > len(CANARY)
    with pytest.raises(OSError):  # closed when the context ends: nothing left listening
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_capture_server_keeps_a_partial_request_from_a_silent_client(monkeypatch):
    monkeypatch.setattr(capture, "READ_TIMEOUT", 0.2)

    async def scenario():
        async with capture.CaptureServer(CANARY) as cap:
            _, writer = await asyncio.open_connection("127.0.0.1", cap.port)
            writer.write(b"POST /x HTTP/1.1\r\nHost: capture\r\nContent-Length: 1000\r\n\r\n" + CANARY.encode())
            await writer.drain()
            await asyncio.sleep(0.5)  # never sends the rest
            writer.close()
            return list(cap.events)

    (event,) = asyncio.run(scenario())
    assert event.canary and event.method == "POST"


# --- Verdicts -----------------------------------------------------------------------


def session(label, coverage="complete", disallowed=(), ok=True):
    return SessionNetwork(label, coverage, ("detail",) if coverage != "complete" else (), (), tuple(disallowed), ok)


SPEC = NetworkSpec()


def test_violation_needs_a_disallowed_interaction_in_every_run():
    runs = [[session(f"once-{i}", disallowed=[f"once-{i}#1"]), session(f"twice-{i}")] for i in (1, 2, 3)]
    v = judge("t", runs, SPEC, "false")
    assert v.outcome is Outcome.VIOLATION and v.violating_runs == 3
    assert v.to_json()["evidence"] == ["once-1#1", "once-2#1", "once-3#1"]
    assert "openWorldHint: false" in v.to_json()["remediation"]


def test_some_runs_only_is_nondeterministic():
    runs = [[session("a", disallowed=["a#1"])], [session("b")], [session("c")]]
    v = judge("t", runs, SPEC, None)
    assert (v.outcome, v.reason) == (Outcome.INCONCLUSIVE, NetworkReason.NONDETERMINISTIC)


def test_observed_violation_stands_despite_partial_coverage():
    runs = [[session(f"s{i}", coverage="partial", disallowed=[f"s{i}#1"])] for i in range(3)]
    assert judge("t", runs, SPEC, None).outcome is Outcome.VIOLATION


@pytest.mark.parametrize(
    ("coverage", "spec", "expected"),
    [
        ("unavailable", SPEC, NetworkReason.OBSERVATION_UNAVAILABLE),
        ("partial", SPEC, NetworkReason.PARTIAL_OBSERVATION),
        ("partial", NetworkSpec(coverage="best-effort"), None),
    ],
)
def test_missing_or_partial_observation_is_never_a_pass(coverage, spec, expected):
    runs = [[session(f"s{i}", coverage=coverage)] for i in range(3)]
    v = judge("t", runs, spec, None)
    if expected is None:
        assert v.outcome is Outcome.PASS
    else:
        assert (v.outcome, v.reason) == (Outcome.INCONCLUSIVE, expected)


def test_failed_sessions_are_inconclusive():
    runs = [[session("a")], [session("b", ok=False)], [session("c")]]
    assert judge("t", runs, SPEC, None).reason is NetworkReason.SETUP_FAILED
    assert judge("t", [], SPEC, None).reason is NetworkReason.SETUP_FAILED


def test_pass_requires_complete_observation_and_nothing_disallowed():
    runs = [[session(f"s{i}")] for i in range(3)]
    v = judge("t", runs, SPEC, "false")
    assert v.outcome is Outcome.PASS and v.reason is None and v.to_json()["remediation"] is None


# --- Configuration -------------------------------------------------------------------


BASE = """
[fixture]
name = "n"
command = ["{python}", "{target}/server.py"]
env = { KEY = "{canary}" }

[state]
locations = ["sqlite:state.db"]

[[adapters]]
kind = "sqlite"
label = "state.db"

[network]
policy = "deny"

[[tools]]
name = "t"
arguments = { secret = "{canary}" }

[tools.expected.network]
verdict = "PASS"
"""


def write(tmp_path, text):
    (tmp_path / "fixture.toml").write_text(text)
    return tmp_path


def test_network_section_parses_and_makes_the_fixture_sandboxed(tmp_path):
    fx = fixtures.load(write(tmp_path, BASE))
    assert fx.network == NetworkSpec() and fx.isolated
    assert fx.tools[0].expected_network == (Outcome.PASS, None)
    assert fx.server_env(tmp_path, canary=CANARY) == {"KEY": CANARY}
    assert fixtures.expand_arguments(fx.tools[0].arguments, CANARY) == {"secret": CANARY}


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('policy = "deny"', 'policy = "maybe"', "policy must be one of"),
        ('policy = "deny"', 'policy = "deny"\nallow = ["x:1"]', 'allow applies only to policy = "allowlist"'),
        ('policy = "deny"', 'policy = "allowlist"', "needs at least one allow entry"),
        ('policy = "deny"', 'policy = "deny"\ncoverage = "most"', "coverage must be one of"),
        ('policy = "deny"', 'policy = "deny"\ncapture = "yes"', "capture must be a boolean"),
        ('policy = "deny"', 'policy = "deny"\nfirewall = true', "unknown keys"),
        ('verdict = "PASS"', 'verdict = "NOT_APPLICABLE:X"', "X"),
        ('verdict = "PASS"', 'verdict = "INCONCLUSIVE"', "reason only for INCONCLUSIVE"),
    ],
)
def test_invalid_network_sections_are_rejected(tmp_path, old, new, message):
    with pytest.raises(fixtures.FixtureError, match=message):
        fixtures.load(write(tmp_path, BASE.replace(old, new)))


def test_network_expectations_need_a_network_section(tmp_path):
    text = BASE.replace('[network]\npolicy = "deny"\n', "").replace('env = { KEY = "{canary}" }\n', "")
    text = text.replace('arguments = { secret = "{canary}" }', "arguments = {}")
    with pytest.raises(fixtures.FixtureError, match="needs a \\[network\\] section"):
        fixtures.load(write(tmp_path, text))


def test_canary_placeholder_needs_a_canary(tmp_path):
    fx = fixtures.load(write(tmp_path, BASE))
    with pytest.raises(fixtures.FixtureError, match="canary"):
        fx.server_env(tmp_path)
    with pytest.raises(fixtures.FixtureError, match="canary"):
        fixtures.expand_arguments({"a": ["{canary}"]}, None)


# --- Sandbox ------------------------------------------------------------------------


def test_profile_allows_only_the_listed_loopback_ports_and_log_directory(tmp_path):
    profile = sandbox._profile(tmp_path / "state", [tmp_path / "logs"], [5555])
    assert '(allow network-outbound (remote ip "localhost:5555"))' in profile
    assert profile.index("(deny network*)") < profile.index("localhost:5555")  # the exception comes after the rule
    assert f'(subpath "{(tmp_path / "logs").resolve()}")' in profile
    assert "localhost:5556" not in sandbox._profile(tmp_path / "state")


def test_linux_namespaces_refuse_a_loopback_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sandbox, "isolation", lambda: sandbox.Isolation("unshare --net --map-root-user", "denied", "not confined")
    )
    with pytest.raises(sandbox.IsolationUnavailable, match="capture endpoint"):
        sandbox.isolated(["true"], tmp_path, loopback_ports=[5555])


def test_fixture_destinations_are_reserved_never_public():
    """The network fixture may only name TEST-NET-1 addresses, .invalid names and loopback."""
    import ipaddress
    import re

    source = (Path(__file__).parent.parent / "fixtures" / "network-side-effects" / "server.py").read_text()
    for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", source):
        addr = ipaddress.ip_address(ip)
        assert addr in ipaddress.ip_network("192.0.2.0/24") or addr.is_loopback, ip
    for host in re.findall(r"https?://([^/\"']+)", source):
        assert host.endswith(".invalid"), host
    for domain in re.findall(r'"([a-z0-9.-]+\.[a-z]{2,})"', source):
        assert domain.endswith((".invalid", ".py", ".db")), domain  # host names, not file names


def test_coverage_details_do_not_reveal_the_local_directory_layout(monkeypatch, tmp_path):
    from mcpgt.network import _display_path

    monkeypatch.chdir(tmp_path)
    assert _display_path(str(tmp_path / ".venv" / "bin" / "python3")) == ".venv/bin/python3"
    assert _display_path(str(Path.home() / "elsewhere" / "tool")) == "~/elsewhere/tool"
    assert _display_path("/usr/bin/curl") == "/usr/bin/curl"

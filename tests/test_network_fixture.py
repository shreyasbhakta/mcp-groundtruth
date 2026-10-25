"""The network test end to end, against fixtures/network-side-effects.

The positive and negative controls themselves are in test_ground_truth (every
tool's expected network verdict). These cover what surrounds them: refusing
to run without isolation, missing observation, the evidence in the report,
the capture endpoint's lifetime, and gating CI on a network VIOLATION.
"""

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt import observer as observers
from mcpgt import report as reports
from mcpgt import runner
from mcpgt.cli import main
from mcpgt.network import derive_network
from mcpgt.sandbox import IsolationUnavailable, isolation
from mcpgt.verdict import derive

FIXTURE = Path(__file__).parent.parent / "fixtures" / "network-side-effects"


def macos_sandbox() -> bool:
    try:
        return sys.platform == "darwin" and isolation().mechanism == "sandbox-exec"
    except IsolationUnavailable:
        return False


needs_macos_sandbox = pytest.mark.skipif(not macos_sandbox(), reason="the capture endpoint needs the macOS sandbox")


def quick(tmp_path, *tools):
    fx = fixtures.load(FIXTURE)
    return asyncio.run(runner.run_fixture(fx, tmp_path, only_tools=list(tools), concurrent_attempts=1))


def test_without_isolation_the_server_is_never_started(tmp_path, monkeypatch, capsys):
    def unavailable():
        raise IsolationUnavailable("no network isolation available (test)")

    started = []
    monkeypatch.setattr(runner, "isolation", unavailable)
    monkeypatch.setattr(runner.WireClient, "start", lambda *a, **k: started.append(a))
    assert main(["run", str(FIXTURE), "--runs-dir", str(tmp_path / "runs")]) == 2
    assert "no network isolation available" in capsys.readouterr().err
    assert started == [] and not (tmp_path / "runs").exists()


@needs_macos_sandbox
def test_unavailable_observer_makes_every_network_verdict_inconclusive(tmp_path, monkeypatch):
    def no_observer(*args, **kwargs):
        raise observers.ObserverUnavailable("no C compiler (test)")

    monkeypatch.setattr(observers, "build", no_observer)
    run = quick(tmp_path, "read_note", "read_note_and_beacon")
    verdicts = {v.tool: v for v in derive_network(run)}
    for tool in ("(server lifecycle)", "read_note", "read_note_and_beacon"):
        v = verdicts[tool]
        assert (v.outcome.value, v.reason.value) == ("INCONCLUSIVE", "OBSERVATION_UNAVAILABLE"), tool
        assert "no C compiler" in v.detail
    # The state-based verdicts are unaffected.
    read_only = [v for v in derive(run) if v.test.value == "read_only"]
    assert {v.outcome.value for v in read_only} == {"PASS"}
    # The capture endpoint still saw nothing: without the observer the server never reached for it.
    assert run.network is not None and run.network.observer_error == "no C compiler (test)"


@needs_macos_sandbox
def test_report_evidence_for_a_blocked_attempt_and_a_transmission(tmp_path):
    run = quick(tmp_path, "read_note_and_beacon", "read_note_and_upload")
    report = reports.build(run, derive(run), "net-test", audit=True)
    tools = {t["tool"]: t["network"] for t in report["tools"]}

    beacon = tools["read_note_and_beacon"]
    assert beacon["outcome"] == "VIOLATION" and beacon["declared_open_world"] == "false"
    (event, *_) = [e for s in beacon["sessions"] for e in s["events"]]
    # The positive control for the boundary: the sandbox refused the connection (EPERM).
    assert (event["destination"], event["outcome"], event["phase"]) == ("192.0.2.1:443", "blocked", "call 1")
    assert beacon["summary"]["transmitted"] == 0 and beacon["evidence"]

    upload = tools["read_note_and_upload"]
    sent = [e for s in upload["sessions"] for e in s["events"] if e["outcome"] == "transmitted"]
    assert sent and all(e["canary"] and e["destination"] == "collector.invalid:80" for e in sent)
    assert sent[0]["detail"]["headers"]["authorization"] == "<redacted>"

    text = json.dumps(report)
    assert "not-a-real-token" not in text
    assert text.count(run.network.canary) == 1  # only the run-level record of what was planted
    assert report["network"]["lifecycle"]["outcome"] == "PASS"
    assert "network: policy deny" in reports.render_text(report)


@needs_macos_sandbox
def test_capture_endpoint_is_closed_when_the_run_ends(tmp_path):
    run = quick(tmp_path, "read_note")
    assert run.network is not None and run.network.capture_port
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", run.network.capture_port), timeout=1)


@needs_macos_sandbox
def test_fail_on_violation_counts_network_violations(tmp_path, capsys):
    args = ["run", str(FIXTURE), "--audit", "--tool", "read_note_and_lookup", "--concurrent-attempts", "1"]
    args += ["--runs-dir", str(tmp_path / "runs")]
    assert main(args) == 0  # audit alone: a measurement
    assert main([*args, "--fail-on-violation"]) == 1
    assert "read_note_and_lookup network" in capsys.readouterr().err

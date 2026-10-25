"""The observer library itself: built for this host, loaded into real processes.

These run without the sandbox, so every destination is a closed port on
127.0.0.1: nothing here can leave the machine. They need a C compiler, and
run on Linux CI as well as macOS.
"""

import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mcpgt import observer as observers

needs_compiler = pytest.mark.skipif(
    not any(shutil.which(c) for c in ("cc", "clang", "gcc")) or sys.platform not in ("darwin", "linux"),
    reason="no C compiler, or an unsupported platform",
)

PROBE = textwrap.dedent(
    """
    import socket, subprocess, sys
    port = int(sys.argv[1])
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1)
    except OSError:
        pass
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        u.sendto(b"payload " + sys.argv[2].encode(), ("127.0.0.1", port))
    except OSError:
        pass
    socket.getaddrinfo("127.0.0.1", port)
    subprocess.run([sys.executable, "-c", "pass"], check=True)          # inherits the observer
    subprocess.run([sys.executable, "-c", "pass"], env={}, check=True)  # starts without it
    """
)


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])  # closed again once this returns


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return observers.build(tmp_path_factory.mktemp("netobs"))


@needs_compiler
def test_build_is_cached_by_source(tmp_path):
    first = observers.build(tmp_path)
    mtime = first.library.stat().st_mtime_ns
    second = observers.build(tmp_path)
    assert second.library == first.library and second.library.stat().st_mtime_ns == mtime
    assert len(first.to_json()["library_sha256"]) == 64


@needs_compiler
def test_observer_records_connects_sends_lookups_and_unobserved_children(built, tmp_path):
    log = tmp_path / "events.jsonl"
    port, canary = closed_port(), "mcpgt-canary-test"
    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    env = {"PATH": "/usr/bin:/bin", **built.environment(log, canary)}
    argv = [*built.command_prefix(), sys.executable, "-I", str(script), str(port), canary]
    subprocess.run(argv, env=env, check=True, timeout=60)

    events = observers.read_log(log)
    assert not events.missing and events.malformed == 0
    ops = [e.op for e in events.events]
    assert "hello" in ops
    (connect,) = [e for e in events.events if e.op == "connect" and e.fields.get("port") == port]
    assert connect.fields["ip"] == "127.0.0.1" and connect.fields["socket"] == "tcp"
    # A connect with a timeout is non-blocking: refused later, which the call itself does not show.
    assert observers.outcome(connect) in ("in-progress", "failed")
    (send,) = [e for e in events.events if e.op == "send"]
    assert send.fields["socket"] == "udp" and send.fields["canary"] is True and send.fields["bytes"] > len(canary)
    assert any(e.op == "resolve" and e.fields["host"] == "127.0.0.1" for e in events.events)
    execs = [e for e in events.events if e.op == "exec"]
    assert len(execs) >= 2, log.read_text()
    gaps = events.unobserved_children()
    assert len(gaps) == 1, log.read_text()  # the child started with an empty environment
    payload = log.read_text()
    assert "payload" not in payload  # lengths and the canary flag only, never bytes


def test_build_without_a_compiler_is_unavailable(tmp_path):
    with pytest.raises(observers.ObserverUnavailable):
        observers.build(tmp_path, compiler=str(tmp_path / "no-such-cc"))


def test_unsupported_platform_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(observers.sys, "platform", "win32")
    with pytest.raises(observers.ObserverUnavailable, match="not supported"):
        observers.build(tmp_path)


NODE_PROBE = """
const net = require("net");
const s = net.connect({ host: process.argv[2], port: Number(process.argv[3]) });
s.on("error", () => {});
setTimeout(() => process.exit(0), 1000);
"""


def node_binary():
    import os

    pinned = os.path.join(".mcpgt", "toolchains")
    for path in sorted(Path(pinned).glob("node-v*/bin/node")) if Path(pinned).is_dir() else []:
        return str(path)
    return shutil.which("node")


def run_node(built, tmp_path, host, port, sandboxed):
    from mcpgt.sandbox import isolated

    log = tmp_path / "node.jsonl"
    script = tmp_path / "probe.js"
    script.write_text(NODE_PROBE)
    argv = [*built.command_prefix(), node_binary(), str(script), host, str(port)]
    if sandboxed:
        argv = isolated(argv, tmp_path / "state", [tmp_path])
        (tmp_path / "state").mkdir()
    env = {"PATH": "/usr/bin:/bin", **built.environment(log, None)}
    subprocess.run(argv, env=env, check=True, timeout=60)
    return observers.read_log(log)


@needs_compiler
@pytest.mark.skipif(node_binary() is None, reason="no node on this host")
def test_node_servers_are_observed(built, tmp_path):
    """The audited servers are Node: their connects must reach the log too."""
    log = run_node(built, tmp_path, "127.0.0.1", closed_port(), sandboxed=False)
    assert log.observed_pids, "node never loaded the observer"
    assert any(e.op == "connect" and e.fields.get("ip") == "127.0.0.1" for e in log.events)


@needs_compiler
@pytest.mark.skipif(node_binary() is None or sys.platform != "darwin", reason="needs node and the macOS sandbox")
def test_node_attempt_inside_the_audit_sandbox_is_recorded_as_blocked(built, tmp_path):
    log = run_node(built, tmp_path, "192.0.2.1", 443, sandboxed=True)  # TEST-NET-1: never routed
    (connect,) = [e for e in log.events if e.op == "connect" and e.fields.get("ip") == "192.0.2.1"]
    assert observers.outcome(connect) == "blocked"

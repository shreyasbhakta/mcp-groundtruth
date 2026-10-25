"""Network observation: the preloaded observer library and its event log.

The sandbox (``sandbox.py``) is what keeps a server off the network. This
module only *records* what the server tried: ``netobs.c`` is compiled once
into ``.mcpgt/netobs/`` and preloaded into the server (``DYLD_INSERT_LIBRARIES``
on macOS, ``LD_PRELOAD`` on Linux), and writes one JSON line per network
operation to a per-session log outside the state directory.

Observation is best-effort and says so. A process that loaded the library
writes a ``hello`` line; a session with no hello was not observed at all, and
a child process started with no hello after its ``exec`` line is a coverage
gap. Neither is ever read as "no network activity".
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).with_name("netobs.c")
CACHE_DIR = Path(".mcpgt") / "netobs"
LOG_ENV = "MCPGT_NETOBS_LOG"
CANARY_ENV = "MCPGT_CANARY"
OPS = ("hello", "resolve", "connect", "send", "exec")
BLOCKED_ERRNOS = {1, 13}  # EPERM, EACCES: the sandbox refused the operation
IN_PROGRESS_ERRNOS = {36, 115}  # EINPROGRESS on macOS and Linux: non-blocking connect under way


class ObserverUnavailable(RuntimeError):
    """The observer library could not be built or cannot be loaded on this host."""


@dataclass(frozen=True, slots=True)
class Observer:
    library: Path
    mechanism: str  # "DYLD_INSERT_LIBRARIES" or "LD_PRELOAD"

    def to_json(self) -> dict[str, str]:
        return {"mechanism": self.mechanism, "library_sha256": _sha256(self.library)}

    def environment(self, log: Path, canary: str | None) -> dict[str, str]:
        """Variables the server needs. On macOS the preload variable is passed
        through ``/usr/bin/env`` instead (see ``command_prefix``), because the
        system strips DYLD_* variables when it starts sandbox-exec."""
        env = {LOG_ENV: str(log.resolve())}
        if canary:
            env[CANARY_ENV] = canary
        if self.mechanism == "LD_PRELOAD":
            env["LD_PRELOAD"] = str(self.library.resolve())
        return env

    def command_prefix(self) -> list[str]:
        if self.mechanism == "DYLD_INSERT_LIBRARIES":
            return ["/usr/bin/env", f"DYLD_INSERT_LIBRARIES={self.library.resolve()}"]
        return []


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(cache_dir: Path = CACHE_DIR, compiler: str | None = None) -> Observer:
    """Compile the observer for this host, reusing a build of the same source."""
    if sys.platform == "darwin":
        mechanism, suffix, flags = "DYLD_INSERT_LIBRARIES", "dylib", ["-dynamiclib"]
    elif sys.platform.startswith("linux"):
        mechanism, suffix, flags = "LD_PRELOAD", "so", ["-shared", "-fPIC", "-ldl"]
    else:
        raise ObserverUnavailable(f"network observation is not supported on {sys.platform}")
    cc = compiler or shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if cc is None:
        raise ObserverUnavailable("no C compiler (cc, clang or gcc) to build the network observer")
    digest = hashlib.sha256(SOURCE.read_bytes() + platform.machine().encode()).hexdigest()[:16]
    library = cache_dir / f"libmcpgt_netobs-{digest}.{suffix}"
    if library.is_file():
        return Observer(library, mechanism)
    cache_dir.mkdir(parents=True, exist_ok=True)
    partial = library.with_suffix(".partial")
    # -ldl must follow the source for the GNU linker.
    argv = [cc, "-O2", "-Wall", *[f for f in flags if f != "-ldl"], "-o", str(partial), str(SOURCE)]
    argv += [f for f in flags if f == "-ldl"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ObserverUnavailable(f"building the network observer failed: {exc}") from exc
    if proc.returncode != 0:
        raise ObserverUnavailable(f"building the network observer failed: {proc.stderr.strip()[:500]}")
    partial.replace(library)
    return Observer(library, mechanism)


# --- Events -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawEvent:
    """One line of an observer log, validated."""

    op: str
    pid: int
    ts: int  # CLOCK_REALTIME, nanoseconds
    fields: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ObserverLog:
    events: tuple[RawEvent, ...]
    malformed: int  # lines that were not a valid event
    missing: bool  # no log file at all: nothing loaded the observer

    @property
    def observed_pids(self) -> frozenset[int]:
        return frozenset(e.pid for e in self.events if e.op == "hello")

    def unobserved_children(self) -> tuple[RawEvent, ...]:
        """exec events whose program never said hello: started without the observer.

        An execve keeps the pid, a posix_spawn names the child's. Either way
        the program is observed only if that pid says hello after the exec.
        """
        gaps = []
        for e in self.events:
            if e.op != "exec" or int(e.fields.get("rc", 0)) != 0:
                continue
            child = int(e.fields.get("child", 0))
            if child <= 0:
                continue
            if not any(h.op == "hello" and h.pid == child and h.ts >= e.ts for h in self.events):
                gaps.append(e)
        return tuple(gaps)


def read_log(path: Path) -> ObserverLog:
    if not path.is_file():
        return ObserverLog((), 0, missing=True)
    events, malformed = [], 0
    for line in path.read_text(errors="replace").splitlines():
        event = parse_line(line)
        if event is None:
            malformed += line.strip() != ""
        else:
            events.append(event)
    events.sort(key=lambda e: e.ts)
    return ObserverLog(tuple(events), malformed, missing=False)


def parse_line(line: str) -> RawEvent | None:
    """A validated event, or None for anything else (truncated, forged or garbled)."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("op") not in OPS:
        return None
    pid, ts = data.get("pid"), data.get("ts")
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(ts, int) or isinstance(ts, bool):
        return None
    if data["op"] in ("connect", "send"):
        if not isinstance(data.get("family"), str) or not isinstance(data.get("errno"), int):
            return None
        if data["family"] in ("inet", "inet6") and not (
            isinstance(data.get("ip"), str) and isinstance(data.get("port"), int)
        ):
            return None
    if data["op"] == "resolve" and not isinstance(data.get("host"), str):
        return None
    fields = {k: v for k, v in data.items() if k not in ("op", "pid", "ts")}
    return RawEvent(data["op"], pid, ts, fields)


def outcome(event: RawEvent) -> str:
    """What happened to one attempt, as far as the call's own result shows.

    blocked      the sandbox refused it (EPERM or EACCES)
    connected    connect() succeeded at once
    in-progress  a non-blocking connect() started and was not refused; its
                 completion is not observed
    sent         sendto()/sendmsg() handed the bytes to the kernel
    resolved     getaddrinfo() returned addresses
    failed       any other error (refused, unreachable, no such name, ...)
    """
    if event.op == "resolve":
        return "resolved" if event.fields.get("rc") == 0 else "failed"
    if event.op not in ("connect", "send"):
        return "unknown"
    rc, err = event.fields.get("rc"), event.fields.get("errno")
    if err in BLOCKED_ERRNOS:
        return "blocked"
    if err in IN_PROGRESS_ERRNOS and event.op == "connect":
        return "in-progress"
    if isinstance(rc, int) and rc >= 0 and err == 0:
        return "connected" if event.op == "connect" else "sent"
    return "failed"

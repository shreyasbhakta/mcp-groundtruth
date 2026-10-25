"""Run a server process with no network access and confined file writes.

Audit targets are third-party code. During a run they get no network, and on
macOS they may write only inside the state directory, so any persistent state
they create lands where the snapshot adapters look. A write anywhere else
fails inside the server instead of becoming state nobody observed.

Mechanisms, chosen by platform:

* macOS: ``sandbox-exec`` with a profile denying all network operations and
  all file writes outside the state directory.
* Linux: ``unshare --net --map-root-user`` (a fresh network namespace with
  only loopback). File writes are not confined.

If no mechanism works, isolation is unavailable and an audit refuses to run.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


class IsolationUnavailable(RuntimeError):
    """No working network-isolation mechanism on this host."""


@dataclass(frozen=True, slots=True)
class Isolation:
    mechanism: str  # e.g. "sandbox-exec"
    network: str  # "denied"
    file_writes: str  # "confined to state directory" or "not confined"

    def to_json(self) -> dict[str, str]:
        return {"mechanism": self.mechanism, "network": self.network, "file_writes": self.file_writes}


def _quote(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def _profile(writable: Path, extra_writable: Sequence[Path] = (), loopback_ports: Sequence[int] = ()) -> str:
    subpaths = " ".join(f'(subpath "{_quote(p)}")' for p in (writable, *extra_writable))
    # Later rules take precedence: each allowed port is an exception to deny network*.
    ports = "".join(f'(allow network-outbound (remote ip "localhost:{int(p)}"))\n' for p in loopback_ports)
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        f"{ports}"
        "(deny file-write*)\n"
        f'(allow file-write* {subpaths} (literal "/dev/null") (literal "/dev/dtracehelper")'
        ' (regex #"^/dev/tty") (regex #"^/dev/fd/"))\n'
    )


@functools.cache
def _unshare_works() -> bool:
    unshare = shutil.which("unshare")
    if unshare is None:
        return False
    probe = subprocess.run([unshare, "--net", "--map-root-user", "true"], capture_output=True, check=False)
    return probe.returncode == 0


def isolation() -> Isolation:
    """The mechanism this host provides, or IsolationUnavailable."""
    if sys.platform == "darwin" and Path(SANDBOX_EXEC).exists():
        return Isolation("sandbox-exec", "denied", "confined to state directory")
    if sys.platform.startswith("linux") and _unshare_works():
        return Isolation("unshare --net --map-root-user", "denied", "not confined")
    raise IsolationUnavailable(
        "no network isolation available (needs macOS sandbox-exec, or Linux unprivileged user namespaces)"
    )


def isolated(
    command: Sequence[str],
    state_dir: Path,
    extra_writable: Sequence[Path] = (),
    loopback_ports: Sequence[int] = (),
) -> list[str]:
    """``command`` wrapped so it runs without network (and, on macOS, can only write under ``state_dir``).

    ``extra_writable`` adds directories it may write (the observer's logs), and
    ``loopback_ports`` the 127.0.0.1 ports it may connect to (the local capture
    endpoint). A Linux network namespace cannot reach a port on the host's
    loopback, so asking for one there is IsolationUnavailable, not a silent
    widening.
    """
    mechanism = isolation().mechanism
    if mechanism == "sandbox-exec":
        return [SANDBOX_EXEC, "-p", _profile(state_dir, extra_writable, loopback_ports), *command]
    if loopback_ports:
        raise IsolationUnavailable(
            "a local capture endpoint is not reachable from a Linux network namespace; "
            "set [network].capture = false, or run on macOS"
        )
    unshare = shutil.which("unshare")
    assert unshare is not None
    return [unshare, "--net", "--map-root-user", *command]

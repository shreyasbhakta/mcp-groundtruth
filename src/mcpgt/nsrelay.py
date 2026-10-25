"""Capture relay for the Linux network namespace.

A server sandboxed with ``unshare --net`` sees only its own, empty network
namespace: it cannot reach the harness's capture endpoint on the host's
loopback. This relay runs *inside* the namespace, as the server's parent:

    unshare --net --map-root-user python -m mcpgt.nsrelay \\
        --port P --unix /tmp/.../capture.sock -- <server command>

It brings the namespace's loopback interface up, listens on 127.0.0.1:P,
starts the server, and copies every connection to the harness's capture
endpoint over a Unix socket. Unix sockets are files, so they cross network
namespaces; nothing else does, and the relay never opens a network
connection of its own.

The relay is not observed: it is started without the observer, and passes
the observer to the server only (``MCPGT_RELAY_LD_PRELOAD`` becomes the
server's ``LD_PRELOAD``), so its own traffic never appears as evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
from collections.abc import Sequence

SIOCGIFFLAGS, SIOCSIFFLAGS, IFF_UP = 0x8913, 0x8914, 0x1
PRELOAD_ENV = "MCPGT_RELAY_LD_PRELOAD"


def loopback_up() -> None:
    """Bring ``lo`` up: a new network namespace starts with it down."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        ifreq = struct.pack("16sH14s", b"lo", 0, b"")
        flags = struct.unpack("16sH14s", fcntl.ioctl(s.fileno(), SIOCGIFFLAGS, ifreq))[1]
        fcntl.ioctl(s.fileno(), SIOCSIFFLAGS, struct.pack("16sH14s", b"lo", flags | IFF_UP, b""))


def _pump(src: socket.socket, dst: socket.socket) -> None:
    with contextlib.suppress(OSError):
        while chunk := src.recv(65536):
            dst.sendall(chunk)
    with contextlib.suppress(OSError):
        dst.shutdown(socket.SHUT_WR)


def relay(conn: socket.socket, unix_path: str) -> None:
    """Copy one connection to the capture endpoint and back."""
    with conn, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
        try:
            upstream.connect(unix_path)
        except OSError:
            return
        back = threading.Thread(target=_pump, args=(upstream, conn), daemon=True)
        back.start()
        _pump(conn, upstream)
        back.join(timeout=5)


def serve(listener: socket.socket, unix_path: str) -> None:
    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=relay, args=(conn, unix_path), daemon=True).start()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mcpgt.nsrelay")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--unix", required=True)
    parser.add_argument("--no-loopback-setup", action="store_true", help="for tests outside a namespace")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no server command after --")
    if not args.no_loopback_setup:
        loopback_up()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", args.port))
    listener.listen(64)
    threading.Thread(target=serve, args=(listener, args.unix), daemon=True).start()

    env = dict(os.environ)
    preload = env.pop(PRELOAD_ENV, None)
    if preload:
        env["LD_PRELOAD"] = preload
    # The server inherits stdin and stdout: it speaks MCP to the harness directly.
    child = subprocess.Popen(command, env=env)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda signum, _frame: child.send_signal(signum))
    code = child.wait()
    listener.close()
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())

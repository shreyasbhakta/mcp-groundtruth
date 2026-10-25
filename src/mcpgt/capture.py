"""A local capture endpoint: a sink that records what reached it.

It listens on 127.0.0.1 only and never forwards anything anywhere. The
sandbox lets the server reach this one port and nothing else, so it stands in
for every external destination: it is the HTTP(S) proxy the server is told to
use, and its URL is passed to the server as ``MCPGT_CAPTURE_URL``.

Reaching it is *transmission*, not just an attempt: the bytes arrived. What
is recorded is metadata (method, host, path without its query string, header
names, sizes) and whether the bytes contained the run's synthetic canary.
Header values are kept only for a short list of harmless headers; payloads
are never stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any

READ_LIMIT = 64 * 1024
READ_TIMEOUT = 2.0
KEPT_HEADERS = frozenset({"host", "content-type", "content-length", "user-agent", "accept", "connection"})
REDACTED = "<redacted>"


@dataclass(frozen=True, slots=True)
class CaptureEvent:
    ts: int  # CLOCK_REALTIME nanoseconds when the connection arrived
    peer_port: int
    kind: str  # "http", "proxy-connect" (an HTTPS proxy request), "raw" (not HTTP)
    bytes: int
    canary: bool
    method: str | None = None
    host: str | None = None  # Host header, CONNECT target or absolute-URI host
    port: int | None = None
    path: str | None = None  # without the query string
    query_redacted: bool = False
    headers: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def redact_headers(lines: list[str]) -> dict[str, str]:
    """Header names, with values only for KEPT_HEADERS."""
    out: dict[str, str] = {}
    for line in lines:
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key = name.strip().lower()
        out[key] = value.strip()[:200] if key in KEPT_HEADERS else REDACTED
    return out


def _split_target(target: str) -> tuple[str | None, int | None, str, bool]:
    """(host, port, path, query_redacted) from a request target."""
    host = port = None
    rest = target
    for scheme, default in (("http://", 80), ("https://", 443)):
        if target.lower().startswith(scheme):
            authority, slash, rest = target[len(scheme) :].partition("/")
            rest = slash + rest
            host, port = _host_port(authority, default)
            break
    path, q, _ = rest.partition("?")
    return host, port, path or "/", bool(q)


def _host_port(authority: str, default: int | None) -> tuple[str | None, int | None]:
    authority = authority.rpartition("@")[2]  # never keep userinfo
    if authority.startswith("["):  # an IPv6 literal: [::1]:8080
        host, _, rest = authority[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, sep, port = authority.rpartition(":")
        if not sep:
            host, port = authority, ""
    return host or None, int(port) if port.isdigit() else default


def parse(data: bytes, canary: str | None, ts: int, peer_port: int) -> CaptureEvent:
    """What one connection delivered, as metadata."""
    found = canary is not None and canary != "" and canary.encode() in data
    head, _, _ = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ") if lines else []
    if len(parts) == 3 and parts[2].startswith("HTTP/"):
        method, target = parts[0].upper(), parts[1]
        headers = redact_headers(lines[1:])
        if method == "CONNECT":
            host, port = _host_port(target, 443)
            return CaptureEvent(
                ts, peer_port, "proxy-connect", len(data), found, method, host, port, None, False, headers
            )
        host, port, path, redacted = _split_target(target)
        if host is None and "host" in headers:
            host, port = _host_port(headers["host"], None)
        return CaptureEvent(ts, peer_port, "http", len(data), found, method, host, port, path, redacted, headers)
    return CaptureEvent(ts, peer_port, "raw", len(data), found)


class CaptureServer:
    """The endpoint. ``async with CaptureServer(canary) as cap:`` binds it; leaving closes it."""

    def __init__(self, canary: str | None = None) -> None:
        self.canary = canary
        self.events: list[CaptureEvent] = []
        self._server: asyncio.Server | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> CaptureServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ts = time.time_ns()
        peer = writer.get_extra_info("peername") or ("", 0)
        buf = bytearray()
        # A slow or silent client still leaves whatever it sent in buf.
        with contextlib.suppress(TimeoutError, ConnectionError):
            await asyncio.wait_for(self._read(reader, buf), READ_TIMEOUT)
        event = parse(bytes(buf), self.canary, ts, int(peer[1]))
        self.events.append(event)
        reply = {
            "http": b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            "proxy-connect": b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        }.get(event.kind, b"")
        with contextlib.suppress(Exception):
            writer.write(reply)
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _read(self, reader: asyncio.StreamReader, buf: bytearray) -> None:
        """Headers, then as much body as Content-Length announces (capped), into buf."""
        while b"\r\n\r\n" not in buf and len(buf) < READ_LIMIT:
            chunk = await reader.read(4096)
            if not chunk:
                return
            buf += chunk
        head, _, body = bytes(buf).partition(b"\r\n\r\n")
        length = 0
        for line in head.decode("latin-1").lower().split("\r\n"):
            if line.startswith("content-length:") and line[15:].strip().isdigit():
                length = int(line[15:].strip())
        want = min(len(buf) + max(0, length - len(body)), READ_LIMIT)
        while len(buf) < want:
            chunk = await reader.read(min(4096, want - len(buf)))
            if not chunk:
                return
            buf += chunk

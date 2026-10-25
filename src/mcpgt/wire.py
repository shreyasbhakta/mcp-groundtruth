"""Minimal MCP client over stdio that keeps every response line verbatim.

Deliberately not the SDK client: the SDK coerces annotation values, edits
results, and fails a whole listing on one malformed tool (docs/methodology.md).
This client parses nothing beyond JSON-RPC framing and hands back the exact
text each response arrived as.

Stdlib only. Requests may be issued concurrently on one connection; responses
are routed by id.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcpgt import __version__

PROTOCOL_VERSION = "2025-11-25"
CLIENT_INFO = {"name": "mcpgt", "version": __version__}
MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_LIST_PAGES = 1000


class WireError(RuntimeError):
    """The connection failed: server exited, sent garbage, or timed out."""


@dataclass(frozen=True, slots=True)
class RawResponse:
    text: str  # the response line exactly as received, minus its newline
    message: Mapping[str, Any]

    @property
    def is_error(self) -> bool:
        return "error" in self.message

    @property
    def result(self) -> Mapping[str, Any] | None:
        result = self.message.get("result")
        return result if isinstance(result, Mapping) else None


class WireClient:
    def __init__(self, process: asyncio.subprocess.Process, timeout: float) -> None:
        self._process = process
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[RawResponse]] = {}
        self._stderr = bytearray()
        self._closed: BaseException | None = None
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr_reader = asyncio.create_task(self._read_stderr())
        self.initialize_response: RawResponse | None = None

    @classmethod
    async def start(
        cls,
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path | None = None,
        timeout: float = 10.0,
    ) -> WireClient:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            limit=MAX_LINE_BYTES,
        )
        return cls(process, timeout)

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def stderr(self) -> str:
        return self._stderr.decode("utf-8", "replace")

    # --- MCP operations ---------------------------------------------------

    async def initialize(self) -> RawResponse:
        response = await self.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
        )
        if response.result is None:
            raise WireError(f"initialize failed: {response.text}")
        await self.notify("notifications/initialized")
        self.initialize_response = response
        return response

    async def list_tools(self) -> list[RawResponse]:
        """Every tools/list page, following nextCursor to the end."""
        pages: list[RawResponse] = []
        cursors: set[str] = set()
        cursor: str | None = None
        while True:
            page = await self.request("tools/list", {} if cursor is None else {"cursor": cursor})
            pages.append(page)
            result = page.result
            cursor = None if result is None else result.get("nextCursor")
            if not isinstance(cursor, str):
                return pages
            if cursor in cursors or len(pages) >= MAX_LIST_PAGES:
                raise WireError(f"tools/list pagination does not terminate (cursor {cursor!r})")
            cursors.add(cursor)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> RawResponse:
        return await self.request("tools/call", {"name": name, "arguments": dict(arguments)})

    # --- JSON-RPC ---------------------------------------------------------

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> RawResponse:
        request_id = next(self._ids)
        future: asyncio.Future[RawResponse] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        try:
            await self._send(message)
            return await asyncio.wait_for(future, self._timeout)
        except TimeoutError as exc:
            raise WireError(f"{method}: no response within {self._timeout}s") from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._send(message)

    async def _send(self, message: Mapping[str, Any]) -> None:
        if self._closed is not None:
            raise WireError(f"connection closed: {self._closed}") from self._closed
        stdin = self._process.stdin
        assert stdin is not None
        try:
            stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise WireError("server closed its stdin") from exc

    async def _read_stdout(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        try:
            while line := await stdout.readline():
                text = line.decode("utf-8").removesuffix("\n").removesuffix("\r")
                if text.strip():
                    await self._dispatch(text)
            self._fail(WireError(f"server exited (stderr: {self.stderr[-2000:]!r})"))
        except Exception as exc:  # malformed framing, bad UTF-8, oversized line
            self._fail(WireError(f"unreadable server output: {exc}"))

    async def _dispatch(self, text: str) -> None:
        message = json.loads(text)
        if not isinstance(message, dict):
            raise WireError(f"not a JSON-RPC message: {text[:200]}")
        if "method" in message:
            if "id" in message:  # server-to-client request: answer ping, refuse the rest
                if message["method"] == "ping":
                    reply = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
                else:
                    reply = {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "not supported by mcpgt"},
                    }
                await self._send(reply)
            return  # notifications are ignored
        future = self._pending.get(message.get("id"))  # type: ignore[arg-type]
        if future is not None and not future.done():
            future.set_result(RawResponse(text=text, message=message))

    async def _read_stderr(self) -> None:
        stderr = self._process.stderr
        assert stderr is not None
        while chunk := await stderr.read(65536):
            self._stderr.extend(chunk)

    def _fail(self, exc: BaseException) -> None:
        self._closed = exc
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)

    async def close(self) -> int:
        """Close stdin, wait for exit, kill if it lingers. Returns the exit code."""
        stdin = self._process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await stdin.wait_closed()
        try:
            await asyncio.wait_for(self._process.wait(), self._timeout)
        except TimeoutError:
            self._process.kill()
            await self._process.wait()
        for task in (self._reader, self._stderr_reader):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(task, self._timeout)
        return self._process.returncode  # type: ignore[return-value]

    async def __aenter__(self) -> WireClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
